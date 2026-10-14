# -*- coding: utf-8 -*-
"""EditorialDelegate —— Atelier Zero 编辑风格列表代理。

用于历史记录、私密区、上下文包目录等列表视图。
设计要点：
- 编号索引(001 起)
- 1px 底线
- hover 左侧 4px Études 蓝竖条
- 选中反相为黑底纸字
- 置顶项用 Études 蓝标记
- 无圆角
"""
import os
from datetime import datetime

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QStyle, QStyledItemDelegate,
                                QStyleOptionViewItem, QWidget)

try:
    from ui.theme import (PAPER, INK, INK_LIGHT, ACCENT, label_font,
                          body_font, head_font, FONT_BODY, FONT_LABEL)
except ImportError:
    from theme import (PAPER, INK, INK_LIGHT, ACCENT, label_font,
                       body_font, head_font, FONT_BODY, FONT_LABEL)


def _fmt_time(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


def _elide_text(text, font, max_width):
    if not text:
        return ""
    fm = QFontMetrics(font)
    return fm.elidedText(str(text).replace("\n", " "), Qt.TextElideMode.ElideRight, max_width)


class EditorialDelegate(QStyledItemDelegate):
    """Atelier Zero 编辑风格列表项代理。

    期望数据：
        index.data(Qt.ItemDataRole.UserRole) -> dict 记录
    可选数据：
        index.data(Qt.ItemDataRole.UserRole + 1) -> str 序号格式(如 "001")
    """

    HEIGHT = 72
    MARGIN_LEFT = 20
    MARGIN_RIGHT = 16
    COL_INDEX_W = 48
    TYPE_W = 64
    TIME_W = 52
    SRC_W = 120
    BLUE_BAR_W = 4

    def __init__(self, parent=None, show_index=True):
        super().__init__(parent)
        self.show_index = show_index

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.HEIGHT)

    def paint(self, painter, option, index):
        rec = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(rec, dict):
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        rect = option.rect
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        active = bool(option.state & QStyle.StateFlag.State_Active)

        # 反相选中
        bg = QColor(PAPER)
        fg = QColor(INK)
        if selected and active:
            bg = QColor(INK)
            fg = QColor(PAPER)

        # 背景
        painter.fillRect(rect, bg)

        # hover 左侧蓝色竖条
        if hover and not selected:
            painter.fillRect(rect.left(), rect.top(), self.BLUE_BAR_W,
                             rect.height(), QColor(ACCENT))

        # 1px 底线
        painter.setPen(QColor(INK))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        # 布局计算
        x = rect.left() + self.MARGIN_LEFT
        y = rect.top()
        h = rect.height()
        avail = rect.width() - self.MARGIN_LEFT - self.MARGIN_RIGHT

        # 序号
        if self.show_index:
            idx_text = index.data(Qt.ItemDataRole.UserRole + 1) or str(index.row() + 1).zfill(3)
            painter.setFont(label_font(10))
            painter.setPen(QColor(INK_LIGHT) if not selected else QColor(PAPER))
            painter.drawText(x, y, self.COL_INDEX_W, h,
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             idx_text)
            x += self.COL_INDEX_W + 12
            avail -= self.COL_INDEX_W + 12

        # 内容摘要
        kind = rec.get("type", "text")
        title, subtitle = self._summary(rec, kind)
        title_font = body_font(13, QFont.Weight.DemiBold if selected else QFont.Weight.Normal)
        sub_font = body_font(11)
        title_w = min(avail - self.TIME_W - self.SRC_W - self.TYPE_W - 40, 420)

        painter.setFont(title_font)
        painter.setPen(fg)
        painter.drawText(x, y + 16, title_w, 24,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elide_text(title, title_font, title_w))

        if subtitle:
            painter.setFont(sub_font)
            painter.setPen(QColor(PAPER if selected else INK_LIGHT))
            painter.drawText(x, y + 40, title_w, 20,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             _elide_text(subtitle, sub_font, title_w))

        # 类型
        tx = x + title_w + 16
        painter.setFont(label_font(9))
        painter.setPen(QColor(ACCENT) if rec.get("pinned") else (QColor(PAPER if selected else INK_LIGHT)))
        painter.drawText(tx, y, self.TYPE_W, h,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         kind.upper())

        # 时间
        tx += self.TYPE_W + 16
        painter.setPen(QColor(PAPER if selected else INK_LIGHT))
        painter.drawText(tx, y, self.TIME_W, h,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         _fmt_time(rec.get("ts")))

        # 来源
        tx += self.TIME_W + 16
        painter.drawText(tx, y, self.SRC_W, h,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         _elide_text(rec.get("src", ""), label_font(9), self.SRC_W))

        # 置顶标记
        if rec.get("pinned"):
            pin_x = rect.right() - 56
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.drawLine(pin_x, y + 26, pin_x + 40, y + 26)
            painter.setFont(label_font(8))
            painter.setPen(QColor(ACCENT))
            painter.drawText(pin_x, y + 30, 40, 20,
                             Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                             "PINNED")

        painter.restore()

    def _summary(self, rec, kind):
        content = rec.get("content", "")
        if kind == "image":
            title = "IMAGE"
            subtitle = f"{rec.get('w', 0)} × {rec.get('h', 0)}"
        elif kind == "files":
            if isinstance(content, list) and content:
                title = os.path.basename(content[0])
                subtitle = f"{len(content)} FILES"
            else:
                title = "FILES"
                subtitle = ""
        elif kind == "text":
            lines = [l for l in str(content).splitlines() if l.strip()]
            title = lines[0] if lines else ""
            subtitle = " ".join(lines[1:2]) if len(lines) > 1 else ""
        else:
            title = str(content).splitlines()[0] if content else ""
            subtitle = ""
        return title, subtitle

    def editorEvent(self, event, model, option, index):
        # 拦截编辑事件，保持只读
        return False
