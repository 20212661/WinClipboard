# -*- coding: utf-8 -*-
"""WarmRowDelegate —— 暖纸风列表行代理。

用于私密区、上下文包目录等 QListWidget 列表。与主界面剪贴板卡片同一套
语言:白卡片 + 1px 暖灰边 + 圆角,选中是淡陶土底 + 左侧陶土橙指示条
(不再整行反相成黑底白字——那是早期「杂志期」的遗留风格)。

- 序号(001 起,可关)用等宽字,弱化到 TER 灰
- 标题 13 DemiBold / 副文 11 号 SEC
- 右侧:类型标签 + 时间 + 置顶徽章,一律右对齐
- 圆角裁剪后绘制,保证选中底色不出框

数据约定不变:
    index.data(Qt.UserRole)      -> dict 记录
    index.data(Qt.UserRole + 1)  -> str 序号(可选)
"""
import os
import sys
from datetime import datetime

from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

import ui_base
from ui_base import *  # noqa: F401,F403
ui_base.register(sys.modules[__name__])


def _fmt_time(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _elide(text, font, width):
    if not text:
        return ""
    fm = QFontMetrics(font)
    return fm.elidedText(str(text).replace("\n", " "),
                         Qt.TextElideMode.ElideRight, max(0, int(width)))


def _tone(alpha_hex, base=None):
    c = QColor(base or ACCENT)
    c.setAlpha(alpha_hex)
    return c


class EditorialDelegate(QStyledItemDelegate):
    """暖纸风列表行(类名保留,避免既有导入断裂)。"""

    HEIGHT = 68
    MARGIN_LEFT = 14
    MARGIN_RIGHT = 14
    INDEX_W = 40
    TYPE_W = 56
    TIME_W = 86
    SRC_W = 110
    RADIUS = 10

    def __init__(self, parent=None, show_index=True):
        super().__init__(parent)
        self.show_index = show_index

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.HEIGHT)

    # ---- 绘制 ----
    def paint(self, painter, option, index):
        rec = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(rec, dict):
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        outer = option.rect
        card = QRectF(outer.left() + 2, outer.top() + 3,
                      outer.width() - 10, outer.height() - 6)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected) \
            and bool(option.state & QStyle.StateFlag.State_Active)

        # 卡片底 / 边
        path = QPainterPath()
        path.addRoundedRect(card, self.RADIUS, self.RADIUS)
        painter.setClipPath(path)
        if selected:
            painter.fillPath(path, _tone(24))
            border = _tone(115)
        elif hover:
            painter.fillPath(path, QColor(SURFACE_HOVER))
            border = QColor(BORDER_HOVER)
        else:
            painter.fillPath(path, QColor(CARD_BG))
            border = QColor(CARD_BORDER)
        painter.setClipping(False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border, 1))
        painter.drawPath(path)

        # 选中:左侧 3px 陶土橙指示条(圆角内侧)
        if selected:
            bar = QPainterPath()
            bar.addRoundedRect(QRectF(card.left(), card.top(), 3,
                                      card.height()), 1.5, 1.5)
            painter.fillPath(bar, QColor(ACCENT))

        x = card.left() + self.MARGIN_LEFT + (4 if selected else 0)
        top = int(card.top())
        h = int(card.height())
        avail = card.width() - self.MARGIN_LEFT - self.MARGIN_RIGHT - 6

        # 序号
        if self.show_index:
            idx_text = index.data(Qt.ItemDataRole.UserRole + 1) or \
                str(index.row() + 1).zfill(3)
            f_idx = ui_base.font(9)
            painter.setFont(f_idx)
            painter.setPen(QColor(TER))
            painter.drawText(int(x), top, self.INDEX_W, h,
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft, idx_text)
            x += self.INDEX_W + 10
            avail -= self.INDEX_W + 10

        # 右侧信息列宽度(按内容有无决定)
        kind = str(rec.get("type", "text")).upper()
        t_txt = _fmt_time(rec.get("ts"))
        src_txt = str(rec.get("src") or "")
        right_w = self.TYPE_W + 12
        if t_txt:
            right_w += self.TIME_W
        if src_txt:
            right_w += self.SRC_W
        if rec.get("pinned"):
            right_w += 52

        text_w = min(avail - right_w - 16, 460)

        # 标题 / 副文
        title, subtitle = self._summary(rec, rec.get("type", "text"))
        f_title = ui_base.font(11, QFont.Weight.DemiBold if selected
                               else QFont.Weight.Medium)
        painter.setFont(f_title)
        painter.setPen(QColor(TEXT))
        painter.drawText(int(x), top, int(text_w), int(h * 0.58),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                         _elide(title, f_title, text_w))

        if subtitle:
            f_sub = ui_base.font(9)
            painter.setFont(f_sub)
            painter.setPen(QColor(SEC))
            painter.drawText(int(x), top + int(h * 0.58), int(text_w),
                             int(h * 0.42) - 4,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             _elide(subtitle, f_sub, text_w))

        # 右侧:类型 → 时间 → 来源 → 置顶
        rx = card.right() - self.MARGIN_RIGHT
        f_lab = ui_base.font(8)
        painter.setFont(f_lab)
        if rec.get("pinned"):
            rx -= 46
            painter.setPen(QColor(ACCENT))
            painter.drawText(int(rx), top, 46, h,
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignRight, "置顶")
        if src_txt:
            rx -= self.SRC_W
            painter.setPen(QColor(TER))
            painter.drawText(int(rx), top, self.SRC_W - 10, h,
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignRight,
                             _elide(src_txt, f_lab, self.SRC_W - 10))
        if t_txt:
            rx -= self.TIME_W
            painter.setPen(QColor(TER))
            painter.drawText(int(rx), top, self.TIME_W - 10, h,
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignRight, t_txt)
        rx -= self.TYPE_W
        painter.setFont(ui_base.font(8, QFont.Weight.DemiBold))
        painter.setPen(QColor(ACCENT_TEXT if selected else TER))
        painter.drawText(int(rx), top, self.TYPE_W - 8, h,
                         Qt.AlignmentFlag.AlignVCenter
                         | Qt.AlignmentFlag.AlignRight, kind)

        painter.restore()

    def _summary(self, rec, kind):
        content = rec.get("content", "")
        if kind == "image":
            return "图片", "%s × %s" % (rec.get("w", 0), rec.get("h", 0))
        if kind == "files":
            if isinstance(content, list) and content:
                return os.path.basename(content[0]), "%d 个文件" % len(content)
            return "文件", ""
        lines = [l for l in str(content).splitlines() if l.strip()]
        title = lines[0] if lines else ""
        subtitle = lines[1] if len(lines) > 1 else ""
        return title, subtitle

    def editorEvent(self, event, model, option, index):
        return False        # 只读
