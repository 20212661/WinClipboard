# -*- coding: utf-8 -*-
"""「带上下文提问」小对话框(非常驻窗口):问题 + 包选择 + 发送。

发送链路(§7.4):assemble(含已放行敏感条目)→ 隐私闸(未确认则
**无网络调用**)→ 复用 workflow_ui.AIWorker 的 API AI 后台调用与取消 →
结果回主线程;提供 复制 / 存入历史 / 加入当前包。不新写 HTTP 客户端。"""
import sys as _sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPlainTextEdit,
                               QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox,
                            FluentIcon as FIF, InfoBar, InfoBarPosition,
                            PrimaryPushButton, PushButton, StrongBodyLabel,
                            TransparentToolButton)

import ui_base
from ui_base import *  # noqa: F401,F403
ui_base.register(_sys.modules[__name__])

from .budget import BudgetError
from .ui_tray import _toast, tray_service
from .assemble import assemble


class AskContextDialog(QWidget):
    """带上下文一次性提问(P5 预留 thread 多轮,当前单轮)。"""

    def __init__(self, app, parent=None):
        super().__init__(None)
        self.app = app
        self._worker = None
        self._zombies = []            # 已放弃但仍在跑的线程(保引用防析构崩溃)
        self._answer = None
        self._sent_prompt = ""
        self.setWindowTitle("带上下文提问 · WinClipboard")
        self.setWindowIcon(getattr(app, "icon", None))
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(640, 560)

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 16)
        v.setSpacing(8)

        head = QHBoxLayout()
        ttl = StrongBodyLabel("带上下文提问")
        ttl.setFont(font(15))
        head.addWidget(ttl)
        head.addStretch(1)
        v.addLayout(head)

        # ---- 包选择 + 预算摘要 ----
        row = QHBoxLayout()
        lab = BodyLabel("上下文包")
        lab.setStyleSheet("color: %s;" % SEC)
        self.cmb_pack = ComboBox()
        self.cmb_pack.setMinimumWidth(200)
        self.cmb_pack.currentIndexChanged.connect(
            lambda _i: self._refresh_summary())
        row.addWidget(lab)
        row.addWidget(self.cmb_pack, 1)
        self.btn_hint = TransparentToolButton(FIF.INFO, self)
        self.btn_hint.setFixedSize(20, 20)
        self.btn_hint.setToolTip("图片条目只存引用,不会发送给 AI;"
                                 "token 为估算值(误差约 ±20%)")
        row.addWidget(self.btn_hint)
        v.addLayout(row)
        self.lbl_summary = CaptionLabel("")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setStyleSheet("color: %s;" % TER)
        v.addWidget(self.lbl_summary)

        # ---- 问题输入 ----
        qlab = BodyLabel("问题(拼装结果在前,问题在末尾)")
        qlab.setStyleSheet("color: %s;" % SEC)
        v.addWidget(qlab)
        self.txt_q = QPlainTextEdit()
        self.txt_q.setPlaceholderText("如:根据以上资料,总结三个要点…")
        self.txt_q.setFixedHeight(84)
        self.txt_q.setStyleSheet(
            "QPlainTextEdit { background: %s; border: 1px solid %s;"
            " border-radius: 8px; padding: 6px; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT))
        v.addWidget(self.txt_q)

        # ---- 发送 ----
        row2 = QHBoxLayout()
        self.btn_send = PrimaryPushButton("发送")
        self.btn_send.setIcon(FIF.SEND.icon())
        self.btn_send.setMinimumWidth(120)
        self.btn_send.clicked.connect(self._send)
        self.btn_cancel_ai = PushButton("停止")
        self.btn_cancel_ai.setVisible(False)
        self.btn_cancel_ai.clicked.connect(self._cancel_ai)
        row2.addStretch(1)
        row2.addWidget(self.btn_cancel_ai)
        row2.addWidget(self.btn_send)
        v.addLayout(row2)

        # ---- 结果区 ----
        rlab = BodyLabel("回答")
        rlab.setStyleSheet("color: %s;" % SEC)
        v.addWidget(rlab)
        self.txt_result = QPlainTextEdit()
        self.txt_result.setReadOnly(True)
        self.txt_result.setPlaceholderText("发送后回答显示在这里")
        self.txt_result.setStyleSheet(
            "QPlainTextEdit { background: %s; border: 1px solid %s;"
            " border-radius: 8px; padding: 6px; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT))
        v.addWidget(self.txt_result, 1)

        row3 = QHBoxLayout()
        self.btn_copy = PushButton("复制回答")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_answer)
        self.btn_to_history = PushButton("存入历史")
        self.btn_to_history.setEnabled(False)
        self.btn_to_history.clicked.connect(self._save_to_history)
        self.btn_to_pack = PushButton("加入当前包")
        self.btn_to_pack.setEnabled(False)
        self.btn_to_pack.clicked.connect(self._save_to_pack)
        row3.addStretch(1)
        row3.addWidget(self.btn_copy)
        row3.addWidget(self.btn_to_history)
        row3.addWidget(self.btn_to_pack)
        v.addLayout(row3)

        self._pack_pids = []
        self._reload_packs()

    # ---- 数据 ----

    def _reload_packs(self):
        svc = tray_service(self.app)
        self.cmb_pack.blockSignals(True)
        self.cmb_pack.clear()
        self._pack_pids = [p.pid for p in svc.packs]
        if svc.packs:
            active = svc.active_pid
            names = []
            for p in svc.packs:
                mark = "● " if p.pid == active else ""
                names.append("%s%s(%d 条,≈%d tokens)"
                             % (mark, p.name, len(p.items),
                                sum(x.est_tokens for x in p.items if x.enabled)))
            self.cmb_pack.addItems(names)
            idx = (self._pack_pids.index(active)
                   if active in self._pack_pids else 0)
            self.cmb_pack.setCurrentIndex(max(0, idx))
        else:
            self.cmb_pack.addItem("(尚无上下文包——先在剪贴板页加入条目)")
        self.cmb_pack.blockSignals(False)
        self._refresh_summary()

    def _selected_pack(self):
        svc = tray_service(self.app)
        i = self.cmb_pack.currentIndex()
        return svc.get(self._pack_pids[i]) if 0 <= i < len(self._pack_pids) \
            else None

    def _refresh_summary(self):
        pack = self._selected_pack()
        if pack is None:
            self.lbl_summary.setText("没有上下文包;发送将只有问题本身。")
            return
        svc = tray_service(self.app)
        est = sum(x.est_tokens for x in pack.items if x.enabled)
        sens = sum(1 for x in pack.items if x.sensitive and x.enabled)
        over = est > pack.budget_tokens
        self.lbl_summary.setText(
            "预算 %d tokens,启用条目合计 ≈%d(%s)%s;发送前经隐私确认%s"
            % (pack.budget_tokens, est,
               "已超,将自动截断/丢弃" if over else "预算内",
               ";含敏感标记 %d 条" % sens if sens else "",
               ",含敏感条目时列出标题"))

    # ---- 发送链路:拼装 → 隐私闸 → AI 线程 ----

    def _send(self):
        if self._worker is not None:
            return
        question = self.txt_q.toPlainText().strip()
        if not question:
            _toast(self.app, "warning", "请输入问题", "问题不能为空",
                   parent=self)
            return
        svc_ai = self._default_service()
        if svc_ai is None:
            _toast(self.app, "warning", "未配置 AI 服务",
                   "先在「AI 服务」页配置至少一个服务", parent=self)
            return
        pack = self._selected_pack()
        if pack is not None:
            try:
                a = assemble(pack, question=question, include_sensitive=True)
            except BudgetError as e:
                _toast(self.app, "error", "无法拼装",
                       "指令条目超出预算(%s)" % (e.detail or ""), parent=self)
                return
            if a.warnings:
                InfoBar.warning("拼装提示", ";".join(a.warnings[:3]),
                                parent=self, position=InfoBarPosition.TOP,
                                duration=7000)
            prompt = a.text
        else:
            prompt = question + "\n"
        gated = self._gate(prompt, svc_ai)      # 未确认 → None → 不发
        if gated is None:
            return
        self._sent_prompt = gated
        self.btn_send.setEnabled(False)
        self.btn_cancel_ai.setVisible(True)
        from workflow_ui import AIWorker        # 复用 API 调用与取消机制
        self._worker = AIWorker(svc_ai, gated)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _default_service(self):
        services = self.app.cfg.get("ai_services") or []
        return services[0] if services else None

    def _gate(self, prompt, svc):
        """隐私闸必经:命中敏感内容三选一(遮盖/原样/取消);取消=None。"""
        hits, masked = cm.privacy_scan(prompt)
        if not hits:
            return prompt
        from workflow_ui import PrivacyGateDialog
        dlg = PrivacyGateDialog(self, hits, masked,
                                str(svc.get("base_url") or ""))
        dlg.exec()
        if dlg.choice == "mask":
            return masked
        if dlg.choice == "raw":
            return prompt
        return None

    def _cancel_ai(self):
        """放弃等待:迟到结果丢弃(与工作流 Job.cancel 同语义,HTTP 不可中断)。"""
        if self._worker is not None:
            self._zombies.append(self._worker)
            self._worker = None
            self.btn_send.setEnabled(True)
            self.btn_cancel_ai.setVisible(False)

    def _on_done(self, _idx, ok, text):
        sender = self.sender()
        if sender in self._zombies:
            self._zombies.remove(sender)     # 已放弃:只回收引用,不呈现
            return
        if self._worker is not sender:
            return
        self._worker = None
        self.btn_send.setEnabled(True)
        self.btn_cancel_ai.setVisible(False)
        if not ok:
            InfoBar.error("调用失败", text[:220], parent=self,
                          position=InfoBarPosition.TOP, duration=-1)
            return
        self._answer = text
        self.txt_result.setPlainText(text)
        for b in (self.btn_copy, self.btn_to_history, self.btn_to_pack):
            b.setEnabled(True)

    # ---- 结果动作 ----

    def _copy_answer(self):
        if not self._answer:
            return
        from viewer import _copy_or_warn
        _copy_or_warn(self.app, self._answer, self)

    def _save_to_history(self):
        if not self._answer:
            return
        try:
            self.app.core.add_item(("text", self._answer, {}, ""))
            _toast(self.app, "success", "已存入历史", "回答已作为新条目入史")
        except Exception as e:
            _toast(self.app, "error", "存入历史失败", str(e)[:160], parent=self)

    def _save_to_pack(self):
        if not self._answer:
            return
        from .ui_tray import add_text_to_pack
        add_text_to_pack(self.app, self._answer,
                         title="回答:" + self.txt_q.toPlainText().strip()[:24])

    def closeEvent(self, e):
        self._cancel_ai()
        super().closeEvent(e)


import clipboard_manager as cm  # noqa: E402  (privacy_scan;置尾避免环状导入期)
