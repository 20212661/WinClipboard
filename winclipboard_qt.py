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
from pinboard import annotations as _anno_layer   # 标注层(非破坏模型)
import workflow_steps as wfs   # AI 工作流 Step Contract(步骤统一契约)
import workflow_runlog as wrl  # 工作流运行记录(Run Inspector 数据层)
import diagnostics as diag     # 诊断快照 + 数据健康检查(纯数据层,无 UI)
from quickpaste import QuickPasteWindow  # 快捷粘贴浮层(默认 Alt+`)
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
                            PasswordLineEdit,
                            CheckBox,
                            InfoBarPosition, BodyLabel, StrongBodyLabel,
                            CaptionLabel, TitleLabel,
                            SmoothScrollDelegate, ScrollBarHandleDisplayMode,
                            TreeWidget, TableWidget,
                            SwitchSettingCard, PushSettingCard, OptionsSettingCard,
                            SettingCardGroup, SettingCard)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QMessageBox

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
            "DIFF_ADD": "#1f7a3f", "DIFF_DEL": "#c0392b", "DIFF_CTX": "rgba(0,0,0,0.45)",
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
            "DIFF_ADD": "#5ec97f", "DIFF_DEL": "#ef8080", "DIFF_CTX": "rgba(255,255,255,0.45)",
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


# 统一字族链:拉丁 Segoe UI;中文依次回退。雅黑的 UI 变体优先(行高与
# Fluent 组件匹配);SimHei/Noto Sans SC 兜底离屏 FreeType 数据库枚举不到
# msyh.ttc(雅黑)的环境。顺序即优先级,勿把中文字体放最前(拉丁字形变丑)。
FONT_FAMILIES = ["Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei",
                 "SimHei", "Noto Sans SC", "PingFang SC"]


def font(px, weight=QFont.Normal):
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(px)
    f.setWeight(weight)
    return f


def apply_fonts(qapp=None):
    """统一应用字族:拉丁用 Segoe UI,中文按序回退到机器上首个可用字体。

    两处必须一起改:
    - Qt 文本:QFont 单字族在缺字形的环境(离屏 FreeType 数据库读不出
      msyh.ttc,枚举不到雅黑)整字变豆腐框,families 回退链才能兜住;
    - qfluentwidgets 组件:内部 getFont 走自己的 fontFamilies 配置
      (默认仅 YaHei/PingFang 回退),不同步覆盖则卡片/标签仍是豆腐框。
    """
    try:
        from qfluentwidgets.common.font import setFontFamilies
        setFontFamilies(list(FONT_FAMILIES))
    except Exception:
        cm.log("qfluentwidgets 字族覆盖失败(组件将用库默认字体)")
    f = font(10)
    if qapp is not None:
        qapp.setFont(f)
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
            meta = " · ".join(x for x in (ext, size, rec.get("src", ""),
                                          cm._rel_time(rec.get("ts", 0))) if x)
        elif kind == "link":
            title = cm.url_info(rec["content"])[0]
            sub = rec["content"].strip()
            meta = " · ".join(x for x in ("链接", rec.get("src", ""),
                                          cm._rel_time(rec.get("ts", 0))) if x)
        else:
            lines = [l for l in rec["content"].strip().splitlines() if l.strip()]
            title = lines[0] if lines else ""
            sub = "\n".join(lines[1:3]) if len(lines) > 1 else ""
            badge = self._badge_of(rec)
            meta = " · ".join(x for x in (badge or "文本", rec.get("src", ""),
                                          cm._rel_time(rec.get("ts", 0))) if x)
        self._set_text(self.title_lbl, elided(title, self.title_lbl.font(), 640))
        self._set_text(self.sub_lbl, elided(sub, self.sub_lbl.font(), 640) if sub else "")
        self.sub_lbl.setVisible(bool(sub))
        self._set_text(self.meta_lbl, meta)
        # 屏幕阅读器:卡片读出条目标题而非整段内容(私密凭据永不进卡片)
        self.setAccessibleName("剪贴板条目 " + (title or "")[:60])
        self._pin_shown = None          # 重绑后置顶态必查一次
        self.update_pin()

    @staticmethod
    def _set_text(lbl, txt):
        if lbl.text() != txt:           # setText 触发重排/重绘,不变则跳过
            lbl.setText(txt)

    def _badge_of(self, rec):
        """子类型徽章(JWT/Base64):带 id(rec) 缓存,同记录重复绑定零探测。
        缓存挂在宿主 app 上并懒初始化——离屏渲染/测试的 FakeApp 可不携带。"""
        cache = getattr(self.app, "_badge_cache", None)
        if cache is None:
            cache = self.app._badge_cache = {}
        key = id(rec)
        got = cache.get(key)
        if got is None:
            got = cm.effective_subtype(rec) or ""
            if len(cache) > 512:
                cache.clear()
            cache[key] = got
        return got or None

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
        return " · ".join(x for x in (kind, self.rec.get("src", ""),
                                      cm._rel_time(self.rec.get("ts", 0))) if x)

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
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.ControlModifier):
            # Ctrl+点击 = 对比流:第一条设基准,第二条开对比窗口
            self.app.select_card(self)
            self.app.compare_pick(self.rec)
            return
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.ShiftModifier):
            # Shift+点击 = 段选(当前选中到此处),Delete 一键批量删除(可撤销)
            self.app.range_select_from(self)
            return
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
        self.search.setAccessibleName("搜索剪贴历史")
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
        # 来源 / 时间筛选(数据来自条目的 src 与 ts;与分类、搜索叠加生效)
        self.cmb_src = ComboBox(self)
        self.cmb_src.setToolTip("按来源应用筛选(条目元信息行显示来源)")
        self.cmb_src.setMinimumWidth(118)
        self.cmb_src.setAccessibleName("按来源应用筛选")
        self.cmb_src.addItem("全部来源", userData="")
        self.cmb_src.currentIndexChanged.connect(
            lambda i: self.app.set_src_filter(self.cmb_src.itemData(i) or ""))
        self.cmb_time = ComboBox(self)
        for _tl, _ts in (("全部时间", "all"), ("今天", "today"),
                         ("近 24 小时", "24h"), ("近 7 天", "7d"),
                         ("近 30 天", "30d")):
            self.cmb_time.addItem(_tl, userData=_ts)
        self.cmb_time.setMinimumWidth(104)
        self.cmb_time.setToolTip("按复制时间筛选")
        self.cmb_time.setAccessibleName("按时间范围筛选")
        self.cmb_time.currentIndexChanged.connect(
            lambda i: self.app.set_time_filter(self.cmb_time.itemData(i) or "all"))
        tools.addWidget(self.cmb_src)
        tools.addWidget(self.cmb_time)
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
        # 窗口化渲染:大视图只物化前 _win_limit 张卡,滚动到底部自动扩展
        # (50,000 条过滤结果也只创建 ~60 个 Card Widget;滚到多深建多少)
        self._win_limit = 60
        self._win_chunk = 80
        self._full_seq = []
        self._btn_more = PushButton("", self)
        self._btn_more.setObjectName("btnMore")
        self._btn_more.setStyleSheet(
            "QPushButton#btnMore { color: %s; background: transparent;"
            " border: none; padding: 8px; }" % TER)
        self._btn_more.setCursor(Qt.PointingHandCursor)
        self._btn_more.clicked.connect(self._extend_window)
        self.scroll.verticalScrollBar().valueChanged.connect(
            self._on_scroll_maybe_extend)
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
        self.pv_btn_copy.setAccessibleName("复制完整内容到剪贴板")
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
        self.app.set_src_filter("")
        self.app.set_time_filter("all")
        self.sync_src_combo()
        self.cmb_time.setCurrentIndex(0)
        self.app.refresh_view()
        self.search.setFocus()

    def sync_src_combo(self):
        """来源下拉与历史同步(最近在前,保留当前选择;被清空的来源回落到全部)。"""
        cur = self.cmb_src.currentData() or ""
        apps = self.app.core.source_apps()[:40]
        if cur and cur not in apps:
            self.app.src_filter = ""       # 来源已被清理,过滤条件同步失效
            cur = ""
        self.cmb_src.blockSignals(True)
        self.cmb_src.clear()
        self.cmb_src.addItem("全部来源", userData="")
        for s in apps:
            self.cmb_src.addItem(s, userData=s)
        idx = self.cmb_src.findData(cur)
        self.cmb_src.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_src.blockSignals(False)

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
        elif e.key() == Qt.Key_F1:                  # 应用内快捷键总览
            self.app.show_shortcut_help()
        elif e.key() == Qt.Key_Down:
            self.app.move_sel(1)
        elif e.key() == Qt.Key_Up:
            self.app.move_sel(-1)
        elif e.key() == Qt.Key_Return or e.key() == Qt.Key_Enter:
            m = e.modifiers()
            if m & Qt.ControlModifier and m & Qt.ShiftModifier:
                self.app.open_transform()          # Ctrl+Shift+Enter 转换粘贴
            elif m & Qt.ShiftModifier:
                self.app.paste_plain()             # Shift+Enter 纯文本
            else:
                self.app.paste_selected()
        elif e.key() == Qt.Key_Delete:
            self.app.delete_selection_or_multi()
        else:
            super().keyPressEvent(e)

    def _resize_to_content(self):
        """兼容保留:页面高度由 FluentWindow 管理,无需自适应。"""

    # ---- 列表(卡片池复用:搜索/分类切换零重建) ----
    def set_view(self, view, history):
        """刷新 = 既有控件的重新排布 + 差量绑定,零销毁重建。

        - 卡片按 rec 身份配对回原控件(bind 幂等,同 rec 零开销),
          配不上的从 _idle 池取,池空才新建;
        - 分组标签/尾部弹簧常驻复用,空组 hide;
        - 视图序列与分组完全未变时连布局都不碰(重复 show/无效刷新零成本);
        - 窗口化:大视图只物化前 _win_limit 张(滚动到底自动扩展,
          见 _on_scroll_maybe_extend)——几万条过滤结果也只建 ~60 个控件。
        """
        self._last_view, self._last_history = view, history
        recs = [history[i] for i in view]
        all_seq = ([r for r in recs if r.get("pinned")]
                   + [r for r in recs if not r.get("pinned")])
        if [id(r) for r in self._full_seq] != [id(r) for r in all_seq]:
            self._full_seq = all_seq
            self._win_limit = 60               # 新视图 → 重置窗口
        seq = self._full_seq[:self._win_limit]
        pinned = [r for r in seq if r.get("pinned")]
        rest = [r for r in seq if not r.get("pinned")]
        ids = [id(r) for r in seq]
        more = len(self._full_seq) - len(seq)

        if ([id(c.rec) for c in self._cards] == ids
                and self._lab_pinned.isVisible() == bool(pinned)
                and self._lab_recent.isVisible() == bool(rest)
                and self._btn_more.isVisible() == (more > 0)
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
        if more > 0:
            # 窗口化截断:显示扩展入口(滚动到底也自动扩展)
            self._btn_more.setText("继续滚动加载更多(还有 %d 条)"
                                   % more)
            self._btn_more.setVisible(True)
            self.cards_layout.addWidget(self._btn_more)
        else:
            self._btn_more.setVisible(False)
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

    def _extend_window(self):
        """扩展物化窗口(+80)并增量重排;已到底时无操作。"""
        if self._win_limit >= len(self._full_seq):
            return
        self._win_limit += self._win_chunk
        self.set_view(self._last_view, self._last_history)

    def _on_scroll_maybe_extend(self, value):
        """滚动接近底部 → 自动扩展窗口(距底 <600px)。
        滚动条未成形(max=0,内容未超视口或首次布局)与置顶态不触发,
        防止视图刚建立就被整页自动展开。"""
        if not self._full_seq or self._win_limit >= len(self._full_seq):
            return
        sb = self.scroll.verticalScrollBar()
        if sb.maximum() > 0 and value > 0 and sb.maximum() - value < 600:
            self._extend_window()

    def card_at(self, vi):
        """视图索引 → 对应卡片控件。卡片列表顺序 = 置顶组+最近组(set_view),
        与 _view 的历史序不一致,任何「按下标取卡」都必须经这里按记录身份解析。
        (离屏预览的 FakeApp 无 _view:退回控件下标,纯装饰性选择不受影响)"""
        view = getattr(self.app, "_view", None)
        if view is None:
            if vi is not None and 0 <= vi < len(self._cards):
                return self._cards[vi]
            return None
        if vi is None or not (0 <= vi < len(view)):
            return None
        hi = view[vi]
        if not (0 <= hi < len(self.app.history)):
            return None
        rec = self.app.history[hi]
        return next((c for c in self._cards if c.rec is rec), None)

    def select_index(self, vi):
        card = self.card_at(vi)
        for c in self._cards:
            c.set_selected(c is card)
        self.update_preview(vi)

    def mark_multi(self, rec_ids):
        """段选高亮(Shift+点击批量选择):按记录身份标亮整段,预览清空。"""
        for c in self._cards:
            c.set_selected(id(c.rec) in rec_ids)
        self.update_preview(None)

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
            # 预览只用缓存缩略图(256px,后台生成);原图仅双击查看器加载,
            # 列表/预览路径不再同步解码原图(大图曾把选中卡成百毫秒级)
            pm = (getattr(app, "_thumbs", None) or {}).get(path)
            if pm is not None:
                self.pv_img.setPixmap(pm.scaled(
                    min(PANEL_W - 40, pm.width() * 8), 128,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                loader = getattr(app, "_thumb_loader", None)
                if loader is not None:
                    loader.submit(path)      # 生成完下次选中即有;本次占位
                self.pv_img.setText("生成缩略图中…")
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
        card = self.card_at(vi)     # 置顶组重排后控件下标≠视图下标,按记录找卡
        if card is not None:
            self.scroll.ensureWidgetVisible(card, 0, 20)

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
            # 粘贴事务目标:显示前的前台窗口 = 用户此刻的工作现场;
            # 之后回车粘贴将以它为校验目标(窗口切换则取消,见 _do_cmd)
            fg = cm._get_foreground()
            if fg and fg != int(self.winId()):
                self.app._paste_target = fg
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
    """私密区:列表默认遮罩显示,点「显示明文」解锁,30 秒后或离开页面
    自动重新遮蔽——防止旁人扫一眼屏幕就拿到凭据明文。"""

    REVEAL_SECONDS = 30

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("privatePage")
        self._revealed = False
        self._mask_timer = QTimer(self)
        self._mask_timer.setSingleShot(True)
        self._mask_timer.setInterval(self.REVEAL_SECONDS * 1000)
        self._mask_timer.timeout.connect(lambda: self._set_revealed(False))
        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)

        ttl = TitleLabel("私密区")
        v.addWidget(ttl)

        if cm.dpapi_available():
            hint = CaptionLabel("以下内容经敏感识别后隔离保存,不出现在历史面板;"
                                "使用 Windows DPAPI 绑定当前 Windows 用户加密。"
                                "该机制主要保护静态磁盘数据(文件被拷走也读不出),"
                                "不能抵御已获得当前用户权限的恶意程序。"
                                "列表默认遮罩,点「显示明文」解锁,30 秒后自动遮回。")
        else:
            hint = CaptionLabel("⚠ 当前环境 DPAPI 不可用,私密区仅 Base64 编码存储(未加密),"
                                "请勿在本机存放重要凭据。列表默认遮罩,"
                                "点「显示明文」解锁,30 秒后自动遮回。")
        hint.setStyleSheet("color: %s;" % TER)
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.lst = QListWidget()
        self.lst.setFont(font(13))
        self.lst.setAccessibleName("私密内容列表(默认遮罩显示)")
        self.lst.itemDoubleClicked.connect(lambda _: self.do_view())
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
        self.b_eye = PushButton("显示明文", self)
        self.b_eye.setIcon(FIF.VIEW.icon())
        b_view = PushButton("查看", self)
        b_view.setIcon(FIF.VIEW.icon())
        b_view.setToolTip("独立窗口查看(JWT 自动解码;仅解码,不验签)")
        b_del = PushButton("删除", self)
        b_del.setIcon(FIF.DELETE.icon())
        b_clear = PushButton("清空", self)
        b_copy.clicked.connect(self.do_copy)
        self.b_eye.clicked.connect(lambda: self._set_revealed(not self._revealed))
        b_view.clicked.connect(self.do_view)
        b_del.clicked.connect(self.do_del)
        b_clear.clicked.connect(self.do_clear)
        for b in (b_copy, self.b_eye, b_view, b_del, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.refresh()

    # ---- 遮罩 / 解锁 ----
    def _mask_text(self, s):
        one = s.replace("\n", " ").replace("\t", " ")
        if self._revealed:
            return one[:80] + ("…" if len(one) > 80 else "")
        n = len(one)
        return "•" * min(n, 18) + ("(%d 字)" % n if n > 18 else "")

    def _set_revealed(self, on):
        if on == self._revealed:
            return
        self._revealed = on
        if on:
            self._mask_timer.start()
        else:
            self._mask_timer.stop()
        self.b_eye.setText("隐藏明文" if on else "显示明文")
        self.b_eye.setIcon(FIF.HIDE.icon() if on else FIF.VIEW.icon())
        self.refresh()

    def hideEvent(self, e):
        # 切走页面立即遮回:不留一个常亮的明文清单
        self._set_revealed(False)
        super().hideEvent(e)

    def refresh(self):
        self.lst.clear()
        for s in self.app.private:
            self.lst.addItem(self._mask_text(s))

    def do_copy(self):
        item = self.lst.currentItem()
        if not item:
            return
        i = self.lst.row(item)
        if not 0 <= i < len(self.app.private):
            return
        # 按钮就是「复制并粘贴」:用户显式要求回贴,不受 auto_paste 默认关影响
        self.app.core.paste_text(self.app.private[i], force=True,
                                 target_hwnd=getattr(self.app, "_paste_target",
                                                     None))

    def do_view(self):
        """私密条目 → 独立查看器(JWT 自动落解码视图;rec 带 private 标记,
        查看器内「复制」只写剪贴板,不往此前聚焦的窗口回贴)。"""
        item = self.lst.currentItem()
        if not item:
            return None
        i = self.lst.row(item)
        if not 0 <= i < len(self.app.private):
            return None
        return self.app.open_viewer({"type": "text", "content": self.app.private[i],
                                     "private": True})

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
# Qt 键值 → VK 码(HotkeyRecorderDialog 用;OEM 符号键,含快捷粘贴默认 `)
_QT_KEY_TO_VK = {
    Qt.Key_QuoteLeft: 0xC0, Qt.Key_Minus: 0xBD, Qt.Key_Equal: 0xBB,
    Qt.Key_Comma: 0xBC, Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF,
    Qt.Key_Semicolon: 0xBA, Qt.Key_Apostrophe: 0xDE,
    Qt.Key_BracketLeft: 0xDB, Qt.Key_BracketRight: 0xDD,
    Qt.Key_Backslash: 0xDC,
}


class HotkeyRecorderDialog(MessageBoxBase):
    """全局热键录制:点击输入框直接按键捕获组合,「应用」经核心线程
    重注册(即冲突检测),被占用时回滚并提示,不再要求手改配置文件。"""

    _MODIFIER_KEYS = (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt,
                      Qt.Key_Meta, Qt.Key_AltGr)

    def __init__(self, parent, cur_label, on_apply):
        super().__init__(parent)
        self._on_apply = on_apply
        self._mod = 0
        self._key = 0

        ttl = StrongBodyLabel("录制全局热键")
        self.viewLayout.addWidget(ttl)
        sub = CaptionLabel("当前热键:%s。点击下方输入框,直接按下新组合键"
                           "(需搭配 Ctrl / Shift / Alt / Win 至少一个);Esc 取消。"
                           % cur_label)
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        self.capture = LineEdit()
        self.capture.setReadOnly(True)
        self.capture.setPlaceholderText("点这里,然后按下组合键(如 Ctrl+Shift+V)")
        # 只读框默认不吃按键:按下即视为录制(实例级重载,不改库组件)
        self.capture.keyPressEvent = self._on_key
        self.viewLayout.addWidget(self.capture)

        self.yesButton.setText("应用")
        self.cancelButton.setText("取消")
        self.yesButton.setEnabled(False)
        self.widget.setFixedWidth(440)

    def _on_key(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.reject()
            return
        mod = 0
        m = e.modifiers()
        if m & Qt.ControlModifier:
            mod |= cm.MOD_CONTROL
        if m & Qt.ShiftModifier:
            mod |= cm.MOD_SHIFT
        if m & Qt.AltModifier:
            mod |= cm.MOD_ALT
        if m & Qt.MetaModifier:
            mod |= cm.MOD_WIN
        if k in self._MODIFIER_KEYS:
            self._mod, self._key = mod, 0
            self.capture.setText(cm.hotkey_label(mod, None))
            self.yesButton.setEnabled(False)
            return
        vk = None
        if Qt.Key_A <= k <= Qt.Key_Z:
            vk = 0x41 + (k - Qt.Key_A)
        elif Qt.Key_0 <= k <= Qt.Key_9:
            vk = 0x30 + (k - Qt.Key_0)
        elif Qt.Key_F1 <= k <= Qt.Key_F12:
            vk = 0x70 + (k - Qt.Key_F1)
        elif k == Qt.Key_Space:
            vk = 0x20
        else:
            vk = _QT_KEY_TO_VK.get(k)      # OEM 键(`、-、= 等,快捷粘贴默认 Alt+`)
        if vk is None:
            self.capture.setText("暂不支持该按键,请用字母 / 数字 / F1-F12 / 空格"
                                 " / ` - = 等符号键")
            self.yesButton.setEnabled(False)
            return
        if mod == 0:
            self.capture.setText("需至少搭配一个修饰键(Ctrl / Shift / Alt / Win)")
            self.yesButton.setEnabled(False)
            return
        self._mod, self._key = mod, vk
        self.capture.setText(cm.hotkey_label(mod, vk))
        self.yesButton.setEnabled(True)

    def validate(self):
        """点「应用」:交给宿主走重注册链路;真正的成败由 hotkey_change 事件回报。"""
        if self._mod and self._key:
            self._on_apply(self._mod, self._key)
            return True
        return False


class ExcludeAppsDialog(MessageBoxBase):
    """按应用排除名单管理:添加(下拉里带历史出现过的来源应用,可直接输
    exe 名) / 移除选中 / 保存。命中名单的程序复制内容不入史——与暂停
    监听同一条隐私控制线,只是按应用长期生效。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        ttl = StrongBodyLabel("按应用排除")
        self.viewLayout.addWidget(ttl)
        sub = CaptionLabel("名单内程序(如密码管理器、验证器)的复制不进入历史。"
                           "名称为进程 exe 文件名,不区分大小写;下拉列出最近复制"
                           "过的来源应用,选中即可一键排除。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        row = QHBoxLayout()
        self.cmb = EditableComboBox()
        seen, sug = set(), []
        for h in app.history:                      # 历史出现过的来源应用(最近在前)
            s = h.get("src")
            if s and s not in seen:
                seen.add(s)
                sug.append(s)
        for s in (app.cfg.get("exclude_apps") or []):
            if s not in seen:
                seen.add(s)
                sug.append(s)
        self.cmb.addItems(sug[:40])
        self.cmb.setMinimumWidth(240)
        b_add = PushButton("添加", self)
        b_add.setIcon(FIF.ADD.icon())
        b_add.clicked.connect(self._add_current)
        row.addWidget(self.cmb, 1)
        row.addWidget(b_add)
        self.viewLayout.addLayout(row)

        self.lst = QListWidget()
        self.lst.setFont(font(12))
        self.lst.setSelectionMode(QListWidget.MultiSelection)
        for s in (app.cfg.get("exclude_apps") or []):
            self.lst.addItem(s)
        self.viewLayout.addWidget(self.lst)

        b_rm = PushButton("移除选中", self)
        b_rm.setIcon(FIF.DELETE.icon())
        b_rm.clicked.connect(self._remove_selected)
        self.viewLayout.addWidget(b_rm, 0, Qt.AlignLeft)

        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(440)

    def _add_current(self):
        t = os.path.basename(self.cmb.currentText().strip()).lower()
        if not t:
            return
        for i in range(self.lst.count()):          # 去重
            if self.lst.item(i).text() == t:
                return
        self.lst.addItem(t)
        self.cmb.setText("")                    # 加完清空输入,便于连续添加
                          # (qfluentwidgets EditableComboBox 无 lineEdit(),任意文本走 setText)

    def _remove_selected(self):
        for it in self.lst.selectedItems():
            self.lst.takeItem(self.lst.row(it))

    def validate(self):
        """点「保存」:整表交给核心归一化落盘(见 core.set_exclude_apps)。"""
        names = [self.lst.item(i).text() for i in range(self.lst.count())]
        self.app.core.set_exclude_apps(names)
        return True


class SettingsPage(QWidget):
    _health_done = Signal(object)     # 健康检查后台线程 → UI 线程弹结果

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("settingsPage")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 24, 36, 16)
        outer.setSpacing(8)
        ttl = TitleLabel("设置")
        outer.addWidget(ttl)
        # 卡片组放进滚动容器:默认窗口尺寸与高 DPI 缩放下底部卡片
        # (开机自启/全局热键)不再被裁掉;标题常驻,只有内容滚动
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(8)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        cfg = app.cfg
        self._health_done.connect(self._show_health_result)

        group = SettingCardGroup("剪贴板", self)
        c_pause = SwitchSettingCard(
            FIF.MUTE, "暂停监听", "暂停期间复制的内容不进入历史(临时隐私模式;"
            "托盘菜单同款开关;重启后自动恢复监听,不落盘)",
            parent=group)
        c_pause.setChecked(bool(getattr(app.core, "monitor_paused", False)))
        self.c_pause = c_pause

        def on_pause(on):
            app.toggle_monitor_pause(bool(on))
        c_pause.checkedChanged.connect(on_pause)

        c_paste = SwitchSettingCard(
            FIF.PASTE, "自动粘贴", "选中条目回车后自动向当前窗口发送 Ctrl+V;"
            "关闭则只写入剪贴板、由你手动粘贴(默认关,更安全)",
            parent=group)
        c_paste.setChecked(bool(cfg.get("auto_paste", False)))

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

        # 按应用排除:命中名单的程序(如密码管理器)复制不入史
        c_excl = SettingCard(
            FIF.FILTER, "按应用排除",
            "名单内程序的复制不进入历史(如密码管理器/验证器);点「管理」增删名单",
            parent=group)
        self.lbl_excl = CaptionLabel("")
        self.lbl_excl.setStyleSheet("color: %s; font-weight: 600;" % ACCENT)
        b_excl = PushButton("管理", self)
        b_excl.clicked.connect(self._edit_exclusions)
        c_excl.hBoxLayout.addWidget(self.lbl_excl, 0, Qt.AlignRight)
        c_excl.hBoxLayout.addSpacing(8)
        c_excl.hBoxLayout.addWidget(b_excl, 0, Qt.AlignRight)
        c_excl.hBoxLayout.addSpacing(16)
        self._refresh_excl_label()

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

        group.addSettingCard(c_pause)
        group.addSettingCard(c_paste)
        group.addSettingCard(c_guard)
        group.addSettingCard(c_excl)
        group.addSettingCard(c_max)
        lay.addWidget(group)

        # ---- 存储与清理 ----
        group3 = SettingCardGroup("存储与清理", self)

        def _combo_index(combo, values, cur):
            for i, v in enumerate(values):
                if v == cur:
                    combo.setCurrentIndex(i)
                    break

        c_days = SettingCard(FIF.BROOM, "自动清理历史",
                             "超期未置顶条目自动删除(含图片文件);启动与每天检查一次",
                             parent=group3)
        combo_days = ComboBox()
        _DAYS_ITEMS = ("关闭", "7 天", "30 天", "90 天")
        _DAYS_VALS = (0, 7, 30, 90)
        combo_days.addItems(list(_DAYS_ITEMS))
        combo_days.setMinimumWidth(110)
        _combo_index(combo_days, _DAYS_VALS, int(cfg.get("cleanup_days", 0) or 0))

        def on_days(i):
            cfg["cleanup_days"] = _DAYS_VALS[i]
            cm.save_config(cfg)
            self._run_cleanup()
        combo_days.currentIndexChanged.connect(on_days)
        self.combo_days = combo_days
        c_days.hBoxLayout.addWidget(combo_days, 0, Qt.AlignRight)
        c_days.hBoxLayout.addSpacing(16)

        c_disk = SettingCard(FIF.SAVE, "图片缓存上限",
                             "截图缓存超过上限时从最旧开始删除(置顶条目豁免)",
                             parent=group3)
        combo_disk = ComboBox()
        _DISK_ITEMS = ("关闭", "200 MB", "500 MB", "1 GB", "2 GB")
        _DISK_VALS = (0, 200, 500, 1024, 2048)
        combo_disk.addItems(list(_DISK_ITEMS))
        combo_disk.setMinimumWidth(110)
        _combo_index(combo_disk, _DISK_VALS, int(cfg.get("disk_cap_mb", 0) or 0))

        def on_disk(i):
            cfg["disk_cap_mb"] = _DISK_VALS[i]
            cm.save_config(cfg)
            self._run_cleanup()
        combo_disk.currentIndexChanged.connect(on_disk)
        self.combo_disk = combo_disk
        c_disk.hBoxLayout.addWidget(combo_disk, 0, Qt.AlignRight)
        c_disk.hBoxLayout.addSpacing(16)

        c_enc = SwitchSettingCard(
            FIF.CERTIFICATE, "历史加密存储",
            "history.json 用 Windows DPAPI 绑定当前用户加密(复制文件到别的"
            "电脑读不出);主要保护静态磁盘数据,不抵御已获得当前用户权限的"
            "恶意程序;关闭则明文存储,便于备份与迁移", parent=group3)
        c_enc.setChecked(bool(cfg.get("encrypt_history")))

        def on_enc(on):
            # 即时双向切换:开 → 全量加密 JSON + 停库写透;
            # 关 → 启用库(空库则迁入当前历史)。见 core.set_history_encryption
            app.core.set_history_encryption(bool(on))
            cm.save_config(cfg)
            InfoBar.success("历史加密已" + ("开启" if on else "关闭"),
                            "存储已转换为" + ("加密 JSON" if on else "SQLite 库")
                            + "格式", parent=self, position=InfoBarPosition.TOP_RIGHT)
        c_enc.checkedChanged.connect(on_enc)
        self.c_enc = c_enc
        if not cm.dpapi_available():       # DPAPI 缺位时不开空头支票(与私密区同原则)
            c_enc.setEnabled(False)
            c_enc.setContent("当前环境 DPAPI 不可用,历史加密无法开启")

        group3.addSettingCard(c_days)
        group3.addSettingCard(c_disk)
        group3.addSettingCard(c_enc)
        lay.addWidget(group3)

        # ---- 备份与迁移 ----
        group4 = SettingCardGroup("备份与迁移", self)
        c_exp = PushSettingCard(
            "导出…", FIF.SAVE_AS, "导出历史(明文 JSON)",
            "全部条目导出为 JSON(可选内嵌图片);不含私密区;文件本身不加密",
            parent=group4)
        c_exp.clicked.connect(self._export_history)
        c_pexp = PushSettingCard(
            "加密备份…", FIF.CERTIFICATE, "加密备份(可迁移)",
            "密码 + AES-256-GCM 认证加密,换电脑凭密码即可恢复;"
            "私密区可选包含(默认不含,且只存在于加密容器内)",
            parent=group4)
        c_pexp.clicked.connect(self._export_portable)
        c_imp = PushSettingCard(
            "导入…", FIF.DOWNLOAD, "导入历史 / 恢复备份",
            "JSON 与加密备份(.wcbak)自动识别,合并去重、图片落盘重建、"
            "原时间/置顶还原",
            parent=group4)
        c_imp.clicked.connect(self._import_history)
        group4.addSettingCard(c_exp)
        group4.addSettingCard(c_pexp)
        group4.addSettingCard(c_imp)
        lay.addWidget(group4)

        group2 = SettingCardGroup("外观与系统", self)
        c_theme = SettingCard(FIF.BRUSH, "界面主题",
                              "跟随系统或固定浅色/深色(更改后立即生效)", parent=group2)
        combo = ComboBox()
        for _tl, _tv in (("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")):
            combo.addItem(_tl, userData=_tv)
        combo.setCurrentIndex(max(0, combo.findData(str(cfg.get("theme", "auto")))))
        combo.setMinimumWidth(120)
        combo.setAccessibleName("界面主题选择")
        self.combo_theme = combo

        def on_theme(t):
            cfg["theme"] = t
            cm.save_config(cfg)
            setTheme(Theme.DARK if t == "dark" else
                     Theme.LIGHT if t == "light" else Theme.AUTO)
            init_palette(isDarkTheme())
            cm.log("主题切换为 %s(部分页面元素需重开窗口刷新)" % t)
        combo.currentIndexChanged.connect(
            lambda i: on_theme(combo.itemData(i)))
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

        c_key = SettingCard(
            FIF.CUT, "全局热键",
            "显示/隐藏主窗口;点「更改」直接按键录制,被占用会即时提示并保留原热键",
            parent=group2)
        self.lbl_hotkey = CaptionLabel(cm.hotkey_label(
            cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
            cfg.get("hotkey_key", 0x56)))
        self.lbl_hotkey.setStyleSheet("color: %s; font-weight: 600;" % ACCENT)
        b_key = PushButton("更改", self)
        b_key.clicked.connect(lambda: HotkeyRecorderDialog(
            self, self.lbl_hotkey.text(), app.apply_hotkey).exec())
        c_key.hBoxLayout.addWidget(self.lbl_hotkey, 0, Qt.AlignRight)
        c_key.hBoxLayout.addSpacing(8)
        c_key.hBoxLayout.addWidget(b_key, 0, Qt.AlignRight)
        c_key.hBoxLayout.addSpacing(16)

        # 快捷粘贴浮层热键(默认 Alt+`):Win+V 式轻入口
        c_qkey = SettingCard(
            FIF.PASTE, "快捷粘贴浮层",
            "弹出超轻量剪贴列表(Win+V 式):搜索 + ↑↓ + Enter 直接粘贴;"
            "点「更改」按键录制,被占用即时提示并保留原热键",
            parent=group2)
        self.lbl_quick = CaptionLabel(cm.hotkey_label(
            cfg.get("quick_mod", cm.MOD_ALT), cfg.get("quick_key", 0xC0)))
        self.lbl_quick.setStyleSheet("color: %s; font-weight: 600;" % ACCENT)
        b_qkey = PushButton("更改", self)
        b_qkey.clicked.connect(lambda: HotkeyRecorderDialog(
            self, self.lbl_quick.text(), app.apply_quick_hotkey).exec())
        c_qkey.hBoxLayout.addWidget(self.lbl_quick, 0, Qt.AlignRight)
        c_qkey.hBoxLayout.addSpacing(8)
        c_qkey.hBoxLayout.addWidget(b_qkey, 0, Qt.AlignRight)
        c_qkey.hBoxLayout.addSpacing(16)

        group2.addSettingCard(c_theme)
        group2.addSettingCard(c_accent)
        group2.addSettingCard(c_mica)
        group2.addSettingCard(c_auto)
        group2.addSettingCard(c_key)
        group2.addSettingCard(c_qkey)
        c_help = PushSettingCard(
            "查看", FIF.HELP, "快捷键与操作",
            "全局热键 / 列表 / 查看器 / 图片编辑的按键总览(主窗口按 F1 也可打开)",
            parent=group2)
        c_help.clicked.connect(app.show_shortcut_help)
        group2.addSettingCard(c_help)
        lay.addWidget(group2)

        # ---- 关于与更新 ----
        groupDiag = SettingCardGroup("诊断", self)
        c_diag = PushSettingCard(
            "复制", FIF.COPY, "诊断信息",
            "版本 / 运行环境 / 条目与存储占用 / 监听状态;"
            "仅元数据(不含任何内容与密钥),可放心粘贴给问题反馈",
            parent=groupDiag)
        c_diag.clicked.connect(self._copy_diagnostics)
        c_health = PushSettingCard(
            "检查", FIF.HEART, "数据健康检查",
            "数据库完整性 / 引用 blob 存在性 / 孤儿文件 / 私密区可解密 / 备份可读",
            parent=groupDiag)
        c_health.clicked.connect(self._run_health_check)
        c_fts = PushSettingCard(
            "重建", FIF.UPDATE, "重建搜索索引",
            "全文索引整体重建(搜索异常时的修复动作,不影响数据)",
            parent=groupDiag)
        c_fts.clicked.connect(self._rebuild_fts)
        c_orph = PushSettingCard(
            "清理", FIF.DELETE, "清理孤立缓存",
            "删除引用计数为零的图片 blob(与启动期自动清理同一套规则,"
            "带新文件时间窗防护)", parent=groupDiag)
        c_orph.clicked.connect(self._clean_orphan_caches)
        c_logs = PushSettingCard(
            "打开", FIF.FOLDER, "打开数据目录",
            "app.log(运行日志)与 logs/perf.log(性能埋点)所在目录",
            parent=groupDiag)
        c_logs.clicked.connect(lambda: cm.reveal_in_explorer(
            os.path.dirname(cm.LOG_FILE)))
        for _c in (c_diag, c_health, c_fts, c_orph, c_logs):
            groupDiag.addSettingCard(_c)
        lay.addWidget(groupDiag)

        group5 = SettingCardGroup("关于与更新", self)
        c_ver = SettingCard(
            FIF.INFO, "当前版本",
            "版本 %s(检查 GitHub 上的新版本发布,发现新版可一键打开发布页)"
            % cm.APP_VERSION, parent=group5)
        b_upd = PushButton("检查更新", self)
        b_upd.clicked.connect(lambda: app.check_update())
        c_ver.hBoxLayout.addWidget(b_upd, 0, Qt.AlignRight)
        c_ver.hBoxLayout.addSpacing(16)
        c_repo = PushSettingCard(
            "打开", FIF.LINK, "项目主页",
            "%s — 源码 / 文档 / 问题反馈" % cm.REPO_URL, parent=group5)
        c_repo.clicked.connect(lambda: cm.open_url(cm.REPO_URL))
        group5.addSettingCard(c_ver)
        group5.addSettingCard(c_repo)
        lay.addWidget(group5)
        lay.addStretch(1)

    def update_hotkey_label(self):
        """热键变更成功后由应用层(hotkey_change 事件)回写当前组合显示。"""
        self.lbl_hotkey.setText(cm.hotkey_label(
            self.app.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
            self.app.cfg.get("hotkey_key", 0x56)))

    def update_quick_label(self):
        """快捷粘贴浮层热键变更后(qp_hotkey_change/启动注册结果)回写显示。"""
        self.lbl_quick.setText(cm.hotkey_label(
            self.app.cfg.get("quick_mod", cm.MOD_ALT),
            self.app.cfg.get("quick_key", 0xC0)))

    def sync_monitor_pause(self):
        """托盘菜单切换暂停监听后,设置页开关同步显示(值未变不发信号,无回环)。"""
        self.c_pause.setChecked(bool(getattr(self.app.core, "monitor_paused", False)))

    def _refresh_excl_label(self):
        n = len(self.app.cfg.get("exclude_apps") or [])
        self.lbl_excl.setText("已排除 %d 个应用" % n if n else "未排除任何应用")

    def _run_cleanup(self):
        """清理策略变更后立即执行一次(核心会按新配置裁剪并置脏落盘)。"""
        n, freed = self.app.core.apply_cleanup()
        if n:
            self.app.refresh_view()
            InfoBar.success("自动清理", "删除 %d 条,释放 %s"
                            % (n, cm._fmt_size(freed)),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)

    # ---- 诊断(建议 19/29;数据层见 diagnostics.py) ----
    def _copy_diagnostics(self):
        """复制诊断信息:轻量快照,仅元数据(不含内容与密钥)。"""
        try:
            text = diag.format_diagnostics(
                diag.collect_diagnostics(self.app.core))
            self.app.core.copy_text(text)
            InfoBar.success("诊断信息已复制",
                            "共 %d 行,可直接粘贴给问题反馈"
                            % len(text.splitlines()),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        except Exception:
            cm.log("复制诊断信息失败:\n%s" % traceback.format_exc())
            InfoBar.error("复制失败", "详情见 app.log", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)

    def _run_health_check(self):
        """数据健康检查(blob 目录扫描在后台线程,完成后回 UI 线程)。"""
        core = self.app.core

        def _work():
            try:
                res = diag.run_health_check(core)
            except Exception:
                cm.log("健康检查异常:\n%s" % traceback.format_exc())
                res = {"ok": False, "results": [
                    {"name": "检查异常", "ok": False,
                     "detail": traceback.format_exc(limit=3)}]}
            self._health_done.emit(res)

        threading.Thread(target=_work, daemon=True,
                         name="health-check").start()

    def _show_health_result(self, res):
        """健康检查结果:InfoBar 概要 + MessageBox 明细。"""
        results = res.get("results") or []
        lines = []
        for r in results:
            mark = "✓" if r["ok"] is True else \
                ("✗" if r["ok"] is False else "—")
            extra = ("  %s" % r["detail"]) if r.get("detail") else ""
            lines.append("%s %s%s" % (mark, r["name"], extra))
        bad = sum(1 for r in results if r["ok"] is False)
        if res.get("ok"):
            InfoBar.success("数据健康检查", "全部通过(共 %d 项)" % len(results),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.warning("数据健康检查", "发现 %d 项问题,详见详情" % bad,
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        MessageBox("数据健康检查", "\n".join(lines) or "无结果", self).exec()

    def _rebuild_fts(self):
        """重建全文索引(JSON 模式无索引,提示后返回)。"""
        if self.app.core.store is None:
            InfoBar.warning("重建搜索索引", "JSON 模式无全文索引", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        try:
            self.app.core.store.rebuild_fts()
            InfoBar.success("重建搜索索引", "全文索引已整体重建", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
        except Exception:
            cm.log("重建搜索索引失败:\n%s" % traceback.format_exc())
            InfoBar.error("重建搜索索引", "失败,详情见 app.log", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)

    def _clean_orphan_caches(self):
        """手动触发一次孤儿清理(与启动期 +3s 的自动清理同一入口/规则)。"""
        removed = self.app.core.cleanup_orphan_images()
        InfoBar.success("清理孤立缓存",
                        "删除 %d 个引用计数为零的文件" % removed if removed
                        else "无孤立文件",
                        parent=self, position=InfoBarPosition.TOP_RIGHT)
        if removed:
            self.app.refresh_view()

    # ---- 备份与迁移 ----
    def _export_history(self):
        """导出历史:先选是否内嵌图片,再选保存路径。"""
        dlg = MessageBoxBase(self)
        ttl = StrongBodyLabel("导出历史")
        dlg.viewLayout.addWidget(ttl)
        sub = CaptionLabel("文本/文件条目始终导出(含富文本与来源应用)。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        dlg.viewLayout.addWidget(sub)
        chk_img = CheckBox("内嵌图片(base64,文件明显变大)")
        dlg.viewLayout.addWidget(chk_img)
        dlg.yesButton.setText("选择保存位置…")
        dlg.cancelButton.setText("取消")
        dlg.widget.setFixedWidth(400)
        if not dlg.exec():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史",
            os.path.join(os.path.expanduser("~"), "Desktop",
                         "winclipboard_export.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            payload = self.app.core.export_payload(bool(chk_img.isChecked()))
            Path(path).write_text(json.dumps(payload, ensure_ascii=False),
                                  encoding="utf-8")
        except Exception:
            cm.log("导出历史失败:\n%s" % traceback.format_exc())
            InfoBar.error("导出失败", "详情见日志(%s)" % cm.LOG_FILE, parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        InfoBar.success("导出完成",
                        "%d 条 → %s" % (len(payload["items"]), path),
                        parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _export_portable(self):
        """可迁移加密备份:密码 → PBKDF2 → AES-256-GCM,跨电脑可恢复。
        私密区默认不包含(勾选后只存在于加密容器内)。"""
        if not cm._AES_OK:
            InfoBar.error("功能不可用",
                          "未安装 cryptography 库(pip install cryptography)",
                          parent=self, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
            return
        dlg = MessageBoxBase(self)
        dlg.viewLayout.addWidget(StrongBodyLabel("导出加密备份(可迁移)"))
        sub = CaptionLabel("密码经 PBKDF2 派生密钥,AES-256-GCM 认证加密;"
                           "拿到任何电脑上用同一密码即可恢复。密码丢失无法找回。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        dlg.viewLayout.addWidget(sub)
        pw1 = PasswordLineEdit()
        pw1.setPlaceholderText("设置备份密码")
        pw2 = PasswordLineEdit()
        pw2.setPlaceholderText("再输入一次")
        chk_img = CheckBox("内嵌图片(base64,文件明显变大)")
        chk_priv = CheckBox("包含私密区(仅存在于加密容器内)")
        for w in (pw1, pw2, chk_img, chk_priv):
            dlg.viewLayout.addWidget(w)
        dlg.yesButton.setText("选择保存位置…")
        dlg.cancelButton.setText("取消")
        dlg.widget.setFixedWidth(420)

        def _valid():
            ok = (len(pw1.text()) >= 4 and pw1.text() == pw2.text())
            if not ok and dlg.yesButton.isEnabled():
                InfoBar.warning("密码无效", "至少 4 位,且两次输入一致",
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
            return ok

        dlg.yesButton.setEnabled(True)
        if not dlg.exec() or not _valid():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出加密备份",
            os.path.join(os.path.expanduser("~"), "Desktop",
                         "clipboard-backup-%s.wcbak"
                         % time.strftime("%Y%m%d")),
            "WinClipboard 加密备份 (*.wcbak)")
        if not path:
            return
        try:
            payload = self.app.core.export_portable_payload(
                bool(chk_img.isChecked()), bool(chk_priv.isChecked()))
            Path(path).write_bytes(
                cm.portable_backup_bytes(payload, pw1.text()))
        except Exception:
            cm.log("导出加密备份失败:\n%s" % traceback.format_exc())
            InfoBar.error("导出失败", "详情见日志(%s)" % cm.LOG_FILE, parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        InfoBar.success("加密备份完成",
                        "%d 条(+%d 私密)→ %s"
                        % (len(payload["items"]), len(payload["private"]), path),
                        parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _import_history(self):
        """导入历史:合并去重,图片落盘重建,时间/置顶还原。
        加密备份(.wcbak)自动识别并要求输入密码。"""
        path, _ = QFileDialog.getOpenFileName(self, "导入历史/备份", "",
                                              "WinClipboard 备份 (*.wcbak *.json);;"
                                              "所有文件 (*.*)")
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            if raw[:8] == b"WCLBBAK1":
                payload = self._ask_portable_password(raw)
                if payload is None:
                    return
                added, skipped, n_priv = \
                    self.app.core.import_portable_payload(payload)
            else:
                payload = json.loads(raw.decode("utf-8"))
                added, skipped = self.app.core.import_payload(payload)
                n_priv = 0
        except Exception as exc:
            cm.log("导入历史失败:\n%s" % traceback.format_exc())
            InfoBar.error("导入失败", str(exc)[:160], parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        self.app.refresh_view()
        msg = "新增 %d 条,跳过 %d 条(重复或文件已不存在)" % (added, skipped)
        if n_priv:
            msg += ";私密区新增 %d 条" % n_priv
        InfoBar.success("导入完成", msg, parent=self,
                        position=InfoBarPosition.TOP_RIGHT)

    def _ask_portable_password(self, raw):
        """解密对话框:密码错误可重试,取消返回 None。"""
        for _ in range(3):
            dlg = MessageBoxBase(self)
            dlg.viewLayout.addWidget(StrongBodyLabel("输入备份密码"))
            sub = CaptionLabel("该备份使用 AES-256-GCM 认证加密,"
                               "密码错误会明确提示。")
            sub.setStyleSheet("color: %s;" % TER)
            dlg.viewLayout.addWidget(sub)
            pw = PasswordLineEdit()
            pw.setPlaceholderText("备份密码")
            dlg.viewLayout.addWidget(pw)
            dlg.yesButton.setText("解密")
            dlg.cancelButton.setText("取消")
            dlg.widget.setFixedWidth(400)
            if not dlg.exec():
                return None
            try:
                return cm.portable_backup_parse(raw, pw.text())
            except ValueError as e:
                InfoBar.error("解密失败", str(e)[:120], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=4000)
        return None

    def _edit_exclusions(self):
        ExcludeAppsDialog(self, self.app).exec()
        self._refresh_excl_label()


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
            if self.editor._layer_mode:
                # 压缩产物是整图重编码:以新底图回写,标注层清空(已含在像素里)
                self.editor._emit_layers(base_override=img, annos_override=[])
            else:
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

    def __init__(self, app, rec, on_result=None, parent=None,
                 annos=None, layer_mode=False):
        super().__init__(parent)
        self.app = app
        self.rec = rec
        self._on_result = on_result     # 编辑产出回调(图钉画布回写用);None=普通编辑
        self._layer_mode = layer_mode   # 非破坏层回写(emit annotations 而非像素)
        self._src_path = str(rec.get("content") or "")
        self._base_dirty = False        # 裁剪/旋转/缩放等底图操作(需替换资产)
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
        if annos:
            self.load_annotations(annos)   # 图钉画布回编:标注层原样恢复

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
            it._factor = 12              # 块状因子(序列化用,重生成靠原图)
        else:
            return
        it._kind = kind                  # 序列化标记(annotations.serialize_items)
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        self.annos.append(it)
        self._record({"type": "add", "item": it})
        self._status()

    @staticmethod
    def _arrow_path(p0, p1):
        return _anno_layer.arrow_path(p0, p1)

    def _mosaic_patch(self, r, factor=12):
        """区域马赛克:从原图裁剪 → 缩小 → 放大(块状),返回像素化贴片。
        只存区域+因子(见 annotations),贴片每次从底图现算。"""
        rect = r.toAlignedRect().intersected(
            QRect(0, 0, self.base.width, self.base.height))
        if rect.width() < 2 or rect.height() < 2:
            return None
        region = self.base.crop((rect.x(), rect.y(),
                                 rect.right() + 1, rect.bottom() + 1))
        small = region.resize((max(1, region.width // factor),
                               max(1, region.height // factor)), cm.RESAMPLE)
        pix = small.resize(region.size, cm.Image.NEAREST)
        return QPixmap.fromImage(pil_to_qimage(pix))

    # ---- 标注层序列化(非破坏模型:矢量参数,不含派生像素) ----
    def annotations(self):
        """当前标注 → 可持久化 dict 列表(图像像素坐标)。"""
        return _anno_layer.serialize_items(self.annos)

    def load_annotations(self, lst):
        """恢复标注层(图钉画布回编):逐条重建为可交互编辑项。"""
        loaded = []
        for d in _anno_layer.sanitize(lst):
            it = self._restore_anno(d)
            if it is not None:
                self.annos.append(it)
                loaded.append(it)
        if loaded:
            self._status()
        return loaded

    def _restore_anno(self, d):
        t = d.get("t")
        col = QColor(d.get("c") or "#FF4040")
        pw = float(d.get("pw") or 4.0)

        def pen():
            p = QPen(col, pw)
            p.setCapStyle(Qt.RoundCap)
            p.setJoinStyle(Qt.RoundJoin)
            return p

        if t in ("rect", "ellipse"):
            r = QRectF(d["x"], d["y"], d["w"], d["h"])
            it = AnnoRect(r) if t == "rect" else AnnoEllipse(r)
            it.setPen(pen())
            it.setBrush(Qt.NoBrush)
        elif t == "arrow":
            it = AnnoPath(self._arrow_path(
                QPointF(0, 0),
                QPointF(d["x2"] - d["x1"], d["y2"] - d["y1"])))
            it.setPos(d["x1"], d["y1"])
            p = pen()
            p.setCosmetic(False)
            it.setPen(p)
            it.setBrush(col)
        elif t == "pen":
            pts = d["pts"]
            path = QPainterPath(QPointF(0, 0))
            for p in pts[1:]:
                path.lineTo(p[0] - pts[0][0], p[1] - pts[0][1])
            it = AnnoPath(path)
            it.setPos(pts[0][0], pts[0][1])
            it.setPen(pen())
            it.setBrush(Qt.NoBrush)
        elif t == "text":
            it = AnnoText(str(d.get("s") or ""))
            f = QFont(self.font())
            f.setPixelSize(int(d.get("px") or 26))
            f.setBold(True)
            it.setFont(f)
            it.setDefaultTextColor(col)
            it.setPos(d["x"], d["y"])
            it.setTextInteractionFlags(Qt.NoTextInteraction)
        elif t == "mosaic":
            pm = self._mosaic_patch(
                QRectF(d["x"], d["y"], d["w"], d["h"]), int(d.get("f") or 12))
            if pm is None:
                return None
            it = AnnoMosaic(pm)
            it.setTransformationMode(Qt.FastTransformation)
            it.setPos(d["x"], d["y"])
            it._factor = int(d.get("f") or 12)
        else:
            return None
        it._kind = t
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        return it

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
        """把全部标注栅格化进位图(变换/裁剪前),返回旧 (base, annos) 供撤销。
        栅格化即底图变更:非破坏层回写时需替换资产。"""
        old = (self.base, list(self.annos))
        if self.annos:
            self._clear_preview()
            self.base = self._final_image()
            for it in self.annos:
                self.scene.removeItem(it)
            self.annos = []
        self._base_dirty = True
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
        self._base_dirty = True
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

    def _emit_layers(self, base_override=None, annos_override=None):
        """非破坏层回写(图钉画布):标注层 + 原图引用;只有底图被
        裁剪/旋转等破坏性操作改写过时才携带新底图像素。"""
        if not self._layer_mode or self._on_result is None:
            return
        base = base_override
        if base is None and self._base_dirty:
            base = self.base               # 现底图(此前的标注已在破坏性操作时栅格化)
        annos = self.annotations() if annos_override is None else annos_override
        self._emit_result({"kind": "layers", "image": self._src_path,
                           "annotations": annos, "base": base})

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
        if self._layer_mode:
            self._emit_layers()
        else:
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
                if self._layer_mode:
                    self._emit_layers()    # 画布走非破坏层;文件是合成导出
                else:
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
        # 用户类型纠正优先于自动识别(右键「类型」设置,仅本条目生效)
        _ov = rec.get("type_override") if self._kind == "text" else None
        if _ov and self._content is not None:
            self._content = self._content._replace(subtype=_ov)
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
        # 查看器「复制」只写剪贴板:不排回贴队列,避免内容被 Ctrl+V
        # 打进打开查看器之前聚焦的窗口(私密凭据与非私密一视同仁)
        self.app.core.copy_text(self._raw)
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


class DiffWindow(QWidget):
    """内容对比窗口:unified 文本 diff + JSON 语义 diff(键序无关)。

    入口:历史面板 Ctrl+点击选基准 → Ctrl+点击另一条;或卡片菜单
    「选为对比基准 / 与基准对比」。A=基准(旧),B=所选(新);JSON 双方
    都合法时默认语义视图,否则回退文本 unified diff。"""

    def __init__(self, app, rec_a, rec_b):
        super().__init__(None)
        self.app = app
        self.ra, self.rb = rec_a, rec_b
        self.ta = self._text_of(rec_a)
        self.tb = self._text_of(rec_b)
        self._json_ok, self._json_changes = cm.json_semantic_diff(self.ta, self.tb)
        self._diff_lines = cm.unified_diff_lines(self.ta, self.tb)

        self.setWindowTitle("对比 · WinClipboard")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(920, 620)
        self.setMinimumSize(660, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QVBoxLayout()
        head.setContentsMargins(16, 14, 16, 6)
        head.setSpacing(2)
        head.addWidget(StrongBodyLabel("内容对比"))
        self.lbl_meta = CaptionLabel("")
        head.addWidget(self.lbl_meta)
        root.addLayout(head)

        bar = QHBoxLayout()
        bar.setContentsMargins(16, 2, 16, 8)
        bar.setSpacing(8)
        self.seg = SegmentedWidget()
        self.seg.currentItemChanged.connect(
            lambda k: self._set_view(k) if k else None)
        bar.addWidget(self.seg)
        bar.addStretch(1)
        self.btn_copy = PushButton("复制结果")
        self.btn_copy.setIcon(FIF.COPY.icon())
        self.btn_copy.clicked.connect(self._copy_result)
        bar.addWidget(self.btn_copy)
        root.addLayout(bar)

        body = QHBoxLayout()
        body.setContentsMargins(16, 2, 16, 10)
        card = SimpleCardWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 0, 0, 0)
        self.ed = _CodeEdit(card)
        self.ed.setReadOnly(True)
        self.ed.setStyleSheet(
            "QPlainTextEdit { background: %s; border: none; color: %s; }"
            % (CARD_BG, TEXT))
        cv.addWidget(self.ed)
        body.addWidget(card)
        root.addLayout(body, 1)

        self._mode = "sem" if self._json_ok else "text"
        self._rebuild_modes()
        self._render()

    @staticmethod
    def _text_of(rec):
        c = rec.get("content")
        if isinstance(c, list):
            return "\n".join(str(p) for p in c)
        return str(c or "")

    def _stamp(self, rec):
        ts = rec.get("ts")
        if not ts:
            return "?"
        t = time.localtime(ts)
        return time.strftime("%m-%d %H:%M", t)

    def _rebuild_modes(self):
        self.seg.blockSignals(True)
        self.seg.clear()
        if self._json_ok:
            self.seg.addItem(routeKey="sem", text="语义")
        self.seg.addItem(routeKey="text", text="文本")
        self.seg.setCurrentItem(self._mode)
        self.seg.blockSignals(False)

    def _set_view(self, key):
        if key and key != self._mode:
            self._mode = key
            self._render()

    def _sem_lines(self):
        ch = self._json_changes
        if not ch:
            return ["两个 JSON 语义相同(键顺序与空白无关)。"]
        lines = []
        for path, kind, old, new in ch:
            if kind == "change":
                lines.append("~ %s:  %s  →  %s" % (path, old, new))
            elif kind == "add":
                lines.append("+ %s:  %s" % (path, new))
            elif kind == "del":
                lines.append("- %s:  %s" % (path, old))
            else:
                lines.append(str(old))
        return lines

    def _render(self):
        lines = self._sem_lines() if self._mode == "sem" else self._diff_lines
        text = "\n".join(lines)
        self.ed.setPlainText(text)
        # 逐行配色:前缀 +/- 上色,@@ 与 ---/+++ 说明行置灰
        fa = QTextCharFormat()
        fa.setForeground(QColor(DIFF_ADD))
        fd = QTextCharFormat()
        fd.setForeground(QColor(DIFF_DEL))
        fc = QTextCharFormat()
        fc.setForeground(QColor(DIFF_CTX))
        runs = []
        pos = 0
        for ln in lines:
            n = len(ln) + 1
            c = ln[:1]
            if c == "+":
                runs.append((pos, pos + n, fa))
            elif c == "-":
                runs.append((pos, pos + n, fd))
            elif c in ("~", "@", "…") or ln.startswith(("---", "+++")):
                runs.append((pos, pos + n, fc))
            pos += n
        apply_format_runs(self.ed.document(), runs)
        n_add = sum(1 for l in self._diff_lines if l.startswith("+") and not l.startswith("+++"))
        n_del = sum(1 for l in self._diff_lines if l.startswith("-") and not l.startswith("---"))
        if self._mode == "sem":
            detail = "语义差异 %d 处" % len(self._json_changes) \
                if self._json_changes else "语义相同"
        else:
            detail = "+%d / -%d 行" % (n_add, n_del)
        self.lbl_meta.setText(
            "A(基准) %s · %s  ↔  B(所选) %s · %s · %s" % (
                self._stamp(self.ra), cm._fmt_size(len(self.ta.encode("utf-8"))),
                self._stamp(self.rb), cm._fmt_size(len(self.tb.encode("utf-8"))),
                detail))

    def _copy_result(self):
        self.app.core.copy_text(self.ed.toPlainText())
        self.btn_copy.setText("已复制")
        QTimer.singleShot(1400, lambda: self.btn_copy.setText("复制结果"))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        try:
            self.app._diffs.remove(self)
        except (ValueError, AttributeError):
            pass
        self.deleteLater()
        e.accept()


class ImageViewerWindow(QWidget):
    """图片查看器(独立窗口,与文本查看器同族):适应窗口 / 1:1 / 滚轮缩放 /
    拖拽平移;双击在 适应↔原始 间切换。「编辑标注」在本窗口内切换到内嵌
    编辑器(不另开窗口),完成/Esc 自动回到查看态。"""

    def __init__(self, app, rec, edit=False, on_result=None,
                 annos=None, layer_mode=False):
        super().__init__(None)
        self.app = app
        self.rec = rec
        self._on_result = on_result       # 编辑产物直通回调(图钉画布回写)
        self._init_annos = annos          # 初始标注层(图钉画布回编继续编辑)
        self._layer_mode = layer_mode     # 非破坏回写(emit 标注层而非像素)
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
                         on_result=self._edited, parent=self,
                         annos=self._init_annos, layer_mode=self._layer_mode)
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
    循环假死——窗口在、定时器与消息全部不跑。布防后事件循环在 GRACE_S
    内打卡一次即视为健康(打卡是循环定时器,启动高峰忙过头也能随后补打,
    只有派发器真死了才会整窗超时);超时才无 Mica 自重启:仅本次会话、
    不改配置(本地桌面下次照常启用),WCLB_NO_MICA 环境变量防重启循环。

    历史坑:早期版本是一次性 2500ms 心跳 + 6s 判决,启动预热变重后心跳
    被排队到判决之后,健康实例每次冷启动都被误杀再无 Mica 重启一遍。

    触发时把判决时长 / 远程会话 / Mica 状态写入日志——这个 workaround
    到底多久救一次场有据可查,不会变成「不敢删但不知道有没有用」的遗物。
    """

    HEARTBEAT_MS = 2500          # 心跳循环定时器周期
    GRACE_S = 30.0               # 宽限判决时限:整窗无任何心跳才判死

    def __init__(self, app):
        self.app = app
        self.beat = None          # None=未打卡;打卡时刻(monotonic)
        self._armed = False
        self._beat_timer = None

    def arm(self):
        """布防(初始化尾部调用一次,幂等);仅 Mica 生效且未设禁用环境变量时。"""
        if self._armed:
            return
        self._armed = True
        if not getattr(self.app, "_mica_active", False) \
                or os.environ.get("WCLB_NO_MICA"):
            return
        t0 = time.monotonic()
        self._beat_timer = QTimer(self.app)
        self._beat_timer.setInterval(self.HEARTBEAT_MS)
        self._beat_timer.timeout.connect(self._heartbeat)
        self._beat_timer.start()
        threading.Thread(target=self._watch, args=(t0,), daemon=True).start()

    def _heartbeat(self):
        self.beat = time.monotonic()

    def _watch(self, t0):
        deadline = t0 + self.GRACE_S
        while time.monotonic() < deadline:
            if self.beat is not None:
                return                    # 事件循环活着(哪怕打卡晚到)
            time.sleep(0.5)
        cm.log("UI 健康监控:事件循环 %.0fs 无心跳,疑似 Mica 致假死,无 Mica 自重启"
               "(remote_session=%s mica=%s)"
               % (self.GRACE_S, cm.is_remote_session(),
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
                    # 256px 缓存:卡片(~92px)与底部预览(≤128px 高)共用,
                    # 2x 余量;原图只在双击查看器时解码
                    self.loaded.emit(path, img.scaled(
                        256, 256, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation))
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
        qw = getattr(self.app, "quick", None)      # 快捷粘贴浮层可见则重绘
        if qw is not None:
            try:
                qw.on_thumb(path, pm)
            except Exception:
                pass


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


class PrivacyGateDialog(MessageBoxBase):
    """AI 发送前隐私门:提示词含敏感内容时三选一(人在回路)。

    [自动遮盖后发送](主按钮) [本次原样发送] [取消]——关闭窗口/Esc 视为取消。
    choice ∈ {"mask", "raw", "cancel"};遮盖文本由 cm.privacy_scan 产生。"""

    def __init__(self, parent, hits, masked_preview, endpoint):
        super().__init__(parent)
        self.choice = "cancel"

        title = StrongBodyLabel("⚠ 内容可能包含敏感信息")
        title.setStyleSheet("color: #C4820A;")
        self.viewLayout.addWidget(title)
        body = BodyLabel("即将发送至 %s 的提示词中检测到:" % (endpoint or "AI 服务"))
        body.setStyleSheet("color: %s;" % TEXT)
        self.viewLayout.addWidget(body)
        for lb in hits:
            row = BodyLabel("•  %s" % lb)
            row.setStyleSheet("color: #C4820A; padding-left: 12px;")
            self.viewLayout.addWidget(row)
        prev = CaptionLabel("遮盖预览:%s" % (masked_preview[:160] + ("…" if len(masked_preview) > 160 else "")))
        prev.setWordWrap(True)
        prev.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(prev)

        self.btn_raw = PushButton("本次原样发送", self)
        self.btn_raw.clicked.connect(lambda: self._pick("raw"))
        self.buttonLayout.insertWidget(0, self.btn_raw)
        self.buttonLayout.insertStretch(1)
        self.yesButton.setText("自动遮盖后发送")
        self.yesButton.clicked.connect(lambda: self._pick("mask"))
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(460)

    def _pick(self, choice):
        self.choice = choice
        self.accept()


class UpdateWorker(QThread):
    """后台检查 GitHub 最新 Release(cm.fetch_latest_release 阻塞 8s 上限)。"""
    done = Signal(str, str, str)     # 状态, 版本, 链接/原因

    def run(self):
        self.done.emit(*cm.fetch_latest_release())


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

        hint = CaptionLabel("步骤类型:本地 AI(API 调用)、网页 AI(提示词进剪贴板,可选自动打开网页)、"
                            "人工确认(粘贴/编辑后继续)、复制到剪贴板、文本变换(本地白名单操作)、"
                            "条件判断(命中正则继续,未命中停链)。提示词占位符:{input}/{previous} = "
                            "上一步产出(首步=原始输入),{origin} = 原始输入。")
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

    # 种类下拉来自 Step Contract 注册表((label, kind) 序,索引约定沿用)
    _KINDS = tuple((label, kind) for kind, label in wfs.EDITOR_KINDS)

    def _add_row(self, step):
        step = step or {}
        kind = wfs.KIND_ALIASES.get(str(step.get("kind", "ai")).lower(),
                                     step.get("kind", "ai"))
        if kind not in (k for _, k in self._KINDS):
            kind = "ai"
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

        # 变换/条件步骤的参数行(其余类型隐藏)
        params = QHBoxLayout()
        op_combo = ComboBox()
        op_combo.addItems([wfs.TransformStep.OP_LABELS[o]
                           for o in wfs.TransformStep.OPS])
        op0 = step.get("op") or "trim"
        if op0 in wfs.TransformStep.OPS:
            op_combo.setCurrentText(wfs.TransformStep.OP_LABELS[op0])
        op_combo.setMinimumWidth(110)
        arg1 = LineEdit()
        arg1.setText(str(step.get("arg1") or step.get("pattern") or ""))
        arg2 = LineEdit()
        arg2.setText(str(step.get("arg2") or ""))
        params.addWidget(op_combo)
        params.addWidget(arg1, 1)
        params.addWidget(arg2, 1)
        col.addLayout(params)

        prompt = TextEdit()
        prompt.setPlaceholderText("提示词 / 给操作者的说明:{input}/{previous} 为上一步产出(首步=原始输入),{origin} 为原始输入")
        prompt.setPlainText(step.get("prompt", ""))
        prompt.setFont(font(12))
        prompt.setFixedHeight(84)
        col.addWidget(prompt)

        row_ref = {"frame": frame, "kind": kind_combo, "combo": combo,
                   "url": url_edit, "prompt": prompt, "nlab": nlab,
                   "op": op_combo, "arg1": arg1, "arg2": arg2}

        def sync_kind():
            k = self._KINDS[kind_combo.currentIndex()][1]
            combo.setVisible(k == "ai")
            url_edit.setVisible(k == "web")
            op_combo.setVisible(k == "transform")
            arg1.setVisible(k in ("transform", "if"))
            arg2.setVisible(k == "transform")
            prompt.setVisible(k != "clipboard")
            if k == "transform":
                arg1.setPlaceholderText("查找内容(替换的原文 / 正则模式)")
                arg2.setPlaceholderText("替换为 / 加后缀内容")
            elif k == "if":
                arg1.setPlaceholderText("正则:当前内容命中则继续,未命中则停止自动串联")
            prompt.setPlaceholderText(
                "给操作者的说明(可选),显示在等待面板" if k == "human"
                else "提示词,{input}/{previous} 为上一步产出,{origin} 为原始输入")
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
            if k == "ai":
                svc = r["combo"].currentText().strip()
                if not svc:
                    InfoBar.error("无法保存", "本地 AI 步骤需要先在「AI 服务」中添加服务",
                                  parent=self, position=InfoBarPosition.TOP_RIGHT)
                    return
                steps.append({"kind": "ai", "service": svc, "prompt": prompt})
            elif k == "web":
                steps.append({"kind": "web", "prompt": prompt,
                              "url": r["url"].text().strip()})
            elif k == "human":
                steps.append({"kind": "human", "prompt": prompt})
            elif k == "clipboard":
                steps.append({"kind": "clipboard"})
            elif k == "transform":
                steps.append(wfs.TransformStep(
                    wfs.TransformStep.OPS[r["op"].currentIndex()],
                    r["arg1"].text(), r["arg2"].text()).to_dict())
            else:                                              # if
                steps.append({"kind": "if", "pattern": r["arg1"].text().strip()})
        wflist = self.app.cfg.setdefault("ai_workflows", [])
        if self.wf is not None:
            self.wf.clear()
            self.wf.update({"name": name, "steps": steps})
        else:
            if any(w.get("name") == name for w in wflist):
                InfoBar.error("名称重复", "已存在同名工作流:%s" % name, parent=self,
                              position=InfoBarPosition.TOP_RIGHT)
                return
            wflist.append({"name": name, "steps": steps})
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
        b_hist = PushButton("运行记录", self)
        b_hist.setIcon(FIF.HISTORY.icon())
        b_svc = PushButton("AI 服务…", self)
        b_svc.setIcon(FIF.CLOUD.icon())
        b_new.clicked.connect(self._new)
        b_edit.clicked.connect(self._edit_current)
        b_del.clicked.connect(self._del_current)
        b_seed.clicked.connect(self._seed)
        b_hist.clicked.connect(self._open_runs)
        b_svc.clicked.connect(self.app.show_services)
        for b in (b_new, b_edit, b_del, b_seed, b_hist, b_svc):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._editors = []
        self._runs_dlg = None
        self.refresh()

    def _open_runs(self):
        """运行记录(Run Inspector):每步大小/耗时/状态 + 运行历史。"""
        if self._runs_dlg is None:
            self._runs_dlg = WorkflowRunInspector(self.app)
        self._runs_dlg._reload()
        self._runs_dlg.show()
        self._runs_dlg.raise_()
        self._runs_dlg.activateWindow()


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
    """工作流运行台:步骤经 workflow_steps 契约分发(ai/web/human/
    clipboard/transform/if,见 STEP_REGISTRY)。

    web:提示词进剪贴板(可自动打开网页),等人粘贴回答;
    human:等待面板人工确认/编辑;ai:本地 API 自动调用;
    clipboard:快照当前内容;transform:本地文本变换;if:条件闸。
    所有步骤的产出都可编辑后再继续(人在回路)。
    """

    def __init__(self, app, wf, content):
        super().__init__(None)
        self.app = app
        self.wf = wf
        self.steps = wf.get("steps") or []
        self.step_idx = 0
        self._origin = content          # 工作流原始输入({origin})
        self.cur = content                 # 链上当前内容(可能是人工编辑后的)
        self.results = []                  # 每步产出的文本
        self._worker = None
        self._waiting = None               # 等待输入面板(卡部件)
        self._wait_body = None
        self._wait_confirm = None
        self._dead = False
        self._auto = False
        self._ctx = None                   # WorkflowContext(惰性,宿主回调注入)
        self._cur_step = None              # 当前步骤实例(取消用)
        # 运行记录(Run Inspector):只记大小/耗时/状态,正文不落盘;
        # 关闭窗口或跑完全程时收尾保存(见 _maybe_finish_runlog/closeEvent)
        self._runlog = wrl.RunRecorder(wf.get("name", ""), content,
                                       len(self.steps))
        self._runlog_saved = False

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
        return wfs.describe(step)

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
            self._runlog.step_ok(idx, text)
            self._waiting = None
            self._wait_body = None
            self._wait_confirm = None
            if self.step_idx >= len(self.steps):
                self._set_busy(False, "全部 %d 步完成,结果可复制" % len(self.steps))
                InfoBar.success("工作流完成", "共 %d 步,最新结果已可复制" % len(self.steps),
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
                self._maybe_finish_runlog()
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
                if self._cur_step is not None:
                    self._cur_step.cancel()   # 契约取消:迟到结果作废
                self._set_busy(False, "已停止:当前步骤结果将被丢弃")
            return
        self._run_step()

    def _on_auto(self):
        self._auto = True
        self._run_step()

    # ---- Step Contract 宿主侧:上下文与回调 ----
    def _ensure_ctx(self):
        if self._ctx is not None:
            return
        self._ctx = wfs.WorkflowContext(
            input_text=self._origin,
            find_service=lambda name: cm.ai_service_by_name(self.app.cfg, name),
            privacy_gate=self._privacy_gate,
            start_ai=self._start_ai,
            copy_text=self.app.core.copy_text,
            open_url=self._open_url)

    def _open_url(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
            return True
        except Exception:
            return False

    def _start_ai(self, svc, prompt, on_done):
        """起 AIWorker 线程;返回句柄(cancel 为尽力而为:HTTP 不可中断,
        迟到结果由 Job.cancelled 丢弃)。"""
        w = AIWorker(svc, prompt, self.step_idx)
        w.done.connect(lambda _i, ok, text: on_done(ok, text))
        w.finished.connect(self._worker_finished)
        self._worker = w
        w.start()
        return w

    def _run_step(self):
        if self._worker is not None or self._waiting is not None \
                or self.step_idx >= len(self.steps):
            return
        idx = self.step_idx
        self._ensure_ctx()
        self._ctx.index = idx
        self._ctx.previous = self.cur
        step = wfs.deserialize(self.steps[idx])
        self._cur_step = step
        self._runlog.step_start(idx, step.kind, step.describe(), self.cur)

        err = step.validate(self._ctx)          # 契约:运行前检查
        if err is not None:
            self._auto = False
            self._runlog.step_error(idx, "配置无效: " + err)
            InfoBar.error("无法运行", err, parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=-1)
            self._set_busy(False, "步骤 %d 配置无效,已停止" % (idx + 1))
            return

        res = step.execute(self._ctx)           # 契约:执行
        if isinstance(res, wfs.Cancelled):
            self._auto = False
            self._runlog.step_cancelled(idx, res.reason)
            self._set_busy(False, "步骤 %d 已取消(%s)" % (idx + 1, res.reason))
            return
        if isinstance(res, wfs.Wait):
            if res.note is not None:
                self._add_block("步骤 %d · %s" % (idx + 1, res.note[0]),
                                res.note[1], "info")
            self._show_wait_panel(idx, res.hint, res.prefill)
            return
        if isinstance(res, wfs.Job):
            self._set_busy(True, "步骤 %d 正在执行(%s)…" % (idx + 1, step.describe()))
            res.start(lambda ok, text, _i=idx: self._on_done(_i, ok, text))
            return
        if isinstance(res, wfs.Done):           # 同步步骤(clipboard/transform/if)
            if res.note is not None:
                self._add_block("步骤 %d · %s" % (idx + 1, res.note[0]),
                                res.note[1], "info")
            self._step_complete(idx, res.text, halt=res.halt)
            if self._auto and self.step_idx < len(self.steps):
                self._run_step()                # 自动模式:同步步骤直接链推

    def _step_complete(self, idx, text, halt=False):
        """一步成功收尾:记录产出、推进游标、刷新界面(halt=条件停链)。"""
        self.results.append(text)
        self.cur = text
        self.step_idx = idx + 1
        self._runlog.step_ok(idx, text, halt=halt)
        self._add_block("步骤 %d · %s" % (idx + 1, self._step_label(self.steps[idx])),
                        text, "result", idx)
        if self.step_idx >= len(self.steps):
            self._set_busy(False, "全部 %d 步完成,结果可复制" % len(self.steps))
            InfoBar.success("工作流完成", "共 %d 步,最新结果已可复制" % len(self.steps),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
            self._maybe_finish_runlog()
        elif halt:
            self._auto = False
            self._set_busy(False, "步骤 %d 条件不满足,自动串联已停(可手动继续)" % (idx + 1))
        elif not self._auto:
            self._set_busy(False, "步骤 %d 完成;可编辑结果或直接继续" % idx)

    def _maybe_finish_runlog(self):
        """全部步骤走完:收尾运行记录并落盘(只记大小/耗时/状态)。"""
        if self._runlog_saved:
            return
        self._runlog_saved = True
        try:
            wrl.save_run(self._runlog.finish())
        except Exception:
            cm.log("运行记录保存失败:\n%s" % traceback.format_exc())

    def _privacy_gate(self, prompt, svc):
        """AI 发送前隐私门:命中敏感内容弹三选一(遮盖/原样/取消);
        无命中原样放行。返回最终提示词,取消返回 None。"""
        hits, masked = cm.privacy_scan(prompt)
        if not hits:
            return prompt
        dlg = PrivacyGateDialog(self, hits, masked,
                                str(svc.get("base_url") or ""))
        dlg.exec()
        if dlg.choice == "mask":
            return masked
        if dlg.choice == "raw":
            return prompt
        return None

    def _on_done(self, idx, ok, text):
        """异步步骤(Job)的完成回调(失败停在当前步可重试)。"""
        if self._dead:
            return
        if not ok:
            self._auto = False
            self._runlog.step_error(idx, text)
            self._set_busy(False, "步骤 %d 失败,可重试" % (idx + 1))
            InfoBar.error("调用失败", text[:220], parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=-1)
            return
        self._step_complete(idx, text)

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
        # 「复制」= 只写剪贴板(不派发 Ctrl+V);显式粘贴入口另行调用
        # core.paste_text(force=True)
        self.app.core.copy_text(text)

    def _copy_latest(self):
        if self.results:
            self._copy_text(self.results[-1])
            InfoBar.success("已复制", "结果已放入剪贴板",
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
            # 中途关闭:运行记录以 partial 落盘(至少跑过一步才记,
            # 零步关闭不污染历史)
            if not self._runlog_saved and self._runlog.steps:
                try:
                    wrl.save_run(self._runlog.abort())
                except Exception:
                    cm.log("运行记录保存失败:\n%s" % traceback.format_exc())
            self.deleteLater()
            e.accept()


# ============================================================
# Workflow Run Inspector(运行记录查看,建议 18)
# ============================================================
class WorkflowRunInspector(QWidget):
    """工作流运行历史:左列运行(最近在前),右侧选中运行的每步明细
    (大小/耗时/状态)。数据来自 workflow_runlog——只记大小不记内容。"""

    _STATUS_TXT = {"ok": "✓ 完成", "halt": "⛔ 停链(条件)", "error": "✗ 失败",
                   "cancelled": "已取消", "running": "—"}
    _RUN_TXT = {"done": "完成", "failed": "失败", "cancelled": "已取消",
                "partial": "未跑完", "running": "进行中"}

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowTitle("工作流运行记录")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(780, 520)

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 20, 28, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        icon = QLabel("🧾")
        icon.setFont(font(16))
        ttl = StrongBodyLabel("运行记录(Run Inspector)")
        ttl.setFont(font(15, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        head.addWidget(icon)
        head.addSpacing(6)
        head.addWidget(ttl)
        head.addStretch(1)
        v.addLayout(head)
        hint = CaptionLabel("每次运行只记录每步的输入/输出大小、耗时与状态,不记录任何内容;"
                            "错误文案已脱敏。最多保留 %d 条,超出丢最旧。" % wrl.KEEP_RUNS)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(10)
        self.lst = QListWidget()
        self.lst.setFont(font(12))
        self.lst.setFixedWidth(290)
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border-radius: 12px;"
            " border: 1px solid %s; outline: none; padding: 4px; }"
            "QListWidget::item { color: %s; border-radius: 8px; padding: 8px 10px; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, ACCENT_SOFT, ACCENT))
        body.addWidget(self.lst)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.lbl_sum = CaptionLabel("选择左侧一条运行查看明细")
        self.lbl_sum.setWordWrap(True)
        self.lbl_sum.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        right.addWidget(self.lbl_sum)
        self.tbl = TableWidget(self)
        self.tbl.setColumnCount(6)
        self.tbl.setHorizontalHeaderLabels(
            ["步骤", "类型", "状态", "输入→输出", "耗时", "说明"])
        self.tbl.verticalHeader().hide()
        self.tbl.setEditTriggers(TableWidget.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        right.addWidget(self.tbl, 1)
        body.addLayout(right, 1)
        v.addLayout(body, 1)

        foot = QHBoxLayout()
        b_clear = PushButton("清空记录", self)
        b_clear.setIcon(FIF.DELETE.icon())
        b_clear.clicked.connect(self._clear)
        foot.addWidget(b_clear)
        foot.addStretch(1)
        b_close = PushButton("关闭", self)
        b_close.clicked.connect(self.close)
        foot.addWidget(b_close)
        v.addLayout(foot)

        self.lst.currentRowChanged.connect(self._show_run)
        self._runs = []
        self._reload()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    def _reload(self):
        self._runs = wrl.load_runs()
        self.lst.blockSignals(True)
        self.lst.clear()
        for r in self._runs:
            self.lst.addItem(self._run_line(r))
        self.lst.blockSignals(False)
        if self._runs:
            self.lst.setCurrentRow(0)
        else:
            self._show_run(-1)

    @staticmethod
    def _run_line(r):
        t = time.strftime("%m-%d %H:%M",
                          time.localtime(r.get("started_at") or 0))
        status = WorkflowRunInspector._RUN_TXT.get(
            r.get("status"), str(r.get("status")))
        return "#%s · %s\n%s · %d 步 · %s" % (
            r.get("id") or "?", r.get("workflow") or "?", t,
            r.get("total_steps") or 0, status)

    def _show_run(self, row):
        self.tbl.setRowCount(0)
        if not (0 <= row < len(self._runs)):
            self.lbl_sum.setText("暂无运行记录" if not self._runs
                                 else "选择左侧一条运行查看明细")
            return
        r = self._runs[row]
        dur = float(r.get("finished_at") or 0) - float(r.get("started_at") or 0)
        self.lbl_sum.setText(
            "「%s」 输入 %s 字符 → 输出 %s 字符 · 总耗时 %s"
            % (r.get("workflow") or "?", r.get("input_chars", 0),
               r.get("output_chars", 0), self._fmt_sec(dur)))
        for s in r.get("steps") or []:
            i = self.tbl.rowCount()
            self.tbl.insertRow(i)
            vals = (str((s.get("idx") or 0) + 1),
                    s.get("label") or s.get("kind") or "?",
                    self._STATUS_TXT.get(s.get("status"),
                                         str(s.get("status"))),
                    "%s → %s 字符" % (s.get("input_chars", 0),
                                      s.get("output_chars", 0)),
                    self._fmt_ms(s.get("ms")),
                    s.get("error") or "")
            for c, txt in enumerate(vals):
                self.tbl.setItem(i, c, QTableWidgetItem(str(txt)))

    @staticmethod
    def _fmt_ms(ms):
        if ms is None:
            return "—"
        return "%.1f s" % (ms / 1000.0) if ms >= 1000 else "%.0f ms" % ms

    @staticmethod
    def _fmt_sec(sec):
        if sec < 0:
            return "—"
        if sec >= 60:
            return "%d 分 %.0f 秒" % (sec // 60, sec % 60)
        return "%.1f 秒" % sec

    def _clear(self):
        if not self._runs:
            return
        if MessageBox("清空运行记录",
                      "确定删除全部 %d 条运行记录?此操作不可撤销。"
                      % len(self._runs), self).exec():
            try:
                wrl.clear_runs()
            except Exception:
                cm.log("清空运行记录失败:\n%s" % traceback.format_exc())
            self._reload()


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


class TransformDialog(MessageBoxBase):
    """粘贴变换对话框(Ctrl+Shift+Enter):选变换 → 实时预览 → 粘贴/复制。"""

    def __init__(self, app, rec):
        super().__init__(app.win)
        self.app = app
        self.rec = rec
        self.setWindowTitle = None
        ttl = StrongBodyLabel("转换粘贴")
        self.viewLayout.addWidget(ttl)
        sub = CaptionLabel("选择变换后预览确认;「粘贴」写回剪贴板并回贴到"
                           "原窗口(带目标窗口校验)。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        from PySide6.QtWidgets import QRadioButton, QVBoxLayout as _VL, QWidget as _W
        box = _W()
        vl = _VL(box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(6)
        self._btns = []
        content = rec.get("content") or ""
        for k, name, _fn in cm.TRANSFORMS:
            if cm.transform_text(content, k) is None and k != "plain":
                continue          # 不适用的变换直接不显示(如非 JSON)
            b = QRadioButton(name)
            b.toggled.connect(lambda on, k=k: self._update(k) if on else None)
            vl.addWidget(b)
            self._btns.append((k, b))
        self.viewLayout.addWidget(box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(180)
        self.viewLayout.addWidget(self.preview)
        b_copy = PushButton("复制")
        b_copy.clicked.connect(self._copy)
        self.viewLayout.addWidget(b_copy, 0, Qt.AlignRight)
        self.yesButton.setText("粘贴并关闭")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(520)
        self._key = "plain"
        if self._btns:
            self._btns[0][1].setChecked(True)
        self._update(self._key)

    def _update(self, key):
        self._key = key
        out = cm.transform_text(self.rec.get("content") or "", key) or ""
        self.preview.setPlainText(out[:4000])

    def _copy(self):
        out = cm.transform_text(self.rec.get("content") or "", self._key) or ""
        self.app.core.copy_text(out)
        InfoBar.success("已复制", "变换结果已放入剪贴板", parent=self.app.win,
                        position=InfoBarPosition.TOP_RIGHT)

    def validate(self):
        self.app.paste_variant(self._key)
        return True


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
    ClipAction(lambda r, h: "与基准对比" if (h._diff_base is not None
               and h._diff_base is not r) else "选为对比基准(Ctrl+点击)",
               lambda r, h: h.compare_pick(r),
               supported=lambda r: r["type"] != "image"),
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

    def __init__(self, qapp, safe_mode=False):
        super().__init__()
        self.qapp = qapp
        # 安全模式(连续启动崩溃后的降级启动):不加载监听/热键/图钉画布/
        # 自动清理/联网检查;历史浏览、导出、设置恢复照常(建议 20)
        self.safe_mode = bool(safe_mode)
        self.category = "all"
        self.query = ""
        self.src_filter = ""            # 来源应用筛选(exe 名;空 = 全部)
        self.time_filter = "all"        # 时间筛选档位(all/today/24h/7d/30d)
        self.selected = None
        self._multi_recs = set()        # Shift+点击的段选(记录身份;Delete 批量删)
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

        # 共享核心(状态加载 + 历史迁移 + 后台线程启动;安全模式不启线程)
        self.core = cm.ClipboardCore(safe_mode=self.safe_mode)

        self.win = MainWindow(self)
        self.panel = self.win.history_page     # 既有内部引用的稳定别名
        self._ai_runners = []
        self._viewers = []
        self._diffs = []
        self._diff_base = None               # 对比基准记录(Ctrl+点击选定)
        self._badge_cache = {}               # id(rec) → 卡片子类型徽章(JWT/Base64)

        # 图钉画布(置顶悬浮多图板):恢复上次钉住的图片与布局;
        # 独立于主窗口存在,程序退出才销毁
        self.pinboard = PinBoardManager(self)

        # 快捷粘贴浮层(Win+V 式轻入口,默认 Alt+`):外壳一次构建,
        # 打开只做过滤 + show,目标 <100ms
        self.quick = QuickPasteWindow(self)

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
        if not self.safe_mode:
            QTimer.singleShot(600, self._prewarm)
            # 孤儿图片清理:启动收尾全部完成后再做(不占启动关键路径,
            # UI 线程执行 + 方法内的新文件时间窗防护,与捕获线程无竞态)
            QTimer.singleShot(3000, self._cleanup_orphan_images)
            # 清理策略周期执行(常驻多日也要按天数/容量裁剪):每 6 小时一次
            self._cleanup_timer = QTimer(self)
            self._cleanup_timer.setInterval(6 * 3600 * 1000)
            self._cleanup_timer.timeout.connect(self._periodic_cleanup)
            self._cleanup_timer.start()

        # UI 事件循环健康监控:Mica 在个别远程会话上致派发器假死时
        # 无 Mica 自重启(机制与触发日志见 UiHealthMonitor)
        self.health = UiHealthMonitor(self)
        self.health.arm()
        # 首次启动欢迎(离屏/测试环境不弹;看过一次写入配置,不再打扰)
        if not self.cfg.get("first_run_shown") \
                and not self.safe_mode \
                and QGuiApplication.platformName() != "offscreen":
            QTimer.singleShot(1500, self._show_welcome)
        # 启动静默检查更新(每 7 天一次;发现新版才提示,不打扰;安全模式不联网)
        try:
            _last_chk = float(self.cfg.get("last_update_check", 0) or 0)
        except (TypeError, ValueError):
            _last_chk = 0.0
        if not self.safe_mode \
                and QGuiApplication.platformName() != "offscreen" \
                and time.time() - _last_chk > 7 * 86400:
            self.cfg["last_update_check"] = time.time()
            cm.save_config(self.cfg)
            QTimer.singleShot(8000, lambda: self.check_update(silent=True))
        if self.safe_mode:
            QTimer.singleShot(1200, self._show_safe_mode_banner)
        # 启动完整落地:Safe Mode 判据清零(本次启动已确认成功)
        cm.confirm_boot_ok()

    def _show_safe_mode_banner(self):
        """安全模式横幅:告知未加载的子系统,指明「导出/恢复后重启」出路。"""
        InfoBar.warning(
            "安全模式运行中",
            "剪贴板监听、全局热键、图钉画布与自动清理未加载;"
            "历史浏览、导出与设置恢复正常可用,退出后正常重启即可。",
            parent=self.win, duration=8000, position=InfoBarPosition.TOP)

    # ---- 剪贴板读取 / 历史维护:见 cm.ClipboardCore(共享后端) ----

    # ---- 视图 ----
    def refresh_view(self):
        t_all = stamp()
        self._view_dirty = False
        self._multi_recs = set()        # 视图重建,段选失效
        with PerfSpan("history_filter"):
            view = self.core.query_view(
                self.category, self.query, src=self.src_filter,
                since=cm.ClipboardCore.time_scope_cutoff(self.time_filter))
        self._view = view
        with PerfSpan("history_cards_render"):
            self.panel.set_view(view, self.history)
            self.panel._resize_to_content()   # 卡片数/展开态变化后面板高度自适应
            self.panel.sync_src_combo()       # 来源下拉跟历史走(保留当前选择)
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

    def set_src_filter(self, src):
        if src == self.src_filter:
            return
        self.src_filter = src
        self.refresh_view()

    def set_time_filter(self, scope):
        if scope == self.time_filter:
            return
        self.time_filter = scope
        self.refresh_view()

    def move_sel(self, delta):
        if not self._view:
            return
        self._multi_recs = set()
        cur = self.selected if self.selected is not None else (0 if delta > 0 else -1)
        cur = max(0, min(len(self._view) - 1, cur + delta))
        self.selected = cur
        self.panel.select_index(cur)
        self.panel.ensure_visible(cur)

    def select_card(self, card):
        """点击/键盘选中卡片:控件顺序(置顶组在前)≠视图顺序(历史序),
        必须按记录身份映射回 _view 索引,否则预览/高亮/回车粘贴错条目。"""
        self._multi_recs = set()
        try:
            hi = next(i for i, h in enumerate(self.history) if h is card.rec)
            vi = self._view.index(hi)
        except (StopIteration, ValueError):
            return
        self.selected = vi
        self.panel.select_index(vi)

    def range_select_from(self, card):
        """Shift+点击:当前选中到该卡整段多选(Delete 批量删除,可整批撤销)。"""
        try:
            hi = next(i for i, h in enumerate(self.history) if h is card.rec)
            end = self._view.index(hi)
        except (StopIteration, ValueError):
            return
        start = self.selected if self.selected is not None else end
        lo, hi2 = sorted((start, end))
        # 存 id(rec) 而不是记录本身:dict 不可哈希,且身份比对免疫刷新重建
        self._multi_recs = {id(self.history[self._view[i]])
                            for i in range(lo, hi2 + 1)}
        self.panel.mark_multi(self._multi_recs)

    def delete_selection_or_multi(self):
        """Delete 键:段选有效删整段,否则删当前条目;两者都可撤销。"""
        recs = [r for r in self.history if id(r) in self._multi_recs]
        if len(recs) > 1:
            n = self.core.batch_soft_delete(recs)
            self._multi_recs = set()
            self.refresh_view()
            self._offer_undo(n, "已删除 %d 条" % n)
        else:
            self.delete_selected()

    def paste_selected(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        rec = self.history[self._view[self.selected]]
        self.paste_rec(rec)

    def paste_rec(self, rec):
        # 先收起主窗(焦点回到目标窗口),再排队粘贴;
        # 目标窗口取唤出主窗前的现场(粘贴事务校验,切换即取消)
        self.win.hide()
        if not self.core.paste_record(
                rec, target_hwnd=getattr(self, "_paste_target", None)):
            MessageBox("WinClipboard", "文件已不存在,无法粘贴。", self.win).exec()
            return

    def _paste_text_now(self, text):
        """变换粘贴的统一出口:收起主窗 + 强制回贴到意图目标窗口。"""
        self.win.hide()
        self.core.paste_text(text, force=True,
                             target_hwnd=getattr(self, "_paste_target", None))

    def _selected_text_rec(self):
        if self.selected is None or self.selected >= len(self._view):
            return None
        rec = self.history[self._view[self.selected]]
        return rec if rec.get("type") == "text" else None

    def paste_plain(self):
        """Shift+Enter:纯文本粘贴(paste_text 只写 CF_UNICODETEXT,不带富格式)。"""
        rec = self._selected_text_rec()
        if rec is not None:
            self._paste_text_now(rec.get("content") or "")

    def paste_variant(self, key):
        """按变换键粘贴(「粘贴为」子菜单 / Transform 对话框共用)。"""
        rec = self._selected_text_rec()
        if rec is None:
            return
        out = cm.transform_text(rec.get("content") or "", key)
        if out is None:
            InfoBar.warning("不适用", "该内容无法应用此变换(如非 JSON)",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        self._paste_text_now(out)

    def open_transform(self):
        """Ctrl+Shift+Enter / 菜单「转换…」:变换预览对话框。"""
        rec = self._selected_text_rec()
        if rec is not None:
            TransformDialog(self, rec).exec()

    def _set_type(self, rec, value):
        """类型纠正(仅当前记录):写条目 + 库持久化 + 视图刷新。"""
        self.core.set_type_override(rec, value)
        self.refresh_view()

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
        if len(self._multi_recs) > 1:      # 段选激活时给批量动作置顶入口
            a = act(menu, "删除所选 %d 条(可撤销)" % len(self._multi_recs))
            a.triggered.connect(lambda _=False: self.delete_selection_or_multi())
            menu.addSeparator()
        if rec.get("type") == "text":
            # ---- 粘贴为(变换)子菜单:不适用项置灰 ----
            sub = RoundMenu("粘贴为", self.panel)
            for k, name, _fn in cm.TRANSFORMS:
                a = act(sub, name)
                if k != "plain" and \
                        cm.transform_text(rec.get("content") or "", k) is None:
                    a.setEnabled(False)
                a.triggered.connect(
                    lambda _=False, k=k: self.paste_variant(k))
            sub.addSeparator()
            a = act(sub, "转换…(Ctrl+Shift+Enter)")
            a.triggered.connect(lambda _=False: self.open_transform())
            menu.addMenu(sub)
            # ---- 类型纠正子菜单:自动 / 纯文本 / JWT / Base64 / JSON / URL ----
            tsub = RoundMenu("类型", self.panel)
            cur = rec.get("type_override") or ""
            for val, label in (("", "自动识别"), ("text", "纯文本"),
                               ("jwt", "JWT"), ("base64", "Base64"),
                               ("json", "JSON"), ("url", "URL")):
                mark = "● " if val == cur else "○ "
                a = act(tsub, mark + label)
                a.triggered.connect(
                    lambda _=False, v=val: self._set_type(rec, v or None))
            menu.addMenu(tsub)
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
        self.core.soft_delete_at(self._view[self.selected])
        self.refresh_view()
        self._offer_undo(1, "已删除")

    def undo_delete(self):
        back = self.core.undo_delete()
        if back:
            self.refresh_view()
            InfoBar.success("已撤销", "恢复了 %d 条记录" % len(back),
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=2500)

    def _offer_undo(self, n, title):
        """删除/清空后的撤销入口:InfoBar 挂「撤销」按钮,条目文件未删,可整批恢复。"""
        bar = InfoBar.warning(title, "%d 条记录可在提示消失前撤销" % n,
                              parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                              duration=6000)
        btn = PushButton("撤销", bar)
        btn.clicked.connect(self.undo_delete)
        bar.addWidget(btn)

    def open_editor(self, rec, on_result=None, annos=None, layer_mode=False):
        """图片编辑入口(内置):查看器内嵌编辑模式——单窗口看图+标注,
        不再另开编辑器窗口。annos/layer_mode 供图钉画布非破坏标注层回编。"""
        if not os.path.exists(rec.get("content", "")):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。", self.panel).exec()
            return
        self.open_image_viewer(rec, edit=True, on_result=on_result,
                               annos=annos, layer_mode=layer_mode)

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

    def compare_pick(self, rec):
        """对比流:第一条 Ctrl+点击设为基准,第二条开对比窗口;再点基准取消。"""
        if rec.get("type") == "image":
            InfoBar.warning("对比", "图片暂不支持对比(仅文本 / 文件列表)",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        if self._diff_base is None:
            self._diff_base = rec
            InfoBar.success("对比基准已选", "Ctrl+点击另一条记录开始对比",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
        elif self._diff_base is rec:
            self._diff_base = None
            InfoBar.info("已取消对比基准", "", parent=self.win,
                         position=InfoBarPosition.TOP_RIGHT, duration=1500)
        else:
            base, self._diff_base = self._diff_base, None
            self.open_diff(base, rec)

    def open_diff(self, rec_a, rec_b):
        """打开内容对比窗口(A=基准/旧,B=所选/新)。"""
        try:
            w = DiffWindow(self, rec_a, rec_b)
            _pop_to_front(w)
            self._diffs.append(w)
            return w
        except Exception:
            cm.log("打开对比窗口失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "对比窗口打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()
            return None

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

    def open_image_viewer(self, rec, edit=False, on_result=None,
                          annos=None, layer_mode=False):
        """打开图片查看器(缩放/平移;与文本查看器同入 _viewers 统一清理)。
        edit=True 直接进入内嵌编辑模式;on_result 为编辑产物直通回调;
        annos/layer_mode 见 ImageEditor(图钉画布非破坏标注层)。"""
        try:
            v = ImageViewerWindow(self, rec, edit=edit, on_result=on_result,
                                  annos=annos, layer_mode=layer_mode)
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

    def apply_hotkey(self, mod, key):
        """设置页热键录制:命令发给核心线程重注册(RegisterHotKey 失败
        = 被占用 = 冲突检测),成败经 hotkey_change 事件回 UI。"""
        try:
            self.core.worker.cmd_q.put(("hotkey", int(mod), int(key)))
        except Exception:
            cm.log("热键更改命令投递失败:\n%s" % traceback.format_exc())

    def apply_quick_hotkey(self, mod, key):
        """快捷粘贴浮层热键录制(同 apply_hotkey 套路,独立命令/事件)。"""
        try:
            self.core.worker.cmd_q.put(("hotkey_qp", int(mod), int(key)))
        except Exception:
            cm.log("快捷粘贴热键更改命令投递失败:\n%s" % traceback.format_exc())

    def _on_qp_hotkey_change(self, ok, mod, key):
        label = cm.hotkey_label(mod, key)
        if ok:
            self.cfg["quick_mod"] = int(mod)
            self.cfg["quick_key"] = int(key)
            cm.save_config(self.cfg)
            InfoBar.success("快捷粘贴热键已更新", "新热键:" + label,
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.error("快捷粘贴热键注册失败",
                          "「%s」可能被其他软件占用,已保留原热键" % label,
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
        try:
            self.win.settings_page.update_quick_label()
        except Exception:
            pass

    def toggle_monitor_pause(self, on=None):
        """临时隐私模式:暂停期间剪贴板事件不入史(不落盘,重启即恢复监听)。"""
        paused = (not self.core.monitor_paused) if on is None else bool(on)
        if paused == self.core.monitor_paused:
            return
        self.core.monitor_paused = paused
        if paused:
            InfoBar.warning("已暂停监听", "期间复制的内容不会进入历史",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=3000)
        else:
            InfoBar.success("已恢复监听", "", parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT, duration=2000)
        try:
            self.win.settings_page.sync_monitor_pause()
        except Exception:
            pass

    def _on_hotkey_change(self, ok, mod, key):
        label = cm.hotkey_label(mod, key)
        if ok:
            self.cfg["hotkey_mod"] = int(mod)
            self.cfg["hotkey_key"] = int(key)
            cm.save_config(self.cfg)
            InfoBar.success("热键已更新", "新热键:" + label, parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.error("热键注册失败",
                          "「%s」可能被其他软件占用,已保留原热键" % label,
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
        try:
            self.win.settings_page.update_hotkey_label()
        except Exception:
            pass

    def show_services(self):
        self.win.services_page.refresh()   # 配置可能在别处变动(如工作流页填入示例)
        self.win.switchTo(self.win.services_page)

    def run_workflow(self, wf, rec):
        runner = WorkflowRunner(self, wf, rec["content"])
        _pop_to_front(runner)
        self._ai_runners.append(runner)

    # ---- 托盘动作 ----
    def clear_all(self):
        box = MessageBox("清空历史", "确定清空全部未置顶条目?清空后 6 秒内仍可撤销。",
                         self.win)
        if not box.exec():
            return
        removed = self.core.soft_clear_unpinned()
        self.refresh_view()
        if removed:
            self._offer_undo(len(removed), "已清空(置顶保留)")

    def show_private(self):
        if not self.private:
            InfoBar.info("私密内容", "暂无敏感内容", parent=self.win,
                         position=InfoBarPosition.TOP_RIGHT)
            return
        self.win.private_page.refresh()
        self.win.switchTo(self.win.private_page)

    def about(self):
        hk = cm.hotkey_label(self.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
                             self.cfg.get("hotkey_key", 0x56))
        MessageBox("关于 WinClipboard",
                   "Windows 桌面剪贴板管理器  v%s\n\n"
                   "热键: %s 弹出/隐藏\n"
                   "左键托盘图标: 切换面板\n"
                   "右键托盘图标: 菜单\n\n"
                   "数据目录:\n" % (cm.APP_VERSION, hk) + cm.APP_DIR,
                   self.panel).exec()

    def shortcut_help_text(self):
        """应用内快捷键总览(全局热键动态跟随当前配置)。"""
        hk = cm.hotkey_label(self.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
                             self.cfg.get("hotkey_key", 0x56))
        qk = cm.hotkey_label(self.cfg.get("quick_mod", cm.MOD_ALT),
                             self.cfg.get("quick_key", 0xC0))
        return ("全局\n"
                "  %s   显示 / 隐藏主窗口(设置页可按键录制更改)\n"
                "  %s   快捷粘贴浮层(Win+V 式:搜索 + ↑↓ + 回车粘贴)\n"
                "  Ctrl + Shift + P   剪贴板图片直钉到画布\n"
                "  Ctrl + Alt + T   画布鼠标穿透切换\n"
                "\n快捷粘贴浮层(%s)\n"
                "  ↑ / ↓   选择      回车   粘贴      Shift + 回车   纯文本粘贴\n"
                "  Ctrl + 回车   打开查看器      Ctrl + 1~9   直选粘贴\n"
                "  Tab   切换类型(全部/文本/链接/图片/文件)\n"
                "  Del   删除条目(可撤销)      Esc   关闭并回到原窗口\n"
                "\n历史列表\n"
                "  ↑ / ↓   移动选择      回车   粘贴选中条目\n"
                "  Shift + 回车   纯文本粘贴(不带富文本格式)\n"
                "  Ctrl + Shift + 回车   转换粘贴(JSON 格式化/压缩、URL 编解码、\n"
                "                        转义、大小写等,实时预览)\n"
                "  Esc   收起主窗口      双击卡片   打开独立查看器\n"
                "  Ctrl + 点击   选两条记录对比(第一条设基准,第二条开对比窗口)\n"
                "\n搜索语法(Search DSL,与筛选下拉叠加生效)\n"
                "  source:vscode   按来源应用(自动补 .exe)\n"
                "  type:json|jwt|base64|url|text|image|files   按类型/子类型\n"
                "  after:2026-09-01 或 after:7d   时间起点\n"
                "  before:2026-09-10 或 before:3d   时间上界\n"
                "  is:pinned   只看置顶;其余词按子串搜索\n"
                "  示例:type:image source:snipping\n"
                "\n内容查看器\n"
                "  Ctrl + F   浮动查找      Ctrl + = / - / 0   字号缩放\n"
                "\n图片编辑(查看器内编辑态)\n"
                "  Ctrl + Z   撤销      Del   删除选中标注      Esc   放弃编辑\n"
                "\n系统托盘\n"
                "  左键   切换面板      右键   菜单(暂停监听 / 清空 / 退出)"
                % (hk, qk, qk))

    def show_shortcut_help(self):
        MessageBox("快捷键与操作", self.shortcut_help_text(), self.win).exec()

    def check_update(self, silent=False):
        """检查 GitHub 最新 Release(后台线程)。silent=True 为启动期静默检查:
        只有发现新版才提示,其余情况不打扰。"""
        try:
            self._update_worker = UpdateWorker(self)
        except Exception:
            return

        def on_done(st, ver, url):
            self._update_worker = None
            if st == "ok":
                bar = InfoBar.warning("发现新版本", "最新 %s(当前 %s)"
                                      % (ver, cm.APP_VERSION), parent=self.win,
                                      position=InfoBarPosition.TOP_RIGHT,
                                      duration=10000)
                b = PushButton("打开发布页", bar)
                b.clicked.connect(lambda: cm.open_url(url))
                bar.addWidget(b)
            elif silent:
                return
            elif st == "latest":
                InfoBar.success("已是最新版本", "当前 %s" % cm.APP_VERSION,
                                parent=self.win,
                                position=InfoBarPosition.TOP_RIGHT)
            elif st == "norelease":
                InfoBar.info("暂无发布版本",
                             "仓库还没有 Release;可在「打开发布页」查看项目主页",
                             parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                             duration=5000)
            else:
                InfoBar.error("检查更新失败", str(url)[:120], parent=self.win,
                              position=InfoBarPosition.TOP_RIGHT, duration=5000)
        self._update_worker.done.connect(on_done)
        self._update_worker.start()

    def _show_welcome(self):
        """首次启动引导:看完即记入配置不再打扰(离屏/测试环境不弹)。"""
        self.cfg["first_run_shown"] = True
        cm.save_config(self.cfg)
        hk = cm.hotkey_label(self.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
                             self.cfg.get("hotkey_key", 0x56))
        box = MessageBox(
            "欢迎使用 WinClipboard",
            "程序已在本机后台运行(系统托盘 📋)。接下来:\n\n"
            "1. 随便复制点什么 —— 文字 / 截图 / 文件都会自动进入历史\n"
            "2. 按 %s 随时唤出或收起主窗口\n"
            "3. 密码 / 银行卡等内容会自动进「私密区」,列表默认遮罩显示\n"
            "4. 设置页可以:按键录制新热键、按应用排除、自动清理、加密历史\n\n"
            "全部操作说明见「设置 → 快捷键与操作」。" % hk, self.win)
        box.yesButton.setText("开始使用")
        box.cancelButton.hide()
        box.buttonLayout.insertStretch(0, 1)   # 按钮靠右,与库对话框一致
        box.exec()

    def _warn_hotkey(self):
        if self._warned_hotkey:
            return
        self._warned_hotkey = True
        MessageBox("WinClipboard",
                   "全局热键(%s)注册失败,可能被其他软件占用。\n"
                   "可在设置页直接录制新热键,或重启后重试。"
                   % cm.hotkey_label(self.cfg.get("hotkey_mod"),
                                     self.cfg.get("hotkey_key")),
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
        elif kind == "private":
            # 敏感内容已隔离(捕获默认静默,用户无从得知去哪了——补一条轻提示)
            jwt = bool(rest[0]) if rest else False
            self.win.private_page.refresh()
            InfoBar.success("已隔离敏感内容到私密区",
                            "JWT 可在 私密区 →「查看」中解码(仅解码,不验签)"
                            if jwt else "可在左侧「私密区」查看",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=3000)
        elif kind == "hotkey":
            self._hotkey_ts = stamp()       # 首帧统计起点(paintEvent 收口)
            with PerfSpan("hotkey_to_show_window"):
                self.win.toggle()
        elif kind == "pin_hotkey":          # Ctrl+Shift+P:直接钉剪贴板图片
            self.pinboard.pin_clipboard_image()
        elif kind == "qp_hotkey":           # Alt+`:快捷粘贴浮层
            with PerfSpan("quickpaste_open"):
                self.quick.toggle()
        elif kind == "qp_hotkey_state":     # 启动期快捷粘贴热键注册结果
            self._qp_hotkey_ok = bool(rest[0])
            try:
                self.win.settings_page.update_quick_label()
            except Exception:
                pass
        elif kind == "qp_hotkey_change":    # 设置页录制快捷粘贴热键的结果
            self._on_qp_hotkey_change(bool(rest[0]), rest[1], rest[2])
        elif kind == "paste_abort":         # 粘贴事务取消(窗口切换/剪贴板被改)
            InfoBar.warning("已取消自动粘贴", str(rest[0]) if rest else "",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=4000)
        elif kind == "ctt_hotkey":          # Ctrl+Alt+T:切换鼠标穿透
            self.pinboard.toggle_click_through(None)
        elif kind == "ctt_hotkey_state":    # 穿透退出热键注册结果(失败则禁穿透)
            self.pinboard.mark_ctt_hotkey(bool(rest[0]))
        elif kind == "hotkey_change":       # 设置页热键录制结果(成败/回滚)
            self._on_hotkey_change(bool(rest[0]), rest[1], rest[2])
        elif kind == "tray":
            act = rest[0]
            if act == "show":
                with PerfSpan("tray_to_show_window"):
                    self.win.toggle()
            elif act == "clear":
                self.clear_all()
            elif act == "pause":
                self.toggle_monitor_pause()
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

    def _periodic_cleanup(self):
        """每 6 小时按 cleanup_days / disk_cap_mb 裁剪一次(常驻多日场景)。"""
        try:
            n, _ = self.core.apply_cleanup()
            if n:
                self.refresh_view()
        except Exception:
            cm.log("周期清理跳过:\n%s" % traceback.format_exc())

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
    streak = cm.begin_boot()     # Safe Mode 判据:本次启动先记"未确认"
    cm.log("启动(Qt+Fluent) pid=%d py=%s %s" %
           (os.getpid(), sys.executable, sys.version.split()[0]))
    perf_probe.setup()                    # 性能埋点 → logs/perf.log(独立于 app.log)

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    apply_fonts(qapp)          # 中文回退字族(见 FONT_FAMILIES 注释)
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

    # 连续启动崩溃 → 提供安全模式(建议 20):不加载监听/热键/画布/自动
    # 任务,保住"导出数据 + 恢复设置"两条退路。选正常启动若再失败,下次仍提示。
    safe = False
    if streak >= cm.SAFE_MODE_THRESHOLD \
            and QGuiApplication.platformName() != "offscreen":
        box = QMessageBox()
        box.setWindowTitle(cm.APP_NAME)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("检测到 WinClipboard 连续 %d 次启动未正常完成。" % streak)
        box.setInformativeText(
            "安全模式不加载剪贴板监听、全局热键、图钉画布与自动清理,\n"
            "历史浏览、导出数据与设置恢复照常可用。\n"
            "可先正常启动;若再次失败,下次仍会提示。")
        b_safe = box.addButton("安全模式启动", QMessageBox.ButtonRole.ActionRole)
        b_norm = box.addButton("正常启动", QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(b_safe)
        box.exec()
        safe = box.clickedButton() is b_safe
        cm.log("启动方式选择: %s(连续未确认启动 %d 次)"
               % ("安全模式" if safe else "正常", streak))

    try:
        app = WinClipboardApp(qapp, safe_mode=safe)
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
    cm.mark_clean_quit()         # 正常退出;崩溃到不了这里——这正是判据
    return rc


if __name__ == "__main__":
    sys.exit(main())
