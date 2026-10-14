#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinClipboard - PySide6 + PyQt-Fluent-Widgets 版
===============================================
UI 基于 Qt6 + qfluentwidgets(Segmented 分段导航 / SearchLineEdit /
Card 卡片 / RoundMenu / InfoBar / 面板弹出动画),支持浅色 / 深色主题
(默认跟随 Windows 系统设置,config.json "theme": light/dark/auto 可覆盖),
后端逻辑复用 clipboard_manager.py(热键 / 托盘 / 剪贴板 / 敏感隔离 / 持久化)。
"""
import os
import sys
import io
import json
import time
import queue
import subprocess
import threading
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm   # 后端复用
import perf_probe
from perf_probe import PerfSpan, since, stamp   # 关键路径性能埋点
from pinboard import PinBoardManager   # 图钉画布(置顶多图板)子包
from pinboard import annotations as _anno_layer   # 标注层(非破坏模型)
import workflow_steps as wfs   # AI 工作流 Step Contract(步骤统一契约)
import workflow_runlog as wrl  # 工作流运行记录(Run Inspector 数据层)
import diagnostics as diag     # 诊断快照 + 数据健康检查(纯数据层,无 UI)
from quickpaste import QuickPasteWindow  # 快捷粘贴浮层(默认 Alt+`)
from urllib.parse import unquote as _unquote, quote as _quote, urlsplit

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
                               QFileDialog, QSizePolicy, QListWidget, QListWidgetItem, QLineEdit,
                               QPlainTextEdit, QPushButton, QSizeGrip,
                               QSpacerItem, QStackedWidget, QTreeWidgetItem,
                               QTableWidgetItem, QHeaderView, QComboBox)

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

# ---- 阶段 8 拆分:调色板/助手与查看器·编辑器子系统迁至独立模块(兼容重导出) ----
import ui_base
from ui_base import *  # noqa: F401,F403
from ui_base import (init_palette, resolve_theme, apply_fonts, _fic,
                     _win_prefers_dark, _mica_supported, _phys_screen,
                     _accent_tint, _PV_TEXT_CAP)
from ui.theme import (apply_atelier_zero, PAPER, INK, INK_LIGHT, ACCENT,
                      DANGER, head_font, body_font, label_font, Divider,
                      IndexLabel, MastheadBadge)
from ui.delegates import EditorialDelegate
import ui.animations as atelier_anim
from viewer import (ViewerWindow, ImageViewerWindow, DiffWindow,  # noqa: F401
                    INSPECTOR_ACTIONS, _copy_or_warn, apply_pygments_format,
                    _pg_get_lexer)
from image_editor import (ImageEditor, CompressDialog,  # noqa: F401
                          ScaleDialog, AnnoRect, AnnoEllipse, AnnoPath,
                          AnnoMosaic, AnnoText)
ui_base.register(sys.modules[__name__])   # 调色板随主题刷新本模块全局

# 局部原生按钮/徽章的统一柔和样式(圆角软边,与 ui/theme.py 全局表同语言;
# 杂志期直角黑框遗留样式已全部收敛到这里)
_SOFT_BTN = (
    "QPushButton { background: #ffffff; color: %s;"
    " border: 1px solid rgba(0,0,0,0.14); border-radius: 6px;"
    " padding: 4px 12px; }"
    "QPushButton:hover { background: rgba(0,0,0,0.05); }" % INK)
_KEY_BADGE = (
    "color: %s; background: transparent; border: 1px solid rgba(0,0,0,0.14);"
    " border-radius: 6px; padding: 2px 8px;" % INK_LIGHT)


class Card(QFrame):
    """剪贴板条目卡片。

    继承自原生 QFrame，完全由 QSS + 代码控制绘制：
    - 1px 底线；悬停 / 选中左侧 3px 主题色指示条
    - 选中 = 柔和主题色底色(约 10% 透明度)，文字保持深色——
      深色截图缩略图在选中行上依然清晰可见(不再整行反相死黑)
    - 置顶项用主题色标记点
    - 右侧操作按钮为浮层(不占文本宽度)：悬停或选中时常驻显示
    业务绑定逻辑(差量复用、事件分发)保持不变。
    """

    THUMB = (92, 68)
    ELIDE_W = 400          # 标题/副文的省略宽度(双栏后左栏可用宽度收窄)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.rec = None                 # 当前绑定记录(差量绑定核心,见 bind)
        self._bound_id = None           # 同 rec 直接返回的守卫
        self._bound_kind = None         # 徽章区结构类型(image/files/link/text)
        self._thumb_lbl = None
        self._pin_shown = None
        self.setProperty("selected", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(72)
        self.setObjectName("cardFrame")
        self._build()
        self._update_style()

    def set_selected(self, on):
        if self.property("selected") != on:
            self.setProperty("selected", on)
            if on:
                self._ensure_btns()     # 选中态常驻操作钮(懒建,池内只建一次)
            self._update_style()
            self._apply_btns()

    def _update_style(self):
        """选中/悬停样式:柔和主题色底 + 左侧指示条。

        文字色恒为深色系(在 _build 一次性设定,选中不再反相),此处
        只改框架一张样式表——大列表批量建卡/重绑的渲染预算敏感。"""
        sel = bool(self.property("selected"))
        hover = bool(self.property("hovered"))
        accent = QColor(ACCENT)
        if sel or hover:
            bg = "rgba(%d,%d,%d,%s)" % (accent.red(), accent.green(),
                                        accent.blue(), "0.10" if sel else "0.03")
            bar = ACCENT
        else:
            bg, bar = PAPER, "transparent"
        self.setStyleSheet(
            "QFrame#cardFrame { background: %s; color: %s; border: none; "
            "border-left: 3px solid %s; "
            "border-bottom: 1px solid rgba(17,17,17,0.12); }" % (bg, INK, bar))

    def _apply_btns(self):
        """操作按钮浮层可见性:悬停或选中时常驻,其余时候隐藏。"""
        if self.act_btns is None:
            return
        show = bool(self.property("hovered")) or bool(self.property("selected"))
        for b in self.act_btns:
            b.setVisible(show)

    def update_pin(self):
        """复用卡片时刷新置顶指示点。"""
        pinned = bool(self.rec.get("pinned"))
        if pinned == getattr(self, "_pin_shown", None):
            return
        self._pin_shown = pinned
        self.dot.setStyleSheet(
            "background: %s; border-radius: 0px;" % (ACCENT if pinned else "transparent"))

    def set_thumb(self, pm):
        """后台缩略图生成完成后回填(卡片先用占位色块占位)。"""
        if self._thumb_lbl is not None:
            self._thumb_lbl.setPixmap(rounded_pixmap(pm, 10))

    def _build(self):
        """只建与内容无关的常驻骨架;内容填充一律走 bind 的差量路径。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 0, 16, 0)
        root.setSpacing(12)

        self.dot = QLabel()
        self.dot.setFixedSize(6, 6)
        self.dot.setStyleSheet("background: transparent;")

        self._left = QVBoxLayout()
        self._left.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.title_lbl = QLabel()
        self.title_lbl.setFont(body_font(13, QFont.Weight.DemiBold))
        self.title_lbl.setMaximumWidth(420)
        self.title_lbl.setStyleSheet("color: %s; background: transparent;" % INK)
        self.sub_lbl = QLabel()
        self.sub_lbl.setFont(body_font(11))
        self.sub_lbl.setMaximumWidth(420)
        self.sub_lbl.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        self.sub_lbl.hide()
        self.meta_lbl = QLabel()
        self.meta_lbl.setFont(label_font(9))
        self.meta_lbl.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)

        mid = QVBoxLayout()
        mid.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(self.dot)
        title_row.addWidget(self.title_lbl)
        title_row.addStretch(1)
        mid.addLayout(title_row)
        mid.addWidget(self.sub_lbl)
        mid.addWidget(self.meta_lbl)

        root.addLayout(self._left)
        root.addLayout(mid, 1)

        # 操作按钮浮层:不进布局(不占文本宽度),resizeEvent 定位到右侧;
        # 悬停或选中时常驻显示(见 _apply_btns)。宽度 = 4×28 + 入包 50 + 间距
        self.act_btns = None
        actw = QWidget(self)
        actw.setFixedSize(184, 30)
        actw.setStyleSheet("background: transparent;")
        self.actw = actw

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.actw is not None:
            self.actw.move(self.width() - self.actw.width() - 14,
                           (self.height() - self.actw.height()) // 2)

    # ---- 差量绑定(固定复用池的核心) ----
    def bind(self, rec):
        """绑定一条记录:同 rec 零开销;徽章区结构只在类型变化时重建,
        文本字段带变化检测地 setText。卡片控件只建不毁,刷新成本≈改文本。"""
        if self._bound_id == id(rec):
            return
        kind = "link" if (rec["type"] == "text" and cm.url_info(rec["content"])) \
            else rec["type"]
        if kind != self._bound_kind:
            self._rebuild_left(kind)
        self.rec = rec
        self._bound_id = id(rec)
        self._bound_kind = kind
        self._fill(rec, kind)
        self.set_selected(False)

    def _rebuild_left(self, kind):
        """按类型重建左侧徽章区(唯一会发生控件增删的位置,仅类型切换时)。"""
        while self._left.count():
            w = self._left.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._thumb_lbl = None
        if kind == "image":
            thumb = QLabel()
            thumb.setFixedSize(*self.THUMB)
            thumb.setStyleSheet(
                "border: 1px solid rgba(17,17,17,0.22); border-radius: 10px;")
            self._thumb_lbl = thumb
            self._left.addWidget(thumb)
        elif kind == "files":
            self._left.addWidget(icon_tile(FIF.FOLDER, 44, 13))
        elif kind == "link":
            self._left.addWidget(icon_tile(FIF.LINK, 34, 10))
        else:
            self._left.addWidget(icon_tile(FIF.DOCUMENT, 34, 10))

    def _fill(self, rec, kind):
        """填充文本字段与缩略图(全部走带守卫的 setText)。"""
        if kind == "image":
            pm = self.app.thumb_cached(rec["content"])
            if pm is None:
                pm = QPixmap(*self.THUMB)
                pm.fill(QColor(THUMB_PH))
                self.app.request_thumb(rec["content"])
            self._thumb_lbl.setPixmap(rounded_pixmap(pm, 10))
            title = "截图"
            sub = "%s × %s" % (rec.get("w", 0), rec.get("h", 0))
            meta = self._meta("PNG")
        elif kind == "files":
            names = rec["content"]
            name = os.path.basename(names[0])
            ext = os.path.splitext(name)[1].lstrip(".").upper()[:4] or "FILE"
            title = name + (" 等 %d 个" % len(names) if len(names) > 1 else "")
            sub = ""
            size = ""
            try:
                total = sum(os.path.getsize(p) for p in names if os.path.exists(p))
                size = cm._fmt_size(total)
            except Exception:
                pass
            meta = " · ".join(x for x in (ext, size, rec.get("src", ""),
                                          cm._rel_time(rec.get("ts", 0))) if x)
        elif kind == "link":
            title = cm.url_info(rec["content"])[0]
            sub = rec["content"].strip()
            meta = " · ".join(x for x in ("链接", rec.get("src", ""),
                                          cm._rel_time(rec.get("ts", 0))) if x)
        else:
            lines = [l for l in rec["content"].strip().splitlines() if l.strip()]
            title = lines[0] if lines else ""
            sub = "\n".join(lines[1:3]) if len(lines) > 1 else ""
            badge = self._badge_of(rec)
            meta = " · ".join(x for x in (badge or "文本", rec.get("src", ""),
                                          cm._rel_time(rec.get("ts", 0))) if x)
        self._set_text(self.title_lbl,
                       elided(title, self.title_lbl.font(), self.ELIDE_W))
        self._set_text(self.sub_lbl,
                       elided(sub, self.sub_lbl.font(), self.ELIDE_W) if sub else "")
        self.sub_lbl.setVisible(bool(sub))
        self._set_text(self.meta_lbl, meta)
        # 屏幕阅读器:卡片读出条目标题而非整段内容(私密凭据永不进卡片)
        self.setAccessibleName("剪贴板条目 " + (title or "")[:60])
        self._pin_shown = None          # 重绑后置顶态必查一次
        self.update_pin()

    @staticmethod
    def _set_text(lbl, txt):
        if lbl.text() != txt:           # setText 触发重排/重绘,不变则跳过
            lbl.setText(txt)

    def _badge_of(self, rec):
        """子类型徽章(JWT/Base64):带 id(rec) 缓存,同记录重复绑定零探测。
        缓存挂在宿主 app 上并懒初始化——离屏渲染/测试的 FakeApp 可不携带。"""
        cache = getattr(self.app, "_badge_cache", None)
        if cache is None:
            cache = self.app._badge_cache = {}
        key = id(rec)
        got = cache.get(key)
        if got is None:
            got = cm.effective_subtype(rec) or ""
            if len(cache) > 512:
                cache.clear()
            cache[key] = got
        return got or None

    def _ensure_btns(self):
        if self.act_btns is not None:
            return
        self.act_btns = []
        actbox = QHBoxLayout(self.actw)
        actbox.setContentsMargins(0, 0, 0, 0)
        actbox.setSpacing(4)
        tips = {"paste": "粘贴", "pack": "加入上下文包",
                "pin": "固定", "del": "删除", "more": "更多"}
        # 强调色胶囊的底/边从主题色派生(真实运行=Études 蓝,预览/测试
        # 走 init_palette 时=库默认蓝,与选中态指示条始终同源)
        acc = QColor(ACCENT)
        acc_tint = "rgba(%d,%d,%d,0.10)" % (acc.red(), acc.green(), acc.blue())
        acc_edge = "rgba(%d,%d,%d,0.35)" % (acc.red(), acc.green(), acc.blue())
        acc_hov = "rgba(%d,%d,%d,0.18)" % (acc.red(), acc.green(), acc.blue())
        # 显式 padding:0 + 圆角 + 自带字号:覆盖全局 QPushButton 的
        # padding/mono/uppercase 杂志样式(曾把 28px 小按钮挤成空黑框)
        neutral_qss = (
            "QPushButton { padding: 0px; border-radius: 6px;"
            " font-family: 'Segoe UI','Microsoft YaHei UI'; font-size: 12px;"
            " background: rgba(255,255,255,0.92); color: #222222;"
            " border: 1px solid rgba(0,0,0,0.14); }"
            "QPushButton:hover { background: rgba(0,0,0,0.06); }")
        pack_qss = (
            "QPushButton { padding: 0px; border-radius: 6px;"
            " font-family: 'Segoe UI','Microsoft YaHei UI'; font-size: 12px;"
            " background: %s; color: %s; font-weight: 600;"
            " border: 1px solid %s; }"
            "QPushButton:hover { background: %s; }"
            % (acc_tint, ACCENT, acc_edge, acc_hov))
        for act, label in (("paste", "贴"), ("pack", "+入包"), ("pin", "钉"),
                           ("del", "删"), ("more", "⋯")):
            b = QPushButton(label, self.actw)
            if act == "pack":
                b.setFixedSize(50, 26)
                b.setStyleSheet(pack_qss)
            else:
                b.setFixedSize(28, 26)
                b.setStyleSheet(neutral_qss)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tips[act])
            b.clicked.connect(lambda _=False, a=act: self.app.card_action(a, self))
            b.setVisible(False)
            actbox.addWidget(b)
            self.act_btns.append(b)

    def _meta(self, kind):
        return " · ".join(x for x in (kind, self.rec.get("src", ""),
                                      cm._rel_time(self.rec.get("ts", 0))) if x)

    def enterEvent(self, e):
        self.setProperty("hovered", True)
        self._ensure_btns()
        self._update_style()
        self._apply_btns()

    def leaveEvent(self, e):
        self.setProperty("hovered", False)
        self._update_style()
        self._apply_btns()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.ControlModifier):
            # Ctrl+点击 = 对比流:第一条设基准,第二条开对比窗口
            self.app.select_card(self)
            self.app.compare_pick(self.rec)
            return
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.ShiftModifier):
            # Shift+点击 = 段选(当前选中到此处),Delete 一键批量删除(可撤销)
            self.app.range_select_from(self)
            return
        if e.button() == Qt.LeftButton:
            self.app.select_card(self)
        elif e.button() == Qt.RightButton:
            self.app.select_card(self)
            self.app.card_menu(self, e.globalPosition().toPoint())
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            # 双击 = 直达查看:链接→默认浏览器;文件→默认程序(同资源管理器双击);
            # 图片→图片查看器;文本/代码→内容查看器;粘贴走 Enter / 悬停按钮 / 右键
            t = self.rec["type"]
            if t == "image":
                self.app.open_image_viewer(self.rec)
            elif t == "files":
                self.app.open_files(self.rec)
            elif t == "text" and cm.url_info(self.rec["content"]):
                self.app.open_link(self.rec)
            else:
                self.app.open_viewer(self.rec)
        super().mouseDoubleClickEvent(e)


# ============================================================
# 主面板(无边框透明窗口 + Fluent 组件)
# ============================================================
class HistoryPage(QWidget):
    """剪贴板历史页(FluentWindow 导航页):搜索 + 分类 + 卡片流 + 底部预览。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("historyPage")
        v = QVBoxLayout(self)
        v.setContentsMargins(36, 20, 36, 12)
        v.setSpacing(10)

        # ---- 顶部工具行:分类导航 + 筛选 + 搜索(空间让给内容列表) ----
        top = QHBoxLayout()
        top.setSpacing(10)
        # 分类导航容器(nav 为既有属性名,集成测试/预览脚本引用)
        self.nav = QWidget(self)
        nav_lay = QHBoxLayout(self.nav)
        nav_lay.setContentsMargins(0, 0, 0, 0)
        nav_lay.setSpacing(0)
        self._cat_btns = {}
        for label, cat in (("全部", "all"), ("文本", "text"), ("图片", "image"),
                           ("链接", "link"), ("文件", "files")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFont(body_font(10, QFont.Weight.DemiBold))
            b.setStyleSheet(
                "QPushButton { background: transparent; color: %s; border: none; "
                "border-bottom: 2px solid transparent; border-radius: 0px; padding: 5px 12px; }"
                "QPushButton:checked { color: %s; border-bottom: 2px solid %s; }"
                "QPushButton:hover:!checked { color: %s; }" %
                (INK_LIGHT, INK, ACCENT, INK))
            b.clicked.connect(lambda _=False, c=cat: self.app.set_category(c))
            self._cat_btns[cat] = b
            nav_lay.addWidget(b)
        self._cat_btns["all"].setChecked(True)
        top.addWidget(self.nav, 0)
        top.addStretch(1)
        # 来源 / 时间筛选
        self.cmb_src = QComboBox(self)
        self.cmb_src.setToolTip("按来源应用筛选")
        self.cmb_src.setMinimumWidth(118)
        self.cmb_src.setAccessibleName("按来源应用筛选")
        self.cmb_src.addItem("全部来源", userData="")
        self.cmb_src.currentIndexChanged.connect(
            lambda i: self.app.set_src_filter(self.cmb_src.itemData(i) or ""))
        self.cmb_time = QComboBox(self)
        for _tl, _ts in (("全部时间", "all"), ("今天", "today"),
                          ("近 24 小时", "24h"), ("近 7 天", "7d"),
                          ("近 30 天", "30d")):
            self.cmb_time.addItem(_tl, userData=_ts)
        self.cmb_time.setMinimumWidth(104)
        self.cmb_time.setToolTip("按复制时间筛选")
        self.cmb_time.setAccessibleName("按时间范围筛选")
        self.cmb_time.currentIndexChanged.connect(
            lambda i: self.app.set_time_filter(self.cmb_time.itemData(i) or "all"))
        top.addWidget(self.cmb_src)
        top.addWidget(self.cmb_time)
        # 搜索(中文占位符提示搜索语法;无障碍名是既有契约)
        self.search = QLineEdit(self)
        self.search.setObjectName("historySearch")
        self.search.setPlaceholderText("搜索剪贴历史（支持 source: / type: / after:）")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索剪贴历史")
        self.search.textChanged.connect(self.app.on_search)
        self.search.returnPressed.connect(self.app.paste_selected)
        self.search.installEventFilter(self)
        self.search.setFont(body_font(11))
        self.search.setMinimumWidth(230)
        self.search.setMaximumWidth(430)
        self.search.setStyleSheet(
            "QLineEdit#historySearch { background: #ffffff; color: %s; "
            "border: 1px solid rgba(0,0,0,0.14); border-radius: 6px; "
            "padding: 5px 10px; }"
            "QLineEdit#historySearch:focus { border: 1px solid %s; }" %
            (INK, ACCENT))
        top.addWidget(self.search, 1)
        # 安全模式徽章仅在安全模式出现(正常启动不再放装饰性 LIVE 徽标)
        if bool(getattr(self.app, "safe_mode", False)):
            self._badge = MastheadBadge("SAFE MODE", danger=True)
            top.addWidget(self._badge)
        v.addLayout(top)

        # ---- 左右双栏:左 = 剪贴板流(卡片 + 预览 + 底栏),右 = AI 工作台 ----
        cols = QHBoxLayout()
        cols.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(8)
        right = QVBoxLayout()
        right.setSpacing(8)

        # ---- 卡片流 ----
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setViewportMargins(0, 0, 6, 0)
        # 内容层与滚动区必须显式透明,否则深色主题下缝隙处露出默认白色底;
        # viewport 也要关掉自动填充 —— 否则它按调色板填白,卡片会糊进白底
        self.scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.content)
        self.cards_layout.setContentsMargins(2, 4, 2, 4)
        self.cards_layout.setSpacing(10)
        self.cards_layout.setAlignment(Qt.AlignTop)
        # 固定复用卡片池:卡片只建不毁,闲置进 _idle 池 hide;两个分组
        # 标签与尾部弹簧常驻,刷新零创建零销毁(见 set_view)
        self._idle = []
        self._cards = []
        # 窗口化渲染:大视图只物化 [_win_lo, _win_limit) 段(滚动到底自动
        # 扩展;深滚动超上限时上方远端释放回池 = 滑动窗口,控件数有界)
        self._win_limit = 60
        self._win_lo = 0
        self._win_chunk = 80
        self._full_seq = []
        # 分批渲染:新建卡片按时间预算分批经 0ms 定时器补齐(见 _render_tick),
        # set_view 重来时清空在途批次 = 视图代数,旧批次绝不回填旧记录
        self._render_pending = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_tick)
        self._btn_more = PushButton("", self)
        self._btn_more.setObjectName("btnMore")
        self._btn_more.setStyleSheet(
            "QPushButton#btnMore { color: %s; background: transparent;"
            " border: none; padding: 8px; }" % TER)
        self._btn_more.setCursor(Qt.PointingHandCursor)
        self._btn_more.clicked.connect(self._extend_window)
        self.scroll.verticalScrollBar().valueChanged.connect(
            self._on_scroll_maybe_extend)
        self._lab_pinned = QLabel("置顶")
        self._lab_recent = QLabel("最近")
        for _lab in (self._lab_pinned, self._lab_recent):
            _lab.setFont(body_font(10, QFont.Weight.DemiBold))
            _lab.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
            _lab.setContentsMargins(6, 10, 0, 4)
            _lab.hide()
        self._tail_spacer = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        # 尾部件占用标志(_update_more_label 增量切换 更多按钮/弹簧 用)
        self._btn_in_layout = False
        self._tail_owned = False
        self._row_h_est = 84
        self.scroll.setWidget(self.content)
        left.addWidget(self.scroll, 1)

        # 空状态提示
        self._empty_box = QWidget()
        eb = QVBoxLayout(self._empty_box)
        eb.setContentsMargins(0, 48, 0, 48)
        eb.setSpacing(12)
        t1 = QLabel("暂无剪贴内容")
        t1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t1.setFont(head_font(18, QFont.Weight.Bold))
        t1.setStyleSheet("color: %s; background: transparent;" % INK)
        t2 = QLabel("复制文字、截图或文件后，按 Ctrl + Shift + V 打开")
        t2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t2.setFont(body_font(12))
        t2.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        self.empty_title = t1
        self.empty_hint = t2
        self.btn_reset_filter = QPushButton("显示全部内容", self._empty_box)
        self.btn_reset_filter.setFont(label_font(9))
        self.btn_reset_filter.setStyleSheet(_SOFT_BTN)
        self.btn_reset_filter.clicked.connect(self.reset_filters)
        eb.addWidget(t1)
        eb.addWidget(t2)
        eb.addWidget(self.btn_reset_filter, 0, Qt.AlignmentFlag.AlignHCenter)
        self._empty_box.setVisible(False)

        # ---- 预览面板(Ditto 式:选中即显示完整内容,常驻面板底部) ----
        self._pv_compact = False
        self._pv_lang = "text"
        self._pv_display = ""
        self._lang_cache = {}          # id(rec) → detect_lang 结果(避免选中切换重复猜)
        self._pv_img_cache = {}        # path → 预览尺寸 QPixmap(避免反复全图解码)
        self.preview = QFrame()
        self.preview.setFrameShape(QFrame.Shape.NoFrame)
        self.preview.setStyleSheet(
            "QFrame { background: #ffffff; color: %s; border: none;"
            " border-top: 1px solid rgba(0,0,0,0.12);"
            " border-radius: 6px; }" % INK)
        pv = QVBoxLayout(self.preview)
        pv.setContentsMargins(0, 10, 0, 10)
        pv.setSpacing(8)
        ph = QHBoxLayout()
        ph.setSpacing(6)
        self.pv_badge = QLabel("预览".upper())
        self.pv_badge.setFont(label_font(10, QFont.Weight.DemiBold))
        self.pv_badge.setStyleSheet("color: %s; background: transparent;" % INK)
        self.pv_stats = QLabel("")
        self.pv_stats.setFont(label_font(9))
        self.pv_stats.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        self.pv_btn_json = QPushButton("紧凑")
        self.pv_btn_json.setFixedHeight(26)
        self.pv_btn_json.setFont(label_font(9))
        self.pv_btn_json.setToolTip("JSON 美化 / 紧凑切换")
        self.pv_btn_json.setStyleSheet(_SOFT_BTN)
        self.pv_btn_json.clicked.connect(self._toggle_pv_json)
        self.pv_btn_json.setVisible(False)
        self.pv_btn_copy = QPushButton("复制")
        self.pv_btn_copy.setFixedHeight(26)
        self.pv_btn_copy.setFont(label_font(9))
        self.pv_btn_copy.setToolTip("复制完整内容")
        self.pv_btn_copy.setAccessibleName("复制完整内容到剪贴板")
        self.pv_btn_copy.setStyleSheet(_SOFT_BTN)
        self.pv_btn_copy.clicked.connect(self._copy_pv)
        ph.addWidget(self.pv_badge)
        ph.addSpacing(4)
        ph.addWidget(self.pv_stats)
        ph.addStretch(1)
        ph.addWidget(self.pv_btn_json)
        ph.addWidget(self.pv_btn_copy)
        pv.addLayout(ph)

        self.pv_edit = QPlainTextEdit()
        self.pv_edit.setReadOnly(True)
        self.pv_edit.setUndoRedoEnabled(False)
        self.pv_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        pf = QFont("Consolas")
        pf.setStyleHint(QFont.StyleHint.Monospace)
        pf.setPointSize(9)
        self.pv_edit.setFont(pf)
        self.pv_edit.setFixedHeight(128)
        self.pv_edit.setStyleSheet(
            "QPlainTextEdit { background: %s; border: 1px solid %s;"
            " border-radius: 0px; padding: 6px; color: %s; }"
            % (PAPER, INK, INK))
        pv.addWidget(self.pv_edit)

        self.pv_img = QLabel()
        self.pv_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pv_img.setFixedHeight(128)
        self.pv_img.setStyleSheet("background: transparent;")
        self.pv_img.setVisible(False)
        pv.addWidget(self.pv_img)
        left.addWidget(self.preview)

        # ---- 右栏:常驻 AI 工作台(上=上下文篮,下=内嵌快速 AI 面板) ----
        from ctxpack.ui_tray import ContextTray
        self.ctx_tray = ContextTray(app, self)
        right.addWidget(self.ctx_tray)
        try:
            from ai_quick import EmbeddedAIPanel
            self.ai_panel = EmbeddedAIPanel(app, self)
        except Exception:
            cm.log("AI 工作台面板构建失败(主界面其余部分不受影响):\n%s"
                   % traceback.format_exc())
            self.ai_panel = None
        if self.ai_panel is not None:
            right.addWidget(self.ai_panel, 1)
        else:
            right.addStretch(1)
        right_host = QWidget()
        right_host.setLayout(right)
        right_host.setMinimumWidth(340)
        cols.addLayout(left, 3)
        cols.addWidget(right_host, 2)
        v.addLayout(cols, 1)

        # ---- 底栏 ----
        foot = QHBoxLayout()
        foot.setContentsMargins(4, 8, 2, 2)
        self.lbl_count = QLabel("0 个项目")
        self.lbl_count.setFont(label_font(9))
        self.lbl_count.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        foot.addWidget(self.lbl_count)
        foot.addStretch(1)
        self.btn_preview = QPushButton("预览")
        self.btn_preview.setCheckable(True)
        preview_on = bool(getattr(app, "cfg", {}).get("preview_visible", True))
        self.btn_preview.setChecked(preview_on)
        self.preview.setVisible(preview_on)
        self.btn_preview.setFixedHeight(26)
        self.btn_preview.setFont(label_font(9))
        self.btn_preview.setToolTip("显示 / 隐藏底部预览面板")
        self.btn_preview.setStyleSheet(_SOFT_BTN)
        self.btn_preview.clicked.connect(lambda checked: self.toggle_preview(checked))
        foot.addWidget(self.btn_preview)
        foot.addSpacing(6)
        for btxt in ("↑ ↓", "Enter", "Esc"):
            b = QLabel(btxt)
            b.setFont(label_font(8))
            b.setStyleSheet(_KEY_BADGE)
            foot.addWidget(b)
        left.addLayout(foot)

        self._cards = []

    # ---- 事件 ----
    def reset_filters(self):
        self.search.clear()
        for key, btn in self._cat_btns.items():
            btn.blockSignals(True)
            btn.setChecked(key == "all")
            btn.blockSignals(False)
        self.app.set_category("all")
        self.app.set_src_filter("")
        self.app.set_time_filter("all")
        self.sync_src_combo()
        self.cmb_time.setCurrentIndex(0)
        self.app.refresh_view()
        self.search.setFocus()

    def sync_src_combo(self):
        """来源下拉与历史同步(最近在前,保留当前选择;被清空的来源回落到全部)。
        来源集合与当前选择都未变时跳过重建(clear+addItem 会触发整轮
        模型重置,大历史高频刷新下的纯无效功)。"""
        cur = self.cmb_src.currentData() or ""
        apps = self.app.core.source_apps()[:40]
        if cur and cur not in apps:
            self.app.src_filter = ""       # 来源已被清理,过滤条件同步失效
            cur = ""
        if (tuple(apps), cur) == getattr(self, "_src_sig", None):
            return                         # 集合与选择未变:零成本返回
        self._src_sig = (tuple(apps), cur)
        self.cmb_src.blockSignals(True)
        self.cmb_src.clear()
        self.cmb_src.addItem("全部来源", userData="")
        for s in apps:
            self.cmb_src.addItem(s, userData=s)
        idx = self.cmb_src.findData(cur)
        self.cmb_src.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_src.blockSignals(False)

    def eventFilter(self, obj, e):
        if obj is self.search:
            if e.type() == QEvent.KeyPress:
                k = e.key()
                if k in (Qt.Key_Down, Qt.Key_Up):
                    self.app.move_sel(1 if k == Qt.Key_Down else -1)
                    return True
                if k == Qt.Key_Escape:
                    self.app.win.hide()
                    return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.app.win.hide()
        elif e.key() == Qt.Key_F1:                  # 应用内快捷键总览
            self.app.show_shortcut_help()
        elif e.key() == Qt.Key_Down:
            self.app.move_sel(1)
        elif e.key() == Qt.Key_Up:
            self.app.move_sel(-1)
        elif e.key() == Qt.Key_Return or e.key() == Qt.Key_Enter:
            m = e.modifiers()
            if m & Qt.ControlModifier and m & Qt.ShiftModifier:
                self.app.open_transform()          # Ctrl+Shift+Enter 转换粘贴
            elif m & Qt.ShiftModifier:
                self.app.paste_plain()             # Shift+Enter 纯文本
            else:
                self.app.paste_selected()
        elif e.key() == Qt.Key_Delete:
            self.app.delete_selection_or_multi()
        else:
            super().keyPressEvent(e)

    def _resize_to_content(self):
        """兼容保留:页面高度由 FluentWindow 管理,无需自适应。"""

    # ---- 列表(卡片池复用:搜索/分类切换零重建) ----
    # 渲染预算:单批新建卡片的时间上限;首批可见区(RENDER_FIRST)例外,
    # 同步完成保证首屏一次画全,其余批次让出事件循环逐批补齐
    WIN_INITIAL = 60
    RENDER_BUDGET_MS = 10.0
    RENDER_FIRST = 8
    RENDER_MIN_BATCH = 10    # 后台批次最少推进张数(摊薄事件循环固定开销;
                             # 这些批次在 GUI 空闲间隙执行,单批最长 ~60ms)
    WIN_MAX = 480            # 物化卡片硬上限(超出触发上方远端释放;
                             # QVBoxLayout activate 成本随条目数上涨,
                             # 上限控制单次激活在几十毫秒内)
    WIN_KEEP = 380           # 释放后保留的目标数量
    TRIM_MARGIN = 1600       # 视口上方安全距离(px):之内的卡片不释放
    IDLE_CAP = 140           # 闲置卡片池上限(多余 deleteLater)

    def set_view(self, view, history):
        """刷新 = 既有控件的重新排布 + 差量绑定,零销毁重建。

        - 卡片按 rec 身份配对回原控件(bind 幂等,同 rec 零开销),
          配不上的从 _idle 池取,池空才新建;
        - 新建超时间预算的记录转后台批次(_render_tick),视图再来时
          在途批次整体作废(等价视图代数:旧任务不把旧记录回填新视图);
        - 分组标签常驻复用(空组 hide),尾部件二选一(更多按钮/弹簧);
        - 视图序列与分组完全未变时连布局都不碰(重复 show/无效刷新零成本);
        - 窗口化:大视图只物化 [_win_lo, _win_limit) 段(滚动到底自动扩展,
          深滚动超 WIN_MAX 时上方远端释放回池)——控件数有硬上限。
        """
        self._render_pending.clear()        # 作废在途渲染批次
        self._last_view, self._last_history = view, history
        recs = [history[i] for i in view]
        all_seq = ([r for r in recs if r.get("pinned")]
                   + [r for r in recs if not r.get("pinned")])
        if [id(r) for r in self._full_seq] != [id(r) for r in all_seq]:
            self._full_seq = all_seq
            self._win_limit = self.WIN_INITIAL
            self._win_lo = 0                # 新视图 → 重置窗口
        seq = self._full_seq[self._win_lo:self._win_limit]
        pinned = [r for r in seq if r.get("pinned")]
        rest = [r for r in seq if not r.get("pinned")]
        ids = [id(r) for r in seq]
        more = len(self._full_seq) - self._win_limit

        if ([id(c.rec) for c in self._cards] == ids
                and self._lab_pinned.isVisible() == bool(pinned)
                and self._lab_recent.isVisible() == bool(rest)
                and self._btn_more.isVisible() == (more > 0)
                and not self._empty_box.isVisible()):
            return

        cur = {id(c.rec): c for c in self._cards}
        idle = self._idle
        # 视图外旧卡先整体视为可复用池(循环内即取即用,不等到循环后):
        # 搜索→全量来回切换时全部走重绑(毫秒级),不再批量新建卡片。
        # taken = 已被身份匹配占用的卡,复用弹出时跳过(防同一控件双用)
        spare = list(self._cards)
        taken = set()
        # 闲置卡也记得上一次绑的 rec:按身份捞回可连 bind 都跳过
        # (搜索过滤→清空的往返因此退化为纯重排)
        idle_by_rec = {}
        for c in idle:
            if c.rec is not None:
                idle_by_rec.setdefault(id(c.rec), c)
        cards = []
        deadline = time.perf_counter() + self.RENDER_BUDGET_MS / 1000.0
        for idx, r in enumerate(seq):
            c = cur.pop(id(r), None) or idle_by_rec.pop(id(r), None)
            if c is not None:
                taken.add(id(c))
                try:
                    idle.remove(c)
                except ValueError:
                    pass
            else:                       # 无身份匹配:闲置→旧卡→预算内新建
                c = idle.pop() if idle else None
                if c is None:
                    while spare:
                        cand = spare.pop()
                        if id(cand) not in taken:
                            c = cand
                            break
                    if c is not None:
                        cur.pop(id(c.rec), None)   # 已被改绑:身份表除名
                if c is not None:
                    c.bind(r)
                else:
                    c = self._new_card(r, deadline)
                    if c is None:       # 超预算:转后台批次(带序号)
                        self._render_pending.append((r, self._win_lo + idx))
                        continue
            c._seq_i = self._win_lo + idx
            cards.append(c)
        idle.extend(cur.values())       # 循环后仍没配上的旧卡 → 闲置池
        # 置顶态只会随分组移动而变(bind 的同 rec 守卫跳过了字段刷新)
        for c, r in zip(cards, seq):
            grp = 1 if r.get("pinned") else 0
            if getattr(c, "_grp", None) != grp:
                c._grp = grp
                c.update_pin()
        self._cards = cards
        self.app._card_widgets = cards
        # 闲置池收回的卡带着显式 hide 标志,重新 addWidget 不会自动显形
        # (只有「链接/文件」这类全空分类会把卡片全部赶进闲置池,丢这行
        # 切回「全部」就是隐形列表:数据在、控件在、一条看不见)
        for c in cards:
            if c.isHidden():
                c.show()

        # 重排布局:全部是既有控件,只挪位置(无创建无 polish 无销毁)。
        # 布局序 = [lab_pinned][置顶卡…][lab_recent][最近卡…](标签常驻,
        # 空组 hide;卡片插入下标据此计算,见 _insert_cards)。
        # 拆建期间禁用布局与重绘,末尾一次激活(大列表防逐项布局级联)。
        self.cards_layout.setEnabled(False)
        self.scroll.setUpdatesEnabled(False)
        try:
            while self.cards_layout.count():
                self.cards_layout.takeAt(0)
            self._tail_owned = False
            self._btn_in_layout = False
            self._empty_box.setVisible(False)
            self.cards_layout.addWidget(self._lab_pinned)
            for c in cards[:len(pinned)]:
                self.cards_layout.addWidget(c)
            self.cards_layout.addWidget(self._lab_recent)
            for c in cards[len(pinned):]:
                self.cards_layout.addWidget(c)
            self._lab_pinned.setVisible(bool(pinned))
            self._lab_recent.setVisible(bool(rest))
            if more > 0:
                # 窗口化截断:显示扩展入口(滚动到底也自动扩展)
                self._btn_more.setText("继续滚动加载更多(还有 %d 条)"
                                       % (more + self._win_lo))
                self.cards_layout.addWidget(self._btn_more)
                self._btn_in_layout = True
                self._btn_more.setVisible(True)
            else:
                self._btn_more.setVisible(False)
                self.cards_layout.addItem(self._tail_spacer)
                self._tail_owned = True
            if not seq:
                filtered = bool(history)
                self.empty_title.setText("没有找到匹配内容" if filtered else "暂无剪贴内容")
                self.empty_hint.setText("试试其他关键词,或清除搜索与分类筛选" if filtered else
                                        "复制文字、截图或文件后,按 Ctrl + Shift + V 打开")
                self.btn_reset_filter.setVisible(filtered)
                self._empty_box.setVisible(True)
                self.cards_layout.addWidget(self._empty_box)
        finally:
            self.cards_layout.setEnabled(True)
            self.scroll.setUpdatesEnabled(True)
            self.cards_layout.activate()
        for c in idle:
            c.hide()
        if self._render_pending:
            self._render_timer.start()
        self._trim_idle()

    def _new_card(self, r, deadline):
        """预算内新建卡片;超预算返回 None(调用方转后台批次)。"""
        if time.perf_counter() > deadline:
            return None
        c = Card(self.app)
        c.bind(r)
        return c

    def _render_tick(self):
        """后台渲染批次:预算内新建若干卡片并按分组位置插入。
        set_view 会清空 _render_pending,过时批次自然作废(等价视图代数)。"""
        if not self._render_pending:
            return
        deadline = time.perf_counter() + self.RENDER_BUDGET_MS / 1000.0
        newly = []
        while self._render_pending:
            if len(newly) >= self.RENDER_MIN_BATCH \
                    and time.perf_counter() > deadline:
                break                   # 每批至少推进 RENDER_MIN_BATCH 张,
                                        # 再按预算截断(事件循环遍历全部
                                        # qfluentwidgets 事件过滤器有固定
                                        # 开销,单卡小批会被它吃满)
            r, seq_i = self._render_pending.pop(0)
            c = self._idle.pop() if self._idle else Card(self.app)
            c.bind(r)
            c._seq_i = seq_i
            newly.append((r, c))
        if newly:
            self._insert_cards(newly)
        if self._render_pending:
            self._render_timer.start()
        else:
            self._trim_idle()

    def _insert_cards(self, newly):
        """把新建/复用的卡片按 _seq_i 顺序插入分组与布局(增量,不重排)。
        newly: [(rec, card)],调用方已设好 card._seq_i(记录在 _full_seq
        中的下标);置顶组/最近组各自保持序列顺序。

        批量插入期间禁用布局与重绘、末尾一次激活——逐个 show() 在大列表
        下每次都触发完整布局级联(600 存活卡实测 14ms/张 → 3.2ms/张)。"""
        pinned_n = sum(1 for e in self._cards if e.rec.get("pinned"))
        lay = self.cards_layout
        lay.setEnabled(False)
        self.scroll.setUpdatesEnabled(False)
        try:
            for r, c in newly:
                c.show()
                pinned = bool(r.get("pinned"))
                c._grp = 1 if pinned else 0
                c.update_pin()
                if pinned:
                    pos = pinned_n                     # 置顶组末尾
                    for i in range(pinned_n):
                        if self._cards[i]._seq_i > c._seq_i:
                            pos = i
                            break
                else:
                    pos = len(self._cards)             # 最近组末尾
                    for i in range(pinned_n, len(self._cards)):
                        if self._cards[i]._seq_i > c._seq_i:
                            pos = i
                            break
                self._cards.insert(pos, c)
                if pinned:
                    pinned_n += 1
                # 布局序 [lab_pinned][置顶卡][lab_recent][最近卡]:卡片下标
                # 换算布局下标 = 前置标签数(置顶卡 1 / 最近卡 2)+ 组内位置
                lay_idx = 1 + pos if pos < pinned_n else 2 + pos
                lay.insertWidget(lay_idx, c)
            self.app._card_widgets = self._cards
            # 分组标签可见性随物化内容变化
            self._lab_pinned.setVisible(pinned_n > 0)
            self._lab_recent.setVisible(len(self._cards) - pinned_n > 0)
        finally:
            lay.setEnabled(True)
            self.scroll.setUpdatesEnabled(True)
            lay.activate()

    def _queue_cards(self, recs, base, visible_first=False):
        """物化一段记录(序列号从 base 起):身份捞回 > 任意闲置 >
        预算内新建/重绑 > 后台批次(_render_tick 续建)。预算对重绑同样
        生效——大段闲置重绑逐个也要毫秒级,不能整段同步做完。"""
        deadline = time.perf_counter() + self.RENDER_BUDGET_MS / 1000.0
        idle_by_rec = {}
        for c in self._idle:
            if c.rec is not None:
                idle_by_rec.setdefault(id(c.rec), c)
        newly, done = [], 0
        for i, r in enumerate(recs):
            c = idle_by_rec.pop(id(r), None)
            if c is not None:
                try:
                    self._idle.remove(c)
                except ValueError:
                    pass
            else:
                c = self._idle.pop() if self._idle else None
                if c is not None:
                    c.bind(r)
                else:
                    if done and time.perf_counter() > deadline:
                        self._render_pending.append((r, base + i))
                        continue
                    c = Card(self.app)
                    c.bind(r)
            if c.rec is not r and c._bound_id != id(r):
                c.bind(r)               # 身份捞回的卡:确保绑定生效
            c._seq_i = base + i
            newly.append((r, c))
            done += 1
            if done >= (self.RENDER_FIRST if visible_first else 1) \
                    and time.perf_counter() > deadline \
                    and i + 1 < len(recs):
                self._render_pending.extend(
                    (rr, base + j) for j, rr in enumerate(recs[i + 1:], i + 1))
                break
        if newly:
            self._insert_cards(newly)
        if self._render_pending:
            self._render_timer.start()

    def _update_more_label(self):
        more = len(self._full_seq) - self._win_limit
        if more > 0:
            self._btn_more.setText("继续滚动加载更多(还有 %d 条)"
                                   % (more + self._win_lo))
            if not self._btn_in_layout:
                if self._tail_owned:
                    self.cards_layout.removeItem(self._tail_spacer)
                    self._tail_owned = False
                self.cards_layout.addWidget(self._btn_more)
                self._btn_in_layout = True
            self._btn_more.setVisible(True)
        else:
            if self._btn_in_layout:
                self.cards_layout.removeWidget(self._btn_more)
                self._btn_in_layout = False
            self._btn_more.setVisible(False)
            if not self._tail_owned:
                self.cards_layout.addItem(self._tail_spacer)
                self._tail_owned = True

    def _extend_window(self):
        """扩展物化窗口(+chunk),只追加新卡片(不拆除既有布局)。"""
        if self._win_limit >= len(self._full_seq):
            return
        lo = self._win_limit
        self._win_limit = min(lo + self._win_chunk, len(self._full_seq))
        self._queue_cards(self._full_seq[lo:self._win_limit], base=lo)
        self._update_more_label()
        self._trim_window()

    def _prepend_window(self):
        """向上扩展物化窗口(滑窗回滚:向上滚动补回上方释放的卡片)。
        上方释放的卡多在闲置池,身份捞回成本≈重绑;滚动值按行高估计
        下移补偿,视口内容不跳。"""
        if self._win_lo <= 0:
            return
        hi = self._win_lo
        self._win_lo = max(0, hi - self._win_chunk)
        recs = self._full_seq[self._win_lo:hi]
        deadline = time.perf_counter() + self.RENDER_BUDGET_MS / 1000.0
        idle_by_rec = {}
        for c in self._idle:
            if c.rec is not None:
                idle_by_rec.setdefault(id(c.rec), c)
        newly, created = [], 0
        for i, r in enumerate(recs):
            c = idle_by_rec.pop(id(r), None)
            if c is not None:
                try:
                    self._idle.remove(c)
                except ValueError:
                    pass
            else:
                c = self._idle.pop() if self._idle else None
                if c is not None:
                    c.bind(r)
                else:
                    if created and time.perf_counter() > deadline:
                        self._render_pending.insert(0, (r, self._win_lo + i))
                        continue
                    c = Card(self.app)
                    created += 1
                    c.bind(r)
            c._seq_i = self._win_lo + i
            newly.append((r, c))
        if newly:
            self._insert_cards(newly)
        if self._render_pending:
            self._render_timer.start()
        sb = self.scroll.verticalScrollBar()
        row = getattr(self, "_row_h_est", 84) + self.cards_layout.spacing()
        delta = int(row * len(recs))
        QTimer.singleShot(
            0, lambda d=delta: sb.setValue(min(sb.maximum(), sb.value() + d)))

    def _trim_window(self):
        """物化超过 WIN_MAX 时,把视口上方远端的最近组卡片释放回闲置池。
        置顶组不动(数量少且置顶语义要求常驻);滚动值按实际控件高度
        补偿,视口内容不跳。"""
        if len(self._cards) <= self.WIN_MAX:
            return
        sb = self.scroll.verticalScrollBar()
        if sb.maximum() <= 0:
            return
        vp = self.scroll.viewport()
        pinned_n = sum(1 for c in self._cards if c.rec.get("pinned"))
        released, freed_px = [], 0
        i = pinned_n
        while i < len(self._cards) \
                and len(self._cards) - len(released) > self.WIN_KEEP:
            c = self._cards[i]
            # mapTo viewport:视口内容坐标(视口上方为负);卡片底边
            # 距视口顶不足安全距离的不释放
            y = c.mapTo(vp, QPoint(0, 0)).y()
            if y + c.height() >= -self.TRIM_MARGIN:
                break
            released.append(c)
            freed_px += c.height() + self.cards_layout.spacing()
            i += 1
        if not released:
            return
        first_i = self._cards.index(released[0])
        self.cards_layout.setEnabled(False)
        self.scroll.setUpdatesEnabled(False)
        try:
            for c in released:
                c.hide()
                self.cards_layout.removeWidget(c)
        finally:
            self.cards_layout.setEnabled(True)
            self.scroll.setUpdatesEnabled(True)
            self.cards_layout.activate()
        del self._cards[first_i:first_i + len(released)]
        self._win_lo += len(released)
        self._row_h_est = max(40, round(freed_px / len(released)
                                        - self.cards_layout.spacing()))
        self._idle.extend(released)
        self.app._card_widgets = self._cards
        self._update_more_label()
        QTimer.singleShot(
            0, lambda v=freed_px: sb.setValue(max(0, sb.value() - v)))
        self._trim_idle()

    def _trim_idle(self):
        """闲置池上限:超出的卡片销毁(新建成本低,长期驻留才贵)。"""
        if len(self._idle) > self.IDLE_CAP:
            for c in self._idle[self.IDLE_CAP:]:
                c.deleteLater()
            del self._idle[self.IDLE_CAP:]

    def _on_scroll_maybe_extend(self, value):
        """滚动接近底部 → 自动扩展窗口(距底 <600px);接近顶部且上方
        有已释放段 → 向上补回(滑窗回滚)。
        滚动条未成形(max=0,内容未超视口或首次布局)与置顶态不触发,
        防止视图刚建立就被整页自动展开。"""
        if not self._full_seq:
            return
        sb = self.scroll.verticalScrollBar()
        if sb.maximum() <= 0 or value <= 0:
            return
        if sb.maximum() - value < 600 and self._win_limit < len(self._full_seq):
            self._extend_window()
        elif value < 800 and self._win_lo > 0:
            self._prepend_window()

    def card_at(self, vi):
        """视图索引 → 对应卡片控件。卡片列表顺序 = 置顶组+最近组(set_view),
        与 _view 的历史序不一致,任何「按下标取卡」都必须经这里按记录身份解析。
        (离屏预览的 FakeApp 无 _view:退回控件下标,纯装饰性选择不受影响)"""
        view = getattr(self.app, "_view", None)
        if view is None:
            if vi is not None and 0 <= vi < len(self._cards):
                return self._cards[vi]
            return None
        if vi is None or not (0 <= vi < len(view)):
            return None
        hi = view[vi]
        if not (0 <= hi < len(self.app.history)):
            return None
        rec = self.app.history[hi]
        return next((c for c in self._cards if c.rec is rec), None)

    def select_index(self, vi):
        card = self.card_at(vi)
        for c in self._cards:
            c.set_selected(c is card)
        self.update_preview(vi)

    def mark_multi(self, rec_ids):
        """段选高亮(Shift+点击批量选择):按记录身份标亮整段,预览清空。"""
        for c in self._cards:
            c.set_selected(id(c.rec) in rec_ids)
        self.update_preview(None)

    # ---- 预览面板(Ditto 式:随选中联动显示完整内容) ----
    def _sync_ai_material(self, vi=None):
        """选中变化 → 右栏 AI 工作台的材料源跟随(生成中不抢占)。"""
        panel = getattr(self, "ai_panel", None)
        if panel is None:
            return
        app = self.app
        if vi is None:
            vi = getattr(app, "selected", None)
        rec = None
        hist = getattr(app, "history", None)
        if vi is not None and hist:
            view = getattr(app, "_view", None)
            if view is None:
                # 测试/预览替身无 _view:按卡片下标直取(与 card_at 同一回退)
                if 0 <= vi < len(hist):
                    rec = hist[vi]
            elif 0 <= vi < len(view) and view[vi] < len(hist):
                rec = hist[view[vi]]
        try:
            panel.set_material(rec)
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        panel = getattr(self, "ai_panel", None)
        if panel is not None:
            panel.refresh_services()      # 服务配置可能在别的页改过

    def update_preview(self, vi=None):
        self._sync_ai_material(vi)
        if not self.preview.isVisible():
            return
        app = self.app
        if vi is None:
            vi = getattr(app, "selected", None)
        rec = None
        _view = getattr(app, "_view", None)
        if vi is not None:
            if _view is None:
                # 测试/预览替身无 _view:按卡片下标直取(与 card_at 同一回退)
                if 0 <= vi < len(app.history):
                    rec = app.history[vi]
            elif 0 <= vi < len(_view) and _view[vi] < len(app.history):
                rec = app.history[_view[vi]]
        if rec is None:
            self.pv_btn_copy.setEnabled(False)
            self.pv_badge.setText("预览")
            self.pv_stats.setText("")
            self.pv_edit.setPlainText("")
            self.pv_img.setVisible(False)
            self.pv_edit.setVisible(True)
            self.pv_btn_json.setVisible(False)
            self._pv_display = ""
            return
        self.pv_btn_copy.setEnabled(True)
        t = rec["type"]
        if t == "image":
            self.pv_edit.setVisible(False)
            self.pv_img.setVisible(True)
            self.pv_btn_json.setVisible(False)
            path = rec.get("content") or ""
            self.pv_img.clear()
            # 预览只用缓存缩略图(256px,后台生成);原图仅双击查看器加载,
            # 列表/预览路径不再同步解码原图(大图曾把选中卡成百毫秒级)
            tc = getattr(app, "thumb_cached", None)
            pm = tc(path) if tc is not None else \
                (getattr(app, "_thumbs", None) or {}).get(path)
            if pm is not None:
                self.pv_img.setPixmap(pm.scaled(
                    min(PANEL_W - 40, pm.width() * 8), 128,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                rt = getattr(app, "request_thumb", None)
                if rt is not None:
                    rt(path, visible=True)  # 可见请求:生成完下次选中即有
                self.pv_img.setText("生成缩略图中…")
            self.pv_badge.setText("截图 · 放大预览")
            self.pv_stats.setText("%s × %s · 双击可全屏查看" %
                                  (rec.get("w", 0), rec.get("h", 0)))
            self._pv_display = ""
            return
        raw = "\n".join(rec.get("content") or []) if t == "files" \
            else (rec.get("content") or "")
        # 超长文本:截断预览 + 全部重活(猜语言/JSON 美化/染色)只做截断段
        truncated = len(raw) > _PV_TEXT_CAP
        lang = "text"
        if t == "text":
            lang = self._lang_cache.get(id(rec))
            if lang is None:
                lang = cm.detect_lang(raw[:_PV_TEXT_CAP])
                if len(self._lang_cache) > 512:
                    self._lang_cache.clear()
                self._lang_cache[id(rec)] = lang
        if lang == "json" and not truncated:
            self._pv_display = cm.json_reformat(raw, self._pv_compact) or raw
        elif truncated:
            self._pv_display = raw[:_PV_TEXT_CAP] + \
                "\n…(共 %d 字符,已截断;右键 → 在独立窗口查看全文)" % len(raw)
        else:
            self._pv_display = raw
        self.pv_img.setVisible(False)
        self.pv_edit.setVisible(True)
        self.pv_edit.setPlainText(self._pv_display)
        apply_pygments_format(self.pv_edit.document(), self._pv_display, lang)
        self._pv_lang = lang
        kind = {"text": dict(cm.VIEW_LANGS).get(lang, "文本"),
                "files": "文件"}[t]
        self.pv_badge.setText("%s · %s" % (kind, cm._rel_time(rec.get("ts", 0))))
        if truncated:
            self.pv_stats.setText("已截断 · 全文 %s" %
                                  cm._fmt_size(len(raw.encode("utf-8"))))
        else:
            self.pv_stats.setText("%d 行 · %s" % (
                self._pv_display.count("\n") + 1,
                cm._fmt_size(len(self._pv_display.encode("utf-8")))))
        self.pv_btn_json.setVisible(lang == "json" and not truncated)

    def _toggle_pv_json(self):
        self._pv_compact = not self._pv_compact
        self.pv_btn_json.setText("紧凑" if not self._pv_compact else "美化")
        self.update_preview()

    def _copy_pv(self):
        app = self.app
        vi = app.selected
        if vi is None or not 0 <= vi < len(app._view):
            return
        rec = app.history[app._view[vi]]
        if not app.core.copy_record(rec):
            InfoBar.error("复制失败", "内容不可用或剪贴板正被占用,请重试", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        InfoBar.success("已复制", "完整原始内容已复制,可切换到目标窗口粘贴", parent=self,
                        position=InfoBarPosition.TOP_RIGHT)

    def toggle_preview(self, on):
        self.btn_preview.setChecked(on)
        self.app.cfg["preview_visible"] = bool(on)
        cm.save_config(self.app.cfg)
        self.preview.setVisible(on)
        if on:
            self.update_preview()
        self._resize_to_content()

    def ensure_visible(self, vi):
        card = self.card_at(vi)     # 置顶组重排后控件下标≠视图下标,按记录找卡
        if card is not None:
            self.scroll.ensureWidgetVisible(card, 0, 20)

# ============================================================
# 私密区窗口
# ============================================================
# ============================================================
# 主窗口(FluentWindow:库标准导航壳,所有功能收进单一窗口)
# ============================================================
class MainWindow(FluentWindow):
    """单一主窗口:左侧导航 = 剪贴板 / 工作流 / AI 服务 / 私密区 / 设置。

    热键或托盘切换显示;点窗口关闭按钮 = 隐藏(程序常驻托盘)。
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        # Atelier Zero：禁用 Mica / 亚克力，使用纸色纯色背景。
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(QColor(PAPER), QColor(PAPER))
        self.setStyleSheet("background: %s;" % PAPER)
        self.setWindowTitle("WinClipboard")
        self.setWindowIcon(app.icon)
        # 提前创建原生窗口句柄:主窗常驻隐藏,但二次启动的唤起路径靠
        # FindWindow("WinClipboard") 找它——没有原生句柄就找不到,会退化成
        # 「已在运行中」弹窗(此前的句柄是保活块里的 winId() 顺手创建的,
        # 保活按远程会话门控后本地就没了,必须在此无条件创建)
        try:
            int(self.winId())
        except Exception:
            pass
        # 主线程派发器保活:远程会话等环境下 Qt 派发器会"无消息即休眠",
        # 后台事件的队列唤醒(排空任务投递)随之停摆;实测任何到达主窗口的
        # 投递消息都能立刻唤醒(WM_TIMER 无效——低优先级合成消息唤不醒 MsgWait)。
        # compat.remote_session_keepalive: auto=检测到远程会话才开(默认,
        # 本地正常环境不背 500ms 空转唤醒) / on=无条件开 / off=关闭
        # (不置 RDP 标志的远控工具假死时,可手动设 on;派发器假死另有
        # UiHealthMonitor 看门狗兜底)。
        try:
            _ka_mode = str((app.cfg.get("compat") or {}).get(
                "remote_session_keepalive", "auto")).lower()
        except Exception:
            _ka_mode = "auto"
        if _ka_mode == "on" or (_ka_mode != "off" and cm.is_remote_session()):
            try:
                import ctypes
                _hwnd = int(self.winId())

                def _keepalive():
                    import time as _t
                    _u = ctypes.windll.user32
                    while True:
                        _u.PostMessageW(_hwnd, 0x0000, 0, 0)   # WM_NULL
                        _t.sleep(0.5)
                threading.Thread(target=_keepalive, daemon=True).start()
            except Exception:
                pass
        self.history_page = HistoryPage(app)
        self.workflow_page = WorkflowPage(app)
        self.services_page = ServicesPage(app)
        self.private_page = PrivatePage(app)
        self.settings_page = SettingsPage(app)
        self.addSubInterface(self.history_page, FIF.HISTORY, "剪贴板")
        self.addSubInterface(self.workflow_page, FIF.ROBOT, "工作流")
        self.addSubInterface(self.services_page, FIF.CLOUD, "AI 服务")
        self.addSubInterface(self.private_page, FIF.IOT, "私密区")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置",
                             NavigationItemPosition.BOTTOM)
        self.resize(1040, 680)

    def toggle(self):
        """热键/托盘:显示并激活,或隐藏(最小化过则还原)。"""
        if self.isVisible() and self.isActiveWindow():
            self.app._hotkey_ts = None      # 收起路径不参与首帧统计
            self.hide()
        else:
            # 粘贴事务目标:显示前的前台窗口 = 用户此刻的工作现场;
            # 之后回车粘贴将以它为校验目标(窗口切换则取消,见 _do_cmd)
            fg = cm._get_foreground()
            if fg and fg != int(self.winId()):
                self.app._paste_target = fg
            self.showWindow()

    def show_fast(self):
        """只做可见性三件套,零重活——热键路径允许的全部同步成本。
        不碰列表/预览/缩略图:先让事件循环把现有卡片画出来。"""
        with PerfSpan("window_reshow"):
            self.show()
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def showWindow(self):
        """热键/托盘显示:先 show_fast 画出现有卡片(启动预热已备好),
        隐藏期间攒下的脏刷新让到下一拍(0ms 定时器 = 让出事件循环,
        首帧绘制先走),绝不挡窗口出现。"""
        self.show_fast()
        self.history_page.search.setFocus()
        if getattr(self.app, "_view_dirty", True):
            QTimer.singleShot(0, self.app.refresh_view)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:     # Esc 在任何页面都能收起窗口
            self.hide()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.hide()          # 常驻托盘:关闭按钮只隐藏
        e.ignore()


class PrivatePage(QWidget):
    """私密区:列表默认遮罩显示,点「显示明文」解锁,30 秒后或离开页面
    自动重新遮蔽——防止旁人扫一眼屏幕就拿到凭据明文。"""

    REVEAL_SECONDS = 30

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("privatePage")
        self._revealed = False
        self._mask_timer = QTimer(self)
        self._mask_timer.setSingleShot(True)
        self._mask_timer.setInterval(self.REVEAL_SECONDS * 1000)
        self._mask_timer.timeout.connect(lambda: self._set_revealed(False))
        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)

        ttl = QLabel("PRIVATE ARCHIVE")
        ttl.setFont(head_font(18, QFont.Weight.Bold))
        ttl.setStyleSheet("color: %s; background: transparent;" % INK)
        v.addWidget(ttl)

        hint = QLabel("以下内容经敏感识别后隔离保存。列表默认遮罩，点「显示明文」解锁，"
                      "30 秒后自动遮回。机制为 Windows DPAPI（绑定当前用户）。")
        hint.setWordWrap(True)
        hint.setFont(body_font(11))
        hint.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        v.addWidget(hint)

        # 实际存储/保存状态行
        self.lbl_state = QLabel("")
        self.lbl_state.setFont(label_font(9))
        self.lbl_state.setWordWrap(True)
        self.lbl_state.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        v.addWidget(self.lbl_state)

        self.lst = QListWidget()
        self.lst.setItemDelegate(EditorialDelegate(self.lst, show_index=True))
        self.lst.setAccessibleName("私密内容列表(默认遮罩显示)")
        self.lst.itemDoubleClicked.connect(lambda _: self.do_view())
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border: none; outline: none; padding: 0px; }"
            "QListWidget::item { color: %s; border-bottom: 1px solid rgba(17,17,17,0.08); "
            "padding: 0px; }"
            "QListWidget::item:selected { background: %s; color: %s; }" %
            (PAPER, INK, INK, PAPER))
        v.addWidget(self.lst, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        b_copy = QPushButton("复制并粘贴", self)
        self.b_eye = QPushButton("显示明文", self)
        b_view = QPushButton("查看", self)
        b_view.setToolTip("独立窗口查看(JWT 自动解码;仅解码,不验签)")
        b_del = QPushButton("删除", self)
        b_clear = QPushButton("清空", self)
        for b in (b_copy, self.b_eye, b_view, b_del, b_clear):
            b.setFont(label_font(9))
            b.setStyleSheet(
                "QPushButton { background: %s; color: %s; border: 1px solid %s; "
                "border-radius: 0px; padding: 6px 14px; }"
                "QPushButton:hover { background: %s; color: %s; }" %
                (PAPER, INK, INK, INK, PAPER))
        b_copy.clicked.connect(self.do_copy)
        self.b_eye.clicked.connect(lambda: self._set_revealed(not self._revealed))
        b_view.clicked.connect(self.do_view)
        b_del.clicked.connect(self.do_del)
        b_clear.clicked.connect(self.do_clear)
        for b in (b_copy, self.b_eye, b_view, b_del, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.refresh()

    # ---- 遮罩 / 解锁 ----
    def _mask_text(self, s):
        one = s.replace("\n", " ").replace("\t", " ")
        if self._revealed:
            return one[:80] + ("…" if len(one) > 80 else "")
        n = len(one)
        return "•" * min(n, 18) + ("(%d 字)" % n if n > 18 else "")

    def _set_revealed(self, on):
        if on == self._revealed:
            return
        self._revealed = on
        if on:
            self._mask_timer.start()
        else:
            self._mask_timer.stop()
        self.b_eye.setText("隐藏明文" if on else "显示明文")
        self.b_eye.setIcon(FIF.HIDE.icon() if on else FIF.VIEW.icon())
        self.refresh()

    def hideEvent(self, e):
        # 切走页面立即遮回:不留一个常亮的明文清单
        self._set_revealed(False)
        super().hideEvent(e)

    def refresh(self):
        self.lst.clear()
        for idx, s in enumerate(self.app.private):
            item = QListWidgetItem()
            # 用字典承载记录，EditorialDelegate 可统一绘制
            rec = {
                "type": "text",
                "content": self._mask_text(s),
                "src": "",
                "ts": 0,
                "pinned": False,
            }
            item.setData(Qt.ItemDataRole.UserRole, rec)
            # 同步写一份 item 文本:委托绘制用 UserRole,这里保住可访问性
            # 与既有断言(item(0).text() 读遮罩/明文)——纯 Atelier 委托化
            # 遗漏,导致集成测试三连挂
            item.setText(rec["content"])
            item.setSizeHint(QSize(self.lst.viewport().width(), 72))
            self.lst.addItem(item)
        self._sync_state()

    def _sync_state(self):
        """状态行如实反映实际存储/保存状态:不以"模块存在"推断加密。"""
        core = self.app.core
        st = getattr(core, "_private_load", None)
        parts = []
        if st is not None:
            if st.status == "unreadable":
                parts.append("当前存储:无法读取(原文件已保留未覆写;"
                             "本会话新增/删除只暂存内存;如需放弃原数据,"
                             "请执行「清空」的破坏性流程)")
            elif st.status == "recovered":
                parts.append("当前存储:已从备份 %s 恢复(主文件损坏,"
                             "原件保留为 private.json.corrupt 供排查)" % st.source)
            elif st.status == "ok" and st.mode == "dpapi":
                parts.append("当前存储:DPAPI 已加密(绑定当前 Windows 用户)")
            elif st.status == "ok" and st.mode == "plaintext":
                parts.append("当前存储:旧明文/未加密(加密迁移失败或 DPAPI 不可用,"
                             "不自动降级写明文)")
        if getattr(core, "_private_dirty", False):
            parts.append("尚有未保存的私密区变更(上次保存失败,原文件未动)")
        if parts:
            self.lbl_state.setText(";".join(parts))
            self.lbl_state.show()
        else:
            self.lbl_state.hide()

    def do_copy(self):
        item = self.lst.currentItem()
        if not item:
            return
        i = self.lst.row(item)
        if not 0 <= i < len(self.app.private):
            return
        # 按钮就是「复制并粘贴」:用户显式要求回贴,不受 auto_paste 默认关影响;
        # 写入失败不派发回贴,如实提示
        if not self.app.core.paste_text(
                self.app.private[i], force=True,
                target_hwnd=getattr(self.app, "_paste_target", None)):
            InfoBar.error("粘贴失败", "剪贴板写入失败,请重试", parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=5000)

    def do_view(self):
        """私密条目 → 独立查看器(JWT 自动落解码视图;rec 带 private 标记,
        查看器内「复制」只写剪贴板,不往此前聚焦的窗口回贴)。"""
        item = self.lst.currentItem()
        if not item:
            return None
        i = self.lst.row(item)
        if not 0 <= i < len(self.app.private):
            return None
        return self.app.open_viewer({"type": "text", "content": self.app.private[i],
                                     "private": True})

    def do_del(self):
        item = self.lst.currentItem()
        if not item:
            return
        i = self.lst.row(item)
        ok, err = self.app.core.delete_private(i)
        self.refresh()
        if not ok:
            InfoBar.error("删除失败", "%s;条目保持原状" % err, parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=6000)

    def do_clear(self):
        st = getattr(self.app.core, "_private_load", None)
        unreadable = st is not None and st.status == "unreadable"
        box = MessageBox(
            "私密内容",
            "确定清空全部私密内容?"
            + ("\n\n注意:原文件当前无法读取,此操作将一并丢弃其中无法读取"
               "的内容(不可恢复)。" if unreadable else ""), self)
        if box.exec():
            ok, err = self.app.core.clear_private()
            self.refresh()
            if not ok:
                InfoBar.error("清空失败", "%s;列表保持原状" % err, parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=6000)
            else:
                InfoBar.success("已清空", "私密区与备份已全部清除", parent=self,
                                position=InfoBarPosition.TOP_RIGHT)


# ============================================================
# 图片编辑器
# ============================================================

# ============================================================
# 设置页(全部使用 qfluentwidgets SettingCard 组件)
# ============================================================
# Qt 键值 → VK 码(HotkeyRecorderDialog 用;OEM 符号键,含快捷粘贴默认 `)
_QT_KEY_TO_VK = {
    Qt.Key_QuoteLeft: 0xC0, Qt.Key_Minus: 0xBD, Qt.Key_Equal: 0xBB,
    Qt.Key_Comma: 0xBC, Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF,
    Qt.Key_Semicolon: 0xBA, Qt.Key_Apostrophe: 0xDE,
    Qt.Key_BracketLeft: 0xDB, Qt.Key_BracketRight: 0xDD,
    Qt.Key_Backslash: 0xDC,
}


class HotkeyRecorderDialog(MessageBoxBase):
    """全局热键录制:点击输入框直接按键捕获组合,「应用」经核心线程
    重注册(即冲突检测),被占用时回滚并提示,不再要求手改配置文件。"""

    _MODIFIER_KEYS = (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt,
                      Qt.Key_Meta, Qt.Key_AltGr)

    def __init__(self, parent, cur_label, on_apply):
        super().__init__(parent)
        self._on_apply = on_apply
        self._mod = 0
        self._key = 0

        ttl = StrongBodyLabel("录制全局热键")
        self.viewLayout.addWidget(ttl)
        sub = CaptionLabel("当前热键:%s。点击下方输入框,直接按下新组合键"
                           "(需搭配 Ctrl / Shift / Alt / Win 至少一个);Esc 取消。"
                           % cur_label)
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        self.capture = LineEdit()
        self.capture.setReadOnly(True)
        self.capture.setPlaceholderText("点这里,然后按下组合键(如 Ctrl+Shift+V)")
        # 只读框默认不吃按键:按下即视为录制(实例级重载,不改库组件)
        self.capture.keyPressEvent = self._on_key
        self.viewLayout.addWidget(self.capture)

        self.yesButton.setText("应用")
        self.cancelButton.setText("取消")
        self.yesButton.setEnabled(False)
        self.widget.setFixedWidth(440)

    def _on_key(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.reject()
            return
        mod = 0
        m = e.modifiers()
        if m & Qt.ControlModifier:
            mod |= cm.MOD_CONTROL
        if m & Qt.ShiftModifier:
            mod |= cm.MOD_SHIFT
        if m & Qt.AltModifier:
            mod |= cm.MOD_ALT
        if m & Qt.MetaModifier:
            mod |= cm.MOD_WIN
        if k in self._MODIFIER_KEYS:
            self._mod, self._key = mod, 0
            self.capture.setText(cm.hotkey_label(mod, None))
            self.yesButton.setEnabled(False)
            return
        vk = None
        if Qt.Key_A <= k <= Qt.Key_Z:
            vk = 0x41 + (k - Qt.Key_A)
        elif Qt.Key_0 <= k <= Qt.Key_9:
            vk = 0x30 + (k - Qt.Key_0)
        elif Qt.Key_F1 <= k <= Qt.Key_F12:
            vk = 0x70 + (k - Qt.Key_F1)
        elif k == Qt.Key_Space:
            vk = 0x20
        else:
            vk = _QT_KEY_TO_VK.get(k)      # OEM 键(`、-、= 等,快捷粘贴默认 Alt+`)
        if vk is None:
            self.capture.setText("暂不支持该按键,请用字母 / 数字 / F1-F12 / 空格"
                                 " / ` - = 等符号键")
            self.yesButton.setEnabled(False)
            return
        if mod == 0:
            self.capture.setText("需至少搭配一个修饰键(Ctrl / Shift / Alt / Win)")
            self.yesButton.setEnabled(False)
            return
        self._mod, self._key = mod, vk
        self.capture.setText(cm.hotkey_label(mod, vk))
        self.yesButton.setEnabled(True)

    def validate(self):
        """点「应用」:交给宿主走重注册链路;真正的成败由 hotkey_change 事件回报。"""
        if self._mod and self._key:
            self._on_apply(self._mod, self._key)
            return True
        return False


class ExcludeAppsDialog(MessageBoxBase):
    """按应用排除名单管理:添加(下拉里带历史出现过的来源应用,可直接输
    exe 名) / 移除选中 / 保存。命中名单的程序复制内容不入史——与暂停
    监听同一条隐私控制线,只是按应用长期生效。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        ttl = StrongBodyLabel("按应用排除")
        self.viewLayout.addWidget(ttl)
        sub = CaptionLabel("名单内程序(如密码管理器、验证器)的复制不进入历史。"
                           "名称为进程 exe 文件名,不区分大小写;下拉列出最近复制"
                           "过的来源应用,选中即可一键排除。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        row = QHBoxLayout()
        self.cmb = EditableComboBox()
        seen, sug = set(), []
        for h in app.history:                      # 历史出现过的来源应用(最近在前)
            s = h.get("src")
            if s and s not in seen:
                seen.add(s)
                sug.append(s)
        for s in (app.cfg.get("exclude_apps") or []):
            if s not in seen:
                seen.add(s)
                sug.append(s)
        self.cmb.addItems(sug[:40])
        self.cmb.setMinimumWidth(240)
        b_add = PushButton("添加", self)
        b_add.setIcon(FIF.ADD.icon())
        b_add.clicked.connect(self._add_current)
        row.addWidget(self.cmb, 1)
        row.addWidget(b_add)
        self.viewLayout.addLayout(row)

        self.lst = QListWidget()
        self.lst.setFont(font(12))
        self.lst.setSelectionMode(QListWidget.MultiSelection)
        for s in (app.cfg.get("exclude_apps") or []):
            self.lst.addItem(s)
        self.viewLayout.addWidget(self.lst)

        b_rm = PushButton("移除选中", self)
        b_rm.setIcon(FIF.DELETE.icon())
        b_rm.clicked.connect(self._remove_selected)
        self.viewLayout.addWidget(b_rm, 0, Qt.AlignLeft)

        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(440)

    def _add_current(self):
        t = os.path.basename(self.cmb.currentText().strip()).lower()
        if not t:
            return
        for i in range(self.lst.count()):          # 去重
            if self.lst.item(i).text() == t:
                return
        self.lst.addItem(t)
        self.cmb.setText("")                    # 加完清空输入,便于连续添加
                          # (qfluentwidgets EditableComboBox 无 lineEdit(),任意文本走 setText)

    def _remove_selected(self):
        for it in self.lst.selectedItems():
            self.lst.takeItem(self.lst.row(it))

    def validate(self):
        """点「保存」:整表交给核心归一化落盘(见 core.set_exclude_apps)。"""
        names = [self.lst.item(i).text() for i in range(self.lst.count())]
        self.app.core.set_exclude_apps(names)
        return True


class SettingsPage(QWidget):
    _health_done = Signal(object)     # 健康检查后台线程 → UI 线程弹结果

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("settingsPage")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 24, 36, 16)
        outer.setSpacing(16)
        # Atelier Zero: 设置页像版权页 / 索引
        top = QHBoxLayout()
        ttl = QLabel("INDEX")
        ttl.setFont(head_font(20, QFont.Weight.Bold))
        ttl.setStyleSheet("color: %s; background: transparent;" % INK)
        sub = QLabel("COLOPHON")
        sub.setFont(label_font(10))
        sub.setStyleSheet("color: %s; background: transparent;" % INK_LIGHT)
        top.addWidget(ttl)
        top.addStretch(1)
        top.addWidget(sub)
        outer.addLayout(top)
        # 1px 分隔线
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: %s;" % INK)
        outer.addWidget(divider)
        # 卡片组放进滚动容器
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(20)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        cfg = app.cfg
        self._health_done.connect(self._show_health_result)

        group = SettingCardGroup("剪贴板", self)
        c_pause = SwitchSettingCard(
            FIF.MUTE, "暂停监听", "暂停期间复制的内容不进入历史(临时隐私模式;"
            "托盘菜单同款开关;重启后自动恢复监听,不落盘)",
            parent=group)
        c_pause.setChecked(bool(getattr(app.core, "monitor_paused", False)))
        self.c_pause = c_pause

        def on_pause(on):
            app.toggle_monitor_pause(bool(on))
        c_pause.checkedChanged.connect(on_pause)

        c_paste = SwitchSettingCard(
            FIF.PASTE, "自动粘贴", "选中条目回车后自动向当前窗口发送 Ctrl+V;"
            "关闭则只写入剪贴板、由你手动粘贴(默认关,更安全)",
            parent=group)
        c_paste.setChecked(bool(cfg.get("auto_paste", False)))

        def on_paste(on):
            cfg["auto_paste"] = bool(on)
            cm.save_config(cfg)
        c_paste.checkedChanged.connect(on_paste)

        c_guard = SwitchSettingCard(
            FIF.IOT, "敏感内容隔离", "密码/银行卡/身份证/密钥自动进私密区,不进入普通历史",
            parent=group)
        c_guard.setChecked(bool(cfg.get("sensitive_guard", True)))

        def on_guard(on):
            cfg["sensitive_guard"] = bool(on)
            cm.save_config(cfg)
        c_guard.checkedChanged.connect(on_guard)

        # 按应用排除:命中名单的程序(如密码管理器)复制不入史
        c_excl = SettingCard(
            FIF.FILTER, "按应用排除",
            "名单内程序的复制不进入历史(如密码管理器/验证器);点「管理」增删名单",
            parent=group)
        self.lbl_excl = CaptionLabel("")
        self.lbl_excl.setStyleSheet("color: %s; font-weight: 600;" % ACCENT)
        b_excl = PushButton("管理", self)
        b_excl.clicked.connect(self._edit_exclusions)
        c_excl.hBoxLayout.addWidget(self.lbl_excl, 0, Qt.AlignRight)
        c_excl.hBoxLayout.addSpacing(8)
        c_excl.hBoxLayout.addWidget(b_excl, 0, Qt.AlignRight)
        c_excl.hBoxLayout.addSpacing(16)
        self._refresh_excl_label()

        c_max = SettingCard(FIF.TAG, "历史条数上限",
                            "超出后从最旧开始淘汰(置顶条目不计入)", parent=group)
        spin = SpinBox()
        spin.setRange(20, 1000)
        spin.setValue(int(cfg.get("max_items", 100)))
        spin.setMinimumWidth(120)

        def on_max(v):
            cfg["max_items"] = int(v)
            cm.save_config(cfg)
            app.core.max_items = int(v)
        spin.valueChanged.connect(on_max)
        c_max.hBoxLayout.addWidget(spin, 0, Qt.AlignRight)
        c_max.hBoxLayout.addSpacing(16)

        group.addSettingCard(c_pause)
        group.addSettingCard(c_paste)
        group.addSettingCard(c_guard)
        group.addSettingCard(c_excl)
        group.addSettingCard(c_max)
        lay.addWidget(group)

        # ---- 存储与清理 ----
        group3 = SettingCardGroup("存储与清理", self)

        def _combo_index(combo, values, cur):
            for i, v in enumerate(values):
                if v == cur:
                    combo.setCurrentIndex(i)
                    break

        c_days = SettingCard(FIF.BROOM, "自动清理历史",
                             "超期未置顶条目自动删除(含图片文件);启动与每天检查一次",
                             parent=group3)
        combo_days = ComboBox()
        _DAYS_ITEMS = ("关闭", "7 天", "30 天", "90 天")
        _DAYS_VALS = (0, 7, 30, 90)
        combo_days.addItems(list(_DAYS_ITEMS))
        combo_days.setMinimumWidth(110)
        _combo_index(combo_days, _DAYS_VALS, int(cfg.get("cleanup_days", 0) or 0))

        def on_days(i):
            cfg["cleanup_days"] = _DAYS_VALS[i]
            cm.save_config(cfg)
            self._run_cleanup()
        combo_days.currentIndexChanged.connect(on_days)
        self.combo_days = combo_days
        c_days.hBoxLayout.addWidget(combo_days, 0, Qt.AlignRight)
        c_days.hBoxLayout.addSpacing(16)

        c_disk = SettingCard(FIF.SAVE, "图片缓存上限",
                             "截图缓存超过上限时从最旧开始删除(置顶条目豁免)",
                             parent=group3)
        combo_disk = ComboBox()
        _DISK_ITEMS = ("关闭", "200 MB", "500 MB", "1 GB", "2 GB")
        _DISK_VALS = (0, 200, 500, 1024, 2048)
        combo_disk.addItems(list(_DISK_ITEMS))
        combo_disk.setMinimumWidth(110)
        _combo_index(combo_disk, _DISK_VALS, int(cfg.get("disk_cap_mb", 0) or 0))

        def on_disk(i):
            cfg["disk_cap_mb"] = _DISK_VALS[i]
            cm.save_config(cfg)
            self._run_cleanup()
        combo_disk.currentIndexChanged.connect(on_disk)
        self.combo_disk = combo_disk
        c_disk.hBoxLayout.addWidget(combo_disk, 0, Qt.AlignRight)
        c_disk.hBoxLayout.addSpacing(16)

        c_enc = SwitchSettingCard(
            FIF.CERTIFICATE, "历史加密存储",
            "历史正文与元数据用 Windows DPAPI 加密，并移除旧明文库；"
            "图片文件不加密。保护静态磁盘数据，不抵御当前用户权限的"
            "恶意程序；关闭则使用 SQLite 明文存储", parent=group3)
        c_enc.setChecked(bool(cfg.get("encrypt_history")))

        def on_enc(on):
            try:
                app.core.set_history_encryption(bool(on))
            except Exception as exc:
                c_enc.blockSignals(True)
                c_enc.setChecked(bool(cfg.get("encrypt_history")))
                c_enc.blockSignals(False)
                InfoBar.error("存储转换未完成", str(exc), parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=8000)
                return
            svc = getattr(app, "_ctxpack_svc", None)
            if svc is not None:            # 服务已在用:随开关对齐(文件已由核心层迁移)
                svc.set_encrypt(bool(on))
            InfoBar.success("历史加密已" + ("开启" if on else "关闭"),
                            "存储已转换为" + ("加密 JSON" if on else "SQLite 库")
                            + "格式", parent=self, position=InfoBarPosition.TOP_RIGHT)
        c_enc.checkedChanged.connect(on_enc)
        self.c_enc = c_enc
        if not cm.dpapi_available():       # DPAPI 缺位时不开空头支票(与私密区同原则)
            c_enc.setEnabled(False)
            c_enc.setContent("当前环境 DPAPI 不可用,历史加密无法开启")

        group3.addSettingCard(c_days)
        group3.addSettingCard(c_disk)
        group3.addSettingCard(c_enc)
        c_wipe = PushSettingCard(
            "清空…", FIF.DELETE, "清空全部数据",
            "历史(含置顶)/图片文件/历史备份全部清空,不可撤销;"
            "私密区与设置可选是否一并清空",
            parent=group3)
        c_wipe.clicked.connect(self._wipe_all_data)
        group3.addSettingCard(c_wipe)
        lay.addWidget(group3)

        # ---- 备份与迁移 ----
        group4 = SettingCardGroup("备份与迁移", self)
        c_exp = PushSettingCard(
            "导出…", FIF.SAVE_AS, "导出历史(明文 JSON)",
            "全部条目导出为 JSON(可选内嵌图片);不含私密区;文件本身不加密",
            parent=group4)
        c_exp.clicked.connect(self._export_history)
        c_pexp = PushSettingCard(
            "加密备份…", FIF.CERTIFICATE, "加密备份(可迁移)",
            "密码 + AES-256-GCM 认证加密,换电脑凭密码即可恢复;"
            "私密区可选包含(默认不含,且只存在于加密容器内)",
            parent=group4)
        c_pexp.clicked.connect(self._export_portable)
        c_imp = PushSettingCard(
            "导入…", FIF.DOWNLOAD, "导入历史 / 恢复备份",
            "JSON 与加密备份(.wcbak)自动识别,合并去重、图片落盘重建、"
            "原时间/置顶还原",
            parent=group4)
        c_imp.clicked.connect(self._import_history)
        group4.addSettingCard(c_exp)
        group4.addSettingCard(c_pexp)
        group4.addSettingCard(c_imp)
        lay.addWidget(group4)

        group2 = SettingCardGroup("外观与系统", self)
        c_theme = SettingCard(FIF.BRUSH, "界面主题",
                              "跟随系统或固定浅色/深色(更改后立即生效)", parent=group2)
        combo = ComboBox()
        for _tl, _tv in (("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")):
            combo.addItem(_tl, userData=_tv)
        combo.setCurrentIndex(max(0, combo.findData(str(cfg.get("theme", "auto")))))
        combo.setMinimumWidth(120)
        combo.setAccessibleName("界面主题选择")
        self.combo_theme = combo

        def on_theme(t):
            cfg["theme"] = t
            cm.save_config(cfg)
            setTheme(Theme.DARK if t == "dark" else
                     Theme.LIGHT if t == "light" else Theme.AUTO)
            init_palette(isDarkTheme())
            cm.log("主题切换为 %s(部分页面元素需重开窗口刷新)" % t)
        combo.currentIndexChanged.connect(
            lambda i: on_theme(combo.itemData(i)))
        c_theme.hBoxLayout.addWidget(combo, 0, Qt.AlignRight)
        c_theme.hBoxLayout.addSpacing(16)

        # 主题色:色板即时应用(选中态/徽章/强调色全部派生自此)
        c_accent = SettingCard(FIF.PALETTE, "主题色",
                               "强调色与选中态跟随;点击色块立即生效,部分元素需重开窗口刷新",
                               parent=group2)
        sw_box = QWidget()
        sw_lay = QHBoxLayout(sw_box)
        sw_lay.setContentsMargins(0, 0, 0, 0)
        sw_lay.setSpacing(6)
        _ACCENTS = ("#0078D4", "#00B7C3", "#107C10", "#CA5010",
                    "#8764B8", "#E3008C", "#C42B1C")
        _cur = str(cfg.get("theme_color", "") or "").upper()
        _sw_btns = []

        def _restyle_swatches():
            for b, hx in zip(_sw_btns, _ACCENTS):
                sel = hx.upper() == str(cfg.get("theme_color", "") or "").upper()
                b.setStyleSheet(
                    "QPushButton { background: %s; border: 2px solid %s;"
                    " border-radius: 10px; }"
                    % (hx, TEXT if sel else "transparent"))

        def _on_accent(hx):
            try:
                setThemeColor(QColor(hx))
            except Exception:
                return
            cfg["theme_color"] = hx
            cm.save_config(cfg)
            init_palette(isDarkTheme())
            _restyle_swatches()
            InfoBar.success("主题色已应用", "部分页面元素需重开窗口刷新", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)

        for _hx in _ACCENTS:
            _b = QPushButton()
            _b.setFixedSize(20, 20)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setToolTip(_hx)
            _b.clicked.connect(lambda _=False, h=_hx: _on_accent(h))
            sw_lay.addWidget(_b)
            _sw_btns.append(_b)
        _restyle_swatches()
        c_accent.hBoxLayout.addWidget(sw_box, 0, Qt.AlignRight)
        c_accent.hBoxLayout.addSpacing(16)

        c_mica = SwitchSettingCard(
            FIF.BRUSH, "Mica 材质", "Win11 云母背景(本地桌面);远程会话自动禁用;更改需重开窗口",
            parent=group2)
        c_mica.setChecked(bool(cfg.get("mica", True)))

        def on_mica(on):
            cfg["mica"] = bool(on)
            cm.save_config(cfg)
        c_mica.checkedChanged.connect(on_mica)

        c_auto = SwitchSettingCard(
            FIF.UP, "开机自启", "登录 Windows 后自动启动 WinClipboard", parent=group2)
        c_auto.setChecked(app.core.is_autostart())

        def on_auto(on):
            if on != app.core.is_autostart():
                app.core.toggle_autostart()
            c_auto.setChecked(app.core.is_autostart())
        c_auto.checkedChanged.connect(on_auto)

        c_key = SettingCard(
            FIF.CUT, "全局热键",
            "显示/隐藏主窗口;点「更改」直接按键录制,被占用会即时提示并保留原热键",
            parent=group2)
        self.lbl_hotkey = CaptionLabel(cm.hotkey_label(
            cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
            cfg.get("hotkey_key", 0x56)))
        self.lbl_hotkey.setStyleSheet("color: %s; font-weight: 600;" % ACCENT)
        b_key = PushButton("更改", self)
        b_key.clicked.connect(lambda: HotkeyRecorderDialog(
            self, self.lbl_hotkey.text(), app.apply_hotkey).exec())
        c_key.hBoxLayout.addWidget(self.lbl_hotkey, 0, Qt.AlignRight)
        c_key.hBoxLayout.addSpacing(8)
        c_key.hBoxLayout.addWidget(b_key, 0, Qt.AlignRight)
        c_key.hBoxLayout.addSpacing(16)

        # 快捷粘贴浮层热键(默认 Alt+`):Win+V 式轻入口
        c_qkey = SettingCard(
            FIF.PASTE, "快捷粘贴浮层",
            "弹出超轻量剪贴列表(Win+V 式):搜索 + ↑↓ + Enter 直接粘贴;"
            "点「更改」按键录制,被占用即时提示并保留原热键",
            parent=group2)
        self.lbl_quick = CaptionLabel(cm.hotkey_label(
            cfg.get("quick_mod", cm.MOD_ALT), cfg.get("quick_key", 0xC0)))
        self.lbl_quick.setStyleSheet("color: %s; font-weight: 600;" % ACCENT)
        b_qkey = PushButton("更改", self)
        b_qkey.clicked.connect(lambda: HotkeyRecorderDialog(
            self, self.lbl_quick.text(), app.apply_quick_hotkey).exec())
        c_qkey.hBoxLayout.addWidget(self.lbl_quick, 0, Qt.AlignRight)
        c_qkey.hBoxLayout.addSpacing(8)
        c_qkey.hBoxLayout.addWidget(b_qkey, 0, Qt.AlignRight)
        c_qkey.hBoxLayout.addSpacing(16)

        group2.addSettingCard(c_theme)
        group2.addSettingCard(c_accent)
        group2.addSettingCard(c_mica)
        group2.addSettingCard(c_auto)
        group2.addSettingCard(c_key)
        group2.addSettingCard(c_qkey)
        c_ai = SettingCard(FIF.ROBOT, "快速 AI 轮盘",
                           "复制文字后唤出轮盘，选择解释、翻译、总结或润色，结果在浮窗显示",
                           parent=group2)
        self.lbl_ai_hotkey = CaptionLabel(cm.hotkey_label(
            cfg.get("ai_hotkey_mod", cm.MOD_CONTROL | cm.MOD_ALT), cfg.get("ai_hotkey_key", 0x20)))
        b_ai_change = PushButton("更改", self)
        b_ai_change.clicked.connect(lambda: HotkeyRecorderDialog(
            self, self.lbl_ai_hotkey.text(), app.apply_ai_hotkey).exec())
        b_ai_open = PushButton("打开", self)
        b_ai_open.clicked.connect(lambda: app.open_quick_ai())
        for widget in (self.lbl_ai_hotkey, b_ai_change, b_ai_open):
            c_ai.hBoxLayout.addWidget(widget, 0, Qt.AlignRight)
            c_ai.hBoxLayout.addSpacing(8)
        group2.addSettingCard(c_ai)
        c_help = PushSettingCard(
            "查看", FIF.HELP, "快捷键与操作",
            "全局热键 / 列表 / 查看器 / 图片编辑的按键总览(主窗口按 F1 也可打开)",
            parent=group2)
        c_help.clicked.connect(app.show_shortcut_help)
        group2.addSettingCard(c_help)
        lay.addWidget(group2)

        # ---- 关于与更新 ----
        groupDiag = SettingCardGroup("诊断", self)
        c_diag = PushSettingCard(
            "复制", FIF.COPY, "诊断信息",
            "版本 / 运行环境 / 条目与存储占用 / 监听状态;"
            "仅元数据(不含任何内容与密钥),可放心粘贴给问题反馈",
            parent=groupDiag)
        c_diag.clicked.connect(self._copy_diagnostics)
        c_health = PushSettingCard(
            "检查", FIF.HEART, "数据健康检查",
            "数据库完整性 / 引用 blob 存在性 / 孤儿文件 / 私密区可解密 / 备份可读",
            parent=groupDiag)
        c_health.clicked.connect(self._run_health_check)
        c_fts = PushSettingCard(
            "重建", FIF.UPDATE, "重建搜索索引",
            "全文索引整体重建(搜索异常时的修复动作,不影响数据)",
            parent=groupDiag)
        c_fts.clicked.connect(self._rebuild_fts)
        c_orph = PushSettingCard(
            "清理", FIF.DELETE, "清理孤立缓存",
            "删除引用计数为零的图片 blob(与启动期自动清理同一套规则,"
            "带新文件时间窗防护)", parent=groupDiag)
        c_orph.clicked.connect(self._clean_orphan_caches)
        c_logs = PushSettingCard(
            "打开", FIF.FOLDER, "打开数据目录",
            "app.log(运行日志)与 logs/perf.log(性能埋点)所在目录",
            parent=groupDiag)
        c_logs.clicked.connect(lambda: cm.reveal_in_explorer(
            os.path.dirname(cm.LOG_FILE)))
        for _c in (c_diag, c_health, c_fts, c_orph, c_logs):
            groupDiag.addSettingCard(_c)
        lay.addWidget(groupDiag)

        group5 = SettingCardGroup("关于与更新", self)
        c_ver = SettingCard(
            FIF.INFO, "当前版本",
            "版本 %s(检查 GitHub 上的新版本发布,发现新版可一键打开发布页)"
            % cm.APP_VERSION, parent=group5)
        b_upd = PushButton("检查更新", self)
        b_upd.clicked.connect(lambda: app.check_update())
        c_ver.hBoxLayout.addWidget(b_upd, 0, Qt.AlignRight)
        c_ver.hBoxLayout.addSpacing(16)
        c_repo = PushSettingCard(
            "打开", FIF.LINK, "项目主页",
            "%s — 源码 / 文档 / 问题反馈" % cm.REPO_URL, parent=group5)
        c_repo.clicked.connect(lambda: cm.open_url(cm.REPO_URL))
        group5.addSettingCard(c_ver)
        group5.addSettingCard(c_repo)
        lay.addWidget(group5)
        lay.addStretch(1)

    def update_hotkey_label(self):
        """热键变更成功后由应用层(hotkey_change 事件)回写当前组合显示。"""
        self.lbl_hotkey.setText(cm.hotkey_label(
            self.app.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
            self.app.cfg.get("hotkey_key", 0x56)))

    def update_ai_hotkey_label(self):
        label = cm.hotkey_label(self.app.cfg.get("ai_hotkey_mod", cm.MOD_CONTROL | cm.MOD_ALT),
                                self.app.cfg.get("ai_hotkey_key", 0x20))
        if not getattr(self.app, "_ai_hotkey_ok", True):
            label += "（被占用）"
        self.lbl_ai_hotkey.setText(label)

    def update_quick_label(self):
        """快捷粘贴浮层热键变更后(qp_hotkey_change/启动注册结果)回写显示。"""
        self.lbl_quick.setText(cm.hotkey_label(
            self.app.cfg.get("quick_mod", cm.MOD_ALT),
            self.app.cfg.get("quick_key", 0xC0)))

    def sync_monitor_pause(self):
        """托盘菜单切换暂停监听后,设置页开关同步显示(值未变不发信号,无回环)。"""
        self.c_pause.setChecked(bool(getattr(self.app.core, "monitor_paused", False)))

    def _refresh_excl_label(self):
        n = len(self.app.cfg.get("exclude_apps") or [])
        self.lbl_excl.setText("已排除 %d 个应用" % n if n else "未排除任何应用")

    def _run_cleanup(self):
        """清理策略变更后立即执行一次(核心会按新配置裁剪并置脏落盘)。"""
        n, freed = self.app.core.apply_cleanup()
        if n:
            self.app.refresh_view()
            InfoBar.success("自动清理", "删除 %d 条,释放 %s"
                            % (n, cm._fmt_size(freed)),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)

    # ---- 诊断(建议 19/29;数据层见 diagnostics.py) ----
    def _wipe_all_data(self):
        """清空全部数据:两步确认(明细+可选范围 → 输入「清空」)后执行。"""
        core = self.app.core
        n_items = len(core.history) + core.store.count() \
            if core.store is not None else len(core.history)
        n_priv = len(core.private)
        _bf, blob_bytes = diag._dir_stats(cm.BLOB_DIR)
        _if_, img_bytes = diag._dir_stats(cm.IMAGE_DIR)

        dlg = MessageBoxBase(self)
        ttl = StrongBodyLabel("清空全部数据")
        dlg.viewLayout.addWidget(ttl)
        body = CaptionLabel(
            "将永久删除(不可撤销,撤销栈同时作废):\n"
            "· 普通历史 %d 条(含置顶,含已删除待回收)\n"
            "· 全部图片文件(约 %s)\n"
            "· 历史备份(.bak1~3)\n"
            "· 上下文包(全部包与条目,含备份)\n\n"
            "不会动:日志与诊断记录、图钉画布、工作流运行记录。"
            % (n_items, cm._fmt_size(blob_bytes + img_bytes)))
        body.setWordWrap(True)
        dlg.viewLayout.addWidget(body)
        chk_priv = CheckBox("同时清空私密区(%d 条,含敏感内容;备份与无法读取"
                            "的原文件一并删除)%s"
                            % (n_priv,
                               "【当前私密区文件不可读,勾选将丢弃其中内容】"
                               if getattr(core, "_private_load", None) is not None
                               and core._private_load.status == "unreadable"
                               else ""))
        chk_cfg = CheckBox("同时重置全部设置(AI 服务密钥 / 工作流 / 主题等)")
        dlg.viewLayout.addWidget(chk_priv)
        dlg.viewLayout.addWidget(chk_cfg)
        dlg.yesButton.setText("下一步…")
        dlg.cancelButton.setText("取消")
        dlg.widget.setFixedWidth(460)
        if not dlg.exec():
            return

        # 第二步:输入「清空」才算数——高危动作的物理确认
        dlg2 = MessageBoxBase(self)
        ttl2 = StrongBodyLabel("最终确认")
        dlg2.viewLayout.addWidget(ttl2)
        tip = CaptionLabel("此操作不可撤销、无法恢复。请输入「清空」两字继续:")
        dlg2.viewLayout.addWidget(tip)
        edit = LineEdit()
        edit.setPlaceholderText("清空")
        dlg2.viewLayout.addWidget(edit)
        dlg2.yesButton.setText("执行清空")
        dlg2.yesButton.setEnabled(False)
        dlg2.cancelButton.setText("取消")
        dlg2.widget.setFixedWidth(400)

        def _gate(text):
            dlg2.yesButton.setEnabled(text.strip() == "清空")

        edit.textChanged.connect(_gate)
        if not dlg2.exec():
            return

        done, ok = core.wipe_all_data(wipe_private=chk_priv.isChecked(),
                                      reset_config=chk_cfg.isChecked())
        # 前端联动:缩略图缓存/列表/私密区/来源下拉全部重建
        self.app.reset_thumbs()
        self.app.refresh_view()
        for page in (getattr(self.app.win, "private_page", None),):
            if page is not None and hasattr(page, "refresh"):
                page.refresh()
        if ok:
            InfoBar.success("已清空全部数据",
                            "完成:%s%s" % ("; ".join(done),
                                           ";建议重启应用使界面完全复位"
                                           if chk_cfg.isChecked() else ""),
                            parent=self, position=InfoBarPosition.TOP_RIGHT,
                            duration=8000)
        else:
            InfoBar.error("清空中途失败",
                          "已完成:%s;详情见日志" % "; ".join(done),
                          parent=self, position=InfoBarPosition.TOP_RIGHT,
                          duration=-1)

    def _copy_diagnostics(self):
        """复制诊断信息:轻量快照,仅元数据(不含内容与密钥)。"""
        try:
            text = diag.format_diagnostics(
                diag.collect_diagnostics(self.app.core))
            if not self.app.core.copy_text(text):
                InfoBar.error("复制失败", "剪贴板写入失败,请重试", parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=5000)
                return
            InfoBar.success("诊断信息已复制",
                            "共 %d 行,可直接粘贴给问题反馈"
                            % len(text.splitlines()),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        except Exception:
            cm.log("复制诊断信息失败:\n%s" % traceback.format_exc())
            InfoBar.error("复制失败", "详情见 app.log", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)

    def _run_health_check(self):
        """数据健康检查(blob 目录扫描在后台线程,完成后回 UI 线程)。"""
        core = self.app.core

        def _work():
            try:
                res = diag.run_health_check(core)
            except Exception:
                cm.log("健康检查异常:\n%s" % traceback.format_exc())
                res = {"ok": False, "results": [
                    {"name": "检查异常", "ok": False,
                     "detail": traceback.format_exc(limit=3)}]}
            self._health_done.emit(res)

        threading.Thread(target=_work, daemon=True,
                         name="health-check").start()

    def _show_health_result(self, res):
        """健康检查结果:InfoBar 概要 + MessageBox 明细。"""
        results = res.get("results") or []
        lines = []
        for r in results:
            mark = "✓" if r["ok"] is True else \
                ("✗" if r["ok"] is False else "—")
            extra = ("  %s" % r["detail"]) if r.get("detail") else ""
            lines.append("%s %s%s" % (mark, r["name"], extra))
        bad = sum(1 for r in results if r["ok"] is False)
        if res.get("ok"):
            InfoBar.success("数据健康检查", "全部通过(共 %d 项)" % len(results),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.warning("数据健康检查", "发现 %d 项问题,详见详情" % bad,
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
        MessageBox("数据健康检查", "\n".join(lines) or "无结果", self).exec()

    def _rebuild_fts(self):
        """重建全文索引(JSON 模式无索引,提示后返回)。"""
        if self.app.core.store is None:
            InfoBar.warning("重建搜索索引", "JSON 模式无全文索引", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        try:
            self.app.core.store.rebuild_fts()
            InfoBar.success("重建搜索索引", "全文索引已整体重建", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
        except Exception:
            cm.log("重建搜索索引失败:\n%s" % traceback.format_exc())
            InfoBar.error("重建搜索索引", "失败,详情见 app.log", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)

    def _clean_orphan_caches(self):
        """手动触发一次孤儿清理(与启动期 +3s 的自动清理同一入口/规则)。"""
        removed = self.app.core.cleanup_orphan_images()
        InfoBar.success("清理孤立缓存",
                        "删除 %d 个引用计数为零的文件" % removed if removed
                        else "无孤立文件",
                        parent=self, position=InfoBarPosition.TOP_RIGHT)
        if removed:
            self.app.refresh_view()

    # ---- 备份与迁移 ----
    def _export_history(self):
        """导出历史:先选是否内嵌图片,再选保存路径。"""
        dlg = MessageBoxBase(self)
        ttl = StrongBodyLabel("导出历史")
        dlg.viewLayout.addWidget(ttl)
        sub = CaptionLabel("文本/文件条目始终导出(含富文本与来源应用)。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        dlg.viewLayout.addWidget(sub)
        chk_img = CheckBox("内嵌图片(base64,文件明显变大)")
        dlg.viewLayout.addWidget(chk_img)
        dlg.yesButton.setText("选择保存位置…")
        dlg.cancelButton.setText("取消")
        dlg.widget.setFixedWidth(400)
        if not dlg.exec():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史",
            os.path.join(os.path.expanduser("~"), "Desktop",
                         "winclipboard_export.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            payload = self.app.core.export_payload(bool(chk_img.isChecked()))
            # indent=2:导出文件给人看/给人diff,不再是挤成一行的机器格式;
            # 导入走 json.load,缩进不影响还原
            Path(path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            cm.log("导出历史失败:\n%s" % traceback.format_exc())
            InfoBar.error("导出失败", "详情见日志(%s)" % cm.LOG_FILE, parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        InfoBar.success("导出完成",
                        "%d 条 → %s" % (len(payload["items"]), path),
                        parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _export_portable(self):
        """可迁移加密备份:密码 → PBKDF2 → AES-256-GCM,跨电脑可恢复。
        私密区默认不包含(勾选后只存在于加密容器内)。"""
        if not cm._AES_OK:
            InfoBar.error("功能不可用",
                          "未安装 cryptography 库(pip install cryptography)",
                          parent=self, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
            return
        dlg = MessageBoxBase(self)
        dlg.viewLayout.addWidget(StrongBodyLabel("导出加密备份(可迁移)"))
        sub = CaptionLabel("密码经 PBKDF2 派生密钥,AES-256-GCM 认证加密;"
                           "拿到任何电脑上用同一密码即可恢复。密码丢失无法找回。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        dlg.viewLayout.addWidget(sub)
        pw1 = PasswordLineEdit()
        pw1.setPlaceholderText("设置备份密码")
        pw2 = PasswordLineEdit()
        pw2.setPlaceholderText("再输入一次")
        chk_img = CheckBox("内嵌图片(base64,文件明显变大)")
        chk_priv = CheckBox("包含私密区(仅存在于加密容器内)")
        for w in (pw1, pw2, chk_img, chk_priv):
            dlg.viewLayout.addWidget(w)
        dlg.yesButton.setText("选择保存位置…")
        dlg.cancelButton.setText("取消")
        dlg.widget.setFixedWidth(420)

        def _valid():
            ok = (len(pw1.text()) >= 4 and pw1.text() == pw2.text())
            if not ok and dlg.yesButton.isEnabled():
                InfoBar.warning("密码无效", "至少 4 位,且两次输入一致",
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
            return ok

        dlg.yesButton.setEnabled(True)
        if not dlg.exec() or not _valid():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出加密备份",
            os.path.join(os.path.expanduser("~"), "Desktop",
                         "clipboard-backup-%s.wcbak"
                         % time.strftime("%Y%m%d")),
            "WinClipboard 加密备份 (*.wcbak)")
        if not path:
            return
        try:
            payload = self.app.core.export_portable_payload(
                bool(chk_img.isChecked()), bool(chk_priv.isChecked()))
            Path(path).write_bytes(
                cm.portable_backup_bytes(payload, pw1.text()))
        except Exception:
            cm.log("导出加密备份失败:\n%s" % traceback.format_exc())
            InfoBar.error("导出失败", "详情见日志(%s)" % cm.LOG_FILE, parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        InfoBar.success("加密备份完成",
                        "%d 条(+%d 私密)→ %s"
                        % (len(payload["items"]), len(payload["private"]), path),
                        parent=self, position=InfoBarPosition.TOP_RIGHT)

    def _import_history(self):
        """导入历史:合并去重,图片落盘重建,时间/置顶还原。
        加密备份(.wcbak)自动识别并要求输入密码。"""
        path, _ = QFileDialog.getOpenFileName(self, "导入历史/备份", "",
                                              "WinClipboard 备份 (*.wcbak *.json);;"
                                              "所有文件 (*.*)")
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            if raw[:8] == b"WCLBBAK1":
                payload = self._ask_portable_password(raw)
                if payload is None:
                    return
                added, skipped, n_priv = \
                    self.app.core.import_portable_payload(payload)
            else:
                payload = json.loads(raw.decode("utf-8"))
                added, skipped = self.app.core.import_payload(payload)
                n_priv = 0
        except Exception as exc:
            cm.log("导入历史失败:\n%s" % traceback.format_exc())
            InfoBar.error("导入失败", str(exc)[:160], parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        self.app.refresh_view()
        msg = "新增 %d 条,跳过 %d 条(重复或文件已不存在)" % (added, skipped)
        if n_priv:
            msg += ";私密区新增 %d 条" % n_priv
        InfoBar.success("导入完成", msg, parent=self,
                        position=InfoBarPosition.TOP_RIGHT)

    def _ask_portable_password(self, raw):
        """解密对话框:密码错误可重试,取消返回 None。"""
        for _ in range(3):
            dlg = MessageBoxBase(self)
            dlg.viewLayout.addWidget(StrongBodyLabel("输入备份密码"))
            sub = CaptionLabel("该备份使用 AES-256-GCM 认证加密,"
                               "密码错误会明确提示。")
            sub.setStyleSheet("color: %s;" % TER)
            dlg.viewLayout.addWidget(sub)
            pw = PasswordLineEdit()
            pw.setPlaceholderText("备份密码")
            dlg.viewLayout.addWidget(pw)
            dlg.yesButton.setText("解密")
            dlg.cancelButton.setText("取消")
            dlg.widget.setFixedWidth(400)
            if not dlg.exec():
                return None
            try:
                return cm.portable_backup_parse(raw, pw.text())
            except ValueError as e:
                InfoBar.error("解密失败", str(e)[:120], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=4000)
        return None

    def _edit_exclusions(self):
        ExcludeAppsDialog(self, self.app).exec()
        self._refresh_excl_label()


# ============================================================
# AI 工作流(多模型协作 · 人在回路)
# ============================================================
class UiHealthMonitor:
    """UI 事件循环健康监控(Mica 假死自愈)。

    个别远程会话(网易UU远程等未置 RDP 标志的工具)上 Mica 会让 Qt 事件
    循环假死——窗口在、定时器与消息全部不跑。布防后事件循环在 GRACE_S
    内打卡一次即视为健康(打卡是循环定时器,启动高峰忙过头也能随后补打,
    只有派发器真死了才会整窗超时);超时才无 Mica 自重启:仅本次会话、
    不改配置(本地桌面下次照常启用),WCLB_NO_MICA 环境变量防重启循环。

    历史坑:早期版本是一次性 2500ms 心跳 + 6s 判决,启动预热变重后心跳
    被排队到判决之后,健康实例每次冷启动都被误杀再无 Mica 重启一遍。

    触发时把判决时长 / 远程会话 / Mica 状态写入日志——这个 workaround
    到底多久救一次场有据可查,不会变成「不敢删但不知道有没有用」的遗物。
    """

    HEARTBEAT_MS = 2500          # 心跳循环定时器周期
    GRACE_S = 30.0               # 宽限判决时限:整窗无任何心跳才判死

    def __init__(self, app):
        self.app = app
        self.beat = None          # None=未打卡;打卡时刻(monotonic)
        self._armed = False
        self._beat_timer = None

    def arm(self):
        """布防(初始化尾部调用一次,幂等);仅 Mica 生效且未设禁用环境变量时。"""
        if self._armed:
            return
        self._armed = True
        if not getattr(self.app, "_mica_active", False) \
                or os.environ.get("WCLB_NO_MICA"):
            return
        t0 = time.monotonic()
        self._beat_timer = QTimer(self.app)
        self._beat_timer.setInterval(self.HEARTBEAT_MS)
        self._beat_timer.timeout.connect(self._heartbeat)
        self._beat_timer.start()
        threading.Thread(target=self._watch, args=(t0,), daemon=True).start()

    def _heartbeat(self):
        self.beat = time.monotonic()

    def _watch(self, t0):
        deadline = t0 + self.GRACE_S
        while time.monotonic() < deadline:
            if self.beat is not None:
                return                    # 事件循环活着(哪怕打卡晚到)
            time.sleep(0.5)
        cm.log("UI 健康监控:事件循环 %.0fs 无心跳,疑似 Mica 致假死,无 Mica 自重启"
               "(remote_session=%s mica=%s)"
               % (self.GRACE_S, cm.is_remote_session(),
                  bool(getattr(self.app, "_mica_active", False))))
        try:
            subprocess.Popen(
                [sys.executable, os.path.abspath(sys.argv[0])],
                env=dict(os.environ, WCLB_NO_MICA="1"),
                close_fds=False)
        except Exception:
            cm.log("看门狗重启失败:\n%s" % traceback.format_exc())
            return
        os._exit(1)


class ThumbLoader(QObject):
    """后台生成缩略图:QImage 的加载/缩放线程安全,完成后经信号投回 GUI 线程
    转 QPixmap 并回填卡片。截图解码+平滑缩放单张可达几十毫秒,放 UI 线程会在
    启动预热全量截图时把窗口冻住(30 张 ≈ 1.5 秒)。

    队列规则(2026-10 性能优化):
    - 可见请求(卡片/预览绑定)优先于后台预热,同路径合并(_pending);
    - 预热(低优先级)在途有上限(PREWARM_CAP),超出直接丢弃——
      可见请求到来时仍会按高优先级提交,不会丢功能;
    - 解码优先走 QImageReader 的解码期缩放 + EXIF 方向(格式支持时),
      不支持自动回退 QImage 全解码后缩放;
    - 失败路径进有界失败表,不再反复重试解码坏文件;
    - 缓存统一经 app._thumbs_put 写入(LRU + 字节预算),不绕过淘汰;
    - shutdown() 退出前排空队列。
    """

    loaded = Signal(str, QImage)
    PREWARM_CAP = 240              # 低优先级(预热)在途上限
    FAILED_CAP = 200               # 解码失败路径表上限

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._q = queue.PriorityQueue()
        self._pending = set()
        self._failed = set()
        self._prewarm_backlog = 0
        self._seq = 0
        self._stop = False
        self.loaded.connect(self._on_loaded)
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, path, visible=True):
        """提交缩略图请求:visible=True(卡片/预览可见)高优先级;
        预热(visible=False)可被队列上限丢弃。"""
        if not path or self._stop or path in self._failed:
            return
        if path in self._pending or path in self.app._thumbs:
            return                          # 在途合并 / 已有缓存
        if visible:
            self._pending.add(path)
            self._seq += 1
            self._q.put((0, self._seq, path))
        else:
            if self._prewarm_backlog >= self.PREWARM_CAP:
                return                      # 预热过载:丢弃,可见请求不受影响
            self._prewarm_backlog += 1
            self._pending.add(path)
            self._seq += 1
            self._q.put((1, self._seq, path))

    def shutdown(self):
        """退出:拒绝新任务并排空在途队列(daemon 线程随进程收尾)。"""
        self._stop = True
        try:
            while True:
                self._q.get_nowait()
                self._q.task_done()
        except queue.Empty:
            pass

    def _remember_failed(self, path):
        f = self._failed
        if len(f) >= self.FAILED_CAP:
            for k in list(f)[:self.FAILED_CAP // 2]:
                f.discard(k)
        f.add(path)

    def _run(self):
        from PySide6.QtCore import QBuffer
        from PySide6.QtGui import QImageReader
        from pathlib import Path as _P
        while not self._stop:
            try:
                prio, _seq, path = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._stop:
                    break
                try:
                    data = _P(path).read_bytes()   # 一次读入,后台线程
                except Exception:                  # 不长期持有文件句柄
                    data = None                    # (删除/清理不与解码竞争)
                if not data:
                    self._remember_failed(path)
                    continue
                img = None
                try:
                    buf = QBuffer()
                    buf.setData(data)
                    buf.open(QBuffer.ReadOnly)
                    reader = QImageReader()
                    reader.setDevice(buf)
                    reader.setAutoTransform(True)      # EXIF 方向
                    sz = reader.size()
                    if sz.isValid():
                        sz.scale(256, 256, Qt.KeepAspectRatio)
                        reader.setScaledSize(sz)       # 解码期缩放(格式支持时)
                        img = reader.read()
                except Exception:
                    img = None
                if img is None or img.isNull():
                    # 回退:全解码后缩放(老格式/reader 不支持时)
                    try:
                        full = QImage.fromData(data)
                        if not full.isNull():
                            img = full.scaled(256, 256, Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation)
                    except Exception:
                        img = None
                if img is not None and not img.isNull():
                    self.loaded.emit(path, img)
                else:
                    self._remember_failed(path)
            except Exception:
                pass
            finally:
                if prio == 1:
                    self._prewarm_backlog -= 1
                self._pending.discard(path)
                try:
                    self._q.task_done()
                except Exception:
                    pass

    def _on_loaded(self, path, img):
        pm = QPixmap.fromImage(img)
        put = getattr(self.app, "_thumbs_put", None)
        if put is not None:
            put(path, pm)                   # 统一 LRU/字节预算(不绕过淘汰)
        else:
            self.app._thumbs[path] = pm     # 测试替身:直写
        for c in self.app._card_widgets:
            if c.rec.get("type") == "image" and c.rec.get("content") == path:
                c.set_thumb(pm)
        qw = getattr(self.app, "quick", None)      # 快捷粘贴浮层可见则重绘
        if qw is not None:
            try:
                qw.on_thumb(path, pm)
            except Exception:
                pass


from workflow_ui import (ServicesPage, WorkflowPage, WorkflowEditorWindow,  # noqa: F401
                        WorkflowRunner, WorkflowRunInspector,
                        ServiceEditorDialog, ProviderCard, PrivacyGateDialog,
                        AIWorker, ModelWorker)




class UpdateWorker(QThread):
    """后台检查 GitHub 最新 Release(cm.fetch_latest_release 阻塞 8s 上限)。"""
    done = Signal(str, str, str)     # 状态, 版本, 链接/原因

    def run(self):
        self.done.emit(*cm.fetch_latest_release())


# ============================================================
# 动作注册表(Action Registry):卡片右键菜单与查看器专属动作的数据源。
# 新增内容动作 = 在这里登记一条,不再往 card_menu/_inspector_actions
# 里堆 if/elif;supported 谓词决定该条目何时出现。
# ============================================================
class ClipAction:
    """卡片动作:supported(rec) 谓词 + run(rec, host) 执行;title 可为
    str 或 callable(rec, host)→str(如「固定/取消固定」随状态变)。"""
    __slots__ = ("title", "run", "supported")

    def __init__(self, title, run, supported=None):
        self.title = title
        self.run = run
        self.supported = supported or (lambda rec: True)


class WorkflowSlot:
    """占位:卡片菜单构建到此处时插入「工作流」子菜单(条目来自用户
    配置,非固定动作,故不进注册表本体)。"""


class TransformDialog(MessageBoxBase):
    """粘贴变换对话框(Ctrl+Shift+Enter):选变换 → 实时预览 → 粘贴/复制。"""

    def __init__(self, app, rec):
        super().__init__(app.win)
        self.app = app
        self.rec = rec
        self.setWindowTitle = None
        ttl = StrongBodyLabel("转换粘贴")
        self.viewLayout.addWidget(ttl)
        sub = CaptionLabel("选择变换后预览确认;「粘贴」写回剪贴板并回贴到"
                           "原窗口(带目标窗口校验)。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        from PySide6.QtWidgets import QRadioButton, QVBoxLayout as _VL, QWidget as _W
        box = _W()
        vl = _VL(box)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(6)
        self._btns = []
        content = rec.get("content") or ""
        for k, name, _fn in cm.TRANSFORMS:
            if cm.transform_text(content, k) is None and k != "plain":
                continue          # 不适用的变换直接不显示(如非 JSON)
            b = QRadioButton(name)
            b.toggled.connect(lambda on, k=k: self._update(k) if on else None)
            vl.addWidget(b)
            self._btns.append((k, b))
        self.viewLayout.addWidget(box)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(180)
        self.viewLayout.addWidget(self.preview)
        b_copy = PushButton("复制")
        b_copy.clicked.connect(self._copy)
        self.viewLayout.addWidget(b_copy, 0, Qt.AlignRight)
        self.yesButton.setText("粘贴并关闭")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(520)
        self._key = "plain"
        if self._btns:
            self._btns[0][1].setChecked(True)
        self._update(self._key)

    def _update(self, key):
        self._key = key
        out = cm.transform_text(self.rec.get("content") or "", key) or ""
        self.preview.setPlainText(out[:4000])

    def _copy(self):
        out = cm.transform_text(self.rec.get("content") or "", self._key) or ""
        if not self.app.core.copy_text(out):
            InfoBar.error("复制失败", "剪贴板写入失败,请重试", parent=self.app.win,
                          position=InfoBarPosition.TOP_RIGHT, duration=5000)
            return
        InfoBar.success("已复制", "变换结果已放入剪贴板", parent=self.app.win,
                        position=InfoBarPosition.TOP_RIGHT)

    def validate(self):
        self.app.paste_variant(self._key)
        return True


CLIP_ACTIONS = [
    ClipAction("粘贴", lambda r, h: h.paste_selected()),
    ClipAction("在独立窗口查看",
               lambda r, h: h.open_image_viewer(r) if r["type"] == "image"
               else h.open_viewer(r)),
    ClipAction("在浏览器打开", lambda r, h: h.open_link(r),
               supported=lambda r: r["type"] == "text"
               and cm.url_info(r["content"] or "")),
    ClipAction("打开文件", lambda r, h: h.open_files(r),
               supported=lambda r: r["type"] == "files"),
    ClipAction("在资源管理器中显示", lambda r, h: h.reveal_first(r),
               supported=lambda r: r["type"] == "files"),
    ClipAction("📌 置顶到屏幕", lambda r, h: h.pin_image(r),
               supported=lambda r: r["type"] == "image"),
    ClipAction(lambda r, h: "图片处理…" if not cm.find_sharex()
               else "图片处理(ShareX)…",
               lambda r, h: h.edit_image(r) if cm.find_sharex()
               else h.open_editor(r),
               supported=lambda r: r["type"] == "image"),
    ClipAction("内置编辑器…", lambda r, h: h.open_editor(r),
               supported=lambda r: r["type"] == "image" and cm.find_sharex()),
    ClipAction(lambda r, h: "与基准对比" if (h._diff_base is not None
               and h._diff_base is not r) else "选为对比基准(Ctrl+点击)",
               lambda r, h: h.compare_pick(r),
               supported=lambda r: r["type"] != "image"),
    WorkflowSlot(),
    ClipAction(lambda r, h: "取消固定" if r.get("pinned") else "固定",
               lambda r, h: h.toggle_pin()),
    ClipAction("删除", lambda r, h: h.delete_selected()),
]




# ============================================================
# 应用主体(后端逻辑不变)
# ============================================================
class _EventBridge(QObject):
    """后台线程 → GUI 线程的事件唤醒桥。

    数据仍走 core.signal_q(queue),本类只提供一个无参 Signal:后台线程
    emit 一次,Qt(队列连接)就在主线程事件循环里排一次空。取代旧的
    30ms QTimer 轮询——常驻应用不必每秒醒 33 次。
    """
    wakeup = Signal()


class WinClipboardApp(QObject):
    # ---- 核心状态别名(只读代理到 self.core;容器原地修改,引用稳定) ----
    @property
    def cfg(self):
        return self.core.cfg

    @property
    def history(self):
        return self.core.history

    @property
    def private(self):
        return self.core.private

    @property
    def worker(self):
        return self.core.worker

    @property
    def signal_q(self):
        return self.core.signal_q

    def ctxpack(self):
        """上下文包服务(延迟构造与缓存见 ui_tray.tray_service;测试可打桩)。"""
        from ctxpack.ui_tray import tray_service
        return tray_service(self)

    def _on_history_change(self):
        """核心投递的 "change" 信号(GUI 线程):剪贴捕获入史后,
        面板可见时合并刷新(防抖),避免连续捕获反复重建列表;
        隐藏期间只置脏标记,等下次显示窗口时刷新。"""
        self._view_dirty = True
        if getattr(self, "panel", None) is not None and self.panel.isVisible():
            self._view_timer.start()

    def __init__(self, qapp, safe_mode=False):
        super().__init__()
        self.qapp = qapp
        # 安全模式(连续启动崩溃后的降级启动):不加载监听/热键/图钉画布/
        # 自动清理/联网检查;历史浏览、导出、设置恢复照常(建议 20)
        self.safe_mode = bool(safe_mode)
        self.category = "all"
        self.query = ""
        self.src_filter = ""            # 来源应用筛选(exe 名;空 = 全部)
        self.time_filter = "all"        # 时间筛选档位(all/today/24h/7d/30d)
        self.selected = None
        self._multi_recs = set()        # Shift+点击的段选(记录身份;Delete 批量删)
        self._view = []
        self._card_widgets = []
        self._warned_hotkey = False
        self._editors = []
        self._thumbs = {}
        self._thumbs_bytes = 0         # 缓存像素字节估算(_thumbs_put 维护)
        self._view_dirty = True            # 隐藏期间历史有变则置位,显示时才重建

        cm.ensure_dirs()
        if not os.path.exists(cm.ICON_FILE):
            try:
                cm.make_icon(cm.ICON_FILE)
            except Exception:
                pass
        self.icon = QIcon(cm.ICON_FILE) if os.path.exists(cm.ICON_FILE) else QIcon()

        # 共享核心(状态加载 + 历史迁移 + 后台线程启动;安全模式不启线程)
        self.core = cm.ClipboardCore(safe_mode=self.safe_mode)

        self.win = MainWindow(self)
        self.panel = self.win.history_page     # 既有内部引用的稳定别名
        self._ai_runners = []
        self._viewers = []
        self._diffs = []
        self._diff_base = None               # 对比基准记录(Ctrl+点击选定)
        self._badge_cache = {}               # id(rec) → 卡片子类型徽章(JWT/Base64)

        # 图钉画布(置顶悬浮多图板):恢复上次钉住的图片与布局;
        # 独立于主窗口存在,程序退出才销毁
        self.pinboard = PinBoardManager(self)

        # 快捷粘贴浮层(Win+V 式轻入口,默认 Alt+`):外壳一次构建,
        # 打开只做过滤 + show,目标 <100ms
        self.quick = QuickPasteWindow(self)
        self._quick_ai = None           # Lazy: wheel/result widgets cost nothing at startup.

        # 定时器必须先于启动事件排空创建。ClipboardCore 构造时已经启动
        # 托盘/剪贴板线程,启动捕获可能在事件桥接通前积压 change 信号；
        # 若先 _drain_events(),可在 _on_history_change() 里访问到尚不存在的
        # _view_timer,造成真实冷启动偶发丢失首次刷新/热键响应。
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self.refresh_view)
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(180)
        self._view_timer.timeout.connect(self.refresh_view)

        # 后台事件桥:queue 传数据,Qt Signal 做唤醒(见 _EventBridge)。
        # 连接必须显式队列化——主线程内 emit(add_item → change)时直接
        # 调用会在排空过程中重入;核心线程先于本桥启动,接好后先排空一次
        # 积压(如启动期投递的 ctt_hotkey_state / 启动捕获)
        self.event_bridge = _EventBridge()
        self.event_bridge.wakeup.connect(self._drain_events,
                                         Qt.ConnectionType.QueuedConnection)
        self.core.on_event = self.event_bridge.wakeup.emit
        self._drain_events()
        # 首帧探针:挂在必绘的内容页上(见 eventFilter)
        self.panel.installEventFilter(self)

        # 历史落盘防抖:变更只置脏,静默 1.5s 后统一写盘一次
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1500)
        self._save_timer.timeout.connect(self.core.flush_history)
        self.core.on_dirty = self._save_timer.start

        # 缩略图后台生成器(启动预热全量截图不再阻塞 UI)
        self._thumb_loader = ThumbLoader(self)
        if not self.safe_mode:
            QTimer.singleShot(600, self._prewarm)
            # 孤儿图片清理:启动收尾全部完成后再做(不占启动关键路径,
            # UI 线程执行 + 方法内的新文件时间窗防护,与捕获线程无竞态)
            QTimer.singleShot(3000, self._cleanup_orphan_images)
            # 清理策略周期执行(常驻多日也要按天数/容量裁剪):每 6 小时一次
            self._cleanup_timer = QTimer(self)
            self._cleanup_timer.setInterval(6 * 3600 * 1000)
            self._cleanup_timer.timeout.connect(self._periodic_cleanup)
            self._cleanup_timer.start()

        # UI 事件循环健康监控:Mica 在个别远程会话上致派发器假死时
        # 无 Mica 自重启(机制与触发日志见 UiHealthMonitor)
        self.health = UiHealthMonitor(self)
        self.health.arm()
        # 首次启动欢迎(离屏/测试环境不弹;看过一次写入配置,不再打扰)
        if not self.cfg.get("first_run_shown") \
                and not self.safe_mode \
                and QGuiApplication.platformName() != "offscreen":
            QTimer.singleShot(1500, self._show_welcome)
        # 启动静默检查更新(每 7 天一次;发现新版才提示,不打扰;安全模式不联网)
        try:
            _last_chk = float(self.cfg.get("last_update_check", 0) or 0)
        except (TypeError, ValueError):
            _last_chk = 0.0
        if not self.safe_mode \
                and QGuiApplication.platformName() != "offscreen" \
                and time.time() - _last_chk > 7 * 86400:
            self.cfg["last_update_check"] = time.time()
            cm.save_config(self.cfg)
            QTimer.singleShot(8000, lambda: self.check_update(silent=True))
        if self.safe_mode:
            QTimer.singleShot(1200, self._show_safe_mode_banner)
        # 启动完整落地:Safe Mode 判据清零(本次启动已确认成功)
        cm.confirm_boot_ok()

    def _show_safe_mode_banner(self):
        """安全模式横幅:告知未加载的子系统,指明「导出/恢复后重启」出路。"""
        InfoBar.warning(
            "安全模式运行中",
            "剪贴板监听、全局热键、图钉画布与自动清理未加载;"
            "历史浏览、导出与设置恢复正常可用,退出后正常重启即可。",
            parent=self.win, duration=8000, position=InfoBarPosition.TOP)

    # ---- 剪贴板读取 / 历史维护:见 cm.ClipboardCore(共享后端) ----

    # ---- 视图 ----
    def refresh_view(self):
        t_all = stamp()
        self._view_dirty = False
        self._multi_recs = set()        # 视图重建,段选失效
        with PerfSpan("history_filter"):
            view = self.core.query_view(
                self.category, self.query, src=self.src_filter,
                since=cm.ClipboardCore.time_scope_cutoff(self.time_filter))
        self._view = view
        with PerfSpan("history_cards_render"):
            self.panel.set_view(view, self.history)
            self.panel._resize_to_content()   # 卡片数/展开态变化后面板高度自适应
            self.panel.sync_src_combo()       # 来源下拉跟历史走(保留当前选择)
        total = len(self.history)
        if len(view) == total:
            self.panel.lbl_count.setText("%d 个项目" % total)
        else:
            self.panel.lbl_count.setText("%d / %d 个项目" % (len(view), total))
        if view:
            self.selected = 0
            self.panel.select_index(0)
        else:
            self.selected = None
            self.panel.update_preview(None)
        since(t_all, "list_refresh")
        # 搜索 → UI 更新全程(含 120ms 防抖;目标 <150ms)
        ts = getattr(self, "_search_ts", None)
        if ts is not None:
            self._search_ts = None
            since(ts, "search_to_ui_update")

    def on_search(self, text):
        self.query = text
        self._search_ts = stamp()
        if self.panel.isVisible():
            self._search_timer.start()   # 120ms 防抖,输入过程中不重建
        else:
            self.refresh_view()

    def set_category(self, cat):
        if cat == self.category:
            return
        self.category = cat
        # 同步原生分类按钮状态
        try:
            hp = self.win.history_page
            for key, btn in hp._cat_btns.items():
                btn.blockSignals(True)
                btn.setChecked(key == cat)
                btn.blockSignals(False)
        except Exception:
            pass
        self.refresh_view()

    def set_src_filter(self, src):
        if src == self.src_filter:
            return
        self.src_filter = src
        self.refresh_view()

    def set_time_filter(self, scope):
        if scope == self.time_filter:
            return
        self.time_filter = scope
        self.refresh_view()

    def move_sel(self, delta):
        if not self._view:
            return
        self._multi_recs = set()
        cur = self.selected if self.selected is not None else (0 if delta > 0 else -1)
        cur = max(0, min(len(self._view) - 1, cur + delta))
        self.selected = cur
        self.panel.select_index(cur)
        self.panel.ensure_visible(cur)

    def select_card(self, card):
        """点击/键盘选中卡片:控件顺序(置顶组在前)≠视图顺序(历史序),
        必须按记录身份映射回 _view 索引,否则预览/高亮/回车粘贴错条目。"""
        self._multi_recs = set()
        try:
            hi = next(i for i, h in enumerate(self.history) if h is card.rec)
            vi = self._view.index(hi)
        except (StopIteration, ValueError):
            return
        self.selected = vi
        self.panel.select_index(vi)

    def range_select_from(self, card):
        """Shift+点击:当前选中到该卡整段多选(Delete 批量删除,可整批撤销)。"""
        try:
            hi = next(i for i, h in enumerate(self.history) if h is card.rec)
            end = self._view.index(hi)
        except (StopIteration, ValueError):
            return
        start = self.selected if self.selected is not None else end
        lo, hi2 = sorted((start, end))
        # 存 id(rec) 而不是记录本身:dict 不可哈希,且身份比对免疫刷新重建
        self._multi_recs = {id(self.history[self._view[i]])
                            for i in range(lo, hi2 + 1)}
        self.panel.mark_multi(self._multi_recs)

    def delete_selection_or_multi(self):
        """Delete 键:段选有效删整段,否则删当前条目;两者都可撤销。"""
        recs = [r for r in self.history if id(r) in self._multi_recs]
        if len(recs) > 1:
            n = self.core.batch_soft_delete(recs)
            self._multi_recs = set()
            self.refresh_view()
            self._offer_undo(n, "已删除 %d 条" % n)
        else:
            self.delete_selected()

    def paste_selected(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        rec = self.history[self._view[self.selected]]
        self.paste_rec(rec)

    def paste_rec(self, rec):
        # 先收起主窗(焦点回到目标窗口),再排队粘贴;
        # 目标窗口取唤出主窗前的现场(粘贴事务校验,切换即取消)
        if rec.get("type") == "files" \
                and not any(os.path.exists(p) for p in rec["content"]):
            MessageBox("WinClipboard", "文件已不存在,无法粘贴。", self.win).exec()
            return
        self.win.hide()
        if not self.core.paste_record(
                rec, target_hwnd=getattr(self, "_paste_target", None)):
            # 写入失败:不派发回贴;主窗拉回并如实提示(选择保留可重试)
            self.win.show()
            InfoBar.error("粘贴失败",
                          "剪贴板写入失败,请重试(其他程序可能正占用剪贴板)",
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)

    def _paste_text_now(self, text):
        """变换粘贴的统一出口:收起主窗 + 强制回贴到意图目标窗口;
        写入失败拉回主窗并提示,不派发回贴。"""
        self.win.hide()
        if not self.core.paste_text(text, force=True,
                                     target_hwnd=getattr(self, "_paste_target",
                                                         None)):
            self.win.show()
            InfoBar.error("粘贴失败",
                          "剪贴板写入失败,请重试(其他程序可能正占用剪贴板)",
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)

    def _selected_text_rec(self):
        if self.selected is None or self.selected >= len(self._view):
            return None
        rec = self.history[self._view[self.selected]]
        return rec if rec.get("type") == "text" else None

    def paste_plain(self):
        """Shift+Enter:纯文本粘贴(paste_text 只写 CF_UNICODETEXT,不带富格式)。"""
        rec = self._selected_text_rec()
        if rec is not None:
            self._paste_text_now(rec.get("content") or "")

    def paste_variant(self, key):
        """按变换键粘贴(「粘贴为」子菜单 / Transform 对话框共用)。"""
        rec = self._selected_text_rec()
        if rec is None:
            return
        out = cm.transform_text(rec.get("content") or "", key)
        if out is None:
            InfoBar.warning("不适用", "该内容无法应用此变换(如非 JSON)",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        self._paste_text_now(out)

    def open_transform(self):
        """Ctrl+Shift+Enter / 菜单「转换…」:变换预览对话框。"""
        rec = self._selected_text_rec()
        if rec is not None:
            TransformDialog(self, rec).exec()

    def _set_type(self, rec, value):
        """类型纠正(仅当前记录):写条目 + 库持久化 + 视图刷新。"""
        self.core.set_type_override(rec, value)
        self.refresh_view()

    # ---- 卡片动作 / 菜单 ----
    def card_action(self, act, card):
        self.select_card(card)
        if act == "paste":
            self.paste_selected()
        elif act == "pack":
            from ctxpack.ui_tray import add_records_to_pack
            add_records_to_pack(self, [card.rec])
        elif act == "pin":
            self.toggle_pin()
        elif act == "del":
            self.delete_selected()
        elif act == "more":
            self.card_menu(card, card.mapToGlobal(card.rect().topRight()))

    def card_menu(self, card, gpos):
        """右键菜单:条目全部来自 CLIP_ACTIONS 注册表(supported 谓词过滤),
        工作流子菜单在 WorkflowSlot 占位处按配置插入。"""
        rec = card.rec
        self.select_card(card)

        def act(menu, text):
            # qfluentwidgets RoundMenu.addAction 只接受 QAction;传 str 会把原始
            # 字符串塞进内部 _actions,首个条目渲染时 'str' object has no
            # attribute 'icon' 崩溃(qfluentwidgets 1.11.3 实测)
            a = QAction(text, menu)
            menu.addAction(a)
            return a

        menu = RoundMenu(parent=self.panel)
        if rec.get("type") == "text":
            act(menu, "快速 AI…").triggered.connect(lambda: self.open_quick_ai(rec))
        if len(self._multi_recs) > 1:      # 段选激活时给批量动作置顶入口
            a = act(menu, "删除所选 %d 条(可撤销)" % len(self._multi_recs))
            a.triggered.connect(lambda _=False: self.delete_selection_or_multi())
            menu.addSeparator()
        # ---- 加入上下文包(上下文篮;多选段选时即批量入口) ----
        from ctxpack.ui_tray import append_ctx_menu
        append_ctx_menu(self, menu,
                        [r for r in self.history if id(r) in self._multi_recs]
                        if len(self._multi_recs) > 1 else [rec])
        if rec.get("type") == "text":
            # ---- 粘贴为(变换)子菜单:不适用项置灰 ----
            sub = RoundMenu("粘贴为", self.panel)
            for k, name, _fn in cm.TRANSFORMS:
                a = act(sub, name)
                if k != "plain" and \
                        cm.transform_text(rec.get("content") or "", k) is None:
                    a.setEnabled(False)
                a.triggered.connect(
                    lambda _=False, k=k: self.paste_variant(k))
            sub.addSeparator()
            a = act(sub, "转换…(Ctrl+Shift+Enter)")
            a.triggered.connect(lambda _=False: self.open_transform())
            menu.addMenu(sub)
            # ---- 类型纠正子菜单:自动 / 纯文本 / JWT / Base64 / JSON / URL ----
            tsub = RoundMenu("类型", self.panel)
            cur = rec.get("type_override") or ""
            for val, label in (("", "自动识别"), ("text", "纯文本"),
                               ("jwt", "JWT"), ("base64", "Base64"),
                               ("json", "JSON"), ("url", "URL")):
                mark = "● " if val == cur else "○ "
                a = act(tsub, mark + label)
                a.triggered.connect(
                    lambda _=False, v=val: self._set_type(rec, v or None))
            menu.addMenu(tsub)
        for entry in CLIP_ACTIONS:
            if isinstance(entry, WorkflowSlot):
                if rec["type"] != "text":
                    continue
                wfs = self.cfg.get("ai_workflows") or []
                if wfs:
                    sub = RoundMenu("工作流", self.panel)
                    for wf in wfs:
                        a = act(sub, wf.get("name", "未命名"))
                        a.triggered.connect(
                            lambda _=False, w=wf: self.run_workflow(w, rec))
                    sub.addSeparator()
                    am = act(sub, "管理工作流…")
                    am.triggered.connect(self.show_ai_hub)
                    menu.addMenu(sub)
                else:
                    a = act(menu, "工作流(先配置)…")
                    a.triggered.connect(self.show_ai_hub)
                continue
            if not entry.supported(rec):
                continue
            title = entry.title(rec, self) if callable(entry.title) else entry.title
            if title == "删除":
                menu.addSeparator()
            a = act(menu, title)
            a.triggered.connect(
                lambda _=False, r=rec, f=entry.run: f(r, self))
        menu.exec(gpos)

    def toggle_pin(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        self.core.toggle_pin_at(self._view[self.selected])
        self.refresh_view()

    def delete_selected(self):
        if self.selected is None or self.selected >= len(self._view):
            return
        self.core.soft_delete_at(self._view[self.selected])
        self.refresh_view()
        self._offer_undo(1, "已删除")

    def undo_delete(self):
        back = self.core.undo_delete()
        if back:
            self.refresh_view()
            InfoBar.success("已撤销", "恢复了 %d 条记录" % len(back),
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=2500)

    def _offer_undo(self, n, title):
        """删除/清空后的撤销入口:InfoBar 挂「撤销」按钮,条目文件未删,可整批恢复。"""
        bar = InfoBar.warning(title, "%d 条记录可在提示消失前撤销" % n,
                              parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                              duration=6000)
        btn = PushButton("撤销", bar)
        btn.clicked.connect(self.undo_delete)
        bar.addWidget(btn)

    def open_editor(self, rec, on_result=None, annos=None, layer_mode=False):
        """图片编辑入口(内置):查看器内嵌编辑模式——单窗口看图+标注,
        不再另开编辑器窗口。annos/layer_mode 供图钉画布非破坏标注层回编。"""
        if not os.path.exists(rec.get("content", "")):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。", self.panel).exec()
            return
        self.open_image_viewer(rec, edit=True, on_result=on_result,
                               annos=annos, layer_mode=layer_mode)

    def edit_image(self, rec):
        """图片编辑统一入口:优先成熟外部工具 ShareX → ksnip(编辑结果经
        剪贴板自动回流历史),都没装/调起失败时回退内置编辑器。"""
        if not os.path.exists(rec.get("content", "")):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。", self.panel).exec()
            return
        if cm.find_sharex() and cm.open_in_sharex(rec["content"]):
            InfoBar.success("已在 ShareX 中打开",
                            "编辑并复制/保存后,结果会自动进入剪贴板历史",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        if cm.find_ksnip() and cm.open_in_ksnip(rec["content"]):
            InfoBar.success("已在 ksnip 中打开",
                            "编辑并复制/保存后,结果会自动进入剪贴板历史",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        self.open_editor(rec)

    def open_viewer(self, rec):
        """打开内容查看器(长内容的独立工具窗口)。"""
        try:
            v = ViewerWindow(self, rec)
            _pop_to_front(v)
            self._viewers.append(v)
            return v
        except Exception:
            cm.log("打开查看器失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "查看器打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()
            return None

    def open_link(self, rec):
        """链接条目:默认浏览器打开(非链接文本退回内容查看器)。"""
        info = cm.url_info(rec.get("content") or "")
        if not info:
            self.open_viewer(rec)
            return
        if cm.open_url(info[1]):
            InfoBar.success("已在浏览器打开", info[1][:80], parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.error("打开失败", info[1][:120], parent=self.win,
                          position=InfoBarPosition.TOP_RIGHT)

    def compare_pick(self, rec):
        """对比流:第一条 Ctrl+点击设为基准,第二条开对比窗口;再点基准取消。"""
        if rec.get("type") == "image":
            InfoBar.warning("对比", "图片暂不支持对比(仅文本 / 文件列表)",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
            return
        if self._diff_base is None:
            self._diff_base = rec
            InfoBar.success("对比基准已选", "Ctrl+点击另一条记录开始对比",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
        elif self._diff_base is rec:
            self._diff_base = None
            InfoBar.info("已取消对比基准", "", parent=self.win,
                         position=InfoBarPosition.TOP_RIGHT, duration=1500)
        else:
            base, self._diff_base = self._diff_base, None
            self.open_diff(base, rec)

    def open_diff(self, rec_a, rec_b):
        """打开内容对比窗口(A=基准/旧,B=所选/新)。"""
        try:
            w = DiffWindow(self, rec_a, rec_b)
            _pop_to_front(w)
            self._diffs.append(w)
            return w
        except Exception:
            cm.log("打开对比窗口失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "对比窗口打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()
            return None

    def open_files(self, rec):
        """文件条目查看:系统默认程序打开(同资源管理器双击;上限 10 个防爆开)。
        文件全部不存在时退回路径清单窗口。"""
        paths = [p for p in rec.get("content") or [] if os.path.exists(p)]
        if not paths:
            self.open_viewer(rec)
            return
        n = 0
        for p in paths[:10]:
            if cm.open_file_default(p):
                n += 1
        if n:
            InfoBar.success("已打开", "%d 个文件已用默认程序打开" % n,
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)

    def reveal_first(self, rec):
        for p in rec.get("content") or []:
            if os.path.exists(p):
                if cm.reveal_in_explorer(p):
                    return
                break
        InfoBar.error("定位失败", "文件不存在或无法打开资源管理器",
                      parent=self.win, position=InfoBarPosition.TOP_RIGHT)

    def open_image_viewer(self, rec, edit=False, on_result=None,
                          annos=None, layer_mode=False):
        """打开图片查看器(缩放/平移;与文本查看器同入 _viewers 统一清理)。
        edit=True 直接进入内嵌编辑模式;on_result 为编辑产物直通回调;
        annos/layer_mode 见 ImageEditor(图钉画布非破坏标注层)。"""
        try:
            v = ImageViewerWindow(self, rec, edit=edit, on_result=on_result,
                                  annos=annos, layer_mode=layer_mode)
            _pop_to_front(v)
            self._viewers.append(v)
            return v
        except ValueError:
            MessageBox("WinClipboard", "无法加载图片文件,可能已被清理。",
                       self.panel).exec()
            return None
        except Exception:
            cm.log("打开图片查看器失败:\n%s" % traceback.format_exc())
            MessageBox("WinClipboard", "图片查看器打开失败,详情见:\n%s" % cm.LOG_FILE,
                       self.panel).exec()
            return None

    def pin_image(self, rec):
        """把图片条目钉到置顶画布(右键图片条目 →「置顶到屏幕」)。
        画布独立持有资产副本(pins/images),历史删除不影响已钉图片;
        见 pinboard.PinBoardManager。"""
        path = str(rec.get("content") or "") if rec.get("type") == "image" else ""
        if not path or not os.path.exists(path):
            MessageBox("WinClipboard", "图片文件不存在,可能已被清理。",
                       self.panel).exec()
            return None
        it = self.pinboard.pin_path(path)
        if it is None:
            MessageBox("WinClipboard", "无法加载图片文件,可能已被清理。",
                       self.panel).exec()
            return None
        return it

    # ---- AI 工作流 / 各功能页(单一窗口内导航切换) ----
    def show_ai_hub(self):
        self.win.switchTo(self.win.workflow_page)

    def apply_hotkey(self, mod, key):
        """设置页热键录制:命令发给核心线程重注册(RegisterHotKey 失败
        = 被占用 = 冲突检测),成败经 hotkey_change 事件回 UI。"""
        try:
            self.core.worker.cmd_q.put(("hotkey", int(mod), int(key)))
        except Exception:
            cm.log("热键更改命令投递失败:\n%s" % traceback.format_exc())

    def apply_quick_hotkey(self, mod, key):
        """快捷粘贴浮层热键录制(同 apply_hotkey 套路,独立命令/事件)。"""
        try:
            self.core.worker.cmd_q.put(("hotkey_qp", int(mod), int(key)))
        except Exception:
            cm.log("快捷粘贴热键更改命令投递失败:\n%s" % traceback.format_exc())

    def open_quick_ai(self, rec=None):
        if self._quick_ai is None:
            from ai_quick import QuickAIWindow
            self._quick_ai = QuickAIWindow(self)
        self._quick_ai.open_wheel(rec)

    def apply_ai_hotkey(self, mod, key):
        self.worker.cmd_q.put(("hotkey_ai", int(mod), int(key)))

    def _on_ai_hotkey_change(self, ok, mod, key):
        if ok:
            self._ai_hotkey_ok = True
            self.cfg["ai_hotkey_mod"], self.cfg["ai_hotkey_key"] = int(mod), int(key)
            saved = cm.save_config(self.cfg)
            InfoBar.success("快速 AI 热键已更新", "已保存" if saved else "本次会话已生效，但配置未保存",
                            parent=self.win)
        else:
            InfoBar.error("快速 AI 热键被占用", "已保留原热键，可重新录制", parent=self.win)
        self.win.settings_page.update_ai_hotkey_label()

    def _on_qp_hotkey_change(self, ok, mod, key):
        label = cm.hotkey_label(mod, key)
        if ok:
            self.cfg["quick_mod"] = int(mod)
            self.cfg["quick_key"] = int(key)
            cm.save_config(self.cfg)
            InfoBar.success("快捷粘贴热键已更新", "新热键:" + label,
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.error("快捷粘贴热键注册失败",
                          "「%s」可能被其他软件占用,已保留原热键" % label,
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
        try:
            self.win.settings_page.update_quick_label()
        except Exception:
            pass

    def toggle_monitor_pause(self, on=None):
        """临时隐私模式:暂停期间剪贴板事件不入史(不落盘,重启即恢复监听)。"""
        paused = (not self.core.monitor_paused) if on is None else bool(on)
        if paused == self.core.monitor_paused:
            return
        self.core.monitor_paused = paused
        if paused:
            InfoBar.warning("已暂停监听", "期间复制的内容不会进入历史",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=3000)
        else:
            InfoBar.success("已恢复监听", "", parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT, duration=2000)
        try:
            self.win.settings_page.sync_monitor_pause()
        except Exception:
            pass

    def _on_hotkey_change(self, ok, mod, key):
        label = cm.hotkey_label(mod, key)
        if ok:
            self.cfg["hotkey_mod"] = int(mod)
            self.cfg["hotkey_key"] = int(key)
            cm.save_config(self.cfg)
            InfoBar.success("热键已更新", "新热键:" + label, parent=self.win,
                            position=InfoBarPosition.TOP_RIGHT)
        else:
            InfoBar.error("热键注册失败",
                          "「%s」可能被其他软件占用,已保留原热键" % label,
                          parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
        try:
            self.win.settings_page.update_hotkey_label()
        except Exception:
            pass

    def show_services(self):
        self.win.services_page.refresh()   # 配置可能在别处变动(如工作流页填入示例)
        self.win.switchTo(self.win.services_page)

    def run_workflow(self, wf, rec):
        try:
            runner = WorkflowRunner(self, wf, rec["content"])
        except Exception as exc:      # 上下文包无法拼装等:拒绝启动并如实说明
            InfoBar.error("工作流无法启动", str(exc), parent=self.win,
                          position=InfoBarPosition.TOP_RIGHT, duration=8000)
            return
        _pop_to_front(runner)
        self._ai_runners.append(runner)

    # ---- 托盘动作 ----
    def clear_all(self):
        box = MessageBox("清空历史", "确定清空全部未置顶条目?清空后 6 秒内仍可撤销。",
                         self.win)
        if not box.exec():
            return
        removed = self.core.soft_clear_unpinned()
        self.refresh_view()
        if removed:
            self._offer_undo(len(removed), "已清空(置顶保留)")

    def show_private(self):
        if not self.private:
            InfoBar.info("私密内容", "暂无敏感内容", parent=self.win,
                         position=InfoBarPosition.TOP_RIGHT)
            return
        self.win.private_page.refresh()
        self.win.switchTo(self.win.private_page)

    def about(self):
        hk = cm.hotkey_label(self.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
                             self.cfg.get("hotkey_key", 0x56))
        MessageBox("关于 WinClipboard",
                   "Windows 桌面剪贴板管理器  v%s\n\n"
                   "热键: %s 弹出/隐藏\n"
                   "左键托盘图标: 切换面板\n"
                   "右键托盘图标: 菜单\n\n"
                   "数据目录:\n" % (cm.APP_VERSION, hk) + cm.APP_DIR,
                   self.panel).exec()

    def shortcut_help_text(self):
        """应用内快捷键总览(全局热键动态跟随当前配置)。"""
        hk = cm.hotkey_label(self.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
                             self.cfg.get("hotkey_key", 0x56))
        qk = cm.hotkey_label(self.cfg.get("quick_mod", cm.MOD_ALT),
                             self.cfg.get("quick_key", 0xC0))
        return ("全局\n"
                "  %s   显示 / 隐藏主窗口(设置页可按键录制更改)\n"
                "  %s   快捷粘贴浮层(Win+V 式:搜索 + ↑↓ + 回车粘贴)\n"
                "  Ctrl + Shift + P   剪贴板图片直钉到画布\n"
                "  %s   快速 AI 轮盘（1~6 选择动作，Esc 取消）\n"
                "  Ctrl + Alt + T   画布鼠标穿透切换\n"
                "\n快捷粘贴浮层(%s)\n"
                "  ↑ / ↓   选择      回车   粘贴      Shift + 回车   纯文本粘贴\n"
                "  Ctrl + 回车   打开查看器      Ctrl + 1~9   直选粘贴\n"
                "  Alt + 回车   将上下文包拼装结果粘贴回原窗口\n"
                "  Ctrl + Q   快速 AI（以选中条目为材料，原窗口为回贴目标）\n"
                "  Tab   切换类型(全部/文本/链接/图片/文件)\n"
                "  Del   删除条目(可撤销)      Esc   关闭并回到原窗口\n"
                "\n历史列表\n"
                "  ↑ / ↓   移动选择      回车   粘贴选中条目\n"
                "  右栏 AI 工作台   选中卡片即材料，Ctrl + 回车 在提问框发送\n"
                "  Shift + 回车   纯文本粘贴(不带富文本格式)\n"
                "  Ctrl + Shift + 回车   转换粘贴(JSON 格式化/压缩、URL 编解码、\n"
                "                        转义、大小写等,实时预览)\n"
                "  Esc   收起主窗口      双击卡片   打开独立查看器\n"
                "  Ctrl + 点击   选两条记录对比(第一条设基准,第二条开对比窗口)\n"
                "\n搜索语法(Search DSL,与筛选下拉叠加生效)\n"
                "  source:vscode   按来源应用(自动补 .exe)\n"
                "  type:json|jwt|base64|url|text|image|files   按类型/子类型\n"
                "  after:2026-09-01 或 after:7d   时间起点\n"
                "  before:2026-09-10 或 before:3d   时间上界\n"
                "  is:pinned   只看置顶;其余词按子串搜索\n"
                "  示例:type:image source:snipping\n"
                "\n内容查看器\n"
                "  Ctrl + F   浮动查找      Ctrl + = / - / 0   字号缩放\n"
                "\n图片编辑(查看器内编辑态)\n"
                "  Ctrl + Z   撤销      Del   删除选中标注      Esc   放弃编辑\n"
                "\n系统托盘\n"
                "  左键   切换面板      右键   菜单(暂停监听 / 清空 / 退出)"
                % (hk, qk, cm.hotkey_label(self.cfg.get("ai_hotkey_mod", cm.MOD_CONTROL | cm.MOD_ALT),
                                          self.cfg.get("ai_hotkey_key", 0x20)), qk))

    def show_shortcut_help(self):
        MessageBox("快捷键与操作", self.shortcut_help_text(), self.win).exec()

    def check_update(self, silent=False):
        """检查 GitHub 最新 Release(后台线程)。silent=True 为启动期静默检查:
        只有发现新版才提示,其余情况不打扰。"""
        try:
            self._update_worker = UpdateWorker(self)
        except Exception:
            return

        def on_done(st, ver, url):
            self._update_worker = None
            if st == "ok":
                bar = InfoBar.warning("发现新版本", "最新 %s(当前 %s)"
                                      % (ver, cm.APP_VERSION), parent=self.win,
                                      position=InfoBarPosition.TOP_RIGHT,
                                      duration=10000)
                b = PushButton("打开发布页", bar)
                b.clicked.connect(lambda: cm.open_url(url))
                bar.addWidget(b)
            elif silent:
                return
            elif st == "latest":
                InfoBar.success("已是最新版本", "当前 %s" % cm.APP_VERSION,
                                parent=self.win,
                                position=InfoBarPosition.TOP_RIGHT)
            elif st == "norelease":
                InfoBar.info("暂无发布版本",
                             "仓库还没有 Release;可在「打开发布页」查看项目主页",
                             parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                             duration=5000)
            else:
                InfoBar.error("检查更新失败", str(url)[:120], parent=self.win,
                              position=InfoBarPosition.TOP_RIGHT, duration=5000)
        self._update_worker.done.connect(on_done)
        self._update_worker.start()

    def _show_welcome(self):
        """首次启动引导:看完即记入配置不再打扰(离屏/测试环境不弹)。"""
        self.cfg["first_run_shown"] = True
        cm.save_config(self.cfg)
        hk = cm.hotkey_label(self.cfg.get("hotkey_mod", cm.MOD_CONTROL | cm.MOD_SHIFT),
                             self.cfg.get("hotkey_key", 0x56))
        box = MessageBox(
            "欢迎使用 WinClipboard",
            "程序已在本机后台运行(系统托盘 📋)。接下来:\n\n"
            "1. 随便复制点什么 —— 文字 / 截图 / 文件都会自动进入历史\n"
            "2. 按 %s 随时唤出或收起主窗口\n"
            "3. 密码 / 银行卡等内容会自动进「私密区」,列表默认遮罩显示\n"
            "4. 设置页可以:按键录制新热键、按应用排除、自动清理、加密历史\n\n"
            "全部操作说明见「设置 → 快捷键与操作」。" % hk, self.win)
        box.yesButton.setText("开始使用")
        box.cancelButton.hide()
        box.buttonLayout.insertStretch(0, 1)   # 按钮靠右,与库对话框一致
        box.exec()

    def _warn_hotkey(self):
        if self._warned_hotkey:
            return
        self._warned_hotkey = True
        MessageBox("WinClipboard",
                   "全局热键(%s)注册失败,可能被其他软件占用。\n"
                   "可在设置页直接录制新热键,或重启后重试。"
                   % cm.hotkey_label(self.cfg.get("hotkey_mod"),
                                     self.cfg.get("hotkey_key")),
                   self.win).exec()

    def quit_app(self):
        if self._quick_ai is not None:
            self._quick_ai.shutdown()
        _ai_panel = getattr(getattr(self, "panel", None), "ai_panel", None)
        if _ai_panel is not None:
            _ai_panel.shutdown()
        for r in self._ai_runners:          # 等 AI 线程收尾,避免 QThread 运行中析构
            try:
                r._dead = True
                if r._worker is not None and r._worker.isRunning():
                    r._worker.wait(3000)
            except Exception:
                pass
        try:
            self._thumb_loader.shutdown()   # 缩略图后台队列:排空再退
        except Exception:
            pass
        try:
            self.pinboard.save_now()       # 画布可见状态随 quit 落盘
        except Exception:
            pass
        self.core.save_all()
        # 私密区保存失败:如实反馈并允许取消退出(未保存变更将随退出丢失;
        # 原文件未被修改,仍是恢复来源)
        if getattr(self.core, "_private_dirty", False) \
                and getattr(self.core, "private_save_error", ""):
            box = MessageBox(
                "私密区未保存",
                "私密区保存失败:%s\n原文件未被修改。"
                "「确定」仍要退出(未保存的变更将丢失),"
                "「取消」返回应用排查后重试。" % self.core.private_save_error,
                self.win)
            if not box.exec():
                return
        self.worker.cmd_q.put(("quit",))
        self.qapp.quit()

    # ---- 信号排空 / 预热 ----
    def eventFilter(self, obj, ev):
        """首帧探针:热键 → HistoryPage 首次 Paint(它必绘;MainWindow 自身
        在 Mica/子窗口全遮下不收 Paint/Expose,顶层挂不到,只能挂内容页)。
        ts 一次一清,常规重绘不产生日志。"""
        if obj is self.panel and ev.type() == QEvent.Paint:
            ts = getattr(self, "_hotkey_ts", None)
            if ts is not None:
                self._hotkey_ts = None
                since(ts, "hotkey_to_first_paint")
        return super().eventFilter(obj, ev)

    def _drain_events(self):
        try:
            while True:
                kind, *rest = self.signal_q.get_nowait()
                try:
                    self._handle_signal(kind, rest)
                except Exception:
                    cm.log("处理信号 %r 失败:\n%s" % (kind, traceback.format_exc()))
        except cm.queue.Empty:
            pass

    def _handle_signal(self, kind, rest):
        if kind == "clip":
            item = rest[0]
            if item:
                self.core.add_item(item)
        elif kind == "change":
            self._on_history_change()
        elif kind == "private":
            # 敏感内容已隔离(捕获默认静默,用户无从得知去哪了——补一条轻提示)
            jwt = bool(rest[0]) if rest else False
            self.win.private_page.refresh()
            InfoBar.success("已隔离敏感内容到私密区",
                            "JWT 可在 私密区 →「查看」中解码(仅解码,不验签)"
                            if jwt else "可在左侧「私密区」查看",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=3000)
        elif kind == "hotkey":
            self._hotkey_ts = stamp()       # 首帧统计起点(paintEvent 收口)
            with PerfSpan("hotkey_to_show_window"):
                self.win.toggle()
        elif kind == "pin_hotkey":          # Ctrl+Shift+P:直接钉剪贴板图片
            self.pinboard.pin_clipboard_image()
        elif kind == "qp_hotkey":           # Alt+`:快捷粘贴浮层
            with PerfSpan("quickpaste_open"):
                self.quick.toggle()
        elif kind == "ai_hotkey":
            self.open_quick_ai()
        elif kind == "ai_hotkey_state":
            self._ai_hotkey_ok = bool(rest[0])
            self.win.settings_page.update_ai_hotkey_label()
        elif kind == "ai_hotkey_change":
            self._on_ai_hotkey_change(bool(rest[0]), rest[1], rest[2])
        elif kind == "qp_hotkey_state":     # 启动期快捷粘贴热键注册结果
            self._qp_hotkey_ok = bool(rest[0])
            try:
                self.win.settings_page.update_quick_label()
            except Exception:
                pass
        elif kind == "qp_hotkey_change":    # 设置页录制快捷粘贴热键的结果
            self._on_qp_hotkey_change(bool(rest[0]), rest[1], rest[2])
        elif kind == "paste_abort":         # 粘贴事务取消(窗口切换/剪贴板被改)
            InfoBar.warning("已取消自动粘贴", str(rest[0]) if rest else "",
                            parent=self.win, position=InfoBarPosition.TOP_RIGHT,
                            duration=4000)
        elif kind == "ctt_hotkey":          # Ctrl+Alt+T:切换鼠标穿透
            self.pinboard.toggle_click_through(None)
        elif kind == "ctt_hotkey_state":    # 穿透退出热键注册结果(失败则禁穿透)
            self.pinboard.mark_ctt_hotkey(bool(rest[0]))
        elif kind == "hotkey_change":       # 设置页热键录制结果(成败/回滚)
            self._on_hotkey_change(bool(rest[0]), rest[1], rest[2])
        elif kind == "tray":
            act = rest[0]
            if act == "show":
                with PerfSpan("tray_to_show_window"):
                    self.win.toggle()
            elif act == "clear":
                self.clear_all()
            elif act == "pause":
                self.toggle_monitor_pause()
            elif act == "private":
                self.show_private()
            elif act == "autostart":
                self.core.toggle_autostart()
            elif act == "about":
                self.about()
            elif act == "hotkey_fail":
                self._warn_hotkey()
            elif act == "ctt_off":          # 托盘菜单:穿透保底关闭入口
                self.pinboard.toggle_click_through(False)
            elif act == "quit":
                self.quit_app()

    # ---- 缩略图缓存(统一 LRU + 字节预算;所有写入路径共用 _thumbs_put) ----
    THUMB_CACHE_BUDGET = 48 * 1024 * 1024   # 像素字节预算(w*h*4)
    THUMB_CACHE_COUNT = 400                 # 条目上限(次级保险)

    def _thumbs_put(self, path, pm):
        """统一写入:LRU 触碰 + 字节预算/条目上限淘汰。所有缓存写入
        (后台生成回填 / 同步兜底)都必须走这里,不允许绕过淘汰逻辑。
        占用按 w*h*4 估算,不含 QImageReader 原始解码与 Qt 平台侧的
        纹理/缓存开销(实际原生占用会略高,预算已留余量)。"""
        old = self._thumbs.pop(path, None)
        if old is not None:
            self._thumbs_bytes -= old.width() * old.height() * 4
        self._thumbs[path] = pm
        self._thumbs_bytes += pm.width() * pm.height() * 4
        while ((self._thumbs_bytes > self.THUMB_CACHE_BUDGET
                or len(self._thumbs) > self.THUMB_CACHE_COUNT)
               and self._thumbs):
            k = next(iter(self._thumbs))    # 插入序 FIFO(最近触碰的在尾)
            v = self._thumbs.pop(k)
            self._thumbs_bytes -= v.width() * v.height() * 4

    def thumb_cached(self, path):
        """仅查缓存(卡片构建用):未命中返回 None 并转后台生成,不阻塞 UI 线程。

        命中时把键移到字典尾部(LRU 触碰):淘汰按插入序 FIFO,列表来回滚动时
        最近用过的缩略图不至于先被挤出去引发重解码。
        """
        if not path:
            return None
        pm = self._thumbs.pop(path, None)
        if pm is not None:
            self._thumbs[path] = pm
            return pm
        self.request_thumb(path, visible=True)
        return None

    def request_thumb(self, path, visible=True):
        loader = getattr(self, "_thumb_loader", None)
        if loader is not None and path:
            loader.submit(path, visible=visible)

    def thumb_for(self, path):
        if not path or not os.path.exists(path):
            return None
        pm = self._thumbs.get(path)
        if pm is not None:
            self._thumbs.pop(path)
            self._thumbs[path] = pm    # LRU 触碰,与 thumb_cached 一致
            return pm
        try:
            # Qt C++ 解码+缩放比 PIL LANCZOS 快约 40% 且无 PNG 编码往返;
            # 2x 尺寸供高 DPI 显示。启动时的全量预热走 ThumbLoader 后台线程,
            # 这里只兜底处理新截图入史的单张生成。
            w, h = Card.THUMB
            img = QImage(path)
            if img.isNull():
                return None
            pm = QPixmap.fromImage(img.scaled(
                w * 2, h * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._thumbs_put(path, pm)
        except Exception:
            return None
        return pm

    def invalidate_thumb(self, path):
        """条目删除/替换/清空后失效缓存(路径按内容寻址,通常无脏数据;
        wipe/清理类批量场景调用 reset_thumbs)。"""
        pm = self._thumbs.pop(path, None)
        if pm is not None:
            self._thumbs_bytes -= pm.width() * pm.height() * 4

    def reset_thumbs(self):
        """整表清空(清空全部数据 / 模式切换等)。"""
        self._thumbs.clear()
        self._thumbs_bytes = 0

    def thumb_cache_stats(self):
        """(条目数, 像素字节)——诊断与性能测试用。"""
        return len(self._thumbs), self._thumbs_bytes

    def _cleanup_orphan_images(self):
        """启动 3 秒后的孤儿图片清理(core 侧自带引用快照与新文件时间窗)。"""
        try:
            self.core.cleanup_orphan_images()
        except Exception:
            cm.log("孤儿图片清理失败:\n%s" % traceback.format_exc())

    def _periodic_cleanup(self):
        """每 6 小时按 cleanup_days / disk_cap_mb 裁剪一次(常驻多日场景)。"""
        try:
            n, _ = self.core.apply_cleanup()
            if n:
                self.refresh_view()
        except Exception:
            cm.log("周期清理跳过:\n%s" % traceback.format_exc())

    def _prewarm(self):
        """启动 600ms 后的空闲预热:后台生成缩略图 + 隐藏时预构建卡片池。

        百张卡片的首次构建实测约 1 秒(QSS 解析 / 标签初始化),过去发生在
        第一次热键唤出时;挪到常驻托盘的空闲期做完,首次唤出直接走池复用(~10ms)。
        缩略图只预热最近有限张(PREWARM_THUMBS):更早的滚到可见时按高
        优先级现生成,不再把全量历史图片一次性排进后台队列。
        """
        try:
            if self._view_dirty:
                self.refresh_view()
                for c in self.panel._cards:
                    c.ensurePolished()   # 隐藏状态下提前完成 QSS 解析
            n = 0
            for h in self.history:
                if h.get("type") == "image":
                    self._thumb_loader.submit(h["content"], visible=False)
                    n += 1
                    if n >= self.PREWARM_THUMBS:
                        break
        except Exception:
            cm.log("启动预热失败:\n%s" % traceback.format_exc())

    PREWARM_THUMBS = 48     # 启动预热缩略图上限(最近优先)


def _pop_to_front(w):
    """把新工具窗口顶到前台(查看器 / 运行台等)。

    双击卡片打开时,调用方主窗仍持有鼠标输入序列,Windows 前台规则常拒绝
    activateWindow、raise_ 也压不过去 → 查看器开在主窗下面(用户实测:
    「双击复制的内容,它不是置顶的,都隐藏在主页面下面」)。标准解法:
    短暂置 TOPMOST 抢到最上层,再摘掉标志落回普通层级——之后主窗仍可
    正常盖回,不产生永久置顶的副作用。
    """
    try:
        w.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        w.show()
        w.raise_()
        w.activateWindow()
        w.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        w.show()
    except Exception:
        try:
            w.show()
        except Exception:
            pass


# ============================================================
# 入口
# ============================================================
def main():
    cm.install_excepthooks()
    cm.ensure_dirs()
    ok, mutex = cm.single_instance()
    if not ok:
        # 二次启动:唤醒已运行实例。优先给托盘宿主窗口投递 WM_HOTKEY,
        # 走实例自己的热键链(toggle/show_fast,内容正常渲染)——直接对主窗
        # Win32 ShowWindow 只对"曾显示过后隐藏"的窗口有效,启动后从未
        # 经过 Qt show 的主窗没有渲染内容,拉起来是空白框(实测坑)。
        try:
            import win32gui
            import win32con
            host = win32gui.FindWindow(None, "WinClipboardTrayHost")
            if host:
                win32gui.PostMessage(host, win32con.WM_HOTKEY, 0, 0)
                return 0
            hwnd = win32gui.FindWindow(None, "WinClipboard")
            if hwnd:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                else:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                return 0
        except Exception:
            pass
        cm.ctypes.windll.user32.MessageBoxW(
            0, "WinClipboard 已经在运行中(检查系统托盘)。", cm.APP_NAME, 0x40)
        return 0
    globals()["_MUTEX"] = mutex
    streak = cm.begin_boot()     # Safe Mode 判据:本次启动先记"未确认"
    cm.log("启动(Qt+Fluent) pid=%d py=%s %s" %
           (os.getpid(), sys.executable, sys.version.split()[0]))
    perf_probe.setup()                    # 性能埋点 → logs/perf.log(独立于 app.log)

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    apply_fonts(qapp)          # 中文回退字族(见 FONT_FAMILIES 注释)
    # Atelier Zero 视觉语言：强制浅色，禁用 Mica/亚克力，固定 Études 蓝强调色。
    # 不再跟随系统深色模式或读取 config 中的主题色设置。
    cfg0 = cm.load_config()
    apply_atelier_zero(qapp, cfg0)

    # 连续启动崩溃 → 提供安全模式(建议 20):不加载监听/热键/画布/自动
    # 任务,保住"导出数据 + 恢复设置"两条退路。选正常启动若再失败,下次仍提示。
    safe = False
    if streak >= cm.SAFE_MODE_THRESHOLD \
            and QGuiApplication.platformName() != "offscreen":
        box = QMessageBox()
        box.setWindowTitle(cm.APP_NAME)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("检测到 WinClipboard 连续 %d 次启动未正常完成。" % streak)
        box.setInformativeText(
            "安全模式不加载剪贴板监听、全局热键、图钉画布与自动清理,\n"
            "历史浏览、导出数据与设置恢复照常可用。\n"
            "可先正常启动;若再次失败,下次仍会提示。")
        b_safe = box.addButton("安全模式启动", QMessageBox.ButtonRole.ActionRole)
        b_norm = box.addButton("正常启动", QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(b_safe)
        box.exec()
        safe = box.clickedButton() is b_safe
        cm.log("启动方式选择: %s(连续未确认启动 %d 次)"
               % ("安全模式" if safe else "正常", streak))

    try:
        app = WinClipboardApp(qapp, safe_mode=safe)
    except Exception:
        cm.log("初始化失败:\n" + traceback.format_exc())
        try:
            cm.ctypes.windll.user32.MessageBoxW(
                0, "WinClipboard 发生错误,详情见:\n%s" % cm.LOG_FILE, cm.APP_NAME, 0x10)
        except Exception:
            pass
        return 1
    if os.environ.get("WCLB_SHOW_ON_START") == "1" and not safe:
        # 受限/无托盘环境拉起即显示主窗(图标缺失时用户没有任何入口;
        # 见 TrayWorker 图标失败降级)。延迟一拍让启动预热先走。
        from PySide6.QtCore import QTimer
        QTimer.singleShot(400, app.win.showWindow)
    rc = qapp.exec()
    cm.log("退出")
    cm.mark_clean_quit()         # 正常退出;崩溃到不了这里——这正是判据
    return rc


if __name__ == "__main__":
    sys.exit(main())
