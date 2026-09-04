#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinClipboard - PySide6 + PyQt-Fluent-Widgets 版
===============================================
UI 基于 Qt6 + qfluentwidgets(亚克力毛玻璃窗口 / Segmented 分段导航 /
SearchLineEdit / Card 卡片 / RoundMenu / InfoBar),后端逻辑复用
clipboard_manager.py(热键 / 托盘 / 剪贴板 / 敏感隔离 / 持久化)。
"""
import os
import sys
import io
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm   # 后端复用

from PySide6.QtCore import (Qt, QTimer, QRectF, QSize, QEvent, QPropertyAnimation,
                            QEasingCurve, QObject)
from PySide6.QtGui import (QGuiApplication, QPixmap, QImage, QPainter, QColor, QFont,
                           QIcon, QPainterPath, QFontMetrics, QPen, QCursor)
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
                               QFrame, QSlider, QRadioButton, QGraphicsView,
                               QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
                               QFileDialog, QButtonGroup, QSizePolicy, QListWidget,
                               QInputDialog)

from qfluentwidgets import (FluentIcon as FIF, setTheme, Theme,
                            setThemeColor, CardWidget,
                            SearchLineEdit, SegmentedWidget, ScrollArea, ToolButton,
                            TransparentToolButton, PrimaryPushButton, PushButton,
                            RoundMenu, MessageBox, MessageBoxBase, InfoBar,
                            InfoBarPosition, BodyLabel, StrongBodyLabel,
                            CaptionLabel, TitleLabel)
from PySide6.QtWidgets import QGraphicsDropShadowEffect

# ============================================================
# 主题与调色板
# ============================================================
# 注意:setTheme/setThemeColor 需在 QApplication 创建后调用(见 main)

ACCENT = "#0071E3"
ACCENT_SOFT = "#E3F0FD"   # 强调色浅底(徽章/胶囊/悬停)
BG = "#F5F5F7"
TEXT = "#1D1D1F"
SEC = "#6E6E73"
TER = "#86868B"
LINE = "#E4E4E9"          # 发丝描边
PANEL_W = 520
PANEL_H_MAX = 640
MARGIN = 26          # 窗口四周留白(投影区域)
RADIUS = 16          # 面板圆角(区域裁剪)


def _fic(name, fallback_text):
    icon = getattr(FIF, name, None)
    return (icon, None) if icon is not None else (None, fallback_text)


def font(px, weight=QFont.Normal):
    f = QFont("Segoe UI", px)
    f.setWeight(weight)
    return f


def elided(text, f, maxw):
    return QFontMetrics(f).elidedText(text, Qt.ElideRight, maxw)


def pil_to_qimage(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return QImage.fromData(buf.getvalue(), "PNG")


def rounded_pixmap(pm, radius):
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, pm.width(), pm.height()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def apply_round_region(widget, r=RADIUS):
    """Win32 圆角裁剪(SetWindowRgn 用物理像素,需按 DPI 换算,否则高 DPI 下会把内容裁掉)。"""
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(widget.winId())
        # Qt 的 width()/height() 是逻辑像素,必须在 150% DPI 下换算成物理像素,
        # 否则区域只有逻辑尺寸大小,窗口右边/下边会被 SetWindowRgn 裁掉。
        try:
            dpr = widget.devicePixelRatio() or 1.0
        except Exception:
            dpr = 1.0
        w = int(round(widget.width() * dpr))
        h = int(round(widget.height() * dpr))
        r_px = int(round(r * 2 * dpr))
        gdi = ctypes.WinDLL("gdi32")
        user32 = ctypes.WinDLL("user32")
        rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, r_px, r_px)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def _phys_screen():
    """枚举真实显示器物理分辨率(远程/虚拟化会话中 Qt 屏幕尺寸可能偏大)。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DEVMODE(ctypes.Structure):
            _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
                        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                        ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                        ("dmOrientation", ctypes.c_short), ("dmPaperSize", ctypes.c_short),
                        ("dmPaperLength", ctypes.c_short), ("dmPaperWidth", ctypes.c_short),
                        ("dmScale", ctypes.c_short), ("dmCopies", ctypes.c_short),
                        ("dmDefaultSource", ctypes.c_short), ("dmPrintQuality", ctypes.c_short),
                        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                        ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
                        ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                        ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                        ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        user32 = ctypes.WinDLL("user32")
        if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    except Exception:
        pass
    return None


def ensure_onscreen(w):
    """把窗口强制移动到当前屏幕可见范围内(与 Qt 同一逻辑坐标系)。"""
    try:
        scr = QGuiApplication.screenAt(w.frameGeometry().center()) or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
    except Exception:
        return
    f = w.frameGeometry()
    x = min(max(f.x(), geo.left()), max(geo.left(), geo.right() - f.width()))
    y = min(max(f.y(), geo.top()), max(geo.top(), geo.bottom() - f.height()))
    if (x, y) != (f.x(), f.y()):
        w.move(x, y)


class Badge(QLabel):
    """键盘徽章:↑↓ / Enter / Esc"""
    def __init__(self, text):
        super().__init__(text)
        self.setFont(font(10))
        self.setStyleSheet(
            "QLabel { background: rgba(0,0,0,0.05); color: %s; border-radius: 5px;"
            " padding: 3px 8px; }" % SEC)


class Card(CardWidget):
    """剪贴板条目卡片。"""

    THUMB = (92, 68)

    def __init__(self, app, rec):
        super().__init__()
        self.app = app
        self.rec = rec
        self.setObjectName("clipCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QFrame#clipCard { background: #FDFDFE; border-radius: 14px;"
            " border: 1px solid rgba(0,0,0,0.03); }"
            "QFrame#clipCard:hover { background: #FFFFFF; border: 1px solid %s; }"
            "QFrame#clipCard[selected=\"true\"] { background: #FFFFFF;"
            " border: 1.5px solid %s; }" % (LINE, ACCENT))
        self._build()

    def set_selected(self, on):
        if self.property("selected") != on:
            self.setProperty("selected", on)
            self.style().unpolish(self)
            self.style().polish(self)

    def update_pin(self):
        """复用卡片时刷新置顶指示点。"""
        self.dot.setStyleSheet(
            "background: %s; border-radius: 4px;"
            % (ACCENT if self.rec.get("pinned") else "transparent"))

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(14)
        rec = self.rec
        t = rec["type"]

        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet(
            "background: %s; border-radius: 4px;" % (ACCENT if rec.get("pinned") else "transparent"))

        left = QVBoxLayout()
        left.setAlignment(Qt.AlignTop)

        if t == "image":
            pm = self.app.thumb_for(rec["content"])
            if pm is None:
                pm = QPixmap(*self.THUMB)
                pm.fill(QColor("#E8E8ED"))
            thumb = QLabel()
            thumb.setPixmap(rounded_pixmap(pm, 10))
            thumb.setFixedSize(*self.THUMB)
            thumb.setStyleSheet("border: 1px solid rgba(0,0,0,0.06); border-radius: 10px;")
            left.addWidget(thumb)
            title = StrongBodyLabel("截图")
            sub = BodyLabel("%s × %s" % (rec.get("w", 0), rec.get("h", 0)))
            meta = CaptionLabel(self._meta("PNG"))
        elif t == "files":
            names = rec["content"]
            name = os.path.basename(names[0])
            ext = os.path.splitext(name)[1].lstrip(".").upper()[:4] or "FILE"
            badge = QLabel(ext)
            badge.setFixedSize(44, 44)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFont(font(10, QFont.Bold))
            badge.setStyleSheet(
                "background: %s; color: %s; border-radius: 12px;" % (ACCENT_SOFT, ACCENT))
            left.addWidget(badge)
            title = StrongBodyLabel(name + (" 等 %d 个" % len(names) if len(names) > 1 else ""))
            sub = None
            size = ""
            try:
                total = sum(os.path.getsize(p) for p in names if os.path.exists(p))
                size = cm._fmt_size(total)
            except Exception:
                pass
            meta = CaptionLabel(" · ".join(x for x in (ext, size, cm._rel_time(rec.get("ts", 0))) if x))
        elif cm.url_info(rec["content"]):
            host = cm.url_info(rec["content"])[0]
            badge = QLabel(host[0].upper())
            badge.setFixedSize(28, 28)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFont(font(12, QFont.Bold))
            badge.setStyleSheet(
                "background: %s; color: #FFFFFF; border-radius: 14px;" % ACCENT)
            left.addWidget(badge)
            title = StrongBodyLabel(host)
            sub = BodyLabel(rec["content"].strip())
            meta = CaptionLabel("链接 · " + cm._rel_time(rec.get("ts", 0)))
        else:
            lines = [l for l in rec["content"].strip().splitlines() if l.strip()]
            badge = QLabel("文")
            badge.setFixedSize(28, 28)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFont(font(12, QFont.Bold))
            badge.setStyleSheet(
                "background: %s; color: %s; border-radius: 9px;" % (ACCENT_SOFT, ACCENT))
            left.addWidget(badge)
            title = StrongBodyLabel(lines[0] if lines else "")
            sub = BodyLabel("\n".join(lines[1:3])) if len(lines) > 1 else None
            meta = CaptionLabel("文本 · " + cm._rel_time(rec.get("ts", 0)))

        root.addLayout(left)

        mid = QVBoxLayout()
        mid.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(self.dot)
        title.setFont(font(14, QFont.DemiBold))
        title.setStyleSheet("color: %s;" % TEXT)
        title.setMaximumWidth(330)
        title.setText(elided(title.text(), title.font(), 330))
        title_row.addWidget(title)
        title_row.addStretch(1)
        mid.addLayout(title_row)
        if sub is not None:
            sub.setFont(font(13))
            sub.setStyleSheet("color: %s;" % SEC)
            sub.setMaximumWidth(330)
            sub.setText(elided(sub.text(), sub.font(), 330))
            mid.addWidget(sub)
        meta.setFont(font(11))
        meta.setStyleSheet("color: %s;" % TER)
        mid.addWidget(meta)
        root.addLayout(mid, 1)

        # 悬停操作按钮(fluent 图标,圆形描边胶囊)
        self.act_btns = []
        actbox = QHBoxLayout()
        actbox.setSpacing(6)
        for act, icon_name, tip in (
                ("paste", "COPY", "粘贴"), ("pin", "PIN", "固定"),
                ("del", "DELETE", "删除"), ("more", "MORE", "更多")):
            icon, txt = _fic(icon_name, {"paste": "贴", "pin": "钉",
                                         "del": "删", "more": "⋯"}[act])
            if icon is not None:
                b = ToolButton(icon, self)
                b.setIconSize(QSize(14, 14))
                b.setStyleSheet(
                    "QToolButton { background: rgba(255,255,255,0.95);"
                    " border: 1px solid %s; border-radius: 15px; }"
                    "QToolButton:hover { background: %s; border-color: %s; }"
                    % (LINE, ACCENT_SOFT, ACCENT))
            else:
                b = PushButton(txt, self)
                b.setFont(font(10))
                b.setStyleSheet(
                    "QPushButton { background: #FFFFFF; border: 1px solid %s;"
                    " border-radius: 15px; color: %s; }"
                    "QPushButton:hover { background: %s; border-color: %s; color: %s; }"
                    % (LINE, SEC, ACCENT_SOFT, ACCENT, ACCENT))
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=act: self.app.card_action(a, self))
            b.setVisible(False)
            actbox.addWidget(b)
            self.act_btns.append(b)
        actw = QWidget()
        actw.setLayout(actbox)
        root.addWidget(actw, 0)
        root.setAlignment(actw, Qt.AlignVCenter)

    def _meta(self, kind):
        return " · ".join(x for x in (kind, cm._rel_time(self.rec.get("ts", 0))) if x)

    def enterEvent(self, e):
        for b in self.act_btns:
            b.setVisible(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        for b in self.act_btns:
            b.setVisible(False)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.app.select_card(self)
        elif e.button() == Qt.RightButton:
            self.app.select_card(self)
            self.app.card_menu(self, e.globalPosition().toPoint())
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.app.paste_rec(self.rec)
        super().mouseDoubleClickEvent(e)


# ============================================================
# 主面板(无边框透明窗口 + Fluent 组件)
# ============================================================
class PanelWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("WinClipboard")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)

        # 面板本体(圆角 + 投影;远程会话下软件渲染昂贵,跳过阴影)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        self.panel.setStyleSheet(
            "QFrame#panel { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            " stop:0 #F8F8FA, stop:1 #F1F1F4); border-radius: %dpx; }" % RADIUS)
        try:
            import ctypes
            remote = bool(ctypes.windll.user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION
        except Exception:
            remote = False
        if not remote:
            shadow = QGraphicsDropShadowEffect(self.panel)
            shadow.setBlurRadius(36)
            shadow.setOffset(0, 8)
            shadow.setColor(QColor(0, 0, 0, 80))
            self.panel.setGraphicsEffect(shadow)
        outer.addWidget(self.panel)

        v = QVBoxLayout(self.panel)
        v.setContentsMargins(18, 12, 18, 10)
        v.setSpacing(8)

        # ---- 头部 ----
        hdr = QHBoxLayout()
        icon_lbl = QLabel()
        try:
            icon_lbl.setPixmap(self.app.icon.pixmap(22, 22))
        except Exception:
            pass
        title = TitleLabel("WinClipboard")
        title.setFont(font(17, QFont.DemiBold))
        title.setStyleSheet("color: %s;" % TEXT)
        chip = CaptionLabel("Ctrl + Shift + V")
        chip.setStyleSheet(
            "QLabel { background: %s; color: %s; border-radius: 8px;"
            " padding: 3px 8px; }" % (ACCENT_SOFT, ACCENT))
        close = TransparentToolButton(FIF.CLOSE, self)
        close.setFixedSize(26, 26)
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.hide_panel)
        hdr.addWidget(icon_lbl)
        hdr.addSpacing(8)
        hdr.addWidget(title)
        hdr.addStretch(1)
        hdr.addWidget(chip)
        hdr.addSpacing(4)
        hdr.addWidget(close)
        v.addLayout(hdr)

        # ---- 搜索 ----
        self.search = SearchLineEdit(self)
        self.search.setObjectName("search")
        self.search.setFixedHeight(46)
        self.search.setPlaceholderText("搜索剪贴板内容")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(
            "LineEdit#search { background: #FFFFFF; border-radius: 14px;"
            " border: 1px solid rgba(0,0,0,0.06); font-size: 15px;"
            " padding: 0 36px 0 38px; color: %s; }"
            "LineEdit#search:focus { border: 1.5px solid %s; background: #FFFFFF; }"
            % (TEXT, ACCENT))
        self.search.textChanged.connect(self.app.on_search)
        self.search.returnPressed.connect(self.app.paste_selected)
        self.search.installEventFilter(self)
        v.addWidget(self.search)

        # ---- 分类导航 ----
        self.nav = SegmentedWidget(self)
        for label, cat in (("全部", "all"), ("文本", "text"), ("图片", "image"),
                           ("链接", "link"), ("文件", "files")):
            self.nav.addItem(routeKey=cat, text=label)
        # 注意:addItem 的 onClick 收到的是 itemClicked(bool) 的布尔值,不能用于回传分类;
        # 改用 currentItemChanged(str),它携带 routeKey 字符串。
        self.nav.currentItemChanged.connect(self.app.set_category)
        self.nav.setCurrentItem("all")
        v.addWidget(self.nav)
        self.nav.setStyleSheet(
            "SegmentedWidget { background: rgba(0,0,0,0.05); border-radius: 12px; }")
        self.nav.setIndicatorColor(ACCENT, ACCENT)

        # ---- 卡片流 ----
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setViewportMargins(0, 0, 6, 0)
        self.content = QWidget()
        self.cards_layout = QVBoxLayout(self.content)
        self.cards_layout.setContentsMargins(2, 4, 2, 4)
        self.cards_layout.setSpacing(10)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.content)
        v.addWidget(self.scroll, 1)

        # 空状态提示(复用单个部件,避免每次刷新重建)
        self._empty_box = QWidget()
        eb = QVBoxLayout(self._empty_box)
        eb.setContentsMargins(0, 40, 0, 40)
        eb.setSpacing(6)
        t1 = BodyLabel("📋 暂无剪贴内容")
        t1.setAlignment(Qt.AlignCenter)
        t1.setStyleSheet("color: %s; font-size: 15px;" % SEC)
        t2 = CaptionLabel("复制文字、截图或文件后,按 Ctrl + Shift + V 打开")
        t2.setAlignment(Qt.AlignCenter)
        t2.setStyleSheet("color: %s;" % TER)
        eb.addWidget(t1)
        eb.addWidget(t2)
        self._empty_box.setVisible(False)

        # ---- 底栏 ----
        foot = QHBoxLayout()
        foot.setContentsMargins(4, 6, 2, 2)
        self.lbl_count = CaptionLabel("0 个项目")
        self.lbl_count.setStyleSheet("color: %s;" % TER)
        foot.addWidget(self.lbl_count)
        foot.addStretch(1)
        for btxt in ("↑ ↓", "Enter", "Esc"):
            foot.addWidget(Badge(btxt))
        v.addLayout(foot)

        self.setFixedWidth(PANEL_W + 2 * MARGIN)
        self._cards = []

    # ---- 事件 ----
    def eventFilter(self, obj, e):
        if obj is self.search:
            if e.type() == QEvent.KeyPress:
                k = e.key()
                if k in (Qt.Key_Down, Qt.Key_Up):
                    self.app.move_sel(1 if k == Qt.Key_Down else -1)
                    return True
                if k == Qt.Key_Escape:
                    self.hide_panel()
                    return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide_panel()
        elif e.key() == Qt.Key_Down:
            self.app.move_sel(1)
        elif e.key() == Qt.Key_Up:
            self.app.move_sel(-1)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.app.paste_selected()
        else:
            super().keyPressEvent(e)

    # ---- 拖拽移动 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if hasattr(self, "_drag") and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if hasattr(self, "_drag"):
            del self._drag
        super().mouseReleaseEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        apply_round_region(self)

    # ---- 显隐 ----
    def show_panel(self):
        self.app.refresh_view()
        self._resize_to_content()
        self._place_near_cursor()
        self.search.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def hide_panel(self):
        self.hide()

    def _resize_to_content(self):
        h = self.content.sizeHint().height()
        panel_h = min(248 + max(h, 60), PANEL_H_MAX)
        self.setFixedHeight(panel_h + 2 * MARGIN)

    def _place_near_cursor(self):
        pos = QCursor.pos()
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        x = pos.x() - 24
        y = pos.y() - 20
        if x + self.width() > geo.right() - 4:
            x = geo.right() - self.width() - 4
        if y + self.height() > geo.bottom() - 4:
            y = geo.bottom() - self.height() - 4
        x = max(geo.left(), x)
        y = max(geo.top(), y)
        self.move(x, y)
        ensure_onscreen(self)

    # ---- 列表(卡片池复用:搜索/分类切换零重建) ----
    def set_view(self, view, history):
        pool = getattr(self, "_pool", {})
        for c in self._cards:
            pool[id(c.rec)] = c
        self._empty_box.setVisible(False)
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
        used = set()
        self._cards = []
        self.app._card_widgets = []
        pinned = [i for i in view if history[i].get("pinned")]
        rest = [i for i in view if not history[i].get("pinned")]
        for label, idxs in (("已固定", pinned), ("最近项目", rest)):
            if not idxs:
                continue
            lab = CaptionLabel(("📌 " if label == "已固定" else "") + label)
            lab.setStyleSheet("color: %s; font-size: 12px; font-weight: 600;" % SEC)
            lab.setContentsMargins(6, 10, 0, 4)
            self.cards_layout.addWidget(lab)
            for i in idxs:
                rec = history[i]
                card = pool.get(id(rec))
                if card is None:
                    card = Card(self.app, rec)
                else:
                    card.update_pin()
                    card.show()
                used.add(id(rec))
                self.cards_layout.addWidget(card)
                self._cards.append(card)
                self.app._card_widgets.append(card)
        self.cards_layout.addStretch(1)
        # 未使用的卡片隐藏(保留复用)
        for rid, c in pool.items():
            if rid not in used:
                c.hide()
        self._pool = pool
        if not view:
            self._empty_box.setVisible(True)
            self.cards_layout.insertWidget(0, self._empty_box)

    def select_index(self, vi):
        for i, c in enumerate(self.app._card_widgets):
            c.set_selected(i == vi)

    def ensure_visible(self, vi):
        if 0 <= vi < len(self.app._card_widgets):
            self.scroll.ensureWidgetVisible(self.app._card_widgets[vi], 0, 20)


# ============================================================
# 私密区窗口
# ============================================================
class PrivateWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("私密内容(敏感信息隔离区)")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(480, 340)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(8)
        hint = CaptionLabel("以下内容保存在本机私密区,不出现在剪贴板历史面板中。")
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)
        self.lst = QListWidget()
        self.lst.itemDoubleClicked.connect(lambda _: self.do_copy())
        self.lst.setStyleSheet(
            "QListWidget { background: white; border-radius: 10px;"
            " border: 1px solid rgba(0,0,0,0.06); }")
        v.addWidget(self.lst, 1)
        row = QHBoxLayout()
        b_copy = PrimaryPushButton("复制并粘贴", self)
        b_del = PushButton("删除", self)
        b_clear = PushButton("清空", self)
        b_close = PushButton("关闭", self)
        b_copy.clicked.connect(self.do_copy)
        b_del.clicked.connect(self.do_del)
        b_clear.clicked.connect(self.do_clear)
        b_close.clicked.connect(self.close)
        for b in (b_copy, b_del, b_clear, b_close):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    def refresh(self):
        self.lst.clear()
        for s in self.app.private:
            one = s.replace("\n", " ").replace("\t", " ")
            self.lst.addItem(one[:80] + ("…" if len(one) > 80 else ""))

    def do_copy(self):
        item = self.lst.currentItem()
        if not item:
            return
        i = self.lst.row(item)
        self.app.core.paste_text(self.app.private[i])
        self.hide()

    def do_del(self):
        item = self.lst.currentItem()
        if item:
            self.app.private.pop(self.lst.row(item))
            cm.save_private(self.app.private)
            self.refresh()

    def do_clear(self):
        box = MessageBox("私密内容", "确定清空全部私密内容?", self)
        if box.exec():
            self.app.private.clear()   # 原地清空,保持核心引用有效
            cm.save_private(self.app.private)
            self.refresh()


# ============================================================
# 图片编辑器
# ============================================================
class CompressDialog(MessageBoxBase):
    """压缩 / 格式转换对话框。

    基于 MessageBoxBase:内容放 viewLayout(自适应高度);
    「复制到剪贴板」复用 yesButton,点击时经 validate() 执行压缩。
    (不能直接用 Dialog —— 它 setFixedSize 固定了尺寸,且已有顶层布局,
    再套 QVBoxLayout 不会生效。)
    """

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

        self.titleLabel = StrongBodyLabel("压缩 / 格式转换")
        self.viewLayout.addWidget(self.titleLabel)

        self.quality = QSlider(Qt.Horizontal)
        self.quality.setRange(10, 95)
        self.quality.setValue(80)
        self.viewLayout.addWidget(self.quality)
        self.valueLabel = CaptionLabel("80")
        self.quality.valueChanged.connect(self.valueLabel.setNum)
        self.viewLayout.addWidget(self.valueLabel, 0, Qt.AlignRight)

        self.radioJpg = QRadioButton("JPG(体积小,质量可调)")
        self.radioPng = QRadioButton("PNG(无损)")
        self.radioJpg.setChecked(True)
        self.radioJpg.toggled.connect(self._on_format)
        self.viewLayout.addWidget(self.radioJpg)
        self.viewLayout.addWidget(self.radioPng)

        self.saveButton = PushButton("💾 保存文件…")
        self.saveButton.clicked.connect(self._on_save)
        self.viewLayout.addWidget(self.saveButton, 0, Qt.AlignLeft)

        self.yesButton.setText("复制到剪贴板")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(380)

    def _on_format(self, _):
        # PNG 无损,质量滑条仅对 JPG 有效
        on = self.radioJpg.isChecked()
        self.quality.setEnabled(on)
        self.valueLabel.setEnabled(on)

    def _encode(self):
        """按当前选项编码图片,返回字节流。"""
        buf = io.BytesIO()
        if self.radioJpg.isChecked():
            self.editor.base.save(buf, "JPEG", quality=self.quality.value())
        else:
            self.editor.base.save(buf, "PNG")
        return buf.getvalue()

    def validate(self):
        """点「复制到剪贴板」时执行(qfluentwidgets 扩展点);True 才关闭。"""
        try:
            img = cm.Image.open(io.BytesIO(self._encode()))
            ok = cm.set_dib_to_clipboard(self.editor.app.worker.hwnd,
                                         cm.pil_to_dib(img))
        except Exception:
            MessageBox("WinClipboard", "压缩失败。", self).exec()
            return False
        if ok:
            self.editor.lbl_status.setText("✅ 已压缩并复制到剪贴板")
            return True
        InfoBar.error("复制失败", "剪贴板被占用,请稍后重试", parent=self.editor,
                      position=InfoBarPosition.BOTTOM_LEFT)
        return False

    def _on_save(self):
        if self.radioJpg.isChecked():
            path, _ = QFileDialog.getSaveFileName(
                self, "保存图片", "clipboard_compressed.jpg",
                "JPEG 图片 (*.jpg)")
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "保存图片", "clipboard_compressed.png",
                "PNG 图片 (*.png)")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(self._encode())
        except Exception:
            MessageBox("WinClipboard", "保存失败。", self).exec()
            return
        self.editor.lbl_status.setText("✅ 已保存: " + path)
        self.accept()


class ImageEditor(QWidget):
    def __init__(self, app, rec):
        super().__init__(None)
        self.app = app
        self.rec = rec
        self.setWindowTitle("图片处理 — " + os.path.basename(rec["content"]))
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowIcon(app.icon)
        self.resize(940, 640)

        try:
            src = cm.Image.open(rec["content"])
            if src.mode != "RGB":
                bg = cm.Image.new("RGB", src.size, (255, 255, 255))
                bg.paste(src, mask=src.split()[-1] if src.mode in ("RGBA", "LA") else None)
                src = bg
            self.base = src
        except Exception:
            MessageBox("WinClipboard", "无法打开图片文件。", self).exec()
            self.base = cm.Image.new("RGB", (400, 300), (255, 255, 255))

        self.undo_stack = []
        self.tool = None
        self.pending_text = None
        self._drag_start = None
        self._rubber = None
        self._font = cm._load_font(26)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._tbtns = {}
        for key, label in (("crop", "裁剪"), ("rotl", "⟲ 左旋"), ("rotr", "⟳ 右旋"),
                           ("scale", "缩放"), (None, None),
                           ("rect", "方框"), ("arrow", "箭头"),
                           ("text", "文字"), ("mosaic", "马赛克")):
            if key is None:
                bar.addSpacing(12)
                continue
            b = PushButton(label, self)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._on_tool(k))
            bar.addWidget(b)
            self._tbtns[key] = b
        bar.addStretch(1)
        b_undo = PushButton("↶ 撤销", self)
        b_compress = PushButton("📦 压缩导出", self)
        b_copy = PrimaryPushButton("📋 复制", self)
        b_save = PushButton("💾 保存", self)
        b_undo.clicked.connect(self._on_undo)
        b_compress.clicked.connect(self._on_compress)
        b_copy.clicked.connect(self._on_copy)
        b_save.clicked.connect(self._on_save)
        for b in (b_undo, b_compress, b_save, b_copy):
            bar.addWidget(b)
        v.addLayout(bar)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setStyleSheet("background: #2B2F36; border: none; border-radius: 8px;")
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self._pix_item = QGraphicsPixmapItem()
        self.scene.addItem(self._pix_item)
        v.addWidget(self.view, 1)

        self.lbl_status = CaptionLabel("")
        self.lbl_status.setStyleSheet("color: %s;" % SEC)
        v.addWidget(self.lbl_status)

        self.view.mousePressEvent = self._press
        self.view.mouseMoveEvent = self._move
        self.view.mouseReleaseEvent = self._release
        self._redraw()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    # ---- 工具 ----
    def _on_tool(self, key):
        if key == "rotl":
            self._transform(lambda im: im.rotate(90, expand=True))
            return
        if key == "rotr":
            self._transform(lambda im: im.rotate(-90, expand=True))
            return
        if key == "scale":
            v, ok = QInputDialog.getInt(self, "缩放", "目标百分比(10–300):", 100, 10, 300)
            if ok and v != 100:
                self._transform(lambda im: im.resize(
                    (max(1, int(im.width * v / 100)), max(1, int(im.height * v / 100))),
                    cm.RESAMPLE))
            return
        if key == "text":
            s, ok = QInputDialog.getText(self, "文字标注", "输入标注文字:")
            if ok and s:
                self.tool = "text"
                self.pending_text = s
            self._sync_tools()
            return
        if self.tool == key:
            self.tool = None
        else:
            self.tool = key
        self._sync_tools()

    def _sync_tools(self):
        for k, b in self._tbtns.items():
            b.setChecked(k == self.tool)

    def _transform(self, fn):
        self._push()
        self.base = fn(self.base)
        self._redraw()

    def _push(self):
        self.undo_stack.append(self.base.copy())
        if len(self.undo_stack) > 10:
            self.undo_stack.pop(0)

    def _to_img(self, sp):
        r = self._pix_item.boundingRect()
        sc = self._pix_item.scale()
        return (int((sp.x() - r.x()) / sc), int((sp.y() - r.y()) / sc))

    def _press(self, e):
        if self.tool in ("crop", "rect", "arrow", "mosaic") and e.button() == Qt.LeftButton:
            self._drag_start = e.position()
        elif self.tool == "text" and self.pending_text and e.button() == Qt.LeftButton:
            self._push()
            x, y = self._to_img(self.view.mapToScene(e.position().toPoint()))
            cm._draw_text(self.base, (max(0, x), max(0, y)), self.pending_text, self._font)
            self.pending_text = None
            self.tool = None
            self._sync_tools()
            self._redraw()

    def _move(self, e):
        if self._drag_start is not None:
            p = e.position()
            r = QRectF(self._drag_start, p).normalized()
            if self._rubber is None:
                self._rubber = QGraphicsRectItem(r)
                pen = QPen(QColor("#FF5252"))
                pen.setWidthF(1.5)
                pen.setStyle(Qt.DashLine)
                self._rubber.setPen(pen)
                self.scene.addItem(self._rubber)
            else:
                self._rubber.setRect(r)

    def _release(self, e):
        if self._drag_start is None:
            return
        start = self.view.mapToScene(self._drag_start.toPoint())
        end = self.view.mapToScene(e.position().toPoint())
        self._drag_start = None
        if self._rubber is not None:
            self.scene.removeItem(self._rubber)
            self._rubber = None
        ix0, iy0 = self._to_img(start)
        ix1, iy1 = self._to_img(end)
        ix0, ix1 = sorted((ix0, ix1))
        iy0, iy1 = sorted((iy0, iy1))
        ix0, iy0 = max(0, ix0), max(0, iy0)
        ix1, iy1 = min(self.base.width, ix1), min(self.base.height, iy1)
        if ix1 - ix0 < 3 or iy1 - iy0 < 3:
            return
        self._push()
        if self.tool == "crop":
            self.base = self.base.crop((ix0, iy0, ix1, iy1))
        elif self.tool == "rect":
            cm.ImageDraw.Draw(self.base).rectangle(
                (ix0, iy0, ix1, iy1), outline=(255, 40, 40), width=4)
        elif self.tool == "arrow":
            cm._draw_arrow(self.base, (ix0, iy0), (ix1, iy1))
        elif self.tool == "mosaic":
            cm._mosaic(self.base, (ix0, iy0, ix1, iy1))
        self.tool = None
        self._sync_tools()
        self._redraw()

    def _redraw(self):
        w, h = self.base.size
        self._pix_item.setPixmap(QPixmap.fromImage(pil_to_qimage(self.base)))
        self.view.resetTransform()
        self.scene.setSceneRect(QRectF(0, 0, w, h))
        self.view.fitInView(self._pix_item, Qt.KeepAspectRatio)
        self._status()

    def _status(self):
        w, h = self.base.size
        tip = {"crop": "拖动框选裁剪区域", "rect": "拖动绘制红色方框",
               "arrow": "拖动绘制箭头", "text": "点击放置文字",
               "mosaic": "拖动框选区域打马赛克"}.get(self.tool, "就绪")
        self.lbl_status.setText("%s   |   尺寸: %s×%s   |   可撤销 %d 步" % (
            tip, w, h, len(self.undo_stack)))

    def _on_undo(self):
        if self.undo_stack:
            self.base = self.undo_stack.pop()
            self._redraw()

    def _on_copy(self):
        ok = cm.set_dib_to_clipboard(self.app.worker.hwnd, cm.pil_to_dib(self.base))
        if ok:
            InfoBar.success("已复制", "图片已复制到剪贴板", parent=self,
                            position=InfoBarPosition.BOTTOM_LEFT)
        else:
            InfoBar.error("复制失败", "剪贴板被占用", parent=self,
                          position=InfoBarPosition.BOTTOM_LEFT)

    def _on_save(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "保存图片", "clipboard_edit.png", "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
        if p:
            fmt = "JPEG" if p.lower().endswith((".jpg", ".jpeg")) else "PNG"
            try:
                self.base.save(p, fmt)
                self.lbl_status.setText("✅ 已保存: " + p)
            except Exception:
                MessageBox("WinClipboard", "保存失败。", self).exec()

    def _on_compress(self):
        CompressDialog(self).exec()


# ============================================================
# 应用主体(后端逻辑不变)
# ============================================================
class WinClipboardApp(QObject):
    # ---- 核心状态别名(只读代理到 self.core;容器原地修改,引用稳定) ----
    @property
    def cfg(self):
        return self.core.cfg

    @property
    def history(self):
        return self.core.history

    @property
    def private(self):
        return self.core.private

    @property
    def worker(self):
        return self.core.worker

    @property
    def signal_q(self):
        return self.core.signal_q

    def _after_history_change(self):
        """核心回调:剪贴捕获入史后,面板可见时合并刷新(防抖)。"""
        if getattr(self, "panel", None) is not None and self.panel.isVisible():
            self._view_timer.start()

    def __init__(self, qapp):
        super().__init__()
        self.qapp = qapp
        self.category = "all"
        self.query = ""
        self.selected = None
        self._view = []
        self._card_widgets = []
        self._warned_hotkey = False
        self._editors = []
        self._thumbs = {}

        cm.ensure_dirs()
        if not os.path.exists(cm.ICON_FILE):
            try:
                cm.make_icon(cm.ICON_FILE)
            except Exception:
                pass
        self.icon = QIcon(cm.ICON_FILE) if os.path.exists(cm.ICON_FILE) else QIcon()

        # 共享核心(状态加载 + 历史迁移 + 后台线程启动)
        self.core = cm.ClipboardCore(on_change=self._after_history_change)

        self.panel = PanelWindow(self)
        self.private_win = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(30)

        # 搜索输入防抖 / 捕获刷新合并(减少重建次数)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self.refresh_view)
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(180)
        self._view_timer.timeout.connect(self.refresh_view)

        QTimer.singleShot(600, self._prewarm)

    # ---- 剪贴板读取 / 历史维护:见 cm.ClipboardCore(共享后端) ----

    # ---- 视图 ----
    def refresh_view(self):
        view = self.core.filter_view(self.category, self.query)
        self._view = view
        self.panel.set_view(view, self.history)
        total = len(self.history)
        if len(view) == total:
            self.panel.lbl_count.setText("%d 个项目" % total)
        else:
            self.panel.lbl_count.setText("%d / %d 个项目" % (len(view), total))
        if view:
            self.selected = 0
            self.panel.select_index(0)
        else:
            self.selected = None

    def on_search(self, text):
        self.query = text
        if self.panel.isVisible():
            self._search_timer.start()   # 120ms 防抖,输入过程中不重建
        else:
            self.refresh_view()

    def set_category(self, cat):
        if cat == self.category:
            return
        self.category = cat
        self.refresh_view()

    def move_sel(self, delta):
        if not self._view:
            return
        cur = self.selected if self.selected is not None else (0 if delta > 0 else -1)
        cur = max(0, min(len(self._view) - 1, cur + delta))
        self.selected = cur
        self.panel.select_index(cur)
        self.panel.ensure_visible(cur)

    def select_card(self, card):
        if card in self._card_widgets:
            self.selected = self._card_widgets.index(card)
            self.panel.select_index(self.selected)

    def paste_selected(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        rec = self.history[self._view[self.selected]]
        self.paste_rec(rec)

    def paste_rec(self, rec):
        if not self.core.paste_record(rec):
            MessageBox("WinClipboard", "文件已不存在,无法粘贴。", self.panel).exec()
            return
        self.panel.hide_panel()

    # ---- 卡片动作 / 菜单 ----
    def card_action(self, act, card):
        self.select_card(card)
        if act == "paste":
            self.paste_selected()
        elif act == "pin":
            self.toggle_pin()
        elif act == "del":
            self.delete_selected()
        elif act == "more":
            self.card_menu(card, card.mapToGlobal(card.rect().topRight()))

    def card_menu(self, card, gpos):
        menu = RoundMenu(parent=self.panel)
        a1 = menu.addAction("粘贴")
        a1.triggered.connect(self.paste_selected)
        rec = card.rec
        if rec["type"] == "image":
            a2 = menu.addAction("图片处理…")
            a2.triggered.connect(lambda: self.open_editor(rec))
        a3 = menu.addAction("取消固定" if rec.get("pinned") else "固定")
        a3.triggered.connect(self.toggle_pin)
        menu.addSeparator()
        a4 = menu.addAction("删除")
        a4.triggered.connect(self.delete_selected)
        menu.exec(gpos)

    def toggle_pin(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        self.core.toggle_pin_at(self._view[self.selected])
        self.refresh_view()

    def delete_selected(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        self.core.delete_at(self._view[self.selected])
        self.refresh_view()

    def open_editor(self, rec):
        if not os.path.exists(rec.get("content", "")):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。", self.panel).exec()
            return
        ed = ImageEditor(self, rec)
        ed.show()
        ed.raise_()
        self._editors.append(ed)

    # ---- 托盘动作 ----
    def clear_all(self):
        self.core.clear_unpinned()
        self.refresh_view()

    def show_private(self):
        if not self.private:
            InfoBar.info("私密内容", "暂无敏感内容", parent=self.panel,
                         position=InfoBarPosition.TOP_RIGHT)
            return
        if self.private_win is None:
            self.private_win = PrivateWindow(self)
        self.private_win.refresh()
        self.private_win.show()
        self.private_win.raise_()
        self.private_win.activateWindow()

    def about(self):
        MessageBox("关于 WinClipboard",
                   "Windows 桌面剪贴板管理器\n\n"
                   "热键: Ctrl+Shift+V 弹出/隐藏\n"
                   "左键托盘图标: 切换面板\n"
                   "右键托盘图标: 菜单\n\n"
                   "数据目录:\n" + cm.APP_DIR, self.panel).exec()

    def _warn_hotkey(self):
        if self._warned_hotkey:
            return
        self._warned_hotkey = True
        MessageBox("WinClipboard",
                   "全局热键(Ctrl+Shift+V)注册失败,可能被其他软件占用。\n"
                   "可在 %s 中修改 hotkey_mod / hotkey_key 后重启。" % cm.CONFIG_FILE,
                   self.panel).exec()

    def quit_app(self):
        self.core.save_all()
        self.worker.cmd_q.put(("quit",))
        self.qapp.quit()

    # ---- 信号轮询 / 预热 ----
    def _poll(self):
        try:
            while True:
                kind, *rest = self.signal_q.get_nowait()
                try:
                    self._handle_signal(kind, rest)
                except Exception:
                    cm.log("处理信号 %r 失败:\n%s" % (kind, traceback.format_exc()))
        except cm.queue.Empty:
            pass

    def _handle_signal(self, kind, rest):
        if kind == "clip":
            item = rest[0]
            if item:
                self.core.add_item(item)
        elif kind == "hotkey":
            if self.panel.isVisible():
                self.panel.hide_panel()
            else:
                self.panel.show_panel()
        elif kind == "tray":
            act = rest[0]
            if act == "show":
                if self.panel.isVisible():
                    self.panel.hide_panel()
                else:
                    self.panel.show_panel()
            elif act == "clear":
                self.clear_all()
            elif act == "private":
                self.show_private()
            elif act == "autostart":
                self.core.toggle_autostart()
            elif act == "about":
                self.about()
            elif act == "hotkey_fail":
                self._warn_hotkey()
            elif act == "quit":
                self.quit_app()

    def thumb_for(self, path):
        if not path or not os.path.exists(path):
            return None
        pm = self._thumbs.get(path)
        if pm is None:
            try:
                img = cm.Image.open(path)
                img.draft("RGB", (Card.THUMB[0] * 4, Card.THUMB[1] * 4))  # JPEG 快速低分辨率解码
                img = img.convert("RGB")
                img = cm.ImageOps.fit(img, Card.THUMB, cm.RESAMPLE, centering=(0.5, 0.5))
                pm = QPixmap.fromImage(pil_to_qimage(img))
                if len(self._thumbs) > 200:
                    self._thumbs.clear()
                self._thumbs[path] = pm
            except Exception:
                return None
        return pm

    def _prewarm(self):
        try:
            for h in self.history:
                if h.get("type") == "image":
                    self.thumb_for(h["content"])
        except Exception:
            pass


# ============================================================
# 入口
# ============================================================
def main():
    cm.install_excepthooks()
    cm.ensure_dirs()
    ok, mutex = cm.single_instance()
    if not ok:
        cm.ctypes.windll.user32.MessageBoxW(
            0, "WinClipboard 已经在运行中(检查系统托盘)。", cm.APP_NAME, 0x40)
        return
    globals()["_MUTEX"] = mutex
    cm.log("启动(Qt+Fluent) pid=%d py=%s %s" %
           (os.getpid(), sys.executable, sys.version.split()[0]))

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setFont(QFont("Segoe UI", 10))
    setTheme(Theme.LIGHT)
    setThemeColor("#0071E3")

    try:
        app = WinClipboardApp(qapp)
    except Exception:
        cm.log("初始化失败:\n" + traceback.format_exc())
        try:
            cm.ctypes.windll.user32.MessageBoxW(
                0, "WinClipboard 发生错误,详情见:\n%s" % cm.LOG_FILE, cm.APP_NAME, 0x10)
        except Exception:
            pass
        return 1
    rc = qapp.exec()
    cm.log("退出")
    return rc


if __name__ == "__main__":
    sys.exit(main())
