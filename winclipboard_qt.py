#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinClipboard - PySide6 + PyQt-Fluent-Widgets 版
===============================================
UI 基于 Qt6 + qfluentwidgets(Segmented 分段导航 / SearchLineEdit /
Card 卡片 / RoundMenu / InfoBar / 面板弹出动画),支持浅色 / 深色主题
(默认跟随 Windows 系统设置,config.json "theme": light/dark/auto 可覆盖),
后端逻辑复用 clipboard_manager.py(热键 / 托盘 / 剪贴板 / 敏感隔离 / 持久化)。
"""
import os
import sys
import io
import json
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm   # 后端复用

from PySide6.QtCore import (Qt, QTimer, QRectF, QSize, QEvent, QPropertyAnimation,
                            QEasingCurve, QObject, QPoint, QThread, Signal)
from PySide6.QtGui import (QGuiApplication, QPixmap, QImage, QPainter, QColor, QFont,
                           QIcon, QPainterPath, QFontMetrics, QPen, QCursor)
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout,
                               QHBoxLayout, QFrame, QGraphicsView,
                               QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
                               QFileDialog, QSizePolicy, QListWidget, QLineEdit)

from qfluentwidgets import (FluentIcon as FIF, setTheme, Theme,
                            setThemeColor, CardWidget,
                            SearchLineEdit, SegmentedWidget, ScrollArea, ToolButton,
                            TransparentToolButton, PrimaryPushButton, PushButton,
                            ToggleButton, Slider, RadioButton, SpinBox, LineEdit,
                            TextEdit, ComboBox,
                            RoundMenu, MessageBox, MessageBoxBase, InfoBar,
                            InfoBarPosition, BodyLabel, StrongBodyLabel,
                            CaptionLabel, TitleLabel)
from PySide6.QtWidgets import QGraphicsOpacityEffect

# ============================================================
# 主题与调色板(浅色 / 深色)
# ============================================================
# 注意:init_palette / setTheme / setThemeColor 需在 QApplication 创建后、
# 构建 UI 之前调用(见 main)。深浅色跟随 Windows 系统设置,可在
# config.json 用 "theme": "dark" / "light" / "auto"(默认)覆盖。

PANEL_W = 520
PANEL_H_MAX = 640
MARGIN = 26          # 窗口四周留白(投影区域)
RADIUS = 16          # 面板圆角(区域裁剪)

_LIGHT = {
    "ACCENT": "#0071E3",
    "ACCENT_SOFT": "#E3F0FD",              # 强调色浅底(徽章/胶囊/悬停)
    "BG": "#F5F5F7",
    "TEXT": "#1D1D1F",
    "SEC": "#6E6E73",
    "TER": "#86868B",
    "LINE": "#E4E4E9",                     # 发丝描边
    "PANEL_TOP": "#F9F9FB",                # 面板渐变(上)
    "PANEL_BOT": "#F1F1F4",                # 面板渐变(下)
    "CARD_BG": "#FDFDFE",
    "CARD_HOVER": "#FFFFFF",
    "CARD_SEL": "#FFFFFF",
    "CARD_BORDER": "rgba(0,0,0,0.045)",
    "FIELD_BG": "#FFFFFF",                 # 输入框/列表底
    "FIELD_BORDER": "rgba(0,0,0,0.07)",
    "BADGE_BG": "rgba(0,0,0,0.05)",        # 键盘徽章/悬停底
    "NAV_BG": "rgba(0,0,0,0.05)",          # 分段导航底
    "THUMB_PH": "#E8E8ED",                 # 缩略图占位
    "THUMB_BORDER": "rgba(0,0,0,0.06)",
    "BTN_BG": "rgba(255,255,255,0.95)",    # 卡片悬停操作按钮
    # 卡片类型徽章色相:(底色, 前景) —— 文本/链接/文件
    "HUE_TEXT": ("#E3F0FD", "#0A6AD8"),
    "HUE_LINK": ("#EAE7FD", "#6A4DE0"),
    "HUE_FILE": ("#FBEDD7", "#B97314"),
}

_DARK = {
    "ACCENT": "#4D9EFF",
    "ACCENT_SOFT": "rgba(77,158,255,0.16)",
    "BG": "#1F1F22",
    "TEXT": "#F1F1F4",
    "SEC": "#A8A8B2",
    "TER": "#80808A",
    "LINE": "#3B3B42",
    "PANEL_TOP": "#2B2B2F",
    "PANEL_BOT": "#232327",
    "CARD_BG": "#2E2E33",
    "CARD_HOVER": "#36363C",
    "CARD_SEL": "#36363C",
    "CARD_BORDER": "rgba(255,255,255,0.06)",
    "FIELD_BG": "#34343A",
    "FIELD_BORDER": "rgba(255,255,255,0.09)",
    "BADGE_BG": "rgba(255,255,255,0.09)",
    "NAV_BG": "rgba(255,255,255,0.06)",
    "THUMB_PH": "#3A3A41",
    "THUMB_BORDER": "rgba(255,255,255,0.08)",
    "BTN_BG": "rgba(58,58,64,0.95)",
    "HUE_TEXT": ("rgba(77,158,255,0.16)", "#6FB2FF"),
    "HUE_LINK": ("rgba(150,124,255,0.17)", "#B4A1FF"),
    "HUE_FILE": ("rgba(240,170,60,0.16)", "#F0B678"),
}

IS_DARK = False


def init_palette(dark):
    """按主题把调色板写入模块级常量(需在构建任何 UI 之前调用一次)。"""
    globals()["IS_DARK"] = bool(dark)
    globals().update(_DARK if dark else _LIGHT)


def _win_prefers_dark():
    """读取 Windows「应用模式」偏好(深色返回 True;读取失败按浅色)。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return int(v) == 0
    except Exception:
        return False


def resolve_theme(cfg):
    """config.json 的 theme(auto/light/dark)→ 是否深色。"""
    pref = str((cfg or {}).get("theme", "auto")).strip().lower()
    if pref == "dark":
        return True
    if pref == "light":
        return False
    return _win_prefers_dark()


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
    """(已停用)SetWindowRgn 圆角裁剪 —— 半透明窗口的圆角由 QSS 像素决定,
    且 SetWindowRgn 会与 WA_TranslucentBackground 的分层表面冲突,
    曾导致面板卡在 alpha=0(窗口存在但不可见,点击穿透)。"""
    return


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
            "QLabel { background: %s; color: %s; border-radius: 6px;"
            " padding: 3px 8px; }" % (BADGE_BG, SEC))


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
            "QFrame#clipCard { background: %s; border-radius: 14px;"
            " border: 1px solid %s; }"
            "QFrame#clipCard:hover { background: %s; border: 1px solid %s; }"
            "QFrame#clipCard[selected=\"true\"] { background: %s;"
            " border: 1.5px solid %s; }"
            % (CARD_BG, CARD_BORDER, CARD_HOVER, LINE, CARD_SEL, ACCENT))
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
                pm.fill(QColor(THUMB_PH))
            thumb = QLabel()
            thumb.setPixmap(rounded_pixmap(pm, 10))
            thumb.setFixedSize(*self.THUMB)
            thumb.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % THUMB_BORDER)
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
            bg, fg = HUE_FILE
            badge.setStyleSheet(
                "background: %s; color: %s; border-radius: 13px;" % (bg, fg))
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
            badge.setFixedSize(30, 30)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFont(font(12, QFont.Bold))
            bg, fg = HUE_LINK
            badge.setStyleSheet(
                "background: %s; color: %s; border-radius: 10px;" % (bg, fg))
            left.addWidget(badge)
            title = StrongBodyLabel(host)
            sub = BodyLabel(rec["content"].strip())
            meta = CaptionLabel("链接 · " + cm._rel_time(rec.get("ts", 0)))
        else:
            lines = [l for l in rec["content"].strip().splitlines() if l.strip()]
            badge = QLabel("文")
            badge.setFixedSize(30, 30)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFont(font(12, QFont.Bold))
            bg, fg = HUE_TEXT
            badge.setStyleSheet(
                "background: %s; color: %s; border-radius: 10px;" % (bg, fg))
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
                    "QToolButton { background: %s;"
                    " border: 1px solid %s; border-radius: 15px; }"
                    "QToolButton:hover { background: %s; border-color: %s; }"
                    % (BTN_BG, LINE, ACCENT_SOFT, ACCENT))
            else:
                b = PushButton(txt, self)
                b.setFont(font(10))
                b.setStyleSheet(
                    "QPushButton { background: %s; border: 1px solid %s;"
                    " border-radius: 15px; color: %s; }"
                    "QPushButton:hover { background: %s; border-color: %s; color: %s; }"
                    % (BTN_BG, LINE, TEXT, ACCENT_SOFT, ACCENT, ACCENT))
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=act: self.app.card_action(a, self))
            b.setVisible(False)
            actbox.addWidget(b)
            self.act_btns.append(b)
        actw = QWidget()
        actw.setLayout(actbox)
        # 固定占位宽度:悬停按钮显隐时不触发行布局重排(滚动经过光标时尤其明显)
        actw.setFixedSize(4 * 30 + 3 * 6, 30)
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

        # 面板本体(圆角;投影见 paintEvent 的静态分层阴影)
        # 注意:不要用 QGraphicsDropShadowEffect —— 效果管线会让滚动时
        # 每一帧都把整个面板离屏渲染一遍再做高斯模糊,实测每步重绘 14ms+,明显卡顿。
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        self.panel.setStyleSheet(
            "QFrame#panel { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            " stop:0 %s, stop:1 %s); border-radius: %dpx;"
            " border: 1px solid %s; }" % (PANEL_TOP, PANEL_BOT, RADIUS, LINE))
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
        ai_btn = TransparentToolButton(FIF.ROBOT, self)
        ai_btn.setFixedSize(26, 26)
        ai_btn.setToolTip("工作流(网页 AI / 人工确认 / 本地 API)")
        ai_btn.setCursor(Qt.PointingHandCursor)
        ai_btn.clicked.connect(self.app.show_ai_hub)
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
        hdr.addWidget(ai_btn)
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
            "LineEdit#search { background: %s; border-radius: 14px;"
            " border: 1px solid %s; font-size: 15px;"
            " padding: 0 36px 0 38px; color: %s; }"
            "LineEdit#search:focus { border: 1.5px solid %s; background: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, ACCENT, FIELD_BG))
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
            "SegmentedWidget { background: %s; border-radius: 12px; }" % NAV_BG)
        self.nav.setIndicatorColor(ACCENT, ACCENT)

        # ---- 卡片流 ----
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setViewportMargins(0, 0, 6, 0)
        # 内容层与滚动区必须显式透明,否则深色主题下缝隙处露出默认白色底
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.content)
        self.cards_layout.setContentsMargins(2, 4, 2, 4)
        self.cards_layout.setSpacing(10)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.content)
        v.addWidget(self.scroll, 1)

        # 空状态提示(复用单个部件,避免每次刷新重建)
        self._empty_box = QWidget()
        eb = QVBoxLayout(self._empty_box)
        eb.setContentsMargins(0, 32, 0, 36)
        eb.setSpacing(8)
        emo = QLabel("📋")
        emo.setFixedSize(64, 64)
        emo.setAlignment(Qt.AlignCenter)
        emo.setFont(font(26))
        emo.setStyleSheet("background: %s; border-radius: 32px;" % BADGE_BG)
        t1 = BodyLabel("暂无剪贴内容")
        t1.setAlignment(Qt.AlignCenter)
        t1.setFont(font(15, QFont.DemiBold))
        t1.setStyleSheet("color: %s;" % SEC)
        t2 = CaptionLabel("复制文字、截图或文件后,按 Ctrl + Shift + V 打开")
        t2.setAlignment(Qt.AlignCenter)
        t2.setStyleSheet("color: %s;" % TER)
        eb.addWidget(emo, 0, Qt.AlignHCenter)
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
        self._anims = []

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

    def paintEvent(self, e):
        """静态软阴影:由外向内逐层填充低透明度圆角矩形,叠加出衰减边缘。

        成本仅为十几次路径填充(亚毫秒),且只在窗口重绘时执行,
        不像 QGraphicsDropShadowEffect 那样把滚动拖入每帧全量模糊。
        """
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = self.panel.geometry()
        peak = 15 if not IS_DARK else 22
        layers = 12
        for i in range(layers, 0, -1):
            t = i / layers                       # 0=贴边 → 1=最外层
            spread = 22 * t
            alpha = int(peak * (1 - t) ** 2) + 2
            if alpha <= 2:
                continue
            rect = QRectF(g.adjusted(-spread, -spread + 7, spread, spread + 7))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(12, 14, 22, alpha))
            p.drawRoundedRect(rect, RADIUS + spread * 0.7, RADIUS + spread * 0.7)
        p.end()

    # ---- 显隐(带淡入上滑 / 淡出动效) ----
    # 注意:不透明度动画用 QGraphicsOpacityEffect 挂在内部 panel 上(纯 Qt 渲染)。
    # 不要对顶层窗口用 windowOpacity 动画 —— 它与 WA_TranslucentBackground 的
    # 分层表面(UpdateLayeredWindow)竞态,面板可能卡在 alpha=0:窗口"可见"但
    # 完全透明,鼠标点击穿透,表现为"面板/按钮点不开"。
    def _fade_effect(self):
        if getattr(self, "_fade", None) is None:
            self._fade = QGraphicsOpacityEffect(self.panel)
            self.panel.setGraphicsEffect(self._fade)
        return self._fade

    def _stop_anims(self):
        # stop() 会同步发出 finished,先断开避免误触发 _after_hide 把面板藏掉
        for a in self._anims:
            try:
                a.finished.disconnect()
            except Exception:
                pass
            a.stop()
        self._anims.clear()

    def show_panel(self):
        self._stop_anims()
        self.app.refresh_view()
        self._resize_to_content()
        self._place_near_cursor()
        self.search.clear()
        end = self.pos()
        start = QPoint(end.x(), end.y() + 12)
        fade = self._fade_effect()
        fade.setOpacity(0.0)          # 先置 0 再 show,避免首帧全不透明闪烁
        self.show()
        self.raise_()
        self.activateWindow()
        fa = QPropertyAnimation(fade, b"opacity", self)
        fa.setDuration(150)
        fa.setStartValue(0.0)
        fa.setEndValue(1.0)
        fa.setEasingCurve(QEasingCurve.OutCubic)
        fa.start()
        self._anims.append(fa)
        pa = QPropertyAnimation(self, b"pos", self)
        pa.setDuration(150)
        pa.setStartValue(start)
        pa.setEndValue(end)
        pa.setEasingCurve(QEasingCurve.OutCubic)
        pa.start()
        self._anims.append(pa)
        self.search.setFocus()

    def hide_panel(self):
        self._stop_anims()
        fade = self._fade_effect()
        fa = QPropertyAnimation(fade, b"opacity", self)
        fa.setDuration(110)
        fa.setStartValue(fade.opacity())
        fa.setEndValue(0.0)
        fa.finished.connect(self._after_hide)
        fa.start()
        self._anims.append(fa)

    def _after_hide(self):
        self.hide()
        self._fade_effect().setOpacity(1.0)   # 复位,供下次淡入起点正确

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
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(520, 400)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)

        head = QHBoxLayout()
        lock = QLabel("🔒")
        lock.setFont(font(18))
        ttl = StrongBodyLabel("私密内容")
        ttl.setFont(font(16, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        head.addWidget(lock)
        head.addSpacing(6)
        head.addWidget(ttl)
        head.addStretch(1)
        v.addLayout(head)

        hint = CaptionLabel("以下内容经敏感识别后隔离保存,不出现在历史面板;使用 Windows DPAPI 加密,仅当前用户可解密。")
        hint.setStyleSheet("color: %s;" % TER)
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.lst = QListWidget()
        self.lst.setFont(font(13))
        self.lst.itemDoubleClicked.connect(lambda _: self.do_copy())
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border-radius: 12px;"
            " border: 1px solid %s; outline: none; padding: 4px; }"
            "QListWidget::item { color: %s; border-radius: 8px; padding: 9px 12px; }"
            "QListWidget::item:hover { background: %s; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, BADGE_BG, ACCENT_SOFT, ACCENT))
        v.addWidget(self.lst, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        b_copy = PrimaryPushButton("复制并粘贴", self)
        b_copy.setIcon(FIF.COPY.icon())
        b_del = PushButton("删除", self)
        b_del.setIcon(FIF.DELETE.icon())
        b_clear = PushButton("清空", self)
        b_close = PushButton("关闭", self)
        b_close.setIcon(FIF.CLOSE.icon())
        b_copy.clicked.connect(self.do_copy)
        b_del.clicked.connect(self.do_del)
        b_clear.clicked.connect(self.do_clear)
        b_close.clicked.connect(self.close)
        for b in (b_copy, b_del, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
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
        sub = CaptionLabel("JPG 体积小、质量可调;PNG 无损,适合截图。")
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        qrow = QHBoxLayout()
        qlab = BodyLabel("质量")
        qlab.setStyleSheet("color: %s;" % SEC)
        self.quality = Slider(Qt.Horizontal)
        self.quality.setRange(10, 95)
        self.quality.setValue(80)
        self.valueLabel = BodyLabel("80")
        self.valueLabel.setStyleSheet("color: %s;" % TEXT)
        self.quality.valueChanged.connect(self.valueLabel.setNum)
        qrow.addWidget(qlab)
        qrow.addWidget(self.quality, 1)
        qrow.addWidget(self.valueLabel)
        self.viewLayout.addLayout(qrow)

        self.radioJpg = RadioButton("JPG(体积小,质量可调)")
        self.radioPng = RadioButton("PNG(无损)")
        self.radioJpg.setChecked(True)
        self.radioJpg.toggled.connect(self._on_format)
        self.viewLayout.addWidget(self.radioJpg)
        self.viewLayout.addWidget(self.radioPng)

        self.saveButton = PushButton("💾 保存文件…", self)
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


class ScaleDialog(MessageBoxBase):
    """缩放对话框:滑条 + 数值框联动(替代原生 QInputDialog,跟随主题)。"""

    def __init__(self, editor):
        super().__init__(editor)
        self.titleLabel = StrongBodyLabel("缩放图片")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("按百分比缩放(10% – 300%)")
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        row = QHBoxLayout()
        self.slider = Slider(Qt.Horizontal)
        self.slider.setRange(10, 300)
        self.slider.setValue(100)
        self.spin = SpinBox()
        self.spin.setRange(10, 300)
        self.spin.setValue(100)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        self.viewLayout.addLayout(row)

        self.yesButton.setText("应用")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(360)

    def value(self):
        return self.spin.value()


class TextDialog(MessageBoxBase):
    """文字标注输入对话框(替代原生 QInputDialog,跟随主题)。"""

    def __init__(self, editor):
        super().__init__(editor)
        self.titleLabel = StrongBodyLabel("文字标注")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("输入文字后,在图片上点击放置。")
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)
        self.edit = LineEdit()
        self.edit.setPlaceholderText("标注文字…")
        self.edit.setClearButtonEnabled(True)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(360)

    def text(self):
        return self.edit.text().strip()


class ImageEditor(QWidget):
    def __init__(self, app, rec):
        super().__init__(None)
        self.app = app
        self.rec = rec
        self.setWindowTitle("图片处理 — " + os.path.basename(rec["content"]))
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowIcon(app.icon)
        self.resize(940, 640)
        self.setStyleSheet("QWidget { background: %s; }" % BG)   # 跟随主题

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
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(8)

        def _vline():
            ln = QFrame(self)
            ln.setFixedSize(1, 20)
            ln.setStyleSheet("background: %s;" % LINE)
            return ln

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._tbtns = {}
        for key, label in (("crop", "裁剪"), ("rotl", "⟲ 左旋"), ("rotr", "⟳ 右旋"),
                           ("scale", "缩放"), (None, None),
                           ("rect", "方框"), ("arrow", "箭头"),
                           ("text", "文字"), ("mosaic", "马赛克")):
            if key is None:
                bar.addSpacing(8)
                bar.addWidget(_vline())
                bar.addSpacing(8)
                continue
            # 工具类按钮用 ToggleButton(自带 Fluent 选中态),旋转/缩放为动作型用 PushButton
            b = ToggleButton(label, self) if key in ("crop", "rect", "arrow",
                                                     "text", "mosaic") \
                else PushButton(label, self)
            b.setCheckable(key in ("crop", "rect", "arrow", "text", "mosaic"))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._on_tool(k))
            bar.addWidget(b)
            self._tbtns[key] = b
        bar.addStretch(1)
        b_undo = PushButton("↶ 撤销", self)
        b_compress = PushButton("📦 压缩导出", self)
        b_save = PushButton("💾 保存", self)
        b_save.setIcon(FIF.SAVE.icon())
        b_copy = PrimaryPushButton("📋 复制", self)
        b_copy.setIcon(FIF.COPY.icon())
        b_undo.clicked.connect(self._on_undo)
        b_compress.clicked.connect(self._on_compress)
        b_copy.clicked.connect(self._on_copy)
        b_save.clicked.connect(self._on_save)
        for b in (b_undo, b_compress, b_save, b_copy):
            bar.addWidget(b)
        v.addLayout(bar)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setStyleSheet(
            "QGraphicsView { background: #26292F; border: none;"
            " border-radius: 10px; }")
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self._pix_item = QGraphicsPixmapItem()
        self.scene.addItem(self._pix_item)
        v.addWidget(self.view, 1)

        self.lbl_status = CaptionLabel("")
        self.lbl_status.setFont(font(11))
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
            d = ScaleDialog(self)
            if d.exec() and d.value() != 100:
                v = d.value()
                self._transform(lambda im: im.resize(
                    (max(1, int(im.width * v / 100)), max(1, int(im.height * v / 100))),
                    cm.RESAMPLE))
            return
        if key == "text":
            d = TextDialog(self)
            if d.exec() and d.text():
                self.tool = "text"
                self.pending_text = d.text()
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
# AI 工作流(多模型协作 · 人在回路)
# ============================================================
class AIWorker(QThread):
    """后台调用 AI(阻塞的 cm.ai_chat 放到线程里,不冻结 UI)。"""
    done = Signal(int, bool, str)     # step_idx, ok, text

    def __init__(self, service, prompt, step_idx=-1, parent=None):
        super().__init__(parent)
        self.service = service
        self.prompt = prompt
        self.step_idx = step_idx

    def run(self):
        ok, text = cm.ai_chat(self.service, self.prompt)
        self.done.emit(self.step_idx, ok, text)


class ServiceEditorDialog(MessageBoxBase):
    """AI 服务新增/编辑表单(OpenAI 兼容)。"""

    def __init__(self, parent, svc=None):
        super().__init__(parent)
        self.svc = dict(svc) if svc else {}
        self.result = None

        self.titleLabel = StrongBodyLabel("AI 服务")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("OpenAI 兼容接口,实际请求 base_url + /chat/completions。"
                           "智谱 GLM、DeepSeek、通义、Kimi、本地 Ollama 均适用。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        def row(label, value, placeholder="", password=False):
            h = QHBoxLayout()
            lab = BodyLabel(label)
            lab.setStyleSheet("color: %s;" % SEC)
            edit = LineEdit()
            edit.setText(value)
            edit.setPlaceholderText(placeholder)
            if password:
                edit.setEchoMode(QLineEdit.Password)
            h.addWidget(lab, 0)
            h.addWidget(edit, 1)
            self.viewLayout.addLayout(h)
            return edit

        self.e_name = row("名称", self.svc.get("name", ""), "如 GLM / DeepSeek / 本地")
        self.e_url = row("接口", self.svc.get("base_url", ""),
                         "https://open.bigmodel.cn/api/paas/v4")
        self.e_model = row("模型", self.svc.get("model", ""), "如 glm-4-flash")
        self.e_key = row("密钥", self.svc.get("api_key", ""), "api key", password=True)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(460)

    def validate(self):
        name = self.e_name.text().strip()
        base = self.e_url.text().strip()
        model = self.e_model.text().strip()
        if not name or not base or not model:
            InfoBar.error("无法保存", "名称 / 接口 / 模型 均不能为空", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return False
        self.result = {"name": name, "base_url": base, "model": model,
                       "api_key": self.e_key.text().strip()}
        return True


class ServicesWindow(QWidget):
    """AI 服务管理(常驻单例,由 WinClipboardApp 持有)。"""

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowTitle("AI 服务")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(580, 420)
        self._test_worker = None

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        ttl = StrongBodyLabel("AI 服务")
        ttl.setFont(font(16, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        v.addWidget(ttl)
        hint = CaptionLabel("工作流每一步可指定不同服务,实现多模型协作。密钥保存在本机 config.json。")
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.lst = QListWidget()
        self.lst.setFont(font(13))
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border-radius: 12px;"
            " border: 1px solid %s; outline: none; padding: 4px; }"
            "QListWidget::item { color: %s; border-radius: 8px; padding: 9px 12px; }"
            "QListWidget::item:hover { background: %s; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, BADGE_BG, ACCENT_SOFT, ACCENT))
        v.addWidget(self.lst, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_add = PushButton("新建", self)
        b_add.setIcon(FIF.ADD.icon())
        b_edit = PushButton("编辑", self)
        b_edit.setIcon(FIF.EDIT.icon())
        b_del = PushButton("删除", self)
        b_del.setIcon(FIF.DELETE.icon())
        self.b_test = PushButton("测试连接", self)
        self.b_test.setIcon(FIF.SEND.icon())
        b_close = PushButton("关闭", self)
        b_close.setIcon(FIF.CLOSE.icon())
        b_add.clicked.connect(lambda: self._edit(None))
        b_edit.clicked.connect(lambda: self._edit_current())
        b_del.clicked.connect(self._del_current)
        self.b_test.clicked.connect(self._test_current)
        b_close.clicked.connect(self.hide)
        for b in (b_add, b_edit, b_del, self.b_test):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)
        self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    def refresh(self):
        self.lst.clear()
        for s in self.app.cfg.get("ai_services") or []:
            self.lst.addItem("%s · %s\n%s" % (s.get("name"), s.get("model"), s.get("base_url")))

    def _current(self):
        item = self.lst.currentItem()
        if not item:
            return None
        i = self.lst.row(item)
        svcs = self.app.cfg.get("ai_services") or []
        return svcs[i] if 0 <= i < len(svcs) else None

    def _save(self):
        cm.save_config(self.app.cfg)
        self.refresh()
        if self.app.ai_hub is not None:
            self.app.ai_hub.refresh()

    def _edit(self, svc):
        dlg = ServiceEditorDialog(self, svc)
        if not dlg.exec():
            return
        r = dlg.result
        svcs = self.app.cfg.setdefault("ai_services", [])
        same = [x for x in svcs if x.get("name") == r["name"] and x is not svc]
        if same:
            InfoBar.error("名称重复", "已存在同名服务:%s" % r["name"], parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        if svc is not None:
            old_name = svc.get("name")
            svc.clear()
            svc.update(r)
            if old_name != r["name"]:   # 服务改名 → 同步工作流引用
                for wf in self.app.cfg.get("ai_workflows") or []:
                    for st in wf.get("steps") or []:
                        if st.get("service") == old_name:
                            st["service"] = r["name"]
        else:
            svcs.append(r)
        self._save()

    def _edit_current(self):
        s = self._current()
        if s is not None:
            self._edit(s)

    def _del_current(self):
        s = self._current()
        if s is None:
            return
        if not MessageBox("删除服务", "确定删除服务「%s」?" % s.get("name"), self).exec():
            return
        try:
            self.app.cfg.get("ai_services", []).remove(s)
        except ValueError:
            pass
        self._save()

    def _test_current(self):
        s = self._current()
        if s is None:
            return
        if self._test_worker is not None:
            return
        self.b_test.setEnabled(False)
        self.b_test.setText("测试中…")
        self._test_worker = AIWorker(s, "请只回复两个字母:OK", -1, self)

        def on_done(_idx, ok, text):
            self.b_test.setEnabled(True)
            self.b_test.setText("测试连接")
            self._test_worker = None
            if ok:
                InfoBar.success("连接成功", "模型回复:%s" % text[:60], parent=self,
                                position=InfoBarPosition.TOP_RIGHT)
            else:
                InfoBar.error("连接失败", text[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)

        self._test_worker.done.connect(on_done)
        self._test_worker.start()


class WorkflowEditorWindow(QWidget):
    """工作流新建/编辑:步骤 = 服务 + 提示词模板({input} 占位)。"""

    def __init__(self, app, wf=None, on_saved=None):
        super().__init__(None)
        self.app = app
        self.wf = wf if wf is not None else None
        self.on_saved = on_saved
        self.setWindowTitle("编辑工作流" if wf else "新建工作流")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(640, 580)
        self._rows = []

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        h = QHBoxLayout()
        lab = BodyLabel("名称")
        lab.setStyleSheet("color: %s;" % SEC)
        self.e_name = LineEdit()
        self.e_name.setPlaceholderText("如:翻译(G)→润色(D)")
        if wf:
            self.e_name.setText(wf.get("name", ""))
        h.addWidget(lab)
        h.addWidget(self.e_name, 1)
        v.addLayout(h)

        hint = CaptionLabel("每步可选三种类型:网页 AI(把提示词放进剪贴板,可选自动打开网页)、"
                            "人工确认(粘贴/编辑后继续)、本地 AI(需在「AI 服务」配置接口)。"
                            "提示词中 {input} = 输入内容 / 上一步输出。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self.steps_box = QVBoxLayout(content)
        self.steps_box.setContentsMargins(0, 0, 0, 0)
        self.steps_box.setSpacing(10)
        self.steps_box.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(content)
        v.addWidget(self.scroll, 1)

        b_add = PushButton("添加步骤", self)
        b_add.setIcon(FIF.ADD.icon())
        b_add.clicked.connect(lambda: self._add_row(None))
        v.addWidget(b_add, 0, Qt.AlignLeft)

        foot = QHBoxLayout()
        b_save = PrimaryPushButton("保存", self)
        b_save.setIcon(FIF.SAVE.icon())
        b_cancel = PushButton("取消", self)
        b_cancel.clicked.connect(self.close)
        b_save.clicked.connect(self._save)
        foot.addStretch(1)
        foot.addWidget(b_cancel)
        foot.addWidget(b_save)
        v.addLayout(foot)

        steps = (wf.get("steps") if wf else None) or [{}]
        for st in steps:
            self._add_row(st)

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    _KINDS = (("网页 AI", "web"), ("人工确认", "human"), ("本地 AI", "ai"))

    def _add_row(self, step):
        step = step or {}
        kind = step.get("kind", "ai")
        frame = QFrame()
        frame.setObjectName("stepCard")
        frame.setStyleSheet("QFrame#stepCard { background: %s; border-radius: 12px;"
                            " border: 1px solid %s; }" % (CARD_BG, CARD_BORDER))
        col = QVBoxLayout(frame)
        col.setContentsMargins(12, 10, 12, 10)
        col.setSpacing(8)

        top = QHBoxLayout()
        nlab = CaptionLabel("第 %d 步" % (len(self._rows) + 1))
        nlab.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        kind_combo = ComboBox()
        kind_combo.addItems([k[0] for k in self._KINDS])
        kind_combo.setCurrentIndex([i for i, k in enumerate(self._KINDS) if k[1] == kind][0])
        kind_combo.setMinimumWidth(100)

        names = [s.get("name", "?") for s in self.app.cfg.get("ai_services") or []]
        combo = ComboBox()
        combo.addItems(names)
        cur = step.get("service")
        if cur in names:
            combo.setCurrentText(cur)
        combo.setMinimumWidth(130)

        url_edit = LineEdit()
        url_edit.setText(str(step.get("url") or ""))
        url_edit.setPlaceholderText("https://chatgpt.com/?q={prompt}(留空=仅复制提示词)")

        b_del = TransparentToolButton(FIF.DELETE, frame)
        b_del.setFixedSize(28, 28)
        b_del.setToolTip("移除此步骤")
        top.addWidget(nlab)
        top.addSpacing(6)
        top.addWidget(kind_combo)
        top.addWidget(combo)
        top.addWidget(url_edit, 1)
        top.addWidget(b_del)
        col.addLayout(top)

        prompt = TextEdit()
        prompt.setPlaceholderText("提示词 / 给操作者的说明:{input} 为输入内容 / 上一步输出")
        prompt.setPlainText(step.get("prompt", ""))
        prompt.setFont(font(12))
        prompt.setFixedHeight(84)
        col.addWidget(prompt)

        row_ref = {"frame": frame, "kind": kind_combo, "combo": combo,
                   "url": url_edit, "prompt": prompt, "nlab": nlab}

        def sync_kind():
            k = self._KINDS[kind_combo.currentIndex()][1]
            combo.setVisible(k == "ai")
            url_edit.setVisible(k == "web")
            prompt.setPlaceholderText(
                "提示词,{input} 为输入内容 / 上一步输出" if k != "human"
                else "给操作者的说明(可选),显示在等待面板")
        kind_combo.currentIndexChanged.connect(sync_kind)
        sync_kind()
        b_del.clicked.connect(lambda _=False, r=row_ref: self._del_row(r))
        self._rows.append(row_ref)
        self.steps_box.addWidget(frame)
        self._renumber()

    def _del_row(self, r):
        if len(self._rows) <= 1:
            InfoBar.warning("至少一步", "工作流至少保留一个步骤", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        self._rows.remove(r)
        self.steps_box.removeWidget(r["frame"])
        r["frame"].deleteLater()
        self._renumber()

    def _renumber(self):
        for i, r in enumerate(self._rows):
            r["nlab"].setText("第 %d 步" % (i + 1))

    def _save(self):
        name = self.e_name.text().strip()
        if not name:
            InfoBar.error("无法保存", "请填写工作流名称", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        steps = []
        for r in self._rows:
            k = self._KINDS[r["kind"].currentIndex()][1]
            prompt = r["prompt"].toPlainText()
            if k == "web":
                steps.append({"kind": "web", "prompt": prompt,
                              "url": r["url"].text().strip()})
            elif k == "human":
                steps.append({"kind": "human", "prompt": prompt})
            else:
                svc = r["combo"].currentText().strip()
                if not svc:
                    InfoBar.error("无法保存", "本地 AI 步骤需要先在「AI 服务」中添加服务",
                                  parent=self, position=InfoBarPosition.TOP_RIGHT)
                    return
                steps.append({"kind": "ai", "service": svc, "prompt": prompt})
        wfs = self.app.cfg.setdefault("ai_workflows", [])
        if self.wf is not None:
            self.wf.clear()
            self.wf.update({"name": name, "steps": steps})
        else:
            if any(w.get("name") == name for w in wfs):
                InfoBar.error("名称重复", "已存在同名工作流:%s" % name, parent=self,
                              position=InfoBarPosition.TOP_RIGHT)
                return
            wfs.append({"name": name, "steps": steps})
        cm.save_config(self.app.cfg)
        if self.on_saved:
            self.on_saved()
        self.close()
        self.deleteLater()


class WorkflowsWindow(QWidget):
    """工作流管理(常驻单例):列表 + 新建/编辑/删除 + 填入示例 + 打开服务管理。"""

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowTitle("工作流")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(620, 470)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        ttl = StrongBodyLabel("工作流")
        ttl.setFont(font(16, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        v.addWidget(ttl)
        hint = CaptionLabel("在面板里右键文本条目 → 「工作流」即可运行。步骤可以是网页 AI(提示词进剪贴板,"
                            "你去网页粘贴)、人工确认(把关/编辑)或本地 API,自由组合、逐步推进。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.lst = QListWidget()
        self.lst.setFont(font(13))
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border-radius: 12px;"
            " border: 1px solid %s; outline: none; padding: 4px; }"
            "QListWidget::item { color: %s; border-radius: 8px; padding: 9px 12px; }"
            "QListWidget::item:hover { background: %s; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, BADGE_BG, ACCENT_SOFT, ACCENT))
        v.addWidget(self.lst, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_new = PushButton("新建", self)
        b_new.setIcon(FIF.ADD.icon())
        b_edit = PushButton("编辑", self)
        b_edit.setIcon(FIF.EDIT.icon())
        b_del = PushButton("删除", self)
        b_del.setIcon(FIF.DELETE.icon())
        b_seed = PushButton("填入示例模板", self)
        b_seed.setIcon(FIF.LIBRARY.icon())
        b_svc = PushButton("AI 服务…", self)
        b_svc.setIcon(FIF.CLOUD.icon())
        b_close = PushButton("关闭", self)
        b_close.setIcon(FIF.CLOSE.icon())
        b_new.clicked.connect(self._new)
        b_edit.clicked.connect(self._edit_current)
        b_del.clicked.connect(self._del_current)
        b_seed.clicked.connect(self._seed)
        b_svc.clicked.connect(self.app.show_services)
        b_close.clicked.connect(self.hide)
        for b in (b_new, b_edit, b_del, b_seed, b_svc):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)
        self._editors = []
        self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    def refresh(self):
        self.lst.clear()
        for wf in self.app.cfg.get("ai_workflows") or []:
            parts = []
            for st in wf.get("steps") or []:
                k = st.get("kind", "ai")
                parts.append("网页" if k == "web" else "人工" if k == "human"
                             else (st.get("service") or "AI"))
            self.lst.addItem("%s\n%d 步:%s" % (wf.get("name", "未命名"),
                                               len(wf.get("steps") or []),
                                               " → ".join(parts)))

    def _current(self):
        item = self.lst.currentItem()
        if not item:
            return None
        i = self.lst.row(item)
        wfs = self.app.cfg.get("ai_workflows") or []
        return wfs[i] if 0 <= i < len(wfs) else None

    def _new(self):
        ed = WorkflowEditorWindow(self.app, None, self.refresh)
        ed.show()
        ed.raise_()
        self._editors.append(ed)

    def _edit_current(self):
        wf = self._current()
        if wf is not None:
            ed = WorkflowEditorWindow(self.app, wf, self.refresh)
            ed.show()
            ed.raise_()
            self._editors.append(ed)

    def _del_current(self):
        wf = self._current()
        if wf is None:
            return
        if not MessageBox("删除工作流", "确定删除「%s」?" % wf.get("name"), self).exec():
            return
        try:
            self.app.cfg.get("ai_workflows", []).remove(wf)
        except ValueError:
            pass
        cm.save_config(self.app.cfg)
        self.refresh()

    def _seed(self):
        cfg = self.app.cfg
        svcs = cfg.setdefault("ai_services", [])
        have_svc = {s.get("name") for s in svcs}
        for s in cm.AI_SEED_SERVICES:
            if s["name"] not in have_svc:
                svcs.append(dict(s))
        wfs = cfg.setdefault("ai_workflows", [])
        have_wf = {w.get("name") for w in wfs}
        added = 0
        for wf in cm.AI_SEED_WORKFLOWS:
            if wf["name"] not in have_wf:
                wfs.append(json.loads(json.dumps(wf, ensure_ascii=False)))
                added += 1
        cm.save_config(cfg)
        self.refresh()
        InfoBar.success("示例已加入", "新增 %d 个工作流模板;请到「AI 服务」填入密钥后使用" % added,
                        parent=self, position=InfoBarPosition.TOP_RIGHT)


class WorkflowRunner(QWidget):
    """工作流运行台:步骤类型 web(网页 AI)/ human(人工确认)/ ai(本地 API)。

    web 步骤:渲染提示词 → 复制到剪贴板(可自动打开网页)→ 等待你把网页回答粘贴回来;
    human 步骤:直接进入等待面板,编辑/确认后作为下一步输入;
    ai 步骤:本地 API 自动调用。所有步骤的产出都可编辑后再继续(人在回路)。
    """

    def __init__(self, app, wf, content):
        super().__init__(None)
        self.app = app
        self.wf = wf
        self.steps = wf.get("steps") or []
        self.step_idx = 0
        self.cur = content                 # 链上当前内容(可能是人工编辑后的)
        self.results = []                  # 每步产出的文本
        self._worker = None
        self._waiting = None               # 等待输入面板(卡部件)
        self._wait_body = None
        self._wait_confirm = None
        self._dead = False
        self._auto = False

        self.setWindowTitle("工作流 — " + wf.get("name", ""))
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(600, 540)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        head = QHBoxLayout()
        icon = QLabel("🧩")
        icon.setFont(font(16))
        ttl = StrongBodyLabel(wf.get("name", "未命名"))
        ttl.setFont(font(15, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        self.chip = CaptionLabel("")
        self.chip.setStyleSheet(
            "QLabel { background: %s; color: %s; border-radius: 8px; padding: 3px 8px; }"
            % (ACCENT_SOFT, ACCENT))
        head.addWidget(icon)
        head.addSpacing(6)
        head.addWidget(ttl)
        head.addStretch(1)
        head.addWidget(self.chip)
        v.addLayout(head)

        hint = CaptionLabel("「{input}」= 输入内容/上一步输出;网页步骤会把提示词放进剪贴板,"
                            "网页回答粘贴回来即可;每步都可编辑后再继续。")
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        self.flow = QVBoxLayout(content_w)
        self.flow.setContentsMargins(2, 2, 6, 2)
        self.flow.setSpacing(10)
        self.flow.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(content_w)
        v.addWidget(self.scroll, 1)

        self.lbl_status = CaptionLabel("就绪")
        self.lbl_status.setStyleSheet("color: %s;" % SEC)
        v.addWidget(self.lbl_status)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.btn_next = PrimaryPushButton(self)
        self.btn_next.clicked.connect(self._on_next)
        self.btn_auto = PushButton("自动跑完", self)
        self.btn_auto.clicked.connect(self._on_auto)
        self.btn_copy = PushButton("复制最新结果", self)
        self.btn_copy.setIcon(FIF.COPY.icon())
        self.btn_copy.clicked.connect(self._copy_latest)
        b_close = PushButton("关闭", self)
        b_close.clicked.connect(self.close)
        foot.addWidget(self.btn_next)
        foot.addWidget(self.btn_auto)
        foot.addWidget(self.btn_copy)
        foot.addStretch(1)
        foot.addWidget(b_close)
        v.addLayout(foot)

        self._add_block("输入 · 原始内容", content, kind="input")
        self._update_ui()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    # ---- 步骤块 ----
    @staticmethod
    def _step_label(step):
        kind = step.get("kind", "ai")
        if kind == "web":
            return "网页"
        if kind == "human":
            return "人工"
        return step.get("service") or "AI"

    def _add_block(self, title, text, kind="result", idx=None):
        card = QFrame()
        card.setObjectName("aiCard")
        card.setStyleSheet("QFrame#aiCard { background: %s; border-radius: 12px;"
                           " border: 1px solid %s; }" % (CARD_BG, CARD_BORDER))
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 10, 14, 12)
        col.setSpacing(8)

        head = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background: %s; border-radius: 4px;"
                          % (ACCENT if kind != "info" else BADGE_BG))
        lab = CaptionLabel(title)
        lab.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        head.addWidget(dot)
        head.addSpacing(4)
        head.addWidget(lab)
        head.addStretch(1)
        col.addLayout(head)

        body = TextEdit()
        body.setReadOnly(True)
        body.setPlainText(text)
        body.setFont(font(12))
        body.setFixedHeight(max(56, 19 * min(text.count("\n") + 1, 12) + 14))
        col.addWidget(body)

        if kind in ("result", "info"):
            state = {"text": text}
            row = QHBoxLayout()
            row.setSpacing(6)
            b_copy = PushButton("复制", card)
            b_copy.setIcon(FIF.COPY.icon())
            b_copy.clicked.connect(lambda: self._copy_text(state["text"]))
            row.addWidget(b_copy)
            if kind == "result":
                b_edit = PushButton("编辑", card)
                b_edit.setIcon(FIF.EDIT.icon())
                b_save = PrimaryPushButton("保存修改", card)
                b_save.setVisible(False)
                row.addWidget(b_edit)
                row.addWidget(b_save)

                def do_edit():
                    body.setReadOnly(False)
                    body.setMinimumHeight(max(120, body.height()))
                    b_edit.setVisible(False)
                    b_save.setVisible(True)
                    body.setFocus()

                def do_save():
                    body.setReadOnly(True)
                    b_edit.setVisible(True)
                    b_save.setVisible(False)
                    state["text"] = body.toPlainText()
                    if idx is not None and 0 <= idx < len(self.results):
                        self.results[idx] = state["text"]
                        if idx == len(self.results) - 1:   # 只有最新结果能改写链上输入
                            self.cur = state["text"]
                            self._status("已保存修改,下一步将使用编辑后的内容")

                b_edit.clicked.connect(do_edit)
                b_save.clicked.connect(do_save)
            row.addStretch(1)
            col.addLayout(row)

        self.flow.addWidget(card)
        sb = self.scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def _show_wait_panel(self, idx, hint, prefill=""):
        """人工/网页步骤的等待面板:粘贴/编辑 → 确认后进入链。"""
        self._auto = False                  # 人工介入即停止自动串联
        card = QFrame()
        card.setObjectName("waitCard")
        card.setStyleSheet("QFrame#waitCard { background: %s; border-radius: 12px;"
                           " border: 1.5px dashed %s; }" % (CARD_BG, ACCENT))
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 10, 14, 12)
        col.setSpacing(8)

        lab = CaptionLabel("✍️ 步骤 %d · 等待输入" % (idx + 1))
        lab.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        col.addWidget(lab)
        hintlab = CaptionLabel(hint)
        hintlab.setWordWrap(True)
        hintlab.setStyleSheet("color: %s;" % TER)
        col.addWidget(hintlab)

        body = TextEdit()
        body.setPlainText(prefill)
        body.setFont(font(12))
        body.setFixedHeight(150)
        col.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(6)
        b_paste = PushButton("📋 从剪贴板粘贴", card)
        b_paste.setIcon(FIF.PASTE.icon())
        b_ok = PrimaryPushButton("✓ 确认,继续", card)
        row.addWidget(b_paste)
        row.addWidget(b_ok)
        row.addStretch(1)
        col.addLayout(row)

        def do_paste():
            body.setPlainText(QApplication.clipboard().text())

        def confirm():
            text = body.toPlainText().strip()
            if not text:
                InfoBar.warning("内容为空", "请粘贴或输入内容后再继续", parent=self,
                                position=InfoBarPosition.TOP_RIGHT)
                return
            body.setReadOnly(True)
            b_paste.setVisible(False)
            b_ok.setVisible(False)
            lab.setText("✓ 步骤 %d · 完成" % (idx + 1))
            card.setObjectName("aiCard")
            card.setStyleSheet("QFrame#aiCard { background: %s; border-radius: 12px;"
                               " border: 1px solid %s; }" % (CARD_BG, CARD_BORDER))
            self.results.append(text)
            self.cur = text
            self.step_idx += 1
            self._waiting = None
            self._wait_body = None
            self._wait_confirm = None
            if self.step_idx >= len(self.steps):
                self._set_busy(False, "全部 %d 步完成,结果可复制" % len(self.steps))
                InfoBar.success("工作流完成", "共 %d 步,最新结果已可复制" % len(self.steps),
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
            else:
                self._set_busy(False, "步骤 %d 完成;可继续下一步" % idx)

        b_paste.clicked.connect(do_paste)
        b_ok.clicked.connect(confirm)
        self.flow.addWidget(card)
        sb = self.scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))
        self._waiting = card
        self._wait_body = body
        self._wait_confirm = confirm
        self._set_busy(True, "等待人工输入(步骤 %d):网页端操作完成后回来粘贴/确认" % (idx + 1))
        body.setFocus()

    # ---- 状态 / 按钮 ----
    def _status(self, msg):
        self.lbl_status.setText(msg)

    def _update_ui(self):
        n, i = len(self.steps), self.step_idx
        done = i >= n
        self.chip.setText("已完成 %d 步" % n if done else "步骤 %d / %d" % (i + 1, n))
        if done:
            self.btn_next.setText("✔ 已完成")
            self.btn_next.setEnabled(False)
        else:
            self.btn_next.setText("▶ 执行第 %d 步 · %s"
                                  % (i + 1, self._step_label(self.steps[i])))
            self.btn_next.setEnabled(True)
        self.btn_auto.setEnabled(not done and self._worker is None and self._waiting is None)
        self.btn_copy.setEnabled(bool(self.results))

    def _set_busy(self, on, msg=None):
        self._status(msg or "")
        if on:
            if self._waiting is not None:
                self.btn_next.setText("等待输入…")
            elif self._auto:
                self.btn_next.setText("停止自动")
                self.btn_next.setEnabled(True)
            else:
                self.btn_next.setText("运行中…")
            self.btn_next.setEnabled(self._waiting is None)
        else:
            self._update_ui()
        self.btn_auto.setEnabled(not on and self.step_idx < len(self.steps)
                                 and self._waiting is None)
        self.btn_copy.setEnabled(bool(self.results))

    # ---- 运行 ----
    def _on_next(self):
        if self._worker is not None or self._waiting is not None:
            if self._worker is not None and self._auto:      # 充当「停止自动」
                self._auto = False
                self._set_busy(False, "已停止自动运行,当前步骤完成后不再继续")
            return
        self._run_step()

    def _on_auto(self):
        self._auto = True
        self._run_step()

    def _run_step(self):
        if self._worker is not None or self._waiting is not None \
                or self.step_idx >= len(self.steps):
            return
        step = self.steps[self.step_idx]
        kind = step.get("kind", "ai")
        idx = self.step_idx

        if kind == "web":
            prompt = cm.render_prompt(step.get("prompt"), self.cur)
            self.app.core.paste_text(prompt)                  # 提示词进剪贴板
            opened = ""
            url = str(step.get("url") or "").strip()
            if url:
                try:
                    import urllib.parse
                    import webbrowser
                    webbrowser.open(url.replace("{prompt}", urllib.parse.quote(prompt)))
                    opened = ",已打开网页"
                except Exception:
                    pass
            self._add_block("步骤 %d · 提示词(已复制%s)" % (idx + 1, opened),
                            prompt, "info")
            self._show_wait_panel(
                idx, "提示词已在剪贴板:切到网页 AI 粘贴发送,把回答复制后"
                     "点「📋 从剪贴板粘贴」(也可直接在此输入)。")

        elif kind == "human":
            self._show_wait_panel(
                idx, str(step.get("prompt") or "确认 / 编辑当前内容后继续:"), self.cur)

        else:                                                  # ai:本地 API
            svc = cm.ai_service_by_name(self.app.cfg, step.get("service"))
            if svc is None:
                InfoBar.error("无法运行", "找不到 AI 服务「%s」,请检查 AI 设置" % step.get("service"),
                              parent=self, position=InfoBarPosition.TOP_RIGHT, duration=-1)
                return
            prompt = cm.render_prompt(step.get("prompt"), self.cur)
            self._set_busy(True, "步骤 %d 正在调用 %s …" % (idx + 1, svc.get("name")))
            self._worker = AIWorker(svc, prompt, idx)
            self._worker.done.connect(self._on_done)
            self._worker.finished.connect(self._worker_finished)
            self._worker.start()

    def _on_done(self, idx, ok, text):
        if self._dead:
            return
        if not ok:
            self._auto = False
            self._set_busy(False, "步骤 %d 失败,可重试" % (idx + 1))
            InfoBar.error("调用失败", text[:220], parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=-1)
            return
        self.results.append(text)
        self.cur = text
        self.step_idx += 1
        self._add_block("步骤 %d · %s" % (idx + 1, self.steps[idx].get("service")),
                        text, "result", idx)
        if self.step_idx >= len(self.steps):
            self._set_busy(False, "全部 %d 步完成,结果可复制" % len(self.steps))
            InfoBar.success("工作流完成", "共 %d 步,最新结果已可复制" % len(self.steps),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        elif not self._auto:
            self._set_busy(False, "步骤 %d 完成;可编辑结果或直接继续" % idx)

    def _worker_finished(self):
        self._worker = None
        if self._dead:
            self.close()                       # 此时 worker 已结束,closeEvent 会真正关闭
        elif self._auto and self.step_idx < len(self.steps):
            self._run_step()                   # 自动模式:链式推进
        else:
            self._update_ui()

    # ---- 复制 / 关闭 ----
    def _copy_text(self, text):
        self.app.core.paste_text(text)

    def _copy_latest(self):
        if self.results:
            self._copy_text(self.results[-1])
            InfoBar.success("已复制", "结果已放入剪贴板(同时记录进历史)",
                            parent=self, position=InfoBarPosition.TOP_RIGHT)

    def closeEvent(self, e):
        if self._worker is not None and self._worker.isRunning():
            self._dead = True
            self.hide()                        # 等线程收尾,避免 QThread 析构崩溃
            e.ignore()
        else:
            try:
                self.app._ai_runners.remove(self)
            except ValueError:
                pass
            self.deleteLater()
            e.accept()


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

    def _on_history_change(self):
        """核心投递的 "change" 信号(GUI 线程):剪贴捕获入史后,
        面板可见时合并刷新(防抖),避免连续捕获反复重建列表。"""
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
        self.core = cm.ClipboardCore()

        self.panel = PanelWindow(self)
        self.private_win = None
        self.ai_hub = None
        self.ai_services_win = None
        self._ai_runners = []

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
        if rec["type"] == "text":
            wfs = self.cfg.get("ai_workflows") or []
            if wfs:
                sub = RoundMenu("工作流", self.panel)
                for wf in wfs:
                    a = sub.addAction(wf.get("name", "未命名"))
                    a.triggered.connect(lambda _=False, w=wf: self.run_workflow(w, rec))
                sub.addSeparator()
                am = sub.addAction("管理工作流…")
                am.triggered.connect(self.show_ai_hub)
                menu.addMenu(sub)
            else:
                aai = menu.addAction("工作流(先配置)…")
                aai.triggered.connect(self.show_ai_hub)
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

    # ---- AI 工作流 ----
    def show_ai_hub(self):
        try:
            if self.ai_hub is None:
                self.ai_hub = WorkflowsWindow(self)
            self.ai_hub.refresh()
            self.ai_hub.show()
            self.ai_hub.raise_()
            self.ai_hub.activateWindow()
        except Exception:
            cm.log("打开 AI 工作流窗口失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "AI 窗口打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()

    def show_services(self):
        try:
            if self.ai_services_win is None:
                self.ai_services_win = ServicesWindow(self)
            self.ai_services_win.refresh()
            self.ai_services_win.show()
            self.ai_services_win.raise_()
            self.ai_services_win.activateWindow()
        except Exception:
            cm.log("打开 AI 服务窗口失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "AI 服务窗口打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()

    def run_workflow(self, wf, rec):
        runner = WorkflowRunner(self, wf, rec["content"])
        runner.show()
        runner.raise_()
        runner.activateWindow()
        self._ai_runners.append(runner)

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
        for r in self._ai_runners:          # 等 AI 线程收尾,避免 QThread 运行中析构
            try:
                r._dead = True
                if r._worker is not None and r._worker.isRunning():
                    r._worker.wait(3000)
            except Exception:
                pass
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
        elif kind == "change":
            self._on_history_change()
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
    dark = resolve_theme(cm.load_config())
    init_palette(dark)
    setTheme(Theme.DARK if dark else Theme.LIGHT)
    setThemeColor(ACCENT)

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
