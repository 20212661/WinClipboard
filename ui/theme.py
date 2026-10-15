# -*- coding: utf-8 -*-
"""Atelier Zero 视觉语言主题系统。

Monocle 的秩序 + Apartamento 的触感 + Études 的图形张力 + 编辑拼贴。
纸感、克制、反相、1px 细线、无圆角、信息密度。
"""
import os
from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QEasingCurve
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPixmap
from PySide6.QtWidgets import QApplication, QWidget, QFrame, QHBoxLayout, QLabel
from qfluentwidgets import setTheme, setThemeColor, Theme

# ============================================================
# 设计 Token —— Claude(Anthropic)暖调设计语言
# ============================================================
# 2026-10-15 起从 Atelier 纸感收敛为 Claude 暖调:羊皮纸暖白画布、
# 暖炭黑文字、陶土橙(Terracotta)点缀;无刺眼冷蓝、无生硬深色边框。
PAPER = "#F4F3EE"          # 画布/主窗口底色(羊皮纸暖白)
INK = "#141413"            # 主文字(暖炭黑)
INK_LIGHT = "#5E5D59"      # 次文字(暖灰)
ACCENT = "#D97757"         # Claude 陶土橙
ACCENT_HOVER = "#C6613F"   # 陶土橙悬停深一档
DANGER = "#B3552F"         # 警示(暖红棕)
SELECTION = INK

BORDER_SOFT = "#E5E3DC"    # 全局柔和暖灰边框
BORDER_HOVER = "#D5D2C8"   # 悬停边框
SURFACE_PILL = "#EFECE6"   # 胶囊/次级控件底色

# 行高 / 间距 (1px 网格)
MASTHEAD_H = 56
ROW_H = 72
DIVIDER = 1
MARGIN = 24

# 字体
FONT_SERIF = "Times New Roman"  # 标题衬线
FONT_SANS = "Helvetica Neue"      # 正文无衬线
FONT_MONO = "IBM Plex Mono"       # 标签/编号

# 回退链
FONT_HEAD = [FONT_SERIF, "Georgia", "Times New Roman", "SimSun"]
FONT_BODY = [FONT_SANS, "Inter", "Segoe UI", "Microsoft YaHei UI", "SimHei"]
FONT_LABEL = [FONT_MONO, "Space Mono", "Consolas", "Courier New"]

# ============================================================
# 颜色工具
# ============================================================
def _ensure_fonts():
    """加载项目 assets/fonts 目录下的自定义字体(如果有)。"""
    base = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    if not base.exists():
        return
    for f in base.iterdir():
        if f.suffix.lower() in (".ttf", ".otf"):
            try:
                QFontDatabase.addApplicationFont(str(f))
            except Exception:
                pass


def _font(families, px, weight=QFont.Normal, letter_spacing=0, uppercase=False):
    f = QFont()
    f.setFamilies(families)
    f.setPointSize(px)
    f.setWeight(weight)
    if letter_spacing:
        f.setLetterSpacing(QFont.PercentageSpacing, letter_spacing)
    if uppercase:
        # QSS 不支持 text-transform; 调用方需手动 .upper()
        pass
    return f


def head_font(px=18, weight=QFont.Normal):
    return _font(FONT_HEAD, px, weight, letter_spacing=102)


def body_font(px=12, weight=QFont.Normal):
    return _font(FONT_BODY, px, weight)


def label_font(px=9, weight=QFont.Normal):
    """大写标签字体(调用方需自行 .upper())。"""
    return _font(FONT_LABEL, px, weight, letter_spacing=110)


# ============================================================
# QSS 全局表
# ============================================================
def _atelier_qss():
    return f"""
QWidget {{
    background: {PAPER};
    color: {INK};
    border: none;
    font-family: "{FONT_SANS}", "Inter", "Segoe UI", "Microsoft YaHei UI";
    font-size: 12px;
    outline: none;
}}

QFrame {{
    background: {PAPER};
    color: {INK};
    border: none;
}}

QFrame#divider {{
    background: {BORDER_SOFT};
    max-height: 1px;
    min-height: 1px;
}}

QLineEdit {{
    background: #FFFFFF;
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    color: {INK};
    padding: 5px 10px;
    font-family: "{FONT_SANS}", "Inter", "Segoe UI";
    font-size: 12px;
}}

QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}

QLineEdit::placeholder {{
    color: {INK_LIGHT};
}}

QListWidget {{
    background: #FFFFFF;
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    outline: none;
    padding: 2px;
}}

QListWidget::item {{
    background: transparent;
    color: {INK};
    border-bottom: 1px solid rgba(0,0,0,0.06);
    padding: 0px;
}}

QListWidget::item:selected {{
    background: {ACCENT};
    color: #ffffff;
}}

QListWidget::item:hover:!selected {{
    background: {SURFACE_PILL};
}}

QPlainTextEdit {{
    background: #FFFFFF;
    color: {INK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 8px;
    font-family: "{FONT_MONO}", "Consolas", monospace;
    font-size: 11px;
}}

QPushButton {{
    background: #FFFFFF;
    color: {INK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 5px 12px;
    font-family: "{FONT_SANS}", "Inter", "Segoe UI", "Microsoft YaHei UI";
    font-size: 12px;
}}

QPushButton:hover {{
    background: {SURFACE_PILL};
}}

QPushButton:pressed {{
    background: {ACCENT};
    color: #ffffff;
    border-color: {ACCENT};
}}

QPushButton#Primary {{
    background: {ACCENT};
    color: #ffffff;
    border: 1px solid {ACCENT};
}}

QPushButton#Primary:hover {{
    background: {INK};
    border-color: {INK};
}}

QComboBox {{
    background: #FFFFFF;
    color: {INK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 4px 10px;
    min-width: 80px;
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QComboBox QAbstractItemView {{
    background: #FFFFFF;
    color: {INK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}

QSpinBox {{
    background: #FFFFFF;
    color: {INK};
    border: 1px solid {BORDER_SOFT};
    border-radius: 6px;
    padding: 4px;
}}

QScrollBar:vertical {{
    background: {PAPER};
    width: 8px;
    border: none;
}}

QScrollBar::handle:vertical {{
    background: {BORDER_HOVER};
    min-height: 24px;
    border-radius: 3px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: {PAPER};
    height: 8px;
    border: none;
}}

QScrollBar::handle:horizontal {{
    background: {BORDER_HOVER};
    min-width: 24px;
    border-radius: 3px;
}}
"""


def _qfluent_global_patch():
    """对无法通过 objectName 覆盖的 qfluentwidgets 全局样式做最小补丁。
    注意：Dialog/InfoBar 等保持默认，只把 MessageBoxBase 等改成无圆角。"""
    return f"""
qfluentwidgets-MessageBoxBase {{
    background: {PAPER};
    color: {INK};
}}
"""


# ============================================================
# 应用主题
# ============================================================
def apply_atelier_zero(app: QApplication, cfg=None):
    """将全局应用切换为 Atelier Zero 视觉语言。

    - 强制浅色主题，禁用 Mica/亚克力。
    - 设置 Études 蓝为强调色。
    - 安装全局 QSS。
    - 返回 (paper, ink, accent) 供代码中直接使用。
    """
    _ensure_fonts()
    setTheme(Theme.LIGHT)
    setThemeColor(QColor(ACCENT))

    app.setStyleSheet(_atelier_qss())

    # 覆盖 qfluentwidgets 字体
    try:
        from qfluentwidgets.common.font import setFontFamilies
        setFontFamilies(FONT_BODY)
    except Exception:
        pass

    app.setFont(body_font(12))
    return PAPER, INK, ACCENT


# ============================================================
# 小工具控件
# ============================================================
class Divider(QFrame):
    """1px 水平或垂直分割线。"""

    def __init__(self, vertical=False, parent=None):
        super().__init__(parent)
        self.setObjectName("divider")
        if vertical:
            self.setFixedWidth(1)
            self.setMinimumHeight(1)
        else:
            self.setFixedHeight(1)
            self.setMinimumWidth(1)


class IndexLabel(QLabel):
    """等宽大写标签，用于 NO·001、ISSUE 001 等。"""

    def __init__(self, text, parent=None):
        super().__init__(text.upper(), parent)
        self.setFont(label_font(10))
        self.setStyleSheet(f"color: {INK_LIGHT}; background: transparent;")


class MastheadBadge(QFrame):
    """SAFE MODE / LIVE 等小徽章。"""

    def __init__(self, text, danger=False, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setStyleSheet(
            f"background: {PAPER}; color: {DANGER if danger else ACCENT}; "
            f"border: 1px solid {DANGER if danger else INK}; padding: 2px 8px;"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lab = QLabel(text.upper())
        lab.setFont(label_font(8))
        lab.setStyleSheet("background: transparent;")
        lay.addWidget(lab)


# ============================================================
# 图片占位生成
# ============================================================
def ensure_collage_assets(base_dir=None):
    """若 assets/atelier_zero 为空，生成 16 张 1×1 像素占位 PNG。
    真实拼贴素材应替换为同名文件。"""
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent / "assets" / "atelier_zero"
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 17):
        path = base_dir / f"collage_{i:02d}.png"
        if not path.exists():
            try:
                from PySide6.QtGui import QImage
                img = QImage(1, 1, QImage.Format.Format_ARGB32)
                # 交替纸色/墨色/蓝色/红色
                palette = [PAPER, INK, ACCENT, DANGER]
                c = QColor(palette[(i - 1) % 4])
                # 让占位图可见：根据 i 微调亮度
                factor = 0.8 + ((i - 1) % 4) * 0.05
                c.setRed(int(c.red() * factor))
                c.setGreen(int(c.green() * factor))
                c.setBlue(int(c.blue() * factor))
                img.fill(c)
                img.save(str(path))
            except Exception as e:
                print(f"[AtelierZero] 无法生成占位图 {path}: {e}")
