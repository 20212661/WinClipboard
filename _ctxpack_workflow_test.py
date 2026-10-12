# -*- coding: utf-8 -*-
"""P4 工作流集成与带上下文提问(离屏)。

Run: .venv/Scripts/python.exe -X utf8 _ctxpack_workflow_test.py
验收(§9-P4):发送内容 == assemble 输出;隐私闸必经(未确认则无网络
调用);旧工作流 JSON 兼容加载;{context} 解析;绑定失败拒绝启动。"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QWidget

app_qt = QApplication.instance() or QApplication([])
app_qt.setQuitOnLastWindowClosed(False)

import clipboard_manager as cm
import workflow_steps as wfs
import workflow_ui as wui
from ctxpack.ui_tray import tray_service
from ctxpack.assemble import assemble


class FakeCore:
    copied = []

    def copy_text(self, t):
        self.copied.append(t)
        return True


class FakeApp:
    def __init__(self):
        self.core = FakeCore()
        self.core.cfg = dict(
            cm.DEFAULT_CFG, encrypt_history=False,
            ai_services=[{"name": "S", "api_key": "k",
                          "base_url": "http://127.0.0.1:9"}])
        self.core.history = []
        self.core.private = []
        self.win = QWidget()
        self.panel = QWidget()
        self.icon = QIcon()
        self._viewers = []
        self._ai_runners = []       # 运行台 closeEvent 会摘除自身

    @property
    def cfg(self):
        return self.core.cfg


class WfBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_ctxwf_")
        self.paths = patch.multiple(
            cm, CTXPACK_FILE=os.path.join(self.tmp.name, "context_packs.json"),
            CONFIG_FILE=os.path.join(self.tmp.name, "config.json"),
            LOG_FILE=os.path.join(self.tmp.name, "app.log"))
        self.paths.start()
        self.fake = FakeApp()
        self.ai_calls = []

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    def seed_pack(self, name="资料包", items=("参考资料甲", "参考资料乙")):
        svc = tray_service(self.fake)
        p = svc.active_pack() or svc.create(name)
        for it in items:
            svc.add_snapshot("text", it, captured_at=0.0)
        return svc, p

    def spin(self, cond, timeout=5.0):
        t0 = time.time()
        while not cond():
            app_qt.processEvents()
            if time.time() - t0 > timeout:
                return False
            time.sleep(0.01)
        return True


class ContextVarTests(WfBase):

    def test_render_and_backcompat(self):
        ctx = wfs.WorkflowContext(input_text="原始")
        self.assertEqual(ctx.context, "")                    # 旧调用不变
        self.assertEqual(ctx.render("A{input}B{origin}C"),
                         "A原始B原始C")
        self.assertEqual(ctx.render("{context}"), "")        # 未绑定为空
        ctx2 = wfs.WorkflowContext(input_text="x", context="<context>资料</context>")
        self.assertEqual(ctx2.render("前\n{context}\n后{input}"),
                         "前\n<context>资料</context>\n后x")

    def test_resolve_active_pid_missing_null_overbudget(self):
        svc, p = self.seed_pack()
        # @active
        text, err = wui.resolve_wf_context(self.fake, {"context_pack": "@active"})
        self.assertEqual(err, "")
        self.assertIn("参考资料甲", text)
        # 明确 pid
        text2, _ = wui.resolve_wf_context(self.fake, {"context_pack": p.pid})
        self.assertEqual(text2, text)
        # null:行为完全不变
        self.assertEqual(wui.resolve_wf_context(self.fake, {}),
                         ("", ""))
        # 不存在的 pid
        text3, err3 = wui.resolve_wf_context(self.fake, {"context_pack": "nope"})
        self.assertEqual((text3, err3[0]), ("", "绑"))
        # 指令条目超预算 → 拒绝
        svc.add_snapshot("text", "字" * 4000, role="instruction",
                         captured_at=0.0)
        p.budget_tokens = 500
        svc.save()
        _t, err4 = wui.resolve_wf_context(self.fake, {"context_pack": "@active"})
        self.assertIn("超出预算", err4)
        # 空包(没有启用条目)
        svc2 = tray_service(self.fake)
        svc2.create("空包")
        svc2.set_active(svc2.packs[-1].pid)
        _t5, err5 = wui.resolve_wf_context(self.fake, {"context_pack": "@active"})
        self.assertIn("没有启用中的条目", err5)

    def test_runner_refuses_when_context_unresolvable(self):
        with self.assertRaises(RuntimeError):
            wui.WorkflowRunner(self.fake,
                               {"name": "W", "context_pack": "nope",
                                "steps": []}, "输入")
        # 无绑定:照常构造(旧工作流兼容;不真正启动线程)
        r = wui.WorkflowRunner(self.fake, {"name": "W", "steps": []}, "输入")
        r.close()

    def test_old_workflow_json_compat(self):
        # 旧 JSON:无 context_pack 字段 → 反序列化/运行语义不变
        old = {"name": "旧工作流",
               "steps": [{"kind": "clipboard"}]}
        steps = [wfs.deserialize(s) for s in old["steps"]]
        self.assertEqual(steps[0].kind, "clipboard")
        ctx = wfs.WorkflowContext(input_text="x")
        self.assertEqual(ctx.render("{input}"), "x")


class EditorTests(WfBase):

    def test_editor_roundtrip_context_pack(self):
        self.seed_pack()
        svc = tray_service(self.fake)
        wf = {"name": "带上下文的翻译",
              "context_pack": svc.active_pack().pid,
              "steps": [{"kind": "clipboard"}]}
        with patch.object(cm, "save_config", return_value=True) as psc:
            ed = wui.WorkflowEditorWindow(self.fake, wf=wf)
            self.assertEqual(ed.cmb_ctx.currentIndex(), 2)   # 不使用/活动包/该包
            ed._save()
            psc.assert_called()
        self.assertEqual(wf["context_pack"], svc.active_pack().pid)
        # 旧工作流(无字段)编辑保存 → 字段= None,不丢其它内容
        wf2 = {"name": "旧流程", "steps": [{"kind": "clipboard"}]}
        with patch.object(cm, "save_config", return_value=True):
            ed2 = wui.WorkflowEditorWindow(self.fake, wf=wf2)
            self.assertEqual(ed2.cmb_ctx.currentIndex(), 0)
            ed2._save()
        self.assertIsNone(wf2["context_pack"])
        self.assertEqual(wf2["name"], "旧流程")


class AskDialogTests(WfBase):

    def ask_dlg(self):
        from ctxpack.ui_ask import AskContextDialog
        dlg = AskContextDialog(self.fake)
        dlg.show()
        app_qt.processEvents()
        return dlg

    def test_send_content_equals_assemble_output(self):
        _svc, p = self.seed_pack()
        dlg = self.ask_dlg()
        dlg.txt_q.setPlainText("请总结")
        dlg._gate = lambda prompt, svc: prompt          # 闸放行(桩)

        def fake_ai(service, prompt):
            self.ai_calls.append(prompt)
            return True, "这是回答"
        with patch.object(cm, "ai_chat", fake_ai):
            dlg._send()
            ok = self.spin(lambda: dlg._answer is not None)
            self.assertTrue(ok, "AI 调用未在限时内完成")
        expected = assemble(p, question="请总结",
                            include_sensitive=True).text
        self.assertEqual(self.ai_calls, [expected])      # 发送内容 == 拼装输出
        self.assertEqual(dlg.txt_result.toPlainText(), "这是回答")
        self.assertTrue(dlg.btn_copy.isEnabled())
        dlg._copy_answer()
        self.assertIn("这是回答", self.fake.core.copied)
        dlg.close()

    def test_gate_cancel_means_no_network_call(self):
        self.seed_pack()
        dlg = self.ask_dlg()
        dlg.txt_q.setPlainText("问题")
        dlg._gate = lambda prompt, svc: None             # 闸:取消

        def fake_ai(service, prompt):
            self.ai_calls.append(prompt)
            return True, "不该有这个回答"
        with patch.object(cm, "ai_chat", fake_ai):
            dlg._send()
            app_qt.processEvents()
        self.assertEqual(self.ai_calls, [])              # 未确认 → 无网络调用
        self.assertIsNone(dlg._answer)
        dlg.close()

    def test_empty_question_and_no_service(self):
        dlg = self.ask_dlg()
        dlg._send()                                      # 空问题:提示不发
        self.assertEqual(self.ai_calls, [])
        dlg.txt_q.setPlainText("有问题")
        saved = self.fake.core.cfg["ai_services"]
        self.fake.core.cfg["ai_services"] = []
        dlg._send()                                      # 无服务:提示不发
        self.assertEqual(self.ai_calls, [])
        self.fake.core.cfg["ai_services"] = saved
        dlg.close()

    def test_cancel_drops_late_result(self):
        self.seed_pack()
        dlg = self.ask_dlg()
        dlg.txt_q.setPlainText("问题")
        dlg._gate = lambda prompt, svc: prompt

        def slow_ai(service, prompt):
            self.ai_calls.append(prompt)
            time.sleep(0.15)
            return True, "迟到回答"
        with patch.object(cm, "ai_chat", slow_ai):
            dlg._send()
            dlg._cancel_ai()                             # 立即放弃
            ok = self.spin(lambda: not dlg._zombies, timeout=3)
            self.assertTrue(ok)
        self.assertIsNone(dlg._answer)                   # 迟到结果被丢弃
        self.assertTrue(dlg.btn_send.isEnabled())
        dlg.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
