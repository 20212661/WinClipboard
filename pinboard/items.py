# -*- coding: utf-8 -*-
"""画布图元:PinImageItem(QGraphicsPixmapItem)。

一张钉图 = 一个 item + 一份 PinRecord。缩放语义:rec.scale 相对原始像素;
条目显示的可能是降采样 preview(见 manager.PinImageLoader),此时 Qt 缩放
需要换算,对外统一走 set_scale_value / scale_value,调用方无需关心。
"""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPen, QColor, QPainterPath, QFont
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsTextItem

from qfluentwidgets import isDarkTheme

from .model import MIN_SCALE, MAX_SCALE

# 选中/占位虚线画笔缓存:paint 每帧每项都会用到,按主题色名缓存避免反复构造
_DASH_PENS = {}


def _dash_pen(color):
    key = color.name()
    pen = _DASH_PENS.get(key)
    if pen is None:
        pen = QPen(color, 2, Qt.DashLine)
        _DASH_PENS[key] = pen
    return pen


class PinImageItem(QGraphicsPixmapItem):
    """可选中/可拖动的钉图;锁定后不可拖动但可选中/删除。"""

    def __init__(self, rec, board):
        super().__init__()
        self.rec = rec
        self.board = board
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsPixmapItem.ItemIsSelectable, True)
        self._update_movable()
        self.setTransformationMode(Qt.SmoothTransformation)
        self._full = False           # 当前 pixmap 是否全分辨率(否则为 preview)
        self._want_full = False      # 滚出视口时释放了全图但用户要 1:1,回视口自动恢复
        self._mode = "free"          # free / fit / actual(双击切换用)
        self._fit_scale = None       # 记住「适应」比例,双击两态可预测
        self._grp_start = None       # 组拖动前各选中项位置快照
        self._note_item = None       # 单图备注(QGraphicsTextItem 子项)
        self.setOpacity(rec.opacity)
        self.setRotation(rec.rotation)
        self.setZValue(rec.z)
        self.setPos(rec.x, rec.y)
        self._apply_scale(rec.scale)
        if rec.note:
            self.set_note(rec.note)

    # ---- 锁定 ----
    @property
    def locked(self):
        return self.rec.locked

    def set_locked(self, on):
        self.rec.locked = bool(on)
        self._update_movable()

    def _update_movable(self):
        self.setFlag(QGraphicsPixmapItem.ItemIsMovable, not self.rec.locked)

    # ---- 缩放换算(rec.scale 相对原始像素) ----
    def _apply_scale(self, s):
        pm = self.pixmap()
        rw = float(self.rec.w or pm.width())
        if not pm.isNull() and rw > 0 and not self._full:
            s = s * rw / pm.width()      # preview 降采样系数补偿
        self.setScale(s)

    def scale_value(self):
        pm = self.pixmap()
        rw = float(self.rec.w or pm.width())
        if not pm.isNull() and rw > 0 and not self._full:
            return self.scale() * pm.width() / rw
        return self.scale()

    def set_scale_value(self, s):
        s = max(MIN_SCALE, min(MAX_SCALE, s))
        self.rec.scale = s
        self._apply_scale(s)
        self._mode = "free"

    # ---- pixmap 装填(manager 回填 preview / 按需全图) ----
    def set_preview(self, pm):
        self.prepareGeometryChange()
        self.setPixmap(pm)
        self._full = False
        self._apply_scale(self.rec.scale)
        self._layout_note()

    def set_full(self, pm):
        self.prepareGeometryChange()
        self.setPixmap(pm)
        self._full = True
        self._apply_scale(self.rec.scale)
        self._layout_note()

    def use_preview(self):
        """释放全分辨率 pixmap,换回 manager 缓存的 preview(LRU 淘汰用)。"""
        pm = self.board.mgr.preview_pixmap(self.rec.asset)
        if pm is None or not self._full:
            return
        self.set_preview(pm)
        if self._mode == "actual":
            self._mode = "free"     # preview 撑不住 1:1,退回自由态

    # ---- 视口感知加载(manager 视口扫描调用,见 _check_visible) ----
    def ensure_preview(self):
        """进入视口:无图时请求 preview;曾因滚出视口释放全图则恢复 1:1。"""
        if self.pixmap().isNull():
            self.board.mgr.submit_preview(self.rec)
        elif self._want_full:
            self._want_full = False
            self._mode = "actual"   # 几何没变,恢复 1:1 意图即可
            self.board.mgr.request_full(self)

    def release_full_viewport(self):
        """滚出视口:全图退回 preview 提前还内存;1:1 意图记在 _want_full。"""
        if not self._full:
            return
        self._want_full = self._mode == "actual"
        self.use_preview()

    # ---- 单图备注:QGraphicsTextItem 挂为子项,移动/缩放/旋转/置顶自动跟随 ----
    def set_note(self, text):
        """设置/更新/清除备注;数据存 rec.note(序列化),展示为图片子项。"""
        self.rec.note = (text or "").strip()
        if not self.rec.note:
            if self._note_item is not None:
                if self._note_item.scene() is not None:
                    self.scene().removeItem(self._note_item)
                self._note_item = None
            return
        if self._note_item is None:
            n = QGraphicsTextItem(self)          # 父项=图片 → 变换全继承
            n.setAcceptedMouseButtons(Qt.MouseButton.NoButton)  # 不吃图片的鼠标事件
            n.setTextInteractionFlags(Qt.NoTextInteraction)
            n.setFlag(QGraphicsTextItem.ItemIsSelectable, False)
            f = QFont("Microsoft YaHei UI")
            f.setPointSize(9)
            f.setBold(True)
            n.setFont(f)
            c = QColor(255, 255, 255, 232) if isDarkTheme() else QColor(0, 0, 0, 220)
            n.setDefaultTextColor(c)
            self._note_item = n
        self._note_item.setPlainText(self.rec.note)
        self._layout_note()

    def _layout_note(self):
        """备注贴在图片下方(pixmap 尺寸变化后重摆)。"""
        n = self._note_item
        if n is None:
            return
        b = self.boundingRect()
        n.setPos(0, b.bottom() + 8)

    # ---- 占位框(图片未解码完成时可见可点) ----
    def boundingRect(self):
        r = super().boundingRect()
        if r.isEmpty() and self.rec.w and self.rec.h:
            return QRectF(0, 0, self.rec.w, self.rec.h)
        return r

    # ---- 交互 ----
    def mousePressEvent(self, e):
        if not self.rec.locked:
            self.setZValue(self.board.top_z())
        self._grp_start = {i: i.pos() for i in self.scene().selectedItems()
                           if not i.rec.locked}
        self._grp_start.setdefault(self, self.pos())
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        grp = self._grp_start
        if grp and len(grp) > 1 and (e.buttons() & Qt.LeftButton):
            d = self.pos() - grp[self]
            for it, p0 in grp.items():
                if it is not self:
                    it.setPos(p0 + d)

    def mouseReleaseEvent(self, e):
        self._grp_start = None
        super().mouseReleaseEvent(e)
        board = getattr(self.scene(), "board", None)
        if board is not None:
            board.after_items_moved()

    def hoverEnterEvent(self, e):
        self.setCursor(Qt.ArrowCursor if self.rec.locked else Qt.SizeAllCursor)
        super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self.unsetCursor()
        super().hoverLeaveEvent(e)

    # ---- 绘制:占位框 / 选中态 / 锁定角标 ----
    def paint(self, p, option, widget=None):
        if self.pixmap().isNull():
            p.setPen(_dash_pen(self.board.theme_color()))
            p.drawRect(self.boundingRect().adjusted(1, 1, -1, -1))
        else:
            super().paint(p, option, widget)
        if self.isSelected():
            p.setPen(_dash_pen(self.board.theme_color()))
            p.drawRect(self.boundingRect().adjusted(1, 1, -1, -1))
        if self.rec.locked:
            self._paint_lock(p)

    def _paint_lock(self, p):
        """右上角小锁角标(纯几何绘制,不依赖字体渲染)。"""
        r = self.boundingRect()
        s = 10.0                       # 锁体边长(DIU,DPI 由 Qt 换算)
        x = r.right() - s - 6
        y = r.top() + 6
        c = QColor(255, 255, 255, 230)
        p.setPen(QPen(QColor(0, 0, 0, 140), 1))
        p.setBrush(c)
        p.drawRoundedRect(x, y + s * 0.45, s, s * 0.55, 2, 2)
        path = QPainterPath()
        path.arcMoveTo(x + s * 0.2, y, s * 0.6, s * 0.7, 180)
        path.arcTo(x + s * 0.2, y, s * 0.6, s * 0.7, 180, -180)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
