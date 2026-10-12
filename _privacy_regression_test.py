# -*- coding: utf-8 -*-
"""私密区存储故障回归(阶段 1)。

Run: .venv/Scripts/python.exe -X utf8 _privacy_regression_test.py
全部路径临时隔离、真 DPAPI、安全模式核心(无线程/热键),失败按注入点模拟。
覆盖:
  1. 加载状态:缺失 / 合法空加密 / 旧明文 / 新 DPAPI / 主文件损坏从备份恢复
     (损坏原件改名留档且不重复制造)/ 全部不可读
  2. 不可读状态:save_all / 自动捕获追加 / 导入合并 / UI 删除均不覆写原文件
  3. 保存失败路径:DPAPI 保护失败(不降级明文)/ 临时文件失败 / 备份轮转失败 /
     os.replace 失败——原文件保留、结果非 ok、可重试
  4. 自动捕获保存失败:内存保留 + 待重试,重试后正确落盘
  5. 删除/清空失败:恢复原列表,无成功假象
  6. 旧明文迁移:主文件与备份链无明文残留;迁移失败保留明文原件
  7. 备份轮转链:bak1~3 新→旧、上限截断、损坏主文件不进备份链
  8. 破坏性清空:主文件 + 备份 + 损坏留档一并清除
  9. 日志与错误摘要不含私密正文
"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clipboard_manager as cm

_SECRET = "TOPSECRET-正文-12345"


class PrivacyRegression(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_priv_regr_")
        root = self.tmp.name
        paths = {"APP_DIR": root}
        for key, name in (("IMAGE_DIR", "images"), ("BLOB_DIR", "data/blobs"),
                          ("HISTORY_FILE", "history.json"),
                          ("DB_FILE", "clipboard.db"),
                          ("PRIVATE_FILE", "private.json"),
                          ("CONFIG_FILE", "config.json"),
                          ("LOG_FILE", "app.log"), ("ICON_FILE", "icon.ico"),
                          ("BOOT_STATE_FILE", "boot_state.json")):
            paths[key] = os.path.join(root, name)
        self.paths = patch.multiple(cm, **paths)
        self.paths.start()
        cm.ensure_dirs()
        cm.save_config(dict(cm.DEFAULT_CFG, sensitive_guard=False))
        self.cores = []
        self.assertTrue(cm.dpapi_available(), "本套件需真实 DPAPI 环境")

    def tearDown(self):
        for core in self.cores:
            if core.store is not None:
                core.store.close()
            core._capture_exec.shutdown(wait=True)
        self.paths.stop()
        self.tmp.cleanup()

    # ---- 工具 ----
    def core(self, pre_private=None):
        """构造安全模式核心;pre_private 在构造前写入私密区文件。"""
        if pre_private is not None:
            if isinstance(pre_private, (bytes, bytearray)):
                Path(cm.PRIVATE_FILE).write_bytes(pre_private)
            else:
                self.assertTrue(cm.save_private(pre_private).ok)
        core = cm.ClipboardCore(safe_mode=True)
        core._schedule_gc = lambda: None
        self.cores.append(core)
        return core

    def main_bytes(self):
        return Path(cm.PRIVATE_FILE).read_bytes()

    def log_text(self):
        try:
            return Path(cm.LOG_FILE).read_text(encoding="utf-8",
                                               errors="replace")
        except Exception:
            return ""

    @staticmethod
    def garbage_json():
        return json.dumps({"dpapi": True, "data": base64.b64encode(
            b"definitely-not-ciphertext").decode()}).encode("utf-8")

    # ---- 1. 加载状态 ----
    def test_load_missing(self):
        core = self.core()
        st = core.private_state()
        self.assertEqual((st.status, st.entries), ("missing", []))

    def test_load_dpapi_and_empty(self):
        core = self.core(pre_private=[])
        st = core.private_state()
        self.assertEqual((st.status, st.mode, st.entries),
                         ("ok", "dpapi", []))
        core2 = self.core(pre_private=[_SECRET])
        st2 = core2.private_state()
        self.assertEqual((st2.status, st2.mode, st2.entries),
                         ("ok", "dpapi", [_SECRET]))

    def test_load_legacy_plaintext(self):
        Path(cm.PRIVATE_FILE).write_text('["旧明文1", "旧明文2"]',
                                         encoding="utf-8")
        st = cm.load_private_status()          # 构造核心前:明文可见
        self.assertEqual((st.status, st.mode), ("ok", "plaintext"))
        self.assertEqual(st.entries, ["旧明文1", "旧明文2"])
        core = self.core()                     # 启动迁移:明文 → DPAPI
        st2 = core.private_state()
        self.assertEqual((st2.status, st2.mode), ("ok", "dpapi"))
        self.assertEqual(st2.entries, ["旧明文1", "旧明文2"])

    def test_recover_from_backup_keeps_corrupt(self):
        self.assertTrue(cm.save_private(["备份内容"]).ok)   # 主文件(好)
        Path(cm.PRIVATE_FILE + ".bak1").write_bytes(self.main_bytes())
        garbage = self.garbage_json()
        Path(cm.PRIVATE_FILE).write_bytes(garbage)          # 主文件损坏
        st = cm.load_private_status()
        self.assertEqual((st.status, st.entries),
                         ("recovered", ["备份内容"]))
        self.assertEqual(st.source, "private.json.bak1")
        corrupt = cm.PRIVATE_FILE + ".corrupt"
        self.assertTrue(Path(corrupt).exists())
        self.assertEqual(Path(corrupt).read_bytes(), garbage)   # 留档可追溯
        # 再加载:损坏主文件已被隔离(不存在)→ missing。
        # 刻意语义:备份只救援"损坏的主文件",不救援"被删除的主文件"
        # (用户手动删除 private.json 视为清除意图,不得从备份复活秘密)。
        st2 = cm.load_private_status()
        self.assertEqual(st2.status, "missing")

    def test_all_unreadable(self):
        Path(cm.PRIVATE_FILE).write_bytes(self.garbage_json())
        for i in (1, 2, 3):
            Path("%s.bak%d" % (cm.PRIVATE_FILE, i)).write_bytes(
                self.garbage_json())
        st = cm.load_private_status()
        self.assertEqual((st.status, st.entries), ("unreadable", []))
        self.assertTrue(st.error)

    # ---- 2. 不可读状态不覆写 ----
    def test_unreadable_save_all_preserves_bytes(self):
        core = self.core(pre_private=self.garbage_json())
        self.assertEqual(core.private_state().status, "unreadable")
        before = self.main_bytes()
        core.save_all()                                     # 退出路径
        self.assertEqual(self.main_bytes(), before)
        self.assertEqual(cm.load_private_status().status, "unreadable")

    def test_unreadable_blocks_write_paths(self):
        core = self.core(pre_private=self.garbage_json())
        before = self.main_bytes()
        # 自动捕获追加
        core._add_private(_SECRET)
        self.assertIn(_SECRET, core.private)
        self.assertTrue(core._private_dirty)
        self.assertEqual(self.main_bytes(), before)
        # 导入合并
        core.import_portable_payload({"items": [], "private": ["导入的秘密"]})
        self.assertIn("导入的秘密", core.private)
        self.assertEqual(self.main_bytes(), before)
        # UI 删除
        ok, err = core.delete_private(0)
        self.assertFalse(ok)
        self.assertIn(_SECRET, core.private)
        self.assertEqual(self.main_bytes(), before)

    # ---- 3. 保存失败路径 ----
    def test_save_dpapi_failure_no_plaintext_downgrade(self):
        self.assertTrue(cm.save_private(["已有"]).ok)
        before = self.main_bytes()
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            res = cm.save_private(["已有", _SECRET])
        self.assertFalse(res.ok)
        self.assertFalse(res.encrypted)
        self.assertEqual(self.main_bytes(), before)          # 原密文保持
        self.assertNotIn(base64.b64encode(_SECRET.encode()).decode(),
                         self.main_bytes().decode("ascii", "replace"))

    def test_save_tmp_write_failure_retryable(self):
        self.assertTrue(cm.save_private(["v1"]).ok)
        before = self.main_bytes()
        with patch.object(cm, "_private_write_payload",
                          side_effect=OSError("disk full")):
            res = cm.save_private(["v1", "v2"])
        self.assertFalse(res.ok)
        self.assertEqual(self.main_bytes(), before)
        self.assertFalse(os.path.exists(cm.PRIVATE_FILE + ".tmp"))
        res2 = cm.save_private(["v1", "v2"])                 # 重试成功
        self.assertTrue(res2.ok)
        self.assertEqual(cm.load_private(), ["v1", "v2"])

    def test_save_rotation_failure_preserves(self):
        self.assertTrue(cm.save_private(["v1"]).ok)
        before = self.main_bytes()
        with patch.object(cm, "_private_rotate_backups",
                          return_value=(False, "注入的轮转失败")):
            res = cm.save_private(["v2"])
        self.assertFalse(res.ok)
        self.assertIn("轮转", res.error)
        self.assertEqual(self.main_bytes(), before)
        self.assertFalse(os.path.exists(cm.PRIVATE_FILE + ".tmp"))

    def test_save_replace_failure_retryable(self):
        self.assertTrue(cm.save_private(["v1"]).ok)
        before = self.main_bytes()
        with patch.object(cm.os, "replace", side_effect=OSError("注入失败")):
            res = cm.save_private(["v2"])
        self.assertFalse(res.ok)
        self.assertEqual(self.main_bytes(), before)
        self.assertFalse(os.path.exists(cm.PRIVATE_FILE + ".tmp"))
        self.assertTrue(cm.save_private(["v2"]).ok)          # 可重试
        self.assertEqual(cm.load_private(), ["v2"])

    # ---- 4. 自动捕获失败 → 重试恢复 ----
    def test_auto_capture_pending_retry(self):
        core = self.core(pre_private=[])
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            core._add_private(_SECRET)
        self.assertEqual(core.private, [_SECRET])
        self.assertTrue(core._private_dirty)
        self.assertIn("DPAPI", core.private_save_error)      # 如实的失败原因
        self.assertEqual(cm.load_private(), [])              # 未落盘
        res = core._private_save()                           # 重试(环境恢复)
        self.assertTrue(res.ok)
        self.assertEqual(cm.load_private(), [_SECRET])
        self.assertFalse(core._private_dirty)

    # ---- 5. 删除/清空失败恢复原列表 ----
    def test_delete_failure_restores(self):
        core = self.core(pre_private=["A", "B"])
        before = self.main_bytes()
        with patch.object(cm.os, "replace", side_effect=OSError("注入失败")):
            ok, err = core.delete_private(0)
        self.assertFalse(ok)
        self.assertEqual(core.private, ["A", "B"])           # 原列表恢复
        self.assertEqual(self.main_bytes(), before)          # 磁盘一致

    def test_clear_failure_restores(self):
        core = self.core(pre_private=["A", "B"])
        before = self.main_bytes()
        with patch.object(cm.os, "replace", side_effect=OSError("注入失败")):
            ok, err = core.clear_private()
        self.assertFalse(ok)
        self.assertEqual(core.private, ["A", "B"])
        self.assertEqual(self.main_bytes(), before)

    # ---- 6. 明文迁移无残留 ----
    def test_plaintext_migration_no_residue(self):
        marker = "PLAINTEXT-MARKER-9527"
        Path(cm.PRIVATE_FILE).write_text(json.dumps([marker]),
                                         encoding="utf-8")
        core = self.core()                                   # 启动即迁移
        st = core.private_state()
        self.assertEqual((st.status, st.mode), ("ok", "dpapi"))
        blobs = [self.main_bytes()] + [
            Path("%s.bak%d" % (cm.PRIVATE_FILE, i)).read_bytes()
            for i in (1, 2, 3)
            if os.path.exists("%s.bak%d" % (cm.PRIVATE_FILE, i))]
        self.assertTrue(blobs)
        for b in blobs:
            self.assertNotIn(marker.encode(), b)             # 正式副本无明文
        self.assertEqual(cm.load_private(), [marker])

    def test_plaintext_migration_failure_keeps_source(self):
        marker = "PLAINTEXT-MARKER-3311"
        Path(cm.PRIVATE_FILE).write_text(json.dumps([marker]),
                                         encoding="utf-8")
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            core = self.core()
        st = core.private_state()
        self.assertEqual((st.status, st.mode), ("ok", "plaintext"))
        self.assertIn(marker, self.main_bytes().decode("utf-8",
                                                       "replace"))  # 原件保留

    # ---- 7. 备份链轮转 ----
    def test_backup_chain_rotation_and_cap(self):
        for v in ("A", "B", "C", "D", "E"):
            self.assertTrue(cm.save_private([v]).ok)
        self.assertEqual(cm.load_private(), ["E"])
        baks = [cm._private_parse_file("%s.bak%d" % (cm.PRIVATE_FILE, i))
                for i in (1, 2, 3)]
        self.assertEqual([b[0][0] if b else None for b in baks],
                         ["D", "C", "B"])                    # 新→旧,最多 3 份
        self.assertFalse(os.path.exists(cm.PRIVATE_FILE + ".bak4"))

    def test_corrupt_main_not_rotated_into_backups(self):
        self.assertTrue(cm.save_private(["好数据"]).ok)
        bak1 = cm.PRIVATE_FILE + ".bak1"
        Path(bak1).write_bytes(self.main_bytes())
        garbage = b"garbage-bytes-main"
        Path(cm.PRIVATE_FILE).write_bytes(garbage)
        ok, err = cm._private_rotate_backups()               # 损坏主文件在位
        self.assertTrue(ok)
        self.assertEqual(Path(bak1).read_bytes(),
                         Path(bak1).read_bytes())            # bak1 未被垃圾覆盖
        got = cm._private_parse_file(bak1)
        self.assertEqual(got[0], ["好数据"])
        # 模块级保存(破坏性路径放行后)覆盖损坏主文件,垃圾不进链
        self.assertTrue(cm.save_private(["新内容"]).ok)
        for i in (1, 2, 3):
            p = "%s.bak%d" % (cm.PRIVATE_FILE, i)
            if os.path.exists(p):
                self.assertNotEqual(Path(p).read_bytes(), garbage)
        self.assertEqual(cm.load_private(), ["新内容"])

    # ---- 8. 破坏性清空清备份 ----
    def test_destructive_clear_removes_backups(self):
        for v in ("A", "B"):
            self.assertTrue(cm.save_private([v]).ok)
        self.assertTrue(os.path.exists(cm.PRIVATE_FILE + ".bak1"))
        Path(cm.PRIVATE_FILE + ".corrupt").write_bytes(b"x")
        core = self.core()
        ok, err = core.clear_private()
        self.assertTrue(ok, err)
        self.assertEqual(cm.load_private(), [])
        for i in (1, 2, 3):
            self.assertFalse(os.path.exists("%s.bak%d" % (cm.PRIVATE_FILE, i)))
        self.assertFalse(os.path.exists(cm.PRIVATE_FILE + ".corrupt"))

    def test_destructive_clear_on_unreadable(self):
        core = self.core(pre_private=self.garbage_json())
        self.assertEqual(core.private_state().status, "unreadable")
        core._add_private("新捕获")                           # 只进内存
        ok, err = core.clear_private()                       # 显式破坏性清空
        self.assertTrue(ok, err)
        self.assertEqual(cm.load_private(), [])
        core2 = self.core()
        self.assertEqual(core2.private_state().status, "ok")  # 恢复可写

    # ---- 9. 日志/摘要不含正文 ----
    def test_logs_and_errors_have_no_secret_body(self):
        core = self.core(pre_private=[])
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            core._add_private(_SECRET)
            res = cm.save_private([_SECRET])
        errs = [res.error, core.private_save_error]
        with patch.object(cm.os, "replace", side_effect=OSError("x")):
            ok, err = core.delete_private(0)
            errs.append(err)
        errs.append(core.private_state().error)
        texts = "\n".join(str(e) for e in errs) + "\n" + self.log_text()
        self.assertNotIn(_SECRET, texts)
        self.assertNotIn("正文-12345", texts)


class ConfigEncryptionRegression(unittest.TestCase):
    """阶段 4:密钥加密的实际状态与失败保留(不静默降级明文)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_cfgenc_")
        root = self.tmp.name
        paths = {"APP_DIR": root}
        for key, name in (("IMAGE_DIR", "images"), ("BLOB_DIR", "data/blobs"),
                          ("HISTORY_FILE", "history.json"),
                          ("DB_FILE", "clipboard.db"),
                          ("PRIVATE_FILE", "private.json"),
                          ("CONFIG_FILE", "config.json"),
                          ("LOG_FILE", "app.log"), ("ICON_FILE", "icon.ico"),
                          ("BOOT_STATE_FILE", "boot_state.json")):
            paths[key] = os.path.join(root, name)
        self.paths = patch.multiple(cm, **paths)
        self.paths.start()
        cm.ensure_dirs()
        self.assertTrue(cm.dpapi_available())

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    KEY = "sk-plain-secret-9527"

    def test_enc_field_no_plaintext_fallback(self):
        self.assertIsNotNone(cm._enc_field(self.KEY))          # 正常 → 密文 dict
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            self.assertIsNone(cm._enc_field(self.KEY))         # 失败 → None

    def test_save_config_success_encrypted_roundtrip(self):
        cfg = dict(cm.DEFAULT_CFG)
        cfg["ai_services"] = [{"name": "S", "base_url": "http://x",
                               "model": "m", "api_key": self.KEY}]
        self.assertTrue(cm.save_config(cfg))
        raw = Path(cm.CONFIG_FILE).read_text(encoding="utf-8")
        self.assertIn('"dpapi": true', raw)
        self.assertNotIn(self.KEY, raw)                        # 无明文
        loaded = cm.load_config()
        self.assertEqual(loaded["ai_services"][0]["api_key"], self.KEY)

    def test_save_config_failure_preserves_file_and_backups(self):
        # 先落一份有效加密配置
        cfg = dict(cm.DEFAULT_CFG)
        cfg["ai_services"] = [{"name": "S", "base_url": "http://x",
                               "model": "m", "api_key": self.KEY}]
        self.assertTrue(cm.save_config(cfg))
        before = Path(cm.CONFIG_FILE).read_bytes()
        # DPAPI 失败 → 保存整体中止,文件与备份不动、无明文副本
        cfg["ai_services"][0]["api_key"] = "sk-second-secret-1357"
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            self.assertFalse(cm.save_config(cfg))
        self.assertEqual(Path(cm.CONFIG_FILE).read_bytes(), before)
        for suffix in (".bak1", ".bak2", ".bak3", ".tmp"):
            p = Path(cm.CONFIG_FILE + suffix)
            if p.exists():
                self.assertNotIn("sk-second-secret-1357",
                                 p.read_text(encoding="utf-8"))
        self.assertFalse(Path(cm.CONFIG_FILE + ".tmp").exists())
        # 已有有效配置仍可恢复:重新加载得到旧密钥
        self.assertEqual(cm.load_config()["ai_services"][0]["api_key"],
                         self.KEY)

    def test_migrate_config_failure_keeps_plaintext_source(self):
        Path(cm.CONFIG_FILE).write_text(json.dumps(
            {"ai_services": [{"name": "S", "api_key": self.KEY}]},
            ensure_ascii=False), encoding="utf-8")
        before = Path(cm.CONFIG_FILE).read_bytes()
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            cm._migrate_config_file()
        self.assertEqual(Path(cm.CONFIG_FILE).read_bytes(), before)  # 原件保留
        # 正常环境重跑迁移 → 明文升级为密文
        cm._migrate_config_file()
        raw = Path(cm.CONFIG_FILE).read_text(encoding="utf-8")
        self.assertNotIn(self.KEY, raw)
        self.assertEqual(cm.load_config()["ai_services"][0]["api_key"],
                         self.KEY)

    def test_logs_no_secret_body(self):
        cfg = dict(cm.DEFAULT_CFG)
        cfg["ai_services"] = [{"name": "S", "api_key": self.KEY}]
        with patch.object(cm, "_dpapi_protect", lambda d: None):
            self.assertFalse(cm.save_config(cfg))
        log_text = Path(cm.LOG_FILE).read_text(encoding="utf-8",
                                               errors="replace")
        self.assertNotIn(self.KEY, log_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
