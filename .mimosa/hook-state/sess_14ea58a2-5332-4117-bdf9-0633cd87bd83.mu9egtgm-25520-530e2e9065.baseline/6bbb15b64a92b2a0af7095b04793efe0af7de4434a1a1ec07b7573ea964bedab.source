# -*- coding: utf-8 -*-
"""从剪贴板历史挑选图片钉入画布的选择器(Fluent 风格对话框)。"""
import os

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPixmap, QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from qfluentwidgets import MessageBoxBase, StrongBodyLabel, CaptionLabel, BodyLabel


class HistoryImagePicker(MessageBoxBase):
    """历史图片缩略图多选;点「钉到画布」后所选路径在 .selected(可能为空)。

    缩略图只读现成缓存(app._thumbs,卡片预热时已生成),未命中的提交给
    ThumbLoader 后台生成、经 loaded 信号回填——对话框打开瞬间绝不在 UI
    线程同步解码 PNG(几十张大图会冻窗秒级)。
    """

    ICON = QSize(150, 98)

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.selected = []
        self.titleLabel = StrongBodyLabel("从剪贴板历史添加图片")
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(CaptionLabel("选中要钉到画布的图片(可多选)"))
        self.listw = QListWidget(self)
        self.listw.setViewMode(QListWidget.IconMode)
        self.listw.setSelectionMode(QListWidget.ExtendedSelection)
        self.listw.setResizeMode(QListWidget.Adjust)
        self.listw.setMovement(QListWidget.Static)
        self.listw.setWordWrap(True)
        self.listw.setIconSize(self.ICON)
        self.listw.setSpacing(8)
        self.listw.setMinimumHeight(320)
        self.viewLayout.addWidget(self.listw)

        self._placeholder = self._make_placeholder()
        self._by_path = {}          # path → QListWidgetItem(后台回填用)
        self._loader = getattr(app, "_thumb_loader", None)
        n = 0
        cache = getattr(app, "thumb_cached", None)
        fallback = getattr(app, "thumb_for", None)
        for h in getattr(app, "history", []) or []:
            if h.get("type") != "image":
                continue
            p = str(h.get("content") or "")
            if not os.path.exists(p):
                continue
            icon = None
            if cache is not None:               # 主程序:缓存命中即显,未命中后台补
                pm = cache(p)
                icon = QIcon(pm) if pm is not None and not pm.isNull() else None
            elif fallback is not None:          # 测试替身等无加载器的宿主:同步兜底
                try:
                    pm = fallback(p)
                    icon = QIcon(pm) if pm is not None and not pm.isNull() else None
                except Exception:
                    icon = None
            it = QListWidgetItem(icon or self._placeholder,
                                 "%d×%d" % (h.get("w", 0), h.get("h", 0)))
            it.setData(Qt.UserRole, p)
            it.setToolTip(p)
            self.listw.addItem(it)
            self._by_path[p] = it
            n += 1
        if not n:
            self.viewLayout.addWidget(BodyLabel("(剪贴板历史里还没有图片)"))
        else:
            self._connect_loader()
        self.yesButton.setText("钉到画布")
        self.cancelButton.setText("取消")

    # ---- 后台缩略图回填 ----
    def _connect_loader(self):
        if self._loader is not None:
            try:
                self._loader.loaded.connect(self._on_thumb_loaded)
            except Exception:
                self._loader = None

    def _disconnect_loader(self):
        if self._loader is not None:
            try:
                self._loader.loaded.disconnect(self._on_thumb_loaded)
            except Exception:
                pass
            self._loader = None

    def _on_thumb_loaded(self, path, img):
        it = self._by_path.get(path)
        if it is not None and not img.isNull():
            it.setIcon(QIcon(QPixmap.fromImage(img)))

    def _make_placeholder(self):
        pm = QPixmap(self.ICON)
        pm.fill(QColor(0, 0, 0, 0))
        return QIcon(pm)

    def validate(self):
        """点「钉到画布」时收集所选(qfluentwidgets 扩展点)。"""
        self.selected = [self.listw.item(i).data(Qt.UserRole)
                         for i in range(self.listw.count())
                         if self.listw.item(i).isSelected()]
        return True

    def closeEvent(self, e):
        self._disconnect_loader()   # 信号引用 item,对话框销毁前必须断开
        super().closeEvent(e)
