# -*- coding: utf-8 -*-
"""剪贴板写入失败贯穿回归(阶段 2)。

Run: .venv/Scripts/python.exe -X utf8 _clipboard_failure_test.py
临时目录 + 安全模式核心;_set_clipboard 用实例桩控制成败,不碰系统剪贴板,
不发送任何真实按键。覆盖计划中的语义表与补充场景:

  | 情况                    | 返回  | 粘贴命令 | 历史提升 |
  | 写入失败+auto_paste 开  | False | 无       | 无       |
  | 写入失败+force=True     | False | 无       | 无       |
  | 写入成功+auto_paste 关  | True  | 无       | 有       |
  | 写入成功+auto_paste 开  | True  | 有(参数) | 有       |
  | 写入成功+force=True     | True  | 有(参数) | 有       |
  | copy_text/record 成败   | True/False | 无  | 无       |

补充:文件全缺失短路(不写剪贴板)、部分缺失+写入失败不改原记录、
部分缺失+成功才落过滤结果、写入异常按失败、失败不留多余抑制窗、
库 seq 不提升、工作流复制/网页步骤失败契约、快捷浮层失败不收起。
"""
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import clipboard_manager as cm
import workflow_steps as wfs


class ClipboardFailureRegression(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_clipfail_")
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

    def tearDown(self):
        for core in self.cores:
            if core.store is not None:
                core.store.close()
            core._capture_exec.shutdown(wait=True)
        self.paths.stop()
        self.tmp.cleanup()

    # ---- 工具 ----
    def core(self, cfg=None):
        core = cm.ClipboardCore(safe_mode=True)
        core._schedule_gc = lambda: None
        if cfg:
            core.cfg.update(cfg)
        self.cores.append(core)
        return core

    @staticmethod
    def drain(core):
        out = []
        while True:
            try:
                out.append(core.worker.cmd_q.get_nowait())
            except queue.Empty:
                return out

    @staticmethod
    def seed(core, *texts):
        for t in texts:
            core.add_item(("text", t))
        return list(core.history)

    # ---- 语义表 ----
    def test_table(self):
        core = self.core()
        recs = self.seed(core, "a", "b")
        rec = recs[1]

        def fresh_stub():
            calls = []
            core._set_clipboard = lambda r: calls.append(r) or True
            return calls

        # 写入失败 + auto_paste 开
        core.cfg["auto_paste"] = True
        core._set_clipboard = lambda r: False
        self.assertFalse(core.paste_record(rec))
        self.assertEqual(self.drain(core), [])
        self.assertIs(core.history[1], rec)          # 未提升
        self.assertIn(rec, core.history)

        # 写入失败 + force=True
        core._set_clipboard = lambda r: False
        self.assertFalse(core.paste_record(rec, force=True, target_hwnd=77))
        self.assertEqual(self.drain(core), [])

        # 写入成功 + auto_paste 关(默认)
        core.cfg["auto_paste"] = False
        calls = fresh_stub()
        self.assertTrue(core.paste_record(rec))
        self.assertEqual(self.drain(core), [])
        self.assertIs(core.history[0], rec)          # 提升
        self.assertEqual(len(calls), 1)

        # 写入成功 + auto_paste 开
        core.cfg["auto_paste"] = True
        self.assertTrue(core.paste_record(rec))
        cmds = self.drain(core)
        self.assertEqual([c[0] for c in cmds], ["paste"])
        self.assertEqual(cmds[0][1], 0)              # target_hwnd=None → 0

        # 写入成功 + force=True(带目标窗口)
        self.assertTrue(core.paste_record(rec, force=True, target_hwnd=1234))
        cmds = self.drain(core)
        self.assertEqual(cmds[0][:2], ("paste", 1234))
        self.assertIs(core.history[0], rec)

    def test_copy_text_and_record_results(self):
        core = self.core()
        core._set_clipboard = lambda r: True
        self.assertTrue(core.copy_text("hello"))
        self.assertEqual(self.drain(core), [])       # 复制不排回贴
        core._set_clipboard = lambda r: False
        self.assertFalse(core.copy_text("hello"))
        rec = {"type": "text", "content": "x"}
        self.assertFalse(core.copy_record(rec))
        core._set_clipboard = lambda r: True
        self.assertTrue(core.copy_record(rec))
        self.assertFalse(core.copy_record(
            {"type": "files", "content": ["Z:/无/1.txt"]}))   # 文件全缺
        self.assertEqual(self.drain(core), [])

    # ---- 补充场景 ----
    def test_files_all_missing_short_circuits(self):
        core = self.core()
        calls = []
        core._set_clipboard = lambda r: calls.append(r) or True
        self.assertFalse(core.paste_record(
            {"type": "files", "content": ["Z:/无/1.txt", "Z:/无/2.txt"]}))
        self.assertEqual(calls, [])                  # 根本不碰剪贴板

    def test_files_partial_missing_failure_keeps_original(self):
        keep = self.tmp.name + "/keep.txt"
        gone = "Z:/无/gone.txt"
        Path(keep).write_text("x", encoding="utf-8")
        core = self.core()
        rec = {"type": "files", "content": [gone, keep]}
        core._set_clipboard = lambda r: False        # 写入失败
        self.assertFalse(core.paste_record(rec))
        self.assertEqual(rec["content"], [gone, keep])   # 原记录未动
        core._set_clipboard = lambda r: True         # 成功才落过滤结果
        self.assertTrue(core.paste_record(rec))
        self.assertEqual(rec["content"], [keep])

    def test_set_clipboard_exception_is_failure(self):
        core = self.core()
        self.seed(core, "a")

        def _boom(r):
            raise OSError("剪贴板被占用")
        core._set_clipboard = _boom
        self.assertFalse(core.paste_record(core.history[0], force=True))
        self.assertEqual(self.drain(core), [])

    def test_failure_restores_suppression_window(self):
        core = self.core()
        self.seed(core, "a")
        # 失败不延长/不新开抑制窗
        before = core._ignore_clip_until
        core._set_clipboard = lambda r: False
        core.paste_record(core.history[0], force=True)
        self.assertEqual(core._ignore_clip_until, before)
        # 已有抑制窗失败时原样保留
        core._ignore_clip_until = time.monotonic() + 999
        core.paste_record(core.history[0], force=True)
        self.assertGreater(core._ignore_clip_until, time.monotonic() + 998)
        # 成功才武装新抑制窗
        core._ignore_clip_until = 0.0
        core._set_clipboard = lambda r: True
        core.paste_record(core.history[0], force=True)
        self.assertGreater(core._ignore_clip_until, time.monotonic())

    def test_no_store_promote_on_failure(self):
        core = self.core()
        self.seed(core, "a", "b")
        rec = core.history[1]
        if core.store is None:
            self.skipTest("JSON 模式无库(seq 提升不适用)")
        with patch.object(core.store, "upsert_promote",
                          side_effect=AssertionError("失败不应提升 seq")):
            core._set_clipboard = lambda r: False
            self.assertFalse(core.paste_record(rec))

    # ---- 工作流步骤契约 ----
    def test_workflow_clipboard_step_failure_contract(self):
        ctx = wfs.WorkflowContext(input_text="内容", copy_text=lambda t: False)
        res = wfs.ClipboardStep().run(ctx)
        self.assertIsInstance(res, wfs.Done)
        self.assertTrue(res.halt)
        self.assertIn("复制失败", res.note[0])

    def test_workflow_web_step_failure_no_open_no_claim(self):
        opened = []

        def _copy(t):
            return False

        ctx = wfs.WorkflowContext(input_text="内容", copy_text=_copy,
                                  open_url=opened.append)
        res = wfs.WebStep(prompt="P:{input}", url="https://example.com/?q=x"
                          ).run(ctx)
        self.assertIsInstance(res, wfs.Done)
        self.assertTrue(res.halt)
        self.assertEqual(opened, [])                 # 失败不开网页
        self.assertIn("复制失败", res.note[0])
        self.assertNotIn("已复制", res.note[1])      # 不宣称已复制

    def test_workflow_copy_none_return_is_failure(self):
        ctx = wfs.WorkflowContext(input_text="内容",
                                  copy_text=lambda t: None)  # 未声明成败
        res = wfs.ClipboardStep().run(ctx)
        self.assertTrue(res.halt)
        web = wfs.WebStep(prompt="{input}", url="https://e.com/?q=1").run(
            wfs.WorkflowContext(input_text="c", copy_text=lambda t: None,
                                open_url=lambda u: True))
        self.assertTrue(web.halt)

    def test_workflow_copy_success_still_works(self):
        copied = []

        def _ok(t):
            copied.append(t)
            return True

        res = wfs.ClipboardStep().run(
            wfs.WorkflowContext(input_text="内容", copy_text=_ok))
        self.assertFalse(res.halt)
        self.assertEqual(copied, ["内容"])

    # ---- 快捷浮层失败不收起(离屏) ----
    def test_quickpaste_stays_open_on_failure(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QObject
        from PySide6.QtGui import QIcon
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        import winclipboard_qt as wq

        class FakeCore2:
            def __init__(self):
                self.results = []

            def paste_record(self, rec, force=False, target_hwnd=None):
                self.results.append((rec["content"], force, target_hwnd))
                return False

            @staticmethod
            def query_view(cat, q, limit=None):
                return [0]

        class FakeApp2(QObject):
            def __init__(self):
                super().__init__()
                self.core = FakeCore2()
                self._thumbs = {}
                self.history = [{"type": "text", "content": "条目A",
                                 "ts": 1, "src": ""}]

        fake = FakeApp2()
        qp = wq.QuickPasteWindow(fake)
        qp.refresh()
        qp.show()
        self.assertTrue(qp.isVisible())
        qp._sel = 0
        qp.act_paste()
        self.assertTrue(qp.isVisible())             # 失败:不按成功流程收起
        self.assertIn("写入失败", qp.footer.text())
        self.assertEqual(fake.core.results[0][0], "条目A")
        # 恢复成功路径:收起浮层
        fake.core.paste_record = lambda rec, force=False, target_hwnd=None: True
        qp.show()
        qp.act_paste()
        self.assertFalse(qp.isVisible())
        qp.hide()


if __name__ == "__main__":
    unittest.main(verbosity=2)
