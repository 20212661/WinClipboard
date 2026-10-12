# -*- coding: utf-8 -*-
"""上下文包存储/服务回归(P2)。

Run: .venv/Scripts/python.exe -X utf8 _ctxpack_store_test.py
全部路径临时隔离;真 DPAPI;故障按注入点模拟(与 privacy 套件同纪律)。
覆盖:
  1. 原子写 + bak1~3 轮转;临时文件写失败原文件不动
  2. 损坏主文件:.corrupt 留档字节保留 / 从备份恢复 / unreadable 禁止自动覆写
  3. DPAPI 失败不降级明文(故障注入)
  4. 加密模式双向迁移 + 明文残留清理(解密方向保留加密备份)
  5. 破坏性清空(主文件+备份+留档)
  6. 服务 CRUD/敏感闸/去重提示/自动落盘与失败保留
  7. 集成:历史加密开关联动迁移(开启失败中止切换)/ 清空全部数据
     / 可迁移备份往返 / 历史操作不碰包文件
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clipboard_manager as cm
import ctxpack
from ctxpack import ContextPackService, load_packs_status, save_packs
from ctxpack import migrate_packs_file, wipe_packs, merge_packs_payload
from ctxpack.service import snapshot_args_from_record


class StoreBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_ctxstore_")
        self.path = os.path.join(self.tmp.name, "context_packs.json")
        # 隔离日志出口(纯存储层测试不依赖 cm 路径重定向)
        self._logpatch = patch.object(ctxpack.store, "log", lambda msg: None)
        self._logpatch.start()
        self.assertTrue(cm.dpapi_available(), "本套件需真实 DPAPI 环境")

    def tearDown(self):
        self._logpatch.stop()
        self.tmp.cleanup()

    def mkpacks(self, n=2):
        packs = []
        for i in range(n):
            p = ctxpack.new_pack("包%d" % i)
            p.items.append(ctxpack.make_item("text", "内容%d" % i))
            packs.append(p)
        return packs


class AtomicWriteTests(StoreBase):

    def test_rotation_and_recovery_chain(self):
        packs = self.mkpacks(1)
        names = []
        for i in range(5):                      # 五次保存 → bak 链滚过 3 份
            packs[0].items.append(ctxpack.make_item("text", "第%d次" % i))
            self.assertTrue(save_packs(self.path, packs, packs[0].pid, True).ok)
            names.append(packs[0].items[-1].content)
        for i in (1, 2, 3):
            self.assertTrue(os.path.exists(self.path + ".bak%d" % i))
        r = load_packs_status(self.path)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.mode, "dpapi")
        self.assertEqual([x.content for x in r.packs[0].items],
                         ["内容0"] + names)
        # bak1 最新(上一次保存 = 5 条)、bak3 最旧:条目数递减
        n1 = len(load_packs_status(self.path + ".bak1").packs[0].items)
        n3 = len(load_packs_status(self.path + ".bak3").packs[0].items)
        self.assertEqual(n1, 5)
        self.assertTrue(n3 < n1)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_tmp_write_failure_preserves_main(self):
        packs = self.mkpacks(1)
        self.assertTrue(save_packs(self.path, packs, None, True).ok)
        before = Path(self.path).read_bytes()
        packs[0].name = "改名后"
        with patch.object(ctxpack.store, "_write_payload",
                   side_effect=OSError("disk full")):
            res = save_packs(self.path, packs, None, True)
        self.assertFalse(res.ok)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(load_packs_status(self.path).packs[0].name, "包0")

    def test_corrupt_main_recovered_from_backup(self):
        packs = self.mkpacks(1)
        self.assertTrue(save_packs(self.path, packs, packs[0].pid, True).ok)
        # 制造:有效 bak1 + 损坏主文件
        os.replace(self.path, self.path + ".bak1")
        Path(self.path).write_bytes(b"{ not json !!!")
        r = load_packs_status(self.path)
        self.assertEqual(r.status, "recovered")
        self.assertEqual(r.source, "context_packs.json.bak1")
        self.assertEqual(r.packs[0].name, "包0")
        # 损坏原件字节级留档且不重复制造;二次损坏(无备份可恢复)时
        # 主文件原样保留、旧留档不被动
        self.assertEqual(Path(self.path + ".corrupt").read_bytes(),
                         b"{ not json !!!")
        os.replace(self.path + ".bak1", self.path)     # 占位模拟再次损坏
        Path(self.path).write_bytes(b"{ again")
        r2 = load_packs_status(self.path)
        self.assertEqual(r2.status, "unreadable")
        self.assertEqual(Path(self.path).read_bytes(), b"{ again")
        self.assertEqual(Path(self.path + ".corrupt").read_bytes(),
                         b"{ not json !!!")

    def test_unreadable_refuses_auto_overwrite(self):
        Path(self.path).write_bytes(b"garbage-not-json")
        svc = ContextPackService(self.path).load()
        self.assertEqual(svc.status, "unreadable")
        svc.create("新包")                       # 变更只进内存
        self.assertEqual(Path(self.path).read_bytes(), b"garbage-not-json")
        self.assertIn("不覆写", svc.last_save_error)
        res = svc.save()
        self.assertFalse(res.ok)
        self.assertEqual(Path(self.path).read_bytes(), b"garbage-not-json")

    def test_dpapi_failure_no_plaintext_downgrade(self):
        packs = self.mkpacks(1)
        self.assertTrue(save_packs(self.path, packs, None, False).ok)  # 明文原状
        before = Path(self.path).read_bytes()
        with patch.object(cm, "_dpapi_protect", return_value=None):
            res = save_packs(self.path, packs, None, True)
        self.assertFalse(res.ok)
        self.assertFalse(res.encrypted)
        self.assertEqual(Path(self.path).read_bytes(), before)  # 原文件不动
        self.assertIn("DPAPI", res.error)

    def test_plaintext_never_enters_backup_chain(self):
        packs = self.mkpacks(1)
        for _ in range(3):
            self.assertTrue(save_packs(self.path, packs, None, False).ok)
        for i in (1, 2, 3):
            self.assertFalse(os.path.exists(self.path + ".bak%d" % i),
                             "明文主文件不得进入备份链")


class MigrationTests(StoreBase):

    def test_migrate_both_directions_and_residue(self):
        packs = self.mkpacks(2)
        self.assertTrue(save_packs(self.path, packs, packs[0].pid, False).ok)
        Path(self.path + ".corrupt").write_text("旧明文留档", encoding="utf-8")
        Path(self.path + ".tmp").write_text("残留", encoding="utf-8")
        ok, err = migrate_packs_file(self.path, encrypt=True)
        self.assertTrue(ok, err)
        r = load_packs_status(self.path)
        self.assertEqual(r.mode, "dpapi")
        self.assertEqual(r.active_pid, packs[0].pid)
        self.assertEqual(len(r.packs), 2)
        self.assertFalse(os.path.exists(self.path + ".corrupt"))  # 明文残留清理
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        # 同模式再迁移:不重写(mtime/字节不变)
        raw = Path(self.path).read_bytes()
        ok, _ = migrate_packs_file(self.path, encrypt=True)
        self.assertTrue(ok)
        self.assertEqual(Path(self.path).read_bytes(), raw)
        # 解密方向:回明文,加密备份链保留
        self.assertTrue(save_packs(self.path, packs, packs[0].pid, True).ok)
        ok, err = migrate_packs_file(self.path, encrypt=False)
        self.assertTrue(ok, err)
        r = load_packs_status(self.path)
        self.assertEqual(r.mode, "plaintext")
        self.assertTrue(os.path.exists(self.path + ".bak1"))  # 加密备份保留

    def test_migrate_missing_and_unreadable(self):
        ok, _ = migrate_packs_file(self.path + ".none", encrypt=True)
        self.assertTrue(ok)                      # 无文件 = 无需迁移
        Path(self.path).write_bytes(b"broken[")
        ok, err = migrate_packs_file(self.path, encrypt=True)
        self.assertFalse(ok)
        self.assertEqual(Path(self.path).read_bytes(), b"broken[")
        self.assertIn("不可读", err)

    def test_wipe_removes_everything(self):
        packs = self.mkpacks(1)
        self.assertTrue(save_packs(self.path, packs, None, True).ok)
        os.replace(self.path, self.path + ".bak1")
        Path(self.path).write_bytes(b"bad")
        load_packs_status(self.path)             # 产生 .corrupt
        Path(self.path).write_bytes(b"bad")      # 重新弄一个主文件
        wipe_packs(self.path)
        for suffix in ("", ".bak1", ".bak2", ".bak3", ".corrupt", ".tmp"):
            self.assertFalse(os.path.exists(self.path + suffix))

    def test_merge_packs_payload_local_wins(self):
        packs = self.mkpacks(2)
        payload = {"version": 1, "active": packs[0].pid,
                   "packs": [p.to_dict() for p in packs]}
        # 空目标:全部导入,采用备份里的活动包
        added, res = merge_packs_payload(self.path, payload, encrypt=False)
        self.assertEqual(added, 2)
        self.assertTrue(res.ok)
        r = load_packs_status(self.path)
        self.assertEqual(r.active_pid, packs[0].pid)
        # 本地已有同 pid 包:本地版本保留,不重复
        packs[0].name = "本地已改名"
        self.assertTrue(save_packs(self.path, packs, packs[1].pid, False).ok)
        added, res = merge_packs_payload(self.path, payload, encrypt=False)
        self.assertEqual(added, 0)
        self.assertIsNone(res)                   # 无变更不写盘
        self.assertEqual(load_packs_status(self.path).packs[0].name, "本地已改名")
        self.assertEqual(load_packs_status(self.path).active_pid, packs[1].pid)


class ServiceTests(StoreBase):

    def setUp(self):
        super().setUp()
        self.hits = {"API 密钥格式"}

        def probe(text):
            return "API 密钥格式" if "sk-SECRET" in text else None
        self.svc = ContextPackService(self.path, sensitive_check=probe).load()

    def test_crud_roundtrip_and_active(self):
        p1 = self.svc.create("工作包")
        self.assertEqual(self.svc.active_pid, p1.pid)
        p2 = self.svc.create("资料包")          # 新建包默认激活
        self.assertEqual(self.svc.active_pid, p2.pid)
        self.svc.set_active(p1.pid)
        self.svc.add_snapshot("text", "条目A", pid=p1.pid, src_app="Notepad",
                              captured_at=100.0, origin_fp="'条目A'")
        self.svc.add_snapshot("code", "print(1)", pid=p2.pid)
        self.svc.rename(p1.pid, "改名包")
        self.svc.update_item(p1.pid, p1.items[0].iid, role="instruction",
                             priority=8)
        self.svc.move_item(p1.pid, p1.items[0].iid, 0)
        fresh = ContextPackService(self.path).load()
        self.assertEqual([x.name for x in fresh.list_packs()], ["改名包", "资料包"])
        self.assertEqual(fresh.active_pid, p1.pid)
        rp = fresh.get(p1.pid)
        self.assertEqual(rp.items[0].role, "instruction")
        self.assertGreater(rp.items[0].est_tokens, 0)
        self.assertTrue(fresh.remove_item(p1.pid, rp.items[0].iid))
        # 删除活动包 → 活动切到剩余第一个
        fresh.delete(p1.pid)
        self.assertEqual(fresh.active_pid, p2.pid)
        self.assertIsNone(fresh.get(p1.pid))

    def test_sensitive_gate_and_release(self):
        self.svc.create("包")
        r = self.svc.add_snapshot("text", "key=sk-SECRET123")
        self.assertFalse(r.ok)
        self.assertTrue(r.reason.startswith("sensitive:"))
        self.assertEqual(len(self.svc.packs[0].items), 0)
        r2 = self.svc.add_snapshot("text", "key=sk-SECRET123",
                                   allow_sensitive=True)
        self.assertTrue(r2.ok)
        self.assertTrue(r2.item.sensitive)
        a = self.svc.assemble_active()
        self.assertIn((r2.item.iid, ctxpack.DROP_SENSITIVE), a.dropped)
        a2 = self.svc.assemble_active(include_sensitive=True)
        self.assertIn(r2.item.iid, a2.included)

    def test_dup_hint_but_still_added(self):
        self.svc.create("包")
        r1 = self.svc.add_snapshot("text", "同指纹", origin_fp="FP1")
        r2 = self.svc.add_snapshot("text", "同指纹", origin_fp="FP1")
        self.assertTrue(r2.ok)
        self.assertEqual(r2.dup_of, r1.item.iid)     # 仅提示,不作引用
        self.assertEqual(len(self.svc.packs[0].items), 2)

    def test_no_pack_and_save_error_visible(self):
        self.assertIsNone(self.svc.assemble_active())
        r = self.svc.add_snapshot("text", "x")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "no_pack")
        with patch.object(ctxpack.store, "_write_payload",
                   side_effect=OSError("no space")):
            p = self.svc.create("落盘失败包")
        self.assertEqual(len(self.svc.packs), 1)     # 内存保留待重试
        self.assertIn("no space", self.svc.last_save_error)
        self.assertTrue(self.svc.save().ok)          # 可重试
        self.assertEqual(ContextPackService(self.path).load().packs[0].name,
                         "落盘失败包")

    def test_snapshot_args_from_record(self):
        args = snapshot_args_from_record(
            {"type": "text", "content": "hello", "ts": 5.0, "src": "app.exe"})
        self.assertEqual(args["kind"], "text")
        self.assertEqual(args["origin_fp"], repr("hello"))
        args = snapshot_args_from_record(
            {"type": "text", "content": "{}", "type_override": "json"})
        self.assertEqual(args["kind"], "json")
        args = snapshot_args_from_record(
            {"type": "files", "content": ["C:\\a\\b.txt", "C:\\c.txt"],
             "ts": 1.0})
        self.assertEqual(args["kind"], "file_list")
        self.assertEqual(args["title"], "b.txt")
        self.assertIn("b.txt", args["content"])
        args = snapshot_args_from_record(
            {"type": "image", "content": {"path": "C:\\x\\img.png",
                                          "w": 640, "h": 480, "hash": "h1"},
             "ts": 2.0})
        self.assertEqual(args["kind"], "image_ref")
        self.assertEqual(args["title"], "img.png(640×480)")
        self.assertNotIn("h1", args["content"])      # 像素哈希不进内容


class CoreIntegrationTests(unittest.TestCase):
    """ClipboardCore 钩子:加密开关联动 / 清空全部数据 / 备份往返。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_ctxint_")
        root = self.tmp.name
        paths = {"APP_DIR": root}
        for key, name in (("IMAGE_DIR", "images"), ("BLOB_DIR", "data/blobs"),
                          ("HISTORY_FILE", "history.json"),
                          ("DB_FILE", "clipboard.db"),
                          ("PRIVATE_FILE", "private.json"),
                          ("CTXPACK_FILE", "context_packs.json"),
                          ("CONFIG_FILE", "config.json"),
                          ("LOG_FILE", "app.log"), ("ICON_FILE", "icon.ico"),
                          ("BOOT_STATE_FILE", "boot_state.json")):
            paths[key] = os.path.join(root, name)
        self.paths = patch.multiple(cm, **paths)
        self.paths.start()
        cm.ensure_dirs()
        cm.save_config(dict(cm.DEFAULT_CFG, sensitive_guard=False))
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            if core.store is not None:
                core.store.close()
            core._capture_exec.shutdown(wait=True)
        self.paths.stop()
        self.tmp.cleanup()

    def core(self):
        core = cm.ClipboardCore(safe_mode=True)
        core._schedule_gc = lambda: None
        self.cores.append(core)
        return core

    def seed_packs(self, encrypt=False, n=1):
        packs = []
        for i in range(n):
            p = ctxpack.new_pack("集成包%d" % i)
            p.items.append(ctxpack.make_item("text", "集成内容%d" % i))
            packs.append(p)
        self.assertTrue(save_packs(cm.CTXPACK_FILE, packs, packs[0].pid,
                                   encrypt).ok)
        return packs

    def test_history_toggle_migrates_packs(self):
        packs = self.seed_packs(encrypt=False)
        core = self.core()
        core.set_history_encryption(True)
        r = load_packs_status(cm.CTXPACK_FILE)
        self.assertEqual(r.mode, "dpapi")
        self.assertEqual(r.packs[0].name, "集成包0")
        core.set_history_encryption(False)
        r = load_packs_status(cm.CTXPACK_FILE)
        self.assertEqual(r.mode, "plaintext")

    def test_enable_failure_aborts_toggle_and_keeps_plaintext(self):
        self.seed_packs(encrypt=False)
        before = Path(cm.CTXPACK_FILE).read_bytes()
        core = self.core()
        with patch.object(cm, "_dpapi_protect", return_value=None):
            with self.assertRaises(OSError):
                core.set_history_encryption(True)
        # 包文件保持明文原样;历史模式未被切成加密
        self.assertEqual(Path(cm.CTXPACK_FILE).read_bytes(), before)
        self.assertFalse(core.cfg.get("encrypt_history"))

    def test_wipe_all_data_clears_packs(self):
        self.seed_packs(encrypt=True)
        Path(cm.CTXPACK_FILE + ".corrupt").write_bytes(b"leftover")
        core = self.core()
        done, ok = core.wipe_all_data()
        self.assertTrue(ok)
        self.assertTrue(any("上下文包" in d for d in done))
        for suffix in ("", ".bak1", ".corrupt"):
            self.assertFalse(os.path.exists(cm.CTXPACK_FILE + suffix))

    def test_history_ops_do_not_touch_packs(self):
        self.seed_packs(encrypt=False)
        core = self.core()
        core.add_item(("text", "一条历史"))
        before = Path(cm.CTXPACK_FILE).read_bytes()
        core.flush_history()
        core.soft_clear_unpinned()             # 可撤销清空(未置顶)
        core.save_all()
        self.assertEqual(Path(cm.CTXPACK_FILE).read_bytes(), before)

    def test_portable_backup_roundtrip(self):
        packs = self.seed_packs(encrypt=False, n=2)
        core = self.core()
        payload = core.export_portable_payload(include_private=True)
        self.assertIsInstance(payload["ctxpacks"], dict)
        self.assertEqual(len(payload["ctxpacks"]["packs"]), 2)
        raw = cm.portable_backup_bytes(payload, "pass-word-123")
        parsed = cm.portable_backup_parse(raw, "pass-word-123")
        # 导入到空包环境(临时目录 B)
        with tempfile.TemporaryDirectory(prefix="wclb_ctximp_") as b:
            with patch.object(cm, "CTXPACK_FILE",
                       os.path.join(b, "context_packs.json")):
                added, skipped, n_priv = core.import_portable_payload(parsed)
                r = load_packs_status(cm.CTXPACK_FILE)
                self.assertEqual(r.status, "ok")
                self.assertEqual(len(r.packs), 2)
                self.assertEqual(r.packs[0].name, "集成包0")
                # 重复导入:pid 去重,不重复落盘
                self.assertEqual(
                    merge_packs_payload(cm.CTXPACK_FILE,
                                        parsed["ctxpacks"], False)[0], 0)
        # 原环境包文件未被导入路径改动
        self.assertEqual(load_packs_status(cm.CTXPACK_FILE).active_pid,
                         packs[0].pid)


if __name__ == "__main__":
    unittest.main(verbosity=1)
