# -*- coding: utf-8 -*-
"""命令面板浮层(Command Palette)—— Ctrl+Shift+P 一键直达全部功能。

设计参考 VSCode / Raycast / Alfred:输入即过滤,↑↓ 选择,Enter 执行,
Esc 原路返回;所有功能注册进 command/ 注册表,不再翻菜单找入口。

- 无边框 Tool 窗口:不进任务栏/Alt-Tab,常驻进程一次构建、show/hide 复用
- 顶部居中出现(光标所在屏),打开即聚焦搜索框;失去焦点自动收起
- 命令条目 = command.Command:name + 分组 + 快捷键提示;when 谓词不满足
  的命令自动隐藏(如"需要选中条目")
- 执行统一走 command.executor.run_command:异常捕获为错误提示,不打散日志
"""
from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)
from qfluentwidgets import CaptionLabel, SearchLineEdit, InfoBar, \
    InfoBarPosition, isDarkTheme, themeColor

import clipboard_manager as cm
from command.executor import run_command

ROW_H = 44
MAX_ROWS = 9
WIDTH = 560
LIST_CAP = 80                       # 面板最多渲染条数(再多的靠继续输入过滤)

CAT_GLYPH = {"剪贴板": "📋", "查看与对比": "🔍", "AI": "✨",
             "图钉画布": "📌", "导航": "🧭", "应用": "⚙"}


class _RowDelegate(QStyledItemDelegate):
    """单行绘制:分组字形 + 命令名 + 分组标签 + 右侧快捷键提示。"""

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def paint(self, p, opt, index):
        cmd = index.data(Qt.UserRole)
        if cmd is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = opt.rect
        hover = opt.state & QStyle.State_MouseOver
        sel = index.row() == self.win._sel
        if sel or hover:
            c = QColor(themeColor())
            c.setAlpha(52 if sel else 26)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r.adjusted(4, 2, -4, -2), 7, 7)
        dark = isDarkTheme()
        # 分组字形
        gf = QFont()
        gf.setPointSize(12)
        p.setFont(gf)
        p.drawText(QRect(r.left() + 10, r.top(), 26, r.height()),
                   Qt.AlignCenter, CAT_GLYPH.get(cmd.category, "•"))
        # 命令名 + 分组标签(次行)
        name_area = QRect(r.left() + 42, r.top() + 4, r.width() - 150, 20)
        nf = QFont()
        nf.setPointSize(9)
        p.setFont(nf)
        p.setPen(QColor(235, 235, 235) if dark else QColor(20, 20, 20))
        p.drawText(name_area, Qt.AlignVCenter, QFontMetrics(nf).elidedText(
            cmd.name, Qt.ElideRight, name_area.width()))
        sub_area = QRect(name_area.x(), name_area.bottom(), name_area.width(), 15)
        sf = QFont()
        sf.setPointSize(8)
        p.setFont(sf)
        p.setPen(QColor(150, 150, 150))
        p.drawText(sub_area, Qt.AlignVCenter, cmd.category)
        # 快捷键提示(右对齐,有才画)
        sc = cmd.shortcut_text()
        if sc:
            p.setFont(sf)
            p.setPen(QColor(130, 130, 130))
            p.drawText(QRect(r.right() - 118, r.top(), 110, r.height()),
                       Qt.AlignVCenter | Qt.AlignRight,
                       QFontMetrics(sf).elidedText(sc, Qt.ElideRight, 108))
        # 序号徽章(Ctrl+数字直选)
        row = index.row()
        if row < 9:
            p.drawText(QRect(r.right() - 30, r.top(), 24, r.height()),
                       Qt.AlignVCenter | Qt.AlignRight, str(row + 1))
        p.restore()

    def sizeHint(self, opt, index):
        return QSize(opt.rect.width() or WIDTH, ROW_H)


class CommandPaletteWindow(QWidget):
    """Ctrl+Shift+P 浮层:输入 → 过滤 → Enter 执行,三击之内触达任何功能。"""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self._cmds = []           # 当前过滤结果(command.Command 列表)
        self._sel = 0
        self._prev_hwnd = None
        self._armed = False       # 失焦自动收起的武装位(避开显示瞬间抖动)
        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.setInterval(260)
        self._arm_timer.timeout.connect(lambda: setattr(self, "_armed", True))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 6)
        lay.setSpacing(4)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("输入命令…(全部功能一键直达)")
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)
        # 输入即过滤(60ms 防抖,输入过程中不重建模型)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(60)
        self._filter_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(self._on_query_changed)

        self.list = QListView(self)
        self.list.setItemDelegate(_RowDelegate(self))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.clicked.connect(self._on_click)
        lay.addWidget(self.list, 1)

        self.footer = CaptionLabel("", self)
        self.footer.setStyleSheet("color:#909090;")
        lay.addWidget(self.footer)

        self.model = QStandardItemModel(self)
        self.list.setModel(self.model)
        self._sync_footer()

    # ---- 打开 / 收起 ----
    def toggle(self):
        if self.isVisible():
            self.close_restore()
        else:
            self.open()

    def open(self):
        import time as _t
        import ctypes
        t0 = _t.perf_counter()
        self._prev_hwnd = None
        try:
            hwnd = ctypes.WinDLL("user32").GetForegroundWindow()
            if hwnd and int(hwnd) != int(self.winId()):
                self._prev_hwnd = int(hwnd)
        except Exception:
            pass
        self._sel = 0
        self.search.clear()
        self.refresh()
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        self.list.scrollTo(self.model.index(0, 0))
        self._arm_timer.start()
        return (_t.perf_counter() - t0) * 1000   # 耗时(ms),测试断言用

    def _silent_hide(self):
        self._arm_timer.stop()
        self._armed = False
        self.hide()

    def close_restore(self):
        """收起并还原前窗焦点(Esc / 执行后)。"""
        self._arm_timer.stop()
        self._armed = False
        self.hide()
        try:
            import ctypes
            if self._prev_hwnd:
                ctypes.WinDLL("user32").SetForegroundWindow(int(self._prev_hwnd))
        except Exception:
            pass

    def hideEvent(self, e):
        self._armed = False
        self._arm_timer.stop()
        super().hideEvent(e)

    def event(self, e):
        if e.type() == QEvent.ActivationChange and self._armed \
                and not self.isActiveWindow() and self.isVisible():
            self._silent_hide()
            return True
        return super().event(e)

    def _place(self):
        """顶部居中出现(光标所在屏;VSCode 式),高度按行数自适应。"""
        from PySide6.QtGui import QCursor, QGuiApplication
        rows = min(self.model.rowCount(), MAX_ROWS) or 1
        h = 92 + rows * ROW_H
        self.resize(WIDTH, h)
        scr = QGuiApplication.screenAt(QCursor.pos()) \
            or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else None
        if geo is None:
            self.move(200, 200)
            return
        x = geo.left() + (geo.width() - WIDTH) // 2
        y = geo.top() + max(20, int(geo.height() * 0.10))
        self.move(x, y)

    # ---- 列表刷新 ----
    def _on_query_changed(self, _t=""):
        self._sel = 0
        self._filter_timer.start()

    def refresh(self):
        from PySide6.QtGui import QStandardItem
        q = self.search.text().strip()
        self._cmds = self.app.commands.search(q)[:LIST_CAP]
        self.model.clear()
        for c in self._cmds:
            it = QStandardItem()
            it.setData(c, Qt.UserRole)
            self.model.appendRow(it)
        if self._sel >= len(self._cmds):
            self._sel = max(0, len(self._cmds) - 1)
        self._sync_footer()
        self.list.viewport().update()

    def _sync_footer(self):
        n = len(self._cmds)
        if n == 0:
            self.footer.setText("无匹配命令 · Esc 关闭")
        else:
            self.footer.setText(
                "↑↓ 选择 · Enter 执行 · Ctrl+1~9 直选 · Esc 关闭 · %d 条命令"
                % n)

    # ---- 键盘/鼠标操作 ----
    def eventFilter(self, obj, e):
        if obj is self.search and e.type() == QEvent.KeyPress:
            k = e.key()
            m = e.modifiers()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self.exec_current()
                return True
            if k == Qt.Key_Escape:
                self.close_restore()
                return True
            if k in (Qt.Key_Up, Qt.Key_Down):
                self.move_sel(-1 if k == Qt.Key_Up else 1)
                return True
            if m & Qt.ControlModifier and Qt.Key_1 <= k <= Qt.Key_9:
                self._sel = k - Qt.Key_1
                self.exec_current()
                return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Up, Qt.Key_Down):
            self.move_sel(-1 if k == Qt.Key_Up else 1)
            return
        if k == Qt.Key_Escape:
            self.close_restore()
            return
        super().keyPressEvent(e)

    def _on_click(self, index):
        self._sel = index.row()
        self.exec_current()

    def move_sel(self, d):
        if not self._cmds:
            return
        self._sel = (self._sel + d) % len(self._cmds)
        self.list.scrollTo(self.model.index(self._sel, 0))
        self.list.viewport().update()

    # ---- 执行 ----
    def exec_current(self):
        if not 0 <= self._sel < len(self._cmds):
            return
        cmd = self._cmds[self._sel]
        self._silent_hide()
        ok, msg = run_command(cmd)
        if not ok:
            cm.log("命令 %s 执行失败:%s" % (cmd.id, msg))
            InfoBar.error("命令执行失败", "%s(%s)" % (msg, cmd.name),
                          parent=self.app.win,
                          position=InfoBarPosition.TOP_RIGHT, duration=6000)
