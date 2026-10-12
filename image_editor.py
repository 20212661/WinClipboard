# -*- coding: utf-8 -*-
"""图片编辑器子系统(阶段 8 拆分,自 winclipboard_qt.py 迁出,行为不变):
压缩/缩放对话框 + 标注图元 + ImageEditor(裁剪/标注/导出)。
标注层复用 pinboard 的非破坏模型(矢量层,原图不动)。
"""
import os
import sys
import io
import json
import time
import queue
import base64
import math
import subprocess
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import (Qt, QTimer, QRectF, QRect, QSize, QEvent, QPropertyAnimation,
                            QEasingCurve, QObject, QPoint, QThread, Signal,
                            QPointF)
from PySide6.QtGui import (QGuiApplication, QPixmap, QImage, QPainter, QColor, QFont,
                           QIcon, QPainterPath, QFontMetrics, QPen, QCursor,
                           QTextCursor, QTextCharFormat, QAction, QTransform,
                           QPolygonF, QShortcut, QKeySequence, QTextDocument)
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout,
                               QHBoxLayout, QFrame, QGraphicsView,
                               QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsEllipseItem, QGraphicsPathItem,
                               QGraphicsLineItem, QGraphicsTextItem, QStyle,
                               QFileDialog, QSizePolicy, QListWidget, QLineEdit,
                               QPlainTextEdit, QPushButton, QSizeGrip,
                               QSpacerItem, QStackedWidget, QTreeWidgetItem,
                               QTableWidgetItem, QHeaderView)
from qfluentwidgets import (FluentIcon as FIF, setTheme, setThemeColor, Theme,
                            themeColor, isDarkTheme, FluentWindow, NavigationItemPosition,
                            CardWidget, SimpleCardWidget, IconWidget,
                            SearchLineEdit, SegmentedWidget, ScrollArea, ToolButton,
                            ToggleToolButton, PrimaryToolButton,
                            TransparentToolButton, PrimaryPushButton, PushButton,
                            ToggleButton, Slider, RadioButton, SpinBox, LineEdit,
                            TextEdit, ComboBox, EditableComboBox,
                            RoundMenu, MessageBox, MessageBoxBase, InfoBar,
                            PasswordLineEdit,
                            CheckBox,
                            InfoBarPosition, BodyLabel, StrongBodyLabel,
                            CaptionLabel, TitleLabel,
                            SmoothScrollDelegate, ScrollBarHandleDisplayMode,
                            TreeWidget, TableWidget,
                            SwitchSettingCard, PushSettingCard, OptionsSettingCard,
                            SettingCardGroup, SettingCard)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QMessageBox

import clipboard_manager as cm
from pinboard import annotations as _anno_layer

import ui_base
from ui_base import *  # noqa: F401,F403
from ui_base import (ensure_onscreen, pil_to_qimage, rounded_pixmap,
                     _fic, _accent_tint)


import sys as _sys
ui_base.register(_sys.modules[__name__])   # 调色板随主题同步本模块全局



class CompressDialog(MessageBoxBase):
    """压缩 / 格式转换对话框。

    基于 MessageBoxBase:内容放 viewLayout(自适应高度);
    「复制到剪贴板」复用 yesButton,点击时经 validate() 执行压缩。
    (不能直接用 Dialog —— 它 setFixedSize 固定了尺寸,且已有顶层布局,
    再套 QVBoxLayout 不会生效。)
    """

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

        self.titleLabel = StrongBodyLabel("压缩 / 格式转换")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("JPG 体积小、质量可调;PNG 无损,适合截图。")
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        qrow = QHBoxLayout()
        qlab = BodyLabel("质量")
        qlab.setStyleSheet("color: %s;" % SEC)
        self.quality = Slider(Qt.Horizontal)
        self.quality.setRange(10, 95)
        self.quality.setValue(80)
        self.valueLabel = BodyLabel("80")
        self.valueLabel.setStyleSheet("color: %s;" % TEXT)
        self.quality.valueChanged.connect(self.valueLabel.setNum)
        qrow.addWidget(qlab)
        qrow.addWidget(self.quality, 1)
        qrow.addWidget(self.valueLabel)
        self.viewLayout.addLayout(qrow)

        self.radioJpg = RadioButton("JPG(体积小,质量可调)")
        self.radioPng = RadioButton("PNG(无损)")
        self.radioJpg.setChecked(True)
        self.radioJpg.toggled.connect(self._on_format)
        self.viewLayout.addWidget(self.radioJpg)
        self.viewLayout.addWidget(self.radioPng)

        self.saveButton = PushButton("💾 保存文件…", self)
        self.saveButton.clicked.connect(self._on_save)
        self.viewLayout.addWidget(self.saveButton, 0, Qt.AlignLeft)

        self.yesButton.setText("复制到剪贴板")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(380)

    def _on_format(self, _):
        # PNG 无损,质量滑条仅对 JPG 有效
        on = self.radioJpg.isChecked()
        self.quality.setEnabled(on)
        self.valueLabel.setEnabled(on)

    def _encode(self):
        """按当前选项编码图片,返回字节流。"""
        buf = io.BytesIO()
        final = self.editor._final_image()   # 含全部矢量标注的合成图
        if self.radioJpg.isChecked():
            final.save(buf, "JPEG", quality=self.quality.value())
        else:
            final.save(buf, "PNG")
        return buf.getvalue()

    def validate(self):
        """点「复制到剪贴板」时执行(qfluentwidgets 扩展点);True 才关闭。"""
        try:
            img = cm.Image.open(io.BytesIO(self._encode()))
            ok = cm.set_dib_to_clipboard(self.editor.app.worker.hwnd,
                                         cm.pil_to_dib(img))
        except Exception:
            MessageBox("WinClipboard", "压缩失败。", self).exec()
            return False
        if ok:
            self.editor.lbl_status.setText("✅ 已压缩并复制到剪贴板")
            if self.editor._layer_mode:
                # 压缩产物是整图重编码:以新底图回写,标注层清空(已含在像素里)
                self.editor._emit_layers(base_override=img, annos_override=[])
            else:
                self.editor._emit_result(img)   # 画布回写走压缩后的版本
            return True
        InfoBar.error("复制失败", "剪贴板被占用,请稍后重试", parent=self.editor,
                      position=InfoBarPosition.BOTTOM_LEFT)
        return False

    def _on_save(self):
        if self.radioJpg.isChecked():
            path, _ = QFileDialog.getSaveFileName(
                self, "保存图片", "clipboard_compressed.jpg",
                "JPEG 图片 (*.jpg)")
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "保存图片", "clipboard_compressed.png",
                "PNG 图片 (*.png)")
        if not path:
            return
        try:
            Path(path).write_bytes(self._encode())
        except Exception:
            MessageBox("WinClipboard", "保存失败。", self).exec()
            return
        self.editor.lbl_status.setText("✅ 已保存: " + path)
        self.accept()


class ScaleDialog(MessageBoxBase):
    """缩放对话框:滑条 + 数值框联动(替代原生 QInputDialog,跟随主题)。"""

    def __init__(self, editor):
        super().__init__(editor)
        self.titleLabel = StrongBodyLabel("缩放图片")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("按百分比缩放(10% – 300%)")
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        row = QHBoxLayout()
        self.slider = Slider(Qt.Horizontal)
        self.slider.setRange(10, 300)
        self.slider.setValue(100)
        self.spin = SpinBox()
        self.spin.setRange(10, 300)
        self.spin.setValue(100)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        self.viewLayout.addLayout(row)

        self.yesButton.setText("应用")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(360)

    def value(self):
        return self.spin.value()


def _glyph_icon(kind, color=None, sz=18):
    """编辑器工具的几何图形图标(矩形/圆/箭头/画笔/马赛克/撤销/对勾/粗细)。
    库里没有这些语义图形,按工具本身的几何含义绘制;部件样式仍全部走库。"""
    pm = QPixmap(sz, sz)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = color if color is not None else QColor(TEXT)
    pen = QPen(c, 1.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    m = 2.5
    if kind == "rect":
        p.drawRect(QRectF(m, m + 1, sz - 2 * m, sz - 2 * m - 2))
    elif kind == "ellipse":
        p.drawEllipse(QRectF(m, m + 1, sz - 2 * m, sz - 2 * m - 2))
    elif kind == "arrow":
        pen.setWidthF(1.8)
        p.drawLine(QPointF(m + 1, sz - m - 1), QPointF(sz - m - 1, m + 1))
        p.drawLine(QPointF(sz - m - 1, m + 1),
                   QPointF(sz - m - 6, m + 2.5))
        p.drawLine(QPointF(sz - m - 1, m + 1),
                   QPointF(sz - m - 6.5, m + 7))
    elif kind == "pen":
        path = QPainterPath(QPointF(m, sz * 0.72))
        path.cubicTo(QPointF(sz * 0.3, sz * 0.32), QPointF(sz * 0.55, sz * 0.88),
                     QPointF(sz - m, sz * 0.28))
        pen.setWidthF(1.9)
        p.drawPath(path)
    elif kind == "mosaic":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cell = (sz - 2 * m) / 3.0
        for rr in range(3):
            for cc in range(3):
                p.drawRect(QRectF(m + cc * cell + 0.8, m + rr * cell + 0.8,
                                  cell - 1.6, cell - 1.6))
    elif kind == "undo":
        path = QPainterPath(QPointF(sz * 0.78, sz * 0.60))
        path.cubicTo(QPointF(sz * 0.74, sz * 0.18), QPointF(sz * 0.28, sz * 0.18),
                     QPointF(sz * 0.26, sz * 0.52))
        pen.setWidthF(1.8)
        p.drawPath(path)
        ex, ey = sz * 0.26, sz * 0.52
        p.drawLine(QPointF(ex, ey), QPointF(ex + 5.5, ey - 3))
        p.drawLine(QPointF(ex, ey), QPointF(ex + 5.5, ey + 5.5))
    elif kind == "select":
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([
            QPointF(m + 3, m), QPointF(m + 3, sz * 0.78),
            QPointF(m + 7, sz * 0.58), QPointF(m + 9.2, sz - m),
            QPointF(m + 11.2, sz - m - 0.8), QPointF(m + 9, sz * 0.52),
            QPointF(m + 13.5, sz * 0.5)]))
    elif kind == "check":
        pen.setWidthF(2.2)
        p.drawLine(QPointF(sz * 0.24, sz * 0.55), QPointF(sz * 0.44, sz * 0.74))
        p.drawLine(QPointF(sz * 0.44, sz * 0.74), QPointF(sz * 0.78, sz * 0.28))
    elif kind == "width":
        for i, w in ((0, 1.2), (1, 2.2), (2, 3.4)):
            pen.setWidthF(w)
            y = m + 2 + i * ((sz - 2 * m - 4) / 2.0)
            p.drawLine(QPointF(m + 1, y), QPointF(sz - m - 1, y))
    p.end()
    return QIcon(pm)


def _qimage_to_pil(qimg):
    """QImage(ARGB32) → PIL RGB(白底合成);编辑器场景渲染导出用。"""
    w, h = qimg.width(), qimg.height()
    rgba = cm.Image.frombuffer("RGBA", (w, h), bytes(qimg.constBits()),
                               "raw", "BGRA", qimg.bytesPerLine(), 1)
    out = cm.Image.new("RGB", (w, h), (255, 255, 255))
    out.paste(rgba, mask=rgba.split()[3])
    return out


# ---- 标注项(矢量:可选中/可拖动/可删除;文字双击再编辑) ----
_SEL_PEN = None          # 选中角标画笔(固定样式,模块级缓存,别在 paint 里反复构造)


def _sel_border(item, painter, option):
    """选中态虚线角标(所有标注项共用)。"""
    global _SEL_PEN
    if not (option.state & QStyle.State_Selected):
        return
    if _SEL_PEN is None:
        _SEL_PEN = QPen(QColor("#00A2FF"), 1)
        _SEL_PEN.setCosmetic(True)
        _SEL_PEN.setStyle(Qt.DashLine)
    painter.setPen(_SEL_PEN)
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(item.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5))


class _AnnoMixin:
    """标注通用行为:editor 反向引用 + 拖动起止记录(撤销 move 用)。"""

    def _anno_init(self, editor):
        self.editor = editor
        self.setFlag(QGraphicsPixmapItem.ItemIsMovable, True)
        self.setFlag(QGraphicsPixmapItem.ItemIsSelectable, True)
        self._move_from = None

    def mousePressEvent(self, e):
        self._move_from = self.pos()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._move_from is not None and self._move_from != self.pos():
            ed = getattr(self, "editor", None)
            if ed is not None:
                ed._record({"type": "move", "item": self, "pos": self._move_from})
        self._move_from = None


class AnnoRect(_AnnoMixin, QGraphicsRectItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoEllipse(_AnnoMixin, QGraphicsEllipseItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoPath(_AnnoMixin, QGraphicsPathItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoMosaic(_AnnoMixin, QGraphicsPixmapItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)


class AnnoText(_AnnoMixin, QGraphicsTextItem):
    def paint(self, p, o, w=None):
        super().paint(p, o, w)
        _sel_border(self, p, o)

    def mouseDoubleClickEvent(self, e):
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus()
        super().mouseDoubleClickEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        if not self.toPlainText().strip():
            ed = getattr(self, "editor", None)
            if ed is not None:
                ed._drop_anno(self, record=False)
                ed._prune_add_record(self)


class ImageEditor(QWidget):
    """图片编辑器(QQ 风格矢量标注)。两种形态:
    - 独立窗口(parent=None,兼容旧入口/测试)
    - 内嵌组件(parent=查看器等容器,不新开窗口——查看↔编辑同窗切换)"""

    finished = Signal()            # 内嵌形态:编辑结束(完成/Esc),容器收回

    def __init__(self, app, rec, on_result=None, parent=None,
                 annos=None, layer_mode=False):
        super().__init__(parent)
        self.app = app
        self.rec = rec
        self._on_result = on_result     # 编辑产出回调(图钉画布回写用);None=普通编辑
        self._layer_mode = layer_mode   # 非破坏层回写(emit annotations 而非像素)
        self._src_path = str(rec.get("content") or "")
        self._base_dirty = False        # 裁剪/旋转/缩放等底图操作(需替换资产)
        self._embedded = parent is not None
        if not self._embedded:
            self.setWindowTitle("图片处理%s — %s" % (
                "(完成复制/保存后自动更新画布)" if on_result else "",
                os.path.basename(rec["content"])))
            self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
            self.setWindowIcon(app.icon)
            self.resize(940, 640)
        self.setStyleSheet("QWidget { background: %s; }" % BG)   # 跟随主题(仅本部件子树)

        try:
            src = cm.Image.open(rec["content"])
            if src.mode != "RGB":
                bg = cm.Image.new("RGB", src.size, (255, 255, 255))
                bg.paste(src, mask=src.split()[-1] if src.mode in ("RGBA", "LA") else None)
                src = bg
            self.base = src
        except Exception:
            MessageBox("WinClipboard", "无法打开图片文件。", self).exec()
            self.base = cm.Image.new("RGB", (400, 300), (255, 255, 255))

        self.undo_stack = []           # 撤销记录(add/del/move/img)
        self.tool = None
        self._drag_start = None
        self._preview = None          # 拖动中的形状预览项(场景内)
        self._preview_path = None     # 画笔预览路径
        self._pen_pts = None          # 画笔采样点(视图坐标)
        self.annos = []               # 标注矢量项(创建序即 z 序)
        self._z = 0
        self._color = QColor("#FF4040")     # 标注颜色(默认红,QQ 风格)
        self._pen_w = 4                      # 线宽:细2 / 中4 / 粗8
        self._font_px = 26                   # 标注文字像素高

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setStyleSheet(
            "QGraphicsView { background: #26292F; border: none; }")
        self.view.setDragMode(QGraphicsView.RubberBandDrag)   # 选择模式=框选
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self._pix_item = QGraphicsPixmapItem()
        self.scene.addItem(self._pix_item)
        v.addWidget(self.view, 1)

        # 状态角标(左上角小字):必须透明背景+鼠标穿透——CaptionLabel 脱离
        # 布局后 parent 到窗口会带出 640×480 的默认尺寸,一旦有背景色/收
        # 事件就是一张盖住画布的白卡(真机已踩)
        self.lbl_status = CaptionLabel("")
        self.lbl_status.setParent(self)
        self.lbl_status.setFont(font(11))
        self.lbl_status.setStyleSheet("color: %s; background: transparent;"
                                      % SEC)
        self.lbl_status.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.lbl_status.adjustSize()

        self._build_toolbar()          # 底部悬浮工具条(QQ 截图编辑风格)

        # 覆写视图鼠标事件做标注绘制;无工具(选择模式)时转发回默认处理器,
        # 让场景项走 Qt 原生的选中/拖动/文字编辑
        self._ovp = self.view.mousePressEvent
        self._ovm = self.view.mouseMoveEvent
        self._ovr = self.view.mouseReleaseEvent
        self.view.mousePressEvent = self._press
        self.view.mouseMoveEvent = self._move
        self.view.mouseReleaseEvent = self._release
        self._redraw()
        if annos:
            self.load_annotations(annos)   # 图钉画布回编:标注层原样恢复

    # ---- 工具条(底部居中悬浮药丸) ----
    COLORS = ("#000000", "#FFFFFF", "#FF4040", "#FF9500",
              "#FFD60A", "#34C759", "#007AFF", "#AF52DE")
    COLOR_NAMES = {"#000000": "黑", "#FFFFFF": "白", "#FF4040": "红",
                   "#FF9500": "橙", "#FFD60A": "黄", "#34C759": "绿",
                   "#007AFF": "蓝", "#AF52DE": "紫"}
    PEN_WIDTHS = ((2, "细"), (4, "中"), (8, "粗"))

    def _build_toolbar(self):
        bar = CardWidget(self)
        self._bar = bar
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 7, 10, 6)
        lay.setSpacing(0)
        self._tbtns = {}

        def tbtn(icon, text, tip, checkable=True, cb=None, primary=False):
            # 竖排组合:图标按钮 + 下方小字(库原生 ToggleToolButton 有明确
            # 选中态;QToolButton 的 textUnderIcon 样式与库 QSS 不合,图标
            # 文字会叠压,实测不可用)
            wrap = QWidget(bar)
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(0)
            b = (PrimaryToolButton if primary else
                 ToggleToolButton if checkable else TransparentToolButton)(wrap)
            b.setIcon(icon)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setFixedSize(44, 38)
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.PointingHandCursor)
            if cb is not None:
                b.clicked.connect(cb)
            lb = CaptionLabel(text, wrap)
            lb.setFont(font(10))
            lb.setAlignment(Qt.AlignCenter)
            wl.addWidget(b, 0, Qt.AlignHCenter)
            wl.addWidget(lb, 0, Qt.AlignHCenter)
            lay.addWidget(wrap)
            return b

        def vsep():
            ln = QFrame(bar)
            ln.setFixedSize(1, 44)
            ln.setStyleSheet("background: %s;" % LINE)
            lay.addWidget(ln)
            lay.addSpacing(4)

        tbtn(_glyph_icon("undo"), "撤销", "撤销上一步(Ctrl+Z)", False,
             self._on_undo)
        lay.addSpacing(2)
        tools = (("select", _glyph_icon("select"), "选择", "点击选中标注后拖动移动,"
                 "Del 删除;双击文字再编辑;空白拖动框选多个"),
                 ("rect", _glyph_icon("rect"), "矩形", "拖动绘制矩形(可连续标注)"),
                 ("ellipse", _glyph_icon("ellipse"), "圆形", "拖动绘制椭圆"),
                 ("arrow", _glyph_icon("arrow"), "箭头", "拖动绘制箭头"),
                 ("pen", _glyph_icon("pen"), "画笔", "按住拖动自由绘制"),
                 ("text", FIF.FONT.icon(), "文字", "点击图片输入文字(双击可再编辑)"),
                 ("mosaic", _glyph_icon("mosaic"), "马赛克", "拖动框选区域打马赛克"),
                 ("crop", FIF.CUT.icon(), "裁剪", "拖动框选裁剪区域"))
        for key, icon, text, tip in tools:
            self._tbtns[key] = tbtn(icon, text, tip,
                                    cb=lambda _=False, k=key: self._on_tool(k))
        vsep()
        self._btn_color = tbtn(self._color_icon(), "颜色", "标注颜色",
                               False, self._pick_color)
        self._btn_width = tbtn(_glyph_icon("width"), "粗细", "线条粗细",
                               False, self._pick_width)
        vsep()
        self._btn_more = tbtn(FIF.MORE.icon(), "更多", "保存 / 压缩 / 旋转 / 缩放",
                              False, self._more_menu)
        self._btn_done = tbtn(_glyph_icon("check", QColor("#FFFFFF")), "完成",
                              "复制到剪贴板并关闭(画布模式同时更新画布)",
                              False, self._on_done, primary=True)

    def _color_icon(self):
        pm = QPixmap(18, 18)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(TEXT), 1.2))
        p.setBrush(self._color)
        p.drawEllipse(2.5, 2.5, 13, 13)
        p.end()
        return QIcon(pm)

    def _menu_at(self, btn):
        """菜单弹出锚点(编辑器坐标系)。按钮包在容器里,geometry() 相对容器,
        必须经 mapTo 从按钮自身坐标系换算——此前用 bar.mapFromParent 误把
        容器坐标当父层坐标,菜单被双重偏移弹到屏幕外(表现为点了没反应)。"""
        return btn.mapTo(self, btn.rect().center())

    def _pick_color(self):
        m = RoundMenu(parent=self)
        for hexc in self.COLORS:
            pm = QPixmap(16, 16)
            pm.fill(Qt.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.Antialiasing)
            pp.setPen(QPen(QColor(TEXT), 1))
            pp.setBrush(QColor(hexc))
            pp.drawEllipse(1.5, 1.5, 13, 13)
            pp.end()
            a = QAction("● " + self.COLOR_NAMES.get(hexc, hexc), m)
            a.setIcon(QIcon(pm))
            a.triggered.connect(lambda _=False, c=hexc: self._set_color(c))
            m.addAction(a)
        m.exec(self._menu_at(self._btn_color))

    def _set_color(self, hexc):
        self._color = QColor(hexc)
        self._btn_color.setIcon(self._color_icon())
        self._apply_style_selected()

    def _pick_width(self):
        m = RoundMenu(parent=self)
        for w, name in self.PEN_WIDTHS:
            mark = "● " if w == self._pen_w else ""
            a = QAction("%s%s(%dpx)" % (mark, name, w), m)
            a.triggered.connect(lambda _=False, v=w: self._set_width(v))
            m.addAction(a)
        m.exec(self._menu_at(self._btn_width))

    def _set_width(self, w):
        self._pen_w = int(w)
        self._apply_style_selected()

    def _apply_style_selected(self):
        """把当前颜色/粗细应用到选中的标注(选中即改,可撤销);无选中只改
        后续绘制参数。文字只改颜色;马赛克不适用。"""
        old = []
        col = QColor(self._color)
        for it in self.annos:
            if not it.isSelected():
                continue
            if isinstance(it, AnnoText):
                old.append((it, "text", QColor(it.defaultTextColor())))
                it.setDefaultTextColor(col)
            elif not isinstance(it, AnnoMosaic):
                pen = QPen(it.pen())
                old.append((it, "pen", QPen(it.pen())))
                pen.setColor(col)
                pen.setWidthF(float(self._pen_w))
                it.setPen(pen)
        if old:
            self._record({"type": "style", "old": old})

    def _more_menu(self):
        m = RoundMenu(parent=self)

        def act(text, fn):
            a = QAction(text, m)
            a.triggered.connect(lambda _=False: fn())
            m.addAction(a)

        act("保存到文件…", self._on_save)
        act("压缩导出…", self._on_compress)
        m.addSeparator()
        act("⟲ 向左旋转 90°", lambda: self._transform(
            lambda im: im.rotate(90, expand=True)))
        act("⟳ 向右旋转 90°", lambda: self._transform(
            lambda im: im.rotate(-90, expand=True)))
        act("按百分比缩放…", self._scale_dialog)
        m.exec(self._menu_at(self._btn_more))

    def _scale_dialog(self):
        d = ScaleDialog(self)
        if d.exec() and d.value() != 100:
            vv = d.value()
            self._transform(lambda im: im.resize(
                (max(1, int(im.width * vv / 100)), max(1, int(im.height * vv / 100))),
                cm.RESAMPLE))

    def _layout_overlays(self):
        if not hasattr(self, "_bar"):
            return
        self._bar.adjustSize()
        self._bar.move(max(8, (self.width() - self._bar.width()) // 2),
                       self.height() - self._bar.height() - 14)
        self._bar.raise_()
        self.lbl_status.move(14, 10)
        self.lbl_status.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_overlays()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)
        self._layout_overlays()

    # ---- 工具 ----
    def _on_tool(self, key):
        self._clear_preview()
        if key == "select":
            self.tool = None
        elif self.tool == key:
            self.tool = None
        else:
            self.tool = key
        # 选择模式开框选;绘制工具关掉避免和形状预览打架
        self.view.setDragMode(QGraphicsView.RubberBandDrag
                              if self.tool is None
                              else QGraphicsView.NoDrag)
        self._sync_tools()
        self._status()

    def _sync_tools(self):
        for k, b in self._tbtns.items():
            b.setChecked(k == self.tool or
                         (k == "select" and self.tool is None))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.tool is not None:
                self._on_tool("select")     # 先退工具,再按才退出(QQ 式)
            elif self._embedded:
                self.finished.emit()        # 内嵌形态:交还容器(查看器回查看态)
            else:
                self.close()
        elif e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self._delete_selected()
        elif e.key() == Qt.Key_Z and e.modifiers() & Qt.ControlModifier:
            self._on_undo()
        else:
            super().keyPressEvent(e)

    # ---- 文字标注(场景内直接输入,双击可再编辑;空文字失焦自动清除) ----
    def _place_text(self, sp):
        it = AnnoText("")
        f = QFont(self.font())
        f.setPixelSize(self._font_px)
        f.setBold(True)
        it.setFont(f)
        it.setDefaultTextColor(QColor(self._color))
        it.setPos(sp)
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        self.annos.append(it)
        self._record({"type": "add", "item": it})
        it.setTextInteractionFlags(Qt.TextEditorInteraction)
        it.setFocus()
        self._status()

    # ---- 预览项(拖动中的形状,场景坐标系) ----
    def _clear_preview(self):
        if self._preview is not None:
            self.scene.removeItem(self._preview)
            self._preview = None
        self._preview_path = None

    def _pen_style(self, dashed=False):
        pen = QPen(QColor(self._color) if not dashed else QColor("#FF5252"),
                   1.5 if dashed else self._pen_w)
        pen.setCosmetic(True)          # 视觉线宽不随视图缩放
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        if dashed:
            pen.setStyle(Qt.DashLine)
        return pen

    def _anno_pen(self):
        pen = QPen(QColor(self._color), self._pen_w)   # 图像空间线宽
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _press(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.tool is None:
            self._ovp(e)               # 选择模式:交给 Qt 原生项交互
            return
        if self.tool == "text":
            self._place_text(self.view.mapToScene(e.position().toPoint()))
            return
        if self.tool in ("crop", "rect", "ellipse", "arrow", "pen", "mosaic"):
            self._drag_start = e.position()
            self._pen_pts = [e.position()] if self.tool == "pen" else None

    def _move(self, e):
        if self._drag_start is None:
            if self.tool is None:
                self._ovm(e)
            return
        # 预览必须画在场景坐标系(mapToScene):视图缩放/平移后仍贴着鼠标
        s1 = self.view.mapToScene(self._drag_start.toPoint())
        s2 = self.view.mapToScene(e.position().toPoint())
        if self.tool == "pen":
            self._pen_pts.append(e.position())
            if self._preview is None:
                self._preview_path = QPainterPath(s1)
                self._preview = QGraphicsPathItem(self._preview_path)
                self._preview.setPen(self._pen_style())
                self.scene.addItem(self._preview)
            else:
                self._preview_path.lineTo(s2)
                self._preview.setPath(self._preview_path)
            return
        r = QRectF(s1, s2).normalized()
        dashed = self.tool in ("crop", "mosaic")
        need = (QGraphicsRectItem if self.tool in ("rect", "crop", "mosaic")
                else QGraphicsEllipseItem if self.tool == "ellipse"
                else QGraphicsLineItem)
        if self._preview is None or not isinstance(self._preview, need):
            self._clear_preview()
            self._preview = need()
            self._preview.setPen(self._pen_style(dashed=dashed))
            self.scene.addItem(self._preview)
        if isinstance(self._preview, QGraphicsLineItem):
            self._preview.setLine(s1.x(), s1.y(), s2.x(), s2.y())
        else:
            self._preview.setRect(r)

    def _release(self, e):
        if self._drag_start is None:
            if self.tool is None:
                self._ovr(e)
            return
        start = self.view.mapToScene(self._drag_start.toPoint())
        end = self.view.mapToScene(e.position().toPoint())
        pen_pts = self._pen_pts
        self._drag_start = None
        self._pen_pts = None
        self._clear_preview()
        tool = self.tool
        if tool == "pen":
            pts = [self._to_img(self.view.mapToScene(p.toPoint()))
                   for p in (pen_pts or [])]
            if len(pts) >= 2 and (abs(pts[-1][0] - pts[0][0]) +
                                  abs(pts[-1][1] - pts[0][1]) > 2):
                self._finish_anno("pen", pts=pts)
            self._status()
            return
        ix0, iy0 = self._to_img(start)
        ix1, iy1 = self._to_img(end)
        if tool == "arrow":
            # 箭头有方向性:必须用原始 起点→终点;下面的 min/max 归一化
            # 会把所有箭头掰成「往右下」,反向/斜向拖拽方向即错(真机已现)
            if (ix1 - ix0) ** 2 + (iy1 - iy0) ** 2 >= 9:
                self._finish_anno("arrow", (ix0, iy0), (ix1, iy1))
            return
        ix0, ix1 = sorted((ix0, ix1))
        iy0, iy1 = sorted((iy0, iy1))
        ix0, iy0 = max(0, ix0), max(0, iy0)
        ix1, iy1 = min(self.base.width, ix1), min(self.base.height, iy1)
        if ix1 - ix0 < 3 or iy1 - iy0 < 3:
            return
        if tool == "crop":
            old_base, old_annos = self._flatten()
            self.base = self.base.crop((ix0, iy0, ix1, iy1))
            self._record({"type": "img", "base": old_base,
                          "annos": old_annos})
            self.tool = None
            self._sync_tools()
            self._redraw()
            return
        # 矢量标注:矩形/椭圆/箭头/马赛克 → 场景项,后续可选中/移动/删除
        self._finish_anno(tool, (ix0, iy0), (ix1, iy1))
        # QQ 式:标注工具保持激活,可连续标注;选择工具才可移动标注

    def _finish_anno(self, kind, p0=None, p1=None, pts=None):
        """按当前颜色/线宽把一个标注收成场景矢量项。"""
        col = QColor(self._color)
        r = QRectF(QPointF(p0[0], p0[1]),
                   QPointF(p1[0], p1[1])).normalized() if p1 is not None else None
        if kind == "rect":
            it = AnnoRect(r)
            it.setPen(self._anno_pen())
            it.setBrush(Qt.NoBrush)
        elif kind == "ellipse":
            it = AnnoEllipse(r)
            it.setPen(self._anno_pen())
            it.setBrush(Qt.NoBrush)
        elif kind == "arrow":
            it = AnnoPath(self._arrow_path(QPointF(0, 0),
                                           QPointF(p1[0] - p0[0],
                                                   p1[1] - p0[1])))
            it.setPos(p0[0], p0[1])
            pen = self._anno_pen()
            pen.setCosmetic(False)
            it.setPen(pen)
            it.setBrush(col)
        elif kind == "pen":
            path = QPainterPath(QPointF(0, 0))
            for pt in pts[1:]:
                path.lineTo(pt[0] - pts[0][0], pt[1] - pts[0][1])
            it = AnnoPath(path)
            it.setPos(pts[0][0], pts[0][1])
            it.setPen(self._anno_pen())
            it.setBrush(Qt.NoBrush)
        elif kind == "mosaic":
            pm = self._mosaic_patch(r)
            if pm is None:
                return
            it = AnnoMosaic(pm)
            it.setTransformationMode(Qt.FastTransformation)
            it.setPos(r.topLeft())
            it._factor = 12              # 块状因子(序列化用,重生成靠原图)
        else:
            return
        it._kind = kind                  # 序列化标记(annotations.serialize_items)
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        self.annos.append(it)
        self._record({"type": "add", "item": it})
        self._status()

    @staticmethod
    def _arrow_path(p0, p1):
        return _anno_layer.arrow_path(p0, p1)

    def _mosaic_patch(self, r, factor=12):
        """区域马赛克:从原图裁剪 → 缩小 → 放大(块状),返回像素化贴片。
        只存区域+因子(见 annotations),贴片每次从底图现算。"""
        rect = r.toAlignedRect().intersected(
            QRect(0, 0, self.base.width, self.base.height))
        if rect.width() < 2 or rect.height() < 2:
            return None
        region = self.base.crop((rect.x(), rect.y(),
                                 rect.right() + 1, rect.bottom() + 1))
        small = region.resize((max(1, region.width // factor),
                               max(1, region.height // factor)), cm.RESAMPLE)
        pix = small.resize(region.size, cm.Image.NEAREST)
        return QPixmap.fromImage(pil_to_qimage(pix))

    # ---- 标注层序列化(非破坏模型:矢量参数,不含派生像素) ----
    def annotations(self):
        """当前标注 → 可持久化 dict 列表(图像像素坐标)。"""
        return _anno_layer.serialize_items(self.annos)

    def load_annotations(self, lst):
        """恢复标注层(图钉画布回编):逐条重建为可交互编辑项。"""
        loaded = []
        for d in _anno_layer.sanitize(lst):
            it = self._restore_anno(d)
            if it is not None:
                self.annos.append(it)
                loaded.append(it)
        if loaded:
            self._status()
        return loaded

    def _restore_anno(self, d):
        t = d.get("t")
        col = QColor(d.get("c") or "#FF4040")
        pw = float(d.get("pw") or 4.0)

        def pen():
            p = QPen(col, pw)
            p.setCapStyle(Qt.RoundCap)
            p.setJoinStyle(Qt.RoundJoin)
            return p

        if t in ("rect", "ellipse"):
            r = QRectF(d["x"], d["y"], d["w"], d["h"])
            it = AnnoRect(r) if t == "rect" else AnnoEllipse(r)
            it.setPen(pen())
            it.setBrush(Qt.NoBrush)
        elif t == "arrow":
            it = AnnoPath(self._arrow_path(
                QPointF(0, 0),
                QPointF(d["x2"] - d["x1"], d["y2"] - d["y1"])))
            it.setPos(d["x1"], d["y1"])
            p = pen()
            p.setCosmetic(False)
            it.setPen(p)
            it.setBrush(col)
        elif t == "pen":
            pts = d["pts"]
            path = QPainterPath(QPointF(0, 0))
            for p in pts[1:]:
                path.lineTo(p[0] - pts[0][0], p[1] - pts[0][1])
            it = AnnoPath(path)
            it.setPos(pts[0][0], pts[0][1])
            it.setPen(pen())
            it.setBrush(Qt.NoBrush)
        elif t == "text":
            it = AnnoText(str(d.get("s") or ""))
            f = QFont(self.font())
            f.setPixelSize(int(d.get("px") or 26))
            f.setBold(True)
            it.setFont(f)
            it.setDefaultTextColor(col)
            it.setPos(d["x"], d["y"])
            it.setTextInteractionFlags(Qt.NoTextInteraction)
        elif t == "mosaic":
            pm = self._mosaic_patch(
                QRectF(d["x"], d["y"], d["w"], d["h"]), int(d.get("f") or 12))
            if pm is None:
                return None
            it = AnnoMosaic(pm)
            it.setTransformationMode(Qt.FastTransformation)
            it.setPos(d["x"], d["y"])
            it._factor = int(d.get("f") or 12)
        else:
            return None
        it._kind = t
        self.scene.addItem(it)
        it._anno_init(self)
        self._z += 1
        it.setZValue(self._z)
        return it

    # ---- 标注管理 / 撤销 ----
    def _record(self, rec):
        self.undo_stack.append(rec)
        del self.undo_stack[:-40]

    def _prune_add_record(self, item):
        """空文字项自动清除时,弹掉它栈顶的 add 记录(撤销不该复活空项)。"""
        if self.undo_stack and self.undo_stack[-1].get("type") == "add" \
                and self.undo_stack[-1].get("item") is item:
            self.undo_stack.pop()

    def _drop_anno(self, it, record=True):
        if it in self.annos:
            self.annos.remove(it)
        self.scene.removeItem(it)
        if record:
            self._record({"type": "del", "items": [it]})
        self._status()

    def _delete_selected(self):
        sel = [i for i in self.annos if i.isSelected()]
        if not sel:
            return
        for it in sel:
            self.annos.remove(it)
            self.scene.removeItem(it)
        self._record({"type": "del", "items": sel})
        self._status()

    def _flatten(self):
        """把全部标注栅格化进位图(变换/裁剪前),返回旧 (base, annos) 供撤销。
        栅格化即底图变更:非破坏层回写时需替换资产。"""
        old = (self.base, list(self.annos))
        if self.annos:
            self._clear_preview()
            self.base = self._final_image()
            for it in self.annos:
                self.scene.removeItem(it)
            self.annos = []
        self._base_dirty = True
        return old

    def _final_image(self):
        """导出图:无标注=原图;有标注=场景整体渲染(所见即所得,含文字)。"""
        if not self.annos:
            return self.base
        w, h = self.base.size
        sel = [(it, it.isSelected()) for it in self.annos]
        for it, _ in sel:
            it.setSelected(False)
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(Qt.white)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        self.scene.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
        p.end()
        for it, s in sel:
            it.setSelected(s)
        return _qimage_to_pil(img)

    def _transform(self, fn):
        old_base, old_annos = self._flatten()
        self.base = fn(self.base)
        self._base_dirty = True
        self._record({"type": "img", "base": old_base, "annos": old_annos})
        self._redraw()

    def _to_img(self, sp):
        r = self._pix_item.boundingRect()
        sc = self._pix_item.scale()
        # round 而非 int 截断:视图<->场景浮点往返有亚像素噪声,截断会整框偏 1px
        return (round((sp.x() - r.x()) / sc), round((sp.y() - r.y()) / sc))

    def _redraw(self):
        w, h = self.base.size
        self._pix_item.setPixmap(QPixmap.fromImage(pil_to_qimage(self.base)))
        self.view.resetTransform()
        self.scene.setSceneRect(QRectF(0, 0, w, h))
        self.view.fitInView(self._pix_item, Qt.KeepAspectRatio)
        self._status()

    def _status(self):
        w, h = self.base.size
        tip = {"crop": "拖动框选裁剪区域", "rect": "拖动绘制矩形(可连续标注)",
               "ellipse": "拖动绘制椭圆", "arrow": "拖动绘制箭头",
               "pen": "按住拖动自由绘制", "text": "点击图片输入文字(双击可再编辑)",
               "mosaic": "拖动框选区域打马赛克"}.get(
                   self.tool, "选择:点击标注后拖动移动,Del 删除,双击文字再编辑")
        self.lbl_status.setText("%s   |   标注 %d 个   |   尺寸: %s×%s   |   可撤销 %d 步" % (
            tip, len(self.annos), w, h, len(self.undo_stack)))
        self.lbl_status.adjustSize()

    def _on_undo(self):
        self._clear_preview()
        if not self.undo_stack:
            return
        rec = self.undo_stack.pop()
        t = rec["type"]
        if t == "add":
            it = rec["item"]
            if it in self.annos:
                self._drop_anno(it, record=False)
        elif t == "del":
            for it in rec["items"]:
                self.scene.addItem(it)      # item 对象仍在,重新挂回场景
                self.annos.append(it)
        elif t == "move":
            rec["item"].setPos(rec["pos"])
        elif t == "style":
            for it, kind, val in rec["old"]:
                if it.scene() is not self.scene:   # 项已被删除则跳过
                    continue
                if kind == "text":
                    it.setDefaultTextColor(val)
                else:
                    it.setPen(val)
        elif t == "img":
            self.base = rec["base"]
            for it in self.annos:
                self.scene.removeItem(it)
            self.annos = []
            for it in rec["annos"]:
                self.scene.addItem(it)
                self.annos.append(it)
        self._redraw()

    def _emit_result(self, result):
        """把编辑产物(路径 str 或 PIL.Image)交给调用方;失败不影响编辑器本身。"""
        if self._on_result is None:
            return
        try:
            self._on_result(result)
        except Exception:
            cm.log("编辑结果回写失败:\n%s" % traceback.format_exc())

    def _emit_layers(self, base_override=None, annos_override=None):
        """非破坏层回写(图钉画布):标注层 + 原图引用;只有底图被
        裁剪/旋转等破坏性操作改写过时才携带新底图像素。"""
        if not self._layer_mode or self._on_result is None:
            return
        base = base_override
        if base is None and self._base_dirty:
            base = self.base               # 现底图(此前的标注已在破坏性操作时栅格化)
        annos = self.annotations() if annos_override is None else annos_override
        self._emit_result({"kind": "layers", "image": self._src_path,
                           "annotations": annos, "base": base})

    def _on_done(self):
        """✓ 完成:复制到剪贴板(画布模式同时回写画布)并关闭编辑器。"""
        self._clear_preview()
        final = self._final_image()
        ok = cm.set_dib_to_clipboard(self.app.worker.hwnd,
                                     cm.pil_to_dib(final))
        if not ok:
            InfoBar.error("复制失败", "剪贴板被占用,可改用「更多 → 保存到文件」",
                          parent=self, position=InfoBarPosition.TOP)
            return
        if self._layer_mode:
            self._emit_layers()
        else:
            self._emit_result(final)
        if self._embedded:
            self.finished.emit()
        else:
            self.close()

    def _on_save(self):
        self._clear_preview()
        p, _ = QFileDialog.getSaveFileName(
            self, "保存图片", "clipboard_edit.png", "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
        if p:
            fmt = "JPEG" if p.lower().endswith((".jpg", ".jpeg")) else "PNG"
            try:
                self._final_image().save(p, fmt)
                self.lbl_status.setText("✅ 已保存: " + p)
                self.lbl_status.adjustSize()
                if self._layer_mode:
                    self._emit_layers()    # 画布走非破坏层;文件是合成导出
                else:
                    self._emit_result(p)
            except Exception:
                MessageBox("WinClipboard", "保存失败。", self).exec()

    def _on_compress(self):
        CompressDialog(self).exec()


