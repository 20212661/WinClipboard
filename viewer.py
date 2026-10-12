# -*- coding: utf-8 -*-
"""内容查看器子系统(阶段 8 拆分,自 winclipboard_qt.py 迁出,行为不变):
JSON/pygments 染色管线 + 代码编辑器 + ViewerWindow(多视图 Inspector)+
DiffWindow(unified/语义 diff)+ ImageViewerWindow(内嵌 ImageEditor 编辑态)
+ INSPECTOR_ACTIONS 注册表(查看器内容专属动作)。
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
from perf_probe import PerfSpan, since, stamp

import ui_base
from ui_base import *  # noqa: F401,F403
from ui_base import ensure_onscreen, _fic
from image_editor import ImageEditor


import sys as _sys
ui_base.register(_sys.modules[__name__])   # 调色板随主题同步本模块全局



# ============================================================
# 内容查看器(文本 / 代码全量查看 · pygments 语法高亮 · JSON 美化)
# ============================================================
# QScintilla 无 PySide6 官方绑定;此处用 PySide6 社区通行方案:
# QPlainTextEdit + pygments 词法 + 自己上色(git-cola 同款思路)。
# pygments 为可选依赖,缺失时退化为纯文本查看;高亮只做一次性整篇渲染
# (文档只读且内容不变,不必用 QSyntaxHighlighter 逐块重算)。
try:
    from pygments.lexers import get_lexer_by_name as _pg_get_lexer
    from pygments.styles import get_style_by_name as _pg_get_style
except Exception:
    _pg_get_lexer = None
    _pg_get_style = None

_HL_CHAR_LIMIT = 60000         # 超过该字符数不染色(pygments 纯 Python:60KB ≈ 250ms,
                               # 旧上限 150KB 实测打开查看器可冻结 UI 超过 1 秒)


_JSON_TOKEN_RE = None       # 惰性编译:键/字符串/数字/常量 单遍正则


def _json_runs(text):
    """JSON 快速染色:pygments 的 JsonLexer 对稠密 JSON 是病态慢
    (实测 4KB 单行 ≈ 1.7s,选中预览直接冻结 UI),这里改单遍正则切 token,
    颜色仍取自 pygments 样式(Name.Tag/String/Number/Keyword.Constant),
    与其他语言的高亮观感一致。返回 runs 或 None(不染色)。"""
    global _JSON_TOKEN_RE
    if _pg_get_style is None:
        return None
    if _JSON_TOKEN_RE is None:
        import re as _re
        _JSON_TOKEN_RE = _re.compile(
            r'"(?:[^"\\]|\\.)*"(?=\s*:)'          # 键(后跟冒号)
            r'|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'   # 数字
            r'|\b(?:true|false|null)\b'            # 常量
            r'|"(?:[^"\\]|\\.)*"',                 # 字符串
        )
    try:
        from pygments.token import Token
        style = _pg_get_style("monokai" if IS_DARK else "default")
        kinds = [("key", Token.Name.Tag), ("num", Token.Number),
                 ("const", Token.Keyword.Constant),
                 ("str", Token.Literal.String.Double)]
        fmts = {}
        for name, tok in kinds:
            st = style.style_for_token(tok)
            f = QTextCharFormat()
            if st.get("color"):
                f.setForeground(QColor("#" + st["color"]))
            if st.get("bold"):
                f.setFontWeight(QFont.Bold)
            if st.get("italic"):
                f.setFontItalic(True)
            fmts[name] = f
    except Exception:
        return None
    runs = []
    for m in _JSON_TOKEN_RE.finditer(text):
        name = _json_kind(m, text)
        if name is None:
            continue
        f = fmts.get(name)
        if f is None or not f.foreground().style():
            continue
        s, e2 = m.span()
        if runs and runs[-1][1] == s and runs[-1][2] is f:
            runs[-1] = (runs[-1][0], e2, f)        # 相邻同色合并
        else:
            runs.append((s, e2, f))
    return runs


def _json_kind(m, text):
    """判定正则命中片段的类型:键=字符串且其后(跳空白)是冒号。"""
    v = m.group(0)
    if v[0] == '"':
        return "key" if _json_is_key(m, text) else "str"
    if v[0] in "-0123456789":
        return "num"
    return "const"


def _json_is_key(m, text):
    end = m.end()
    while end < len(text) and text[end] in " \t\r\n":
        end += 1
    return end < len(text) and text[end] == ":"


def pygments_runs(text, lang, force=False):
    """计算 pygments 染色 run 列表 [(start, end, QTextCharFormat)],不动文档。

    只返回有视觉效果的 run(默认样式跳过);lang == "text"、pygments 缺失、
    文本超长时返回 None。拆出本函数是为了让查看器缓存染色结果——
    原文↔代码视图来回切换时只重放 runs,不重跑词法(25KB 文本≈400ms)。
    """
    if lang == "json":
        if len(text) > _HL_CHAR_LIMIT and not force:
            return None
        return _json_runs(text)
    if lang == "text" or _pg_get_lexer is None \
            or (len(text) > _HL_CHAR_LIMIT and not force):
        return None
    try:
        lexer = _pg_get_lexer(lang)
        style = _pg_get_style("monokai" if IS_DARK else "default")
    except Exception:
        return None
    fmt_cache = {}

    def fmt_of(tok):
        hit = fmt_cache.get(tok)
        if hit is None:
            # 部分 token(如 YAML 的 Literal.Scalar.Plain)在样式表无映射,
            # 本版 pygments 的 style_for_token 会直接 KeyError(soak 实捕)——
            # 沿 Token 父链回退到最近的有样式祖先,全链缺失则按无样式。
            st = None
            t = tok
            while t is not None:
                try:
                    st = style.style_for_token(t)
                    break
                except KeyError:
                    t = t.parent
            if st is None:
                st = {}
            f = QTextCharFormat()
            if st.get("color"):
                f.setForeground(QColor("#" + st["color"]))
            if st.get("bold"):
                f.setFontWeight(QFont.Bold)
            if st.get("italic"):
                f.setFontItalic(True)
            if st.get("underline"):
                f.setFontUnderline(True)
            hit = (f, bool(st.get("color") or st.get("bold")
                           or st.get("italic") or st.get("underline")))
            fmt_cache[tok] = hit
        return hit

    toks = []           # (start, end, token) 合并相邻同 token
    pos = 0
    limit = len(text)
    for tok, val in lexer.get_tokens(text):
        n = len(val)
        if n == 0:
            continue
        end = min(pos + n, limit)
        if end <= pos:
            break
        if toks and toks[-1][2] is tok:
            toks[-1] = (toks[-1][0], end, tok)
        else:
            toks.append((pos, end, tok))
        pos = end
        if pos >= limit:
            break
    runs = []
    for s, e2, tok in toks:
        f, has_effect = fmt_of(tok)
        if has_effect:
            runs.append((s, e2, f))
    return runs


def apply_format_runs(doc, runs):
    """把 pygments_runs 的结果批量落到文档(一次性游标重放)。

    必须包在 beginEditBlock 里:单行长文本(如压缩 JSON)逐次 setCharFormat
    每次都整块重排,600 个 run 实测 1.6s;批处理推迟重排,同场景 ~9ms。
    """
    if not runs:
        return False
    cur = QTextCursor(doc)
    cur.beginEditBlock()
    for s, e2, f in runs:
        cur.setPosition(s)
        cur.setPosition(e2, QTextCursor.KeepAnchor)
        cur.setCharFormat(f)
    cur.endEditBlock()
    return True


def apply_pygments_format(doc, text, lang, force=False):
    """把 pygments 染色一次性应用到只读文档(预览面板等共用入口)。"""
    runs = pygments_runs(text, lang, force)
    return bool(runs) and apply_format_runs(doc, runs)


class _LineArea(QWidget):
    """行号栏:绘制回调交给宿主编辑器(经典 Qt 行号区模式)。"""

    def __init__(self, editor):
        super().__init__(editor)
        self._ed = editor

    def paintEvent(self, e):
        self._ed.line_area_paint(self)

    def mousePressEvent(self, e):        # 点行号 = 选中该行,顺手支持
        self._ed.line_area_click(e.pos().y())


class _CodeEdit(QPlainTextEdit):
    """只读代码区:等宽字体 + 行号栏 + 行号点击选中整行 + Fluent 细滚动条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QFrame.NoFrame)
        f = QFont()
        f.setFamilies(["Cascadia Mono", "Consolas"])   # Win11 优先,缺失自动回退
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.setFont(f)
        self._line_area = _LineArea(self)
        self.blockCountChanged.connect(self._update_area_width)
        self.updateRequest.connect(self._update_area)
        self._update_area_width(0)
        # Fluent 细滚动条替换系统粗滚动条;手柄悬停才展开
        self._scroll_del = SmoothScrollDelegate(self, useAni=True)
        for b in (self._scroll_del.vScrollBar, self._scroll_del.hScrollBar):
            b.handleDisplayMode = ScrollBarHandleDisplayMode.ON_HOVER

    def _update_area_width(self, _):
        digits = max(3, len(str(max(1, self.blockCount()))))
        w = self.fontMetrics().horizontalAdvance("9") * digits + 20
        self._line_area.resize(w, self.height())
        self.setViewportMargins(w, 12, 16, 12)

    def _update_area(self, rect, dy):
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(0, rect.y(), self._line_area.width(), rect.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # 行号栏贴住视口左缘(视口本身已被 viewportMargins 右移),上下对齐视口
        vg = self.viewport().geometry()
        self._line_area.setGeometry(vg.left() - self._line_area.width(), vg.top(),
                                    self._line_area.width(), vg.height())

    def line_area_paint(self, area):
        p = QPainter(area)
        p.fillRect(area.rect(), QColor(GUTTER_BG))
        p.setPen(QColor(FIELD_BORDER))
        p.drawLine(area.width() - 1, 0, area.width() - 1, area.height())
        blk = self.firstVisibleBlock()
        n = blk.blockNumber()
        top = round(self.blockBoundingGeometry(blk).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(blk).height())
        p.setPen(QColor(TER))
        while blk.isValid() and top <= area.height():
            if blk.isVisible():
                p.drawText(0, top, area.width() - 10, self.fontMetrics().height(),
                           Qt.AlignRight, str(n + 1))
            blk = blk.next()
            n += 1
            top = bottom
            bottom = top + round(self.blockBoundingRect(blk).height())
            if bottom == top:            # 空文档保护
                break
        p.end()

    def line_area_click(self, y):
        blk = self.cursorForPosition(QPoint(0, max(0, y))).block()
        c = self.textCursor()
        c.setPosition(blk.position())
        c.setPosition(blk.position() + blk.length() - 1, QTextCursor.KeepAnchor)
        self.setTextCursor(c)


class ViewerWindow(QWidget):
    """内容查看器(智能 Clipboard Inspector):按识别格式切换阅读视图。

    结构 = 头部「标题+已识别信息」/ 工具栏(视图切换·换行·字号·查找 →
    复制·更多) / 内容卡片(QStackedWidget:文本编辑器·JSON 结构树·
    Markdown 预览·CSV 表格 + 浮动查找条) / 底部状态栏。
    视图可用性由「识别层(cm.detect_lang) + 内容解析」决定:JSON→
    原文/格式化/结构(带有效性),Markdown→原文/预览,CSV→原文/表格,
    XML→原文/格式化,代码→原文/高亮,纯文本/文件→原文;语言可在「更多」
    菜单手动覆盖。复制始终复制原始记录。
    """

    _ZOOMS = (75, 90, 100, 110, 125, 150)
    _TREE_NODE_CAP = 3000          # 结构树总节点上限(超出部分显示省略行)
    _TREE_LAZY_DEPTH = 2           # 首次只物化到该层,更深展开时再建
    _TREE_WIDE = 400               # 超过该子项数的宽节点也走懒加载
    _TABLE_ROW_CAP = 2000

    def __init__(self, app, rec):
        super().__init__(None)
        self.app = app
        self.rec = rec
        if rec.get("type") == "files":
            self._kind = "files"
            self._raw = "\n".join(rec.get("content") or [])
        else:
            self._kind = "text"
            self._raw = str(rec.get("content") or "")
        # 内容识别管线:category/subtype/confidence(URL/JWT/Base64 等智能
        # 视图的数据源;手动改语言后智能视图让位 → 见 _mode_table)
        self._content = cm.detect_content(self._raw) if self._kind == "text" else None
        # 用户类型纠正优先于自动识别(右键「类型」设置,仅本条目生效)
        _ov = rec.get("type_override") if self._kind == "text" else None
        if _ov and self._content is not None:
            self._content = self._content._replace(subtype=_ov)
        self._auto_lang = self._content.lang if self._content is not None else "text"
        self._lang_index = 0                    # VIEW_LANGS 下标(0 = 自动识别)
        self._base_pt = 10                      # 100% 时的字号(磅)
        self._meta_suffix = ""
        self._mode = "raw"
        self._hl_cache = {}                  # (lang, 文本长度) → 染色 runs(切视图重放)
        self._ed_text = None                 # 染色编辑器当前文本(相同则跳过重灌)
        self._ed_raw_text = None             # 原文编辑器当前文本
        self._ed_hl_state = None             # 染色编辑器已呈现的 (lang, 长度)
        # 内容解析(原始文本不变,一次性算好供视图/元信息取用)
        if self._kind == "text":
            self._json_err = cm.json_error(self._raw)
            self._xml_fmt = cm.xml_pretty(self._raw)
            self._csv_delim = cm.csv_sniff(self._raw)
            self._csv_rows = cm.parse_csv_rows(
                self._raw, self._csv_delim or ",") if self._csv_delim else []
            _sub = self._content.subtype if self._content is not None else ""
            self._url_inf = cm.url_inspect(self._raw) if _sub == "web_url" else None
            self._jwt_inf = cm.jwt_inspect(self._raw) if _sub == "jwt" else None
            self._b64 = cm.b64_decode_auto(self._raw) if _sub == "base64" else None
        else:
            self._json_err = None
            self._xml_fmt = None
            self._csv_delim = None
            self._csv_rows = []
            self._url_inf = None
            self._jwt_inf = None
            self._b64 = None

        self.setWindowTitle("查看 · WinClipboard")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(960, 640)
        self.setMinimumSize(720, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 头部:标题 + 已识别信息(只放文本,不放按钮) ----
        head = QVBoxLayout()
        head.setContentsMargins(16, 14, 16, 6)
        head.setSpacing(2)
        self.lbl_title = StrongBodyLabel(
            "文件列表" if self._kind == "files" else "文本内容")
        self.lbl_meta = CaptionLabel("")
        head.addWidget(self.lbl_title)
        head.addWidget(self.lbl_meta)
        root.addLayout(head)

        # ---- 工具栏:视图切换 + 高频操作;主按钮 = 复制/打开 ----
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 2, 16, 8)
        bar.setSpacing(8)
        self.seg = SegmentedWidget()
        self.seg.currentItemChanged.connect(
            lambda k: self._set_view(k) if k else None)
        self.btn_wrap = ToggleButton("自动换行")
        if self._auto_lang == "text" and self._kind == "text":
            self.btn_wrap.setChecked(True)      # 纯文本默认自动换行
            # 注意:先设状态再接信号——此时编辑器尚未创建,toggled 会踩空
        self.btn_wrap.toggled.connect(self._set_wrap)
        self.cmb_zoom = ComboBox()
        self.cmb_zoom.addItems(["%d%%" % z for z in self._ZOOMS])
        self.cmb_zoom.setCurrentIndex(self._ZOOMS.index(100))
        self.cmb_zoom.setFixedWidth(88)
        self.cmb_zoom.currentIndexChanged.connect(
            lambda i: self._apply_zoom(self._ZOOMS[i]))
        self.btn_find = TransparentToolButton(FIF.SEARCH)
        self.btn_find.setToolTip("查找 (Ctrl+F)")
        self.btn_find.clicked.connect(self._open_find)
        if self._kind == "files":
            self.btn_copy = PushButton("复制")
            self.btn_copy.setIcon(FIF.COPY.icon())
            self.btn_open = PrimaryPushButton("打开")
            self.btn_open.setIcon(FIF.FOLDER.icon())
            self.btn_open.setToolTip("系统默认程序打开全部文件")
            self.btn_open.clicked.connect(lambda: self.app.open_files(self.rec))
        else:
            self.btn_copy = PrimaryPushButton("复制")
            self.btn_copy.setIcon(FIF.COPY.icon())
            self.btn_open = None
        self.btn_copy.setMinimumWidth(96)
        self.btn_copy.clicked.connect(self._copy_all)
        self.btn_more = TransparentToolButton(FIF.MORE)
        self.btn_more.setToolTip("更多操作")
        self.btn_more.clicked.connect(self._show_more_menu)
        bar.addWidget(self.seg)
        bar.addWidget(self.btn_wrap)
        bar.addWidget(self.cmb_zoom)
        bar.addWidget(self.btn_find)
        bar.addStretch(1)
        bar.addWidget(self.btn_copy)
        if self.btn_open is not None:
            bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_more)
        root.addLayout(bar)

        # ---- 内容卡片:多视图堆叠 + 浮动查找条 ----
        # 两个文本页:editor_raw(原文,永不染色)与 editor(格式化/代码,染色
        # 结果常驻文档)——原文↔代码切换是纯换页,不再清格式/重放 runs
        # (实测 25KB 文本重放 4800 个 run 要 230ms,这就是切换卡顿的来源)。
        _ed_qss = ("QPlainTextEdit { background: %s; border: none; color: %s;"
                   " selection-background-color: %s; }"
                   % (CARD_BG, TEXT, ACCENT_SOFT))
        body = QHBoxLayout()
        body.setContentsMargins(16, 2, 16, 10)
        self.card = SimpleCardWidget()
        cv = QVBoxLayout(self.card)
        cv.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self.card)
        self.editor_raw = _CodeEdit(self.card)
        self.editor_raw.setStyleSheet(_ed_qss)
        self.editor = _CodeEdit(self.card)
        self.editor.setStyleSheet(_ed_qss)
        self.tree = TreeWidget(self.card)
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.itemExpanded.connect(self._on_tree_expand)   # 懒加载逐层建
        self.md_view = TextEdit(self.card)
        self.md_view.setReadOnly(True)
        self.md_view.setViewportMargins(8, 8, 8, 8)
        self.table = TableWidget(self.card)
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)   # 只读
        self.table.setWordWrap(False)
        # 字段/值 信息页(URL/JWT 共用):右键或双击复制值
        self.info = TreeWidget(self.card)
        self.info.setColumnCount(2)
        self.info.setHeaderLabels(["字段", "值"])
        self.info.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.info.header().setStretchLastSection(True)
        self.info.setBorderVisible(True)
        self.info.setContextMenuPolicy(Qt.CustomContextMenu)
        self.info.customContextMenuRequested.connect(self._info_menu)
        self.info.itemDoubleClicked.connect(
            lambda it, _col: self.app.core.copy_text(str(it.data(1, Qt.UserRole) or "")))
        # Base64 解码图片页
        self.img_view = QLabel(self.card)
        self.img_view.setAlignment(Qt.AlignCenter)
        for pw in (self.editor_raw, self.editor, self.tree,
                   self.md_view, self.table, self.info, self.img_view):
            self.stack.addWidget(pw)
        SmoothScrollDelegate(self.tree, useAni=True)
        SmoothScrollDelegate(self.table, useAni=True)
        SmoothScrollDelegate(self.info, useAni=True)
        cv.addWidget(self.stack)
        body.addWidget(self.card)
        root.addLayout(body, 1)
        if self._kind == "files":
            self.editor_raw.viewport().installEventFilter(self)   # 双击行 = 打开

        self._build_find_bar()
        self._build_actions()
        self._build_statusbar()
        self._build_shortcuts()

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.setInterval(1400)
        self._copy_timer.timeout.connect(self._copy_restore)
        for ed in (self.editor, self.editor_raw):
            ed.cursorPositionChanged.connect(self._refresh_pos)
            ed.selectionChanged.connect(self._refresh_pos)
        self._tree_nodes = 0
        self._set_wrap(self.btn_wrap.isChecked())   # 初始换行态落到编辑器

        self._render()

    # ---- 局部构建 ----
    def _build_find_bar(self):
        """内容区右上角浮动查找条(默认隐藏,Ctrl+F 呼出)。"""
        self._find = QFrame(self.card)
        self._find.setObjectName("viewerFindBar")
        self._find.setStyleSheet(
            "QFrame#viewerFindBar { background: %s; border: 1px solid %s;"
            " border-radius: 6px; }" % (CARD_BG, FIELD_BORDER))
        fl = QHBoxLayout(self._find)
        fl.setContentsMargins(8, 6, 8, 6)
        fl.setSpacing(6)
        self.ed_find = SearchLineEdit()
        self.ed_find.setPlaceholderText("查找内容…")
        self.ed_find.setFixedWidth(220)
        self.ed_find.textChanged.connect(self._find_text_changed)
        self.ed_find.searchSignal.connect(lambda: self._find_goto(False))
        self.ed_find.installEventFilter(self)          # Esc 关闭
        self.lbl_find_n = CaptionLabel("0/0")
        self.lbl_find_n.setFixedWidth(44)
        self.lbl_find_n.setAlignment(Qt.AlignCenter)
        b_prev = TransparentToolButton(FIF.CARE_UP_SOLID)
        b_prev.clicked.connect(lambda: self._find_goto(True))
        b_next = TransparentToolButton(FIF.CARE_DOWN_SOLID)
        b_next.clicked.connect(lambda: self._find_goto(False))
        b_close = TransparentToolButton(FIF.CLOSE)
        b_close.clicked.connect(self._close_find)
        for w in (self.ed_find, self.lbl_find_n, b_prev, b_next, b_close):
            fl.addWidget(w)
        self._find.adjustSize()
        self._find.hide()

    def _build_actions(self):
        """「更多」菜单用的常驻 action(窗口持有,菜单随建随删)。"""
        self._lang_acts = []
        for i, (key, name) in enumerate(cm.VIEW_LANGS):
            a = QAction(name, self)
            a.setVisible(self._kind == "text")
            a.triggered.connect(lambda _=False, i=i: self._set_lang_index(i))
            self._lang_acts.append(a)
        self._act_hl = QAction("强制语法高亮", self)
        self._act_hl.setToolTip("文本较长,自动高亮已跳过;点击强制染色"
                                "(大文本会卡顿片刻)")
        self._act_hl.setVisible(False)
        self._act_hl.triggered.connect(self._force_highlight)
        self._act_reveal = QAction("在资源管理器中显示", self)
        self._act_reveal.setVisible(self._kind == "files")
        self._act_reveal.triggered.connect(lambda: self.app.reveal_first(self.rec))

    def _build_statusbar(self):
        sb = QFrame()
        sb.setObjectName("viewerStatusBar")
        sb.setFixedHeight(30)
        sb.setStyleSheet("QFrame#viewerStatusBar {"
                         " border-top: 1px solid %s; }" % LINE)
        sl = QHBoxLayout(sb)
        sl.setContentsMargins(16, 0, 16, 0)
        self.lbl_pos = CaptionLabel("")
        self.lbl_state = CaptionLabel(
            "双击行打开文件" if self._kind == "files" else "已保存到历史")
        sl.addWidget(self.lbl_pos)
        sl.addStretch(1)
        sl.addWidget(self.lbl_state)
        self.layout().addWidget(sb)
        eol = ("CRLF" if "\r\n" in self._raw else
               "CR" if "\r" in self._raw else "LF" if "\n" in self._raw else "")
        self._pos_prefix = "UTF-8 · %s" % eol if eol else "UTF-8"

    def _build_shortcuts(self):
        QShortcut(QKeySequence.Find, self).activated.connect(self._open_find)
        for seq in ("Ctrl+=", "Ctrl++"):
            QShortcut(QKeySequence(seq), self).activated.connect(
                lambda: self._zoom_step(1))
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(
            lambda: self._zoom_step(-1))
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(
            lambda: self._set_zoom(100))

    # ---- 视图模型 ----
    def _mode_table(self):
        """当前语言/内容 → (默认模式, [(key,label),…]);单视图返回空表。"""
        if self._kind != "text":
            return "raw", []
        # 智能视图(仅自动识别时;手动改语言 = 用户要按语言看,智能视图让位)
        if self._lang_index == 0 and self._content is not None:
            sub = self._content.subtype
            if sub == "web_url" and self._url_inf:
                return "url", [("raw", "原文"), ("url", "URL")]
            if sub == "jwt" and self._jwt_inf:
                return "jwt", [("raw", "原文"), ("jwt", "JWT")]
            if sub == "base64" and self._b64:
                return "b64", [("raw", "原文"), ("b64", "解码")]
        lang = self._resolved_lang()
        if lang == "json":
            if self._json_err is None:
                return "fmt", [("raw", "原文"), ("fmt", "格式化"), ("tree", "结构")]
            return "raw", [("raw", "原文")]
        if lang == "markdown":
            return "preview", [("raw", "原文"), ("preview", "预览")]
        if lang == "csv":
            if self._csv_rows:
                return "table", [("raw", "原文"), ("table", "表格")]
            return "raw", [("raw", "原文")]   # 手动强选 CSV 但嗅探不出分隔符
        if lang == "xml":
            if self._xml_fmt:
                return "fmt", [("raw", "原文"), ("fmt", "格式化")]
            return "raw", [("raw", "原文")]
        if lang == "text" or _pg_get_lexer is None:
            return "raw", []
        return "code", [("raw", "原文"), ("code", "代码")]

    def _rebuild_modes(self):
        default, modes = self._mode_table()
        if self._mode not in [k for k, _ in modes]:
            self._mode = default
        self.seg.blockSignals(True)               # 重建期间不触发视图切换
        self.seg.clear()
        for key, label in modes:
            self.seg.addItem(routeKey=key, text=label)
        self.seg.setVisible(len(modes) > 1)
        self.seg.setCurrentItem(self._mode)
        self.seg.blockSignals(False)

    def _set_view(self, key):
        if not key or key == self._mode:
            return
        self._mode = key
        self._apply_view()

    def _apply_view(self):
        """把当前模式落到堆叠页与内容(识别→选择对应查看器)。"""
        mode = self._mode
        if mode == "raw":
            self.stack.setCurrentWidget(self.editor_raw)
            if self._ed_raw_text != self._raw:
                self.editor_raw.setPlainText(self._raw)
                self._ed_raw_text = self._raw
            self._meta_suffix = ""
            self._act_hl.setVisible(False)
        elif mode in ("fmt", "code"):
            self.stack.setCurrentWidget(self.editor)
            text = self._display_text()
            if text != self._ed_text:            # 换语言/格式才重灌
                self.editor.setPlainText(text)
                self._ed_text = text
                self._ed_hl_state = None
            self._apply_highlight()
        elif mode == "tree":
            self.stack.setCurrentWidget(self.tree)
            self._build_tree()
        elif mode == "preview":
            self.stack.setCurrentWidget(self.md_view)
            self.md_view.setMarkdown(self._raw)
        elif mode == "table":
            self.stack.setCurrentWidget(self.table)
            self._build_table()
        elif mode in ("url", "jwt"):
            self.stack.setCurrentWidget(self.info)
            self._build_url_info() if mode == "url" else self._build_jwt_info()
        elif mode == "b64":
            kind, raw = self._b64 or (None, None)
            if kind == "image":
                self.stack.setCurrentWidget(self.img_view)
                self._show_b64_image(raw)
            else:
                # 解码文本落到染色编辑器(不染色:解码后语言未知)
                self.stack.setCurrentWidget(self.editor)
                text = raw.decode("utf-8", "replace")
                if text != self._ed_text:
                    self.editor.setPlainText(text)
                    self._ed_text = text
                    self._ed_hl_state = None
        self._refresh_meta()
        self._refresh_pos()

    def _active_edit(self):
        """当前可见的文本编辑器(查找/行列信息都作用在它上面)。"""
        return self.editor_raw if self.stack.currentWidget() is self.editor_raw \
            else self.editor

    def _display_text(self):
        if self._mode == "fmt":
            lang = self._resolved_lang()
            if lang == "json":
                return cm.json_reformat(self._raw) or self._raw
            if lang == "xml":
                return self._xml_fmt or self._raw
        return self._raw

    # ---- 结构树 / 表格 ----
    def _build_tree(self):
        self.tree.clear()
        self._tree_nodes = 0
        try:
            obj = json.loads(self._raw)
        except Exception:
            return
        self._fill_tree(None, "", obj, 0)
        # 只展开非懒节点:expandToDepth 会连宽节点的占位也展开,
        # 触发 _on_tree_expand 把懒加载又搬回打开时刻
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, Qt.UserRole) is None:
                top.setExpanded(True)
                for j in range(top.childCount()):
                    ch = top.child(j)
                    if ch.data(0, Qt.UserRole) is None and ch.childCount():
                        ch.setExpanded(True)

    def _fill_tree(self, parent_item, key, val, depth):
        if depth > 80:                       # 防 RecursionError
            self._tree_add(parent_item, "…(嵌套过深,已截断)")
            return
        self._tree_nodes += 1
        if isinstance(val, dict):
            pairs = [(str(k), v) for k, v in val.items()]
            label = ("%s  {%d}" % (key, len(pairs))) if key else "{ %d 项 }" % len(pairs)
            item = self._tree_add(parent_item, label)
            self._add_tree_kids(item, pairs, depth)
        elif isinstance(val, list):
            pairs = [("[%d]" % i, v) for i, v in enumerate(val)]
            label = ("%s  [%d]" % (key, len(pairs))) if key else "[ %d 项 ]" % len(pairs)
            item = self._tree_add(parent_item, label)
            self._add_tree_kids(item, pairs, depth)
        else:
            vs = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            if len(vs) > 80:
                vs = vs[:80] + "…"
            self._tree_add(parent_item, ("%s:  %s" % (key, vs)) if key else vs)

    def _add_tree_kids(self, item, pairs, depth):
        """子级物化规则:浅层小节点直接建;深层或宽节点(>400 项)挂占位,
        展开时再建——保证大 JSON 打开即显,构建成本摊到用户点击时。"""
        if not pairs:
            return
        if depth + 1 > self._TREE_LAZY_DEPTH or len(pairs) > self._TREE_WIDE:
            self._tree_add(item, "… 共 %d 项(展开加载)" % len(pairs))
            item.setData(0, Qt.UserRole, pairs)
            return
        for k, v in pairs:
            if self._tree_nodes >= self._TREE_NODE_CAP:
                self._tree_add(item, "… 其余已省略")
                return
            self._fill_tree(item, k, v, depth + 1)

    def _on_tree_expand(self, item):
        """展开带占位的节点:一次性物化其子级(受总节点上限约束)。"""
        pairs = item.data(0, Qt.UserRole)
        if not pairs:
            return
        item.setData(0, Qt.UserRole, None)
        if item.childCount():
            item.removeChild(item.child(0))          # 去掉占位行
        depth = 0
        p = item.parent()
        while p is not None:
            depth += 1
            p = p.parent()
        for k, v in pairs:
            if self._tree_nodes >= self._TREE_NODE_CAP:
                self._tree_add(item, "… 其余已省略")
                return
            self._fill_tree(item, k, v, depth + 1)

    def _tree_add(self, parent_item, text):
        it = QTreeWidgetItem([text])
        if parent_item is None:
            self.tree.addTopLevelItem(it)
        else:
            parent_item.addChild(it)
        return it

    def _build_table(self):
        self.table.setUpdatesEnabled(False)          # 批量填充不逐行重绘
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        rows = self._csv_rows
        if rows:
            header, data = rows[0], rows[1:]
            ncol = max(len(r) for r in rows)
            self.table.setColumnCount(ncol)
            self.table.setHorizontalHeaderLabels(
                [header[i] if i < len(header) else "" for i in range(ncol)])
            self.table.setRowCount(min(len(data), self._TABLE_ROW_CAP))
            for r, row in enumerate(data[:self._TABLE_ROW_CAP]):
                for c in range(ncol):
                    self.table.setItem(
                        r, c, QTableWidgetItem(row[c] if c < len(row) else ""))
            # 采样列宽(表头+前 60 行):全量 ResizeToContents 对 2000 行要几百 ms
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            fm = self.table.fontMetrics()
            for c in range(ncol):
                w = fm.horizontalAdvance(header[c] if c < len(header) else "")
                for rr in range(min(60, self.table.rowCount())):
                    it = self.table.item(rr, c)
                    if it is not None:
                        w = max(w, fm.horizontalAdvance(it.text()))
                self.table.setColumnWidth(c, min(w + 28, 400))
        self.table.setUpdatesEnabled(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    # ---- URL / JWT / Base64 Inspector ----
    def _info_add(self, field, value, parent=None, full=None):
        """加一行「字段/值」;超长值显示截断,完整值存 UserRole 供复制。"""
        shown = value if value is not None else ""
        if len(shown) > 160:
            shown = shown[:160] + "…"
        it = QTreeWidgetItem([field, shown])
        it.setData(1, Qt.UserRole, value if full is None else full)
        it.setToolTip(1, shown)
        if parent is None:
            self.info.addTopLevelItem(it)
        else:
            parent.addChild(it)
        return it

    def _build_url_info(self):
        inf = self._url_inf or {}
        self.info.clear()
        self._info_add("Scheme", inf.get("scheme") or "")
        self._info_add("主机", inf.get("host") or "")
        if inf.get("port"):
            self._info_add("端口", str(inf["port"]))
        self._info_add("路径", inf.get("path") or "/")
        q = inf.get("query") or []
        if q:
            g = self._info_add("Query 参数", "%d 项" % len(q))
            for k, v in q:
                self._info_add(k or "(空)", v, g)
        if inf.get("fragment"):
            self._info_add("Fragment", inf["fragment"])

    def _build_jwt_info(self):
        inf = self._jwt_inf or {}
        self.info.clear()
        head, pay = inf.get("header") or {}, inf.get("payload") or {}
        self._info_add("算法 (alg)", str(head.get("alg", "?")))
        warn = self._info_add("签名", "未验证")
        warn.setForeground(1, QColor("#cf9a00"))
        warn.setToolTip(1, "仅解码展示:没有密钥,无法判定令牌真伪")
        g = self._info_add("Header", "%d 个字段" % len(head))
        for k, v in head.items():
            self._info_add(str(k), v if isinstance(v, str)
                           else json.dumps(v, ensure_ascii=False), g)
        g = self._info_add("Payload", "%d 个字段" % len(pay))
        for k, v in pay.items():
            if isinstance(v, str):
                vs = v
            else:
                vs = json.dumps(v, ensure_ascii=False)
            self._info_add(str(k), vs, g)
        for label, key in (("签发时间 (iat)", "iat"), ("生效时间 (nbf)", "nbf"),
                           ("过期时间 (exp)", "exp")):
            ts = inf.get(key)
            if ts:
                self._info_add(label, time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(ts)))
        exp = inf.get("exp")
        if exp:
            left = exp - time.time()
            if left <= 0:
                it = self._info_add("状态", "已过期")
                it.setForeground(1, QColor("#e8493f"))
            else:
                hrs = left // 3600
                self._info_add("状态", "约 %s 后过期" % (
                    "%.1f 小时" % hrs if hrs >= 1 else "%d 分钟" % max(1, left // 60)))
        else:
            self._info_add("状态", "无 exp 字段(视为不过期)")

    def _show_b64_image(self, raw):
        img = QImage.fromData(raw)
        if img.isNull():
            self.img_view.setText("(解码出图像魔数,但无法解析像素)")
            self.img_view.setPixmap(QPixmap())
            return
        pm = QPixmap.fromImage(img)
        if pm.width() > 1600 or pm.height() > 1200:
            pm = pm.scaled(1600, 1200, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        self.img_view.setPixmap(pm)
        self.img_view.setToolTip("原始尺寸 %d × %d" % (img.width(), img.height()))

    def _info_menu(self, pos):
        it = self.info.itemAt(pos)
        if it is None:
            return
        val = str(it.data(1, Qt.UserRole) or "")
        field = it.text(0)
        m = RoundMenu(parent=self.info)
        a1 = QAction("复制「%s」" % field, m)
        a1.triggered.connect(lambda _=False, v=val: self.app.core.copy_text(v))
        m.addAction(a1)
        if val and len(val) > 160:
            a2 = QAction("复制完整值(%d 字符)" % len(val), m)
            a2.triggered.connect(lambda _=False, v=val: self.app.core.copy_text(v))
            m.addAction(a2)
        m.exec(self.info.viewport().mapToGlobal(pos))
        m.deleteLater()

    def _inspector_actions(self):
        """URL/JWT/Base64 的内容专属动作:INSPECTOR_ACTIONS 注册表按
        subtype 过滤(返回 [(标题, 回调)])。"""
        if self._lang_index != 0 or self._content is None:
            return []
        sub = self._content.subtype
        out = []
        for st, title, run, guard in INSPECTOR_ACTIONS:
            if st == sub and (guard is None or guard(self)):
                out.append((title, lambda r=run: r(self)))
        return out

    def _save_b64_image(self):
        kind, raw = self._b64 or (None, None)
        if not raw:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "解码图另存为", "clipboard_decoded.png", "PNG 图片 (*.png)")
        if not path:
            return
        try:
            Path(path).write_bytes(raw)
        except Exception:
            MessageBox("WinClipboard", "保存失败。", self).exec()


    # ---- 显示与渲染 ----
    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)
        self._place_find()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_find()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self._find.isVisible():
                self._close_find()
            else:
                self.close()
            return
        super().keyPressEvent(e)

    def _resolved_lang(self):
        key = cm.VIEW_LANGS[self._lang_index][0]
        return self._auto_lang if key == "auto" else key

    def _set_lang_index(self, i):
        if i != self._lang_index:
            self._lang_index = i
            # 语言/自动档切换必然换模式表:直接跳默认视图(URL/JWT 等
            # 智能视图的 lang 恒为 text,靠 _render 的语言变化检测捕捉不到)
            self._mode = self._mode_table()[0]
            self._render()

    def _set_lang_key(self, key):
        """按 VIEW_LANGS key 选语言(测试/内部调用入口)。"""
        self._set_lang_index([k for k, _ in cm.VIEW_LANGS].index(key))

    def _render(self):
        # 换格式(语言)时跳到该格式的默认视图;同一格式下保留用户当前视图
        lang = self._resolved_lang()
        if lang != getattr(self, "_shown_lang", None):
            self._mode = self._mode_table()[0]
            self._shown_lang = lang
        self._rebuild_modes()
        self._apply_view()

    def _refresh_meta(self):
        if self._kind == "files":
            base = "%d 个文件" % len(self.rec.get("content") or [])
        else:
            size = cm._fmt_size(len(self._raw.encode("utf-8")))
            lang = self._resolved_lang()
            if lang == "json":
                valid = "无效(%s)" % self._json_err if self._json_err else "有效"
                base = "已识别:JSON · %s · %d 行 · %s" % (
                    valid, self._raw.count("\n") + 1, size)
            elif lang == "csv" and self._csv_rows:
                nrow = len(self._csv_rows)
                shown = "≥%d" % self._TABLE_ROW_CAP \
                    if nrow > self._TABLE_ROW_CAP else str(nrow)
                ncol = max(len(r) for r in self._csv_rows)
                base = "已识别:CSV 表格 · %s 行 × %d 列 · %s" % (shown, ncol, size)
            elif self._lang_index == 0 and self._content is not None and \
                    self._content.subtype == "web_url" and self._url_inf:
                base = "已识别:URL · %s · %d 行 · %s" % (
                    self._url_inf["host"], self._raw.count("\n") + 1, size)
            elif self._lang_index == 0 and self._content is not None and \
                    self._content.subtype == "jwt" and self._jwt_inf:
                base = "已识别:JWT · 仅解码,签名未验证 · %s" % size
            elif self._lang_index == 0 and self._content is not None and \
                    self._content.subtype == "base64" and self._b64:
                base = "已识别:Base64 · 解码为%s(%s) · %s" % (
                    "图片" if self._b64[0] == "image" else "文本",
                    cm._fmt_size(len(self._b64[1])), size)
            else:
                base = "已识别:%s · %d 行 · %s" % (
                    self._lang_name(), self._raw.count("\n") + 1, size)
        ts = self.rec.get("ts")
        if ts:
            t = time.localtime(ts)
            now = time.localtime()
            base += " · " + (time.strftime("%H:%M", t) if
                             (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday)
                             else time.strftime("%m-%d %H:%M", t))
        self.lbl_meta.setText(base + self._meta_suffix)

    def _refresh_pos(self):
        if self._kind != "text":
            self.lbl_pos.setText(self._editor_pos_info())
            return
        prefix = self._pos_prefix + " · " + self._lang_name()
        lang = self._resolved_lang()
        if lang == "json":
            prefix += " · " + ("无效" if self._json_err else "有效")
        cur = self.stack.currentWidget()
        if cur is self.tree:
            info = "%d 个节点" % self._tree_nodes
        elif cur is self.table and self._csv_rows:
            info = "%d 行 × %d 列" % (
                min(len(self._csv_rows), self._TABLE_ROW_CAP + 1),
                max(len(r) for r in self._csv_rows))
        elif cur is self.md_view:
            info = "预览"
        elif cur is self.info:
            info = "%d 行" % self.info.topLevelItemCount()
        elif cur is self.img_view:
            info = "图像"
        else:
            info = self._editor_pos_info()
        self.lbl_pos.setText("%s · %s" % (prefix, info))

    def _editor_pos_info(self):
        c = self._active_edit().textCursor()
        if c.hasSelection():
            return "已选择 %d 个字符" % len(c.selectedText())
        return "Ln %d, Col %d" % (c.blockNumber() + 1, c.positionInBlock() + 1)

    def eventFilter(self, obj, e):
        if obj is self.ed_find and e.type() == QEvent.KeyPress \
                and e.key() == Qt.Key_Escape:
            self._close_find()
            return True
        if obj is self.editor_raw.viewport() \
                and e.type() == QEvent.MouseButtonDblClick:
            self._open_path_line(self.editor_raw.cursorForPosition(
                e.position().toPoint()).block().text())
            return True
        return super().eventFilter(obj, e)

    def _open_path_line(self, line):
        p = line.strip().strip('"')
        if p and os.path.exists(p):
            cm.open_file_default(p)

    def _lang_name(self):
        key = self._resolved_lang()
        return dict(cm.VIEW_LANGS).get(key, key)

    def _apply_highlight(self):
        """染色到当前文档:文档已按该 (lang, 长度) 染过就直接显示(0 成本);
        未染过才跑词法(25KB≈110ms)+ 落 runs(≈230ms),染完常驻文档。"""
        doc = self.editor.document()
        lang = self._resolved_lang()
        text = self.editor.toPlainText()
        key = (lang, len(text))
        if self._ed_hl_state == key:
            return                                  # 原文↔代码来回切不再重染
        runs = self._hl_cache.get(key)
        if runs is None:
            if lang != "text" and _pg_get_lexer is not None \
                    and len(text) > _HL_CHAR_LIMIT:
                self._act_hl.setVisible(True)       # 超长:菜单留强制入口
                self._meta_suffix = " · 文本过长,未高亮"
                self._refresh_meta()
                return
            runs = pygments_runs(text, lang) or []
            self._hl_cache[key] = runs
        self._ed_hl_state = key
        self._act_hl.setVisible(False)
        self._meta_suffix = ""
        apply_format_runs(doc, runs)                # 新灌的文本无需先清格式
        self._refresh_meta()

    def _force_highlight(self):
        """超长文本用户主动要求染色:接受一次性耗时(几百 KB 可达秒级)。"""
        self._act_hl.setVisible(False)
        text = self.editor.toPlainText()
        lang = self._resolved_lang()
        runs = pygments_runs(text, lang, force=True)
        if runs:
            self._hl_cache[(lang, len(text))] = runs
            self._ed_hl_state = (lang, len(text))
            apply_format_runs(self.editor.document(), runs)
            self._meta_suffix = " · 已高亮"
            self._refresh_meta()

    # ---- 更多菜单 ----
    def _show_more_menu(self):
        m = RoundMenu(parent=self)
        if self._kind == "text":
            sub = RoundMenu("语言", self)
            for i, a in enumerate(self._lang_acts):
                a.setIcon(FIF.ACCEPT.icon() if i == self._lang_index else QIcon())
                sub.addAction(a)
            m.addMenu(sub)
            if self._act_hl.isVisible():
                m.addSeparator()
                m.addAction(self._act_hl)
            insp = self._inspector_actions()
            if insp:
                m.addSeparator()
                for title, cb in insp:
                    a = QAction(title, m)
                    a.triggered.connect(lambda _=False, f=cb: f())
                    m.addAction(a)
        else:
            m.addAction(self._act_reveal)
        m.exec(self.btn_more.mapToGlobal(
            QPoint(self.btn_more.width() // 2, self.btn_more.height() + 8)))
        m.deleteLater()

    # ---- 查找 ----
    def _open_find(self):
        if self.stack.currentWidget() not in (self.editor, self.editor_raw):
            self._set_view("raw")                   # 查找定位在文本视图
        if self._find.isVisible():
            self.ed_find.setFocus()
            self.ed_find.selectAll()
            return
        self._find.show()
        self._find.raise_()
        self._place_find()
        self.ed_find.setFocus()
        self._find_text_changed(self.ed_find.text())

    def _close_find(self):
        self._find.hide()
        self._active_edit().setFocus()

    def _place_find(self):
        if not self._find.isVisible():
            return
        self._find.move(self.card.width() - self._find.width() - 12, 10)

    def _find_matches(self):
        """当前关键词的全部匹配起点(非重叠);超大文档放弃实时计数。"""
        text = self.ed_find.text()
        if not text:
            return None
        doc = self._active_edit().document()
        if doc.characterCount() > 300000:
            return None
        out = []
        c = doc.find(text, QTextCursor(doc))
        while not c.isNull():
            out.append(c.selectionStart())
            nxt = doc.find(text, c)
            if nxt.isNull() or nxt.position() <= c.position():
                break
            c = nxt
        return out

    def _find_text_changed(self, _):
        ms = self._find_matches()
        if ms is None:
            self.lbl_find_n.setText("-/-")
            return
        cur = self._active_edit().textCursor().position()
        idx = 0
        while idx < len(ms) and ms[idx] < cur:
            idx += 1
        self.lbl_find_n.setText("%d/%d" % (min(idx + 1, len(ms)) if ms else 0,
                                           len(ms)))

    def _find_goto(self, back=False):
        text = self.ed_find.text()
        if not text:
            return
        ed = self._active_edit()
        flags = (QTextDocument.FindFlag.FindBackward if back
                 else QTextDocument.FindFlag(0))
        if not ed.find(text, flags):                # 到头/尾回绕
            c = ed.textCursor()
            c.movePosition(QTextCursor.End if back else QTextCursor.Start)
            ed.setTextCursor(c)
            ed.find(text, flags)
        self._find_text_changed("")

    # ---- 工具 ----
    def _set_wrap(self, on):
        for ed in (self.editor, self.editor_raw):
            ed.setLineWrapMode(
                QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)

    def _apply_zoom(self, pct):
        pt = max(6, round(self._base_pt * pct / 100))
        for w in (self.editor, self.editor_raw, self.md_view,
                  self.tree, self.table, self.info):
            f = w.font()
            f.setPointSize(pt)
            w.setFont(f)
        self.editor._update_area_width(0)
        self.editor_raw._update_area_width(0)

    def _set_zoom(self, pct):
        self.cmb_zoom.setCurrentIndex(self._ZOOMS.index(pct))
        self._apply_zoom(pct)                       # 索引未变时信号不来,手动兜底

    def _zoom_step(self, d):
        i = self.cmb_zoom.currentIndex() + d
        self._set_zoom(self._ZOOMS[max(0, min(len(self._ZOOMS) - 1, i))])

    def _copy_all(self):
        # 查看器「复制」只写剪贴板:不排回贴队列,避免内容被 Ctrl+V
        # 打进打开查看器之前聚焦的窗口(私密凭据与非私密一视同仁)
        if not self.app.core.copy_text(self._raw):
            InfoBar.error("复制失败", "剪贴板写入失败,请重试", parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=5000)
            return
        self.btn_copy.setText("已复制")             # 按钮自身反馈,不弹提示层
        self.btn_copy.setIcon(FIF.ACCEPT.icon())
        self._copy_timer.start()

    def _copy_restore(self):
        self.btn_copy.setText("复制")
        self.btn_copy.setIcon(FIF.COPY.icon())

    def closeEvent(self, e):
        try:
            self.app._viewers.remove(self)
        except ValueError:
            pass
        self.deleteLater()
        e.accept()


class DiffWindow(QWidget):
    """内容对比窗口:unified 文本 diff + JSON 语义 diff(键序无关)。

    入口:历史面板 Ctrl+点击选基准 → Ctrl+点击另一条;或卡片菜单
    「选为对比基准 / 与基准对比」。A=基准(旧),B=所选(新);JSON 双方
    都合法时默认语义视图,否则回退文本 unified diff。"""

    def __init__(self, app, rec_a, rec_b):
        super().__init__(None)
        self.app = app
        self.ra, self.rb = rec_a, rec_b
        self.ta = self._text_of(rec_a)
        self.tb = self._text_of(rec_b)
        self._json_ok, self._json_changes = cm.json_semantic_diff(self.ta, self.tb)
        self._diff_lines = cm.unified_diff_lines(self.ta, self.tb)

        self.setWindowTitle("对比 · WinClipboard")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(920, 620)
        self.setMinimumSize(660, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QVBoxLayout()
        head.setContentsMargins(16, 14, 16, 6)
        head.setSpacing(2)
        head.addWidget(StrongBodyLabel("内容对比"))
        self.lbl_meta = CaptionLabel("")
        head.addWidget(self.lbl_meta)
        root.addLayout(head)

        bar = QHBoxLayout()
        bar.setContentsMargins(16, 2, 16, 8)
        bar.setSpacing(8)
        self.seg = SegmentedWidget()
        self.seg.currentItemChanged.connect(
            lambda k: self._set_view(k) if k else None)
        bar.addWidget(self.seg)
        bar.addStretch(1)
        self.btn_copy = PushButton("复制结果")
        self.btn_copy.setIcon(FIF.COPY.icon())
        self.btn_copy.clicked.connect(self._copy_result)
        bar.addWidget(self.btn_copy)
        root.addLayout(bar)

        body = QHBoxLayout()
        body.setContentsMargins(16, 2, 16, 10)
        card = SimpleCardWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 0, 0, 0)
        self.ed = _CodeEdit(card)
        self.ed.setReadOnly(True)
        self.ed.setStyleSheet(
            "QPlainTextEdit { background: %s; border: none; color: %s; }"
            % (CARD_BG, TEXT))
        cv.addWidget(self.ed)
        body.addWidget(card)
        root.addLayout(body, 1)

        self._mode = "sem" if self._json_ok else "text"
        self._rebuild_modes()
        self._render()

    @staticmethod
    def _text_of(rec):
        c = rec.get("content")
        if isinstance(c, list):
            return "\n".join(str(p) for p in c)
        return str(c or "")

    def _stamp(self, rec):
        ts = rec.get("ts")
        if not ts:
            return "?"
        t = time.localtime(ts)
        return time.strftime("%m-%d %H:%M", t)

    def _rebuild_modes(self):
        self.seg.blockSignals(True)
        self.seg.clear()
        if self._json_ok:
            self.seg.addItem(routeKey="sem", text="语义")
        self.seg.addItem(routeKey="text", text="文本")
        self.seg.setCurrentItem(self._mode)
        self.seg.blockSignals(False)

    def _set_view(self, key):
        if key and key != self._mode:
            self._mode = key
            self._render()

    def _sem_lines(self):
        ch = self._json_changes
        if not ch:
            return ["两个 JSON 语义相同(键顺序与空白无关)。"]
        lines = []
        for path, kind, old, new in ch:
            if kind == "change":
                lines.append("~ %s:  %s  →  %s" % (path, old, new))
            elif kind == "add":
                lines.append("+ %s:  %s" % (path, new))
            elif kind == "del":
                lines.append("- %s:  %s" % (path, old))
            else:
                lines.append(str(old))
        return lines

    def _render(self):
        lines = self._sem_lines() if self._mode == "sem" else self._diff_lines
        text = "\n".join(lines)
        self.ed.setPlainText(text)
        # 逐行配色:前缀 +/- 上色,@@ 与 ---/+++ 说明行置灰
        fa = QTextCharFormat()
        fa.setForeground(QColor(DIFF_ADD))
        fd = QTextCharFormat()
        fd.setForeground(QColor(DIFF_DEL))
        fc = QTextCharFormat()
        fc.setForeground(QColor(DIFF_CTX))
        runs = []
        pos = 0
        for ln in lines:
            n = len(ln) + 1
            c = ln[:1]
            if c == "+":
                runs.append((pos, pos + n, fa))
            elif c == "-":
                runs.append((pos, pos + n, fd))
            elif c in ("~", "@", "…") or ln.startswith(("---", "+++")):
                runs.append((pos, pos + n, fc))
            pos += n
        apply_format_runs(self.ed.document(), runs)
        n_add = sum(1 for l in self._diff_lines if l.startswith("+") and not l.startswith("+++"))
        n_del = sum(1 for l in self._diff_lines if l.startswith("-") and not l.startswith("---"))
        if self._mode == "sem":
            detail = "语义差异 %d 处" % len(self._json_changes) \
                if self._json_changes else "语义相同"
        else:
            detail = "+%d / -%d 行" % (n_add, n_del)
        self.lbl_meta.setText(
            "A(基准) %s · %s  ↔  B(所选) %s · %s · %s" % (
                self._stamp(self.ra), cm._fmt_size(len(self.ta.encode("utf-8"))),
                self._stamp(self.rb), cm._fmt_size(len(self.tb.encode("utf-8"))),
                detail))

    def _copy_result(self):
        if not self.app.core.copy_text(self.ed.toPlainText()):
            InfoBar.error("复制失败", "剪贴板写入失败,请重试", parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=5000)
            return
        self.btn_copy.setText("已复制")
        QTimer.singleShot(1400, lambda: self.btn_copy.setText("复制结果"))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        try:
            self.app._diffs.remove(self)
        except (ValueError, AttributeError):
            pass
        self.deleteLater()
        e.accept()


class ImageViewerWindow(QWidget):
    """图片查看器(独立窗口,与文本查看器同族):适应窗口 / 1:1 / 滚轮缩放 /
    拖拽平移;双击在 适应↔原始 间切换。「编辑标注」在本窗口内切换到内嵌
    编辑器(不另开窗口),完成/Esc 自动回到查看态。"""

    def __init__(self, app, rec, edit=False, on_result=None,
                 annos=None, layer_mode=False):
        super().__init__(None)
        self.app = app
        self.rec = rec
        self._on_result = on_result       # 编辑产物直通回调(图钉画布回写)
        self._init_annos = annos          # 初始标注层(图钉画布回编继续编辑)
        self._layer_mode = layer_mode     # 非破坏回写(emit 标注层而非像素)
        self._editor = None               # 内嵌编辑器(编辑态非 None)
        path = str(rec.get("content") or "")
        pm = QPixmap(path)
        if pm.isNull():
            raise ValueError("无法加载图片: %s" % path)
        self._pm = pm

        self.setWindowTitle("图片查看 · WinClipboard")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(880, 620)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)
        self.v = v

        # ---- 头部:统计 + 缩放控制 + 动作 ----
        head = QHBoxLayout()
        head.setSpacing(6)
        self.lbl_title = StrongBodyLabel("图片查看")
        self.lbl_title.setFont(font(15, QFont.DemiBold))
        size_str = ""
        try:
            size_str = cm._fmt_size(os.path.getsize(path))
        except Exception:
            pass
        ext = os.path.splitext(path)[1].lstrip(".").upper() or "IMG"
        self.lbl_stats = CaptionLabel("%d × %d · %s · %s"
                                      % (pm.width(), pm.height(), size_str, ext))
        self.lbl_stats.setStyleSheet("color: %s;" % TER)
        self.lbl_zoom = CaptionLabel("适应")
        self.lbl_zoom.setStyleSheet("color: %s;" % SEC)
        self.b_out = PushButton("−")
        self.b_out.setFixedWidth(40)
        self.b_out.setToolTip("缩小(滚轮/加减号)")
        self.b_fit = PushButton("适应窗口")
        self.b_fit.setCheckable(True)
        self.b_fit.setChecked(True)
        self.b_fit.setToolTip("缩放到完整可见(快捷键 0)")
        self.b_100 = PushButton("1:1")
        self.b_100.setToolTip("原始像素(快捷键 1)")
        self.b_in = PushButton("+")
        self.b_in.setFixedWidth(40)
        self.b_in.setToolTip("放大(滚轮/加减号)")
        b_copy = PushButton("复制")
        b_copy.setIcon(FIF.COPY.icon())
        b_pin = PushButton("置顶")
        b_pin.setIcon(FIF.PIN.icon())
        b_pin.setToolTip("钉到屏幕置顶画布:可多图自由拖动 / 缩放 / 排列")
        b_pin.clicked.connect(self._pin)
        self.b_edit = PushButton("✏ 编辑标注")
        self.b_edit.setIcon(FIF.EDIT.icon())
        self.b_edit.setToolTip("在本窗口内标注(矩形/圆形/箭头/画笔/文字/马赛克),"
                               "完成后回到查看")
        b_close = PushButton("关闭")
        b_close.setIcon(FIF.CLOSE.icon())
        for w in (self.lbl_title, self.lbl_stats, self.lbl_zoom):
            head.addWidget(w)
        self.lbl_title.setStyleSheet("color: %s;" % TEXT)
        head.addSpacing(8)
        head.addStretch(1)
        for w in (self.b_out, self.b_fit, self.b_100, self.b_in,
                  b_copy, b_pin, self.b_edit, b_close):
            head.addWidget(w)
        v.addLayout(head)

        # ---- 画布 ----
        self.view = QGraphicsView()
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setStyleSheet(
            "QGraphicsView { background: #26292F; border: none; border-radius: 10px; }")
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)   # 放大后拖拽平移
        self.scene = QGraphicsScene(self)
        self._pix = QGraphicsPixmapItem(pm)
        self._pix.setTransformationMode(Qt.SmoothTransformation)
        self.scene.addItem(self._pix)
        self.scene.setSceneRect(QRectF(0, 0, pm.width(), pm.height()))
        self.view.setScene(self.scene)
        v.addWidget(self.view, 1)

        self._fitting = True
        self.b_out.clicked.connect(lambda: self._zoom_by(0.8))
        self.b_in.clicked.connect(lambda: self._zoom_by(1.25))
        self.b_fit.clicked.connect(lambda on: self._do_fit() if on else self._do_100())
        self.b_100.clicked.connect(self._do_100)
        b_copy.clicked.connect(self._copy)
        self.b_edit.clicked.connect(self._toggle_edit)
        b_close.clicked.connect(self.close)
        self.view.wheelEvent = self._wheel
        self.view.mouseDoubleClickEvent = self._dbl
        if edit:
            self._enter_edit()

    # ---- 查看 ↔ 编辑(同窗切换,不另开窗口) ----
    def _toggle_edit(self):
        self._exit_edit() if self._editor is not None else self._enter_edit()

    def _enter_edit(self):
        if self._editor is not None:
            return
        ed = ImageEditor(self.app, {"type": "image", "content": self.rec["content"],
                                    "w": self._pm.width(), "h": self._pm.height()},
                         on_result=self._edited, parent=self,
                         annos=self._init_annos, layer_mode=self._layer_mode)
        ed.finished.connect(self._exit_edit)
        self._editor = ed
        self.v.replaceWidget(self.view, ed)
        self.view.hide()          # replaceWidget 不隐藏旧控件,会叠加渲染
        ed.show()
        ed.setFocus()
        for w in (self.b_out, self.b_fit, self.b_100, self.b_in, self.lbl_zoom):
            w.hide()
        self.b_edit.setText("退出编辑")
        self.lbl_title.setText("图片编辑")

    def _exit_edit(self):
        ed = self._editor
        if ed is None:
            return
        self._editor = None
        try:
            ed.finished.disconnect(self._exit_edit)
        except Exception:
            pass
        self.v.replaceWidget(ed, self.view)
        ed.hide()                 # 同上:先藏再删,避免 deleteLater 前残留渲染
        ed.deleteLater()
        self.view.show()
        for w in (self.b_out, self.b_fit, self.b_100, self.b_in, self.lbl_zoom):
            w.show()
        self.b_edit.setText("✏ 编辑标注")
        self.lbl_title.setText("图片查看")
        self.view.setFocus()

    def _edited(self, result):
        """编辑产物直通(画布回写等);历史场景产物经剪贴板自动回流。"""
        if self._on_result is None:
            return
        try:
            self._on_result(result)
        except Exception:
            cm.log("查看器编辑结果回写失败:\n%s" % traceback.format_exc())

    # ---- 显示 / 缩放 ----
    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)
        QTimer.singleShot(0, self._do_fit)   # 需要最终窗口尺寸

    def _do_fit(self):
        self.view.resetTransform()
        self.view.fitInView(self._pix, Qt.KeepAspectRatio)
        if self.view.transform().m11() > 1.0:   # 小图不放大到模糊,封顶 100%
            self.view.resetTransform()
        self._fitting = True
        self.b_fit.setChecked(True)
        self._sync()

    def _do_100(self):
        self.view.resetTransform()
        self._fitting = False
        self.b_fit.setChecked(False)
        self._sync()

    def _zoom_by(self, f, anchor_mouse=False):
        cur = self.view.transform().m11()
        if not 0.02 <= cur * f <= 40:
            return
        self._fitting = False
        self.b_fit.setChecked(False)
        self.view.setTransformationAnchor(
            QGraphicsView.AnchorUnderMouse if anchor_mouse
            else QGraphicsView.AnchorViewCenter)
        self.view.scale(f, f)
        self._sync()

    def _wheel(self, e):
        self._zoom_by(1.25 if e.angleDelta().y() > 0 else 0.8, anchor_mouse=True)

    def _dbl(self, e):
        self._do_100() if self._fitting else self._do_fit()

    def _sync(self):
        s = self.view.transform().m11()
        self.lbl_zoom.setText("适应" if self._fitting else "%d%%" % round(s * 100))

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            if self._editor is not None:
                self._exit_edit()     # 编辑态:先退编辑,再按才关窗
            else:
                self.close()
        elif self._editor is not None:
            super().keyPressEvent(e)  # 编辑态让快捷键(Delete/Ctrl+Z)进编辑器
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom_by(1.25)
        elif k == Qt.Key_Minus:
            self._zoom_by(0.8)
        elif k == Qt.Key_0:
            self._do_fit()
        elif k == Qt.Key_1:
            self._do_100()
        else:
            super().keyPressEvent(e)

    # ---- 动作 ----
    def _copy(self):
        try:
            img = cm.Image.open(self.rec["content"])
            ok = cm.set_dib_to_clipboard(self.app.worker.hwnd, cm.pil_to_dib(img))
        except Exception:
            ok = False
        if ok:
            InfoBar.success("已复制", "图片已复制到剪贴板", parent=self,
                            position=InfoBarPosition.BOTTOM_LEFT)
        else:
            InfoBar.error("复制失败", "剪贴板被占用", parent=self,
                          position=InfoBarPosition.BOTTOM_LEFT)

    def _pin(self):
        # 钉到置顶画布(预览替身没有 pin_image,静默跳过)
        f = getattr(self.app, "pin_image", None)
        if f is not None:
            f(self.rec)

    def closeEvent(self, e):
        try:
            self.app._viewers.remove(self)
        except ValueError:
            pass
        self.deleteLater()
        e.accept()





def _copy_or_warn(app, text, parent=None):
    """查看器/树/查找结果等小件复制的统一出口:成功保持安静,失败如实
    提示(剪贴板被占用时不静默丢内容)。返回是否写入成功。"""
    if app.core.copy_text(text):
        return True
    InfoBar.error("复制失败", "剪贴板写入失败,请重试",
                  parent=parent or app.win,
                  position=InfoBarPosition.TOP_RIGHT, duration=5000)
    return False


# 查看器内容专属动作:(subtype, 标题, run(viewer), 额外门槛(viewer)→bool|None)
INSPECTOR_ACTIONS = [
    ("web_url", "在浏览器打开", lambda v: cm.open_url(v._raw.strip()), None),
    ("web_url", "复制主机名",
     lambda v: _copy_or_warn(v.app, v._url_inf["host"], v), None),
    ("web_url", "复制路径",
     lambda v: _copy_or_warn(v.app, v._url_inf["path"], v), None),
    ("web_url", "复制参数串",
     lambda v: _copy_or_warn(v.app, urlsplit(v._raw.strip()).query, v),
     lambda v: bool(v._url_inf["query"])),
    ("web_url", "URL 解码并复制",
     lambda v: _copy_or_warn(v.app, _unquote(v._raw.strip()), v), None),
    ("web_url", "URL 编码并复制",
     lambda v: _copy_or_warn(v.app, _quote(v._raw.strip(), safe=":/?&=#%+"), v),
     None),
    ("jwt", "复制 Header (JSON)",
     lambda v: _copy_or_warn(
         v.app, json.dumps(v._jwt_inf["header"], ensure_ascii=False, indent=2), v),
     None),
    ("jwt", "复制 Payload (JSON)",
     lambda v: _copy_or_warn(
         v.app, json.dumps(v._jwt_inf["payload"], ensure_ascii=False, indent=2), v),
     None),
    ("jwt", "复制解码全文 (Header.Payload)",
     lambda v: _copy_or_warn(
         v.app,
         "%s.%s" % (json.dumps(v._jwt_inf["header"], ensure_ascii=False),
                    json.dumps(v._jwt_inf["payload"], ensure_ascii=False)), v),
     None),
    ("base64", "解码图另存为…", lambda v: v._save_b64_image(),
     lambda v: v._b64 and v._b64[0] == "image"),
    ("base64", "复制解码文本",
     lambda v: _copy_or_warn(v.app, v._b64[1].decode("utf-8", "replace"), v),
     lambda v: v._b64 and v._b64[0] == "text"),
]
