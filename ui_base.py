# -*- coding: utf-8 -*-
"""UI 基础层(阶段 8 拆分):主题调色板 + 字体 + 图标/文本/窗口通用助手。

调色板常量在这里定义并被各 UI 模块裸名引用;模块导入 ui_base 的公开名并
调用 register() 登记自己后,init_palette(dark) 会把派生色同步写进每个
注册模块的全局——深浅色切换语义与拆分前的单一模块完全一致。
"""
import os
import sys
import io
import subprocess

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QPainter,
                           QPainterPath, QPixmap, QFontMetrics)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import FluentIcon as FIF, IconWidget, themeColor

import clipboard_manager as cm

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
ACCENT = "#D97757"               # Claude 陶土橙(Terracotta)
ACCENT_SOFT = "rgba(217,119,87,0.10)"
TEXT = "#141413"                 # 暖炭黑
SEC = "#5E5D59"
TER = "#8C8A84"
LINE = "#E5E3DC"
BG = "#F4F3EE"                   # 羊皮纸暖白
CARD_BG = "#FFFFFF"
CARD_BORDER = "#E5E3DC"
FIELD_BG = "#FFFFFF"
FIELD_BORDER = "#E5E3DC"
BADGE_BG = "#EFECE6"
NAV_BG = "#ECEAE4"
THUMB_PH = "#EFECE6"
THUMB_BORDER = "#E5E3DC"
GUTTER_BG = "#FAF9F5"       # 查看器行号栏(比内容卡片深半档)
SURFACE_HOVER = "#FCFBF9"   # 卡片悬停底色(比白卡深半档)
ACCENT_TEXT = "#B4552F"     # 主题色在小字上的可读加深版
PANEL_TOP = CARD_BG
PANEL_BOT = CARD_BG


def init_palette(dark):
    """按 qfluentwidgets 当前主题刷新派生色,并同步到所有已注册模块
    (常量名保留,供既有布局代码裸名引用;拆分出的模块注册后同样生效)。"""
    acc = themeColor()
    values = {
        "IS_DARK": bool(dark),
        "ACCENT": acc.name(),
        "ACCENT_SOFT": "rgba(%d,%d,%d,0.14)" % (acc.red(), acc.green(), acc.blue()),
    }
    values.update({
        False: {   # Claude Warm Light(暖纸白 + 陶土橙)
            "TEXT": "#141413", "SEC": "#5E5D59", "TER": "#8C8A84",
            "LINE": "#E5E3DC", "BG": "#F4F3EE", "CARD_BG": "#FFFFFF",
            "CARD_BORDER": "#E5E3DC",
            "FIELD_BG": "#FFFFFF", "FIELD_BORDER": "#E5E3DC",
            "BADGE_BG": "#EFECE6", "NAV_BG": "#ECEAE4",
            "THUMB_PH": "#EFECE6", "THUMB_BORDER": "#E5E3DC",
            "GUTTER_BG": "#FAF9F5",
            "SURFACE_HOVER": "#FCFBF9", "ACCENT_TEXT": "#B4552F",
            "DIFF_ADD": "#3f7a4f", "DIFF_DEL": "#b3552f",
            "DIFF_CTX": "#8C8A84",
        },
        True: {    # Claude Warm Dark
            "TEXT": "#ECECEC", "SEC": "#A6A49E", "TER": "#8C8A84",
            "LINE": "#32312E", "BG": "#191918", "CARD_BG": "#232321",
            "CARD_BORDER": "#32312E",
            "FIELD_BG": "#2A2927", "FIELD_BORDER": "#32312E",
            "BADGE_BG": "#2A2927", "NAV_BG": "#232321",
            "THUMB_PH": "#2A2927", "THUMB_BORDER": "#32312E",
            "GUTTER_BG": "#232321",
            "SURFACE_HOVER": "#262624", "ACCENT_TEXT": "#E9A084",
            "DIFF_ADD": "#5ec97f", "DIFF_DEL": "#ef8080",
            "DIFF_CTX": "rgba(255,255,255,0.45)",
        },
    }[bool(dark)])
    values["PANEL_TOP"] = values["CARD_BG"]
    values["PANEL_BOT"] = values["CARD_BG"]
    for m in [sys.modules[__name__]] + list(_PALETTE_MODULES):
        for k, v in values.items():
            setattr(m, k, v)




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


def accent_rgba(alpha):
    """当前强调色的半透明 QSS 颜色(#AARRGGBB)。

    自绘卡片的选中底色/描边一律走这里,设置页换主题色后跟着变;
    写死 rgba(217,119,87,…) 会让色板形同虚设。
    """
    c = QColor(ACCENT)
    return "#%02X%02X%02X%02X" % (max(0, min(255, int(round(alpha * 255)))),
                                  c.red(), c.green(), c.blue())




# ---- 多模块调色板同步:拆分出的 UI 模块注册自己,init_palette 统一刷新 ----
_PALETTE_MODULES = []


def register(mod):
    """登记一个使用调色板常量的模块(init_palette 会同步刷新其全局)。"""
    if mod is not None and mod not in _PALETTE_MODULES:
        _PALETTE_MODULES.append(mod)


DIFF_ADD = "#1f7a3f"        # 静态缺省(init_palette 会按主题覆盖)
DIFF_DEL = "#c0392b"
DIFF_CTX = "rgba(0,0,0,0.45)"

