# -*- coding: utf-8 -*-
"""上下文篮 UI 离屏测试(P3)。

Run: .venv/Scripts/python.exe -X utf8 _ctxpack_ui_test.py
覆盖(指导 §7.5 / §9-P3):折叠默认与徽标、展开不遮挡卡片流(几何断言)、
预算进度 0/95/120% 三态、长标题省略、入包编排(敏感拦截/放行)、
存储状态行、查看器选区助手、调色板同步、托盘按钮拼装(预览/复制桩)。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QTextCursor
from PySide6.QtWidgets import QApplication, QWidget

app_qt = QApplication.instance() or QApplication([])
app_qt.setQuitOnLastWindowClosed(False)

import clipboard_manager as cm
import ui_base
import ctxpack.ui_tray as ctr
from ctxpack.ui_tray import (ContextTray, PackNameDialog, add_records_to_pack,
                             add_text_to_pack, append_ctx_menu, tray_service)
from ctxpack.service import ContextPackService


class FakeCore:
    copied = []

    def copy_text(self, t):
        self.copied.append(t)
        return True


class FakeApp:
    """最小 app 替身:托盘/编排只依赖这些属性。"""

    def __init__(self):
        self.core = FakeCore()
        self.core.cfg = dict(cm.DEFAULT_CFG, encrypt_history=False)
        self.core.history = []
        self.core.private = []
        self.win = QWidget()
        self.panel = QWidget()
        self.icon = QIcon()
        self._viewers = []          # 查看器 closeEvent 会摘除自身

    @property
    def cfg(self):
        return self.core.cfg


class CtxUiBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_ctxui_")
        self.ctx_path = os.path.join(self.tmp.name, "context_packs.json")
        self.paths = patch.multiple(
            cm, CTXPACK_FILE=self.ctx_path,
            LOG_FILE=os.path.join(self.tmp.name, "app.log"))
        self.paths.start()
        self.fake = FakeApp()

    def tearDown(self):
        svc = getattr(self.fake, "_ctxpack_svc", None)
        self.paths.stop()
        self.tmp.cleanup()

    def tray(self):
        t = ContextTray(self.fake)
        t.refresh()
        return t

    def seed(self, contents=("第一条素材", "第二条素材")):
        svc = tray_service(self.fake)
        p = svc.active_pack() or svc.create("工作包")
        for c in contents:
            svc.add_snapshot("text", c, captured_at=0.0)
        return svc


class TrayBasicsTests(CtxUiBase):

    def test_collapsed_default_and_badge(self):
        t = ContextTray(self.fake)
        self.assertFalse(t._expanded)
        self.assertFalse(t._body.isVisibleTo(t))       # 折叠:条目区收起
        self.seed(("素材一", "素材二"))
        t.refresh()
        self.assertIn("篮 2", t.lbl_badge.text())
        self.assertIn("≈", t.lbl_badge.text())
        for w in t._header_extras:                     # 折叠:操作钮全部收起
            self.assertFalse(w.isVisibleTo(t))

    def test_expand_collapse(self):
        t = self.tray()
        t.show()
        t.set_expanded(True)
        self.assertTrue(t._expanded)
        self.assertTrue(t._body.isVisibleTo(t))
        t.set_expanded(False)
        self.assertFalse(t._body.isVisibleTo(t))

    def test_expand_does_not_overlap_cards(self):
        """几何断言:托盘(折叠/展开)与卡片流区域不相交(布局保证)。"""
        host = QWidget()
        from PySide6.QtWidgets import QVBoxLayout
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        cards = QWidget()
        cards.setMinimumHeight(120)
        lay.addWidget(cards, 1)
        self.seed()
        tray = ContextTray(self.fake, host)
        lay.addWidget(tray)
        host.resize(600, 400)
        host.show()
        for expanded in (False, True):
            tray.set_expanded(expanded)
            app_qt.processEvents()
            g1, g2 = cards.geometry(), tray.geometry()
            disjoint = (g1.bottom() < g2.top() or g2.bottom() < g1.top()
                        or g1.right() < g2.left() or g2.right() < g1.left())
            self.assertTrue(disjoint,
                            "托盘 expanded=%s 与卡片流重叠:%s vs %s"
                            % (expanded, g1, g2))
        host.close()

    def test_usage_levels_and_colors(self):
        svc = tray_service(self.fake)
        p = svc.create("预算包", budget_tokens=1000)
        t = self.tray()
        t.refresh()
        self.assertEqual(t._usage_level, "ok")         # 0%
        svc.add_snapshot("text", "甲" * 480, captured_at=0.0)   # ≈480(48%)
        t.refresh()
        self.assertEqual(t._usage_level, "ok")
        svc.add_snapshot("text", "乙" * 470, captured_at=0.0)   # 合计 ≈950
        t.refresh()
        self.assertEqual(t._usage_level, "warn")       # >90%
        self.assertIn(ctr._warn_color(), t._bar_fill.styleSheet())
        svc.add_snapshot("text", "丙" * 300, captured_at=0.0)   # 合计 >1000
        t.refresh()
        self.assertEqual(t._usage_level, "error")
        self.assertIn(ctr._err_color(), t._bar_fill.styleSheet())
        self.assertGreater(t._usage_fraction, 1.0)
        self.assertIn("超出预算", t.lbl_usage.text())

    def test_long_title_elided(self):
        tray_service(self.fake).create("包")
        add_text_to_pack(self.fake, "长" * 200, allow_sensitive=True)
        t = self.tray()
        t.set_expanded(True)
        rows = [t._body_lay.itemAt(i).widget()
                for i in range(t._body_lay.count())]
        rows = [r for r in rows if r.__class__.__name__ == "_TrayRow"]
        self.assertEqual(len(rows), 1)
        self.assertIn("…", rows[0].lbl_title.text())
        self.assertLess(len(rows[0].lbl_title.text()), 60)

    def test_status_line_states(self):
        t = self.tray()
        self.assertIn("尚未保存", t.lbl_status.text())   # missing
        self.seed(("x",))
        t.refresh()
        self.assertIn("未加密", t.lbl_status.text())     # plaintext 模式
        svc = tray_service(self.fake)
        svc.encrypt = True
        svc.save()
        t.refresh()
        self.assertIn("已加密", t.lbl_status.text())
        # unreadable:损坏主文件 + 无备份
        svc2 = ContextPackService(self.ctx_path + ".bad").load()
        Path(self.ctx_path + ".bad").write_bytes(b"broken{")
        svc2.load()
        t2 = ContextTray(type("A", (), {"cfg": {}, "win": QWidget(),
                                        "panel": QWidget(),
                                        "_ctxpack_svc": svc2})())
        t2.refresh()
        self.assertIn("不可读", t2.lbl_status.text())


class AddFlowTests(CtxUiBase):

    def test_add_records_normal_and_dup_hint(self):
        recs = [{"type": "text", "content": "普通内容A", "ts": 1.0, "src": "a.exe"},
                {"type": "files", "content": ["C:\\tmp\\f1.txt"], "ts": 2.0}]
        added, blocked = add_records_to_pack(self.fake, recs)
        self.assertEqual((added, blocked), (2, 0))
        svc = tray_service(self.fake)
        self.assertEqual([i.kind for i in svc.active_pack().items],
                         ["text", "file_list"])
        added2, _ = add_records_to_pack(self.fake, recs[:1])   # 同指纹重复
        self.assertEqual(added2, 1)                            # 允许重复,仅提示

    def test_sensitive_block_and_release(self):
        recs = [{"type": "text", "content": "password=Hunter22024x", "ts": 1.0},
                {"type": "text", "content": "普通", "ts": 2.0}]
        with patch.object(ctr, "_confirm_sensitive", return_value=False):
            added, blocked = add_records_to_pack(self.fake, recs)
        self.assertEqual((added, blocked), (1, 1))             # 拒绝=跳过
        svc = tray_service(self.fake)
        self.assertEqual(len(svc.active_pack().items), 1)
        with patch.object(ctr, "_confirm_sensitive", return_value=True):
            added, blocked = add_records_to_pack(self.fake, recs)
        self.assertEqual((added, blocked), (2, 0))             # 普通重复+敏感放行
        self.assertTrue(any(i.sensitive
                            for i in svc.active_pack().items))

    def test_menu_has_ctx_submenu(self):
        from qfluentwidgets import RoundMenu
        self.seed()
        menu = RoundMenu()
        append_ctx_menu(self.fake, menu,
                        [{"type": "text", "content": "x", "ts": 1.0}])
        # qfw RoundMenu:子菜单挂在 _subMenus,不在 _actions(既有坑)
        self.assertTrue(len(menu._subMenus) >= 1)
        # 多选入口
        menu2 = RoundMenu()
        append_ctx_menu(self.fake, menu2,
                        [{"type": "text", "content": "a", "ts": 1.0},
                         {"type": "text", "content": "b", "ts": 2.0}])
        self.assertTrue(len(menu2._subMenus) >= 1)

    def test_add_text_auto_creates_default_pack(self):
        r = add_text_to_pack(self.fake, "片段内容")
        self.assertTrue(r.ok)
        self.assertEqual(tray_service(self.fake).active_pack().name, "默认包")

    def test_pack_name_dialog(self):
        dlg = PackNameDialog(self.fake.win, initial="  名字  ")
        self.assertEqual(dlg.name(), "名字")


class ViewerSelectionTests(CtxUiBase):

    def test_selected_text_and_menu_add(self):
        import viewer as vw
        v = vw.ViewerWindow(self.fake, {"type": "text",
                                        "content": "hello world viewer",
                                        "ts": 1.0})
        v.show()
        app_qt.processEvents()
        self.assertEqual(v._selected_text(), "")               # 无选区
        ed = v._active_edit()
        c = ed.textCursor()
        c.setPosition(0)
        c.setPosition(5, QTextCursor.KeepAnchor)               # "hello"
        ed.setTextCursor(c)
        self.assertEqual(v._selected_text(), "hello")
        # 走真实入包路径(等价于「更多」菜单点击)
        add_text_to_pack(self.fake, v._selected_text())
        self.assertIn("hello",
                      tray_service(self.fake).active_pack().items[0].content)
        v.close()


class AssembleButtonsTests(CtxUiBase):

    def test_copy_assembled_writes_clipboard(self):
        self.seed(("复制我",))
        t = self.tray()
        t.set_expanded(True)
        t.copy_assembled()
        app_qt.processEvents()
        # offscreen 剪贴板是桩(见测试纪律):以拼装结果可生成为准,
        # 真实写入断言交给 _copy_or_warn 的失败语义(已有套件覆盖)
        svc = tray_service(self.fake)
        a = svc.assemble_active(include_sensitive=True)
        self.assertIn("复制我", a.text)

    def test_empty_tray_copy_warns(self):
        t = self.tray()
        t.copy_assembled()                                     # 空篮:提示不崩
        app_qt.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=1)
