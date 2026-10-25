# -*- coding: utf-8 -*-
"""Warm Paper 设计套件 —— 页面级共用组件与排版常量。

统一各页面的「页头 / 空状态 / 卡片容器 / 药丸徽章」,避免每个页面各写一套
手搓 QSS(历史上设置页用 INDEX/COLOPHON 全大写衬线、私密区用 PRIVATE
ARCHIVE 全大写、工作流页用裸列表,三种语言并存)。

原则:
- 只用调色板常量(ui_base)+ 库原生控件(qfluentwidgets TitleLabel /
  CaptionLabel / IconWidget / PushButton),不引入第二套配色;
- 深浅色跟随 init_palette 写回本模块全局(register 已登记);
- 组件只做「布局 + 一层边框」,不加阴影、不加渐变。
"""
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QLabel, QVBoxLayout, QHBoxLayout,
                               QFrame, QSizePolicy)
from PySide6.QtGui import QFont
from qfluentwidgets import (CaptionLabel, IconWidget, PushButton, TitleLabel)

import ui_base
from ui_base import *  # noqa: F401,F403
ui_base.register(sys.modules[__name__])   # 调色板随主题刷新本模块全局

# ---- 排版刻度(1px 网格) ----
PAGE_PAD_X = 32        # 页面左右留白
PAGE_PAD_TOP = 24
PAGE_GAP = 16          # 页头与内容之间
CARD_RADIUS = 12
CTRL_RADIUS = 8

# ---- 药丸色调(暖纸底上的低饱和四色,不引入冷色) ----
_TONES_LIGHT = {
    "accent":  ("@accent:0.13", "#B4552F"),
    "green":   ("rgba(63,122,79,0.12)", "#37694A"),
    "sand":    ("rgba(176,132,86,0.14)", "#7C5C34"),
    "neutral": ("#EFECE6", "#5E5D59"),
}
_TONES_DARK = {
    "accent":  ("@accent:0.20", "#E9A084"),
    "green":   ("rgba(94,201,127,0.16)", "#8FD6A7"),
    "sand":    ("rgba(210,170,120,0.18)", "#D9BC94"),
    "neutral": ("#2A2927", "#A6A49E"),
}


def tone(name="neutral"):
    """(底色, 前景色) —— 深浅色各一套,调用时求值。

    accent 项写成占位符 @accent:<alpha>,取值时现算,保证设置页换主题色后
    药丸底色跟着变。"""
    table = _TONES_DARK if globals().get("IS_DARK") else _TONES_LIGHT
    bg, fg = table.get(name, table["neutral"])
    if bg.startswith("@accent:"):
        bg = accent_rgba(float(bg.split(":", 1)[1]))
    return bg, fg


def card_qss(bg=None, border=None, radius=CARD_RADIUS, name=None):
    """卡片/容器的一段 QSS(白面 + 1px 暖灰边 + 圆角)。"""
    sel = ("#%s" % name) if name else ""
    return ("%s { background: %s; border: 1px solid %s; border-radius: %dpx; }"
            % (sel or "QFrame", bg or globals().get("CARD_BG", "#FFFFFF"),
               border or globals().get("CARD_BORDER", "#E5E3DC"), radius))


class HairLine(QFrame):
    """1px 暖灰分隔线(替代历史上 1px 纯黑的编辑风横线)。"""

    def __init__(self, vertical=False, parent=None):
        super().__init__(parent)
        color = globals().get("LINE", "#E5E3DC")
        if vertical:
            self.setFixedWidth(1)
            self.setStyleSheet("background: %s;" % color)
        else:
            self.setFixedHeight(1)
            self.setStyleSheet("background: %s;" % color)


class Chip(QLabel):
    """小药丸:步骤类型 / 数量 / 状态。文字小一号,底色低饱和。"""

    def __init__(self, text="", tone_name="neutral", parent=None):
        super().__init__(text, parent)
        self._tone_name = tone_name
        f = QFont()
        f.setPointSize(8)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.refresh_tone()

    def set_tone(self, name):
        self._tone_name = name
        self.refresh_tone()

    def refresh_tone(self):
        bg, fg = tone(self._tone_name)
        self.setStyleSheet(
            "QLabel { background: %s; color: %s; border-radius: 6px;"
            " padding: 2px 8px; }" % (bg, fg))


class PageHeader(QWidget):
    """页面头部:中文标题(库原生 TitleLabel) + 一行说明 + 右侧动作区。

    actions 里的控件右对齐、与标题同基线;不画通栏横线,靠留白与
    卡片的 1px 边框分层。
    """

    def __init__(self, title, subtitle="", actions=(), parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.title = TitleLabel(title, self)
        self.title.setStyleSheet("color: %s;" % globals().get("TEXT", "#141413"))
        row.addWidget(self.title)
        row.addStretch(1)
        self.actions = list(actions)
        for w in self.actions:
            row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(row)

        self.subtitle = CaptionLabel(subtitle, self)
        self.subtitle.setWordWrap(True)
        self.subtitle.setStyleSheet("color: %s;" % globals().get("SEC", "#5E5D59"))
        self.subtitle.setVisible(bool(subtitle))
        v.addWidget(self.subtitle)


class EmptyState(QWidget):
    """空状态:图标瓦片 + 主文案 + 说明 + 可选动作按钮(居中)。"""

    def __init__(self, icon, title, hint="", action_text="", on_action=None,
                 parent=None):
        super().__init__(parent)
        self._icon_name = icon
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 40, 0, 40)
        v.setSpacing(10)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.tile = QWidget(self)
        self.tile.setFixedSize(48, 48)
        self._tile_icon = IconWidget(self.tile)
        self._tile_icon.setFixedSize(22, 22)
        if icon is not None:
            self._tile_icon.setIcon(icon)
        tl = QVBoxLayout(self.tile)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self._tile_icon, 0, Qt.AlignmentFlag.AlignCenter)
        self._refresh_tile()

        self.lbl_title = QLabel(title, self)
        f = QFont()
        f.setPointSize(11)
        f.setWeight(QFont.Weight.DemiBold)
        self.lbl_title.setFont(f)
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_title.setStyleSheet(
            "color: %s; background: transparent;" % globals().get("TEXT", "#141413"))

        self.lbl_hint = QLabel(hint, self)
        self.lbl_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet(
            "color: %s; background: transparent;" % globals().get("SEC", "#5E5D59"))
        self.lbl_hint.setVisible(bool(hint))

        self.btn = PushButton(action_text, self)
        if on_action is not None:
            self.btn.clicked.connect(on_action)
        self.btn.setVisible(bool(action_text))

        v.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.lbl_title)
        v.addWidget(self.lbl_hint)
        v.addSpacing(4)
        v.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignHCenter)

    def _refresh_tile(self):
        self.tile.setStyleSheet(
            "background: %s; border-radius: 14px;"
            % globals().get("BADGE_BG", "#EFECE6"))

    def set_icon(self, icon):
        self._tile_icon.setIcon(icon)


class CardPanel(QFrame):
    """白面卡片容器:统一 1px 边 + 圆角 + 内边距。"""

    def __init__(self, parent=None, radius=CARD_RADIUS, pad=(14, 12, 14, 12),
                 spacing=8):
        super().__init__(parent)
        self.setObjectName("cardPanel")
        self.setStyleSheet(card_qss(name="cardPanel", radius=radius))
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*pad)
        self.body.setSpacing(spacing)


class SectionLabel(QLabel):
    """区块小标题(列表分组用,如「置顶」「最近」)。"""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        f = QFont()
        f.setPointSize(9)
        f.setWeight(QFont.Weight.DemiBold)
        self.setFont(f)
        self.setStyleSheet(
            "color: %s; background: transparent;" % globals().get("SEC", "#5E5D59"))


def action_button(text, icon=None, primary=False, parent=None):
    """页面头/工具栏统一按钮:普通 = 白底细边,primary = 陶土橙实心。"""
    btn = PushButton(text, parent)
    if icon is not None:
        btn.setIcon(icon.icon() if hasattr(icon, "icon") else icon)
    if primary:
        btn.setStyleSheet(
            "QPushButton { background: %s; color: #FFFFFF; border: 1px solid %s;"
            " border-radius: %dpx; padding: 5px 16px; font-weight: 600; }"
            "QPushButton:hover { background: %s; border-color: %s; }"
            % (globals().get("ACCENT", "#D97757"), globals().get("ACCENT", "#D97757"),
               CTRL_RADIUS, globals().get("TEXT", "#141413"),
               globals().get("TEXT", "#141413")))
    return btn
