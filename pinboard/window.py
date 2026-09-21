# -*- coding: utf-8 -*-
"""置顶图钉画布窗口。

无边框 + 置顶 + Tool(不进 Alt+Tab);标题栏或 Alt+空白拖动移窗,右下角 grip
改窗口大小;空白拖动框选,悬图滚轮缩放(光标锚),双击 1:1↔适应;
Del 删除 / R 旋转 / L 锁定 / Ctrl+滚轮 透明度 / Home·End·PgUp·PgDn 层级;
Esc 收起(内容保留)。状态持久化与资产生命周期在 manager.PinBoardManager,
本类只管交互与视图。
"""
import os
import traceback

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QGuiApplication, QAction, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSizeGrip,
                               QFileDialog)

from qfluentwidgets import (FluentIcon as FIF, RoundMenu, MessageBox,
                            MessageBoxBase, InfoBar, InfoBarPosition,
                            StrongBodyLabel, CaptionLabel,
                            TransparentToolButton, TextEdit, LineEdit,
                            isDarkTheme, themeColor)

import clipboard_manager as cm

from . import layout
from .items import PinImageItem
from .model import MIN_SCALE, MAX_SCALE
from .view import BoardScene, BoardView

# 整板缩放护栏;下限取 4%——大画板内容可能很远,「适应全部」要装得下
MIN_ZOOM, MAX_ZOOM = 0.04, 8.0


def _ensure_onscreen(w):
    """把窗口强制移动到当前屏幕可见范围内(多显示器/拔屏后恢复用)。"""
    try:
        scr = QGuiApplication.screenAt(w.frameGeometry().center()) \
            or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
    except Exception:
        return
    f = w.frameGeometry()
    x = min(max(f.x(), geo.left()), max(geo.left(), geo.right() - f.width()))
    y = min(max(f.y(), geo.top()), max(geo.top(), geo.bottom() - f.height()))
    if (x, y) != (f.x(), f.y()):
        w.move(x, y)


class NoteDialog(MessageBoxBase):
    """单图备注编辑(添加/修改/删除一体:清空文本=删除备注)。"""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.titleLabel = StrongBodyLabel("图片备注")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("备注贴在图片下方,随图片移动/缩放/置顶;清空则删除")
        sub.setStyleSheet("color: %s;" % ("rgba(255,255,255,0.63)"
                                         if isDarkTheme() else "rgba(0,0,0,0.63)"))
        self.viewLayout.addWidget(sub)
        self.edit = TextEdit(self)
        self.edit.setPlainText(text or "")
        self.edit.setPlaceholderText("给这张图写点备注…")
        self.edit.setFixedHeight(110)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(380)

    def text(self):
        return self.edit.toPlainText()


class PinBoardWindow(QWidget):
    MIN_W, MIN_H = 420, 260
    GRID_GAP = 18

    def __init__(self, mgr):
        super().__init__(None)
        self.mgr = mgr
        self._restoring = False
        self._ctt = False          # 鼠标穿透中
        self._pending_center = None   # 恢复时的视图中心(show 后才能精确 centerOn)

        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint |
                            Qt.WindowStaysOnTopHint)
        self.setWindowTitle("图钉画布 · WinClipboard")
        ic = getattr(mgr.app, "icon", None)
        if ic is not None:
            self.setWindowIcon(ic)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.resize(780, 520)

        self.scene = BoardScene(self)
        self.view = BoardView(self)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(4)
        self.lbl_title = StrongBodyLabel("图钉画布")
        self.lbl_count = CaptionLabel("0 张")
        for wgt in (self.lbl_title, self.lbl_count):
            head.addWidget(wgt)
            wgt.installEventFilter(self)     # 标题栏拖动 = 移动窗口
        head.addStretch(1)

        def tbtn(icon, tip, cb, checkable=False):
            b = TransparentToolButton(icon)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.clicked.connect(cb)
            head.addWidget(b)
            return b

        tbtn(FIF.ADD, "添加图片文件(也可直接拖进来)", self.pick_files)
        tbtn(FIF.PASTE, "粘贴剪贴板图片(Ctrl+V)", self.paste_from_clipboard)
        tbtn(FIF.LAYOUT, "网格自动排列", self.arrange_grid)
        tbtn(FIF.FIT_PAGE, "全部恢复 1:1 实际像素", self.reset_all)
        tbtn(FIF.FULL_SCREEN, "整板缩放到适应窗口", self.fit_all)
        self.btn_ctt = tbtn(FIF.TRANSPARENT,
                            "鼠标穿透:画布仅显示,点击穿透到下层程序"
                            "(Ctrl+Alt+T 切换)",
                            lambda: self.mgr.toggle_click_through(None),
                            checkable=True)
        tbtn(FIF.DELETE, "删除选中图片(无选中时清空画布)", self.delete_smart)
        tbtn(FIF.CLOSE, "收起画布(内容保留;Esc 同)", self.close)
        v.addLayout(head)
        v.addWidget(self.view, 1)

        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(18, 18)
        v.addWidget(self.grip, 0, Qt.AlignRight | Qt.AlignBottom)

        self._head_drag = None
        self._apply_theme()

    # ---- 主题(跟随库当前深浅色;show 时刷新,切主题后重开生效) ----
    def theme_color(self):
        return QColor(themeColor())

    def _apply_theme(self):
        dark = isDarkTheme()
        canvas = "#181a1e" if dark else "#f2f2f2"
        border = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.10)"
        sec = "rgba(255,255,255,0.63)" if dark else "rgba(0,0,0,0.63)"
        text = "rgba(255,255,255,0.93)" if dark else "rgba(0,0,0,0.90)"
        self.lbl_title.setStyleSheet("color:%s;" % text)
        self.lbl_count.setStyleSheet("color:%s;" % sec)
        self.view.setStyleSheet(
            "QGraphicsView{background:%s;border:1px solid %s;border-radius:8px;}"
            % (canvas, border))

    def showEvent(self, e):
        self._apply_theme()
        self._sync_scene_rect()
        if self._pending_center is not None:
            c, self._pending_center = self._pending_center, None
            self.view.centerOn(c)
        super().showEvent(e)

    def resizeEvent(self, e):
        self._sync_scene_rect()
        if not self._restoring:
            self.mgr.schedule_save()
        super().resizeEvent(e)

    def moveEvent(self, e):
        if not self._restoring:
            self.mgr.schedule_save()
        super().moveEvent(e)

    # ---- 标题栏拖动移窗 ----
    def eventFilter(self, obj, e):
        t = e.type()
        if obj in (self.lbl_title, self.lbl_count):
            if t == e.Type.MouseButtonPress and \
                    e.button() == Qt.LeftButton:
                self._head_drag = (e.globalPosition().toPoint(),
                                   self.frameGeometry().topLeft())
                return True
            if t == e.Type.MouseMove and self._head_drag is not None:
                g0, w0 = self._head_drag
                self.move(w0 + (e.globalPosition().toPoint() - g0))
                return True
            if t == e.Type.MouseButtonRelease:
                self._head_drag = None
                self.mgr.schedule_save()
                return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if self.handle_key(e):
            e.accept()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        # 收起 ≠ 清空:条目保留,open 记 False,程序重启不自动弹出
        try:
            self.mgr.save_now(open_=False)
        except Exception:
            cm.log("图钉画布保存失败:\n%s" % traceback.format_exc())
        e.accept()

    # ---- 键盘 ----
    def handle_key(self, e):
        k = e.key()
        mods = e.modifiers()
        sel = self.selected_items()
        if k in (Qt.Key_Delete, Qt.Key_Backspace) and not mods & \
                (Qt.ControlModifier | Qt.AltModifier):
            self.remove_selected()
        elif k == Qt.Key_Escape:
            self.close()
        elif k == Qt.Key_R and mods & Qt.ShiftModifier:
            self.rotate_items(sel, -90.0)
        elif k == Qt.Key_R and not mods:
            self.rotate_items(sel, 90.0)
        elif k == Qt.Key_L and not mods:
            self.toggle_lock(sel)
        elif k == Qt.Key_V and mods & Qt.ControlModifier:
            self.paste_from_clipboard()
        elif k == Qt.Key_A and mods & Qt.ControlModifier:
            for it in self.pin_items():
                it.setSelected(True)
        elif k == Qt.Key_Home:
            self.z_set(sel, "front")
        elif k == Qt.Key_End:
            self.z_set(sel, "back")
        elif k == Qt.Key_PageUp:
            self.z_set(sel, "raise")
        elif k == Qt.Key_PageDown:
            self.z_set(sel, "lower")
        else:
            return False
        return True

    # ---- 条目管理 ----
    def pin_items(self):
        return [it for it in self.scene.items() if isinstance(it, PinImageItem)]

    def selected_items(self):
        return [it for it in self.pin_items() if it.isSelected()]

    def items_for_asset(self, name):
        return [it for it in self.pin_items() if it.rec.asset == name]

    def top_z(self):
        zs = [i.zValue() for i in self.scene.items()] or [0.0]
        return max(zs)

    def _bottom_z(self):
        zs = [i.zValue() for i in self.scene.items()] or [0.0]
        return min(zs)

    def _update_count(self):
        n, s = len(self.pin_items()), len(self.selected_items())
        self.lbl_count.setText("%d 张" % n if not s else "%d/%d 张" % (s, n))

    def on_selection_changed(self):
        self._update_count()

    def add_record_item(self, rec, fit_big=False):
        """由 rec 建条目(rec.x/y/scale 生效);fit_big 时超大图先缩到画布一半。"""
        if fit_big:
            vr = self.visible_rect()
            lim = min(vr.width(), vr.height()) * 0.55
            big = max(rec.w, rec.h)
            if lim > 50 and big > lim:
                rec.scale = max(MIN_SCALE, min(1.0, lim / big))
        it = PinImageItem(rec, self)
        self.scene.addItem(it)
        self._sync_scene_rect()
        self._update_count()
        return it

    def remove_item(self, it):
        self.scene.removeItem(it)
        self.mgr.on_item_removed(it.rec)
        self._sync_scene_rect()
        self._update_count()

    def remove_selected(self):
        for it in self.selected_items():
            self.remove_item(it)

    def delete_smart(self):
        """工具栏删除:选中感知——有选中只删选中的那几张(单张/多张都精确),
        无选中才走「清空画布」(带确认)。此前垃圾桶按钮直连清空整板,
        想删一张却弹「移除全部 N 张」/误清整板,即「删错/删整块」的根源。"""
        if self.selected_items():
            self.remove_selected()
        else:
            self.clear_all()

    def clear_all(self):
        n = len(self.pin_items())
        if not n:
            return
        if MessageBox("清空画布", "移除画布上的全部 %d 张图片?" % n,
                      self).exec():
            for it in self.pin_items():
                self.scene.removeItem(it)
                self.mgr.on_item_removed(it.rec)
            self._sync_scene_rect()
            self._update_count()
            self.mgr.schedule_save()

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "添加图片到画布", "",
            "图片 (%s)" % " ".join("*" + e for e in self.mgr.img_exts))
        for p in files:
            self.mgr.pin_path(p)

    def pick_from_history(self):
        """从剪贴板历史挑图片钉入(缩略图多选对话框)。"""
        try:
            from .picker import HistoryImagePicker
            d = HistoryImagePicker(self.mgr.app, self)
            if d.exec() and d.selected:
                self.mgr.show_board()
                for p in d.selected:
                    self.mgr.pin_path(p, show=False)
        except Exception:
            cm.log("图钉画布「从历史添加」失败:\n%s" % traceback.format_exc())

    def sync_records(self):
        """落盘前把条目运行态同步回 rec(持久化唯一入口)。"""
        for it in self.pin_items():
            r = it.rec
            r.x, r.y = it.x(), it.y()
            r.scale = it.scale_value()
            r.rotation = it.rotation()
            r.opacity = it.opacity()
            r.z = it.zValue()

    # ---- 交互目标(悬停项选中则作用于整组选中) ----
    def targets_for(self, it):
        sel = self.selected_items()
        if it.isSelected() and len(sel) > 1:
            return sel
        return [it]

    def _act(self, menu, text):
        # RoundMenu.addAction 只收 QAction(传 str 渲染即崩,见主程序 card_menu 注)
        a = QAction(text, menu)
        menu.addAction(a)
        return a

    # ---- 缩放 / 适应 ----
    def actual_scale(self):
        """屏幕 1:1 对应的条目缩放(随整板 zoom 反算)。"""
        z = self.view.transform().m11() or 1.0
        return max(MIN_SCALE, min(MAX_SCALE, 1.0 / z))

    def scale_item(self, it, factor, anchor=None):
        """缩放单图;给 anchor(场景坐标)时以该点为锚(光标下内容不动)。
        该公式对任意 rotation 成立(R·kI·R⁻¹ = kI)。"""
        if it.rec.locked:
            return
        old = it.scale_value()
        new = max(MIN_SCALE, min(MAX_SCALE, old * factor))
        if new == old:
            return
        pos = it.pos()
        it.set_scale_value(new)
        if anchor is not None:
            rel = anchor - pos
            it.setPos(anchor - rel * (new / old))
        it._mode = "free"
        self._sync_scene_rect()
        self.mgr.schedule_save()

    def _recenter(self, it, center):
        c2 = it.mapToScene(it.boundingRect().center())
        it.moveBy(center.x() - c2.x(), center.y() - c2.y())

    def make_actual(self, it):
        """1:1 实际像素;中心锚定不跳位;按需换全分辨率 pixmap。"""
        center = it.mapToScene(it.boundingRect().center())
        it.set_scale_value(self.actual_scale())
        it._mode = "actual"
        self.mgr.request_full(it)
        self._recenter(it, center)
        self._sync_scene_rect()
        self.mgr.schedule_save()

    def fit_single(self, it, remember=False):
        """缩放到当前可视区内居中(约 88%);remember 时优先复用上次适应比例。"""
        r = self.visible_rect()
        b = it.boundingRect()
        if r.isEmpty() or b.width() < 1 or b.height() < 1:
            return
        s = max(MIN_SCALE, min(MAX_SCALE,
                               min(r.width() * 0.88 / b.width(),
                                   r.height() * 0.88 / b.height())))
        if remember and it._fit_scale is not None:
            s = max(MIN_SCALE, min(MAX_SCALE, it._fit_scale))
        it._fit_scale = s
        it.set_scale_value(s)
        it._mode = "fit"
        self._recenter(it, r.center())
        self.mgr.schedule_save()

    def toggle_fit_item(self, it):
        """双击:1:1 ↔ 适应,可预测两态循环(适应比例有记忆)。"""
        if it._mode == "actual":
            self.fit_single(it, remember=True)
        else:
            self.make_actual(it)

    def zoom_view(self, factor):
        m11 = self.view.transform().m11() or 1.0
        if not (MIN_ZOOM <= m11 * factor <= MAX_ZOOM):
            return
        self.view.scale(factor, factor)
        self._sync_scene_rect()
        self.mgr.schedule_save()

    def reset_all(self):
        for it in self.pin_items():
            if not it.rec.locked:
                it.set_scale_value(1.0)
        self._sync_scene_rect()
        self.mgr.schedule_save()

    def fit_all(self):
        items = self.pin_items()
        if not items:
            return
        self.view.resetTransform()
        r = QRectF()
        for it in items:
            r = r.united(it.mapRectToScene(it.boundingRect()))
        if r.isEmpty():
            return
        self.view.fitInView(r.adjusted(-24, -24, 24, 24), Qt.KeepAspectRatio)
        z = self.view.transform().m11()
        if not (MIN_ZOOM <= z <= MAX_ZOOM):
            # 超出护栏:夹到边界并居中(不整段回退,「适应」语义优先)
            z = max(MIN_ZOOM, min(MAX_ZOOM, z))
            self.view.resetTransform()
            self.view.scale(z, z)
            self.view.centerOn(r.center())
        self._sync_scene_rect()
        self.mgr.schedule_save()

    # ---- 旋转 / 透明度 / 锁定 / 层级 ----
    def rotate_items(self, items, deg):
        for it in items:
            if it.rec.locked:
                continue
            center = it.mapToScene(it.boundingRect().center())
            it.setRotation((it.rotation() + deg) % 360.0)
            self._recenter(it, center)
        if items:
            self._sync_scene_rect()
            self.mgr.schedule_save()

    def nudge_opacity(self, items, delta):
        from .model import MIN_OPACITY, MAX_OPACITY
        for it in items:
            v = max(MIN_OPACITY, min(MAX_OPACITY, it.opacity() + delta))
            it.setOpacity(v)
            it.rec.opacity = v
        if items:
            self.mgr.schedule_save()

    def set_opacity(self, items, value):
        from .model import MIN_OPACITY, MAX_OPACITY
        v = max(MIN_OPACITY, min(MAX_OPACITY, float(value)))
        for it in items:
            it.setOpacity(v)
            it.rec.opacity = v
        if items:
            self.mgr.schedule_save()

    def toggle_lock(self, items):
        for it in items:
            it.set_locked(not it.rec.locked)
        if items:
            self.mgr.schedule_save()

    def z_set(self, items, mode):
        if not items:
            return
        if mode == "front":
            base = self.top_z()
            for i, it in enumerate(items):
                it.setZValue(base + 1.0 + i)
        elif mode == "back":
            base = self._bottom_z()
            for i, it in enumerate(items):
                it.setZValue(base - 1.0 - i)
        elif mode == "raise":
            for it in items:
                it.setZValue(it.zValue() + 1.0)
        elif mode == "lower":
            for it in items:
                it.setZValue(it.zValue() - 1.0)
        self.mgr.schedule_save()

    # ---- 排列(纯算法在 layout.py;≥2 选中作用于选中,否则全部) ----
    def _arrange_targets(self, min_sel=2):
        sel = self.selected_items()
        return sel if len(sel) >= min_sel else self.pin_items()

    def _arrange(self, fn, gap=None):
        items = self._arrange_targets()
        if not items:
            return
        self.view.resetTransform()       # 排列结果与屏幕坐标 1:1
        self._sync_scene_rect()
        fn(items, self.visible_rect(),
           gap if gap is not None else self.GRID_GAP)
        self._sync_scene_rect()
        self.mgr.schedule_save()

    def arrange_grid(self):
        self._arrange(layout.grid)

    def arrange_row(self):
        self._arrange(layout.row)

    def arrange_column(self):
        self._arrange(layout.column)

    def arrange_compact(self):
        self._arrange(layout.compact, gap=14)

    def _align_targets(self):
        sel = self.selected_items()
        return sel if len(sel) >= 2 else None

    def align_items(self, mode):
        items = self._align_targets()
        if not items:
            return
        layout.align(items, mode)
        self.after_items_moved()

    def distribute_items(self, axis):
        items = self._align_targets()
        if not items:
            return
        layout.distribute(items, axis)
        self.after_items_moved()

    # ---- 场景几何(大画板:场景随内容扩张,不钳制条目) ----
    SCENE_MARGIN = 600      # 内容包围盒四周的余量(可拖出去的呼吸空间)

    def visible_rect(self):
        """当前可视区域(场景坐标);空视图退化为窗口尺寸。"""
        vr = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        if vr.isEmpty() or vr.width() < 10 or vr.height() < 10:
            vr = QRectF(0, 0, max(100, self.view.width()),
                        max(100, self.view.height()))
        return vr

    def _sync_scene_rect(self):
        """场景 = 全部条目包围盒 ∪ 可视区,四周留余量(平移/滚动条有行程)。"""
        r = self.scene.itemsBoundingRect()
        if r.isEmpty():
            r = QRectF(QPointF(0, 0), self.visible_rect().size())
        r = r.united(self.visible_rect())
        m = self.SCENE_MARGIN
        self.scene.setSceneRect(r.adjusted(-m, -m, m, m))

    def after_items_moved(self):
        """条目拖动/对齐等几何变化后的统一收口(场景扩张 + 防抖落盘)。"""
        self._sync_scene_rect()
        self.mgr.schedule_save()

    # ---- 恢复 ----
    def restore(self, recs, geo=None, zoom=None, open_=False, center=None):
        self._restoring = True
        try:
            # 视口感知加载:不再对全量记录预提交 preview,恢复的视图中心附近
            # 先装,其余等滚进视口(远离视口 = 占位框 + metadata)
            for r in recs:
                self.scene.addItem(PinImageItem(r, self))
            if isinstance(geo, (list, tuple)) and len(geo) == 4:
                try:
                    self.resize(max(self.MIN_W, int(geo[2])),
                                max(self.MIN_H, int(geo[3])))
                    self.move(int(geo[0]), int(geo[1]))
                    _ensure_onscreen(self)
                except Exception:
                    pass
            if isinstance(zoom, (int, float)) and MIN_ZOOM <= zoom <= MAX_ZOOM:
                self.view.resetTransform()
                self.view.scale(zoom, zoom)
            self._sync_scene_rect()
            if center is not None:
                try:
                    self.view.centerOn(center)
                    self._pending_center = center   # show 后按真实视口再对一次
                except Exception:
                    pass
            self._update_count()
        finally:
            self._restoring = False
        # 恢复的视图中心附近立即装 preview(不等 show 后的首帧重绘)
        self.mgr.check_visible_now()
        if open_ and self.pin_items():
            self.show()
            self.raise_()

    # ---- 粘贴 / 复制 / 查看器 ----
    def paste_from_clipboard(self):
        self.mgr.paste_from_clipboard(center=True)

    def copy_item(self, it):
        worker = getattr(self.mgr.app, "worker", None)
        hwnd = worker.hwnd if worker is not None else 0
        try:
            ok = cm.set_dib_to_clipboard(hwnd, cm.pil_to_dib(
                self.mgr.composed_image(it.rec)))   # 原图+标注层现场合成
        except Exception:
            cm.log("图钉画布复制失败:\n%s" % traceback.format_exc())
            ok = False
        (InfoBar.success if ok else InfoBar.error)(
            "已复制" if ok else "复制失败",
            "图片已复制到剪贴板" if ok else "剪贴板被占用或文件无法读取",
            parent=self, position=InfoBarPosition.BOTTOM_LEFT,
            duration=1500)

    def open_in_viewer(self, it):
        f = getattr(self.mgr.app, "open_image_viewer", None)
        if f is None:
            return
        f({"type": "image", "content": self.mgr.asset_full(it.rec.asset),
           "w": it.rec.w, "h": it.rec.h})

    def edit_item(self, it):
        """编辑标注:内置编辑器(方框/箭头/文字/马赛克/裁剪),非破坏模型——
        纯标注编辑只回写标注层(原图不动);裁剪/旋转等底图操作才替换资产。"""
        f = getattr(self.mgr.app, "open_editor", None)
        if f is None:
            return

        def done(result):
            if isinstance(result, dict) and result.get("kind") == "layers":
                ok = True
                if result.get("base") is not None:
                    ok = self.mgr.replace_asset_from(it, result["base"])
                if ok:
                    ok = self.mgr.set_annotations(
                        it, result.get("annotations") or [])
                (InfoBar.success if ok else InfoBar.error)(
                    "标注已更新" if ok else "更新失败",
                    "标注层已保存(原图未改动)" if ok else "标注层写入失败",
                    parent=self, position=InfoBarPosition.TOP, duration=1800)
                return
            ok = self.mgr.replace_asset_from(it, result)
            (InfoBar.success if ok else InfoBar.error)(
                "画布已更新" if ok else "更新失败",
                "标注结果已替换画布图片" if ok else "结果文件无法收编",
                parent=self, position=InfoBarPosition.TOP, duration=1800)

        f({"type": "image", "content": self.mgr.asset_full(it.rec.asset),
           "w": it.rec.w, "h": it.rec.h}, on_result=done,
          annos=it.rec.annos, layer_mode=True)

    def edit_note(self, it):
        """单图备注:独立于其他图片,随图移动/缩放/置顶跟随(子项继承变换)。"""
        d = NoteDialog(it.rec.note, self)
        if d.exec():
            it.set_note(d.text())
            self.mgr.schedule_save()

    # ---- 鼠标穿透 ----
    def set_click_through(self, on):
        if on == self._ctt:
            return
        self._ctt = on
        self.setWindowFlag(Qt.WindowTransparentForInput, on)
        self.show()                      # Windows 改 flags 需重 show
        self.btn_ctt.setChecked(on)
        self.lbl_title.setText("图钉画布 · 穿透中" if on else "图钉画布")
        if on:
            InfoBar.info("鼠标穿透已开启",
                         "画布不收鼠标;Ctrl+Alt+T 或 托盘右键→退出画布鼠标穿透 可关闭",
                         parent=self, position=InfoBarPosition.TOP,
                         duration=8000)
        cm.log("图钉画布鼠标穿透已%s" % ("开启" if on else "关闭"))

    # ---- 菜单 ----
    def item_menu(self, it, gpos):
        m = RoundMenu(parent=self)
        a = self._act(m, "复制图片")
        a.triggered.connect(lambda _=False, i=it: self.copy_item(i))
        a = self._act(m, "在图片查看器打开")
        a.triggered.connect(lambda _=False, i=it: self.open_in_viewer(i))
        a = self._act(m, "✏ 编辑标注…")
        a.triggered.connect(lambda _=False, i=it: self.edit_item(i))
        a = self._act(m, ("📝 编辑备注…" if it.rec.note else "📝 添加备注…"))
        a.triggered.connect(lambda _=False, i=it: self.edit_note(i))
        m.addSeparator()
        a = self._act(m, "1:1 实际像素(双击)")
        a.triggered.connect(lambda _=False, i=it: self.make_actual(i))
        a = self._act(m, "适应画布")
        a.triggered.connect(lambda _=False, i=it: self.fit_single(i))
        rot = RoundMenu("旋转", self)
        a = self._act(rot, "顺时针 90°(R)")
        a.triggered.connect(lambda _=False: self.rotate_items([it], 90.0))
        a = self._act(rot, "逆时针 90°(Shift+R)")
        a.triggered.connect(lambda _=False: self.rotate_items([it], -90.0))
        m.addMenu(rot)
        a = self._act(m, "解锁(L)" if it.rec.locked else "锁定(L)")
        a.triggered.connect(lambda _=False: self.toggle_lock([it]))
        op = RoundMenu("透明度(Ctrl+滚轮)", self)
        for pct in (100, 85, 70, 55, 40, 25):
            a = self._act(op, "%d%%" % pct)
            a.triggered.connect(lambda _=False, v=pct / 100.0:
                                self.set_opacity([it], v))
        m.addMenu(op)
        zs = RoundMenu("层级", self)
        for text, mode in (("置于顶层(Home)", "front"),
                           ("上移一层(PgUp)", "raise"),
                           ("下移一层(PgDn)", "lower"),
                           ("置于底层(End)", "back")):
            a = self._act(zs, text)
            a.triggered.connect(lambda _=False, md=mode: self.z_set([it], md))
        m.addMenu(zs)
        m.addSeparator()
        a = self._act(m, "从画布移除(Del)")
        a.triggered.connect(lambda _=False, i=it: self.remove_item(i))
        m.exec(gpos)

    def board_menu(self, gpos):
        m = RoundMenu(parent=self)
        a = self._act(m, "粘贴图片(Ctrl+V)")
        a.triggered.connect(self.paste_from_clipboard)
        a = self._act(m, "从剪贴板历史添加…")
        a.triggered.connect(self.pick_from_history)
        a = self._act(m, "添加图片文件…")
        a.triggered.connect(self.pick_files)
        m.addSeparator()
        a = self._act(m, "网格排列")
        a.triggered.connect(self.arrange_grid)
        a = self._act(m, "横向排列")
        a.triggered.connect(self.arrange_row)
        a = self._act(m, "纵向排列")
        a.triggered.connect(self.arrange_column)
        a = self._act(m, "紧凑排列")
        a.triggered.connect(self.arrange_compact)
        al = RoundMenu("对齐 / 等间距(先多选)", self)
        for text, fn in (("左对齐", lambda: self.align_items("left")),
                         ("右对齐", lambda: self.align_items("right")),
                         ("顶部对齐", lambda: self.align_items("top")),
                         ("底部对齐", lambda: self.align_items("bottom")),
                         ("水平居中", lambda: self.align_items("hcenter")),
                         ("垂直居中", lambda: self.align_items("vcenter")),
                         ("水平等间距", lambda: self.distribute_items("h")),
                         ("垂直等间距", lambda: self.distribute_items("v"))):
            a = self._act(al, text)
            a.triggered.connect(lambda _=False, f=fn: f())
        m.addMenu(al)
        m.addSeparator()
        a = self._act(m, "全部 1:1")
        a.triggered.connect(self.reset_all)
        a = self._act(m, "整板适应窗口")
        a.triggered.connect(self.fit_all)
        a = self._act(m, "全部取消选择")
        a.triggered.connect(lambda: self.scene.clearSelection())
        m.addSeparator()
        a = self._act(m, "鼠标穿透(Ctrl+Alt+T)")
        a.triggered.connect(lambda: self.mgr.toggle_click_through(None))
        a = self._act(m, "清空画布")
        a.triggered.connect(self.clear_all)
        a = self._act(m, "收起画布")
        a.triggered.connect(self.close)
        m.exec(gpos)
