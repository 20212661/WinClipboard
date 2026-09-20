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
import time
import queue
import subprocess
import threading
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm   # 后端复用
import perf_probe
from perf_probe import PerfSpan, since, stamp   # 关键路径性能埋点
from pinboard import PinBoardManager   # 图钉画布(置顶多图板)子包
from urllib.parse import unquote as _unquote, quote as _quote, urlsplit

from PySide6.QtCore import (Qt, QTimer, QRectF, QRect, QSize, QEvent, QPropertyAnimation,
                            QEasingCurve, QObject, QPoint, QThread, Signal,
                            QPointF)
from PySide6.QtGui import (QGuiApplication, QPixmap, QImage, QPainter, QColor, QFont,
                           QIcon, QPainterPath, QFontMetrics, QPen, QCursor,
                           QTextCursor, QTextCharFormat, QAction, QTransform,
                           QPolygonF, QShortcut, QKeySequence, QTextDocument)
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout,
                               QHBoxLayout, QFrame, QGraphicsView,
                               QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsEllipseItem, QGraphicsPathItem,
                               QGraphicsLineItem, QGraphicsTextItem, QStyle,
                               QFileDialog, QSizePolicy, QListWidget, QLineEdit,
                               QPlainTextEdit, QPushButton, QSizeGrip,
                               QSpacerItem, QStackedWidget, QTreeWidgetItem,
                               QTableWidgetItem, QHeaderView)

from qfluentwidgets import (FluentIcon as FIF, setTheme, setThemeColor, Theme,
                            themeColor, isDarkTheme, FluentWindow, NavigationItemPosition,
                            CardWidget, SimpleCardWidget, IconWidget,
                            SearchLineEdit, SegmentedWidget, ScrollArea, ToolButton,
                            ToggleToolButton, PrimaryToolButton,
                            TransparentToolButton, PrimaryPushButton, PushButton,
                            ToggleButton, Slider, RadioButton, SpinBox, LineEdit,
                            TextEdit, ComboBox, EditableComboBox,
                            RoundMenu, MessageBox, MessageBoxBase, InfoBar,
                            InfoBarPosition, BodyLabel, StrongBodyLabel,
                            CaptionLabel, TitleLabel,
                            SmoothScrollDelegate, ScrollBarHandleDisplayMode,
                            TreeWidget, TableWidget,
                            SwitchSettingCard, PushSettingCard, OptionsSettingCard,
                            SettingCardGroup, SettingCard)
from PySide6.QtWidgets import QGraphicsOpacityEffect

# ============================================================
# 主题与调色板(浅色 / 深色)
# ============================================================
# 注意:init_palette / setTheme 需在 QApplication 创建后、
# 构建 UI 之前调用(见 main)。深浅色跟随 Windows 系统设置,可在
# config.json 用 "theme": "dark" / "light" / "auto"(默认)覆盖。

PANEL_W = 520
PANEL_H_MAX = 640
MARGIN = 26          # 布局留白
RADIUS = 16          # 圆角

# 底部预览面板的大文本处理上限:预览区可视高度只有 ~5 行,超长文本截断展示,
# 语法识别 / JSON 美化 / 高亮全部基于截断段;全文查看走独立查看器窗口。
# (pygments 对 20KB 文本的全量染色实测 ~340ms,过去选中大条目会在 UI 线程卡住)
_PV_TEXT_CAP = 4000

# ============================================================
# 主题:全部派生自 qfluentwidgets 当前主题 —— 强调色来自 themeColor()
# (可在设置页换色),中性色与库全局样式表一致。不再自调配色。
# ============================================================
IS_DARK = False
ACCENT = "#0078D4"
ACCENT_SOFT = "rgba(0,120,212,0.14)"
TEXT = "rgba(0,0,0,0.90)"
SEC = "rgba(0,0,0,0.63)"
TER = "rgba(0,0,0,0.45)"
LINE = "rgba(0,0,0,0.08)"
BG = "#f9f9f9"
CARD_BG = "#ffffff"
CARD_BORDER = "rgba(0,0,0,0.06)"
FIELD_BG = "#ffffff"
FIELD_BORDER = "rgba(0,0,0,0.09)"
BADGE_BG = "rgba(0,0,0,0.05)"
NAV_BG = "rgba(0,0,0,0.05)"
THUMB_PH = "rgba(0,0,0,0.06)"
THUMB_BORDER = "rgba(0,0,0,0.06)"
GUTTER_BG = "#fafafa"       # 查看器行号栏(比内容卡片深半档)
PANEL_TOP = CARD_BG
PANEL_BOT = CARD_BG


def init_palette(dark):
    """按 qfluentwidgets 当前主题刷新派生色(常量名保留,供既有布局引用)。"""
    g = globals()
    g["IS_DARK"] = bool(dark)
    acc = themeColor()
    accent = acc.name()
    accent_soft = "rgba(%d,%d,%d,0.14)" % (acc.red(), acc.green(), acc.blue())
    neutral = {
        False: {   # 浅色,与 qfluentwidgets 浅色样式表一致
            "TEXT": "rgba(0,0,0,0.90)", "SEC": "rgba(0,0,0,0.63)",
            "TER": "rgba(0,0,0,0.45)", "LINE": "rgba(0,0,0,0.08)",
            "BG": "#f3f3f3", "CARD_BG": "#ffffff",
            "CARD_BORDER": "rgba(0,0,0,0.06)",
            "FIELD_BG": "#ffffff", "FIELD_BORDER": "rgba(0,0,0,0.09)",
            "BADGE_BG": "rgba(0,0,0,0.05)", "NAV_BG": "rgba(0,0,0,0.05)",
            "THUMB_PH": "rgba(0,0,0,0.06)", "THUMB_BORDER": "rgba(0,0,0,0.06)",
            "GUTTER_BG": "#fafafa",
        },
        True: {    # 深色
            "TEXT": "rgba(255,255,255,0.93)", "SEC": "rgba(255,255,255,0.65)",
            "TER": "rgba(255,255,255,0.44)", "LINE": "rgba(255,255,255,0.08)",
            "BG": "#1e1e1e", "CARD_BG": "#2b2b2b",
            "CARD_BORDER": "rgba(255,255,255,0.08)",
            "FIELD_BG": "#333333", "FIELD_BORDER": "rgba(255,255,255,0.10)",
            "BADGE_BG": "rgba(255,255,255,0.08)", "NAV_BG": "rgba(255,255,255,0.06)",
            "THUMB_PH": "rgba(255,255,255,0.08)", "THUMB_BORDER": "rgba(255,255,255,0.08)",
            "GUTTER_BG": "#303030",
        },
    }[bool(dark)]
    g.update(neutral)
    g["ACCENT"] = accent
    g["ACCENT_SOFT"] = accent_soft
    g["PANEL_TOP"] = g["CARD_BG"]
    g["PANEL_BOT"] = g["CARD_BG"]


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


def _mica_supported():
    """Mica 是否可用:仅真实桌面会话(本地登录)且非离屏平台。
    RDP / offscreen / 第三方远程工具(网易UU远程等,不置 RDP 标志)下
    DWM 合成异常——实测 UU 远程会话中 Mica 让 Qt 事件循环假死(窗口在、
    定时器与消息全不跑),故检测到远程进程直接跳过。"""
    try:
        import ctypes
        if os.environ.get("WCLB_NO_MICA"):
            return False
        if QGuiApplication.platformName() == "offscreen":
            return False
        if ctypes.windll.user32.GetSystemMetrics(0x1000):   # SM_REMOTESESSION
            return False
        # 已知第三方远程工具进程名(命中即视为远程会话)
        remote_hints = ("uuremote", "uuremote_service", "网易uu远程",
                        "todesk", "sunloginclient", "rustdesk")
        try:
            # tasklist 输出 GBK:按字节捕获再容错解码(text=True 会随
            # PYTHONUTF8 用 utf-8 解,在读取线程里炸 UnicodeDecodeError,
            # 外层 except 拦不住,每次真实桌面启动都污染日志)
            raw = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=3).stdout
            out = raw.decode("gbk", errors="replace").lower()
            if any(h in out for h in remote_hints):
                cm.log("检测到第三方远程会话,本进程跳过 Mica 材质")
                return False
        except Exception:
            pass
        return True
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


def icon_tile(icon, size=34, radius=10):
    """圆角图标瓦片(BADGE_BG 底 + 主题色 Fluent 图标,gallery 风格)。"""
    box = QWidget()
    box.setFixedSize(size, size)
    box.setStyleSheet("background: %s; border-radius: %dpx;" % (BADGE_BG, radius))
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    ic = IconWidget(box)
    ic.setIcon(icon)
    isz = 16 if size < 40 else 20
    ic.setFixedSize(isz, isz)
    lay.addWidget(ic, 0, Qt.AlignCenter)
    return box


def _accent_tint(alpha):
    """主题色淡染(选中态背景)。"""
    c = QColor(ACCENT)
    c.setAlpha(alpha)
    return c


class Card(CardWidget):
    """剪贴板条目卡片。

    背景/hover/按压/描边全部交给库原生 CardWidget 绘制(圆角 6、顶部细描边);
    选中态通过 _normalBackgroundColor 通道给主题色淡染,不自绘样式表。
    """

    THUMB = (92, 68)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.rec = None                 # 当前绑定记录(差量绑定核心,见 bind)
        self._bound_id = None           # 同 rec 直接返回的守卫
        self._bound_kind = None         # 徽章区结构类型(image/files/link/text)
        self._thumb_lbl = None
        self._pin_shown = None
        self.setProperty("selected", False)
        self.setBorderRadius(6)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build()

    def _normalBackgroundColor(self):
        if self.property("selected"):
            return _accent_tint(46 if isDarkTheme() else 26)
        return super()._normalBackgroundColor()

    def _hoverBackgroundColor(self):
        if self.property("selected"):
            return _accent_tint(60 if isDarkTheme() else 42)
        return super()._hoverBackgroundColor()

    def set_selected(self, on):
        if self.property("selected") != on:
            self.setProperty("selected", on)
            self._updateBackgroundColor()

    def update_pin(self):
        """复用卡片时刷新置顶指示点(状态未变则跳过,setStyleSheet 需重新解析 QSS)。"""
        pinned = bool(self.rec.get("pinned"))
        if pinned == getattr(self, "_pin_shown", None):
            return
        self._pin_shown = pinned
        self.dot.setStyleSheet(
            "background: %s; border-radius: 4px;" % (ACCENT if pinned else "transparent"))

    def set_thumb(self, pm):
        """后台缩略图生成完成后回填(卡片先用占位色块占位)。"""
        if self._thumb_lbl is not None:
            self._thumb_lbl.setPixmap(rounded_pixmap(pm, 10))

    def _build(self):
        """只建与内容无关的常驻骨架;内容填充一律走 bind 的差量路径。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(14)

        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet("background: transparent; border-radius: 4px;")

        self._left = QVBoxLayout()          # 徽章区:结构随类型变(见 _rebuild_left)
        self._left.setAlignment(Qt.AlignTop)

        self.title_lbl = StrongBodyLabel()
        self.title_lbl.setFont(font(14, QFont.DemiBold))
        self.title_lbl.setMaximumWidth(640)
        self.sub_lbl = BodyLabel()
        self.sub_lbl.setFont(font(13))
        self.sub_lbl.setMaximumWidth(640)
        self.sub_lbl.hide()
        self.meta_lbl = CaptionLabel()
        self.meta_lbl.setFont(font(11))

        mid = QVBoxLayout()
        mid.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(self.dot)
        title_row.addWidget(self.title_lbl)
        title_row.addStretch(1)
        mid.addLayout(title_row)
        mid.addWidget(self.sub_lbl)
        mid.addWidget(self.meta_lbl)

        root.addLayout(self._left)
        root.addLayout(mid, 1)

        # 悬停操作按钮:首次悬停才创建(库按钮初始化各需解析一次 QSS,
        # 实测 4 个按钮 ~5.7ms,是百张卡片首建数秒级卡顿的最大单项);
        # 平时只留固定宽度占位容器,显隐不触发行布局重排
        self.act_btns = None
        actw = QWidget()
        actw.setFixedSize(4 * 30 + 3 * 6, 30)
        self.actw = actw
        root.addWidget(actw, 0)
        root.setAlignment(actw, Qt.AlignVCenter)

    # ---- 差量绑定(固定复用池的核心) ----
    def bind(self, rec):
        """绑定一条记录:同 rec 零开销;徽章区结构只在类型变化时重建,
        文本字段带变化检测地 setText。卡片控件只建不毁,刷新成本≈改文本。"""
        if self._bound_id == id(rec):
            return
        kind = "link" if (rec["type"] == "text" and cm.url_info(rec["content"])) \
            else rec["type"]
        if kind != self._bound_kind:
            self._rebuild_left(kind)
        self.rec = rec
        self._bound_id = id(rec)
        self._bound_kind = kind
        self._fill(rec, kind)
        self.set_selected(False)

    def _rebuild_left(self, kind):
        """按类型重建左侧徽章区(唯一会发生控件增删的位置,仅类型切换时)。"""
        while self._left.count():
            w = self._left.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._thumb_lbl = None
        if kind == "image":
            thumb = QLabel()
            thumb.setFixedSize(*self.THUMB)
            thumb.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % THUMB_BORDER)
            self._thumb_lbl = thumb
            self._left.addWidget(thumb)
        elif kind == "files":
            self._left.addWidget(icon_tile(FIF.FOLDER, 44, 13))
        elif kind == "link":
            self._left.addWidget(icon_tile(FIF.LINK, 34, 10))
        else:
            self._left.addWidget(icon_tile(FIF.DOCUMENT, 34, 10))

    def _fill(self, rec, kind):
        """填充文本字段与缩略图(全部走带守卫的 setText)。"""
        if kind == "image":
            pm = self.app.thumb_cached(rec["content"])
            if pm is None:
                pm = QPixmap(*self.THUMB)
                pm.fill(QColor(THUMB_PH))
                self.app.request_thumb(rec["content"])
            self._thumb_lbl.setPixmap(rounded_pixmap(pm, 10))
            title = "截图"
            sub = "%s × %s" % (rec.get("w", 0), rec.get("h", 0))
            meta = self._meta("PNG")
        elif kind == "files":
            names = rec["content"]
            name = os.path.basename(names[0])
            ext = os.path.splitext(name)[1].lstrip(".").upper()[:4] or "FILE"
            title = name + (" 等 %d 个" % len(names) if len(names) > 1 else "")
            sub = ""
            size = ""
            try:
                total = sum(os.path.getsize(p) for p in names if os.path.exists(p))
                size = cm._fmt_size(total)
            except Exception:
                pass
            meta = " · ".join(x for x in (ext, size, cm._rel_time(rec.get("ts", 0))) if x)
        elif kind == "link":
            title = cm.url_info(rec["content"])[0]
            sub = rec["content"].strip()
            meta = "链接 · " + cm._rel_time(rec.get("ts", 0))
        else:
            lines = [l for l in rec["content"].strip().splitlines() if l.strip()]
            title = lines[0] if lines else ""
            sub = "\n".join(lines[1:3]) if len(lines) > 1 else ""
            meta = "文本 · " + cm._rel_time(rec.get("ts", 0))
        self._set_text(self.title_lbl, elided(title, self.title_lbl.font(), 640))
        self._set_text(self.sub_lbl, elided(sub, self.sub_lbl.font(), 640) if sub else "")
        self.sub_lbl.setVisible(bool(sub))
        self._set_text(self.meta_lbl, meta)
        self._pin_shown = None          # 重绑后置顶态必查一次
        self.update_pin()

    @staticmethod
    def _set_text(lbl, txt):
        if lbl.text() != txt:           # setText 触发重排/重绘,不变则跳过
            lbl.setText(txt)

    def _ensure_btns(self):
        if self.act_btns is not None:
            return
        self.act_btns = []
        actbox = QHBoxLayout(self.actw)
        actbox.setSpacing(6)
        for act, icon_name, tip in (
                ("paste", "COPY", "粘贴"), ("pin", "PIN", "固定"),
                ("del", "DELETE", "删除"), ("more", "MORE", "更多")):
            icon, txt = _fic(icon_name, {"paste": "贴", "pin": "钉",
                                         "del": "删", "more": "⋯"}[act])
            if icon is not None:
                b = TransparentToolButton(icon, self)
                b.setIconSize(QSize(15, 15))
            else:
                b = PushButton(txt, self)
                b.setFont(font(10))
            b.setFixedSize(30, 30)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=act: self.app.card_action(a, self))
            b.setVisible(False)
            actbox.addWidget(b)
            self.act_btns.append(b)

    def _meta(self, kind):
        return " · ".join(x for x in (kind, cm._rel_time(self.rec.get("ts", 0))) if x)

    def enterEvent(self, e):
        self._ensure_btns()
        for b in self.act_btns:
            b.setVisible(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self.act_btns is not None:
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
            # 双击 = 直达查看:链接→默认浏览器;文件→默认程序(同资源管理器双击);
            # 图片→图片查看器;文本/代码→内容查看器;粘贴走 Enter / 悬停按钮 / 右键
            t = self.rec["type"]
            if t == "image":
                self.app.open_image_viewer(self.rec)
            elif t == "files":
                self.app.open_files(self.rec)
            elif t == "text" and cm.url_info(self.rec["content"]):
                self.app.open_link(self.rec)
            else:
                self.app.open_viewer(self.rec)
        super().mouseDoubleClickEvent(e)


# ============================================================
# 主面板(无边框透明窗口 + Fluent 组件)
# ============================================================
class HistoryPage(QWidget):
    """剪贴板历史页(FluentWindow 导航页):搜索 + 分类 + 卡片流 + 底部预览。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("historyPage")
        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(12)

        # ---- 头部 ----
        hdr = QHBoxLayout()
        icon_lbl = QLabel()
        try:
            icon_lbl.setPixmap(self.app.icon.pixmap(22, 22))
        except Exception:
            pass
        title = TitleLabel("剪贴板")
        chip = CaptionLabel("Ctrl + Shift + V 显示/隐藏")
        chip.setStyleSheet(
            "QLabel { background: %s; color: %s; border-radius: 8px;"
            " padding: 3px 8px; }" % (ACCENT_SOFT, ACCENT))
        hdr.addWidget(icon_lbl)
        hdr.addSpacing(10)
        hdr.addWidget(title)
        hdr.addStretch(1)
        hdr.addWidget(chip)
        v.addLayout(hdr)

        # ---- 搜索 + 分类(同一行;库默认外观) ----
        tools = QHBoxLayout()
        tools.setSpacing(12)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("搜索内容、文件名或图片尺寸")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.app.on_search)
        self.search.returnPressed.connect(self.app.paste_selected)
        self.search.installEventFilter(self)
        self.nav = SegmentedWidget(self)
        for label, cat in (("全部", "all"), ("文本", "text"), ("图片", "image"),
                           ("链接", "link"), ("文件", "files")):
            self.nav.addItem(routeKey=cat, text=label)
        # 注意:addItem 的 onClick 收到的是 itemClicked(bool) 的布尔值,不能用于回传分类;
        # 改用 currentItemChanged(str),它携带 routeKey 字符串。
        self.nav.currentItemChanged.connect(self.app.set_category)
        self.nav.setCurrentItem("all")
        tools.addWidget(self.search, 1)
        tools.addWidget(self.nav, 0, Qt.AlignVCenter)
        v.addLayout(tools)

        # ---- 卡片流 ----
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setViewportMargins(0, 0, 6, 0)
        # 内容层与滚动区必须显式透明,否则深色主题下缝隙处露出默认白色底;
        # viewport 也要关掉自动填充 —— 否则它按调色板填白,卡片会糊进白底
        self.scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.content)
        self.cards_layout.setContentsMargins(2, 4, 2, 4)
        self.cards_layout.setSpacing(10)
        self.cards_layout.setAlignment(Qt.AlignTop)
        # 固定复用卡片池:卡片只建不毁,闲置进 _idle 池 hide;两个分组
        # 标签与尾部弹簧常驻,刷新零创建零销毁(见 set_view)
        self._idle = []
        self._cards = []
        self._lab_pinned = CaptionLabel("📌 已固定")
        self._lab_recent = CaptionLabel("最近项目")
        for _lab in (self._lab_pinned, self._lab_recent):
            _lab.setStyleSheet("color: %s; font-size: 12px; font-weight: 600;" % SEC)
            _lab.setContentsMargins(6, 10, 0, 4)
            _lab.hide()
        self._tail_spacer = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        self.scroll.setWidget(self.content)
        v.addWidget(self.scroll, 1)

        # 空状态提示(复用单个部件,避免每次刷新重建)
        self._empty_box = QWidget()
        eb = QVBoxLayout(self._empty_box)
        eb.setContentsMargins(0, 32, 0, 36)
        eb.setSpacing(8)
        halo = QWidget()
        halo.setFixedSize(64, 64)
        halo.setStyleSheet("background: %s; border-radius: 32px;" % BADGE_BG)
        _ic = IconWidget(halo)
        _ic.setIcon(FIF.PASTE)
        _ic.setFixedSize(26, 26)
        _ic.move((64 - 26) // 2, (64 - 26) // 2)
        t1 = StrongBodyLabel("暂无剪贴内容")
        t1.setAlignment(Qt.AlignCenter)
        t1.setFont(font(15, QFont.DemiBold))
        t2 = CaptionLabel("复制文字、截图或文件后,按 Ctrl + Shift + V 打开")
        t2.setAlignment(Qt.AlignCenter)
        self.empty_title = t1
        self.empty_hint = t2
        self.btn_reset_filter = PushButton("显示全部内容", self._empty_box)
        self.btn_reset_filter.clicked.connect(self.reset_filters)
        eb.addWidget(halo, 0, Qt.AlignHCenter)
        eb.addWidget(t1)
        eb.addWidget(t2)
        eb.addWidget(self.btn_reset_filter, 0, Qt.AlignHCenter)
        self._empty_box.setVisible(False)

        # ---- 预览面板(Ditto 式:选中即显示完整内容,常驻面板底部) ----
        self._pv_compact = False
        self._pv_lang = "text"
        self._pv_display = ""
        self._lang_cache = {}          # id(rec) → detect_lang 结果(避免选中切换重复猜)
        self._pv_img_cache = {}        # path → 预览尺寸 QPixmap(避免反复全图解码)
        self.preview = SimpleCardWidget()
        self.preview.setBorderRadius(8)
        pv = QVBoxLayout(self.preview)
        pv.setContentsMargins(10, 6, 10, 8)
        pv.setSpacing(6)
        ph = QHBoxLayout()
        ph.setSpacing(6)
        self.pv_badge = CaptionLabel("预览")
        self.pv_badge.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        self.pv_stats = CaptionLabel("")
        self.pv_stats.setStyleSheet("color: %s;" % TER)
        self.pv_btn_json = PushButton("紧凑")
        self.pv_btn_json.setFixedHeight(24)
        self.pv_btn_json.setFont(font(10))
        self.pv_btn_json.setToolTip("JSON 美化 / 紧凑切换")
        self.pv_btn_json.clicked.connect(self._toggle_pv_json)
        self.pv_btn_json.setVisible(False)
        self.pv_btn_copy = TransparentToolButton(FIF.COPY, self.preview)
        self.pv_btn_copy.setFixedSize(24, 24)
        self.pv_btn_copy.setIconSize(QSize(13, 13))
        self.pv_btn_copy.setToolTip("复制完整内容")
        self.pv_btn_copy.clicked.connect(self._copy_pv)
        ph.addWidget(self.pv_badge)
        ph.addSpacing(4)
        ph.addWidget(self.pv_stats)
        ph.addStretch(1)
        ph.addWidget(self.pv_btn_json)
        ph.addWidget(self.pv_btn_copy)
        pv.addLayout(ph)

        self.pv_edit = QPlainTextEdit()
        self.pv_edit.setReadOnly(True)
        self.pv_edit.setUndoRedoEnabled(False)
        self.pv_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        pf = QFont("Consolas")
        pf.setStyleHint(QFont.Monospace)
        pf.setPointSize(9)
        self.pv_edit.setFont(pf)
        self.pv_edit.setFixedHeight(128)
        self.pv_edit.setStyleSheet(
            "QPlainTextEdit { background: %s; border: 1px solid %s;"
            " border-radius: 8px; padding: 6px; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT))
        pv.addWidget(self.pv_edit)

        self.pv_img = QLabel()
        self.pv_img.setAlignment(Qt.AlignCenter)
        self.pv_img.setFixedHeight(128)
        self.pv_img.setStyleSheet("border-radius: 8px;")
        self.pv_img.setVisible(False)
        pv.addWidget(self.pv_img)
        v.addWidget(self.preview)

        # ---- 底栏 ----
        foot = QHBoxLayout()
        foot.setContentsMargins(4, 6, 2, 2)
        self.lbl_count = CaptionLabel("0 个项目")
        self.lbl_count.setStyleSheet("color: %s;" % TER)
        foot.addWidget(self.lbl_count)
        foot.addStretch(1)
        self.btn_preview = PushButton("预览")
        self.btn_preview.setCheckable(True)
        preview_on = bool(getattr(app, "cfg", {}).get("preview_visible", True))
        self.btn_preview.setChecked(preview_on)
        self.preview.setVisible(preview_on)
        self.btn_preview.setFixedHeight(22)
        self.btn_preview.setFont(font(10))
        self.btn_preview.setToolTip("显示 / 隐藏底部预览面板")
        self.btn_preview.clicked.connect(lambda checked: self.toggle_preview(checked))
        foot.addWidget(self.btn_preview)
        foot.addSpacing(6)
        for btxt in ("↑ ↓", "Enter", "Esc"):
            foot.addWidget(Badge(btxt))
        v.addLayout(foot)

        self._cards = []

    # ---- 事件 ----
    def reset_filters(self):
        self.search.clear()
        self.nav.setCurrentItem("all")
        self.app.set_category("all")
        self.search.setFocus()

    def eventFilter(self, obj, e):
        if obj is self.search:
            if e.type() == QEvent.KeyPress:
                k = e.key()
                if k in (Qt.Key_Down, Qt.Key_Up):
                    self.app.move_sel(1 if k == Qt.Key_Down else -1)
                    return True
                if k == Qt.Key_Escape:
                    self.app.win.hide()
                    return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.app.win.hide()
        elif e.key() == Qt.Key_Down:
            self.app.move_sel(1)
        elif e.key() == Qt.Key_Up:
            self.app.move_sel(-1)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.app.paste_selected()
        else:
            super().keyPressEvent(e)

    def _resize_to_content(self):
        """兼容保留:页面高度由 FluentWindow 管理,无需自适应。"""

    # ---- 列表(卡片池复用:搜索/分类切换零重建) ----
    def set_view(self, view, history):
        """刷新 = 既有控件的重新排布 + 差量绑定,零销毁重建。

        - 卡片按 rec 身份配对回原控件(bind 幂等,同 rec 零开销),
          配不上的从 _idle 池取,池空才新建(只增不减,上限≈条数上限);
        - 分组标签/尾部弹簧常驻复用,空组 hide;
        - 视图序列与分组完全未变时连布局都不碰(重复 show/无效刷新零成本)。
        """
        recs = [history[i] for i in view]
        pinned = [r for r in recs if r.get("pinned")]
        rest = [r for r in recs if not r.get("pinned")]
        seq = pinned + rest
        ids = [id(r) for r in seq]

        if ([id(c.rec) for c in self._cards] == ids
                and self._lab_pinned.isVisible() == bool(pinned)
                and self._lab_recent.isVisible() == bool(rest)
                and not self._empty_box.isVisible()):
            return

        cur = {id(c.rec): c for c in self._cards}
        idle = self._idle
        # 闲置卡也记得上一次绑的 rec:按身份捞回可连 bind 都跳过
        # (搜索过滤→清空的往返因此退化为纯重排)
        idle_by_rec = {}
        for c in idle:
            if c.rec is not None:
                idle_by_rec.setdefault(id(c.rec), c)
        cards = []
        for r in seq:
            c = cur.pop(id(r), None) or idle_by_rec.pop(id(r), None)
            if c is not None:
                try:
                    idle.remove(c)
                except ValueError:
                    pass
            else:                       # 无身份匹配:先吃任意闲置,池空才新建
                c = idle.pop() if idle else Card(self.app)
            c.bind(r)
            cards.append(c)
        for c in cur.values():          # 本次视图外的 → 闲置池
            idle.append(c)
        # 置顶态只会随分组移动而变(bind 的同 rec 守卫跳过了字段刷新)
        for c, r in zip(cards, seq):
            grp = 1 if r.get("pinned") else 0
            if getattr(c, "_grp", None) != grp:
                c._grp = grp
                c.update_pin()
        self._cards = cards
        self.app._card_widgets = cards
        # 闲置池收回的卡带着显式 hide 标志,重新 addWidget 不会自动显形
        # (只有「链接/文件」这类全空分类会把卡片全部赶进闲置池,丢这行
        # 切回「全部」就是隐形列表:数据在、控件在、一条看不见)
        for c in cards:
            if c.isHidden():
                c.show()

        # 重排布局:全部是既有控件,只挪位置(无创建无 polish 无销毁)
        while self.cards_layout.count():
            self.cards_layout.takeAt(0)
        self._empty_box.setVisible(False)
        if pinned:
            self._lab_pinned.show()
            self.cards_layout.addWidget(self._lab_pinned)
        else:
            self._lab_pinned.hide()
        for c in cards[:len(pinned)]:
            self.cards_layout.addWidget(c)
        if rest:
            self._lab_recent.show()
            self.cards_layout.addWidget(self._lab_recent)
        else:
            self._lab_recent.hide()
        for c in cards[len(pinned):]:
            self.cards_layout.addWidget(c)
        if not seq:
            filtered = bool(history)
            self.empty_title.setText("没有找到匹配内容" if filtered else "暂无剪贴内容")
            self.empty_hint.setText("试试其他关键词,或清除搜索与分类筛选" if filtered else
                                    "复制文字、截图或文件后,按 Ctrl + Shift + V 打开")
            self.btn_reset_filter.setVisible(filtered)
            self._empty_box.setVisible(True)
            self.cards_layout.addWidget(self._empty_box)
        else:
            self.cards_layout.addItem(self._tail_spacer)
        for c in idle:
            c.hide()

    def select_index(self, vi):
        for i, c in enumerate(self.app._card_widgets):
            c.set_selected(i == vi)
        self.update_preview(vi)

    # ---- 预览面板(Ditto 式:随选中联动显示完整内容) ----
    def update_preview(self, vi=None):
        if not self.preview.isVisible():
            return
        app = self.app
        if vi is None:
            vi = app.selected
        rec = None
        if vi is not None and app._view and 0 <= vi < len(app._view) \
                and app._view[vi] < len(app.history):
            rec = app.history[app._view[vi]]
        if rec is None:
            self.pv_btn_copy.setEnabled(False)
            self.pv_badge.setText("预览")
            self.pv_stats.setText("")
            self.pv_edit.setPlainText("")
            self.pv_img.setVisible(False)
            self.pv_edit.setVisible(True)
            self.pv_btn_json.setVisible(False)
            self._pv_display = ""
            return
        self.pv_btn_copy.setEnabled(True)
        t = rec["type"]
        if t == "image":
            self.pv_edit.setVisible(False)
            self.pv_img.setVisible(True)
            self.pv_btn_json.setVisible(False)
            path = rec.get("content") or ""
            self.pv_img.clear()
            pm = self._pv_img_cache.get(path)
            if pm is None:
                img = QImage(path)
                if not img.isNull():
                    w = min(PANEL_W - 40, img.width())
                    pm = QPixmap.fromImage(img.scaled(
                        w, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    if len(self._pv_img_cache) > 32:   # FIFO,防止缓存无限增长
                        self._pv_img_cache.pop(next(iter(self._pv_img_cache)))
                    self._pv_img_cache[path] = pm
            if pm is not None:
                self.pv_img.setPixmap(pm)
            self.pv_badge.setText("截图 · 放大预览")
            self.pv_stats.setText("%s × %s · 双击可全屏查看" %
                                  (rec.get("w", 0), rec.get("h", 0)))
            self._pv_display = ""
            return
        raw = "\n".join(rec.get("content") or []) if t == "files" \
            else (rec.get("content") or "")
        # 超长文本:截断预览 + 全部重活(猜语言/JSON 美化/染色)只做截断段
        truncated = len(raw) > _PV_TEXT_CAP
        lang = "text"
        if t == "text":
            lang = self._lang_cache.get(id(rec))
            if lang is None:
                lang = cm.detect_lang(raw[:_PV_TEXT_CAP])
                if len(self._lang_cache) > 512:
                    self._lang_cache.clear()
                self._lang_cache[id(rec)] = lang
        if lang == "json" and not truncated:
            self._pv_display = cm.json_reformat(raw, self._pv_compact) or raw
        elif truncated:
            self._pv_display = raw[:_PV_TEXT_CAP] + \
                "\n…(共 %d 字符,已截断;右键 → 在独立窗口查看全文)" % len(raw)
        else:
            self._pv_display = raw
        self.pv_img.setVisible(False)
        self.pv_edit.setVisible(True)
        self.pv_edit.setPlainText(self._pv_display)
        apply_pygments_format(self.pv_edit.document(), self._pv_display, lang)
        self._pv_lang = lang
        kind = {"text": dict(cm.VIEW_LANGS).get(lang, "文本"),
                "files": "文件"}[t]
        self.pv_badge.setText("%s · %s" % (kind, cm._rel_time(rec.get("ts", 0))))
        if truncated:
            self.pv_stats.setText("已截断 · 全文 %s" %
                                  cm._fmt_size(len(raw.encode("utf-8"))))
        else:
            self.pv_stats.setText("%d 行 · %s" % (
                self._pv_display.count("\n") + 1,
                cm._fmt_size(len(self._pv_display.encode("utf-8")))))
        self.pv_btn_json.setVisible(lang == "json" and not truncated)

    def _toggle_pv_json(self):
        self._pv_compact = not self._pv_compact
        self.pv_btn_json.setText("紧凑" if not self._pv_compact else "美化")
        self.update_preview()

    def _copy_pv(self):
        app = self.app
        vi = app.selected
        if vi is None or not 0 <= vi < len(app._view):
            return
        rec = app.history[app._view[vi]]
        if not app.core.copy_record(rec):
            InfoBar.error("复制失败", "内容不可用或剪贴板正被占用,请重试", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        InfoBar.success("已复制", "完整原始内容已复制,可切换到目标窗口粘贴", parent=self,
                        position=InfoBarPosition.TOP_RIGHT)

    def toggle_preview(self, on):
        self.btn_preview.setChecked(on)
        self.app.cfg["preview_visible"] = bool(on)
        cm.save_config(self.app.cfg)
        self.preview.setVisible(on)
        if on:
            self.update_preview()
        self._resize_to_content()

    def ensure_visible(self, vi):
        if 0 <= vi < len(self.app._card_widgets):
            self.scroll.ensureWidgetVisible(self.app._card_widgets[vi], 0, 20)


# ============================================================
# 私密区窗口
# ============================================================
# ============================================================
# 主窗口(FluentWindow:库标准导航壳,所有功能收进单一窗口)
# ============================================================
class MainWindow(FluentWindow):
    """单一主窗口:左侧导航 = 剪贴板 / 工作流 / AI 服务 / 私密区 / 设置。

    热键或托盘切换显示;点窗口关闭按钮 = 隐藏(程序常驻托盘)。
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        # Mica 材质只在真实桌面会话可用(Win11 + 本地登录,库对旧系统自动退化);
        # 远程桌面/离屏渲染下 DWM 不合成 Mica,窗口会整体透明(实测过的坑),
        # 那时回退到库 API 的纯色背景(浅 231 / 深 24,配合 StackedWidget
        # 白@50% 叠色的画布对比,半透明卡片才有层次)。
        if _mica_supported() and bool(app.cfg.get("mica", True)):
            self.setMicaEffectEnabled(True)
            app._mica_active = True     # 供启动看门狗(见 WinClipboardApp)
        else:
            self.setMicaEffectEnabled(False)
            self.setCustomBackgroundColor(QColor(231, 231, 231), QColor(24, 24, 24))
        self.setWindowTitle("WinClipboard")
        self.setWindowIcon(app.icon)
        # 提前创建原生窗口句柄:主窗常驻隐藏,但二次启动的唤起路径靠
        # FindWindow("WinClipboard") 找它——没有原生句柄就找不到,会退化成
        # 「已在运行中」弹窗(此前的句柄是保活块里的 winId() 顺手创建的,
        # 保活按远程会话门控后本地就没了,必须在此无条件创建)
        try:
            int(self.winId())
        except Exception:
            pass
        # 主线程派发器保活:远程会话等环境下 Qt 派发器会"无消息即休眠",
        # 后台事件的队列唤醒(排空任务投递)随之停摆;实测任何到达主窗口的
        # 投递消息都能立刻唤醒(WM_TIMER 无效——低优先级合成消息唤不醒 MsgWait)。
        # compat.remote_session_keepalive: auto=检测到远程会话才开(默认,
        # 本地正常环境不背 500ms 空转唤醒) / on=无条件开 / off=关闭
        # (不置 RDP 标志的远控工具假死时,可手动设 on;派发器假死另有
        # UiHealthMonitor 看门狗兜底)。
        try:
            _ka_mode = str((app.cfg.get("compat") or {}).get(
                "remote_session_keepalive", "auto")).lower()
        except Exception:
            _ka_mode = "auto"
        if _ka_mode == "on" or (_ka_mode != "off" and cm.is_remote_session()):
            try:
                import ctypes
                _hwnd = int(self.winId())

                def _keepalive():
                    import time as _t
                    _u = ctypes.windll.user32
                    while True:
                        _u.PostMessageW(_hwnd, 0x0000, 0, 0)   # WM_NULL
                        _t.sleep(0.5)
                threading.Thread(target=_keepalive, daemon=True).start()
            except Exception:
                pass
        self.history_page = HistoryPage(app)
        self.workflow_page = WorkflowPage(app)
        self.services_page = ServicesPage(app)
        self.private_page = PrivatePage(app)
        self.settings_page = SettingsPage(app)
        self.addSubInterface(self.history_page, FIF.HISTORY, "剪贴板")
        self.addSubInterface(self.workflow_page, FIF.ROBOT, "工作流")
        self.addSubInterface(self.services_page, FIF.CLOUD, "AI 服务")
        self.addSubInterface(self.private_page, FIF.IOT, "私密区")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置",
                             NavigationItemPosition.BOTTOM)
        self.resize(980, 660)

    def toggle(self):
        """热键/托盘:显示并激活,或隐藏(最小化过则还原)。"""
        if self.isVisible() and self.isActiveWindow():
            self.app._hotkey_ts = None      # 收起路径不参与首帧统计
            self.hide()
        else:
            self.showWindow()

    def show_fast(self):
        """只做可见性三件套,零重活——热键路径允许的全部同步成本。
        不碰列表/预览/缩略图:先让事件循环把现有卡片画出来。"""
        with PerfSpan("window_reshow"):
            self.show()
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def showWindow(self):
        """热键/托盘显示:先 show_fast 画出现有卡片(启动预热已备好),
        隐藏期间攒下的脏刷新让到下一拍(0ms 定时器 = 让出事件循环,
        首帧绘制先走),绝不挡窗口出现。"""
        self.show_fast()
        self.history_page.search.setFocus()
        if getattr(self.app, "_view_dirty", True):
            QTimer.singleShot(0, self.app.refresh_view)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:     # Esc 在任何页面都能收起窗口
            self.hide()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.hide()          # 常驻托盘:关闭按钮只隐藏
        e.ignore()


class PrivatePage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("privatePage")
        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)

        ttl = TitleLabel("私密区")
        v.addWidget(ttl)

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
        b_copy.clicked.connect(self.do_copy)
        b_del.clicked.connect(self.do_del)
        b_clear.clicked.connect(self.do_clear)
        for b in (b_copy, b_del, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.refresh()


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
        if not 0 <= i < len(self.app.private):
            return
        self.app.core.paste_text(self.app.private[i])

    def do_del(self):
        item = self.lst.currentItem()
        if item:
            i = self.lst.row(item)
            if not 0 <= i < len(self.app.private):
                return
            self.app.private.pop(i)
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

# ============================================================
# 设置页(全部使用 qfluentwidgets SettingCard 组件)
# ============================================================
class SettingsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("settingsPage")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 24, 36, 16)
        lay.setSpacing(8)
        ttl = TitleLabel("设置")
        lay.addWidget(ttl)
        cfg = app.cfg

        group = SettingCardGroup("剪贴板", self)
        c_paste = SwitchSettingCard(
            FIF.PASTE, "自动粘贴", "选中条目回车后自动粘贴到当前窗口(部分程序可能不生效)",
            parent=group)
        c_paste.setChecked(bool(cfg.get("auto_paste", True)))

        def on_paste(on):
            cfg["auto_paste"] = bool(on)
            cm.save_config(cfg)
        c_paste.checkedChanged.connect(on_paste)

        c_guard = SwitchSettingCard(
            FIF.IOT, "敏感内容隔离", "密码/银行卡/身份证/密钥自动进私密区,不进入普通历史",
            parent=group)
        c_guard.setChecked(bool(cfg.get("sensitive_guard", True)))

        def on_guard(on):
            cfg["sensitive_guard"] = bool(on)
            cm.save_config(cfg)
        c_guard.checkedChanged.connect(on_guard)

        c_max = SettingCard(FIF.TAG, "历史条数上限",
                            "超出后从最旧开始淘汰(置顶条目不计入)", parent=group)
        spin = SpinBox()
        spin.setRange(20, 1000)
        spin.setValue(int(cfg.get("max_items", 100)))
        spin.setMinimumWidth(120)

        def on_max(v):
            cfg["max_items"] = int(v)
            cm.save_config(cfg)
            app.core.max_items = int(v)
        spin.valueChanged.connect(on_max)
        c_max.hBoxLayout.addWidget(spin, 0, Qt.AlignRight)
        c_max.hBoxLayout.addSpacing(16)

        group.addSettingCard(c_paste)
        group.addSettingCard(c_guard)
        group.addSettingCard(c_max)
        lay.addWidget(group)

        group2 = SettingCardGroup("外观与系统", self)
        c_theme = SettingCard(FIF.BRUSH, "界面主题",
                              "跟随系统或固定浅色/深色(更改后立即生效)", parent=group2)
        combo = ComboBox()
        combo.addItems(["auto", "light", "dark"])
        combo.setCurrentText(str(cfg.get("theme", "auto")))
        combo.setMinimumWidth(120)

        def on_theme(t):
            cfg["theme"] = t
            cm.save_config(cfg)
            setTheme(Theme.DARK if t == "dark" else
                     Theme.LIGHT if t == "light" else Theme.AUTO)
            init_palette(isDarkTheme())
            cm.log("主题切换为 %s(部分页面元素需重开窗口刷新)" % t)
        combo.currentTextChanged.connect(on_theme)
        c_theme.hBoxLayout.addWidget(combo, 0, Qt.AlignRight)
        c_theme.hBoxLayout.addSpacing(16)

        # 主题色:色板即时应用(选中态/徽章/强调色全部派生自此)
        c_accent = SettingCard(FIF.PALETTE, "主题色",
                               "强调色与选中态跟随;点击色块立即生效,部分元素需重开窗口刷新",
                               parent=group2)
        sw_box = QWidget()
        sw_lay = QHBoxLayout(sw_box)
        sw_lay.setContentsMargins(0, 0, 0, 0)
        sw_lay.setSpacing(6)
        _ACCENTS = ("#0078D4", "#00B7C3", "#107C10", "#CA5010",
                    "#8764B8", "#E3008C", "#C42B1C")
        _cur = str(cfg.get("theme_color", "") or "").upper()
        _sw_btns = []

        def _restyle_swatches():
            for b, hx in zip(_sw_btns, _ACCENTS):
                sel = hx.upper() == str(cfg.get("theme_color", "") or "").upper()
                b.setStyleSheet(
                    "QPushButton { background: %s; border: 2px solid %s;"
                    " border-radius: 10px; }"
                    % (hx, TEXT if sel else "transparent"))

        def _on_accent(hx):
            try:
                setThemeColor(QColor(hx))
            except Exception:
                return
            cfg["theme_color"] = hx
            cm.save_config(cfg)
            init_palette(isDarkTheme())
            _restyle_swatches()
            InfoBar.success("主题色已应用", "部分页面元素需重开窗口刷新", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)

        for _hx in _ACCENTS:
            _b = QPushButton()
            _b.setFixedSize(20, 20)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setToolTip(_hx)
            _b.clicked.connect(lambda _=False, h=_hx: _on_accent(h))
            sw_lay.addWidget(_b)
            _sw_btns.append(_b)
        _restyle_swatches()
        c_accent.hBoxLayout.addWidget(sw_box, 0, Qt.AlignRight)
        c_accent.hBoxLayout.addSpacing(16)

        c_mica = SwitchSettingCard(
            FIF.BRUSH, "Mica 材质", "Win11 云母背景(本地桌面);远程会话自动禁用;更改需重开窗口",
            parent=group2)
        c_mica.setChecked(bool(cfg.get("mica", True)))

        def on_mica(on):
            cfg["mica"] = bool(on)
            cm.save_config(cfg)
        c_mica.checkedChanged.connect(on_mica)

        c_auto = SwitchSettingCard(
            FIF.UP, "开机自启", "登录 Windows 后自动启动 WinClipboard", parent=group2)
        c_auto.setChecked(app.core.is_autostart())

        def on_auto(on):
            if on != app.core.is_autostart():
                app.core.toggle_autostart()
            c_auto.setChecked(app.core.is_autostart())
        c_auto.checkedChanged.connect(on_auto)

        c_key = PushSettingCard(
            "查看说明", FIF.CUT, "全局热键",
            "Ctrl + Shift + V 显示/隐藏主窗口(被占用时可修改)", parent=group2)

        def show_key_help():
            MessageBox(
                "修改全局热键",
                "编辑配置文件中的热键位掩码后重启:\n"
                "  hotkey_mod: Ctrl=2, Shift=4, Alt=1, Win=8(6 = Ctrl+Shift)\n"
                "  hotkey_key: 主键虚拟键码(86 = V)\n\n配置文件:\n%s" % cm.CONFIG_FILE,
                self).exec()
        c_key.clicked.connect(show_key_help)

        group2.addSettingCard(c_theme)
        group2.addSettingCard(c_accent)
        group2.addSettingCard(c_mica)
        group2.addSettingCard(c_auto)
        group2.addSettingCard(c_key)
        lay.addWidget(group2)
        lay.addStretch(1)


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
        final = self.editor._final_image()   # 含全部矢量标注的合成图
        if self.radioJpg.isChecked():
            final.save(buf, "JPEG", quality=self.quality.value())
        else:
            final.save(buf, "PNG")
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
            self.editor._emit_result(img)   # 画布回写走压缩后的版本
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
            Path(path).write_bytes(self._encode())
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


def _glyph_icon(kind, color=None, sz=18):
    """编辑器工具的几何图形图标(矩形/圆/箭头/画笔/马赛克/撤销/对勾/粗细)。
    库里没有这些语义图形,按工具本身的几何含义绘制;部件样式仍全部走库。"""
    pm = QPixmap(sz, sz)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = color if color is not None else QColor(TEXT)
    pen = QPen(c, 1.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    m = 2.5
    if kind == "rect":
        p.drawRect(QRectF(m, m + 1, sz - 2 * m, sz - 2 * m - 2))
    elif kind == "ellipse":
        p.drawEllipse(QRectF(m, m + 1, sz - 2 * m, sz - 2 * m - 2))
    elif kind == "arrow":
        pen.setWidthF(1.8)
        p.drawLine(QPointF(m + 1, sz - m - 1), QPointF(sz - m - 1, m + 1))
        p.drawLine(QPointF(sz - m - 1, m + 1),
                   QPointF(sz - m - 6, m + 2.5))
        p.drawLine(QPointF(sz - m - 1, m + 1),
                   QPointF(sz - m - 6.5, m + 7))
    elif kind == "pen":
        path = QPainterPath(QPointF(m, sz * 0.72))
        path.cubicTo(QPointF(sz * 0.3, sz * 0.32), QPointF(sz * 0.55, sz * 0.88),
                     QPointF(sz - m, sz * 0.28))
        pen.setWidthF(1.9)
        p.drawPath(path)
    elif kind == "mosaic":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cell = (sz - 2 * m) / 3.0
        for rr in range(3):
            for cc in range(3):
                p.drawRect(QRectF(m + cc * cell + 0.8, m + rr * cell + 0.8,
                                  cell - 1.6, cell - 1.6))
    elif kind == "undo":
        path = QPainterPath(QPointF(sz * 0.78, sz * 0.60))
        path.cubicTo(QPointF(sz * 0.74, sz * 0.18), QPointF(sz * 0.28, sz * 0.18),
                     QPointF(sz * 0.26, sz * 0.52))
        pen.setWidthF(1.8)
        p.drawPath(path)
        ex, ey = sz * 0.26, sz * 0.52
        p.drawLine(QPointF(ex, ey), QPointF(ex + 5.5, ey - 3))
        p.drawLine(QPointF(ex, ey), QPointF(ex + 5.5, ey + 5.5))
    elif kind == "select":
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([
            QPointF(m + 3, m), QPointF(m + 3, sz * 0.78),
            QPointF(m + 7, sz * 0.58), QPointF(m + 9.2, sz - m),
            QPointF(m + 11.2, sz - m - 0.8), QPointF(m + 9, sz * 0.52),
            QPointF(m + 13.5, sz * 0.5)]))
    elif kind == "check":
        pen.setWidthF(2.2)
        p.drawLine(QPointF(sz * 0.24, sz * 0.55), QPointF(sz * 0.44, sz * 0.74))
        p.drawLine(QPointF(sz * 0.44, sz * 0.74), QPointF(sz * 0.78, sz * 0.28))
    elif kind == "width":
        for i, w in ((0, 1.2), (1, 2.2), (2, 3.4)):
            pen.setWidthF(w)
            y = m + 2 + i * ((sz - 2 * m - 4) / 2.0)
            p.drawLine(QPointF(m + 1, y), QPointF(sz - m - 1, y))
    p.end()
    return QIcon(pm)


def _qimage_to_pil(qimg):
    """QImage(ARGB32) → PIL RGB(白底合成);编辑器场景渲染导出用。"""
    w, h = qimg.width(), qimg.height()
    rgba = cm.Image.frombuffer("RGBA", (w, h), bytes(qimg.constBits()),
                               "raw", "BGRA", qimg.bytesPerLine(), 1)
    out = cm.Image.new("RGB", (w, h), (255, 255, 255))
    out.paste(rgba, mask=rgba.split()[3])
    return out


# ---- 标注项(矢量:可选中/可拖动/可删除;文字双击再编辑) ----
_SEL_PEN = None          # 选中角标画笔(固定样式,模块级缓存,别在 paint 里反复构造)


def _sel_border(item, painter, option):
    """选中态虚线角标(所有标注项共用)。"""
    global _SEL_PEN
    if not (option.state & QStyle.State_Selected):
        return
    if _SEL_PEN is None:
        _SEL_PEN = QPen(QColor("#00A2FF"), 1)
        _SEL_PEN.setCosmetic(True)
        _SEL_PEN.setStyle(Qt.DashLine)
    painter.setPen(_SEL_PEN)
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(item.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5))


class _AnnoMixin:
    """标注通用行为:editor 反向引用 + 拖动起止记录(撤销 move 用)。"""

    def _anno_init(self, editor):
        self.editor = editor
        self.setFlag(QGraphicsPixmapItem.ItemIsMovable, True)
        self.setFlag(QGraphicsPixmapItem.ItemIsSelectable, True)
        self._move_from = None

    def mousePressEvent(self, e):
        self._move_from = self.pos()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._move_from is not None and self._move_from != self.pos():
            ed = getattr(self, "editor", None)
            if ed is not None:
                ed._record({"type": "move", "item": self, "pos": self._move_from})
        self._move_from = None


class AnnoRect(_AnnoMixin, QGraphicsRectItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoEllipse(_AnnoMixin, QGraphicsEllipseItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoPath(_AnnoMixin, QGraphicsPathItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoMosaic(_AnnoMixin, QGraphicsPixmapItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoText(_AnnoMixin, QGraphicsTextItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)

    def mouseDoubleClickEvent(self, e):
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus()
        super().mouseDoubleClickEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        if not self.toPlainText().strip():
            ed = getattr(self, "editor", None)
            if ed is not None:
                ed._drop_anno(self, record=False)
                ed._prune_add_record(self)


class ImageEditor(QWidget):
    """图片编辑器(QQ 风格矢量标注)。两种形态:
    - 独立窗口(parent=None,兼容旧入口/测试)
    - 内嵌组件(parent=查看器等容器,不新开窗口——查看↔编辑同窗切换)"""

    finished = Signal()            # 内嵌形态:编辑结束(完成/Esc),容器收回

    def __init__(self, app, rec, on_result=None, parent=None):
        super().__init__(parent)
        self.app = app
        self.rec = rec
        self._on_result = on_result     # 编辑产出回调(图钉画布回写用);None=普通编辑
        self._embedded = parent is not None
        if not self._embedded:
            self.setWindowTitle("图片处理%s — %s" % (
                "(完成复制/保存后自动更新画布)" if on_result else "",
                os.path.basename(rec["content"])))
            self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
            self.setWindowIcon(app.icon)
            self.resize(940, 640)
        self.setStyleSheet("QWidget { background: %s; }" % BG)   # 跟随主题(仅本部件子树)

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

        self.undo_stack = []           # 撤销记录(add/del/move/img)
        self.tool = None
        self._drag_start = None
        self._preview = None          # 拖动中的形状预览项(场景内)
        self._preview_path = None     # 画笔预览路径
        self._pen_pts = None          # 画笔采样点(视图坐标)
        self.annos = []               # 标注矢量项(创建序即 z 序)
        self._z = 0
        self._color = QColor("#FF4040")     # 标注颜色(默认红,QQ 风格)
        self._pen_w = 4                      # 线宽:细2 / 中4 / 粗8
        self._font_px = 26                   # 标注文字像素高

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setStyleSheet(
            "QGraphicsView { background: #26292F; border: none; }")
        self.view.setDragMode(QGraphicsView.RubberBandDrag)   # 选择模式=框选
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self._pix_item = QGraphicsPixmapItem()
        self.scene.addItem(self._pix_item)
        v.addWidget(self.view, 1)

        # 状态角标(左上角小字):必须透明背景+鼠标穿透——CaptionLabel 脱离
        # 布局后 parent 到窗口会带出 640×480 的默认尺寸,一旦有背景色/收
        # 事件就是一张盖住画布的白卡(真机已踩)
        self.lbl_status = CaptionLabel("")
        self.lbl_status.setParent(self)
        self.lbl_status.setFont(font(11))
        self.lbl_status.setStyleSheet("color: %s; background: transparent;"
                                      % SEC)
        self.lbl_status.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.lbl_status.adjustSize()

        self._build_toolbar()          # 底部悬浮工具条(QQ 截图编辑风格)

        # 覆写视图鼠标事件做标注绘制;无工具(选择模式)时转发回默认处理器,
        # 让场景项走 Qt 原生的选中/拖动/文字编辑
        self._ovp = self.view.mousePressEvent
        self._ovm = self.view.mouseMoveEvent
        self._ovr = self.view.mouseReleaseEvent
        self.view.mousePressEvent = self._press
        self.view.mouseMoveEvent = self._move
        self.view.mouseReleaseEvent = self._release
        self._redraw()

    # ---- 工具条(底部居中悬浮药丸) ----
    COLORS = ("#000000", "#FFFFFF", "#FF4040", "#FF9500",
              "#FFD60A", "#34C759", "#007AFF", "#AF52DE")
    COLOR_NAMES = {"#000000": "黑", "#FFFFFF": "白", "#FF4040": "红",
                   "#FF9500": "橙", "#FFD60A": "黄", "#34C759": "绿",
                   "#007AFF": "蓝", "#AF52DE": "紫"}
    PEN_WIDTHS = ((2, "细"), (4, "中"), (8, "粗"))

    def _build_toolbar(self):
        bar = CardWidget(self)
        self._bar = bar
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 7, 10, 6)
        lay.setSpacing(0)
        self._tbtns = {}

        def tbtn(icon, text, tip, checkable=True, cb=None, primary=False):
            # 竖排组合:图标按钮 + 下方小字(库原生 ToggleToolButton 有明确
            # 选中态;QToolButton 的 textUnderIcon 样式与库 QSS 不合,图标
            # 文字会叠压,实测不可用)
            wrap = QWidget(bar)
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(0)
            b = (PrimaryToolButton if primary else
                 ToggleToolButton if checkable else TransparentToolButton)(wrap)
            b.setIcon(icon)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setFixedSize(44, 38)
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.PointingHandCursor)
            if cb is not None:
                b.clicked.connect(cb)
            lb = CaptionLabel(text, wrap)
            lb.setFont(font(10))
            lb.setAlignment(Qt.AlignCenter)
            wl.addWidget(b, 0, Qt.AlignHCenter)
            wl.addWidget(lb, 0, Qt.AlignHCenter)
            lay.addWidget(wrap)
            return b

        def vsep():
            ln = QFrame(bar)
            ln.setFixedSize(1, 44)
            ln.setStyleSheet("background: %s;" % LINE)
            lay.addWidget(ln)
            lay.addSpacing(4)

        tbtn(_glyph_icon("undo"), "撤销", "撤销上一步(Ctrl+Z)", False,
             self._on_undo)
        lay.addSpacing(2)
        tools = (("select", _glyph_icon("select"), "选择", "点击选中标注后拖动移动,"
                 "Del 删除;双击文字再编辑;空白拖动框选多个"),
                 ("rect", _glyph_icon("rect"), "矩形", "拖动绘制矩形(可连续标注)"),
                 ("ellipse", _glyph_icon("ellipse"), "圆形", "拖动绘制椭圆"),
                 ("arrow", _glyph_icon("arrow"), "箭头", "拖动绘制箭头"),
                 ("pen", _glyph_icon("pen"), "画笔", "按住拖动自由绘制"),
                 ("text", FIF.FONT.icon(), "文字", "点击图片输入文字(双击可再编辑)"),
                 ("mosaic", _glyph_icon("mosaic"), "马赛克", "拖动框选区域打马赛克"),
                 ("crop", FIF.CUT.icon(), "裁剪", "拖动框选裁剪区域"))
        for key, icon, text, tip in tools:
            self._tbtns[key] = tbtn(icon, text, tip,
                                    cb=lambda _=False, k=key: self._on_tool(k))
        vsep()
        self._btn_color = tbtn(self._color_icon(), "颜色", "标注颜色",
                               False, self._pick_color)
        self._btn_width = tbtn(_glyph_icon("width"), "粗细", "线条粗细",
                               False, self._pick_width)
        vsep()
        self._btn_more = tbtn(FIF.MORE.icon(), "更多", "保存 / 压缩 / 旋转 / 缩放",
                              False, self._more_menu)
        self._btn_done = tbtn(_glyph_icon("check", QColor("#FFFFFF")), "完成",
                              "复制到剪贴板并关闭(画布模式同时更新画布)",
                              False, self._on_done, primary=True)

    def _color_icon(self):
        pm = QPixmap(18, 18)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(TEXT), 1.2))
        p.setBrush(self._color)
        p.drawEllipse(2.5, 2.5, 13, 13)
        p.end()
        return QIcon(pm)

    def _menu_at(self, btn):
        """菜单弹出锚点(编辑器坐标系)。按钮包在容器里,geometry() 相对容器,
        必须经 mapTo 从按钮自身坐标系换算——此前用 bar.mapFromParent 误把
        容器坐标当父层坐标,菜单被双重偏移弹到屏幕外(表现为点了没反应)。"""
        return btn.mapTo(self, btn.rect().center())

    def _pick_color(self):
        m = RoundMenu(parent=self)
        for hexc in self.COLORS:
            pm = QPixmap(16, 16)
            pm.fill(Qt.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.Antialiasing)
            pp.setPen(QPen(QColor(TEXT), 1))
            pp.setBrush(QColor(hexc))
            pp.drawEllipse(1.5, 1.5, 13, 13)
            pp.end()
            a = QAction("● " + self.COLOR_NAMES.get(hexc, hexc), m)
            a.setIcon(QIcon(pm))
            a.triggered.connect(lambda _=False, c=hexc: self._set_color(c))
            m.addAction(a)
        m.exec(self._menu_at(self._btn_color))

    def _set_color(self, hexc):
        self._color = QColor(hexc)
        self._btn_color.setIcon(self._color_icon())
        self._apply_style_selected()

    def _pick_width(self):
        m = RoundMenu(parent=self)
        for w, name in self.PEN_WIDTHS:
            mark = "● " if w == self._pen_w else ""
            a = QAction("%s%s(%dpx)" % (mark, name, w), m)
            a.triggered.connect(lambda _=False, v=w: self._set_width(v))
            m.addAction(a)
        m.exec(self._menu_at(self._btn_width))

    def _set_width(self, w):
        self._pen_w = int(w)
        self._apply_style_selected()

    def _apply_style_selected(self):
        """把当前颜色/粗细应用到选中的标注(选中即改,可撤销);无选中只改
        后续绘制参数。文字只改颜色;马赛克不适用。"""
        old = []
        col = QColor(self._color)
        for it in self.annos:
            if not it.isSelected():
                continue
            if isinstance(it, AnnoText):
                old.append((it, "text", QColor(it.defaultTextColor())))
                it.setDefaultTextColor(col)
            elif not isinstance(it, AnnoMosaic):
                pen = QPen(it.pen())
                old.append((it, "pen", QPen(it.pen())))
                pen.setColor(col)
                pen.setWidthF(float(self._pen_w))
                it.setPen(pen)
        if old:
            self._record({"type": "style", "old": old})

    def _more_menu(self):
        m = RoundMenu(parent=self)

        def act(text, fn):
            a = QAction(text, m)
            a.triggered.connect(lambda _=False: fn())
            m.addAction(a)

        act("保存到文件…", self._on_save)
        act("压缩导出…", self._on_compress)
        m.addSeparator()
        act("⟲ 向左旋转 90°", lambda: self._transform(
            lambda im: im.rotate(90, expand=True)))
        act("⟳ 向右旋转 90°", lambda: self._transform(
            lambda im: im.rotate(-90, expand=True)))
        act("按百分比缩放…", self._scale_dialog)
        m.exec(self._menu_at(self._btn_more))

    def _scale_dialog(self):
        d = ScaleDialog(self)
        if d.exec() and d.value() != 100:
            vv = d.value()
            self._transform(lambda im: im.resize(
                (max(1, int(im.width * vv / 100)), max(1, int(im.height * vv / 100))),
                cm.RESAMPLE))

    def _layout_overlays(self):
        if not hasattr(self, "_bar"):
            return
        self._bar.adjustSize()
        self._bar.move(max(8, (self.width() - self._bar.width()) // 2),
                       self.height() - self._bar.height() - 14)
        self._bar.raise_()
        self.lbl_status.move(14, 10)
        self.lbl_status.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_overlays()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)
        self._layout_overlays()

    # ---- 工具 ----
    def _on_tool(self, key):
        self._clear_preview()
        if key == "select":
            self.tool = None
        elif self.tool == key:
            self.tool = None
        else:
            self.tool = key
        # 选择模式开框选;绘制工具关掉避免和形状预览打架
        self.view.setDragMode(QGraphicsView.RubberBandDrag
                              if self.tool is None
                              else QGraphicsView.NoDrag)
        self._sync_tools()
        self._status()

    def _sync_tools(self):
        for k, b in self._tbtns.items():
            b.setChecked(k == self.tool or
                         (k == "select" and self.tool is None))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.tool is not None:
                self._on_tool("select")     # 先退工具,再按才退出(QQ 式)
            elif self._embedded:
                self.finished.emit()        # 内嵌形态:交还容器(查看器回查看态)
            else:
                self.close()
        elif e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self._delete_selected()
        elif e.key() == Qt.Key_Z and e.modifiers() & Qt.ControlModifier:
            self._on_undo()
        else:
            super().keyPressEvent(e)

    # ---- 文字标注(场景内直接输入,双击可再编辑;空文字失焦自动清除) ----
    def _place_text(self, sp):
        it = AnnoText("")
        f = QFont(self.font())
        f.setPixelSize(self._font_px)
        f.setBold(True)
        it.setFont(f)
        it.setDefaultTextColor(QColor(self._color))
        it.setPos(sp)
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        self.annos.append(it)
        self._record({"type": "add", "item": it})
        it.setTextInteractionFlags(Qt.TextEditorInteraction)
        it.setFocus()
        self._status()

    # ---- 预览项(拖动中的形状,场景坐标系) ----
    def _clear_preview(self):
        if self._preview is not None:
            self.scene.removeItem(self._preview)
            self._preview = None
        self._preview_path = None

    def _pen_style(self, dashed=False):
        pen = QPen(QColor(self._color) if not dashed else QColor("#FF5252"),
                   1.5 if dashed else self._pen_w)
        pen.setCosmetic(True)          # 视觉线宽不随视图缩放
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        if dashed:
            pen.setStyle(Qt.DashLine)
        return pen

    def _anno_pen(self):
        pen = QPen(QColor(self._color), self._pen_w)   # 图像空间线宽
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _press(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.tool is None:
            self._ovp(e)               # 选择模式:交给 Qt 原生项交互
            return
        if self.tool == "text":
            self._place_text(self.view.mapToScene(e.position().toPoint()))
            return
        if self.tool in ("crop", "rect", "ellipse", "arrow", "pen", "mosaic"):
            self._drag_start = e.position()
            self._pen_pts = [e.position()] if self.tool == "pen" else None

    def _move(self, e):
        if self._drag_start is None:
            if self.tool is None:
                self._ovm(e)
            return
        # 预览必须画在场景坐标系(mapToScene):视图缩放/平移后仍贴着鼠标
        s1 = self.view.mapToScene(self._drag_start.toPoint())
        s2 = self.view.mapToScene(e.position().toPoint())
        if self.tool == "pen":
            self._pen_pts.append(e.position())
            if self._preview is None:
                self._preview_path = QPainterPath(s1)
                self._preview = QGraphicsPathItem(self._preview_path)
                self._preview.setPen(self._pen_style())
                self.scene.addItem(self._preview)
            else:
                self._preview_path.lineTo(s2)
                self._preview.setPath(self._preview_path)
            return
        r = QRectF(s1, s2).normalized()
        dashed = self.tool in ("crop", "mosaic")
        need = (QGraphicsRectItem if self.tool in ("rect", "crop", "mosaic")
                else QGraphicsEllipseItem if self.tool == "ellipse"
                else QGraphicsLineItem)
        if self._preview is None or not isinstance(self._preview, need):
            self._clear_preview()
            self._preview = need()
            self._preview.setPen(self._pen_style(dashed=dashed))
            self.scene.addItem(self._preview)
        if isinstance(self._preview, QGraphicsLineItem):
            self._preview.setLine(s1.x(), s1.y(), s2.x(), s2.y())
        else:
            self._preview.setRect(r)

    def _release(self, e):
        if self._drag_start is None:
            if self.tool is None:
                self._ovr(e)
            return
        start = self.view.mapToScene(self._drag_start.toPoint())
        end = self.view.mapToScene(e.position().toPoint())
        pen_pts = self._pen_pts
        self._drag_start = None
        self._pen_pts = None
        self._clear_preview()
        tool = self.tool
        if tool == "pen":
            pts = [self._to_img(self.view.mapToScene(p.toPoint()))
                   for p in (pen_pts or [])]
            if len(pts) >= 2 and (abs(pts[-1][0] - pts[0][0]) +
                                  abs(pts[-1][1] - pts[0][1]) > 2):
                self._finish_anno("pen", pts=pts)
            self._status()
            return
        ix0, iy0 = self._to_img(start)
        ix1, iy1 = self._to_img(end)
        if tool == "arrow":
            # 箭头有方向性:必须用原始 起点→终点;下面的 min/max 归一化
            # 会把所有箭头掰成「往右下」,反向/斜向拖拽方向即错(真机已现)
            if (ix1 - ix0) ** 2 + (iy1 - iy0) ** 2 >= 9:
                self._finish_anno("arrow", (ix0, iy0), (ix1, iy1))
            return
        ix0, ix1 = sorted((ix0, ix1))
        iy0, iy1 = sorted((iy0, iy1))
        ix0, iy0 = max(0, ix0), max(0, iy0)
        ix1, iy1 = min(self.base.width, ix1), min(self.base.height, iy1)
        if ix1 - ix0 < 3 or iy1 - iy0 < 3:
            return
        if tool == "crop":
            old_base, old_annos = self._flatten()
            self.base = self.base.crop((ix0, iy0, ix1, iy1))
            self._record({"type": "img", "base": old_base,
                          "annos": old_annos})
            self.tool = None
            self._sync_tools()
            self._redraw()
            return
        # 矢量标注:矩形/椭圆/箭头/马赛克 → 场景项,后续可选中/移动/删除
        self._finish_anno(tool, (ix0, iy0), (ix1, iy1))
        # QQ 式:标注工具保持激活,可连续标注;选择工具才可移动标注

    def _finish_anno(self, kind, p0=None, p1=None, pts=None):
        """按当前颜色/线宽把一个标注收成场景矢量项。"""
        col = QColor(self._color)
        r = QRectF(QPointF(p0[0], p0[1]),
                   QPointF(p1[0], p1[1])).normalized() if p1 is not None else None
        if kind == "rect":
            it = AnnoRect(r)
            it.setPen(self._anno_pen())
            it.setBrush(Qt.NoBrush)
        elif kind == "ellipse":
            it = AnnoEllipse(r)
            it.setPen(self._anno_pen())
            it.setBrush(Qt.NoBrush)
        elif kind == "arrow":
            it = AnnoPath(self._arrow_path(QPointF(0, 0),
                                           QPointF(p1[0] - p0[0],
                                                   p1[1] - p0[1])))
            it.setPos(p0[0], p0[1])
            pen = self._anno_pen()
            pen.setCosmetic(False)
            it.setPen(pen)
            it.setBrush(col)
        elif kind == "pen":
            path = QPainterPath(QPointF(0, 0))
            for pt in pts[1:]:
                path.lineTo(pt[0] - pts[0][0], pt[1] - pts[0][1])
            it = AnnoPath(path)
            it.setPos(pts[0][0], pts[0][1])
            it.setPen(self._anno_pen())
            it.setBrush(Qt.NoBrush)
        elif kind == "mosaic":
            pm = self._mosaic_patch(r)
            if pm is None:
                return
            it = AnnoMosaic(pm)
            it.setTransformationMode(Qt.FastTransformation)
            it.setPos(r.topLeft())
        else:
            return
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        self.annos.append(it)
        self._record({"type": "add", "item": it})
        self._status()

    @staticmethod
    def _arrow_path(p0, p1):
        import math as _m
        path = QPainterPath(p0)
        path.lineTo(p1)
        ang = _m.atan2(p1.y() - p0.y(), p1.x() - p0.x())
        L = 14.0
        a = 0.45
        head = QPolygonF([
            p1,
            QPointF(p1.x() - L * _m.cos(ang - a), p1.y() - L * _m.sin(ang - a)),
            QPointF(p1.x() - L * _m.cos(ang + a), p1.y() - L * _m.sin(ang + a))])
        path.addPolygon(head)
        path.closeSubpath()
        return path

    def _mosaic_patch(self, r):
        """区域马赛克:从原图裁剪 → 缩小 → 放大(块状),返回像素化贴片。"""
        rect = r.toAlignedRect().intersected(
            QRect(0, 0, self.base.width, self.base.height))
        if rect.width() < 2 or rect.height() < 2:
            return None
        region = self.base.crop((rect.x(), rect.y(),
                                 rect.right() + 1, rect.bottom() + 1))
        small = region.resize((max(1, region.width // 12),
                               max(1, region.height // 12)), cm.RESAMPLE)
        pix = small.resize(region.size, cm.Image.NEAREST)
        return QPixmap.fromImage(pil_to_qimage(pix))

    # ---- 标注管理 / 撤销 ----
    def _record(self, rec):
        self.undo_stack.append(rec)
        del self.undo_stack[:-40]

    def _prune_add_record(self, item):
        """空文字项自动清除时,弹掉它栈顶的 add 记录(撤销不该复活空项)。"""
        if self.undo_stack and self.undo_stack[-1].get("type") == "add" \
                and self.undo_stack[-1].get("item") is item:
            self.undo_stack.pop()

    def _drop_anno(self, it, record=True):
        if it in self.annos:
            self.annos.remove(it)
        self.scene.removeItem(it)
        if record:
            self._record({"type": "del", "items": [it]})
        self._status()

    def _delete_selected(self):
        sel = [i for i in self.annos if i.isSelected()]
        if not sel:
            return
        for it in sel:
            self.annos.remove(it)
            self.scene.removeItem(it)
        self._record({"type": "del", "items": sel})
        self._status()

    def _flatten(self):
        """把全部标注栅格化进位图(变换/裁剪前),返回旧 (base, annos) 供撤销。"""
        old = (self.base, list(self.annos))
        if self.annos:
            self._clear_preview()
            self.base = self._final_image()
            for it in self.annos:
                self.scene.removeItem(it)
            self.annos = []
        return old

    def _final_image(self):
        """导出图:无标注=原图;有标注=场景整体渲染(所见即所得,含文字)。"""
        if not self.annos:
            return self.base
        w, h = self.base.size
        sel = [(it, it.isSelected()) for it in self.annos]
        for it, _ in sel:
            it.setSelected(False)
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(Qt.white)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        self.scene.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
        p.end()
        for it, s in sel:
            it.setSelected(s)
        return _qimage_to_pil(img)

    def _transform(self, fn):
        old_base, old_annos = self._flatten()
        self.base = fn(self.base)
        self._record({"type": "img", "base": old_base, "annos": old_annos})
        self._redraw()

    def _to_img(self, sp):
        r = self._pix_item.boundingRect()
        sc = self._pix_item.scale()
        # round 而非 int 截断:视图<->场景浮点往返有亚像素噪声,截断会整框偏 1px
        return (round((sp.x() - r.x()) / sc), round((sp.y() - r.y()) / sc))

    def _redraw(self):
        w, h = self.base.size
        self._pix_item.setPixmap(QPixmap.fromImage(pil_to_qimage(self.base)))
        self.view.resetTransform()
        self.scene.setSceneRect(QRectF(0, 0, w, h))
        self.view.fitInView(self._pix_item, Qt.KeepAspectRatio)
        self._status()

    def _status(self):
        w, h = self.base.size
        tip = {"crop": "拖动框选裁剪区域", "rect": "拖动绘制矩形(可连续标注)",
               "ellipse": "拖动绘制椭圆", "arrow": "拖动绘制箭头",
               "pen": "按住拖动自由绘制", "text": "点击图片输入文字(双击可再编辑)",
               "mosaic": "拖动框选区域打马赛克"}.get(
                   self.tool, "选择:点击标注后拖动移动,Del 删除,双击文字再编辑")
        self.lbl_status.setText("%s   |   标注 %d 个   |   尺寸: %s×%s   |   可撤销 %d 步" % (
            tip, len(self.annos), w, h, len(self.undo_stack)))
        self.lbl_status.adjustSize()

    def _on_undo(self):
        self._clear_preview()
        if not self.undo_stack:
            return
        rec = self.undo_stack.pop()
        t = rec["type"]
        if t == "add":
            it = rec["item"]
            if it in self.annos:
                self._drop_anno(it, record=False)
        elif t == "del":
            for it in rec["items"]:
                self.scene.addItem(it)      # item 对象仍在,重新挂回场景
                self.annos.append(it)
        elif t == "move":
            rec["item"].setPos(rec["pos"])
        elif t == "style":
            for it, kind, val in rec["old"]:
                if it.scene() is not self.scene:   # 项已被删除则跳过
                    continue
                if kind == "text":
                    it.setDefaultTextColor(val)
                else:
                    it.setPen(val)
        elif t == "img":
            self.base = rec["base"]
            for it in self.annos:
                self.scene.removeItem(it)
            self.annos = []
            for it in rec["annos"]:
                self.scene.addItem(it)
                self.annos.append(it)
        self._redraw()

    def _emit_result(self, result):
        """把编辑产物(路径 str 或 PIL.Image)交给调用方;失败不影响编辑器本身。"""
        if self._on_result is None:
            return
        try:
            self._on_result(result)
        except Exception:
            cm.log("编辑结果回写失败:\n%s" % traceback.format_exc())

    def _on_done(self):
        """✓ 完成:复制到剪贴板(画布模式同时回写画布)并关闭编辑器。"""
        self._clear_preview()
        final = self._final_image()
        ok = cm.set_dib_to_clipboard(self.app.worker.hwnd,
                                     cm.pil_to_dib(final))
        if not ok:
            InfoBar.error("复制失败", "剪贴板被占用,可改用「更多 → 保存到文件」",
                          parent=self, position=InfoBarPosition.TOP)
            return
        self._emit_result(final)
        if self._embedded:
            self.finished.emit()
        else:
            self.close()

    def _on_save(self):
        self._clear_preview()
        p, _ = QFileDialog.getSaveFileName(
            self, "保存图片", "clipboard_edit.png", "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
        if p:
            fmt = "JPEG" if p.lower().endswith((".jpg", ".jpeg")) else "PNG"
            try:
                self._final_image().save(p, fmt)
                self.lbl_status.setText("✅ 已保存: " + p)
                self.lbl_status.adjustSize()
                self._emit_result(p)
            except Exception:
                MessageBox("WinClipboard", "保存失败。", self).exec()

    def _on_compress(self):
        CompressDialog(self).exec()


# ============================================================
# 内容查看器(文本 / 代码全量查看 · pygments 语法高亮 · JSON 美化)
# ============================================================
# QScintilla 无 PySide6 官方绑定;此处用 PySide6 社区通行方案:
# QPlainTextEdit + pygments 词法 + 自己上色(git-cola 同款思路)。
# pygments 为可选依赖,缺失时退化为纯文本查看;高亮只做一次性整篇渲染
# (文档只读且内容不变,不必用 QSyntaxHighlighter 逐块重算)。
try:
    from pygments.lexers import get_lexer_by_name as _pg_get_lexer
    from pygments.styles import get_style_by_name as _pg_get_style
except Exception:
    _pg_get_lexer = None
    _pg_get_style = None

_HL_CHAR_LIMIT = 60000         # 超过该字符数不染色(pygments 纯 Python:60KB ≈ 250ms,
                               # 旧上限 150KB 实测打开查看器可冻结 UI 超过 1 秒)


_JSON_TOKEN_RE = None       # 惰性编译:键/字符串/数字/常量 单遍正则


def _json_runs(text):
    """JSON 快速染色:pygments 的 JsonLexer 对稠密 JSON 是病态慢
    (实测 4KB 单行 ≈ 1.7s,选中预览直接冻结 UI),这里改单遍正则切 token,
    颜色仍取自 pygments 样式(Name.Tag/String/Number/Keyword.Constant),
    与其他语言的高亮观感一致。返回 runs 或 None(不染色)。"""
    global _JSON_TOKEN_RE
    if _pg_get_style is None:
        return None
    if _JSON_TOKEN_RE is None:
        import re as _re
        _JSON_TOKEN_RE = _re.compile(
            r'"(?:[^"\\]|\\.)*"(?=\s*:)'          # 键(后跟冒号)
            r'|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'   # 数字
            r'|\b(?:true|false|null)\b'            # 常量
            r'|"(?:[^"\\]|\\.)*"',                 # 字符串
        )
    try:
        from pygments.token import Token
        style = _pg_get_style("monokai" if IS_DARK else "default")
        kinds = [("key", Token.Name.Tag), ("num", Token.Number),
                 ("const", Token.Keyword.Constant),
                 ("str", Token.Literal.String.Double)]
        fmts = {}
        for name, tok in kinds:
            st = style.style_for_token(tok)
            f = QTextCharFormat()
            if st.get("color"):
                f.setForeground(QColor("#" + st["color"]))
            if st.get("bold"):
                f.setFontWeight(QFont.Bold)
            if st.get("italic"):
                f.setFontItalic(True)
            fmts[name] = f
    except Exception:
        return None
    runs = []
    for m in _JSON_TOKEN_RE.finditer(text):
        name = _json_kind(m, text)
        if name is None:
            continue
        f = fmts.get(name)
        if f is None or not f.foreground().style():
            continue
        s, e2 = m.span()
        if runs and runs[-1][1] == s and runs[-1][2] is f:
            runs[-1] = (runs[-1][0], e2, f)        # 相邻同色合并
        else:
            runs.append((s, e2, f))
    return runs


def _json_kind(m, text):
    """判定正则命中片段的类型:键=字符串且其后(跳空白)是冒号。"""
    v = m.group(0)
    if v[0] == '"':
        return "key" if _json_is_key(m, text) else "str"
    if v[0] in "-0123456789":
        return "num"
    return "const"


def _json_is_key(m, text):
    end = m.end()
    while end < len(text) and text[end] in " \t\r\n":
        end += 1
    return end < len(text) and text[end] == ":"


def pygments_runs(text, lang, force=False):
    """计算 pygments 染色 run 列表 [(start, end, QTextCharFormat)],不动文档。

    只返回有视觉效果的 run(默认样式跳过);lang == "text"、pygments 缺失、
    文本超长时返回 None。拆出本函数是为了让查看器缓存染色结果——
    原文↔代码视图来回切换时只重放 runs,不重跑词法(25KB 文本≈400ms)。
    """
    if lang == "json":
        if len(text) > _HL_CHAR_LIMIT and not force:
            return None
        return _json_runs(text)
    if lang == "text" or _pg_get_lexer is None \
            or (len(text) > _HL_CHAR_LIMIT and not force):
        return None
    try:
        lexer = _pg_get_lexer(lang)
        style = _pg_get_style("monokai" if IS_DARK else "default")
    except Exception:
        return None
    fmt_cache = {}

    def fmt_of(tok):
        hit = fmt_cache.get(tok)
        if hit is None:
            st = style.style_for_token(tok)
            f = QTextCharFormat()
            if st.get("color"):
                f.setForeground(QColor("#" + st["color"]))
            if st.get("bold"):
                f.setFontWeight(QFont.Bold)
            if st.get("italic"):
                f.setFontItalic(True)
            if st.get("underline"):
                f.setFontUnderline(True)
            hit = (f, bool(st.get("color") or st.get("bold")
                           or st.get("italic") or st.get("underline")))
            fmt_cache[tok] = hit
        return hit

    toks = []           # (start, end, token) 合并相邻同 token
    pos = 0
    limit = len(text)
    for tok, val in lexer.get_tokens(text):
        n = len(val)
        if n == 0:
            continue
        end = min(pos + n, limit)
        if end <= pos:
            break
        if toks and toks[-1][2] is tok:
            toks[-1] = (toks[-1][0], end, tok)
        else:
            toks.append((pos, end, tok))
        pos = end
        if pos >= limit:
            break
    runs = []
    for s, e2, tok in toks:
        f, has_effect = fmt_of(tok)
        if has_effect:
            runs.append((s, e2, f))
    return runs


def apply_format_runs(doc, runs):
    """把 pygments_runs 的结果批量落到文档(一次性游标重放)。

    必须包在 beginEditBlock 里:单行长文本(如压缩 JSON)逐次 setCharFormat
    每次都整块重排,600 个 run 实测 1.6s;批处理推迟重排,同场景 ~9ms。
    """
    if not runs:
        return False
    cur = QTextCursor(doc)
    cur.beginEditBlock()
    for s, e2, f in runs:
        cur.setPosition(s)
        cur.setPosition(e2, QTextCursor.KeepAnchor)
        cur.setCharFormat(f)
    cur.endEditBlock()
    return True


def apply_pygments_format(doc, text, lang, force=False):
    """把 pygments 染色一次性应用到只读文档(预览面板等共用入口)。"""
    runs = pygments_runs(text, lang, force)
    return bool(runs) and apply_format_runs(doc, runs)


class _LineArea(QWidget):
    """行号栏:绘制回调交给宿主编辑器(经典 Qt 行号区模式)。"""

    def __init__(self, editor):
        super().__init__(editor)
        self._ed = editor

    def paintEvent(self, e):
        self._ed.line_area_paint(self)

    def mousePressEvent(self, e):        # 点行号 = 选中该行,顺手支持
        self._ed.line_area_click(e.pos().y())


class _CodeEdit(QPlainTextEdit):
    """只读代码区:等宽字体 + 行号栏 + 行号点击选中整行 + Fluent 细滚动条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QFrame.NoFrame)
        f = QFont()
        f.setFamilies(["Cascadia Mono", "Consolas"])   # Win11 优先,缺失自动回退
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.setFont(f)
        self._line_area = _LineArea(self)
        self.blockCountChanged.connect(self._update_area_width)
        self.updateRequest.connect(self._update_area)
        self._update_area_width(0)
        # Fluent 细滚动条替换系统粗滚动条;手柄悬停才展开
        self._scroll_del = SmoothScrollDelegate(self, useAni=True)
        for b in (self._scroll_del.vScrollBar, self._scroll_del.hScrollBar):
            b.handleDisplayMode = ScrollBarHandleDisplayMode.ON_HOVER

    def _update_area_width(self, _):
        digits = max(3, len(str(max(1, self.blockCount()))))
        w = self.fontMetrics().horizontalAdvance("9") * digits + 20
        self._line_area.resize(w, self.height())
        self.setViewportMargins(w, 12, 16, 12)

    def _update_area(self, rect, dy):
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(0, rect.y(), self._line_area.width(), rect.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # 行号栏贴住视口左缘(视口本身已被 viewportMargins 右移),上下对齐视口
        vg = self.viewport().geometry()
        self._line_area.setGeometry(vg.left() - self._line_area.width(), vg.top(),
                                    self._line_area.width(), vg.height())

    def line_area_paint(self, area):
        p = QPainter(area)
        p.fillRect(area.rect(), QColor(GUTTER_BG))
        p.setPen(QColor(FIELD_BORDER))
        p.drawLine(area.width() - 1, 0, area.width() - 1, area.height())
        blk = self.firstVisibleBlock()
        n = blk.blockNumber()
        top = round(self.blockBoundingGeometry(blk).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(blk).height())
        p.setPen(QColor(TER))
        while blk.isValid() and top <= area.height():
            if blk.isVisible():
                p.drawText(0, top, area.width() - 10, self.fontMetrics().height(),
                           Qt.AlignRight, str(n + 1))
            blk = blk.next()
            n += 1
            top = bottom
            bottom = top + round(self.blockBoundingRect(blk).height())
            if bottom == top:            # 空文档保护
                break
        p.end()

    def line_area_click(self, y):
        blk = self.cursorForPosition(QPoint(0, max(0, y))).block()
        c = self.textCursor()
        c.setPosition(blk.position())
        c.setPosition(blk.position() + blk.length() - 1, QTextCursor.KeepAnchor)
        self.setTextCursor(c)


class ViewerWindow(QWidget):
    """内容查看器(智能 Clipboard Inspector):按识别格式切换阅读视图。

    结构 = 头部「标题+已识别信息」/ 工具栏(视图切换·换行·字号·查找 →
    复制·更多) / 内容卡片(QStackedWidget:文本编辑器·JSON 结构树·
    Markdown 预览·CSV 表格 + 浮动查找条) / 底部状态栏。
    视图可用性由「识别层(cm.detect_lang) + 内容解析」决定:JSON→
    原文/格式化/结构(带有效性),Markdown→原文/预览,CSV→原文/表格,
    XML→原文/格式化,代码→原文/高亮,纯文本/文件→原文;语言可在「更多」
    菜单手动覆盖。复制始终复制原始记录。
    """

    _ZOOMS = (75, 90, 100, 110, 125, 150)
    _TREE_NODE_CAP = 3000          # 结构树总节点上限(超出部分显示省略行)
    _TREE_LAZY_DEPTH = 2           # 首次只物化到该层,更深展开时再建
    _TREE_WIDE = 400               # 超过该子项数的宽节点也走懒加载
    _TABLE_ROW_CAP = 2000

    def __init__(self, app, rec):
        super().__init__(None)
        self.app = app
        self.rec = rec
        if rec.get("type") == "files":
            self._kind = "files"
            self._raw = "\n".join(rec.get("content") or [])
        else:
            self._kind = "text"
            self._raw = str(rec.get("content") or "")
        # 内容识别管线:category/subtype/confidence(URL/JWT/Base64 等智能
        # 视图的数据源;手动改语言后智能视图让位 → 见 _mode_table)
        self._content = cm.detect_content(self._raw) if self._kind == "text" else None
        self._auto_lang = self._content.lang if self._content is not None else "text"
        self._lang_index = 0                    # VIEW_LANGS 下标(0 = 自动识别)
        self._base_pt = 10                      # 100% 时的字号(磅)
        self._meta_suffix = ""
        self._mode = "raw"
        self._hl_cache = {}                  # (lang, 文本长度) → 染色 runs(切视图重放)
        self._ed_text = None                 # 染色编辑器当前文本(相同则跳过重灌)
        self._ed_raw_text = None             # 原文编辑器当前文本
        self._ed_hl_state = None             # 染色编辑器已呈现的 (lang, 长度)
        # 内容解析(原始文本不变,一次性算好供视图/元信息取用)
        if self._kind == "text":
            self._json_err = cm.json_error(self._raw)
            self._xml_fmt = cm.xml_pretty(self._raw)
            self._csv_delim = cm.csv_sniff(self._raw)
            self._csv_rows = cm.parse_csv_rows(
                self._raw, self._csv_delim or ",") if self._csv_delim else []
            _sub = self._content.subtype if self._content is not None else ""
            self._url_inf = cm.url_inspect(self._raw) if _sub == "web_url" else None
            self._jwt_inf = cm.jwt_inspect(self._raw) if _sub == "jwt" else None
            self._b64 = cm.b64_decode_auto(self._raw) if _sub == "base64" else None
        else:
            self._json_err = None
            self._xml_fmt = None
            self._csv_delim = None
            self._csv_rows = []
            self._url_inf = None
            self._jwt_inf = None
            self._b64 = None

        self.setWindowTitle("查看 · WinClipboard")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(960, 640)
        self.setMinimumSize(720, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 头部:标题 + 已识别信息(只放文本,不放按钮) ----
        head = QVBoxLayout()
        head.setContentsMargins(16, 14, 16, 6)
        head.setSpacing(2)
        self.lbl_title = StrongBodyLabel(
            "文件列表" if self._kind == "files" else "文本内容")
        self.lbl_meta = CaptionLabel("")
        head.addWidget(self.lbl_title)
        head.addWidget(self.lbl_meta)
        root.addLayout(head)

        # ---- 工具栏:视图切换 + 高频操作;主按钮 = 复制/打开 ----
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 2, 16, 8)
        bar.setSpacing(8)
        self.seg = SegmentedWidget()
        self.seg.currentItemChanged.connect(
            lambda k: self._set_view(k) if k else None)
        self.btn_wrap = ToggleButton("自动换行")
        if self._auto_lang == "text" and self._kind == "text":
            self.btn_wrap.setChecked(True)      # 纯文本默认自动换行
            # 注意:先设状态再接信号——此时编辑器尚未创建,toggled 会踩空
        self.btn_wrap.toggled.connect(self._set_wrap)
        self.cmb_zoom = ComboBox()
        self.cmb_zoom.addItems(["%d%%" % z for z in self._ZOOMS])
        self.cmb_zoom.setCurrentIndex(self._ZOOMS.index(100))
        self.cmb_zoom.setFixedWidth(88)
        self.cmb_zoom.currentIndexChanged.connect(
            lambda i: self._apply_zoom(self._ZOOMS[i]))
        self.btn_find = TransparentToolButton(FIF.SEARCH)
        self.btn_find.setToolTip("查找 (Ctrl+F)")
        self.btn_find.clicked.connect(self._open_find)
        if self._kind == "files":
            self.btn_copy = PushButton("复制")
            self.btn_copy.setIcon(FIF.COPY.icon())
            self.btn_open = PrimaryPushButton("打开")
            self.btn_open.setIcon(FIF.FOLDER.icon())
            self.btn_open.setToolTip("系统默认程序打开全部文件")
            self.btn_open.clicked.connect(lambda: self.app.open_files(self.rec))
        else:
            self.btn_copy = PrimaryPushButton("复制")
            self.btn_copy.setIcon(FIF.COPY.icon())
            self.btn_open = None
        self.btn_copy.setMinimumWidth(96)
        self.btn_copy.clicked.connect(self._copy_all)
        self.btn_more = TransparentToolButton(FIF.MORE)
        self.btn_more.setToolTip("更多操作")
        self.btn_more.clicked.connect(self._show_more_menu)
        bar.addWidget(self.seg)
        bar.addWidget(self.btn_wrap)
        bar.addWidget(self.cmb_zoom)
        bar.addWidget(self.btn_find)
        bar.addStretch(1)
        bar.addWidget(self.btn_copy)
        if self.btn_open is not None:
            bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_more)
        root.addLayout(bar)

        # ---- 内容卡片:多视图堆叠 + 浮动查找条 ----
        # 两个文本页:editor_raw(原文,永不染色)与 editor(格式化/代码,染色
        # 结果常驻文档)——原文↔代码切换是纯换页,不再清格式/重放 runs
        # (实测 25KB 文本重放 4800 个 run 要 230ms,这就是切换卡顿的来源)。
        _ed_qss = ("QPlainTextEdit { background: %s; border: none; color: %s;"
                   " selection-background-color: %s; }"
                   % (CARD_BG, TEXT, ACCENT_SOFT))
        body = QHBoxLayout()
        body.setContentsMargins(16, 2, 16, 10)
        self.card = SimpleCardWidget()
        cv = QVBoxLayout(self.card)
        cv.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self.card)
        self.editor_raw = _CodeEdit(self.card)
        self.editor_raw.setStyleSheet(_ed_qss)
        self.editor = _CodeEdit(self.card)
        self.editor.setStyleSheet(_ed_qss)
        self.tree = TreeWidget(self.card)
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.itemExpanded.connect(self._on_tree_expand)   # 懒加载逐层建
        self.md_view = TextEdit(self.card)
        self.md_view.setReadOnly(True)
        self.md_view.setViewportMargins(8, 8, 8, 8)
        self.table = TableWidget(self.card)
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)   # 只读
        self.table.setWordWrap(False)
        # 字段/值 信息页(URL/JWT 共用):右键或双击复制值
        self.info = TreeWidget(self.card)
        self.info.setColumnCount(2)
        self.info.setHeaderLabels(["字段", "值"])
        self.info.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.info.header().setStretchLastSection(True)
        self.info.setBorderVisible(True)
        self.info.setContextMenuPolicy(Qt.CustomContextMenu)
        self.info.customContextMenuRequested.connect(self._info_menu)
        self.info.itemDoubleClicked.connect(
            lambda it, _col: self.app.core.copy_text(str(it.data(1, Qt.UserRole) or "")))
        # Base64 解码图片页
        self.img_view = QLabel(self.card)
        self.img_view.setAlignment(Qt.AlignCenter)
        for pw in (self.editor_raw, self.editor, self.tree,
                   self.md_view, self.table, self.info, self.img_view):
            self.stack.addWidget(pw)
        SmoothScrollDelegate(self.tree, useAni=True)
        SmoothScrollDelegate(self.table, useAni=True)
        SmoothScrollDelegate(self.info, useAni=True)
        cv.addWidget(self.stack)
        body.addWidget(self.card)
        root.addLayout(body, 1)
        if self._kind == "files":
            self.editor_raw.viewport().installEventFilter(self)   # 双击行 = 打开

        self._build_find_bar()
        self._build_actions()
        self._build_statusbar()
        self._build_shortcuts()

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.setInterval(1400)
        self._copy_timer.timeout.connect(self._copy_restore)
        for ed in (self.editor, self.editor_raw):
            ed.cursorPositionChanged.connect(self._refresh_pos)
            ed.selectionChanged.connect(self._refresh_pos)
        self._tree_nodes = 0
        self._set_wrap(self.btn_wrap.isChecked())   # 初始换行态落到编辑器

        self._render()

    # ---- 局部构建 ----
    def _build_find_bar(self):
        """内容区右上角浮动查找条(默认隐藏,Ctrl+F 呼出)。"""
        self._find = QFrame(self.card)
        self._find.setObjectName("viewerFindBar")
        self._find.setStyleSheet(
            "QFrame#viewerFindBar { background: %s; border: 1px solid %s;"
            " border-radius: 6px; }" % (CARD_BG, FIELD_BORDER))
        fl = QHBoxLayout(self._find)
        fl.setContentsMargins(8, 6, 8, 6)
        fl.setSpacing(6)
        self.ed_find = SearchLineEdit()
        self.ed_find.setPlaceholderText("查找内容…")
        self.ed_find.setFixedWidth(220)
        self.ed_find.textChanged.connect(self._find_text_changed)
        self.ed_find.searchSignal.connect(lambda: self._find_goto(False))
        self.ed_find.installEventFilter(self)          # Esc 关闭
        self.lbl_find_n = CaptionLabel("0/0")
        self.lbl_find_n.setFixedWidth(44)
        self.lbl_find_n.setAlignment(Qt.AlignCenter)
        b_prev = TransparentToolButton(FIF.CARE_UP_SOLID)
        b_prev.clicked.connect(lambda: self._find_goto(True))
        b_next = TransparentToolButton(FIF.CARE_DOWN_SOLID)
        b_next.clicked.connect(lambda: self._find_goto(False))
        b_close = TransparentToolButton(FIF.CLOSE)
        b_close.clicked.connect(self._close_find)
        for w in (self.ed_find, self.lbl_find_n, b_prev, b_next, b_close):
            fl.addWidget(w)
        self._find.adjustSize()
        self._find.hide()

    def _build_actions(self):
        """「更多」菜单用的常驻 action(窗口持有,菜单随建随删)。"""
        self._lang_acts = []
        for i, (key, name) in enumerate(cm.VIEW_LANGS):
            a = QAction(name, self)
            a.setVisible(self._kind == "text")
            a.triggered.connect(lambda _=False, i=i: self._set_lang_index(i))
            self._lang_acts.append(a)
        self._act_hl = QAction("强制语法高亮", self)
        self._act_hl.setToolTip("文本较长,自动高亮已跳过;点击强制染色"
                                "(大文本会卡顿片刻)")
        self._act_hl.setVisible(False)
        self._act_hl.triggered.connect(self._force_highlight)
        self._act_reveal = QAction("在资源管理器中显示", self)
        self._act_reveal.setVisible(self._kind == "files")
        self._act_reveal.triggered.connect(lambda: self.app.reveal_first(self.rec))

    def _build_statusbar(self):
        sb = QFrame()
        sb.setObjectName("viewerStatusBar")
        sb.setFixedHeight(30)
        sb.setStyleSheet("QFrame#viewerStatusBar {"
                         " border-top: 1px solid %s; }" % LINE)
        sl = QHBoxLayout(sb)
        sl.setContentsMargins(16, 0, 16, 0)
        self.lbl_pos = CaptionLabel("")
        self.lbl_state = CaptionLabel(
            "双击行打开文件" if self._kind == "files" else "已保存到历史")
        sl.addWidget(self.lbl_pos)
        sl.addStretch(1)
        sl.addWidget(self.lbl_state)
        self.layout().addWidget(sb)
        eol = ("CRLF" if "\r\n" in self._raw else
               "CR" if "\r" in self._raw else "LF" if "\n" in self._raw else "")
        self._pos_prefix = "UTF-8 · %s" % eol if eol else "UTF-8"

    def _build_shortcuts(self):
        QShortcut(QKeySequence.Find, self).activated.connect(self._open_find)
        for seq in ("Ctrl+=", "Ctrl++"):
            QShortcut(QKeySequence(seq), self).activated.connect(
                lambda: self._zoom_step(1))
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(
            lambda: self._zoom_step(-1))
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(
            lambda: self._set_zoom(100))

    # ---- 视图模型 ----
    def _mode_table(self):
        """当前语言/内容 → (默认模式, [(key,label),…]);单视图返回空表。"""
        if self._kind != "text":
            return "raw", []
        # 智能视图(仅自动识别时;手动改语言 = 用户要按语言看,智能视图让位)
        if self._lang_index == 0 and self._content is not None:
            sub = self._content.subtype
            if sub == "web_url" and self._url_inf:
                return "url", [("raw", "原文"), ("url", "URL")]
            if sub == "jwt" and self._jwt_inf:
                return "jwt", [("raw", "原文"), ("jwt", "JWT")]
            if sub == "base64" and self._b64:
                return "b64", [("raw", "原文"), ("b64", "解码")]
        lang = self._resolved_lang()
        if lang == "json":
            if self._json_err is None:
                return "fmt", [("raw", "原文"), ("fmt", "格式化"), ("tree", "结构")]
            return "raw", [("raw", "原文")]
        if lang == "markdown":
            return "preview", [("raw", "原文"), ("preview", "预览")]
        if lang == "csv":
            if self._csv_rows:
                return "table", [("raw", "原文"), ("table", "表格")]
            return "raw", [("raw", "原文")]   # 手动强选 CSV 但嗅探不出分隔符
        if lang == "xml":
            if self._xml_fmt:
                return "fmt", [("raw", "原文"), ("fmt", "格式化")]
            return "raw", [("raw", "原文")]
        if lang == "text" or _pg_get_lexer is None:
            return "raw", []
        return "code", [("raw", "原文"), ("code", "代码")]

    def _rebuild_modes(self):
        default, modes = self._mode_table()
        if self._mode not in [k for k, _ in modes]:
            self._mode = default
        self.seg.blockSignals(True)               # 重建期间不触发视图切换
        self.seg.clear()
        for key, label in modes:
            self.seg.addItem(routeKey=key, text=label)
        self.seg.setVisible(len(modes) > 1)
        self.seg.setCurrentItem(self._mode)
        self.seg.blockSignals(False)

    def _set_view(self, key):
        if not key or key == self._mode:
            return
        self._mode = key
        self._apply_view()

    def _apply_view(self):
        """把当前模式落到堆叠页与内容(识别→选择对应查看器)。"""
        mode = self._mode
        if mode == "raw":
            self.stack.setCurrentWidget(self.editor_raw)
            if self._ed_raw_text != self._raw:
                self.editor_raw.setPlainText(self._raw)
                self._ed_raw_text = self._raw
            self._meta_suffix = ""
            self._act_hl.setVisible(False)
        elif mode in ("fmt", "code"):
            self.stack.setCurrentWidget(self.editor)
            text = self._display_text()
            if text != self._ed_text:            # 换语言/格式才重灌
                self.editor.setPlainText(text)
                self._ed_text = text
                self._ed_hl_state = None
            self._apply_highlight()
        elif mode == "tree":
            self.stack.setCurrentWidget(self.tree)
            self._build_tree()
        elif mode == "preview":
            self.stack.setCurrentWidget(self.md_view)
            self.md_view.setMarkdown(self._raw)
        elif mode == "table":
            self.stack.setCurrentWidget(self.table)
            self._build_table()
        elif mode in ("url", "jwt"):
            self.stack.setCurrentWidget(self.info)
            self._build_url_info() if mode == "url" else self._build_jwt_info()
        elif mode == "b64":
            kind, raw = self._b64 or (None, None)
            if kind == "image":
                self.stack.setCurrentWidget(self.img_view)
                self._show_b64_image(raw)
            else:
                # 解码文本落到染色编辑器(不染色:解码后语言未知)
                self.stack.setCurrentWidget(self.editor)
                text = raw.decode("utf-8", "replace")
                if text != self._ed_text:
                    self.editor.setPlainText(text)
                    self._ed_text = text
                    self._ed_hl_state = None
        self._refresh_meta()
        self._refresh_pos()

    def _active_edit(self):
        """当前可见的文本编辑器(查找/行列信息都作用在它上面)。"""
        return self.editor_raw if self.stack.currentWidget() is self.editor_raw \
            else self.editor

    def _display_text(self):
        if self._mode == "fmt":
            lang = self._resolved_lang()
            if lang == "json":
                return cm.json_reformat(self._raw) or self._raw
            if lang == "xml":
                return self._xml_fmt or self._raw
        return self._raw

    # ---- 结构树 / 表格 ----
    def _build_tree(self):
        self.tree.clear()
        self._tree_nodes = 0
        try:
            obj = json.loads(self._raw)
        except Exception:
            return
        self._fill_tree(None, "", obj, 0)
        # 只展开非懒节点:expandToDepth 会连宽节点的占位也展开,
        # 触发 _on_tree_expand 把懒加载又搬回打开时刻
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, Qt.UserRole) is None:
                top.setExpanded(True)
                for j in range(top.childCount()):
                    ch = top.child(j)
                    if ch.data(0, Qt.UserRole) is None and ch.childCount():
                        ch.setExpanded(True)

    def _fill_tree(self, parent_item, key, val, depth):
        if depth > 80:                       # 防 RecursionError
            self._tree_add(parent_item, "…(嵌套过深,已截断)")
            return
        self._tree_nodes += 1
        if isinstance(val, dict):
            pairs = [(str(k), v) for k, v in val.items()]
            label = ("%s  {%d}" % (key, len(pairs))) if key else "{ %d 项 }" % len(pairs)
            item = self._tree_add(parent_item, label)
            self._add_tree_kids(item, pairs, depth)
        elif isinstance(val, list):
            pairs = [("[%d]" % i, v) for i, v in enumerate(val)]
            label = ("%s  [%d]" % (key, len(pairs))) if key else "[ %d 项 ]" % len(pairs)
            item = self._tree_add(parent_item, label)
            self._add_tree_kids(item, pairs, depth)
        else:
            vs = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            if len(vs) > 80:
                vs = vs[:80] + "…"
            self._tree_add(parent_item, ("%s:  %s" % (key, vs)) if key else vs)

    def _add_tree_kids(self, item, pairs, depth):
        """子级物化规则:浅层小节点直接建;深层或宽节点(>400 项)挂占位,
        展开时再建——保证大 JSON 打开即显,构建成本摊到用户点击时。"""
        if not pairs:
            return
        if depth + 1 > self._TREE_LAZY_DEPTH or len(pairs) > self._TREE_WIDE:
            self._tree_add(item, "… 共 %d 项(展开加载)" % len(pairs))
            item.setData(0, Qt.UserRole, pairs)
            return
        for k, v in pairs:
            if self._tree_nodes >= self._TREE_NODE_CAP:
                self._tree_add(item, "… 其余已省略")
                return
            self._fill_tree(item, k, v, depth + 1)

    def _on_tree_expand(self, item):
        """展开带占位的节点:一次性物化其子级(受总节点上限约束)。"""
        pairs = item.data(0, Qt.UserRole)
        if not pairs:
            return
        item.setData(0, Qt.UserRole, None)
        if item.childCount():
            item.removeChild(item.child(0))          # 去掉占位行
        depth = 0
        p = item.parent()
        while p is not None:
            depth += 1
            p = p.parent()
        for k, v in pairs:
            if self._tree_nodes >= self._TREE_NODE_CAP:
                self._tree_add(item, "… 其余已省略")
                return
            self._fill_tree(item, k, v, depth + 1)

    def _tree_add(self, parent_item, text):
        it = QTreeWidgetItem([text])
        if parent_item is None:
            self.tree.addTopLevelItem(it)
        else:
            parent_item.addChild(it)
        return it

    def _build_table(self):
        self.table.setUpdatesEnabled(False)          # 批量填充不逐行重绘
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        rows = self._csv_rows
        if rows:
            header, data = rows[0], rows[1:]
            ncol = max(len(r) for r in rows)
            self.table.setColumnCount(ncol)
            self.table.setHorizontalHeaderLabels(
                [header[i] if i < len(header) else "" for i in range(ncol)])
            self.table.setRowCount(min(len(data), self._TABLE_ROW_CAP))
            for r, row in enumerate(data[:self._TABLE_ROW_CAP]):
                for c in range(ncol):
                    self.table.setItem(
                        r, c, QTableWidgetItem(row[c] if c < len(row) else ""))
            # 采样列宽(表头+前 60 行):全量 ResizeToContents 对 2000 行要几百 ms
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            fm = self.table.fontMetrics()
            for c in range(ncol):
                w = fm.horizontalAdvance(header[c] if c < len(header) else "")
                for rr in range(min(60, self.table.rowCount())):
                    it = self.table.item(rr, c)
                    if it is not None:
                        w = max(w, fm.horizontalAdvance(it.text()))
                self.table.setColumnWidth(c, min(w + 28, 400))
        self.table.setUpdatesEnabled(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    # ---- URL / JWT / Base64 Inspector ----
    def _info_add(self, field, value, parent=None, full=None):
        """加一行「字段/值」;超长值显示截断,完整值存 UserRole 供复制。"""
        shown = value if value is not None else ""
        if len(shown) > 160:
            shown = shown[:160] + "…"
        it = QTreeWidgetItem([field, shown])
        it.setData(1, Qt.UserRole, value if full is None else full)
        it.setToolTip(1, shown)
        if parent is None:
            self.info.addTopLevelItem(it)
        else:
            parent.addChild(it)
        return it

    def _build_url_info(self):
        inf = self._url_inf or {}
        self.info.clear()
        self._info_add("Scheme", inf.get("scheme") or "")
        self._info_add("主机", inf.get("host") or "")
        if inf.get("port"):
            self._info_add("端口", str(inf["port"]))
        self._info_add("路径", inf.get("path") or "/")
        q = inf.get("query") or []
        if q:
            g = self._info_add("Query 参数", "%d 项" % len(q))
            for k, v in q:
                self._info_add(k or "(空)", v, g)
        if inf.get("fragment"):
            self._info_add("Fragment", inf["fragment"])

    def _build_jwt_info(self):
        inf = self._jwt_inf or {}
        self.info.clear()
        head, pay = inf.get("header") or {}, inf.get("payload") or {}
        self._info_add("算法 (alg)", str(head.get("alg", "?")))
        warn = self._info_add("签名", "未验证")
        warn.setForeground(1, QColor("#cf9a00"))
        warn.setToolTip(1, "仅解码展示:没有密钥,无法判定令牌真伪")
        g = self._info_add("Header", "%d 个字段" % len(head))
        for k, v in head.items():
            self._info_add(str(k), v if isinstance(v, str)
                           else json.dumps(v, ensure_ascii=False), g)
        g = self._info_add("Payload", "%d 个字段" % len(pay))
        for k, v in pay.items():
            if isinstance(v, str):
                vs = v
            else:
                vs = json.dumps(v, ensure_ascii=False)
            self._info_add(str(k), vs, g)
        for label, key in (("签发时间 (iat)", "iat"), ("生效时间 (nbf)", "nbf"),
                           ("过期时间 (exp)", "exp")):
            ts = inf.get(key)
            if ts:
                self._info_add(label, time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(ts)))
        exp = inf.get("exp")
        if exp:
            left = exp - time.time()
            if left <= 0:
                it = self._info_add("状态", "已过期")
                it.setForeground(1, QColor("#e8493f"))
            else:
                hrs = left // 3600
                self._info_add("状态", "约 %s 后过期" % (
                    "%.1f 小时" % hrs if hrs >= 1 else "%d 分钟" % max(1, left // 60)))
        else:
            self._info_add("状态", "无 exp 字段(视为不过期)")

    def _show_b64_image(self, raw):
        img = QImage.fromData(raw)
        if img.isNull():
            self.img_view.setText("(解码出图像魔数,但无法解析像素)")
            self.img_view.setPixmap(QPixmap())
            return
        pm = QPixmap.fromImage(img)
        if pm.width() > 1600 or pm.height() > 1200:
            pm = pm.scaled(1600, 1200, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        self.img_view.setPixmap(pm)
        self.img_view.setToolTip("原始尺寸 %d × %d" % (img.width(), img.height()))

    def _info_menu(self, pos):
        it = self.info.itemAt(pos)
        if it is None:
            return
        val = str(it.data(1, Qt.UserRole) or "")
        field = it.text(0)
        m = RoundMenu(parent=self.info)
        a1 = QAction("复制「%s」" % field, m)
        a1.triggered.connect(lambda _=False, v=val: self.app.core.copy_text(v))
        m.addAction(a1)
        if val and len(val) > 160:
            a2 = QAction("复制完整值(%d 字符)" % len(val), m)
            a2.triggered.connect(lambda _=False, v=val: self.app.core.copy_text(v))
            m.addAction(a2)
        m.exec(self.info.viewport().mapToGlobal(pos))
        m.deleteLater()

    def _inspector_actions(self):
        """URL/JWT/Base64 的内容专属动作:INSPECTOR_ACTIONS 注册表按
        subtype 过滤(返回 [(标题, 回调)])。"""
        if self._lang_index != 0 or self._content is None:
            return []
        sub = self._content.subtype
        out = []
        for st, title, run, guard in INSPECTOR_ACTIONS:
            if st == sub and (guard is None or guard(self)):
                out.append((title, lambda r=run: r(self)))
        return out

    def _save_b64_image(self):
        kind, raw = self._b64 or (None, None)
        if not raw:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "解码图另存为", "clipboard_decoded.png", "PNG 图片 (*.png)")
        if not path:
            return
        try:
            Path(path).write_bytes(raw)
        except Exception:
            MessageBox("WinClipboard", "保存失败。", self).exec()


    # ---- 显示与渲染 ----
    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)
        self._place_find()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_find()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self._find.isVisible():
                self._close_find()
            else:
                self.close()
            return
        super().keyPressEvent(e)

    def _resolved_lang(self):
        key = cm.VIEW_LANGS[self._lang_index][0]
        return self._auto_lang if key == "auto" else key

    def _set_lang_index(self, i):
        if i != self._lang_index:
            self._lang_index = i
            # 语言/自动档切换必然换模式表:直接跳默认视图(URL/JWT 等
            # 智能视图的 lang 恒为 text,靠 _render 的语言变化检测捕捉不到)
            self._mode = self._mode_table()[0]
            self._render()

    def _set_lang_key(self, key):
        """按 VIEW_LANGS key 选语言(测试/内部调用入口)。"""
        self._set_lang_index([k for k, _ in cm.VIEW_LANGS].index(key))

    def _render(self):
        # 换格式(语言)时跳到该格式的默认视图;同一格式下保留用户当前视图
        lang = self._resolved_lang()
        if lang != getattr(self, "_shown_lang", None):
            self._mode = self._mode_table()[0]
            self._shown_lang = lang
        self._rebuild_modes()
        self._apply_view()

    def _refresh_meta(self):
        if self._kind == "files":
            base = "%d 个文件" % len(self.rec.get("content") or [])
        else:
            size = cm._fmt_size(len(self._raw.encode("utf-8")))
            lang = self._resolved_lang()
            if lang == "json":
                valid = "无效(%s)" % self._json_err if self._json_err else "有效"
                base = "已识别:JSON · %s · %d 行 · %s" % (
                    valid, self._raw.count("\n") + 1, size)
            elif lang == "csv" and self._csv_rows:
                nrow = len(self._csv_rows)
                shown = "≥%d" % self._TABLE_ROW_CAP \
                    if nrow > self._TABLE_ROW_CAP else str(nrow)
                ncol = max(len(r) for r in self._csv_rows)
                base = "已识别:CSV 表格 · %s 行 × %d 列 · %s" % (shown, ncol, size)
            elif self._lang_index == 0 and self._content is not None and \
                    self._content.subtype == "web_url" and self._url_inf:
                base = "已识别:URL · %s · %d 行 · %s" % (
                    self._url_inf["host"], self._raw.count("\n") + 1, size)
            elif self._lang_index == 0 and self._content is not None and \
                    self._content.subtype == "jwt" and self._jwt_inf:
                base = "已识别:JWT · 仅解码,签名未验证 · %s" % size
            elif self._lang_index == 0 and self._content is not None and \
                    self._content.subtype == "base64" and self._b64:
                base = "已识别:Base64 · 解码为%s(%s) · %s" % (
                    "图片" if self._b64[0] == "image" else "文本",
                    cm._fmt_size(len(self._b64[1])), size)
            else:
                base = "已识别:%s · %d 行 · %s" % (
                    self._lang_name(), self._raw.count("\n") + 1, size)
        ts = self.rec.get("ts")
        if ts:
            t = time.localtime(ts)
            now = time.localtime()
            base += " · " + (time.strftime("%H:%M", t) if
                             (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday)
                             else time.strftime("%m-%d %H:%M", t))
        self.lbl_meta.setText(base + self._meta_suffix)

    def _refresh_pos(self):
        if self._kind != "text":
            self.lbl_pos.setText(self._editor_pos_info())
            return
        prefix = self._pos_prefix + " · " + self._lang_name()
        lang = self._resolved_lang()
        if lang == "json":
            prefix += " · " + ("无效" if self._json_err else "有效")
        cur = self.stack.currentWidget()
        if cur is self.tree:
            info = "%d 个节点" % self._tree_nodes
        elif cur is self.table and self._csv_rows:
            info = "%d 行 × %d 列" % (
                min(len(self._csv_rows), self._TABLE_ROW_CAP + 1),
                max(len(r) for r in self._csv_rows))
        elif cur is self.md_view:
            info = "预览"
        elif cur is self.info:
            info = "%d 行" % self.info.topLevelItemCount()
        elif cur is self.img_view:
            info = "图像"
        else:
            info = self._editor_pos_info()
        self.lbl_pos.setText("%s · %s" % (prefix, info))

    def _editor_pos_info(self):
        c = self._active_edit().textCursor()
        if c.hasSelection():
            return "已选择 %d 个字符" % len(c.selectedText())
        return "Ln %d, Col %d" % (c.blockNumber() + 1, c.positionInBlock() + 1)

    def eventFilter(self, obj, e):
        if obj is self.ed_find and e.type() == QEvent.KeyPress \
                and e.key() == Qt.Key_Escape:
            self._close_find()
            return True
        if obj is self.editor_raw.viewport() \
                and e.type() == QEvent.MouseButtonDblClick:
            self._open_path_line(self.editor_raw.cursorForPosition(
                e.position().toPoint()).block().text())
            return True
        return super().eventFilter(obj, e)

    def _open_path_line(self, line):
        p = line.strip().strip('"')
        if p and os.path.exists(p):
            cm.open_file_default(p)

    def _lang_name(self):
        key = self._resolved_lang()
        return dict(cm.VIEW_LANGS).get(key, key)

    def _apply_highlight(self):
        """染色到当前文档:文档已按该 (lang, 长度) 染过就直接显示(0 成本);
        未染过才跑词法(25KB≈110ms)+ 落 runs(≈230ms),染完常驻文档。"""
        doc = self.editor.document()
        lang = self._resolved_lang()
        text = self.editor.toPlainText()
        key = (lang, len(text))
        if self._ed_hl_state == key:
            return                                  # 原文↔代码来回切不再重染
        runs = self._hl_cache.get(key)
        if runs is None:
            if lang != "text" and _pg_get_lexer is not None \
                    and len(text) > _HL_CHAR_LIMIT:
                self._act_hl.setVisible(True)       # 超长:菜单留强制入口
                self._meta_suffix = " · 文本过长,未高亮"
                self._refresh_meta()
                return
            runs = pygments_runs(text, lang) or []
            self._hl_cache[key] = runs
        self._ed_hl_state = key
        self._act_hl.setVisible(False)
        self._meta_suffix = ""
        apply_format_runs(doc, runs)                # 新灌的文本无需先清格式
        self._refresh_meta()

    def _force_highlight(self):
        """超长文本用户主动要求染色:接受一次性耗时(几百 KB 可达秒级)。"""
        self._act_hl.setVisible(False)
        text = self.editor.toPlainText()
        lang = self._resolved_lang()
        runs = pygments_runs(text, lang, force=True)
        if runs:
            self._hl_cache[(lang, len(text))] = runs
            self._ed_hl_state = (lang, len(text))
            apply_format_runs(self.editor.document(), runs)
            self._meta_suffix = " · 已高亮"
            self._refresh_meta()

    # ---- 更多菜单 ----
    def _show_more_menu(self):
        m = RoundMenu(parent=self)
        if self._kind == "text":
            sub = RoundMenu("语言", self)
            for i, a in enumerate(self._lang_acts):
                a.setIcon(FIF.ACCEPT.icon() if i == self._lang_index else QIcon())
                sub.addAction(a)
            m.addMenu(sub)
            if self._act_hl.isVisible():
                m.addSeparator()
                m.addAction(self._act_hl)
            insp = self._inspector_actions()
            if insp:
                m.addSeparator()
                for title, cb in insp:
                    a = QAction(title, m)
                    a.triggered.connect(lambda _=False, f=cb: f())
                    m.addAction(a)
        else:
            m.addAction(self._act_reveal)
        m.exec(self.btn_more.mapToGlobal(
            QPoint(self.btn_more.width() // 2, self.btn_more.height() + 8)))
        m.deleteLater()

    # ---- 查找 ----
    def _open_find(self):
        if self.stack.currentWidget() not in (self.editor, self.editor_raw):
            self._set_view("raw")                   # 查找定位在文本视图
        if self._find.isVisible():
            self.ed_find.setFocus()
            self.ed_find.selectAll()
            return
        self._find.show()
        self._find.raise_()
        self._place_find()
        self.ed_find.setFocus()
        self._find_text_changed(self.ed_find.text())

    def _close_find(self):
        self._find.hide()
        self._active_edit().setFocus()

    def _place_find(self):
        if not self._find.isVisible():
            return
        self._find.move(self.card.width() - self._find.width() - 12, 10)

    def _find_matches(self):
        """当前关键词的全部匹配起点(非重叠);超大文档放弃实时计数。"""
        text = self.ed_find.text()
        if not text:
            return None
        doc = self._active_edit().document()
        if doc.characterCount() > 300000:
            return None
        out = []
        c = doc.find(text, QTextCursor(doc))
        while not c.isNull():
            out.append(c.selectionStart())
            nxt = doc.find(text, c)
            if nxt.isNull() or nxt.position() <= c.position():
                break
            c = nxt
        return out

    def _find_text_changed(self, _):
        ms = self._find_matches()
        if ms is None:
            self.lbl_find_n.setText("-/-")
            return
        cur = self._active_edit().textCursor().position()
        idx = 0
        while idx < len(ms) and ms[idx] < cur:
            idx += 1
        self.lbl_find_n.setText("%d/%d" % (min(idx + 1, len(ms)) if ms else 0,
                                           len(ms)))

    def _find_goto(self, back=False):
        text = self.ed_find.text()
        if not text:
            return
        ed = self._active_edit()
        flags = (QTextDocument.FindFlag.FindBackward if back
                 else QTextDocument.FindFlag(0))
        if not ed.find(text, flags):                # 到头/尾回绕
            c = ed.textCursor()
            c.movePosition(QTextCursor.End if back else QTextCursor.Start)
            ed.setTextCursor(c)
            ed.find(text, flags)
        self._find_text_changed("")

    # ---- 工具 ----
    def _set_wrap(self, on):
        for ed in (self.editor, self.editor_raw):
            ed.setLineWrapMode(
                QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)

    def _apply_zoom(self, pct):
        pt = max(6, round(self._base_pt * pct / 100))
        for w in (self.editor, self.editor_raw, self.md_view,
                  self.tree, self.table, self.info):
            f = w.font()
            f.setPointSize(pt)
            w.setFont(f)
        self.editor._update_area_width(0)
        self.editor_raw._update_area_width(0)

    def _set_zoom(self, pct):
        self.cmb_zoom.setCurrentIndex(self._ZOOMS.index(pct))
        self._apply_zoom(pct)                       # 索引未变时信号不来,手动兜底

    def _zoom_step(self, d):
        i = self.cmb_zoom.currentIndex() + d
        self._set_zoom(self._ZOOMS[max(0, min(len(self._ZOOMS) - 1, i))])

    def _copy_all(self):
        self.app.core.paste_text(self._raw)         # 始终复制原始记录
        self.btn_copy.setText("已复制")             # 按钮自身反馈,不弹提示层
        self.btn_copy.setIcon(FIF.ACCEPT.icon())
        self._copy_timer.start()

    def _copy_restore(self):
        self.btn_copy.setText("复制")
        self.btn_copy.setIcon(FIF.COPY.icon())

    def closeEvent(self, e):
        try:
            self.app._viewers.remove(self)
        except ValueError:
            pass
        self.deleteLater()
        e.accept()


class ImageViewerWindow(QWidget):
    """图片查看器(独立窗口,与文本查看器同族):适应窗口 / 1:1 / 滚轮缩放 /
    拖拽平移;双击在 适应↔原始 间切换。「编辑标注」在本窗口内切换到内嵌
    编辑器(不另开窗口),完成/Esc 自动回到查看态。"""

    def __init__(self, app, rec, edit=False, on_result=None):
        super().__init__(None)
        self.app = app
        self.rec = rec
        self._on_result = on_result       # 编辑产物直通回调(图钉画布回写)
        self._editor = None               # 内嵌编辑器(编辑态非 None)
        path = str(rec.get("content") or "")
        pm = QPixmap(path)
        if pm.isNull():
            raise ValueError("无法加载图片: %s" % path)
        self._pm = pm

        self.setWindowTitle("图片查看 · WinClipboard")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(880, 620)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)
        self.v = v

        # ---- 头部:统计 + 缩放控制 + 动作 ----
        head = QHBoxLayout()
        head.setSpacing(6)
        self.lbl_title = StrongBodyLabel("图片查看")
        self.lbl_title.setFont(font(15, QFont.DemiBold))
        size_str = ""
        try:
            size_str = cm._fmt_size(os.path.getsize(path))
        except Exception:
            pass
        ext = os.path.splitext(path)[1].lstrip(".").upper() or "IMG"
        self.lbl_stats = CaptionLabel("%d × %d · %s · %s"
                                      % (pm.width(), pm.height(), size_str, ext))
        self.lbl_stats.setStyleSheet("color: %s;" % TER)
        self.lbl_zoom = CaptionLabel("适应")
        self.lbl_zoom.setStyleSheet("color: %s;" % SEC)
        self.b_out = PushButton("−")
        self.b_out.setFixedWidth(40)
        self.b_out.setToolTip("缩小(滚轮/加减号)")
        self.b_fit = PushButton("适应窗口")
        self.b_fit.setCheckable(True)
        self.b_fit.setChecked(True)
        self.b_fit.setToolTip("缩放到完整可见(快捷键 0)")
        self.b_100 = PushButton("1:1")
        self.b_100.setToolTip("原始像素(快捷键 1)")
        self.b_in = PushButton("+")
        self.b_in.setFixedWidth(40)
        self.b_in.setToolTip("放大(滚轮/加减号)")
        b_copy = PushButton("复制")
        b_copy.setIcon(FIF.COPY.icon())
        b_pin = PushButton("置顶")
        b_pin.setIcon(FIF.PIN.icon())
        b_pin.setToolTip("钉到屏幕置顶画布:可多图自由拖动 / 缩放 / 排列")
        b_pin.clicked.connect(self._pin)
        self.b_edit = PushButton("✏ 编辑标注")
        self.b_edit.setIcon(FIF.EDIT.icon())
        self.b_edit.setToolTip("在本窗口内标注(矩形/圆形/箭头/画笔/文字/马赛克),"
                               "完成后回到查看")
        b_close = PushButton("关闭")
        b_close.setIcon(FIF.CLOSE.icon())
        for w in (self.lbl_title, self.lbl_stats, self.lbl_zoom):
            head.addWidget(w)
        self.lbl_title.setStyleSheet("color: %s;" % TEXT)
        head.addSpacing(8)
        head.addStretch(1)
        for w in (self.b_out, self.b_fit, self.b_100, self.b_in,
                  b_copy, b_pin, self.b_edit, b_close):
            head.addWidget(w)
        v.addLayout(head)

        # ---- 画布 ----
        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setStyleSheet(
            "QGraphicsView { background: #26292F; border: none; border-radius: 10px; }")
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)   # 放大后拖拽平移
        self.scene = QGraphicsScene(self)
        self._pix = QGraphicsPixmapItem(pm)
        self._pix.setTransformationMode(Qt.SmoothTransformation)
        self.scene.addItem(self._pix)
        self.scene.setSceneRect(QRectF(0, 0, pm.width(), pm.height()))
        self.view.setScene(self.scene)
        v.addWidget(self.view, 1)

        self._fitting = True
        self.b_out.clicked.connect(lambda: self._zoom_by(0.8))
        self.b_in.clicked.connect(lambda: self._zoom_by(1.25))
        self.b_fit.clicked.connect(lambda on: self._do_fit() if on else self._do_100())
        self.b_100.clicked.connect(self._do_100)
        b_copy.clicked.connect(self._copy)
        self.b_edit.clicked.connect(self._toggle_edit)
        b_close.clicked.connect(self.close)
        self.view.wheelEvent = self._wheel
        self.view.mouseDoubleClickEvent = self._dbl
        if edit:
            self._enter_edit()

    # ---- 查看 ↔ 编辑(同窗切换,不另开窗口) ----
    def _toggle_edit(self):
        self._exit_edit() if self._editor is not None else self._enter_edit()

    def _enter_edit(self):
        if self._editor is not None:
            return
        ed = ImageEditor(self.app, {"type": "image", "content": self.rec["content"],
                                    "w": self._pm.width(), "h": self._pm.height()},
                         on_result=self._edited, parent=self)
        ed.finished.connect(self._exit_edit)
        self._editor = ed
        self.v.replaceWidget(self.view, ed)
        self.view.hide()          # replaceWidget 不隐藏旧控件,会叠加渲染
        ed.show()
        ed.setFocus()
        for w in (self.b_out, self.b_fit, self.b_100, self.b_in, self.lbl_zoom):
            w.hide()
        self.b_edit.setText("退出编辑")
        self.lbl_title.setText("图片编辑")

    def _exit_edit(self):
        ed = self._editor
        if ed is None:
            return
        self._editor = None
        try:
            ed.finished.disconnect(self._exit_edit)
        except Exception:
            pass
        self.v.replaceWidget(ed, self.view)
        ed.hide()                 # 同上:先藏再删,避免 deleteLater 前残留渲染
        ed.deleteLater()
        self.view.show()
        for w in (self.b_out, self.b_fit, self.b_100, self.b_in, self.lbl_zoom):
            w.show()
        self.b_edit.setText("✏ 编辑标注")
        self.lbl_title.setText("图片查看")
        self.view.setFocus()

    def _edited(self, result):
        """编辑产物直通(画布回写等);历史场景产物经剪贴板自动回流。"""
        if self._on_result is None:
            return
        try:
            self._on_result(result)
        except Exception:
            cm.log("查看器编辑结果回写失败:\n%s" % traceback.format_exc())

    # ---- 显示 / 缩放 ----
    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)
        QTimer.singleShot(0, self._do_fit)   # 需要最终窗口尺寸

    def _do_fit(self):
        self.view.resetTransform()
        self.view.fitInView(self._pix, Qt.KeepAspectRatio)
        if self.view.transform().m11() > 1.0:   # 小图不放大到模糊,封顶 100%
            self.view.resetTransform()
        self._fitting = True
        self.b_fit.setChecked(True)
        self._sync()

    def _do_100(self):
        self.view.resetTransform()
        self._fitting = False
        self.b_fit.setChecked(False)
        self._sync()

    def _zoom_by(self, f, anchor_mouse=False):
        cur = self.view.transform().m11()
        if not 0.02 <= cur * f <= 40:
            return
        self._fitting = False
        self.b_fit.setChecked(False)
        self.view.setTransformationAnchor(
            QGraphicsView.AnchorUnderMouse if anchor_mouse
            else QGraphicsView.AnchorViewCenter)
        self.view.scale(f, f)
        self._sync()

    def _wheel(self, e):
        self._zoom_by(1.25 if e.angleDelta().y() > 0 else 0.8, anchor_mouse=True)

    def _dbl(self, e):
        self._do_100() if self._fitting else self._do_fit()

    def _sync(self):
        s = self.view.transform().m11()
        self.lbl_zoom.setText("适应" if self._fitting else "%d%%" % round(s * 100))

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            if self._editor is not None:
                self._exit_edit()     # 编辑态:先退编辑,再按才关窗
            else:
                self.close()
        elif self._editor is not None:
            super().keyPressEvent(e)  # 编辑态让快捷键(Delete/Ctrl+Z)进编辑器
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom_by(1.25)
        elif k == Qt.Key_Minus:
            self._zoom_by(0.8)
        elif k == Qt.Key_0:
            self._do_fit()
        elif k == Qt.Key_1:
            self._do_100()
        else:
            super().keyPressEvent(e)

    # ---- 动作 ----
    def _copy(self):
        try:
            img = cm.Image.open(self.rec["content"])
            ok = cm.set_dib_to_clipboard(self.app.worker.hwnd, cm.pil_to_dib(img))
        except Exception:
            ok = False
        if ok:
            InfoBar.success("已复制", "图片已复制到剪贴板", parent=self,
                            position=InfoBarPosition.BOTTOM_LEFT)
        else:
            InfoBar.error("复制失败", "剪贴板被占用", parent=self,
                          position=InfoBarPosition.BOTTOM_LEFT)

    def _pin(self):
        # 钉到置顶画布(预览替身没有 pin_image,静默跳过)
        f = getattr(self.app, "pin_image", None)
        if f is not None:
            f(self.rec)

    def closeEvent(self, e):
        try:
            self.app._viewers.remove(self)
        except ValueError:
            pass
        self.deleteLater()
        e.accept()


# ============================================================
# AI 工作流(多模型协作 · 人在回路)
# ============================================================
class UiHealthMonitor:
    """UI 事件循环健康监控(Mica 假死自愈)。

    个别远程会话(网易UU远程等未置 RDP 标志的工具)上 Mica 会让 Qt 事件
    循环假死——窗口在、定时器与消息全部不跑。启动后 HEARTBEAT_MS 内事件
    循环必须打卡(证明派发器活着),否则无 Mica 自重启:仅本次会话、不改
    配置(本地桌面下次照常启用),WCLB_NO_MICA 环境变量防重启循环。

    触发时把判决时长 / 远程会话 / Mica 状态写入日志——这个 workaround
    到底多久救一次场有据可查,不会变成「不敢删但不知道有没有用」的遗物。
    """

    HEARTBEAT_MS = 2500          # 事件循环须在此前打卡
    VERDICT_S = 6.0              # 看门狗判决时限(须大于打卡时限)

    def __init__(self, app):
        self.app = app
        self.beat = None          # None=未打卡;打卡时刻(monotonic)
        self._armed = False

    def arm(self):
        """布防(初始化尾部调用一次,幂等);仅 Mica 生效且未设禁用环境变量时。"""
        if self._armed:
            return
        self._armed = True
        if not getattr(self.app, "_mica_active", False) \
                or os.environ.get("WCLB_NO_MICA"):
            return
        t0 = time.monotonic()
        QTimer.singleShot(self.HEARTBEAT_MS, self._heartbeat)
        threading.Thread(target=self._watch, args=(t0,), daemon=True).start()

    def _heartbeat(self):
        self.beat = time.monotonic()

    def _watch(self, t0):
        time.sleep(self.VERDICT_S)
        if self.beat is not None:
            return
        cm.log("UI 健康监控:事件循环 %.1fs 无心跳,疑似 Mica 致假死,无 Mica 自重启"
               "(elapsed=%.1fs remote_session=%s mica=%s)"
               % (self.VERDICT_S, time.monotonic() - t0,
                  cm.is_remote_session(),
                  bool(getattr(self.app, "_mica_active", False))))
        try:
            subprocess.Popen(
                [sys.executable, os.path.abspath(sys.argv[0])],
                env=dict(os.environ, WCLB_NO_MICA="1"),
                close_fds=False)
        except Exception:
            cm.log("看门狗重启失败:\n%s" % traceback.format_exc())
            return
        os._exit(1)


class ThumbLoader(QObject):
    """后台生成缩略图:QImage 的加载/缩放线程安全,完成后经信号投回 GUI 线程
    转 QPixmap 并回填卡片。截图解码+平滑缩放单张可达几十毫秒,放 UI 线程会在
    启动预热全量截图时把窗口冻住(30 张 ≈ 1.5 秒)。"""

    loaded = Signal(str, QImage)

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._q = queue.Queue()
        self._pending = set()
        self.loaded.connect(self._on_loaded)
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, path):
        if path and path not in self._pending and path not in self.app._thumbs:
            self._pending.add(path)
            self._q.put(path)

    def _run(self):
        while True:
            path = self._q.get()
            try:
                img = QImage(path)          # 线程安全;失败得到 null 图
                if not img.isNull():
                    w, h = Card.THUMB
                    self.loaded.emit(path, img.scaled(
                        w * 2, h * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            except Exception:
                pass
            finally:
                self._pending.discard(path)

    def _on_loaded(self, path, img):
        pm = QPixmap.fromImage(img)
        self.app._thumbs[path] = pm
        for c in self.app._card_widgets:
            if c.rec.get("type") == "image" and c.rec.get("content") == path:
                c.set_thumb(pm)


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


class ModelWorker(QThread):
    """后台拉取端点模型清单(GET /models,OpenAI 标准发现端点)。"""
    done = Signal(bool, object)     # ok, [模型 id] 或 错误文本

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self):
        ok, payload = cm.list_models(self.service)
        self.done.emit(ok, payload)


class ServiceEditorDialog(MessageBoxBase):
    """API 端点配置表单(hermes 风格):Name / Provider ID / Endpoint URL /
    Default Model(可「发现模型」自动拉取)/ API 密钥(可留空)+ ⚡测试。

    聚合引擎见 cm.ai_chat:openai SDK 统一客户端(OpenAI 兼容端点通吃,
    含 Ollama 与 one-api 类本地网关),SDK 缺席时退回零依赖 urllib。
    provider 字段为路由前缀预留(anthropic/gemini 原生协议),OpenAI 兼容
    端点保持 openai 即可。
    """

    # 常用 litellm 风格路由前缀(可自由输入)
    _PIDS = ("openai", "deepseek", "anthropic", "gemini", "groq", "mistral", "ollama", "azure")

    def __init__(self, parent, svc=None, preset=None):
        super().__init__(parent)
        self.svc = dict(svc) if svc else dict(preset or {})
        self.result = None
        self._presets = {p["name"]: p for p in cm.AI_PRESET_SERVICES}
        self._model_worker = None
        self._chat_worker = None

        self.titleLabel = StrongBodyLabel("API 端点")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("OpenAI 兼容接口;「发现模型」自动拉取端点的模型清单(本地网关 / Ollama 同样适用)。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        # ---- 供应商(预设一键预填;自定义则手填全部字段) ----
        self.cmb_provider = ComboBox()
        self.cmb_provider.addItems([p["name"] for p in cm.AI_PRESET_SERVICES] + ["自定义"])
        init_name = str(self.svc.get("name") or "")
        self.cmb_provider.setCurrentText(init_name if init_name in self._presets else "自定义")
        self.cmb_provider.setMinimumWidth(220)
        self.cmb_provider.currentTextChanged.connect(self._on_provider)
        self._row("供应商", self.cmb_provider)

        # ---- 名称(仅自定义,hermes: Name) ----
        self.e_name = LineEdit()
        self.e_name.setPlaceholderText("如 公司中转 / Axet Proxy")
        self.e_name.setText("" if init_name in self._presets else init_name)
        self.row_name, _lbl = self._row("名称", self.e_name)

        # ---- Provider ID(hermes 同名字段:路由前缀,openai=OpenAI 兼容直连) ----
        self.cmb_pid = EditableComboBox()
        self.cmb_pid.addItems(list(self._PIDS))
        self.cmb_pid.setText(str(self.svc.get("provider") or "openai"))
        self.cmb_pid.setMinimumWidth(160)
        self.cmb_pid.setToolTip("openai = 按 OpenAI 兼容协议直连接口地址;"
                                "其余为原生协议路由前缀预留")
        self._row("Provider ID", self.cmb_pid)

        # ---- 接口地址(hermes: Endpoint URL,主表单位置) ----
        self.e_url = LineEdit()
        self.e_url.setPlaceholderText("http://127.0.0.1:8081/v1(自定义必填)")
        self._row("接口地址", self.e_url)

        # ---- 默认模型 + 发现模型(hermes: Default Model / Discover models) ----
        self.cmb_model = EditableComboBox()
        self.cmb_model.setMinimumWidth(180)
        self.cmb_model.currentTextChanged.connect(self._update_model_note)
        self.btn_discover = PushButton("发现模型", self)
        self.btn_discover.setIcon(FIF.SYNC.icon())
        self.btn_discover.setFixedHeight(28)
        self.btn_discover.setCursor(Qt.PointingHandCursor)
        self.btn_discover.setToolTip("从接口地址拉取可用模型清单(GET /models)")
        self.btn_discover.clicked.connect(self._discover)
        mrow = QWidget()
        mh = QHBoxLayout(mrow)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.setSpacing(8)
        mh.addWidget(self.cmb_model, 1)
        mh.addWidget(self.btn_discover, 0)
        self._row("默认模型", mrow)
        self.lbl_model_note = CaptionLabel("")
        self.lbl_model_note.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(self.lbl_model_note)

        # ---- API 密钥(可留空:本地网关 / Ollama / 网页 AI 优先) ----
        self.e_key = LineEdit()
        self.e_key.setEchoMode(QLineEdit.Password)
        self.e_key.setText(str(self.svc.get("api_key") or ""))
        self._kw, self.lbl_key = self._row("API 密钥", self.e_key)
        key_note = CaptionLabel("可留空;保存于本机(DPAPI 加密),只用于发起 API 请求")
        key_note.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(key_note)

        # ---- 按钮区:⚡测试(hermes: Test)+ 取消 / 保存 ----
        self.btn_test = PushButton("⚡ 测试", self)
        self.btn_test.setFixedHeight(28)
        self.btn_test.setCursor(Qt.PointingHandCursor)
        self.btn_test.clicked.connect(self._test_now)
        self.buttonLayout.insertWidget(0, self.btn_test)
        self.buttonLayout.insertStretch(1)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(500)

        # 按当前供应商填充,再把已有配置覆盖进去
        self._on_provider(self.cmb_provider.currentText())
        if self.svc:
            self.e_url.setText(str(self.svc.get("base_url") or ""))
            if self.svc.get("model"):
                self.cmb_model.setText(str(self.svc["model"]))
            self._update_model_note()

    # ---- 行辅助:返回 (行容器, 标签),容器可整体显隐,标签可改文字 ----
    def _row(self, label_text, field):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        lab = BodyLabel(label_text)
        lab.setStyleSheet("color: %s;" % SEC)
        lab.setMinimumWidth(72)
        h.addWidget(lab, 0)
        h.addWidget(field, 1)
        self.viewLayout.addWidget(w)
        return w, lab

    def _on_provider(self, name):
        p = self._presets.get(name)
        self.row_name.setVisible(name == "自定义")
        self.lbl_key.setText("%s API 密钥" % name if p else "API 密钥")
        self.cmb_model.clear()
        if p:
            self.e_url.setText(p["base_url"])
            self.cmb_pid.setText("openai")   # 预设均为 OpenAI 兼容端点
            self.cmb_model.addItems([m["id"] for m in p.get("models") or []])
            self.cmb_model.setCurrentIndex(0)
        else:
            self.cmb_model.setText("")   # EditableComboBox 的 setCurrentText 只认列表内文本
        self._update_model_note()

    def _update_model_note(self, *_):
        cur = self.cmb_model.currentText().strip()
        p = self._presets.get(self.cmb_provider.currentText())
        note = ""
        if p:
            for m in p.get("models") or []:
                if m["id"] == cur:
                    note = m["note"]
                    break
        self.lbl_model_note.setText(note)
        self.lbl_model_note.setVisible(bool(note))

    # ---- 发现模型(后台 GET /models) ----
    def _discover(self):
        if self._model_worker is not None:
            return
        base = self.e_url.text().strip()
        if not base:
            InfoBar.warning("缺少接口地址", "先填写接口地址,再发现模型", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("发现中…")
        self._model_worker = ModelWorker(
            {"base_url": base, "api_key": self.e_key.text().strip()}, self)

        def on_done(ok, payload):
            self.btn_discover.setEnabled(True)
            self.btn_discover.setText("发现模型")
            self._model_worker = None
            if not ok:
                InfoBar.error("发现失败", str(payload)[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)
                return
            self._apply_models(payload)
            InfoBar.success("发现完成", "从该端点拉取到 %d 个模型" % len(payload),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)

        self._model_worker.done.connect(on_done)
        self._model_worker.start()

    def _apply_models(self, models):
        """把发现到的模型清单填进下拉(保留已输入的模型名,若有)。"""
        cur = self.cmb_model.currentText().strip()
        self.cmb_model.clear()
        self.cmb_model.addItems(models)
        if cur in models:
            self.cmb_model.setCurrentText(cur)
        else:
            self.cmb_model.setText(cur or (models[0] if models else ""))
        self._update_model_note()

    # ---- ⚡ 测试(不关对话框;后台真实调用当前表单配置) ----
    def _temp_service(self):
        return {"name": "test", "base_url": self.e_url.text().strip(),
                "model": self.cmb_model.currentText().strip(),
                "api_key": self.e_key.text().strip(),
                "provider": self.cmb_pid.currentText().strip() or "openai"}

    def _test_now(self):
        if self._chat_worker is not None:
            return
        svc = self._temp_service()
        if not svc["base_url"] or not svc["model"]:
            InfoBar.warning("无法测试", "先填写 接口地址 和 模型 再测试", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中…")
        self._chat_worker = AIWorker(svc, "请只回复两个字母:OK", -1, self)

        def on_done(_idx, ok, text):
            self.btn_test.setEnabled(True)
            self.btn_test.setText("⚡ 测试")
            self._chat_worker = None
            if ok:
                InfoBar.success("连接成功", "模型回复:%s" % text[:60], parent=self,
                                position=InfoBarPosition.TOP_RIGHT)
            else:
                InfoBar.error("连接失败", text[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)

        self._chat_worker.done.connect(on_done)
        self._chat_worker.start()

    def validate(self):
        provider = self.cmb_provider.currentText()
        model = self.cmb_model.currentText().strip()
        base = self.e_url.text().strip()
        if provider == "自定义":
            name = self.e_name.text().strip()
            if not name or not base or not model:
                InfoBar.error("无法保存", "自定义供应商需填写 名称 / 接口地址 / 模型",
                              parent=self, position=InfoBarPosition.TOP_RIGHT)
                return False
        else:
            name = provider
            if not model:
                InfoBar.error("无法保存", "请选择或输入模型", parent=self,
                              position=InfoBarPosition.TOP_RIGHT)
                return False
            if not base:
                base = self._presets[provider]["base_url"]
        self.result = {"name": name, "base_url": base, "model": model,
                       "api_key": self.e_key.text().strip(),
                       "provider": self.cmb_pid.currentText().strip() or "openai"}
        return True


class ProviderCard(SimpleCardWidget):
    """AI 供应商卡片(OpenCode 风格):状态点 + 名称/模型/接口 + 编辑/测试/删除。"""

    def __init__(self, page, svc):
        super().__init__()
        self.svc = svc
        self.setBorderRadius(8)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(14)

        # 状态点:绿 = 已填密钥可用,灰 = 未配置(语义色,不随主题派生)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet(
            "background: %s; border-radius: 4px;"
            % ("#3fb950" if svc.get("api_key") else NAV_BG))
        h.addWidget(self.dot, 0, Qt.AlignVCenter)

        left = QVBoxLayout()
        left.setSpacing(3)
        name = StrongBodyLabel(str(svc.get("name") or ""))
        name.setFont(font(14, QFont.DemiBold))
        sub = BodyLabel("%s · %s%s" % (
            svc.get("model"), svc.get("base_url"),
            "" if svc.get("api_key") else " · 未配置密钥"))
        sub.setFont(font(12))
        # 长 base_url 必须换行:否则卡片最小宽度被撑过视口,右侧按钮被裁掉
        sub.setWordWrap(True)
        left.addWidget(name)
        left.addWidget(sub)
        h.addLayout(left, 1)

        self.btn_test = PushButton("测试", self)
        self.btn_test.setIcon(FIF.SEND.icon())
        self.btn_edit = PushButton("编辑", self)
        self.btn_edit.setIcon(FIF.EDIT.icon())
        self.btn_del = PushButton("删除", self)
        self.btn_del.setIcon(FIF.DELETE.icon())
        for b in (self.btn_test, self.btn_edit, self.btn_del):
            b.setFixedHeight(30)
            b.setCursor(Qt.PointingHandCursor)
            h.addWidget(b)
        self.btn_test.clicked.connect(lambda: page.test_service(svc, self))
        self.btn_edit.clicked.connect(lambda: page.edit_service(svc))
        self.btn_del.clicked.connect(lambda: page.del_service(svc))


class ServicesPage(QWidget):
    """AI 供应商管理(OpenCode 风格):供应商卡片 + 预设/自定义两种添加入口。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("servicesPage")
        self._test_worker = None
        self.cards = []

        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(TitleLabel("AI 服务"))
        head.addStretch(1)
        b_cfg = PushButton("打开配置文件", self)
        b_cfg.setIcon(FIF.DOCUMENT.icon())
        b_cfg.clicked.connect(self._open_config)
        head.addWidget(b_cfg)
        v.addLayout(head)

        hint = CaptionLabel("工作流每一步可指定不同服务,实现多模型协作。"
                            "「添加提供方」从常用供应商一键预填;密钥经 DPAPI 加密保存在本机。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(content)
        self.cards_layout.setContentsMargins(2, 4, 2, 4)
        self.cards_layout.setSpacing(10)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(content)
        v.addWidget(self.scroll, 1)

        self._empty_hint = CaptionLabel(
            "还没有 AI 服务。点下方「添加提供方」选择常用供应商,或添加自定义 OpenAI 兼容接口。")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setStyleSheet("color: %s;" % TER)
        self.cards_layout.addWidget(self._empty_hint)

        # 底部双入口(OpenCode 同款):预设清单 / 自定义表单
        addrow = QHBoxLayout()
        addrow.setSpacing(12)
        self.b_add_preset = PushButton("添加提供方", self)
        self.b_add_preset.setIcon(FIF.ADD.icon())
        self.b_add_preset.setCursor(Qt.PointingHandCursor)
        self.b_add_preset.clicked.connect(self._add_preset)
        self.b_add_custom = PushButton("添加自定义提供方", self)
        self.b_add_custom.setIcon(FIF.ADD.icon())
        self.b_add_custom.setCursor(Qt.PointingHandCursor)
        self.b_add_custom.clicked.connect(lambda: self._edit(None))
        for b in (self.b_add_preset, self.b_add_custom):
            b.setFixedHeight(34)
            addrow.addWidget(b, 1)
        v.addLayout(addrow)

        self.refresh()

    def refresh(self):
        for c in self.cards:
            self.cards_layout.removeWidget(c)
            c.deleteLater()
        self.cards = []
        svcs = self.app.cfg.get("ai_services") or []
        self._empty_hint.setVisible(not svcs)
        for s in svcs:
            c = ProviderCard(self, s)
            self.cards_layout.addWidget(c)
            self.cards.append(c)

    # ---- 添加 ----
    def _add_preset(self):
        """从常用供应商预设清单选择并预填(密钥留空,保存前填写)。"""
        menu = RoundMenu(parent=self)
        for p in cm.AI_PRESET_SERVICES:
            # RoundMenu.addAction 只收 QAction(传 str 会在渲染时崩溃)
            a = QAction("%s · %s" % (p["name"], p["model"]), menu)
            a.triggered.connect(lambda _=False, p=p: self._add_from_preset(p))
            menu.addAction(a)
        menu.exec(self.b_add_preset.mapToGlobal(
            QPoint(0, self.b_add_preset.height())))

    def _add_from_preset(self, preset):
        svcs = self.app.cfg.setdefault("ai_services", [])
        if any(x.get("name") == preset["name"] for x in svcs):
            InfoBar.error("已存在", "服务「%s」已在列表中,可直接编辑填写密钥" % preset["name"],
                          parent=self, position=InfoBarPosition.TOP_RIGHT)
            return
        self._edit(None, preset=preset)

    def _open_config(self):
        try:
            os.startfile(cm.CONFIG_FILE)
        except Exception:
            InfoBar.error("打开失败", "配置文件不存在或无法打开:\n%s" % cm.CONFIG_FILE,
                          parent=self, position=InfoBarPosition.TOP_RIGHT)

    # ---- 编辑 / 删除 / 测试 ----
    def edit_service(self, svc):
        self._edit(svc)

    def del_service(self, svc):
        if not MessageBox("删除服务", "确定删除服务「%s」?" % svc.get("name"), self).exec():
            return
        try:
            self.app.cfg.get("ai_services", []).remove(svc)
        except ValueError:
            pass
        self._save()

    def test_service(self, svc, card):
        if self._test_worker is not None:
            return

        def set_test_btn(text, enabled):
            try:                    # 测试期间卡片可能已被刷新销毁
                card.btn_test.setText(text)
                card.btn_test.setEnabled(enabled)
            except RuntimeError:
                pass

        set_test_btn("测试中…", False)
        self._test_worker = AIWorker(svc, "请只回复两个字母:OK", -1, self)

        def on_done(_idx, ok, text):
            set_test_btn("测试", True)
            self._test_worker = None
            if ok:
                InfoBar.success("连接成功", "「%s」回复:%s" % (svc.get("name"), text[:60]),
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
            else:
                InfoBar.error("连接失败", text[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)

        self._test_worker.done.connect(on_done)
        self._test_worker.start()

    def _save(self):
        cm.save_config(self.app.cfg)
        self.refresh()

    def _edit(self, svc, preset=None):
        dlg = ServiceEditorDialog(self, svc, preset)
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
        v.setContentsMargins(36, 24, 36, 16)
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


class WorkflowPage(QWidget):
    """工作流管理(常驻单例):列表 + 新建/编辑/删除 + 填入示例 + 打开服务管理。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("workflowPage")

        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)
        ttl = TitleLabel("工作流")
        v.addWidget(ttl)
        hint = CaptionLabel("右键剪贴板页的文本条目 → 「工作流」即可运行。步骤可以是网页 AI(提示词进剪贴板、"
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
        b_new.clicked.connect(self._new)
        b_edit.clicked.connect(self._edit_current)
        b_del.clicked.connect(self._del_current)
        b_seed.clicked.connect(self._seed)
        b_svc.clicked.connect(self.app.show_services)
        for b in (b_new, b_edit, b_del, b_seed, b_svc):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._editors = []
        self.refresh()


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
        v.setContentsMargins(36, 24, 36, 16)
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
# 动作注册表(Action Registry):卡片右键菜单与查看器专属动作的数据源。
# 新增内容动作 = 在这里登记一条,不再往 card_menu/_inspector_actions
# 里堆 if/elif;supported 谓词决定该条目何时出现。
# ============================================================
class ClipAction:
    """卡片动作:supported(rec) 谓词 + run(rec, host) 执行;title 可为
    str 或 callable(rec, host)→str(如「固定/取消固定」随状态变)。"""
    __slots__ = ("title", "run", "supported")

    def __init__(self, title, run, supported=None):
        self.title = title
        self.run = run
        self.supported = supported or (lambda rec: True)


class WorkflowSlot:
    """占位:卡片菜单构建到此处时插入「工作流」子菜单(条目来自用户
    配置,非固定动作,故不进注册表本体)。"""


CLIP_ACTIONS = [
    ClipAction("粘贴", lambda r, h: h.paste_selected()),
    ClipAction("在独立窗口查看",
               lambda r, h: h.open_image_viewer(r) if r["type"] == "image"
               else h.open_viewer(r)),
    ClipAction("在浏览器打开", lambda r, h: h.open_link(r),
               supported=lambda r: r["type"] == "text"
               and cm.url_info(r["content"] or "")),
    ClipAction("打开文件", lambda r, h: h.open_files(r),
               supported=lambda r: r["type"] == "files"),
    ClipAction("在资源管理器中显示", lambda r, h: h.reveal_first(r),
               supported=lambda r: r["type"] == "files"),
    ClipAction("📌 置顶到屏幕", lambda r, h: h.pin_image(r),
               supported=lambda r: r["type"] == "image"),
    ClipAction(lambda r, h: "图片处理…" if not cm.find_sharex()
               else "图片处理(ShareX)…",
               lambda r, h: h.edit_image(r) if cm.find_sharex()
               else h.open_editor(r),
               supported=lambda r: r["type"] == "image"),
    ClipAction("内置编辑器…", lambda r, h: h.open_editor(r),
               supported=lambda r: r["type"] == "image" and cm.find_sharex()),
    WorkflowSlot(),
    ClipAction(lambda r, h: "取消固定" if r.get("pinned") else "固定",
               lambda r, h: h.toggle_pin()),
    ClipAction("删除", lambda r, h: h.delete_selected()),
]


# 查看器内容专属动作:(subtype, 标题, run(viewer), 额外门槛(viewer)→bool|None)
INSPECTOR_ACTIONS = [
    ("web_url", "在浏览器打开", lambda v: cm.open_url(v._raw.strip()), None),
    ("web_url", "复制主机名",
     lambda v: v.app.core.copy_text(v._url_inf["host"]), None),
    ("web_url", "复制路径",
     lambda v: v.app.core.copy_text(v._url_inf["path"]), None),
    ("web_url", "复制参数串",
     lambda v: v.app.core.copy_text(urlsplit(v._raw.strip()).query),
     lambda v: bool(v._url_inf["query"])),
    ("web_url", "URL 解码并复制",
     lambda v: v.app.core.copy_text(_unquote(v._raw.strip())), None),
    ("web_url", "URL 编码并复制",
     lambda v: v.app.core.copy_text(_quote(v._raw.strip(), safe=":/?&=#%+")), None),
    ("jwt", "复制 Header (JSON)",
     lambda v: v.app.core.copy_text(
         json.dumps(v._jwt_inf["header"], ensure_ascii=False, indent=2)), None),
    ("jwt", "复制 Payload (JSON)",
     lambda v: v.app.core.copy_text(
         json.dumps(v._jwt_inf["payload"], ensure_ascii=False, indent=2)), None),
    ("jwt", "复制解码全文 (Header.Payload)",
     lambda v: v.app.core.copy_text(
         "%s.%s" % (json.dumps(v._jwt_inf["header"], ensure_ascii=False),
                    json.dumps(v._jwt_inf["payload"], ensure_ascii=False))), None),
    ("base64", "解码图另存为…", lambda v: v._save_b64_image(),
     lambda v: v._b64 and v._b64[0] == "image"),
    ("base64", "复制解码文本",
     lambda v: v.app.core.copy_text(v._b64[1].decode("utf-8", "replace")),
     lambda v: v._b64 and v._b64[0] == "text"),
]


# ============================================================
# 应用主体(后端逻辑不变)
# ============================================================
class _EventBridge(QObject):
    """后台线程 → GUI 线程的事件唤醒桥。

    数据仍走 core.signal_q(queue),本类只提供一个无参 Signal:后台线程
    emit 一次,Qt(队列连接)就在主线程事件循环里排一次空。取代旧的
    30ms QTimer 轮询——常驻应用不必每秒醒 33 次。
    """
    wakeup = Signal()


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
        面板可见时合并刷新(防抖),避免连续捕获反复重建列表;
        隐藏期间只置脏标记,等下次显示窗口时刷新。"""
        self._view_dirty = True
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
        self._view_dirty = True            # 隐藏期间历史有变则置位,显示时才重建

        cm.ensure_dirs()
        if not os.path.exists(cm.ICON_FILE):
            try:
                cm.make_icon(cm.ICON_FILE)
            except Exception:
                pass
        self.icon = QIcon(cm.ICON_FILE) if os.path.exists(cm.ICON_FILE) else QIcon()

        # 共享核心(状态加载 + 历史迁移 + 后台线程启动)
        self.core = cm.ClipboardCore()

        self.win = MainWindow(self)
        self.panel = self.win.history_page     # 既有内部引用的稳定别名
        self._ai_runners = []
        self._viewers = []

        # 图钉画布(置顶悬浮多图板):恢复上次钉住的图片与布局;
        # 独立于主窗口存在,程序退出才销毁
        self.pinboard = PinBoardManager(self)

        # 定时器必须先于启动事件排空创建。ClipboardCore 构造时已经启动
        # 托盘/剪贴板线程,启动捕获可能在事件桥接通前积压 change 信号；
        # 若先 _drain_events(),可在 _on_history_change() 里访问到尚不存在的
        # _view_timer,造成真实冷启动偶发丢失首次刷新/热键响应。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self.refresh_view)
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(180)
        self._view_timer.timeout.connect(self.refresh_view)

        # 后台事件桥:queue 传数据,Qt Signal 做唤醒(见 _EventBridge)。
        # 连接必须显式队列化——主线程内 emit(add_item → change)时直接
        # 调用会在排空过程中重入;核心线程先于本桥启动,接好后先排空一次
        # 积压(如启动期投递的 ctt_hotkey_state / 启动捕获)
        self.event_bridge = _EventBridge()
        self.event_bridge.wakeup.connect(self._drain_events,
                                         Qt.ConnectionType.QueuedConnection)
        self.core.on_event = self.event_bridge.wakeup.emit
        self._drain_events()
        # 首帧探针:挂在必绘的内容页上(见 eventFilter)
        self.panel.installEventFilter(self)

        # 历史落盘防抖:变更只置脏,静默 1.5s 后统一写盘一次
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1500)
        self._save_timer.timeout.connect(self.core.flush_history)
        self.core.on_dirty = self._save_timer.start

        # 缩略图后台生成器(启动预热全量截图不再阻塞 UI)
        self._thumb_loader = ThumbLoader(self)
        QTimer.singleShot(600, self._prewarm)
        # 孤儿图片清理:启动收尾全部完成后再做(不占启动关键路径,
        # UI 线程执行 + 方法内的新文件时间窗防护,与捕获线程无竞态)
        QTimer.singleShot(3000, self._cleanup_orphan_images)

        # UI 事件循环健康监控:Mica 在个别远程会话上致派发器假死时
        # 无 Mica 自重启(机制与触发日志见 UiHealthMonitor)
        self.health = UiHealthMonitor(self)
        self.health.arm()

    # ---- 剪贴板读取 / 历史维护:见 cm.ClipboardCore(共享后端) ----

    # ---- 视图 ----
    def refresh_view(self):
        t_all = stamp()
        self._view_dirty = False
        with PerfSpan("history_filter"):
            view = self.core.filter_view(self.category, self.query)
        self._view = view
        with PerfSpan("history_cards_render"):
            self.panel.set_view(view, self.history)
            self.panel._resize_to_content()   # 卡片数/展开态变化后面板高度自适应
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
            self.panel.update_preview(None)
        since(t_all, "list_refresh")
        # 搜索 → UI 更新全程(含 120ms 防抖;目标 <150ms)
        ts = getattr(self, "_search_ts", None)
        if ts is not None:
            self._search_ts = None
            since(ts, "search_to_ui_update")

    def on_search(self, text):
        self.query = text
        self._search_ts = stamp()
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
            MessageBox("WinClipboard", "文件已不存在,无法粘贴。", self.win).exec()
            return
        self.win.hide()                        # 粘贴完成即收起,回到此前的工作现场

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
        """右键菜单:条目全部来自 CLIP_ACTIONS 注册表(supported 谓词过滤),
        工作流子菜单在 WorkflowSlot 占位处按配置插入。"""
        rec = card.rec
        self.select_card(card)

        def act(menu, text):
            # qfluentwidgets RoundMenu.addAction 只接受 QAction;传 str 会把原始
            # 字符串塞进内部 _actions,首个条目渲染时 'str' object has no
            # attribute 'icon' 崩溃(qfluentwidgets 1.11.3 实测)
            a = QAction(text, menu)
            menu.addAction(a)
            return a

        menu = RoundMenu(parent=self.panel)
        for entry in CLIP_ACTIONS:
            if isinstance(entry, WorkflowSlot):
                if rec["type"] != "text":
                    continue
                wfs = self.cfg.get("ai_workflows") or []
                if wfs:
                    sub = RoundMenu("工作流", self.panel)
                    for wf in wfs:
                        a = act(sub, wf.get("name", "未命名"))
                        a.triggered.connect(
                            lambda _=False, w=wf: self.run_workflow(w, rec))
                    sub.addSeparator()
                    am = act(sub, "管理工作流…")
                    am.triggered.connect(self.show_ai_hub)
                    menu.addMenu(sub)
                else:
                    a = act(menu, "工作流(先配置)…")
                    a.triggered.connect(self.show_ai_hub)
                continue
            if not entry.supported(rec):
                continue
            title = entry.title(rec, self) if callable(entry.title) else entry.title
            if title == "删除":
                menu.addSeparator()
            a = act(menu, title)
            a.triggered.connect(
                lambda _=False, r=rec, f=entry.run: f(r, self))
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

    def open_editor(self, rec, on_result=None):
        """图片编辑入口(内置):查看器内嵌编辑模式——单窗口看图+标注,
        不再另开编辑器窗口。"""
        if not os.path.exists(rec.get("content", "")):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。", self.panel).exec()
            return
        self.open_image_viewer(rec, edit=True, on_result=on_result)

    def edit_image(self, rec):
        """图片编辑统一入口:优先成熟外部工具 ShareX → ksnip(编辑结果经
        剪贴板自动回流历史),都没装/调起失败时回退内置编辑器。"""
        if not os.path.exists(rec.get("content", "")):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。", self.panel).exec()
            return
        if cm.find_sharex() and cm.open_in_sharex(rec["content"]):
            InfoBar.success("已在 ShareX 中打开",
                            "编辑并复制/保存后,结果会自动进入剪贴板历史",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        if cm.find_ksnip() and cm.open_in_ksnip(rec["content"]):
            InfoBar.success("已在 ksnip 中打开",
                            "编辑并复制/保存后,结果会自动进入剪贴板历史",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        self.open_editor(rec)

    def open_viewer(self, rec):
        """打开内容查看器(长内容的独立工具窗口)。"""
        try:
            v = ViewerWindow(self, rec)
            _pop_to_front(v)
            self._viewers.append(v)
            return v
        except Exception:
            cm.log("打开查看器失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "查看器打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()
            return None

    def open_link(self, rec):
        """链接条目:默认浏览器打开(非链接文本退回内容查看器)。"""
        info = cm.url_info(rec.get("content") or "")
        if not info:
            self.open_viewer(rec)
            return
        if cm.open_url(info[1]):
            InfoBar.success("已在浏览器打开", info[1][:80], parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.error("打开失败", info[1][:120], parent=self.win,
                          position=InfoBarPosition.TOP_RIGHT)

    def open_files(self, rec):
        """文件条目查看:系统默认程序打开(同资源管理器双击;上限 10 个防爆开)。
        文件全部不存在时退回路径清单窗口。"""
        paths = [p for p in rec.get("content") or [] if os.path.exists(p)]
        if not paths:
            self.open_viewer(rec)
            return
        n = 0
        for p in paths[:10]:
            if cm.open_file_default(p):
                n += 1
        if n:
            InfoBar.success("已打开", "%d 个文件已用默认程序打开" % n,
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)

    def reveal_first(self, rec):
        for p in rec.get("content") or []:
            if os.path.exists(p):
                if cm.reveal_in_explorer(p):
                    return
                break
        InfoBar.error("定位失败", "文件不存在或无法打开资源管理器",
                      parent=self.win, position=InfoBarPosition.TOP_RIGHT)

    def open_image_viewer(self, rec, edit=False, on_result=None):
        """打开图片查看器(缩放/平移;与文本查看器同入 _viewers 统一清理)。
        edit=True 直接进入内嵌编辑模式;on_result 为编辑产物直通回调。"""
        try:
            v = ImageViewerWindow(self, rec, edit=edit, on_result=on_result)
            _pop_to_front(v)
            self._viewers.append(v)
            return v
        except ValueError:
            MessageBox("WinClipboard", "无法加载图片文件,可能已被清理。",
                       self.panel).exec()
            return None
        except Exception:
            cm.log("打开图片查看器失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "图片查看器打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()
            return None

    def pin_image(self, rec):
        """把图片条目钉到置顶画布(右键图片条目 →「置顶到屏幕」)。
        画布独立持有资产副本(pins/images),历史删除不影响已钉图片;
        见 pinboard.PinBoardManager。"""
        path = str(rec.get("content") or "") if rec.get("type") == "image" else ""
        if not path or not os.path.exists(path):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。",
                       self.panel).exec()
            return None
        it = self.pinboard.pin_path(path)
        if it is None:
            MessageBox("WinClipboard", "无法加载图片文件,可能已被清理。",
                       self.panel).exec()
            return None
        return it

    # ---- AI 工作流 / 各功能页(单一窗口内导航切换) ----
    def show_ai_hub(self):
        self.win.switchTo(self.win.workflow_page)

    def show_services(self):
        self.win.services_page.refresh()   # 配置可能在别处变动(如工作流页填入示例)
        self.win.switchTo(self.win.services_page)

    def run_workflow(self, wf, rec):
        runner = WorkflowRunner(self, wf, rec["content"])
        _pop_to_front(runner)
        self._ai_runners.append(runner)

    # ---- 托盘动作 ----
    def clear_all(self):
        self.core.clear_unpinned()
        self.refresh_view()

    def show_private(self):
        if not self.private:
            InfoBar.info("私密内容", "暂无敏感内容", parent=self.win,
                         position=InfoBarPosition.TOP_RIGHT)
            return
        self.win.private_page.refresh()
        self.win.switchTo(self.win.private_page)

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
                   self.win).exec()

    def quit_app(self):
        for r in self._ai_runners:          # 等 AI 线程收尾,避免 QThread 运行中析构
            try:
                r._dead = True
                if r._worker is not None and r._worker.isRunning():
                    r._worker.wait(3000)
            except Exception:
                pass
        try:
            self.pinboard.save_now()       # 画布可见状态随 quit 落盘
        except Exception:
            pass
        self.core.save_all()
        self.worker.cmd_q.put(("quit",))
        self.qapp.quit()

    # ---- 信号排空 / 预热 ----
    def eventFilter(self, obj, ev):
        """首帧探针:热键 → HistoryPage 首次 Paint(它必绘;MainWindow 自身
        在 Mica/子窗口全遮下不收 Paint/Expose,顶层挂不到,只能挂内容页)。
        ts 一次一清,常规重绘不产生日志。"""
        if obj is self.panel and ev.type() == QEvent.Paint:
            ts = getattr(self, "_hotkey_ts", None)
            if ts is not None:
                self._hotkey_ts = None
                since(ts, "hotkey_to_first_paint")
        return super().eventFilter(obj, ev)

    def _drain_events(self):
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
            self._hotkey_ts = stamp()       # 首帧统计起点(paintEvent 收口)
            with PerfSpan("hotkey_to_show_window"):
                self.win.toggle()
        elif kind == "pin_hotkey":          # Ctrl+Shift+P:直接钉剪贴板图片
            self.pinboard.pin_clipboard_image()
        elif kind == "ctt_hotkey":          # Ctrl+Alt+T:切换鼠标穿透
            self.pinboard.toggle_click_through(None)
        elif kind == "ctt_hotkey_state":    # 穿透退出热键注册结果(失败则禁穿透)
            self.pinboard.mark_ctt_hotkey(bool(rest[0]))
        elif kind == "tray":
            act = rest[0]
            if act == "show":
                with PerfSpan("tray_to_show_window"):
                    self.win.toggle()
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
            elif act == "ctt_off":          # 托盘菜单:穿透保底关闭入口
                self.pinboard.toggle_click_through(False)
            elif act == "quit":
                self.quit_app()

    def thumb_cached(self, path):
        """仅查缓存(卡片构建用):未命中返回 None 并转后台生成,不阻塞 UI 线程。

        命中时把键移到字典尾部(LRU 触碰):淘汰按插入序 FIFO,列表来回滚动时
        最近用过的缩略图不至于先被挤出去引发重解码。
        """
        if not path:
            return None
        pm = self._thumbs.pop(path, None)
        if pm is not None:
            self._thumbs[path] = pm
            return pm
        self.request_thumb(path)
        return None

    def request_thumb(self, path):
        loader = getattr(self, "_thumb_loader", None)
        if loader is not None and path:
            loader.submit(path)

    def thumb_for(self, path):
        if not path or not os.path.exists(path):
            return None
        pm = self._thumbs.get(path)
        if pm is not None:
            self._thumbs.pop(path)
            self._thumbs[path] = pm    # LRU 触碰,与 thumb_cached 一致
            return pm
        try:
            # Qt C++ 解码+缩放比 PIL LANCZOS 快约 40% 且无 PNG 编码往返;
            # 2x 尺寸供高 DPI 显示。启动时的全量预热走 ThumbLoader 后台线程,
            # 这里只兜底处理新截图入史的单张生成。
            w, h = Card.THUMB
            img = QImage(path)
            if img.isNull():
                return None
            pm = QPixmap.fromImage(img.scaled(
                w * 2, h * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._thumbs[path] = pm
            while len(self._thumbs) > 300:    # FIFO 淘汰;整表清空会引发批量重解码
                self._thumbs.pop(next(iter(self._thumbs)))
        except Exception:
            return None
        return pm

    def _cleanup_orphan_images(self):
        """启动 3 秒后的孤儿图片清理(core 侧自带引用快照与新文件时间窗)。"""
        try:
            self.core.cleanup_orphan_images()
        except Exception:
            cm.log("孤儿图片清理失败:\n%s" % traceback.format_exc())

    def _prewarm(self):
        """启动 600ms 后的空闲预热:后台生成缩略图 + 隐藏时预构建卡片池。

        百张卡片的首次构建实测约 1 秒(QSS 解析 / 标签初始化),过去发生在
        第一次热键唤出时;挪到常驻托盘的空闲期做完,首次唤出直接走池复用(~10ms)。
        """
        try:
            if self._view_dirty:
                self.refresh_view()
                for c in self.panel._cards:
                    c.ensurePolished()   # 隐藏状态下提前完成 QSS 解析
            for h in self.history:
                if h.get("type") == "image":
                    self._thumb_loader.submit(h["content"])
        except Exception:
            cm.log("启动预热失败:\n%s" % traceback.format_exc())


def _pop_to_front(w):
    """把新工具窗口顶到前台(查看器 / 运行台等)。

    双击卡片打开时,调用方主窗仍持有鼠标输入序列,Windows 前台规则常拒绝
    activateWindow、raise_ 也压不过去 → 查看器开在主窗下面(用户实测:
    「双击复制的内容,它不是置顶的,都隐藏在主页面下面」)。标准解法:
    短暂置 TOPMOST 抢到最上层,再摘掉标志落回普通层级——之后主窗仍可
    正常盖回,不产生永久置顶的副作用。
    """
    try:
        w.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        w.show()
        w.raise_()
        w.activateWindow()
        w.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        w.show()
    except Exception:
        try:
            w.show()
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
        # 二次启动:唤醒已运行实例。优先给托盘宿主窗口投递 WM_HOTKEY,
        # 走实例自己的热键链(toggle/show_fast,内容正常渲染)——直接对主窗
        # Win32 ShowWindow 只对"曾显示过后隐藏"的窗口有效,启动后从未
        # 经过 Qt show 的主窗没有渲染内容,拉起来是空白框(实测坑)。
        try:
            import win32gui
            import win32con
            host = win32gui.FindWindow(None, "WinClipboardTrayHost")
            if host:
                win32gui.PostMessage(host, win32con.WM_HOTKEY, 0, 0)
                return 0
            hwnd = win32gui.FindWindow(None, "WinClipboard")
            if hwnd:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                else:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                return 0
        except Exception:
            pass
        cm.ctypes.windll.user32.MessageBoxW(
            0, "WinClipboard 已经在运行中(检查系统托盘)。", cm.APP_NAME, 0x40)
        return 0
    globals()["_MUTEX"] = mutex
    cm.log("启动(Qt+Fluent) pid=%d py=%s %s" %
           (os.getpid(), sys.executable, sys.version.split()[0]))
    perf_probe.setup()                    # 性能埋点 → logs/perf.log(独立于 app.log)

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setFont(QFont("Segoe UI", 10))
    # 主题交给 qfluentwidgets:config 的 light/dark 显式覆盖,auto 跟随系统;
    # theme_color 为强调色覆盖(config.json 「主题色」设置页写入)
    cfg0 = cm.load_config()
    pref = str(cfg0.get("theme", "auto")).strip().lower()
    setTheme(Theme.DARK if pref == "dark" else
             Theme.LIGHT if pref == "light" else Theme.AUTO)
    tcolor = str(cfg0.get("theme_color", "") or "").strip()
    if tcolor:
        try:
            setThemeColor(QColor(tcolor))
        except Exception:
            pass
    init_palette(isDarkTheme())

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
