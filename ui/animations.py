# -*- coding: utf-8 -*-
"""Atelier Zero 动效库。

克制、单色调、无弹跳、无 3D。
仅使用 QPropertyAnimation + 透明度/几何属性。
"""
from PySide6.QtCore import QPropertyAnimation, QEasingCurve, QAbstractAnimation
from PySide6.QtWidgets import QWidget


def fade_in(widget: QWidget, duration_ms=700, start=0.0, end=1.0,
            easing=QEasingCurve.Type.OutCubic, on_finished=None):
    """让 widget 从 start 透明度淡入到 end。

    若 widget 已有 opacity effect，会复用；否则新建 QGraphicsOpacityEffect。
    """
    if widget is None:
        return None

    widget.show()
    opacity = widget.windowOpacity()
    # 尝试复用已有的 opacity effect
    effect = widget.graphicsEffect()
    if effect is None or not hasattr(effect, "setOpacity"):
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)

    effect.setOpacity(start)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(easing)
    if on_finished is not None:
        anim.finished.connect(on_finished)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def slide_in(widget: QWidget, direction="down", distance=24, duration_ms=700,
             easing=QEasingCurve.Type.OutCubic, on_finished=None):
    """让 widget 从指定方向滑入原位。

    direction: down/up/left/right
    """
    if widget is None:
        return None

    widget.show()
    geom = widget.geometry()
    x, y, w, h = geom.x(), geom.y(), geom.width(), geom.height()

    start_pos = (x, y)
    if direction == "down":
        start_pos = (x, y - distance)
    elif direction == "up":
        start_pos = (x, y + distance)
    elif direction == "left":
        start_pos = (x + distance, y)
    elif direction == "right":
        start_pos = (x - distance, y)

    widget.move(start_pos[0], start_pos[1])
    anim = QPropertyAnimation(widget, b"pos", widget)
    anim.setDuration(duration_ms)
    anim.setStartValue(start_pos)
    anim.setEndValue((x, y))
    anim.setEasingCurve(easing)
    if on_finished is not None:
        anim.finished.connect(on_finished)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_insert(widget: QWidget, duration_ms=700):
    """列表/卡片插入时的标准淡入，可组合轻微下滑。"""
    fade_in(widget, duration_ms=duration_ms)
    slide_in(widget, direction="down", distance=8, duration_ms=duration_ms)
