# -*- coding: utf-8 -*-
"""快捷粘贴浮层(Quick Paste Overlay)—— Win+V 式超轻量入口。

设计目标:『我刚才复制的东西,几次操作能拿回来?』——默认 Alt+` 弹出,
输入过滤、↑↓ 选择、Enter 粘贴,全程不离开键盘。

- 无边框 Tool 窗口:不进任务栏/Alt-Tab,常驻进程一次构建、show/hide 复用
- 打开即聚焦搜索框;失去焦点自动收起(点别处 = 关闭,同 Win+V)
- Enter 粘贴 / Shift+Enter 纯文本粘贴(不带富文本格式)
  / Ctrl+Enter 打开查看器 / Ctrl+数字 直选 / Tab 切类型 / Del 删除 / Esc 原路返回
- 粘贴前先隐藏浮层并还原前窗焦点,复用核心 worker 的 70ms 稳定延迟回贴
- 图片只画缩略图(ThumbLoader 后台生成),从不解码原图
"""
import ctypes

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPixmap,
                           QStandardItemModel)
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)
from qfluentwidgets import CaptionLabel, SearchLineEdit, isDarkTheme, themeColor

import clipboard_manager as cm

CATS = ("all", "text", "link", "image", "files")
CAT_LABEL = {"all": "全部", "text": "文本", "link": "链接",
             "image": "图片", "files": "文件"}
GLYPH = {"text": "🧾", "link": "🔗", "image": "🖼", "files": "📁"}
ROW_H = 46
MAX_ROWS = 8
LIST_CAP = 60                      # 浮层最多渲染的条数(再多的靠继续输入过滤)
WIDTH = 520


def plain_rec(rec):
    """纯文本粘贴用的条目副本:剥离富文本格式(html/rtf)。"""
    return {k: v for k, v in rec.items() if k not in ("html", "rtf")}


def caret_screen_pos():
    """前台窗口的光标(插入符)屏幕坐标;取不到退回鼠标位置。"""
    try:
        import ctypes.wintypes as wt

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("flags", wt.DWORD),
                        ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                        ("hwndCapture", wt.HWND), ("hwndActiveOwner", wt.HWND),
                        ("hwndFocusRestore", wt.HWND),
                        ("rcCaret", wt.RECT)]

        user32 = ctypes.WinDLL("user32")
        user32.GetGUIThreadInfo.restype = wt.BOOL
        user32.GetGUIThreadInfo.argtypes = [wt.DWORD,
                                            ctypes.POINTER(GUITHREADINFO)]
        info = GUITHREADINFO()
        info.cb = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            r = info.rcCaret
            if r.right > r.left or r.bottom > r.top:
                return int(r.left), int(r.bottom)
    except Exception:
        pass
    from PySide6.QtGui import QCursor
    p = QCursor.pos()
    return p.x(), p.y()


def _restore_focus(hwnd):
    if hwnd:
        try:
            ctypes.WinDLL("user32").SetForegroundWindow(int(hwnd))
        except Exception:
            pass


class _RowDelegate(QStyledItemDelegate):
    """单行绘制:图标位(缩略图/字形)+ 标题 + 元信息 + 序号徽章。"""

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def paint(self, p, opt, index):
        rec = index.data(Qt.UserRole)
        if rec is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = opt.rect
        hover = opt.state & QStyle.State_MouseOver
        sel = index.row() == self.win._sel
        if sel or hover:
            base = themeColor() if sel else QColor(128, 128, 128)
            c = QColor(base)
            c.setAlpha(52 if sel else 26)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r.adjusted(4, 2, -4, -2), 7, 7)
            p.setBrush(c)
            p.drawRoundedRect(r.adjusted(4, 2, -4, -2), 7, 7)
        # 图标位
        tile = QRect(r.left() + 10, r.top() + 7, 32, 32)
        thumb = self.win.thumb_of(rec)
        if thumb is not None:
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(tile, 6, 6)
            p.setBrush(QColor(255, 255, 255, 16))
            p.drawRoundedRect(tile, 6, 6)
            pm = thumb.scaled(32, 32, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)
            p.drawPixmap(tile.x() + (32 - pm.width()) // 2,
                         tile.y() + (32 - pm.height()) // 2, pm)
        else:
            p.setPen(QColor(255, 255, 255, 200) if isDarkTheme()
                     else QColor(0, 0, 0, 170))
            f = p.font()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(tile, Qt.AlignCenter,
                       GLYPH.get(rec["type"], "🧾"))
        # 标题 + 元信息
        dark = isDarkTheme()
        title_area = QRect(tile.right() + 10, r.top() + 5,
                           r.width() - tile.width() - 66, 20)
        meta_area = QRect(title_area.x(), title_area.bottom() + 1,
                          title_area.width(), 16)
        tf = QFont()
        tf.setPointSize(9)
        p.setFont(tf)
        p.setPen(QColor(255, 255, 255) if dark else QColor(20, 20, 20))
        title = self.win.title_of(rec)
        pinned = "📌 " if rec.get("pinned") else ""
        p.drawText(title_area, Qt.AlignVCenter,
                   pinned + QFontMetrics(tf).elidedText(
                       title, Qt.ElideRight, title_area.width()))
        mf = QFont()
        mf.setPointSize(8)
        p.setFont(mf)
        p.setPen(QColor(160, 160, 160))
        p.drawText(meta_area, Qt.AlignVCenter, self.win.meta_of(rec))
        # 序号徽章(Ctrl+数字直选)
        row = index.row()
        if row < 9:
            nf = QFont()
            nf.setPointSize(8)
            p.setFont(nf)
            p.setPen(QColor(150, 150, 150))
            p.drawText(QRect(r.right() - 30, r.top(), 24, r.height()),
                       Qt.AlignVCenter | Qt.AlignRight, str(row + 1))
        p.restore()

    def sizeHint(self, opt, index):
        return QSize(opt.rect.width() or WIDTH, ROW_H)


class QuickPasteWindow(QWidget):
    """Alt+` 浮层:打开 → 过滤 → Enter 粘贴,三击之内拿回任何近期内容。"""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._view = []            # history 索引列表(filter_view 输出)
        self._sel = 0              # 当前行(0-based)
        self._cat = "all"
        self._prev_hwnd = None
        self._armed = False        # 失焦自动收起的武装位(避开显示瞬间的抖动)
        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.setInterval(260)
        self._arm_timer.timeout.connect(lambda: setattr(self, "_armed", True))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 6)
        lay.setSpacing(4)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("搜索剪贴历史…")
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)

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

    # ---- 状态查询(供 delegate 回调) ----
    def thumb_of(self, rec):
        if rec.get("type") != "image":
            return None
        path = rec.get("content")
        pm = (getattr(self.app, "_thumbs", None) or {}).get(path)
        if pm is None:
            loader = getattr(self.app, "_thumb_loader", None)
            if loader is not None:
                loader.submit(path)      # 后台生成,完成后经 on_thumb 重绘
        return pm

    @staticmethod
    def title_of(rec):
        t = rec.get("type")
        if t == "image":
            return "图片 %s×%s" % (rec.get("w", 0), rec.get("h", 0))
        if t == "files":
            c = rec.get("content") or []
            return (c[0] if c else "?") + (" 等 %d 个" % len(c) if len(c) > 1 else "")
        c = str(rec.get("content") or "")
        return c.replace("\n", " ").replace("\t", " ")

    @staticmethod
    def meta_of(rec):
        bits = [x for x in (rec.get("src", ""),
                            cm._rel_time(rec.get("ts", 0))) if x]
        return " · ".join(bits) if bits else "—"

    # ---- 打开 / 收起 ----
    def toggle(self):
        if self.isVisible():
            self.close_restore()
        else:
            self.open()

    def open(self):
        import time as _t
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
        """收起但不抢回焦点(用户点了别处时用)。"""
        self._arm_timer.stop()
        self._armed = False
        self.hide()

    def close_restore(self):
        """收起并还原前窗焦点(Esc / 粘贴后)。"""
        self._arm_timer.stop()
        self._armed = False
        self.hide()
        _restore_focus(self._prev_hwnd)

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
        """贴着光标/鼠标出现;放不下就往上翻;始终钳制在屏幕内。"""
        from PySide6.QtGui import QGuiApplication
        rows = min(self.model.rowCount(), MAX_ROWS) or 1
        h = 96 + rows * ROW_H
        self.resize(WIDTH, h)
        scr = QGuiApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else None
        cx, cy = caret_screen_pos()
        x, y = cx - WIDTH // 2, cy + 12
        if geo is not None:
            if y + h > geo.bottom():
                y = max(geo.top(), cy - h - 12)
            x = min(max(x, geo.left()), geo.right() - WIDTH)
            y = min(max(y, geo.top()), geo.bottom() - h)
        self.move(x, y)

    # ---- 列表刷新 ----
    def refresh(self):
        from PySide6.QtGui import QStandardItem
        q = self.search.text().strip()
        # SQL 优先(库模式走索引过滤),无库自动回退内存 filter_view
        view = self.app.core.query_view(self._cat, q, limit=LIST_CAP)
        self._view = view[:LIST_CAP]
        self.model.clear()
        for hi in self._view:
            it = QStandardItem()
            it.setData(self.app.history[hi], Qt.UserRole)
            self.model.appendRow(it)
        if self._sel >= len(self._view):
            self._sel = max(0, len(self._view) - 1)
        self._sync_footer()
        self.list.viewport().update()

    def _sync_footer(self):
        n = len(self._view)
        total = len(self.app.history)
        more = " · 仅显示前 %d 条,输入可过滤" % LIST_CAP if total > LIST_CAP \
            and self._cat == "all" and not self.search.text().strip() else ""
        if n == 0:
            self.footer.setText("无匹配条目 · Tab 切类型,Esc 关闭")
        else:
            self.footer.setText(
                "↑↓ 选择 · Enter 粘贴 · Shift+Enter 纯文本 · Ctrl+Enter 查看"
                " · Tab 类型:%s · Esc 关闭%s"
                % (CAT_LABEL.get(self._cat, self._cat), more))

    def on_thumb(self, path, pm):
        """ThumbLoader 后台生成完毕的回填(浮层可见时刷新重绘)。"""
        if self.isVisible():
            self.list.viewport().update()

    def _rec_at(self, row):
        if 0 <= row < len(self._view):
            return self._view[row], self.app.history[self._view[row]]
        return None, None

    # ---- 键盘/鼠标操作 ----
    def eventFilter(self, obj, e):
        if obj is self.search and e.type() == QEvent.KeyPress:
            k = e.key()
            m = e.modifiers()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if m & Qt.ControlModifier:
                    self.act_view()
                elif m & Qt.ShiftModifier:
                    self.act_paste(plain=True)
                else:
                    self.act_paste()
                return True
            if k == Qt.Key_Escape:
                self.close_restore()
                return True
            if k == Qt.Key_Tab:
                self.cycle_cat()
                return True
            if k in (Qt.Key_Up, Qt.Key_Down):
                self.move_sel(-1 if k == Qt.Key_Up else 1)
                return True
            if k == Qt.Key_Delete and not self.search.text():
                self.act_del()
                return True
            if m & Qt.ControlModifier and Qt.Key_1 <= k <= Qt.Key_9:
                self._sel = k - Qt.Key_1
                self.act_paste()
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
        self.act_paste()

    def move_sel(self, d):
        if not self._view:
            return
        self._sel = (self._sel + d) % len(self._view)
        self.list.scrollTo(self.model.index(self._sel, 0))
        self.list.viewport().update()

    def cycle_cat(self):
        i = CATS.index(self._cat) if self._cat in CATS else 0
        self._cat = CATS[(i + 1) % len(CATS)]
        self._sel = 0
        self.search.setPlaceholderText(
            "搜索剪贴历史 · 类型:%s" % CAT_LABEL[self._cat])
        self.refresh()

    def act_paste(self, plain=False):
        hi, rec = self._rec_at(self._sel)
        if rec is None:
            return
        if plain:
            rec = plain_rec(rec)
        target = self._prev_hwnd or getattr(self.app, "_paste_target", None)
        # 先收起并还原焦点,再写剪贴板排队回贴;目标窗口传入粘贴事务,
        # 发送前校验前台与剪贴板序列号,切换即取消(worker 内置宽限循环)
        self.close_restore()
        try:
            self.app.core.paste_record(rec, force=True, target_hwnd=target)
        except Exception:
            cm.log("快捷粘贴失败:\n%s" % __import__("traceback").format_exc())

    def act_view(self):
        hi, rec = self._rec_at(self._sel)
        if rec is None:
            return
        self._silent_hide()
        try:
            self.app.open_viewer(rec)
        except Exception:
            pass

    def act_del(self):
        hi, rec = self._rec_at(self._sel)
        if rec is None:
            return
        try:
            self.app.core.soft_delete_at(hi)
            self.app._offer_undo(1, "已删除")
        except Exception:
            pass
        self.refresh()
