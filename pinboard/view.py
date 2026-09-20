# -*- coding: utf-8 -*-
"""画布视图与场景(大画板模型)。

场景是一块随内容扩张的大画布,不再把图片钳回可视区;平移/缩放自由导航:

- 空白处左键拖动 = 橡皮筋框选
- 中键拖动 或 Space+左键拖动 = 平移画布(Space 对图上按压也生效)
- Alt + 空白拖动 = 移动整个窗口(标题栏拖动同效)
- 悬图滚轮 = 缩放该图(光标锚);空白滚轮 = 缩放整板(AnchorUnderMouse)
- Ctrl+滚轮 = 选中图透明度;Alt+滚轮 = 细粒度旋转
- 双击图 = 1:1 ↔ 适应;右键菜单;图片文件可直接拖进来钉住
"""
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QPixmap
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from qfluentwidgets import isDarkTheme, themeColor

from .items import PinImageItem

# 点阵背景纹理缓存:按 (主题, 步长) 缓存画刷——drawBackground 每帧都跑,
# Python 循环逐点绘制在最远缩放档要 ~60ms/帧(实测),纹理填充只要 ~2ms
_DOT_BRUSHES = {}


def _dot_brush(dark, step):
    key = (bool(dark), step)
    b = _DOT_BRUSHES.get(key)
    if b is None:
        c = QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 26)
        tile = QPixmap(int(step), int(step))
        tile.fill(Qt.transparent)
        tp = QPainter(tile)
        tp.setRenderHint(QPainter.Antialiasing)
        r = step * 0.06 + 0.7          # 点半径随间距缩放,缩放时视觉稳定
        if r <= 1.0:
            tp.setPen(QPen(c, 1))
            tp.drawPoint(int(step / 2), int(step / 2))
        else:
            tp.setPen(QPen(c, 1))
            tp.drawEllipse(QRectF(step / 2 - r, step / 2 - r, r * 2, r * 2))
        tp.end()
        b = QBrush(tile)
        _DOT_BRUSHES[key] = b
    return b


class BoardScene(QGraphicsScene):
    """场景:持有 board 反向引用;选中变化通知窗口刷新计数。"""

    def __init__(self, board):
        super().__init__(board)
        self.board = board
        self.selectionChanged.connect(board.on_selection_changed)


class BoardView(QGraphicsView):
    def __init__(self, board):
        super().__init__(board.scene)
        self.board = board
        self.setRenderHints(QPainter.Antialiasing |
                            QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.RubberBandDrag)   # 空白左键拖动 = 框选
        self.setAcceptDrops(True)
        self._win_drag = None
        self._pan = None         # 平移锚(全局点 + 滚动条初值)
        self._space = False      # Space 按住(平移预备)

    # ---- 大画板背景:点阵网格(纹理画刷填充;画板感 + 提供空间参照) ----
    def drawBackground(self, p, rect):
        dark = isDarkTheme()
        step = 50.0
        # 缩得太远时抽稀点阵步长,并量化到 50×2^k 以复用缓存的纹理
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        while (vr.width() / step) * (vr.height() / step) > 3600:
            step *= 2
        p.fillRect(rect, _dot_brush(dark, step))

    def paintEvent(self, e):
        super().paintEvent(e)
        # 任何视口变化(平移/缩放/居中/resize/show)都伴随重绘:以重绘为信号
        # 节流扫描可见区,视口外的图不吃解码与内存(见 manager._check_visible)
        self.board.mgr.schedule_visible_check()

    # ---- 键盘:Space 平移预备 ----
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space:
            self._space = True
            self.viewport().setCursor(Qt.OpenHandCursor)
            e.accept()
            return
        if self.board.handle_key(e):
            e.accept()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space:
            self._space = False
            self.viewport().unsetCursor()
            e.accept()
            return
        super().keyReleaseEvent(e)

    # ---- 鼠标:平移 / 移窗 / 框选 ----
    def _want_pan(self, e):
        if e.button() == Qt.MiddleButton:
            return True
        return e.button() == Qt.LeftButton and self._space

    def mousePressEvent(self, e):
        if self._want_pan(e):
            self._start_pan(e)
            e.accept()
            return
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.AltModifier) \
                and self.itemAt(e.position().toPoint()) is None:
            self._win_drag = (e.globalPosition().toPoint(),
                              self.board.frameGeometry().topLeft())
            e.accept()
            return
        super().mousePressEvent(e)

    def _start_pan(self, e):
        self._pan = (e.globalPosition().toPoint(),
                     self.horizontalScrollBar().value(),
                     self.verticalScrollBar().value())
        self.viewport().setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            if e.buttons() & (Qt.LeftButton | Qt.MiddleButton):
                g = e.globalPosition().toPoint() - self._pan[0]
                self.horizontalScrollBar().setValue(self._pan[1] - g.x())
                self.verticalScrollBar().setValue(self._pan[2] - g.y())
            e.accept()
            return
        if self._win_drag is not None and (e.buttons() & Qt.LeftButton):
            g0, w0 = self._win_drag
            self.board.move(w0 + (e.globalPosition().toPoint() - g0))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            self.viewport().unsetCursor()
            self.board.mgr.schedule_save()      # 视图位置变了
            e.accept()
            return
        if self._win_drag is not None:
            self._win_drag = None
            self.board.mgr.schedule_save()      # 窗口位置变了
            e.accept()
            return
        super().mouseReleaseEvent(e)

    # ---- 滚轮分发 ----
    def wheelEvent(self, e):
        pos = e.position().toPoint()
        it = self.itemAt(pos)
        mods = e.modifiers()
        up = e.angleDelta().y() > 0
        if isinstance(it, PinImageItem):
            targets = self.board.targets_for(it)
            if mods & Qt.ControlModifier:
                self.board.nudge_opacity(targets, 0.05 if up else -0.05)
            elif mods & Qt.AltModifier:
                self.board.rotate_items(targets, 4.0 if up else -4.0)
            else:
                self.board.scale_item(it, 1.12 if up else 1 / 1.12,
                                      self.mapToScene(pos))
        elif mods & (Qt.ControlModifier | Qt.AltModifier):
            pass          # 空白处 Ctrl/Alt 滚轮不整板缩放,防误触
        else:
            self.board.zoom_view(1.1 if up else 1 / 1.1)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        it = self.itemAt(e.position().toPoint())
        if isinstance(it, PinImageItem):
            self.board.toggle_fit_item(it)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e):
        it = self.itemAt(e.position().toPoint())
        if isinstance(it, PinImageItem):
            self.board.item_menu(it, e.globalPos())
        else:
            self.board.board_menu(e.globalPos())

    # ---- 拖图片文件进来钉住 ----
    def dragEnterEvent(self, e):
        if any(u.isLocalFile() and str(u.toLocalFile()).lower().endswith(
                self.board.mgr.img_exts) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        scene_pos = self.mapToScene(e.position().toPoint())
        for u in e.mimeData().urls():
            self.board.mgr.pin_path(str(u.toLocalFile()), scene_pos=scene_pos)
        e.acceptProposedAction()
