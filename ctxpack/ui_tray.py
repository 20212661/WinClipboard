# -*- coding: utf-8 -*-
"""上下文篮托盘(剪贴板页底部,默认折叠)+ 入包编排(敏感确认 / Toast)。

winclipboard_qt 只调本模块入口(append_ctx_menu / tray_service /
refresh_tray),不在主文件继续膨胀;本模块 import 时 register 到
ui_base(调色板随主题同步),颜色全部从调色板派生,不硬编码。
服务延迟构造:首次用到才读盘(热键首帧/首屏同步段零影响)。"""
import sys as _sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFontMetrics
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (CheckBox, ComboBox, FluentIcon as FIF,
                            InfoBar, InfoBarPosition, LineEdit, MessageBox,
                            MessageBoxBase, PrimaryPushButton, PushButton,
                            RoundMenu, SimpleCardWidget, StrongBodyLabel,
                            TransparentToolButton)

import ui_base
from ui_base import *  # noqa: F401,F403
ui_base.register(_sys.modules[__name__])   # 调色板随主题同步本模块全局


# ============================================================
# 服务访问(延迟构造,挂在 app 上缓存)与通用编排
# ============================================================

def tray_service(app):
    """app 的上下文包服务(首次调用才 import ctxpack.service 并读盘)。"""
    svc = getattr(app, "_ctxpack_svc", None)
    if svc is None:
        import clipboard_manager as cm
        from .service import ContextPackService
        svc = ContextPackService(cm.CTXPACK_FILE,
                                 sensitive_check=cm._sensitive_reason).load()
        cfg = getattr(app, "cfg", None) or {}
        svc.set_encrypt(bool(cfg.get("encrypt_history")))
        app._ctxpack_svc = svc
    return svc


def refresh_tray(app):
    tray = getattr(getattr(app, "panel", None), "ctx_tray", None)
    if tray is not None:
        tray.refresh()


def _toast(app, kind, title, body, duration=5000):
    parent = getattr(app, "win", None) or getattr(app, "panel", None)
    fn = {"success": InfoBar.success, "warning": InfoBar.warning,
          "error": InfoBar.error}.get(kind, InfoBar.info)
    fn(title, body, parent=parent, position=InfoBarPosition.TOP_RIGHT,
       duration=duration)


def _report_save_state(app):
    """保存失败如实呈现(内存保留待重试);unreadable 状态托盘状态行也会标。"""
    svc = tray_service(app)
    if svc.last_save_error:
        _toast(app, "warning", "上下文包未保存",
               svc.last_save_error + "(变更保留在内存,稍后自动重试或重启后重试)",
               duration=6000)


def _confirm_sensitive(app, blocked):
    """敏感命中合并确认:列出标题与原因;放行=入包并置 sensitive。
    发送给 AI 前仍必经隐私闸(PrivacyGate),此处只管入包。"""
    names = "、".join("「%s」" % a["title"][:24] for a, _r in blocked[:3])
    if len(blocked) > 3:
        names += " 等 %d 条" % len(blocked)
    reasons = "、".join(sorted({r for _a, r in blocked}))
    box = MessageBox(
        "加入前确认:检测到敏感内容",
        "以下条目命中敏感规则(%s):\n%s\n\n"
        "确认加入会把它标记为敏感条目(预览/发送时会再提示,发送前必须经"
        "隐私确认)。取消则不加入。" % (reasons, names),
        getattr(app, "win", None))
    box.yesButton.setText("确认加入")
    box.cancelButton.setText("跳过这些条目")
    return box.exec()


class PackNameDialog(MessageBoxBase):
    """新建/重命名包的名称输入对话框。"""

    def __init__(self, parent, title="新建上下文包", initial=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        ttl = StrongBodyLabel(title)
        self.viewLayout.addWidget(ttl)
        self.edit = LineEdit()
        self.edit.setPlaceholderText("包名(1-30 字)")
        self.edit.setText(initial)
        self.edit.setMaxLength(30)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(380)

    def name(self):
        return self.edit.text().strip()


def add_text_to_pack(app, text, title=None, src_app="", pid=None,
                     allow_sensitive=False):
    """文本片段入包(查看器选区路径)。无任何包时自动建「默认包」。"""
    svc = tray_service(app)
    if pid is None and svc.active_pack() is None:
        svc.create("默认包")
    r = svc.add_snapshot("text", text or "", title=title, src_app=src_app,
                         captured_at=time.time(), pid=pid,
                         allow_sensitive=allow_sensitive)
    if r.ok:
        refresh_tray(app)
        _toast(app, "success", "已加入上下文篮",
               "「%s」→ %s(约 %d tokens)%s"
               % (r.item.title[:24], svc.active_pack().name, r.item.est_tokens,
                  ";该内容已在包内" if r.dup_of else ""))
        _report_save_state(app)
    elif r.reason.startswith("sensitive:"):
        if _confirm_sensitive(app, [({"title": title or (text or "")[:24],
                                      "content": text or "", "src_app": src_app,
                                      "kind": "text"}, r.reason.split(":", 1)[1])]):
            return add_text_to_pack(app, text, title=title, src_app=src_app,
                                    pid=pid, allow_sensitive=True)
    elif r.reason == "no_pack":
        _toast(app, "warning", "未加入", "没有可用的上下文包")
    return r


def add_records_to_pack(app, recs, pid=None, create_name=None):
    """卡片/多选入包编排:逐条快照(与历史解耦);敏感命中合并一次确认;
    结果 Toast。返回 (成功数, 敏感跳过数)。"""
    import clipboard_manager as cm
    from .service import snapshot_args_from_record
    svc = tray_service(app)
    if pid is None and create_name:
        pid = svc.create(create_name).pid
    added, dups, blocked = 0, 0, []
    target = svc.get(pid) if pid else svc.active_pack()
    if target is None:
        pid = svc.create("默认包").pid
    for rec in recs:
        args = snapshot_args_from_record(rec, sha1_fallback=cm.file_sha1)
        r = svc.add_snapshot(pid=pid, **args)
        if r.ok:
            added += 1
            dups += 1 if r.dup_of else 0
        elif r.reason.startswith("sensitive:"):
            blocked.append((args, r.reason.split(":", 1)[1]))
    if blocked and _confirm_sensitive(app, blocked):
        for args, _reason in blocked:
            svc.add_snapshot(pid=pid, allow_sensitive=True, **args)
            added += 1
        blocked = []
    refresh_tray(app)
    name = (svc.get(pid) if pid else svc.active_pack()).name
    msg = "%d 条已加入「%s」" % (added, name)
    if dups:
        msg += "(%d 条与包内已有内容同指纹)" % dups
    if blocked:
        msg += ";%d 条敏感内容未加入" % len(blocked)
    _toast(app, "success" if added else "warning",
           "上下文篮" if added else "未加入", msg)
    _report_save_state(app)
    return added, len(blocked)


def append_ctx_menu(app, menu, recs):
    """卡片右键菜单追加「加入上下文包 ▸」(多选段选时即批量入口)。"""
    svc = tray_service(app)
    parent = getattr(app, "panel", None) or getattr(app, "win", None)
    sub = RoundMenu("加入上下文包", parent)
    active = svc.active_pack()

    def _q(text):
        a = QAction(text, sub)
        sub.addAction(a)
        return a

    if len(recs) > 1:
        a = _q("所选 %d 条 → %s" % (len(recs),
                                    active.name if active else "新建包"))
        a.triggered.connect(lambda _=False: add_records_to_pack(app, recs))
    else:
        if active is not None:
            a = _q("→ %s(%d 条)" % (active.name, len(active.items)))
            a.triggered.connect(lambda _=False: add_records_to_pack(app, recs))
        a2 = _q("→ 新建包…")
        a2.triggered.connect(lambda _=False: _create_then_add(app, recs))
    menu.addMenu(sub)


def _create_then_add(app, recs):
    dlg = PackNameDialog(getattr(app, "win", None))
    if dlg.exec() and dlg.name():
        add_records_to_pack(app, recs, create_name=dlg.name())


# ============================================================
# 托盘本体
# ============================================================

def _warn_color():
    return "#b5730a" if not IS_DARK else "#e6a23c"


def _err_color():
    return "#c0392b" if not IS_DARK else "#e0554d"


class _TrayRow(QWidget):
    """单条目行:启停 / 标题(省略)/ 类型徽章 / 估算 / 敏感标记 / 排序 / 移除。"""

    def __init__(self, tray, pack, item):
        super().__init__()
        self.tray = tray
        self.pack = pack
        self.item = item
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.chk = CheckBox()
        self.chk.setChecked(item.enabled)
        self.chk.setFixedSize(18, 18)
        self.chk.setToolTip("暂停/启用此条目(暂停的不参与拼装)")
        self.chk.toggled.connect(self._toggle)
        h.addWidget(self.chk)
        self.lbl_title = QLabel()
        self.lbl_title.setFont(font(10))
        fm = QFontMetrics(self.lbl_title.font())
        self.lbl_title.setText(fm.elidedText(item.title, Qt.ElideRight, 210))
        self.lbl_title.setToolTip(item.title)
        h.addWidget(self.lbl_title)
        h.addWidget(Badge(item.kind))
        if item.sensitive:
            b = Badge("敏感")
            b.setStyleSheet("QLabel { background: %s; color: %s;"
                            " border-radius: 6px; padding: 3px 8px; }"
                            % (ACCENT_SOFT, _warn_color()))
            h.addWidget(b)
        self.lbl_tok = QLabel("≈%d" % item.est_tokens)
        self.lbl_tok.setFont(font(10))
        self.lbl_tok.setStyleSheet("color: %s;" % TER)
        h.addWidget(self.lbl_tok)
        h.addStretch(1)
        for icon, tip, fn in ((FIF.UP, "上移", self._up),
                              (FIF.DOWN, "下移", self._down),
                              (FIF.CLOSE, "移出上下文篮", self._remove)):
            b = TransparentToolButton(icon, self)
            b.setFixedSize(22, 22)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)

    def _svc(self):
        return tray_service(self.tray.app)

    def _toggle(self, on):
        self._svc().update_item(self.pack.pid, self.item.iid, enabled=bool(on))
        self.tray.refresh()

    def _up(self):
        idx = next((i for i, x in enumerate(self.pack.items)
                    if x.iid == self.item.iid), None)
        if idx is not None and idx > 0:
            self._svc().move_item(self.pack.pid, self.item.iid, idx - 1)
            self.tray.refresh()

    def _down(self):
        idx = next((i for i, x in enumerate(self.pack.items)
                    if x.iid == self.item.iid), None)
        if idx is not None and idx < len(self.pack.items) - 1:
            self._svc().move_item(self.pack.pid, self.item.iid, idx + 1)
            self.tray.refresh()

    def _remove(self):
        self._svc().remove_item(self.pack.pid, self.item.iid)
        self.tray.refresh()
        _report_save_state(self.tray.app)


class ContextTray(SimpleCardWidget):
    """上下文篮托盘:折叠=标题+徽标一行;展开=包切换/预算进度/条目行。

    预算进度 >90% 警示色、>100% 错误色(颜色从调色板派生);
    存储状态行如实显示(已加密/明文/不可读/未保存)。"""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._expanded = False
        self._pack_pids = []
        self.setBorderRadius(8)
        self.setStyleSheet(
            "ContextTray { background: %s; border: 1px solid %s; }"
            % (CARD_BG, CARD_BORDER))
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        root.setSpacing(4)

        # ---- 头部(折叠态也可见) ----
        h = QHBoxLayout()
        h.setSpacing(6)
        self.btn_expand = TransparentToolButton(FIF.DOWN, self)
        self.btn_expand.setFixedSize(22, 22)
        self.btn_expand.setToolTip("展开 / 折叠上下文篮")
        self.btn_expand.clicked.connect(lambda: self.set_expanded(
            not self._expanded))
        h.addWidget(self.btn_expand)
        self.lbl_title = StrongBodyLabel("上下文篮")
        self.lbl_title.setFont(font(11))
        h.addWidget(self.lbl_title)
        self.lbl_badge = QLabel("")
        self.lbl_badge.setFont(font(10))
        self.lbl_badge.setStyleSheet("color: %s;" % SEC)
        h.addWidget(self.lbl_badge)
        h.addStretch(1)
        self.cmb_pack = ComboBox()
        self.cmb_pack.setFixedWidth(150)
        self.cmb_pack.currentIndexChanged.connect(self._switch_pack)
        h.addWidget(self.cmb_pack)
        self.btn_new = TransparentToolButton(FIF.ADD, self)
        self.btn_new.setFixedSize(22, 22)
        self.btn_new.setToolTip("新建上下文包")
        self.btn_new.clicked.connect(self._create_pack)
        h.addWidget(self.btn_new)
        self.btn_preview = PushButton("预览拼装")
        self.btn_preview.setFont(font(10))
        self.btn_preview.setFixedHeight(24)
        self.btn_preview.clicked.connect(self.preview)
        h.addWidget(self.btn_preview)
        self.btn_copy = PushButton("复制拼装")
        self.btn_copy.setFont(font(10))
        self.btn_copy.setFixedHeight(24)
        self.btn_copy.clicked.connect(self.copy_assembled)
        h.addWidget(self.btn_copy)
        root.addLayout(h)
        self._header_extras = (self.cmb_pack, self.btn_new,
                               self.btn_preview, self.btn_copy)

        # ---- 预算进度条(展开态) ----
        pu = QHBoxLayout()
        pu.setContentsMargins(2, 0, 2, 0)
        pu.setSpacing(8)
        self._bar_bg = QFrame()
        self._bar_bg.setFixedHeight(6)
        self._bar_bg.setStyleSheet(
            "QFrame { background: %s; border-radius: 3px; }" % BADGE_BG)
        self._bar_fill = QFrame(self._bar_bg)
        self._bar_fill.setStyleSheet(
            "QFrame { background: %s; border-radius: 3px; }" % ACCENT)
        pu.addWidget(self._bar_bg, 1)
        self.lbl_usage = QLabel("")
        self.lbl_usage.setFont(font(10))
        self.lbl_usage.setStyleSheet("color: %s;" % SEC)
        pu.addWidget(self.lbl_usage)
        self._progress_row = QWidget()
        QVBoxLayout(self._progress_row).addLayout(pu)
        root.addWidget(self._progress_row)

        # ---- 存储状态行(展开态;私密区风格:如实,不粉饰) ----
        self.lbl_status = QLabel("")
        self.lbl_status.setFont(font(10))
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)

        # ---- 条目区(展开态) ----
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(2, 0, 2, 0)
        self._body_lay.setSpacing(2)
        self._empty = QLabel(
            "空——右键卡片「加入上下文包」收集素材;图片只存引用,不会发送给 AI")
        self._empty.setFont(font(10))
        self._empty.setStyleSheet("color: %s;" % TER)
        self._empty.setWordWrap(True)
        self._body_lay.addWidget(self._empty)
        root.addWidget(self._body)

        self.set_expanded(False)
        # 首帧零成本:徽标刷新延后到启动后的空闲点(不进首屏同步段)
        QTimer.singleShot(1200, self.refresh)

    # ---- 展开态管理 ----

    def set_expanded(self, on):
        self._expanded = bool(on)
        self.btn_expand.setIcon(FIF.UP.icon() if on else FIF.DOWN.icon())
        for w in self._header_extras:
            w.setVisible(on)
        self._progress_row.setVisible(on)
        self.lbl_status.setVisible(on)
        self._body.setVisible(on)
        if on:
            self.refresh()

    # ---- 数据刷新 ----

    def refresh(self):
        svc = tray_service(self.app)
        pack = svc.active_pack()
        # 包下拉(平行 pid 列表,避开 qfw userData 坑)
        self.cmb_pack.blockSignals(True)
        self.cmb_pack.clear()
        self._pack_pids = [p.pid for p in svc.packs]
        names = ["%s(%d)" % (p.name, len(p.items)) for p in svc.packs]
        if names:
            self.cmb_pack.addItems(names)
            idx = self._pack_pids.index(pack.pid) if pack else 0
            self.cmb_pack.setCurrentIndex(max(0, idx))
        else:
            self.cmb_pack.addItem("(尚无上下文包)")
        self.cmb_pack.blockSignals(False)
        # 条目行(清空重建;_empty 常驻标签保留复用)
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._empty:
                w.deleteLater()
        self._body_lay.addWidget(self._empty)
        if pack and pack.items:
            self._empty.setVisible(False)
            for it in pack.items:
                self._body_lay.addWidget(_TrayRow(self, pack, it))
        else:
            self._empty.setVisible(True)
        self._empty.setText(
            "空——右键卡片「加入上下文包」收集素材;图片只存引用,不会发送给 AI"
            if svc.packs else "尚无上下文包——右键卡片「加入上下文包」新建")
        # 徽标与预算
        n = len(pack.items) if pack else 0
        est = sum(x.est_tokens for x in pack.items if x.enabled) if pack else 0
        self.lbl_badge.setText("篮 %d · ≈%s" % (n, _fmt_tokens(est)) if n
                               else "上下文篮(空)")
        self._set_usage(est, pack.budget_tokens if pack else 4000)
        # 存储状态行
        self.lbl_status.setText(self._status_text(svc))
        self.lbl_status.setStyleSheet(
            "color: %s;" % (_err_color()
                            if svc.status == "unreadable" or svc.last_save_error
                            else TER))

    @staticmethod
    def _status_text(svc):
        if svc.status == "unreadable":
            return "⚠ 包文件不可读:本会话不覆写原文件(数据仍在磁盘);可从备份手工恢复或用「清空全部数据」重置"
        if svc.status == "recovered":
            base = "已从备份 %s 恢复(损坏原件留档 .corrupt)" % svc.load_source
        elif svc.status == "missing":
            base = "尚未保存(首次加入条目后创建文件)"
        else:
            base = "已加密存储(DPAPI)" if svc.mode == "dpapi" else \
                   "未加密(跟随「历史加密」开关;明文便于迁移)"
        if svc.last_save_error:
            base += ";⚠ 未保存:%s(内存保留待重试)" % svc.last_save_error
        return base

    def _set_usage(self, est, budget):
        """预算进度:>90% 警示、>100% 错误(超出部分拼装时截断/丢弃)。"""
        frac = (est / float(budget)) if budget else 0.0
        level = "error" if frac > 1.0 else ("warn" if frac > 0.9 else "ok")
        self._usage_fraction = frac
        self._usage_level = level
        color = {"ok": ACCENT, "warn": _warn_color(),
                 "error": _err_color()}[level]
        self._bar_fill.setStyleSheet(
            "QFrame { background: %s; border-radius: 3px; }" % color)
        self._bar_bg.updateGeometry()
        self._apply_bar_width()
        tip = "超出预算:拼装时将截断或丢弃低优先级条目" if level == "error" \
            else "接近预算(>90%)" if level == "warn" else ""
        self.lbl_usage.setText("≈%s / %d%s" % (
            _fmt_tokens(est), budget,
            " · 估算值,误差约 ±20%%%s" % (" · " + tip if tip else "")))

    def _apply_bar_width(self):
        w = self._bar_bg.width()
        frac = getattr(self, "_usage_fraction", 0.0)
        self._bar_fill.setGeometry(0, 0, int(w * min(1.0, frac)), 6)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_bar_width()

    # ---- 动作 ----

    def _switch_pack(self, index):
        if 0 <= index < len(self._pack_pids):
            tray_service(self.app).set_active(self._pack_pids[index])
            self.refresh()

    def _create_pack(self):
        dlg = PackNameDialog(self)
        if dlg.exec() and dlg.name():
            tray_service(self.app).create(dlg.name())
            self.refresh()

    def _assemble_or_warn(self):
        """拼装(本地预览用:含已放行的敏感条目;警告原样呈现)。"""
        from .budget import BudgetError
        svc = tray_service(self.app)
        if svc.active_pack() is None:
            _toast(self.app, "warning", "上下文篮为空", "先加入条目再拼装")
            return None
        try:
            a = svc.assemble_active(include_sensitive=True)
        except BudgetError as e:
            _toast(self.app, "error", "无法拼装",
                   "指令条目超出预算(%s);请调大预算或精简指令条目" % e.detail,
                   duration=7000)
            return None
        if a is None or not a.ok:
            _toast(self.app, "warning", "上下文篮为空",
                   "没有启用中的条目可拼装")
            return None
        return a

    def preview(self):
        """预览拼装结果(复用查看器只读代码视图;本地不外发)。"""
        a = self._assemble_or_warn()
        if a is None:
            return
        if a.warnings:
            _toast(self.app, "warning", "拼装有 %d 条提示" % len(a.warnings),
                   ";".join(a.warnings[:3]), duration=7000)
        from viewer import ViewerWindow
        v = ViewerWindow(self.app, {"type": "text", "content": a.text,
                                    "ts": time.time()})
        v.setWindowTitle("拼装预览 · %s(≈%d tokens)· WinClipboard"
                         % (tray_service(self.app).active_pack().name,
                            a.used_tokens))
        v.setAttribute(Qt.WA_DeleteOnClose)
        v.show()

    def copy_assembled(self):
        """复制拼装结果(走查看器 _copy_or_warn:失败如实提示)。"""
        a = self._assemble_or_warn()
        if a is None:
            return
        from viewer import _copy_or_warn
        if _copy_or_warn(self.app, a.text, self.app.win):
            _toast(self.app, "success", "已复制拼装结果",
                   "≈%d tokens(估算)" % a.used_tokens)


def _fmt_tokens(n):
    """token 数短格式:1234 → 1.2k。"""
    if n >= 1000:
        v = n / 1000.0
        return ("%.1fk" % v).replace(".0k", "k")
    return str(int(n))
