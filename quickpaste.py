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

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFontMetrics, QPainter, QPainterPath,
                           QPen, QStandardItemModel)
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)
from qfluentwidgets import CaptionLabel, SearchLineEdit

import clipboard_manager as cm
import ui_base

CATS = ("all", "text", "link", "image", "files")
CAT_LABEL = {"all": "全部", "text": "文本", "link": "链接",
             "image": "图片", "files": "文件"}
GLYPH = {"text": "🧾", "link": "🔗", "image": "🖼", "files": "📁"}
ROW_H = 50
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
    """单行绘制:类型瓦片 + 标题 + 元信息 + 序号。

    与主界面卡片同一套暖纸语言(白底、1px 边框、选中淡陶土 + 左侧指示条);
    图标用 Fluent 矢量图标,不用 emoji(emoji 在中文 Windows 上是彩色字形,
    和其余界面不是一套视觉)。
    """

    _ICONS = {"text": "DOCUMENT", "link": "LINK", "image": "PHOTO",
              "files": "FOLDER"}

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def _icon(self, kind):
        from qfluentwidgets import FluentIcon as FIF
        return getattr(FIF, self._ICONS.get(kind, "DOCUMENT"), FIF.DOCUMENT).icon()

    def paint(self, p, opt, index):
        rec = index.data(Qt.UserRole)
        if rec is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(4, 2, -4, -2)
        sel = index.row() == self.win._sel
        hover = bool(opt.state & QStyle.State_MouseOver)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        if sel:
            c = QColor(ui_base.ACCENT)
            c.setAlpha(22)
            p.fillPath(path, c)
            border = QColor(ui_base.ACCENT)
            border.setAlpha(110)
        elif hover:
            p.fillPath(path, QColor(ui_base.SURFACE_HOVER))
            border = QColor(ui_base.BORDER_HOVER)
        else:
            p.fillPath(path, QColor(ui_base.CARD_BG))
            border = QColor(ui_base.LINE)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(border, 1))
        p.drawPath(path)
        if sel:
            bar = QPainterPath()
            bar.addRoundedRect(QRectF(r.left(), r.top(), 3, r.height()), 1.5, 1.5)
            p.fillPath(bar, QColor(ui_base.ACCENT))

        # 类型瓦片:有缩略图就贴缩略图(裁圆角),否则用矢量图标
        tile = QRectF(r.left() + 8, r.center().y() - 15, 30, 30)
        tp = QPainterPath()
        tp.addRoundedRect(tile, 8, 8)
        p.fillPath(tp, QColor(ui_base.BADGE_BG))
        thumb = self.win.thumb_of(rec)
        if thumb is not None and not thumb.isNull():
            p.save()
            p.setClipPath(tp)
            pm = thumb.scaled(int(tile.width()), int(tile.height()),
                              Qt.KeepAspectRatioByExpanding,
                              Qt.SmoothTransformation)
            p.drawPixmap(int(tile.center().x() - pm.width() / 2),
                         int(tile.center().y() - pm.height() / 2), pm)
            p.restore()
        else:
            ico = self._icon(rec.get("type"))
            if not ico.isNull():
                ico.paint(p, QRectF(tile.center().x() - 8,
                                    tile.center().y() - 8, 16, 16).toRect())

        # 标题 + 元信息
        fx = tile.right() + 10
        fw = r.right() - fx - 34
        tf = ui_base.font(10)
        p.setFont(tf)
        p.setPen(QColor(ui_base.TEXT))
        title = self.win.title_of(rec)
        if rec.get("pinned"):
            title = "📌 " + title
        p.drawText(QRectF(fx, r.top() + 7, fw, 18).toRect(),
                   Qt.AlignVCenter,
                   QFontMetrics(tf).elidedText(title, Qt.ElideRight, int(fw)))
        mf = ui_base.font(8)
        p.setFont(mf)
        p.setPen(QColor(ui_base.TER))
        p.drawText(QRectF(fx, r.top() + 25, fw, 14).toRect(), Qt.AlignVCenter,
                   QFontMetrics(mf).elidedText(self.win.meta_of(rec),
                                               Qt.ElideRight, int(fw)))
        # 序号(Ctrl+数字直选)
        row = index.row()
        if row < 9:
            p.setFont(mf)
            p.setPen(QColor(ui_base.ACCENT_TEXT if sel else ui_base.TER))
            p.drawText(QRectF(r.right() - 28, r.top(), 20, r.height()).toRect(),
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
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(6)
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
        # 细滚动条:默认样式在无边框浮层里是一根黑条,和暖纸底不搭
        self.list.setStyleSheet(
            "QListView { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px;"
            " margin: 2px 0 2px 0; }"
            "QScrollBar::handle:vertical { background: #DCD9D0;"
            " border-radius: 3px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: #C9C5BA; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            " height: 0px; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            " background: transparent; }")
        self.list.setViewportMargins(0, 2, 0, 2)
        self.list.clicked.connect(self._on_click)
        lay.addWidget(self.list, 1)

        self.footer = CaptionLabel("", self)
        self.footer.setMinimumHeight(18)
        self.footer.setStyleSheet("color: %s;" % ui_base.TER)
        lay.addWidget(self.footer)

        self.model = QStandardItemModel(self)
        self.list.setModel(self.model)
        self._sync_footer()
        self._apply_surface()

    def _apply_surface(self):
        """浮层底板:暖白 + 1px 边(无边框窗口靠 QSS 画边界,不做透明圆角)。"""
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "QuickPasteWindow { background: %s; border: 1px solid %s; }"
            % (ui_base.BG, ui_base.LINE))

    # ---- 状态查询(供 delegate 回调) ----
    def thumb_of(self, rec):
        if rec.get("type") != "image":
            return None
        path = rec.get("content")
        pm = (getattr(self.app, "_thumbs", None) or {}).get(path)
        if pm is None:
            tc = getattr(self.app, "thumb_cached", None)
            if tc is not None:
                pm = tc(path)        # 可见请求:高优先级后台生成
        if pm is None:
            loader = getattr(self.app, "_thumb_loader", None)
            if loader is not None:
                loader.submit(path, visible=True)
                # 后台生成,完成后经 on_thumb 重绘
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
                "↑↓ 选择 · Enter 粘贴 · Alt+Enter 贴上下文包 · Ctrl+Q 调 AI"
                " · Ctrl+B 入包 · Tab 类型:%s · Esc 关闭%s"
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
                if m & Qt.AltModifier:
                    self.act_paste_pack()
                elif m & Qt.ControlModifier:
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
            if (m & Qt.ControlModifier) and k == Qt.Key_Q:
                self.act_ask_ai()
                return True
            if (m & Qt.ControlModifier) and k == Qt.Key_B:
                _, rec = self._rec_at(self._sel)
                if rec is not None:
                    try:
                        from ctxpack.ui_tray import add_records_to_pack
                        added, _ = add_records_to_pack(self.app, [rec])
                        if added:
                            self.footer.setText("✓ 已加入上下文包 · 可继续选择,Esc 关闭")
                    except Exception:
                        self.footer.setText("✗ 入包失败,详情见 app.log")
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
        # 先写剪贴板:失败不按成功流程收起浮层(留在原地供重试/换条目),
        # 也不派发回贴;成功才收起还原焦点。目标窗口传入粘贴事务,
        # 发送前校验前台与剪贴板序列号,切换即取消(worker 内置宽限循环)。
        try:
            ok = self.app.core.paste_record(rec, force=True, target_hwnd=target)
        except Exception:
            cm.log("快捷粘贴失败:\n%s" % __import__("traceback").format_exc())
            ok = False
        if not ok:
            self.footer.setText("剪贴板写入失败,请重试(Enter 再试 / Esc 返回)")
            return
        self.close_restore()

    def act_view(self):
        hi, rec = self._rec_at(self._sel)
        if rec is None:
            return
        self._silent_hide()
        try:
            self.app.open_viewer(rec)
        except Exception:
            pass

    def act_paste_pack(self):
        """Alt+Enter:当前上下文包拼装结果直接贴回原窗口。

        拼装含已放行的敏感条目(与「复制拼装」一致的本地语义);
        空篮/写失败留在浮层原地,footer 给出下一步提示。"""
        try:
            from ctxpack.ui_tray import tray_service
            a = tray_service(self.app).assemble_active(include_sensitive=True)
        except Exception:
            a = None
        if a is None or not getattr(a, "ok", False) or not (a.text or "").strip():
            self.footer.setText("上下文篮为空 · 右键卡片「加入上下文包」后再按 Alt+Enter")
            return
        target = self._prev_hwnd or getattr(self.app, "_paste_target", None)
        try:
            ok = self.app.core.paste_text(a.text, force=True, target_hwnd=target)
        except Exception:
            cm.log("贴包失败:\n%s" % __import__("traceback").format_exc())
            ok = False
        if not ok:
            self.footer.setText("剪贴板写入失败,请重试(Alt+Enter 再试 / Esc 返回)")
            return
        self.close_restore()

    def act_ask_ai(self):
        """Ctrl+Q:把选中条目(或上下文包)交给快速 AI;原窗口记为回贴目标。"""
        if self._prev_hwnd:
            self.app._paste_target = self._prev_hwnd
        hi, rec = self._rec_at(self._sel)
        self._silent_hide()
        try:
            if rec is not None and rec.get("type") == "text":
                self.app.open_quick_ai(rec)
                return
            qa = getattr(self.app, "_quick_ai", None)
            if qa is not None:
                qa.open_with_context()
            elif hasattr(self.app, "open_quick_ai"):
                self.app.open_quick_ai()
        except Exception:
            cm.log("快速 AI 拉起失败:\n%s" % __import__("traceback").format_exc())
            self.footer.setText("快速 AI 拉起失败,详情见 app.log")
            self.show()

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
