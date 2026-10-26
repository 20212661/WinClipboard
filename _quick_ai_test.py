"""Quick AI regressions: all clipboard/network endpoints are stand-ins."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_sandbox = tempfile.TemporaryDirectory(prefix="wclb_quick_ai_")
os.environ["APPDATA"] = _sandbox.name
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import setTheme, Theme
import ai_quick as qa
import ui_base

qt = QApplication.instance() or QApplication([])
ui_base.apply_fonts(qt)


def pump_until(test, condition):
    deadline = time.monotonic() + 3
    while not condition() and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(0.005)
    test.assertTrue(condition())


class QuickAI(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            cfg={"ai_services": [{"name": "Test", "base_url": "http://unused", "model": "test"}]},
            win=QWidget(),
            core=SimpleNamespace(copy_text=Mock(return_value=True), paste_text=Mock(return_value=True),
                                 add_item=Mock()))
        self.w = qa.QuickAIWindow(self.app)
        self.w._snapshot = "frozen {input} material"
        self.w.select_action("ask")

    def tearDown(self):
        self.w.shutdown()
        self.app.win.close()
        qt.processEvents()

    def test_one_pass_prompt(self):
        prompt = qa.build_prompt("explain", "literal {input}", context="literal {previous}")
        self.assertIn("literal {input}", prompt)
        self.assertIn("literal {previous}", prompt)

    def test_empty_and_oversized(self):
        for args in (("explain", ""), ("ask", "text"), ("explain", "x" * (qa.MAX_INPUT_CHARS + 1))):
            with self.assertRaises(ValueError):
                qa.build_prompt(*args)

    def test_wheel_directions(self):
        import math
        for i in range(6):
            a = math.radians(-90 + i * 60)
            point = QPointF(180 + math.cos(a) * 100, 180 + math.sin(a) * 100)
            self.assertEqual(self.w.wheel.action_at(point), i)
        self.assertEqual(self.w.wheel.action_at(QPointF(180, 180)), -1)
        self.assertEqual(self.w.wheel.action_at(QPointF(0, 0)), -1)

    def test_css_palette_rgba(self):
        self.assertEqual(qa.palette_color("rgba(255,255,255,0.93)").red(), 255)
        self.assertGreater(qa.palette_color("rgba(255,255,255,0.93)").alpha(), 230)

    def test_native_hotkey_routes_to_quick_ai(self):
        from win32_host import TrayWorker, ID_HOTKEY_AI
        from queue import Queue
        posted = []
        host = TrayWorker.__new__(TrayWorker)
        host.app = SimpleNamespace(_post=lambda event: posted.append(event),
                                   cfg=dict(qa.cm.DEFAULT_CFG))
        host.hwnd = None
        host.on_hotkey(None, 0, ID_HOTKEY_AI, 0)
        self.assertEqual(posted, [("ai_hotkey",)])

    def test_hotkey_conflict_restores_previous(self):
        import win32_host as host_module
        host = host_module.TrayWorker.__new__(host_module.TrayWorker)
        posted = []
        host.app = SimpleNamespace(cfg=dict(qa.cm.DEFAULT_CFG),
                                   _post=lambda event: posted.append(event))
        host.hwnd = 123
        native = SimpleNamespace(UnregisterHotKey=Mock(), RegisterHotKey=Mock(side_effect=(False, True)))
        with patch.object(host_module, "user32", native):
            host._do_cmd(("hotkey_ai", 3, 65))
        self.assertEqual(native.RegisterHotKey.call_count, 2)
        self.assertEqual(posted, [("ai_hotkey_change", False, 3, 65)])

    def test_help_has_ai_entry(self):
        from winclipboard_qt import WinClipboardApp
        self.assertIn("快速 AI 轮盘", WinClipboardApp.shortcut_help_text(self.app))

    def test_system_snapshot_not_history(self):
        with patch.object(qa, "clipboard_text", return_value="live text"), \
                patch.object(qa.cm, "_get_foreground", return_value=4321):
            self.w.open_wheel()
        self.assertEqual(self.w._snapshot, "live text")
        self.assertEqual(self.w._target, 4321)

    def test_record_source_target(self):
        self.app._paste_target = 6543
        self.w.open_wheel({"type": "text", "content": "selected text"})
        self.assertEqual(self.w._snapshot, "selected text")
        self.assertEqual(self.w._target, 6543)

    def test_read_failure_keeps_previous_snapshot(self):
        with patch.object(qa, "clipboard_text", side_effect=OSError("busy")), \
                patch.object(qa.cm, "_get_foreground", return_value=1), \
                patch.object(qa.InfoBar, "warning"):
            self.w.open_wheel()
        self.assertFalse(self.w.wheel.isVisible())
        # No stale material is offered after read failure.
        self.assertEqual(self.w.input.toPlainText(), "frozen {input} material")

    def test_gate_cancel_means_no_network(self):
        self.w.question.setPlainText("what?")
        with patch.object(self.w, "_gate", return_value=None), patch.object(qa.cm, "ai_chat") as chat:
            self.w.send()
            chat.assert_not_called()
        self.assertIsNone(self.w._active)

    def test_success_and_no_auto_clipboard_write(self):
        self.w.question.setPlainText("what?")
        captured = []
        def chat(service, prompt):
            captured.append(prompt)
            return True, "answer"
        with patch.object(qa.cm, "ai_chat", side_effect=chat), patch.object(self.w, "_gate", side_effect=lambda p, s: p):
            self.w.send()
            pump_until(self, lambda: not self.w._pending)
        self.assertEqual(self.w._answer, "answer")
        self.assertIn("frozen {input} material", captured[0])
        self.app.core.copy_text.assert_not_called()
        self.app.core.paste_text.assert_not_called()

    def test_cancelled_late_answer_ignored(self):
        self.w._active = 10
        self.w._pending.add(10)
        self.w.cancel()
        self.w._on_done(10, True, "late")
        self.assertEqual(self.w.output.toPlainText(), "")
        self.assertFalse(self.w._pending)

    def test_old_answer_does_not_replace_new_request(self):
        self.w._active = 11
        self.w._pending.update((10, 11))
        self.w._on_done(10, True, "old")
        self.assertEqual(self.w._active, 11)
        self.assertEqual(self.w._answer, "")
        self.w._on_done(11, True, "new")
        self.assertEqual(self.w._answer, "new")

    def test_clipboard_failure_keeps_window(self):
        self.w._answer = "answer"
        self.app.core.copy_text.return_value = False
        self.w.copy_answer()
        self.assertIn("失败", self.w.status.text())
        self.assertTrue(self.w.isVisible())

    def test_missing_target_no_write(self):
        self.w._answer = "answer"
        self.w._target = 0
        self.w.paste_answer()
        self.app.core.paste_text.assert_not_called()

    def test_history_failure_reported(self):
        self.w._answer = "answer"
        self.app.core.add_item.side_effect = OSError("disk full")
        self.w.save_history()
        self.assertIn("失败", self.w.status.text())

    def test_paste_failure_does_not_hide_window(self):
        self.w._answer = "answer"
        self.w._target = 456
        self.app.core.paste_text.return_value = False
        with patch.object(qa.cm.win32gui, "IsWindow", return_value=True):
            self.w.paste_answer()
        self.assertTrue(self.w.isVisible())
        self.app.core.paste_text.assert_called_once_with("answer", force=True, target_hwnd=456)

    def test_close_before_auto_send(self):
        self.w.hide()
        with patch.object(qa.cm, "ai_chat") as chat:
            self.w.send()
            chat.assert_not_called()

    def test_inflight_limit(self):
        self.w._pending.update((1, 2))
        with patch.object(qa.cm, "ai_chat") as chat:
            self.w.send()
            chat.assert_not_called()
        self.w._sync_buttons()
        self.assertFalse(self.w.send_button.isEnabled())

    def test_missing_context_does_not_send(self):
        self.w.question.setPlainText("what?")
        self.w.use_context.setChecked(True)
        with patch("ctxpack.ui_tray.tray_service", return_value=SimpleNamespace(assemble_active=lambda **kw: None)), \
                patch.object(qa.cm, "ai_chat") as chat:
            self.w.send()
            chat.assert_not_called()
        self.assertIn("没有可用", self.w.status.text())

    def test_layout_and_previews(self):
        output = Path(__file__).resolve().parent / "docs" / "ui" / "quick_ai"
        output.mkdir(parents=True, exist_ok=True)
        for mode in ("light", "dark"):
            setTheme(Theme.LIGHT if mode == "light" else Theme.DARK)
            ui_base.init_palette(mode == "dark")
            self.w.setStyleSheet("QuickAIWindow { background: %s; }" % ui_base.BG)
            self.w._answer = "这段代码使用字典缓存查询结果。\n\n建议：先检查键是否存在，再处理缓存未命中的情况。"
            self.w.output.setPlainText(self.w._answer)
            self.w._sync_buttons()
            qt.processEvents()
            for widget in (self.w.input, self.w.output, self.w.send_button, self.w.pack):
                rect = widget.geometry()
                self.assertGreater(rect.width(), 0)
                self.assertLessEqual(rect.right(), self.w.width())
                self.assertLessEqual(rect.bottom(), self.w.height())
            self.w.grab().save(str(output / ("result_" + mode + ".png")))
            self.w.wheel._hover = 0
            self.w.wheel.grab().save(str(output / ("wheel_" + mode + ".png")))


class RegistryUnificationTests(unittest.TestCase):
    """PR3B-4:入口动作定义与 Registry 统一(同 id 同提示词;未覆盖走兼容路径)。"""

    def test_translate_uses_registry_definition(self):
        p = qa.build_prompt("translate", "hello")
        self.assertIn("判断下面文本的语言", p)        # Registry 内置文案
        self.assertNotIn("将下面的内容翻译为中文", p)  # 旧本地文案让位
        self.assertIn("hello", p)

    def test_summarize_uses_registry_definition(self):
        p = qa.build_prompt("summarize", "内容")
        self.assertIn("先一句话摘要", p)

    def test_uncovered_actions_keep_local_text(self):
        # Registry 未定义 explain/polish:保留本地文案(规则 6 兼容路径)
        self.assertIn("用清楚易懂的中文解释", qa.build_prompt("explain", "x"))
        self.assertIn("润色", qa.build_prompt("polish", "x"))

    def test_user_override_wins(self):
        import json as _json
        from pathlib import Path
        import ai as _ai
        ud = Path(_sandbox.name) / "user_acts"
        ud.mkdir(exist_ok=True)
        (ud / "ov.json").write_text(_json.dumps(
            {"id": "translate", "name": "翻译", "workflow": "translate"},
            ensure_ascii=False), encoding="utf-8")
        _ai.init(str(ud))
        try:
            cfg = {"ai_workflows": [{"name": "翻译", "steps": [
                {"kind": "ai", "service": "", "prompt": "用户版翻译:{input}"}]}]}
            p = qa.build_prompt("translate", "材料", cfg=cfg)
            self.assertIn("用户版翻译", p)
        finally:
            _ai.init()      # 还原纯内置,不泄漏到其他用例

    def test_icon_whitelist_and_fallback(self):
        from qfluentwidgets import FluentIcon as _FIF
        self.assertIs(qa.registry_action_icon({"icon": "DICTIONARY"}),
                      _FIF.DICTIONARY)
        self.assertIsNone(qa.registry_action_icon({"icon": "HACKED"}))
        self.assertIsNone(qa.registry_action_icon({}))
        self.assertIsNone(qa.registry_action_icon(None))


class DualPanelConsistency(unittest.TestCase):
    """PR3B-1:QuickAIWindow 与 EmbeddedAIPanel 经同一 ChatSession 的行为一致性。"""

    def setUp(self):
        self.app = SimpleNamespace(
            cfg={"ai_services": [{"name": "T", "base_url": "http://u", "model": "m"}]},
            win=QWidget(),
            core=SimpleNamespace(copy_text=Mock(return_value=True),
                                 paste_text=Mock(return_value=True),
                                 add_item=Mock()))
        self.q = qa.QuickAIWindow(self.app)
        self.q.show()          # QuickAIWindow.send 有 isVisible 守卫
        self.q._snapshot = "共享材料"   # select_action 会用快照重填输入框
        # 用 ask 动作+预填问题:非 ask 动作带材料会触发自动发送,打穿补丁窗口
        self.q.select_action("ask")
        self.q.question.setPlainText("怎么看?")
        self.p = qa.EmbeddedAIPanel(self.app)
        self.p.set_material({"type": "text", "content": "共享材料"})
        self.p.refresh_services()
        self.p.question.setPlainText("怎么看?")   # 面板默认动作=ask,问题必填

    def tearDown(self):
        self.q.shutdown()
        self.p.shutdown()
        self.app.win.close()
        qt.processEvents()

    def _chat_ok(self, captured, answer):
        def chat(service, prompt):
            captured.append(prompt)
            return True, answer
        return chat

    def test_same_input_same_flow(self):
        captured = []
        with patch.object(qa.cm, "ai_chat",
                          side_effect=self._chat_ok(captured, "同一回答")), \
                patch.object(self.q, "_gate", side_effect=lambda p, s: p), \
                patch.object(self.p, "_gate", side_effect=lambda p, s: p):
            self.q.send()
            self.p.send()
            pump_until(self, lambda: not self.q._pending and not self.p._pending)
        self.assertEqual(self.q._answer, "同一回答")
        self.assertEqual(self.p._answer, "同一回答")
        self.assertIn("共享材料", captured[0])
        self.assertIn("共享材料", captured[1])
        self.assertIn("已完成", self.q.status.text())
        self.assertIn("已完成", self.p.status.text())

    def test_cancel_during_flight_drops_late_result(self):
        import threading
        gate = threading.Event()

        def chat(service, prompt):
            gate.wait(3)
            return True, "迟到"

        with patch.object(qa.cm, "ai_chat", side_effect=chat), \
                patch.object(self.q, "_gate", side_effect=lambda p, s: p), \
                patch.object(self.p, "_gate", side_effect=lambda p, s: p):
            self.q.send()
            self.p.send()
            self.q.cancel()
            self.p.cancel()
            gate.set()
            pump_until(self, lambda: not self.q._pending and not self.p._pending)
        self.assertEqual(self.q.output.toPlainText(), "")
        self.assertEqual(self.p.output.toPlainText(), "")
        self.assertIn("已停止等待", self.q.status.text())
        self.assertIn("已停止等待", self.p.status.text())

    def test_material_switch_during_flight_keeps_request(self):
        import threading
        prompts = []
        gate = threading.Event()

        def chat(service, prompt):
            prompts.append(prompt)
            gate.wait(3)
            return True, "答"

        with patch.object(qa.cm, "ai_chat", side_effect=chat), \
                patch.object(self.p, "_gate", side_effect=lambda p, s: p):
            self.p.send()
            pump_until(self, lambda: len(prompts) == 1)
            # 生成中切历史卡片:材料不抢占(在途请求在 spawn 时已冻结)
            self.p.set_material({"type": "text", "content": "新卡片材料"})
            gate.set()
            pump_until(self, lambda: not self.p._pending)
        self.assertIn("共享材料", prompts[0])
        self.assertNotIn("新卡片材料", prompts[0])
        self.assertEqual(self.p._answer, "答")
        self.assertEqual(self.p._material, "共享材料")

    def test_gate_cancel_blocks_both(self):
        with patch.object(self.q, "_gate", return_value=None), \
                patch.object(self.p, "_gate", return_value=None), \
                patch.object(qa.cm, "ai_chat") as chat:
            self.q.send()
            self.p.send()
            chat.assert_not_called()
        self.assertIn("已取消", self.q.status.text())
        self.assertIn("已取消", self.p.status.text())

    def test_inflight_cap_blocks_both(self):
        self.q._pending.update((1, 2))
        self.p._pending.update((1, 2))
        with patch.object(qa.cm, "ai_chat") as chat:
            self.q.send()
            self.p.send()
            chat.assert_not_called()

    def test_same_action_same_prompt_across_entries(self):
        # PR3B-4 示范:translate 在快速窗(轮盘自动发送)与右栏生成
        # 完全一致的 Prompt,且指令取自 Registry 定义
        captured = []

        def chat(svc, prompt):
            captured.append(prompt)
            return True, "答"

        with patch.object(qa.cm, "ai_chat", side_effect=chat), \
                patch.object(self.q, "_gate", side_effect=lambda p, s: p), \
                patch.object(self.p, "_gate", side_effect=lambda p, s: p):
            self.q.select_action("translate")   # 非 ask 带材料 → 自动发送
            pump_until(self, lambda: len(captured) >= 1)
            self.p._quick_btns["translate"].setChecked(True)
            self.p.question.setPlainText("")    # 同输入:translate 无问题
            self.p.send()
            pump_until(self, lambda: len(captured) >= 2)
        self.assertEqual(captured[0], captured[1])
        self.assertIn("判断下面文本的语言", captured[0])

    def test_duplicate_done_never_fires_outlets(self):
        # 同一请求的结果回调重复到达:结果不重复执行出口(出口仅由用户点击触发)
        self.q.question.setPlainText("q")
        with patch.object(qa.cm, "ai_chat", side_effect=self._chat_ok([], "答案")), \
                patch.object(self.q, "_gate", side_effect=lambda p, s: p):
            self.q.send()
            pump_until(self, lambda: not self.q._pending)
        rid = self.q._session._serial
        self.q._on_done(rid, True, "答案")          # 迟到重复回调
        self.q._on_done(rid, True, "答案")
        self.app.core.copy_text.assert_not_called()
        self.app.core.paste_text.assert_not_called()
        self.app.core.add_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
