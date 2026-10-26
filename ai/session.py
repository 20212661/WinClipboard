# -*- coding: utf-8 -*-
"""AI 会话执行核(PR3B-1):QuickAIWindow 与 EmbeddedAIPanel 的共享层。

从两个面板逐行同文的重复块抽取:发送纪律(串号/在途上限/迟到丢弃/
QApplication 销毁守卫)、服务解析、隐私授权(privacy_scan 策略 + 宿主
注入的确认弹窗)、上下文拼装策略。面板保留全部 UI 与属性外观——
_active/_pending 经属性/同引用集合转发到本会话,现有测试与调用方
不受影响。

纯 Python 无 Qt:线程结果经宿主注入的 emit 回调(生产 = Qt
Signal.emit,QueuedConnection 回 UI 线程)返回。cm.ai_chat 仍是唯一
模型调用出口,调用时点解析以便测试 patch 生效。

取消语义如实:stop_receiving 只让 UI 停止接收并丢弃迟到结果,HTTP
在途请求不可中断——不得宣称「已取消请求」。
"""
import threading

import clipboard_manager as cm

MAX_PENDING = 2


def resolve_service(services, index):
    """下拉选中项 → 服务 dict 副本;越界/无服务返回 None。

    副本隔离:调用方就地改写不影响配置列表。"""
    if not 0 <= index < len(services or []):
        return None
    return dict(services[index])


def authorize(prompt, service, confirm):
    """发送前隐私授权:对本次实际发送的完整输入做 privacy_scan。

    无命中原样放行(不弹窗);命中时交宿主确认
    confirm(hits, masked, endpoint) → "mask"/"raw"/"cancel"。
    返回最终要发送的提示词;取消返回 None。"""
    hits, masked = cm.privacy_scan(prompt)
    if not hits:
        return prompt
    choice = confirm(hits, masked, (service or {}).get("base_url") or "")
    if choice == "mask":
        return masked
    if choice == "raw":
        return prompt
    return None


def assemble_context(enabled, assemble_active, notify=None):
    """勾选附加时拼装当前上下文包;失败抛 ValueError(send/copy 共用)。

    assemble_active() 由宿主注入(生产 = ctxpack tray_service 的
    assemble_active(include_sensitive=True),拼装即快照);策略统一在
    此:None/不可拼装 → 报错,warnings → 交 notify 呈现(最多 3 条)。"""
    if not enabled:
        return ""
    assembled = assemble_active()
    if assembled is None or not assembled.ok:
        raise ValueError("没有可用的当前上下文包，请先添加资料或取消附加")
    warnings = list(getattr(assembled, "warnings", None) or [])
    if warnings and notify is not None:
        notify(warnings[:3])
    return assembled.text


class ChatSession:
    """单面板会话状态机 + 发送线程纪律。

    状态:idle / running(active=request_id)/ shutdown;最多
    MAX_PENDING 条在途。receive() 只对 active 匹配且未 shutdown 的
    请求返回 True(迟到/已停止接收的结果由调用方丢弃)。
    """

    def __init__(self, emit, chat=None):
        self._emit = emit          # (request_id, ok, text) → UI 线程回调
        self._chat = chat          # 测试注入;缺省在调用时解析 cm.ai_chat
        self._serial = 0
        self.active = None         # 当前呈现归属的请求(面板 _active 转发)
        self.pending = set()       # 在途集合(面板 _pending 与之同引用)
        self.shutdown_flag = False

    @property
    def busy(self):
        return self.active is not None

    def can_start(self):
        return (not self.shutdown_flag and self.active is None
                and len(self.pending) < MAX_PENDING)

    def spawn(self, service, prompt):
        """登记一次发送并在后台 daemon 线程调用 cm.ai_chat;返回 request_id。

        prompt/service 按值冻结:发送后用户切卡片、改上下文篮,都不
        影响在途请求。"""
        self._serial += 1
        request_id = self._serial
        self.active = request_id
        self.pending.add(request_id)

        def run():
            try:
                fn = self._chat or cm.ai_chat   # 调用时解析,patch 可见
                ok, text = fn(service, prompt)
            except Exception as exc:
                ok, text = False, cm.sanitize_log(str(exc))
            try:
                self._emit(request_id, ok, text)
            except RuntimeError:
                pass  # QApplication 已销毁(窗口关闭后的迟到回调)

        threading.Thread(target=run, name="quick-ai", daemon=True).start()
        return request_id

    def receive(self, request_id):
        """在途结束登记;返回该结果是否应呈现(active 匹配且未 shutdown)。"""
        self.pending.discard(request_id)
        if request_id == self.active and not self.shutdown_flag:
            self.active = None
            return True
        return False

    def stop_receiving(self):
        """UI 停止接收:迟到结果将被丢弃;HTTP 在途请求不会被中断。"""
        self.active = None

    def shutdown(self):
        self.shutdown_flag = True
        self.active = None
