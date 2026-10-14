# -*- coding: utf-8 -*-
"""Atelier Zero 原生设置组件。

QFrame 分组 + 1px 顶线 + 大写标签 + 原生控件。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QWidget, QVBoxLayout, QHBoxLayout,
                                QLabel, QPushButton, QCheckBox, QSpinBox,
                                QComboBox, QSizePolicy)

try:
    from ui.theme import (PAPER, INK, INK_LIGHT, ACCENT, DANGER,
                          head_font, body_font, label_font)
except ImportError:
    from theme import (PAPER, INK, INK_LIGHT, ACCENT, DANGER,
                       head_font, body_font, label_font)


class _Divider(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setStyleSheet("background: %s;" % INK)


def _btn_style(primary=False, danger=False):
    if danger:
        return ("QPushButton { background: %s; color: %s; border: 1px solid %s; "
                "border-radius: 0px; padding: 6px 14px; }"
                "QPushButton:hover { background: %s; color: %s; }" %
                (PAPER, DANGER, DANGER, DANGER, PAPER))
    if primary:
        return ("QPushButton { background: %s; color: %s; border: 1px solid %s; "
                "border-radius: 0px; padding: 6px 14px; }"
                "QPushButton:hover { background: %s; color: %s; }" %
                (ACCENT, PAPER, ACCENT, INK, PAPER))
    return ("QPushButton { background: %s; color: %s; border: 1px solid %s; "
            "border-radius: 0px; padding: 6px 14px; }"
            "QPushButton:hover { background: %s; color: %s; }" %
            (PAPER, INK, INK, INK, PAPER))


class AtelierGroup(QFrame):
    """Atelier Zero 设置分组：1px 顶线 + 大写标题 + 内容垂直排列。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QFrame { background: transparent; border: none; }")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(8)
        # 1px 顶线
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: %s;" % INK)
        self.v.addWidget(line)
        # 大写标题
        ttl = QLabel(title.upper())
        ttl.setFont(label_font(10, QFont.Weight.DemiBold))
        ttl.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        self.v.addWidget(ttl)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 4, 0, 12)
        self.body.setSpacing(0)
        self.v.addLayout(self.body)

    def add_row(self, row):
        self.body.addWidget(row)


class AtelierRow(QFrame):
    """单行设置项：标题 + 说明 + 右侧控件。"""

    def __init__(self, title, desc, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QFrame { background: transparent; border: none; }")
        self.h = QHBoxLayout(self)
        self.h.setContentsMargins(0, 10, 0, 10)
        self.h.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(2)
        self.title_lbl = QLabel(title)
        self.title_lbl.setFont(body_font(12, QFont.Weight.DemiBold))
        self.title_lbl.setStyleSheet("color: %s; background: transparent;" % INK)
        left.addWidget(self.title_lbl)
        if desc:
            self.desc_lbl = QLabel(desc)
            self.desc_lbl.setFont(body_font(10))
            self.desc_lbl.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
            left.addWidget(self.desc_lbl)
        self.h.addLayout(left, 1)

    def add_widget(self, w, align=Qt.AlignmentFlag.AlignVCenter):
        self.h.addWidget(w, 0, align)


class AtelierSwitchRow(AtelierRow):
    """开关行。"""

    def __init__(self, title, desc, checked=False, on_change=None, parent=None):
        super().__init__(title, desc, parent)
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(checked)
        self.checkbox.setStyleSheet(
            "QCheckBox { background: transparent; }"
            "QCheckBox::indicator { width: 20px; height: 20px; }"
            "QCheckBox::indicator:checked { background: %s; border: 2px solid %s; }"
            "QCheckBox::indicator:unchecked { background: %s; border: 2px solid %s; }" %
            (ACCENT, ACCENT, PAPER, INK))
        if on_change:
            self.checkbox.stateChanged.connect(lambda s: on_change(s == Qt.CheckState.Checked))
        self.add_widget(self.checkbox)

    def set_checked(self, on):
        self.checkbox.setChecked(on)

    setChecked = set_checked  # 兼容 qfluentwidgets SwitchSettingCard API

    def is_checked(self):
        return self.checkbox.isChecked()


class AtelierSpinRow(AtelierRow):
    """数值行。"""

    def __init__(self, title, desc, value, minv, maxv, on_change, parent=None):
        super().__init__(title, desc, parent)
        self.spin = QSpinBox()
        self.spin.setRange(minv, maxv)
        self.spin.setValue(value)
        self.spin.setMinimumWidth(80)
        self.spin.setStyleSheet(
            "QSpinBox { background: %s; color: %s; border: 1px solid %s; "
            "border-radius: 0px; padding: 4px; }" % (PAPER, INK, INK))
        if on_change:
            self.spin.valueChanged.connect(on_change)
        self.add_widget(self.spin)


class AtelierComboRow(AtelierRow):
    """下拉行。"""

    def __init__(self, title, desc, items, current_index, on_change, parent=None):
        super().__init__(title, desc, parent)
        self.combo = QComboBox()
        self.combo.addItems(items)
        self.combo.setCurrentIndex(current_index)
        self.combo.setStyleSheet(
            "QComboBox { background: %s; color: %s; border: 1px solid %s; "
            "border-radius: 0px; padding: 4px 8px; }"
            "QComboBox QAbstractItemView { background: %s; color: %s; "
            "selection-background-color: %s; selection-color: %s; }" %
            (PAPER, INK, INK, PAPER, INK, INK, PAPER))
        if on_change:
            self.combo.currentIndexChanged.connect(on_change)
        self.add_widget(self.combo)


class AtelierButton(QPushButton):
    def __init__(self, text, primary=False, danger=False, parent=None):
        super().__init__(text, parent)
        self.setFont(label_font(9))
        self.setStyleSheet(_btn_style(primary=primary, danger=danger))
