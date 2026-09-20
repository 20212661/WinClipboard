# -*- coding: utf-8 -*-
"""图钉画布排列算法(纯几何,不触持久化;窗口层调用后统一 sync 场景 + 落盘)。

所有函数直接操作 QGraphicsItem 列表:包围盒取 mapRectToScene(含缩放/旋转),
位移用 moveBy(场景系平移,不受旋转变换影响),因此对旋转过的图片同样成立。
"""
from PySide6.QtCore import QRectF


def _box(it):
    """条目当前场景包围盒(含缩放与旋转)。"""
    return it.mapRectToScene(it.boundingRect())


def _move_topleft(it, x, y):
    b = _box(it)
    it.moveBy(x - b.left(), y - b.top())


def grid(items, rect, gap=18, pad=14):
    """逐行铺排(保留各自缩放/旋转):按左上角顺序,行宽不超 rect。"""
    its = sorted(items, key=lambda i: (i.pos().y(), i.pos().x()))
    x0, y0 = rect.left() + pad, rect.top() + pad
    x, y = x0, y0
    row_h = 0.0
    for it in its:
        b = _box(it)
        if x > x0 and x + b.width() > rect.right() - pad:
            x = x0
            y += row_h + gap
            row_h = 0.0
        _move_topleft(it, x, y)
        x += b.width() + gap
        row_h = max(row_h, b.height())


def row(items, rect, gap=18, pad=14):
    """横向单行排列。"""
    its = sorted(items, key=lambda i: (i.pos().y(), i.pos().x()))
    x = rect.left() + pad
    for it in its:
        b = _box(it)
        _move_topleft(it, x, rect.top() + pad)
        x += b.width() + gap


def column(items, rect, gap=18, pad=14):
    """纵向单列排列。"""
    its = sorted(items, key=lambda i: (i.pos().y(), i.pos().x()))
    y = rect.top() + pad
    for it in its:
        b = _box(it)
        _move_topleft(it, rect.left() + pad, y)
        y += b.height() + gap


def compact(items, rect, gap=14, pad=14):
    """紧凑排列:按高度降序做 shelf packing(先放高的,矮的塞余白),
    大小差异大时比 grid 留白更少。"""
    its = sorted(items, key=lambda i: _box(i).height(), reverse=True)
    x0, y0 = rect.left() + pad, rect.top() + pad
    x, y = x0, y0
    row_h = 0.0
    for it in its:
        b = _box(it)
        if x > x0 and x + b.width() > rect.right() - pad:
            x = x0
            y += row_h + gap
            row_h = 0.0
        _move_topleft(it, x, y)
        x += b.width() + gap
        row_h = max(row_h, b.height())


def align(items, mode):
    """对齐到全体包围盒的某条边/中线:mode ∈
    left/right/top/bottom/hcenter/vcenter。"""
    if not items:
        return
    u = QRectF()
    for it in items:
        u = u.united(_box(it))
    for it in items:
        b = _box(it)
        if mode == "left":
            it.moveBy(u.left() - b.left(), 0)
        elif mode == "right":
            it.moveBy(u.right() - b.right(), 0)
        elif mode == "top":
            it.moveBy(0, u.top() - b.top())
        elif mode == "bottom":
            it.moveBy(0, u.bottom() - b.bottom())
        elif mode == "hcenter":
            it.moveBy(u.center().x() - b.center().x(), 0)
        elif mode == "vcenter":
            it.moveBy(0, u.center().y() - b.center().y())


def distribute(items, axis):
    """等间距:axis="h" 沿水平方向均分间隙,"v" 沿垂直方向;首尾不动。"""
    if len(items) < 3:
        return
    its = sorted(items, key=lambda i: (_box(i).left() if axis == "h"
                                       else _box(i).top()))
    u = QRectF()
    for it in its:
        u = u.united(_box(it))
    if axis == "h":
        span = sum(_box(i).width() for i in its)
        free = u.width() - span
        gap = free / (len(its) - 1)
        cursor = u.left()
        for it in its:
            b = _box(it)
            it.moveBy(cursor - b.left(), 0)
            cursor += b.width() + gap
    else:
        span = sum(_box(i).height() for i in its)
        free = u.height() - span
        gap = free / (len(its) - 1)
        cursor = u.top()
        for it in its:
            b = _box(it)
            it.moveBy(0, cursor - b.top())
            cursor += b.height() + gap
