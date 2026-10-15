# -*- coding: utf-8 -*-
"""AI 工作流 UI 子系统(阶段 8 拆分,自 winclipboard_qt.py 迁出,行为不变):
服务管理(ServiceEditor/ProviderCard/ServicesPage)、工作流编辑/管理/运行
(WorkflowEditorWindow/WorkflowPage/WorkflowRunner)、运行记录查看
(WorkflowRunInspector)、AIWorker/ModelWorker 后台线程与隐私门对话框。
数据层:workflow_steps(步骤契约)/ workflow_runlog(运行记录)/ cm(AI 调用)。
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
import workflow_steps as wfs
import workflow_runlog as wrl
from perf_probe import PerfSpan, since, stamp

import ui_base
from ui_base import *  # noqa: F401,F403
from ui_base import ensure_onscreen, _fic

import sys as _sys
ui_base.register(_sys.modules[__name__])   # 调色板随主题同步本模块全局


class AIWorker(QThread):
    """后台调用 AI(阻塞的 cm.ai_chat 放到线程里,不冻结 UI)。"""
    done = Signal(int, bool, str)     # step_idx, ok, text

    def __init__(self, service, prompt, step_idx=-1, parent=None):
        super().__init__(parent)
        self.service = service
        self.prompt = prompt
        self.step_idx = step_idx

    def run(self):
        ok, text = cm.ai_chat(self.service, self.prompt)
        self.done.emit(self.step_idx, ok, text)


class PrivacyGateDialog(MessageBoxBase):
    """AI 发送前隐私门:提示词含敏感内容时三选一(人在回路)。

    [自动遮盖后发送](主按钮) [本次原样发送] [取消]——关闭窗口/Esc 视为取消。
    choice ∈ {"mask", "raw", "cancel"};遮盖文本由 cm.privacy_scan 产生。"""

    def __init__(self, parent, hits, masked_preview, endpoint):
        super().__init__(parent)
        self.choice = "cancel"

        title = StrongBodyLabel("⚠ 内容可能包含敏感信息")
        title.setStyleSheet("color: #C4820A;")
        self.viewLayout.addWidget(title)
        body = BodyLabel("即将发送至 %s 的提示词中检测到:" % (endpoint or "AI 服务"))
        body.setStyleSheet("color: %s;" % TEXT)
        self.viewLayout.addWidget(body)
        for lb in hits:
            row = BodyLabel("•  %s" % lb)
            row.setStyleSheet("color: #C4820A; padding-left: 12px;")
            self.viewLayout.addWidget(row)
        prev = CaptionLabel("遮盖预览:%s" % (masked_preview[:160] + ("…" if len(masked_preview) > 160 else "")))
        prev.setWordWrap(True)
        prev.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(prev)

        self.btn_raw = PushButton("本次原样发送", self)
        self.btn_raw.clicked.connect(lambda: self._pick("raw"))
        self.buttonLayout.insertWidget(0, self.btn_raw)
        self.buttonLayout.insertStretch(1)
        self.yesButton.setText("自动遮盖后发送")
        self.yesButton.clicked.connect(lambda: self._pick("mask"))
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(460)

    def _pick(self, choice):
        self.choice = choice
        self.accept()




class ModelWorker(QThread):
    """后台拉取端点模型清单(GET /models,OpenAI 标准发现端点)。"""
    done = Signal(bool, object)     # ok, [模型 id] 或 错误文本

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self):
        ok, payload = cm.list_models(self.service)
        self.done.emit(ok, payload)


class ServiceEditorDialog(MessageBoxBase):
    """API 端点配置表单(hermes 风格):Name / Provider ID / Endpoint URL /
    Default Model(可「发现模型」自动拉取)/ API 密钥(可留空)+ ⚡测试。

    聚合引擎见 cm.ai_chat:openai SDK 统一客户端(OpenAI 兼容端点通吃,
    含 Ollama 与 one-api 类本地网关),SDK 缺席时退回零依赖 urllib。
    provider 字段为路由前缀预留(anthropic/gemini 原生协议),OpenAI 兼容
    端点保持 openai 即可。
    """

    # 常用 litellm 风格路由前缀(可自由输入)
    _PIDS = ("openai", "deepseek", "anthropic", "gemini", "groq", "mistral", "ollama", "azure")

    def __init__(self, parent, svc=None, preset=None):
        super().__init__(parent)
        self.svc = dict(svc) if svc else dict(preset or {})
        self.result = None
        self._presets = {p["name"]: p for p in cm.AI_PRESET_SERVICES}
        self._model_worker = None
        self._chat_worker = None

        self.titleLabel = StrongBodyLabel("API 端点")
        self.viewLayout.addWidget(self.titleLabel)
        sub = CaptionLabel("OpenAI 兼容接口;「发现模型」自动拉取端点的模型清单(本地网关 / Ollama 同样适用)。")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(sub)

        # ---- 供应商(预设一键预填;自定义则手填全部字段) ----
        self.cmb_provider = ComboBox()
        self.cmb_provider.addItems([p["name"] for p in cm.AI_PRESET_SERVICES] + ["自定义"])
        init_name = str(self.svc.get("name") or "")
        self.cmb_provider.setCurrentText(init_name if init_name in self._presets else "自定义")
        self.cmb_provider.setMinimumWidth(220)
        self.cmb_provider.currentTextChanged.connect(self._on_provider)
        self._row("供应商", self.cmb_provider)

        # ---- 名称(仅自定义,hermes: Name) ----
        self.e_name = LineEdit()
        self.e_name.setPlaceholderText("如 公司中转 / Axet Proxy")
        self.e_name.setText("" if init_name in self._presets else init_name)
        self.row_name, _lbl = self._row("名称", self.e_name)

        # ---- Provider ID(hermes 同名字段:路由前缀,openai=OpenAI 兼容直连) ----
        self.cmb_pid = EditableComboBox()
        self.cmb_pid.addItems(list(self._PIDS))
        self.cmb_pid.setText(str(self.svc.get("provider") or "openai"))
        self.cmb_pid.setMinimumWidth(160)
        self.cmb_pid.setToolTip("openai = 按 OpenAI 兼容协议直连接口地址;"
                                "其余为原生协议路由前缀预留")
        self._row("Provider ID", self.cmb_pid)

        # ---- 接口地址(hermes: Endpoint URL,主表单位置) ----
        self.e_url = LineEdit()
        self.e_url.setPlaceholderText("http://127.0.0.1:8081/v1(自定义必填)")
        self._row("接口地址", self.e_url)

        # ---- 默认模型 + 发现模型(hermes: Default Model / Discover models) ----
        self.cmb_model = EditableComboBox()
        self.cmb_model.setMinimumWidth(180)
        self.cmb_model.currentTextChanged.connect(self._update_model_note)
        self.btn_discover = PushButton("发现模型", self)
        self.btn_discover.setIcon(FIF.SYNC.icon())
        self.btn_discover.setFixedHeight(28)
        self.btn_discover.setCursor(Qt.PointingHandCursor)
        self.btn_discover.setToolTip("从接口地址拉取可用模型清单(GET /models)")
        self.btn_discover.clicked.connect(self._discover)
        mrow = QWidget()
        mh = QHBoxLayout(mrow)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.setSpacing(8)
        mh.addWidget(self.cmb_model, 1)
        mh.addWidget(self.btn_discover, 0)
        self._row("默认模型", mrow)
        self.lbl_model_note = CaptionLabel("")
        self.lbl_model_note.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(self.lbl_model_note)

        # ---- API 密钥(可留空:本地网关 / Ollama / 网页 AI 优先) ----
        self.e_key = LineEdit()
        self.e_key.setEchoMode(QLineEdit.Password)
        self.e_key.setText(str(self.svc.get("api_key") or ""))
        self._kw, self.lbl_key = self._row("API 密钥", self.e_key)
        key_note = CaptionLabel("可留空;保存于本机(DPAPI 加密),只用于发起 API 请求")
        key_note.setStyleSheet("color: %s;" % TER)
        self.viewLayout.addWidget(key_note)

        # ---- 按钮区:⚡测试(hermes: Test)+ 取消 / 保存 ----
        self.btn_test = PushButton("⚡ 测试", self)
        self.btn_test.setFixedHeight(28)
        self.btn_test.setCursor(Qt.PointingHandCursor)
        self.btn_test.clicked.connect(self._test_now)
        self.buttonLayout.insertWidget(0, self.btn_test)
        self.buttonLayout.insertStretch(1)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setFixedWidth(500)

        # 按当前供应商填充,再把已有配置覆盖进去
        self._on_provider(self.cmb_provider.currentText())
        if self.svc:
            self.e_url.setText(str(self.svc.get("base_url") or ""))
            if self.svc.get("model"):
                self.cmb_model.setText(str(self.svc["model"]))
            self._update_model_note()

    # ---- 行辅助:返回 (行容器, 标签),容器可整体显隐,标签可改文字 ----
    def _row(self, label_text, field):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        lab = BodyLabel(label_text)
        lab.setStyleSheet("color: %s;" % SEC)
        lab.setMinimumWidth(72)
        h.addWidget(lab, 0)
        h.addWidget(field, 1)
        self.viewLayout.addWidget(w)
        return w, lab

    def _on_provider(self, name):
        p = self._presets.get(name)
        self.row_name.setVisible(name == "自定义")
        self.lbl_key.setText("%s API 密钥" % name if p else "API 密钥")
        self.cmb_model.clear()
        if p:
            self.e_url.setText(p["base_url"])
            self.cmb_pid.setText("openai")   # 预设均为 OpenAI 兼容端点
            self.cmb_model.addItems([m["id"] for m in p.get("models") or []])
            self.cmb_model.setCurrentIndex(0)
        else:
            self.cmb_model.setText("")   # EditableComboBox 的 setCurrentText 只认列表内文本
        self._update_model_note()

    def _update_model_note(self, *_):
        cur = self.cmb_model.currentText().strip()
        p = self._presets.get(self.cmb_provider.currentText())
        note = ""
        if p:
            for m in p.get("models") or []:
                if m["id"] == cur:
                    note = m["note"]
                    break
        self.lbl_model_note.setText(note)
        self.lbl_model_note.setVisible(bool(note))

    # ---- 发现模型(后台 GET /models) ----
    def _discover(self):
        if self._model_worker is not None:
            return
        base = self.e_url.text().strip()
        if not base:
            InfoBar.warning("缺少接口地址", "先填写接口地址,再发现模型", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("发现中…")
        self._model_worker = ModelWorker(
            {"base_url": base, "api_key": self.e_key.text().strip()}, self)

        def on_done(ok, payload):
            self.btn_discover.setEnabled(True)
            self.btn_discover.setText("发现模型")
            self._model_worker = None
            if not ok:
                InfoBar.error("发现失败", str(payload)[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)
                return
            self._apply_models(payload)
            InfoBar.success("发现完成", "从该端点拉取到 %d 个模型" % len(payload),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)

        self._model_worker.done.connect(on_done)
        self._model_worker.start()

    def _apply_models(self, models):
        """把发现到的模型清单填进下拉(保留已输入的模型名,若有)。"""
        cur = self.cmb_model.currentText().strip()
        self.cmb_model.clear()
        self.cmb_model.addItems(models)
        if cur in models:
            self.cmb_model.setCurrentText(cur)
        else:
            self.cmb_model.setText(cur or (models[0] if models else ""))
        self._update_model_note()

    # ---- ⚡ 测试(不关对话框;后台真实调用当前表单配置) ----
    def _temp_service(self):
        return {"name": "test", "base_url": self.e_url.text().strip(),
                "model": self.cmb_model.currentText().strip(),
                "api_key": self.e_key.text().strip(),
                "provider": self.cmb_pid.currentText().strip() or "openai"}

    def _test_now(self):
        if self._chat_worker is not None:
            return
        svc = self._temp_service()
        if not svc["base_url"] or not svc["model"]:
            InfoBar.warning("无法测试", "先填写 接口地址 和 模型 再测试", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中…")
        self._chat_worker = AIWorker(svc, "请只回复两个字母:OK", -1, self)

        def on_done(_idx, ok, text):
            self.btn_test.setEnabled(True)
            self.btn_test.setText("⚡ 测试")
            self._chat_worker = None
            if ok:
                InfoBar.success("连接成功", "模型回复:%s" % text[:60], parent=self,
                                position=InfoBarPosition.TOP_RIGHT)
            else:
                InfoBar.error("连接失败", text[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)

        self._chat_worker.done.connect(on_done)
        self._chat_worker.start()

    def validate(self):
        provider = self.cmb_provider.currentText()
        model = self.cmb_model.currentText().strip()
        base = self.e_url.text().strip()
        if provider == "自定义":
            name = self.e_name.text().strip()
            if not name or not base or not model:
                InfoBar.error("无法保存", "自定义供应商需填写 名称 / 接口地址 / 模型",
                              parent=self, position=InfoBarPosition.TOP_RIGHT)
                return False
        else:
            name = provider
            if not model:
                InfoBar.error("无法保存", "请选择或输入模型", parent=self,
                              position=InfoBarPosition.TOP_RIGHT)
                return False
            if not base:
                base = self._presets[provider]["base_url"]
        self.result = {"name": name, "base_url": base, "model": model,
                       "api_key": self.e_key.text().strip(),
                       "provider": self.cmb_pid.currentText().strip() or "openai"}
        return True


class ProviderCard(SimpleCardWidget):
    """AI 供应商卡片(OpenCode 风格):状态点 + 名称/模型/接口 + 编辑/测试/删除。"""

    def __init__(self, page, svc):
        super().__init__()
        self.svc = svc
        self.setBorderRadius(8)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(14)

        # 状态点:绿 = 已填密钥可用,灰 = 未配置(语义色,不随主题派生)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet(
            "background: %s; border-radius: 4px;"
            % ("#3fb950" if svc.get("api_key") else NAV_BG))
        h.addWidget(self.dot, 0, Qt.AlignVCenter)

        left = QVBoxLayout()
        left.setSpacing(3)
        name = StrongBodyLabel(str(svc.get("name") or ""))
        name.setFont(font(14, QFont.DemiBold))
        sub = BodyLabel("%s · %s%s" % (
            svc.get("model"), svc.get("base_url"),
            "" if svc.get("api_key") else " · 未配置密钥"))
        sub.setFont(font(12))
        # 长 base_url 必须换行:否则卡片最小宽度被撑过视口,右侧按钮被裁掉
        sub.setWordWrap(True)
        left.addWidget(name)
        left.addWidget(sub)
        h.addLayout(left, 1)

        self.btn_test = PushButton("测试", self)
        self.btn_test.setIcon(FIF.SEND.icon())
        self.btn_edit = PushButton("编辑", self)
        self.btn_edit.setIcon(FIF.EDIT.icon())
        self.btn_del = PushButton("删除", self)
        self.btn_del.setIcon(FIF.DELETE.icon())
        for b in (self.btn_test, self.btn_edit, self.btn_del):
            b.setFixedHeight(30)
            b.setCursor(Qt.PointingHandCursor)
            h.addWidget(b)
        self.btn_test.clicked.connect(lambda: page.test_service(svc, self))
        self.btn_edit.clicked.connect(lambda: page.edit_service(svc))
        self.btn_del.clicked.connect(lambda: page.del_service(svc))


class ServicesPage(QWidget):
    """AI 供应商管理(OpenCode 风格):供应商卡片 + 预设/自定义两种添加入口。"""

    def __init__(self, app, embedded=False):
        """embedded=True:作为「AI 模型与服务」区块内嵌进设置页
        (紧凑边距、小节标题;False 保持独立页面形态,兼容旧测试)。"""
        super().__init__()
        self.app = app
        self.setObjectName("servicesPage")
        self._test_worker = None
        self.cards = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 8 if embedded else 16)
        v.setSpacing(10)

        head = QHBoxLayout()
        if embedded:
            sec = StrongBodyLabel("AI 模型与服务")
            sec.setFont(font(14, QFont.DemiBold))
            head.addWidget(sec)
        else:
            head.addWidget(TitleLabel("AI 服务"))
        head.addStretch(1)
        b_cfg = PushButton("打开配置文件", self)
        b_cfg.setIcon(FIF.DOCUMENT.icon())
        b_cfg.clicked.connect(self._open_config)
        head.addWidget(b_cfg)
        v.addLayout(head)

        hint = CaptionLabel("工作流每一步可指定不同服务,实现多模型协作。"
                            "「添加提供方」从常用供应商一键预填;密钥经 DPAPI 加密保存在本机。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(content)
        self.cards_layout.setContentsMargins(2, 4, 2, 4)
        self.cards_layout.setSpacing(10)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(content)
        v.addWidget(self.scroll, 1)

        self._empty_hint = CaptionLabel(
            "还没有 AI 服务。点下方「添加提供方」选择常用供应商,或添加自定义 OpenAI 兼容接口。")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setStyleSheet("color: %s;" % TER)
        self.cards_layout.addWidget(self._empty_hint)

        # 底部双入口(OpenCode 同款):预设清单 / 自定义表单
        addrow = QHBoxLayout()
        addrow.setSpacing(12)
        self.b_add_preset = PushButton("添加提供方", self)
        self.b_add_preset.setIcon(FIF.ADD.icon())
        self.b_add_preset.setCursor(Qt.PointingHandCursor)
        self.b_add_preset.clicked.connect(self._add_preset)
        self.b_add_custom = PushButton("添加自定义提供方", self)
        self.b_add_custom.setIcon(FIF.ADD.icon())
        self.b_add_custom.setCursor(Qt.PointingHandCursor)
        self.b_add_custom.clicked.connect(lambda: self._edit(None))
        for b in (self.b_add_preset, self.b_add_custom):
            b.setFixedHeight(34)
            addrow.addWidget(b, 1)
        v.addLayout(addrow)

        self.refresh()

    def refresh(self):
        for c in self.cards:
            self.cards_layout.removeWidget(c)
            c.deleteLater()
        self.cards = []
        svcs = self.app.cfg.get("ai_services") or []
        self._empty_hint.setVisible(not svcs)
        for s in svcs:
            c = ProviderCard(self, s)
            self.cards_layout.addWidget(c)
            self.cards.append(c)

    # ---- 添加 ----
    def _add_preset(self):
        """从常用供应商预设清单选择并预填(密钥留空,保存前填写)。"""
        menu = RoundMenu(parent=self)
        for p in cm.AI_PRESET_SERVICES:
            # RoundMenu.addAction 只收 QAction(传 str 会在渲染时崩溃)
            a = QAction("%s · %s" % (p["name"], p["model"]), menu)
            a.triggered.connect(lambda _=False, p=p: self._add_from_preset(p))
            menu.addAction(a)
        menu.exec(self.b_add_preset.mapToGlobal(
            QPoint(0, self.b_add_preset.height())))

    def _add_from_preset(self, preset):
        svcs = self.app.cfg.setdefault("ai_services", [])
        if any(x.get("name") == preset["name"] for x in svcs):
            InfoBar.error("已存在", "服务「%s」已在列表中,可直接编辑填写密钥" % preset["name"],
                          parent=self, position=InfoBarPosition.TOP_RIGHT)
            return
        self._edit(None, preset=preset)

    def _open_config(self):
        try:
            os.startfile(cm.CONFIG_FILE)
        except Exception:
            InfoBar.error("打开失败", "配置文件不存在或无法打开:\n%s" % cm.CONFIG_FILE,
                          parent=self, position=InfoBarPosition.TOP_RIGHT)

    # ---- 编辑 / 删除 / 测试 ----
    def edit_service(self, svc):
        self._edit(svc)

    def del_service(self, svc):
        if not MessageBox("删除服务", "确定删除服务「%s」?" % svc.get("name"), self).exec():
            return
        try:
            self.app.cfg.get("ai_services", []).remove(svc)
        except ValueError:
            pass
        self._save()

    def test_service(self, svc, card):
        if self._test_worker is not None:
            return

        def set_test_btn(text, enabled):
            try:                    # 测试期间卡片可能已被刷新销毁
                card.btn_test.setText(text)
                card.btn_test.setEnabled(enabled)
            except RuntimeError:
                pass

        set_test_btn("测试中…", False)
        self._test_worker = AIWorker(svc, "请只回复两个字母:OK", -1, self)

        def on_done(_idx, ok, text):
            set_test_btn("测试", True)
            self._test_worker = None
            if ok:
                InfoBar.success("连接成功", "「%s」回复:%s" % (svc.get("name"), text[:60]),
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
            else:
                InfoBar.error("连接失败", text[:200], parent=self,
                              position=InfoBarPosition.TOP_RIGHT, duration=-1)

        self._test_worker.done.connect(on_done)
        self._test_worker.start()

    def _save(self):
        if not cm.save_config(self.app.cfg):
            InfoBar.error("配置未保存",
                          "AI 服务密钥加密失败(DPAPI 不可用),原配置文件保持不变;"
                          "新密钥在本会话内可用,重启后需重新填写",
                          parent=self, position=InfoBarPosition.TOP_RIGHT,
                          duration=8000)
        self.refresh()

    def _edit(self, svc, preset=None):
        dlg = ServiceEditorDialog(self, svc, preset)
        if not dlg.exec():
            return
        r = dlg.result
        svcs = self.app.cfg.setdefault("ai_services", [])
        same = [x for x in svcs if x.get("name") == r["name"] and x is not svc]
        if same:
            InfoBar.error("名称重复", "已存在同名服务:%s" % r["name"], parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        if svc is not None:
            old_name = svc.get("name")
            svc.clear()
            svc.update(r)
            if old_name != r["name"]:   # 服务改名 → 同步工作流引用
                for wf in self.app.cfg.get("ai_workflows") or []:
                    for st in wf.get("steps") or []:
                        if st.get("service") == old_name:
                            st["service"] = r["name"]
        else:
            svcs.append(r)
        self._save()


class WorkflowEditorWindow(QWidget):
    """工作流新建/编辑:步骤 = 服务 + 提示词模板({input} 占位)。"""

    def __init__(self, app, wf=None, on_saved=None):
        super().__init__(None)
        self.app = app
        self.wf = wf if wf is not None else None
        self.on_saved = on_saved
        self.setWindowTitle("编辑工作流" if wf else "新建工作流")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(640, 580)
        self._rows = []

        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)

        h = QHBoxLayout()
        lab = BodyLabel("名称")
        lab.setStyleSheet("color: %s;" % SEC)
        self.e_name = LineEdit()
        self.e_name.setPlaceholderText("如:翻译(G)→润色(D)")
        if wf:
            self.e_name.setText(wf.get("name", ""))
        h.addWidget(lab)
        h.addWidget(self.e_name, 1)
        v.addLayout(h)

        # 上下文包绑定:{context} 占位符注入拼装结果(预算/敏感规则见上下文篮)
        h2 = QHBoxLayout()
        lab2 = BodyLabel("上下文包")
        lab2.setStyleSheet("color: %s;" % SEC)
        self.cmb_ctx = ComboBox()
        self._ctx_values = [""]
        _ctx_items = ["不使用"]
        try:
            from ctxpack.ui_tray import tray_service
            _svc = tray_service(app)
            if _svc.packs:
                self._ctx_values.append("@active")
                _ctx_items.append("活动包(%s)" % _svc.active_pack().name
                                  if _svc.active_pack() else "活动包")
                for _p in _svc.packs:
                    self._ctx_values.append(_p.pid)
                    _ctx_items.append(_p.name)
        except Exception:
            pass
        self.cmb_ctx.addItems(_ctx_items)
        _cur_cp = (wf or {}).get("context_pack") or ""
        self.cmb_ctx.setCurrentIndex(
            self._ctx_values.index(_cur_cp) if _cur_cp in self._ctx_values else 0)
        self.cmb_ctx.setToolTip(
            "提示词里的 {context} 占位符替换为该包的拼装结果"
            "(token 预算内自动截断;发送前仍经隐私确认)")
        h2.addWidget(lab2)
        h2.addWidget(self.cmb_ctx, 1)
        v.addLayout(h2)

        hint = CaptionLabel("步骤类型:本地 AI(API 调用)、网页 AI(提示词进剪贴板,可选自动打开网页)、"
                            "人工确认(粘贴/编辑后继续)、复制到剪贴板、文本变换(本地白名单操作)、"
                            "条件判断(命中正则继续,未命中停链)。提示词占位符:{input}/{previous} = "
                            "上一步产出(首步=原始输入),{origin} = 原始输入,"
                            "{context} = 绑定的上下文包拼装(若上方选择了包)。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        self.steps_box = QVBoxLayout(content)
        self.steps_box.setContentsMargins(0, 0, 0, 0)
        self.steps_box.setSpacing(10)
        self.steps_box.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(content)
        v.addWidget(self.scroll, 1)

        b_add = PushButton("添加步骤", self)
        b_add.setIcon(FIF.ADD.icon())
        b_add.clicked.connect(lambda: self._add_row(None))
        v.addWidget(b_add, 0, Qt.AlignLeft)

        foot = QHBoxLayout()
        b_save = PrimaryPushButton("保存", self)
        b_save.setIcon(FIF.SAVE.icon())
        b_cancel = PushButton("取消", self)
        b_cancel.clicked.connect(self.close)
        b_save.clicked.connect(self._save)
        foot.addStretch(1)
        foot.addWidget(b_cancel)
        foot.addWidget(b_save)
        v.addLayout(foot)

        steps = (wf.get("steps") if wf else None) or [{}]
        for st in steps:
            self._add_row(st)

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    # 种类下拉来自 Step Contract 注册表((label, kind) 序,索引约定沿用)
    _KINDS = tuple((label, kind) for kind, label in wfs.EDITOR_KINDS)

    def _add_row(self, step):
        step = step or {}
        kind = wfs.KIND_ALIASES.get(str(step.get("kind", "ai")).lower(),
                                     step.get("kind", "ai"))
        if kind not in (k for _, k in self._KINDS):
            kind = "ai"
        frame = QFrame()
        frame.setObjectName("stepCard")
        frame.setStyleSheet("QFrame#stepCard { background: %s; border-radius: 12px;"
                            " border: 1px solid %s; }" % (CARD_BG, CARD_BORDER))
        col = QVBoxLayout(frame)
        col.setContentsMargins(12, 10, 12, 10)
        col.setSpacing(8)

        top = QHBoxLayout()
        nlab = CaptionLabel("第 %d 步" % (len(self._rows) + 1))
        nlab.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        kind_combo = ComboBox()
        kind_combo.addItems([k[0] for k in self._KINDS])
        kind_combo.setCurrentIndex([i for i, k in enumerate(self._KINDS) if k[1] == kind][0])
        kind_combo.setMinimumWidth(100)

        names = [s.get("name", "?") for s in self.app.cfg.get("ai_services") or []]
        combo = ComboBox()
        combo.addItems(names)
        cur = step.get("service")
        if cur in names:
            combo.setCurrentText(cur)
        combo.setMinimumWidth(130)

        url_edit = LineEdit()
        url_edit.setText(str(step.get("url") or ""))
        url_edit.setPlaceholderText("https://chatgpt.com/?q={prompt}(留空=仅复制提示词)")

        b_del = TransparentToolButton(FIF.DELETE, frame)
        b_del.setFixedSize(28, 28)
        b_del.setToolTip("移除此步骤")
        top.addWidget(nlab)
        top.addSpacing(6)
        top.addWidget(kind_combo)
        top.addWidget(combo)
        top.addWidget(url_edit, 1)
        top.addWidget(b_del)
        col.addLayout(top)

        # 变换/条件步骤的参数行(其余类型隐藏)
        params = QHBoxLayout()
        op_combo = ComboBox()
        op_combo.addItems([wfs.TransformStep.OP_LABELS[o]
                           for o in wfs.TransformStep.OPS])
        op0 = step.get("op") or "trim"
        if op0 in wfs.TransformStep.OPS:
            op_combo.setCurrentText(wfs.TransformStep.OP_LABELS[op0])
        op_combo.setMinimumWidth(110)
        arg1 = LineEdit()
        arg1.setText(str(step.get("arg1") or step.get("pattern") or ""))
        arg2 = LineEdit()
        arg2.setText(str(step.get("arg2") or ""))
        params.addWidget(op_combo)
        params.addWidget(arg1, 1)
        params.addWidget(arg2, 1)
        col.addLayout(params)

        prompt = TextEdit()
        prompt.setPlaceholderText("提示词 / 给操作者的说明:{input}/{previous} 为上一步产出(首步=原始输入),{origin} 为原始输入")
        prompt.setPlainText(step.get("prompt", ""))
        prompt.setFont(font(12))
        prompt.setFixedHeight(84)
        col.addWidget(prompt)

        row_ref = {"frame": frame, "kind": kind_combo, "combo": combo,
                   "url": url_edit, "prompt": prompt, "nlab": nlab,
                   "op": op_combo, "arg1": arg1, "arg2": arg2}

        def sync_kind():
            k = self._KINDS[kind_combo.currentIndex()][1]
            combo.setVisible(k == "ai")
            url_edit.setVisible(k == "web")
            op_combo.setVisible(k == "transform")
            arg1.setVisible(k in ("transform", "if"))
            arg2.setVisible(k == "transform")
            prompt.setVisible(k != "clipboard")
            if k == "transform":
                arg1.setPlaceholderText("查找内容(替换的原文 / 正则模式)")
                arg2.setPlaceholderText("替换为 / 加后缀内容")
            elif k == "if":
                arg1.setPlaceholderText("正则:当前内容命中则继续,未命中则停止自动串联")
            prompt.setPlaceholderText(
                "给操作者的说明(可选),显示在等待面板" if k == "human"
                else "提示词,{input}/{previous} 为上一步产出,{origin} 为原始输入")
        kind_combo.currentIndexChanged.connect(sync_kind)
        sync_kind()
        b_del.clicked.connect(lambda _=False, r=row_ref: self._del_row(r))
        self._rows.append(row_ref)
        self.steps_box.addWidget(frame)
        self._renumber()

    def _del_row(self, r):
        if len(self._rows) <= 1:
            InfoBar.warning("至少一步", "工作流至少保留一个步骤", parent=self,
                            position=InfoBarPosition.TOP_RIGHT)
            return
        self._rows.remove(r)
        self.steps_box.removeWidget(r["frame"])
        r["frame"].deleteLater()
        self._renumber()

    def _renumber(self):
        for i, r in enumerate(self._rows):
            r["nlab"].setText("第 %d 步" % (i + 1))

    def _save(self):
        name = self.e_name.text().strip()
        if not name:
            InfoBar.error("无法保存", "请填写工作流名称", parent=self,
                          position=InfoBarPosition.TOP_RIGHT)
            return
        steps = []
        for r in self._rows:
            k = self._KINDS[r["kind"].currentIndex()][1]
            prompt = r["prompt"].toPlainText()
            if k == "ai":
                svc = r["combo"].currentText().strip()
                if not svc:
                    InfoBar.error("无法保存", "本地 AI 步骤需要先在「AI 服务」中添加服务",
                                  parent=self, position=InfoBarPosition.TOP_RIGHT)
                    return
                steps.append({"kind": "ai", "service": svc, "prompt": prompt})
            elif k == "web":
                steps.append({"kind": "web", "prompt": prompt,
                              "url": r["url"].text().strip()})
            elif k == "human":
                steps.append({"kind": "human", "prompt": prompt})
            elif k == "clipboard":
                steps.append({"kind": "clipboard"})
            elif k == "transform":
                steps.append(wfs.TransformStep(
                    wfs.TransformStep.OPS[r["op"].currentIndex()],
                    r["arg1"].text(), r["arg2"].text()).to_dict())
            else:                                              # if
                steps.append({"kind": "if", "pattern": r["arg1"].text().strip()})
        wflist = self.app.cfg.setdefault("ai_workflows", [])
        cp = self._ctx_values[self.cmb_ctx.currentIndex()]
        if self.wf is not None:
            self.wf.clear()
            self.wf.update({"name": name, "steps": steps,
                            "context_pack": cp or None})
        else:
            if any(w.get("name") == name for w in wflist):
                InfoBar.error("名称重复", "已存在同名工作流:%s" % name, parent=self,
                              position=InfoBarPosition.TOP_RIGHT)
                return
            wflist.append({"name": name, "steps": steps,
                           "context_pack": cp or None})
        cm.save_config(self.app.cfg)
        if self.on_saved:
            self.on_saved()
        self.close()
        self.deleteLater()


class WorkflowPage(QWidget):
    """工作流管理(常驻单例):列表 + 新建/编辑/删除 + 填入示例 + 打开服务管理。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("workflowPage")

        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)
        ttl = TitleLabel("工作流")
        v.addWidget(ttl)
        hint = CaptionLabel("右键剪贴板页的文本条目 → 「工作流」即可运行。步骤可以是网页 AI(提示词进剪贴板、"
                            "你去网页粘贴)、人工确认(把关/编辑)或本地 API,自由组合、逐步推进。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.lst = QListWidget()
        self.lst.setFont(font(13))
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border-radius: 12px;"
            " border: 1px solid %s; outline: none; padding: 4px; }"
            "QListWidget::item { color: %s; border-radius: 8px; padding: 9px 12px; }"
            "QListWidget::item:hover { background: %s; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, BADGE_BG, ACCENT_SOFT, ACCENT))
        v.addWidget(self.lst, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_new = PushButton("新建", self)
        b_new.setIcon(FIF.ADD.icon())
        b_edit = PushButton("编辑", self)
        b_edit.setIcon(FIF.EDIT.icon())
        b_del = PushButton("删除", self)
        b_del.setIcon(FIF.DELETE.icon())
        b_seed = PushButton("填入示例模板", self)
        b_seed.setIcon(FIF.LIBRARY.icon())
        b_hist = PushButton("运行记录", self)
        b_hist.setIcon(FIF.HISTORY.icon())
        b_svc = PushButton("AI 服务…", self)
        b_svc.setIcon(FIF.CLOUD.icon())
        b_new.clicked.connect(self._new)
        b_edit.clicked.connect(self._edit_current)
        b_del.clicked.connect(self._del_current)
        b_seed.clicked.connect(self._seed)
        b_hist.clicked.connect(self._open_runs)
        b_svc.clicked.connect(self.app.show_services)
        for b in (b_new, b_edit, b_del, b_seed, b_hist, b_svc):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self._editors = []
        self._runs_dlg = None
        self.refresh()

    def _open_runs(self):
        """运行记录(Run Inspector):每步大小/耗时/状态 + 运行历史。"""
        if self._runs_dlg is None:
            self._runs_dlg = WorkflowRunInspector(self.app)
        self._runs_dlg._reload()
        self._runs_dlg.show()
        self._runs_dlg.raise_()
        self._runs_dlg.activateWindow()


    def refresh(self):
        self.lst.clear()
        for wf in self.app.cfg.get("ai_workflows") or []:
            parts = []
            for st in wf.get("steps") or []:
                k = st.get("kind", "ai")
                parts.append("网页" if k == "web" else "人工" if k == "human"
                             else (st.get("service") or "AI"))
            self.lst.addItem("%s\n%d 步:%s" % (wf.get("name", "未命名"),
                                               len(wf.get("steps") or []),
                                               " → ".join(parts)))

    def _current(self):
        item = self.lst.currentItem()
        if not item:
            return None
        i = self.lst.row(item)
        wfs = self.app.cfg.get("ai_workflows") or []
        return wfs[i] if 0 <= i < len(wfs) else None

    def _new(self):
        ed = WorkflowEditorWindow(self.app, None, self.refresh)
        ed.show()
        ed.raise_()
        self._editors.append(ed)

    def _edit_current(self):
        wf = self._current()
        if wf is not None:
            ed = WorkflowEditorWindow(self.app, wf, self.refresh)
            ed.show()
            ed.raise_()
            self._editors.append(ed)

    def _del_current(self):
        wf = self._current()
        if wf is None:
            return
        if not MessageBox("删除工作流", "确定删除「%s」?" % wf.get("name"), self).exec():
            return
        try:
            self.app.cfg.get("ai_workflows", []).remove(wf)
        except ValueError:
            pass
        cm.save_config(self.app.cfg)
        self.refresh()

    def _seed(self):
        cfg = self.app.cfg
        svcs = cfg.setdefault("ai_services", [])
        have_svc = {s.get("name") for s in svcs}
        for s in cm.AI_SEED_SERVICES:
            if s["name"] not in have_svc:
                svcs.append(dict(s))
        wfs = cfg.setdefault("ai_workflows", [])
        have_wf = {w.get("name") for w in wfs}
        added = 0
        for wf in cm.AI_SEED_WORKFLOWS:
            if wf["name"] not in have_wf:
                wfs.append(json.loads(json.dumps(wf, ensure_ascii=False)))
                added += 1
        cm.save_config(cfg)
        self.refresh()
        InfoBar.success("示例已加入", "新增 %d 个工作流模板;请到 设置 →「AI 模型与服务」填入密钥后使用" % added,
                        parent=self, position=InfoBarPosition.TOP_RIGHT)


def resolve_wf_context(app, wf):
    """工作流 → ({context} 拼装文本, 错误说明)。

    wf.context_pack:null(默认,不使用,行为完全不变)| 包 id | "@active"。
    拼装包含已放行的敏感条目(发送前各 AI 步骤仍必经隐私门);
    指令条目超预算 / 包不存在 / 没有启用条目 → 返回错误,运行台拒绝启动。"""
    cp = (wf or {}).get("context_pack")
    if not cp:
        return "", ""
    from ctxpack.ui_tray import tray_service
    from ctxpack.assemble import assemble
    from ctxpack.budget import BudgetError
    svc = tray_service(app)
    pack = svc.active_pack() if cp == "@active" else svc.get(cp)
    if pack is None:
        return "", "绑定的工作流上下文包不存在(可能已被删除)"
    try:
        a = assemble(pack, include_sensitive=True)
    except BudgetError as e:
        return "", "上下文包指令条目超出预算(%s);请调大预算或精简指令条目" % (
            e.detail or "INSTRUCTIONS_OVER_BUDGET")
    if not a.ok:
        return "", "上下文包没有启用中的条目,无法拼装"
    return a.text, ""


class WorkflowRunner(QWidget):
    """工作流运行台:步骤经 workflow_steps 契约分发(ai/web/human/
    clipboard/transform/if,见 STEP_REGISTRY)。

    web:提示词进剪贴板(可自动打开网页),等人粘贴回答;
    human:等待面板人工确认/编辑;ai:本地 API 自动调用;
    clipboard:快照当前内容;transform:本地文本变换;if:条件闸。
    所有步骤的产出都可编辑后再继续(人在回路)。
    """

    def __init__(self, app, wf, content):
        # {context}:绑定包先拼装,失败即拒绝启动(不发无上下文的半截请求)
        ctx_text, ctx_err = resolve_wf_context(app, wf)
        if ctx_err:
            raise RuntimeError(ctx_err)
        super().__init__(None)
        self.app = app
        self.wf = wf
        self.steps = wf.get("steps") or []
        self.step_idx = 0
        self._origin = content          # 工作流原始输入({origin})
        self._context_text = ctx_text   # {context} 上下文包拼装文本(恒定)
        self.cur = content                 # 链上当前内容(可能是人工编辑后的)
        self.results = []                  # 每步产出的文本
        self._worker = None
        self._waiting = None               # 等待输入面板(卡部件)
        self._wait_body = None
        self._wait_confirm = None
        self._dead = False
        self._auto = False
        self._ctx = None                   # WorkflowContext(惰性,宿主回调注入)
        self._cur_step = None              # 当前步骤实例(取消用)
        # 运行记录(Run Inspector):只记大小/耗时/状态,正文不落盘;
        # 关闭窗口或跑完全程时收尾保存(见 _maybe_finish_runlog/closeEvent)
        self._runlog = wrl.RunRecorder(wf.get("name", ""), content,
                                       len(self.steps))
        self._runlog_saved = False

        self.setWindowTitle("工作流 — " + wf.get("name", ""))
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(600, 540)

        v = QVBoxLayout(self)
        v.setContentsMargins(36, 24, 36, 16)
        v.setSpacing(10)

        head = QHBoxLayout()
        icon = QLabel("🧩")
        icon.setFont(font(16))
        ttl = StrongBodyLabel(wf.get("name", "未命名"))
        ttl.setFont(font(15, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        self.chip = CaptionLabel("")
        self.chip.setStyleSheet(
            "QLabel { background: %s; color: %s; border-radius: 8px; padding: 3px 8px; }"
            % (ACCENT_SOFT, ACCENT))
        head.addWidget(icon)
        head.addSpacing(6)
        head.addWidget(ttl)
        head.addStretch(1)
        head.addWidget(self.chip)
        v.addLayout(head)

        hint = CaptionLabel("「{input}」= 输入内容/上一步输出;网页步骤会把提示词放进剪贴板,"
                            "网页回答粘贴回来即可;每步都可编辑后再继续。")
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        self.flow = QVBoxLayout(content_w)
        self.flow.setContentsMargins(2, 2, 6, 2)
        self.flow.setSpacing(10)
        self.flow.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(content_w)
        v.addWidget(self.scroll, 1)

        self.lbl_status = CaptionLabel("就绪")
        self.lbl_status.setStyleSheet("color: %s;" % SEC)
        v.addWidget(self.lbl_status)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.btn_next = PrimaryPushButton(self)
        self.btn_next.clicked.connect(self._on_next)
        self.btn_auto = PushButton("自动跑完", self)
        self.btn_auto.clicked.connect(self._on_auto)
        self.btn_copy = PushButton("复制最新结果", self)
        self.btn_copy.setIcon(FIF.COPY.icon())
        self.btn_copy.clicked.connect(self._copy_latest)
        b_close = PushButton("关闭", self)
        b_close.clicked.connect(self.close)
        foot.addWidget(self.btn_next)
        foot.addWidget(self.btn_auto)
        foot.addWidget(self.btn_copy)
        foot.addStretch(1)
        foot.addWidget(b_close)
        v.addLayout(foot)

        self._add_block("输入 · 原始内容", content, kind="input")
        self._update_ui()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    # ---- 步骤块 ----
    @staticmethod
    def _step_label(step):
        return wfs.describe(step)

    def _add_block(self, title, text, kind="result", idx=None):
        card = QFrame()
        card.setObjectName("aiCard")
        card.setStyleSheet("QFrame#aiCard { background: %s; border-radius: 12px;"
                           " border: 1px solid %s; }" % (CARD_BG, CARD_BORDER))
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 10, 14, 12)
        col.setSpacing(8)

        head = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background: %s; border-radius: 4px;"
                          % (ACCENT if kind != "info" else BADGE_BG))
        lab = CaptionLabel(title)
        lab.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        head.addWidget(dot)
        head.addSpacing(4)
        head.addWidget(lab)
        head.addStretch(1)
        col.addLayout(head)

        body = TextEdit()
        body.setReadOnly(True)
        body.setPlainText(text)
        body.setFont(font(12))
        body.setFixedHeight(max(56, 19 * min(text.count("\n") + 1, 12) + 14))
        col.addWidget(body)

        if kind in ("result", "info"):
            state = {"text": text}
            row = QHBoxLayout()
            row.setSpacing(6)
            b_copy = PushButton("复制", card)
            b_copy.setIcon(FIF.COPY.icon())
            b_copy.clicked.connect(lambda: self._copy_text(state["text"]))
            row.addWidget(b_copy)
            if kind == "result":
                b_edit = PushButton("编辑", card)
                b_edit.setIcon(FIF.EDIT.icon())
                b_save = PrimaryPushButton("保存修改", card)
                b_save.setVisible(False)
                row.addWidget(b_edit)
                row.addWidget(b_save)

                def do_edit():
                    body.setReadOnly(False)
                    body.setMinimumHeight(max(120, body.height()))
                    b_edit.setVisible(False)
                    b_save.setVisible(True)
                    body.setFocus()

                def do_save():
                    body.setReadOnly(True)
                    b_edit.setVisible(True)
                    b_save.setVisible(False)
                    state["text"] = body.toPlainText()
                    if idx is not None and 0 <= idx < len(self.results):
                        self.results[idx] = state["text"]
                        if idx == len(self.results) - 1:   # 只有最新结果能改写链上输入
                            self.cur = state["text"]
                            self._status("已保存修改,下一步将使用编辑后的内容")

                b_edit.clicked.connect(do_edit)
                b_save.clicked.connect(do_save)
            row.addStretch(1)
            col.addLayout(row)

        self.flow.addWidget(card)
        sb = self.scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def _show_wait_panel(self, idx, hint, prefill=""):
        """人工/网页步骤的等待面板:粘贴/编辑 → 确认后进入链。"""
        self._auto = False                  # 人工介入即停止自动串联
        card = QFrame()
        card.setObjectName("waitCard")
        card.setStyleSheet("QFrame#waitCard { background: %s; border-radius: 12px;"
                           " border: 1.5px dashed %s; }" % (CARD_BG, ACCENT))
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 10, 14, 12)
        col.setSpacing(8)

        lab = CaptionLabel("✍️ 步骤 %d · 等待输入" % (idx + 1))
        lab.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        col.addWidget(lab)
        hintlab = CaptionLabel(hint)
        hintlab.setWordWrap(True)
        hintlab.setStyleSheet("color: %s;" % TER)
        col.addWidget(hintlab)

        body = TextEdit()
        body.setPlainText(prefill)
        body.setFont(font(12))
        body.setFixedHeight(150)
        col.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(6)
        b_paste = PushButton("📋 从剪贴板粘贴", card)
        b_paste.setIcon(FIF.PASTE.icon())
        b_ok = PrimaryPushButton("✓ 确认,继续", card)
        row.addWidget(b_paste)
        row.addWidget(b_ok)
        row.addStretch(1)
        col.addLayout(row)

        def do_paste():
            body.setPlainText(QApplication.clipboard().text())

        def confirm():
            text = body.toPlainText().strip()
            if not text:
                InfoBar.warning("内容为空", "请粘贴或输入内容后再继续", parent=self,
                                position=InfoBarPosition.TOP_RIGHT)
                return
            body.setReadOnly(True)
            b_paste.setVisible(False)
            b_ok.setVisible(False)
            lab.setText("✓ 步骤 %d · 完成" % (idx + 1))
            card.setObjectName("aiCard")
            card.setStyleSheet("QFrame#aiCard { background: %s; border-radius: 12px;"
                               " border: 1px solid %s; }" % (CARD_BG, CARD_BORDER))
            self.results.append(text)
            self.cur = text
            self.step_idx += 1
            self._runlog.step_ok(idx, text)
            self._waiting = None
            self._wait_body = None
            self._wait_confirm = None
            if self.step_idx >= len(self.steps):
                self._set_busy(False, "全部 %d 步完成,结果可复制" % len(self.steps))
                InfoBar.success("工作流完成", "共 %d 步,最新结果已可复制" % len(self.steps),
                                parent=self, position=InfoBarPosition.TOP_RIGHT)
                self._maybe_finish_runlog()
            else:
                self._set_busy(False, "步骤 %d 完成;可继续下一步" % idx)

        b_paste.clicked.connect(do_paste)
        b_ok.clicked.connect(confirm)
        self.flow.addWidget(card)
        sb = self.scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))
        self._waiting = card
        self._wait_body = body
        self._wait_confirm = confirm
        self._set_busy(True, "等待人工输入(步骤 %d):网页端操作完成后回来粘贴/确认" % (idx + 1))
        body.setFocus()

    # ---- 状态 / 按钮 ----
    def _status(self, msg):
        self.lbl_status.setText(msg)

    def _update_ui(self):
        n, i = len(self.steps), self.step_idx
        done = i >= n
        self.chip.setText("已完成 %d 步" % n if done else "步骤 %d / %d" % (i + 1, n))
        if done:
            self.btn_next.setText("✔ 已完成")
            self.btn_next.setEnabled(False)
        else:
            self.btn_next.setText("▶ 执行第 %d 步 · %s"
                                  % (i + 1, self._step_label(self.steps[i])))
            self.btn_next.setEnabled(True)
        self.btn_auto.setEnabled(not done and self._worker is None and self._waiting is None)
        self.btn_copy.setEnabled(bool(self.results))

    def _set_busy(self, on, msg=None):
        self._status(msg or "")
        if on:
            if self._waiting is not None:
                self.btn_next.setText("等待输入…")
            elif self._auto:
                self.btn_next.setText("停止自动")
                self.btn_next.setEnabled(True)
            else:
                self.btn_next.setText("运行中…")
            self.btn_next.setEnabled(self._waiting is None)
        else:
            self._update_ui()
        self.btn_auto.setEnabled(not on and self.step_idx < len(self.steps)
                                 and self._waiting is None)
        self.btn_copy.setEnabled(bool(self.results))

    # ---- 运行 ----
    def _on_next(self):
        if self._worker is not None or self._waiting is not None:
            if self._worker is not None and self._auto:      # 充当「停止自动」
                self._auto = False
                if self._cur_step is not None:
                    self._cur_step.cancel()   # 契约取消:迟到结果作废
                self._set_busy(False, "已停止:当前步骤结果将被丢弃")
            return
        self._run_step()

    def _on_auto(self):
        self._auto = True
        self._run_step()

    # ---- Step Contract 宿主侧:上下文与回调 ----
    def _ensure_ctx(self):
        if self._ctx is not None:
            return
        self._ctx = wfs.WorkflowContext(
            input_text=self._origin,
            find_service=lambda name: cm.ai_service_by_name(self.app.cfg, name),
            privacy_gate=self._privacy_gate,
            start_ai=self._start_ai,
            copy_text=self.app.core.copy_text,
            open_url=self._open_url,
            context=self._context_text)

    def _open_url(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
            return True
        except Exception:
            return False

    def _start_ai(self, svc, prompt, on_done):
        """起 AIWorker 线程;返回句柄(cancel 为尽力而为:HTTP 不可中断,
        迟到结果由 Job.cancelled 丢弃)。"""
        w = AIWorker(svc, prompt, self.step_idx)
        w.done.connect(lambda _i, ok, text: on_done(ok, text))
        w.finished.connect(self._worker_finished)
        self._worker = w
        w.start()
        return w

    def _run_step(self):
        if self._worker is not None or self._waiting is not None \
                or self.step_idx >= len(self.steps):
            return
        idx = self.step_idx
        self._ensure_ctx()
        self._ctx.index = idx
        self._ctx.previous = self.cur
        step = wfs.deserialize(self.steps[idx])
        self._cur_step = step
        self._runlog.step_start(idx, step.kind, step.describe(), self.cur)

        err = step.validate(self._ctx)          # 契约:运行前检查
        if err is not None:
            self._auto = False
            self._runlog.step_error(idx, "配置无效: " + err)
            InfoBar.error("无法运行", err, parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=-1)
            self._set_busy(False, "步骤 %d 配置无效,已停止" % (idx + 1))
            return

        res = step.execute(self._ctx)           # 契约:执行
        if isinstance(res, wfs.Cancelled):
            self._auto = False
            self._runlog.step_cancelled(idx, res.reason)
            self._set_busy(False, "步骤 %d 已取消(%s)" % (idx + 1, res.reason))
            return
        if isinstance(res, wfs.Wait):
            if res.note is not None:
                self._add_block("步骤 %d · %s" % (idx + 1, res.note[0]),
                                res.note[1], "info")
            self._show_wait_panel(idx, res.hint, res.prefill)
            return
        if isinstance(res, wfs.Job):
            self._set_busy(True, "步骤 %d 正在执行(%s)…" % (idx + 1, step.describe()))
            res.start(lambda ok, text, _i=idx: self._on_done(_i, ok, text))
            return
        if isinstance(res, wfs.Done):           # 同步步骤(clipboard/transform/if)
            if res.note is not None:
                self._add_block("步骤 %d · %s" % (idx + 1, res.note[0]),
                                res.note[1], "info")
            self._step_complete(idx, res.text, halt=res.halt)
            if self._auto and self.step_idx < len(self.steps):
                self._run_step()                # 自动模式:同步步骤直接链推

    def _step_complete(self, idx, text, halt=False):
        """一步成功收尾:记录产出、推进游标、刷新界面(halt=条件停链)。"""
        self.results.append(text)
        self.cur = text
        self.step_idx = idx + 1
        self._runlog.step_ok(idx, text, halt=halt)
        self._add_block("步骤 %d · %s" % (idx + 1, self._step_label(self.steps[idx])),
                        text, "result", idx)
        if self.step_idx >= len(self.steps):
            self._set_busy(False, "全部 %d 步完成,结果可复制" % len(self.steps))
            InfoBar.success("工作流完成", "共 %d 步,最新结果已可复制" % len(self.steps),
                            parent=self, position=InfoBarPosition.TOP_RIGHT)
            self._maybe_finish_runlog()
        elif halt:
            self._auto = False
            self._set_busy(False, "步骤 %d 条件不满足,自动串联已停(可手动继续)" % (idx + 1))
        elif not self._auto:
            self._set_busy(False, "步骤 %d 完成;可编辑结果或直接继续" % idx)

    def _maybe_finish_runlog(self):
        """全部步骤走完:收尾运行记录并落盘(只记大小/耗时/状态)。
        保存失败不打断工作流,经现有提示渠道反馈。"""
        if self._runlog_saved:
            return
        self._runlog_saved = True
        try:
            if wrl.save_run(self._runlog.finish()) is None:
                InfoBar.warning("运行记录未保存", "运行已完成;运行记录写入失败"
                                "(原文件保留),详情见 app.log", parent=self,
                                position=InfoBarPosition.TOP_RIGHT, duration=5000)
        except Exception:
            cm.log("运行记录保存失败:\n%s" % traceback.format_exc())

    def _privacy_gate(self, prompt, svc):
        """AI 发送前隐私门:命中敏感内容弹三选一(遮盖/原样/取消);
        无命中原样放行。返回最终提示词,取消返回 None。"""
        hits, masked = cm.privacy_scan(prompt)
        if not hits:
            return prompt
        dlg = PrivacyGateDialog(self, hits, masked,
                                str(svc.get("base_url") or ""))
        dlg.exec()
        if dlg.choice == "mask":
            return masked
        if dlg.choice == "raw":
            return prompt
        return None

    def _on_done(self, idx, ok, text):
        """异步步骤(Job)的完成回调(失败停在当前步可重试)。"""
        if self._dead:
            return
        if not ok:
            self._auto = False
            self._runlog.step_error(idx, text)
            self._set_busy(False, "步骤 %d 失败,可重试" % (idx + 1))
            InfoBar.error("调用失败", text[:220], parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=-1)
            return
        self._step_complete(idx, text)

    def _worker_finished(self):
        self._worker = None
        if self._dead:
            self.close()                       # 此时 worker 已结束,closeEvent 会真正关闭
        elif self._auto and self.step_idx < len(self.steps):
            self._run_step()                   # 自动模式:链式推进
        else:
            self._update_ui()

    # ---- 复制 / 关闭 ----
    def _copy_text(self, text):
        # 「复制」= 只写剪贴板(不派发 Ctrl+V);显式粘贴入口另行调用
        # core.paste_text(force=True)。写入失败如实提示,不宣称已复制
        if not self.app.core.copy_text(text):
            InfoBar.error("复制失败", "剪贴板写入失败,请重试", parent=self,
                          position=InfoBarPosition.TOP_RIGHT, duration=5000)
            return False
        return True

    def _copy_latest(self):
        if self.results and self._copy_text(self.results[-1]):
            InfoBar.success("已复制", "结果已放入剪贴板",
                            parent=self, position=InfoBarPosition.TOP_RIGHT)

    def closeEvent(self, e):
        if self._worker is not None and self._worker.isRunning():
            self._dead = True
            self.hide()                        # 等线程收尾,避免 QThread 析构崩溃
            e.ignore()
        else:
            try:
                self.app._ai_runners.remove(self)
            except ValueError:
                pass
            # 中途关闭:运行记录以 partial 落盘(至少跑过一步才记,
            # 零步关闭不污染历史)
            if not self._runlog_saved and self._runlog.steps:
                try:
                    wrl.save_run(self._runlog.abort())
                except Exception:
                    cm.log("运行记录保存失败:\n%s" % traceback.format_exc())
            self.deleteLater()
            e.accept()


# ============================================================
# Workflow Run Inspector(运行记录查看,建议 18)
# ============================================================
class WorkflowRunInspector(QWidget):
    """工作流运行历史:左列运行(最近在前),右侧选中运行的每步明细
    (大小/耗时/状态)。数据来自 workflow_runlog——只记大小不记内容。"""

    _STATUS_TXT = {"ok": "✓ 完成", "halt": "⛔ 停链(条件)", "error": "✗ 失败",
                   "cancelled": "已取消", "running": "—"}
    _RUN_TXT = {"done": "完成", "failed": "失败", "cancelled": "已取消",
                "partial": "未跑完", "running": "进行中"}

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowTitle("工作流运行记录")
        self.setWindowIcon(app.icon)
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet("QWidget { background: %s; }" % BG)
        self.resize(780, 520)

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 20, 28, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        icon = QLabel("🧾")
        icon.setFont(font(16))
        ttl = StrongBodyLabel("运行记录(Run Inspector)")
        ttl.setFont(font(15, QFont.DemiBold))
        ttl.setStyleSheet("color: %s;" % TEXT)
        head.addWidget(icon)
        head.addSpacing(6)
        head.addWidget(ttl)
        head.addStretch(1)
        v.addLayout(head)
        hint = CaptionLabel("每次运行只记录每步的输入/输出大小、耗时与状态,不记录任何内容;"
                            "错误文案已脱敏。最多保留 %d 条,超出丢最旧。" % wrl.KEEP_RUNS)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s;" % TER)
        v.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(10)
        self.lst = QListWidget()
        self.lst.setFont(font(12))
        self.lst.setFixedWidth(290)
        self.lst.setStyleSheet(
            "QListWidget { background: %s; border-radius: 12px;"
            " border: 1px solid %s; outline: none; padding: 4px; }"
            "QListWidget::item { color: %s; border-radius: 8px; padding: 8px 10px; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (FIELD_BG, FIELD_BORDER, TEXT, ACCENT_SOFT, ACCENT))
        body.addWidget(self.lst)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.lbl_sum = CaptionLabel("选择左侧一条运行查看明细")
        self.lbl_sum.setWordWrap(True)
        self.lbl_sum.setStyleSheet("color: %s; font-weight: 600;" % SEC)
        right.addWidget(self.lbl_sum)
        self.tbl = TableWidget(self)
        self.tbl.setColumnCount(6)
        self.tbl.setHorizontalHeaderLabels(
            ["步骤", "类型", "状态", "输入→输出", "耗时", "说明"])
        self.tbl.verticalHeader().hide()
        self.tbl.setEditTriggers(TableWidget.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        right.addWidget(self.tbl, 1)
        body.addLayout(right, 1)
        v.addLayout(body, 1)

        foot = QHBoxLayout()
        b_clear = PushButton("清空记录", self)
        b_clear.setIcon(FIF.DELETE.icon())
        b_clear.clicked.connect(self._clear)
        foot.addWidget(b_clear)
        foot.addStretch(1)
        b_close = PushButton("关闭", self)
        b_close.clicked.connect(self.close)
        foot.addWidget(b_close)
        v.addLayout(foot)

        self.lst.currentRowChanged.connect(self._show_run)
        self._runs = []
        self._reload()

    def showEvent(self, e):
        super().showEvent(e)
        ensure_onscreen(self)

    def _reload(self):
        self._runs = wrl.load_runs()
        self.lst.blockSignals(True)
        self.lst.clear()
        for r in self._runs:
            self.lst.addItem(self._run_line(r))
        self.lst.blockSignals(False)
        if self._runs:
            self.lst.setCurrentRow(0)
        else:
            self._show_run(-1)

    @staticmethod
    def _run_line(r):
        t = time.strftime("%m-%d %H:%M",
                          time.localtime(r.get("started_at") or 0))
        status = WorkflowRunInspector._RUN_TXT.get(
            r.get("status"), str(r.get("status")))
        return "#%s · %s\n%s · %d 步 · %s" % (
            r.get("id") or "?", r.get("workflow") or "?", t,
            r.get("total_steps") or 0, status)

    def _show_run(self, row):
        self.tbl.setRowCount(0)
        if not (0 <= row < len(self._runs)):
            self.lbl_sum.setText("暂无运行记录" if not self._runs
                                 else "选择左侧一条运行查看明细")
            return
        r = self._runs[row]
        dur = float(r.get("finished_at") or 0) - float(r.get("started_at") or 0)
        self.lbl_sum.setText(
            "「%s」 输入 %s 字符 → 输出 %s 字符 · 总耗时 %s"
            % (r.get("workflow") or "?", r.get("input_chars", 0),
               r.get("output_chars", 0), self._fmt_sec(dur)))
        for s in r.get("steps") or []:
            i = self.tbl.rowCount()
            self.tbl.insertRow(i)
            vals = (str((s.get("idx") or 0) + 1),
                    s.get("label") or s.get("kind") or "?",
                    self._STATUS_TXT.get(s.get("status"),
                                         str(s.get("status"))),
                    "%s → %s 字符" % (s.get("input_chars", 0),
                                      s.get("output_chars", 0)),
                    self._fmt_ms(s.get("ms")),
                    s.get("error") or "")
            for c, txt in enumerate(vals):
                self.tbl.setItem(i, c, QTableWidgetItem(str(txt)))

    @staticmethod
    def _fmt_ms(ms):
        if ms is None:
            return "—"
        return "%.1f s" % (ms / 1000.0) if ms >= 1000 else "%.0f ms" % ms

    @staticmethod
    def _fmt_sec(sec):
        if sec < 0:
            return "—"
        if sec >= 60:
            return "%d 分 %.0f 秒" % (sec // 60, sec % 60)
        return "%.1f 秒" % sec

    def _clear(self):
        if not self._runs:
            return
        if MessageBox("清空运行记录",
                      "确定删除全部 %d 条运行记录?此操作不可撤销。"
                      % len(self._runs), self).exec():
            try:
                if not wrl.clear_runs():
                    InfoBar.error("清空失败", "运行记录写入失败,原文件保留;"
                                  "详情见 app.log", parent=self,
                                  position=InfoBarPosition.TOP_RIGHT,
                                  duration=5000)
            except Exception:
                cm.log("清空运行记录失败:\n%s" % traceback.format_exc())
            self._reload()


