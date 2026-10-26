# -*- coding: utf-8 -*-
"""AI Action Registry(PR3A,移植自 feature/ai-action-bar 并适配 main)。

运行: python _ai_actions_test.py   退出码 0 = 全部通过
覆盖:
  1. 动作注册表:内置装载/排序/字段校验/for_record 过滤/用户目录扫描(覆盖与坏文件)
  2. 执行器:工作流解析(用户配置优先/内置回落/深拷贝隔离/守卫按 applies 放行)
  3. 契约:ActionRequest/ActionResult + run_request(context_pack 注入/auto 形参)
  4. 默认服务:cm.ai_service_by_name 空名 = 第一个已配置服务
  5. Step Contract:AiStep 空服务经 find_service 回退到默认服务
  6. 端到端:假 AI 服务器 + WorkflowRunner 跑通内置「解释代码」(空服务名)

与分支版的差异:main 工作流编辑器尚无「(默认服务)」下拉项(分支 UI
件未随移植),对应断言移除;新增契约段。UI 入口接线(卡片菜单等)
不在 PR3A 范围。
"""
import os
import sys
import json
import time
import tempfile
import threading
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 隔离持久化路径 ----
import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_aiact_test_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

import ai
import ai.registry
import ai.executor
import workflow_steps as wfs
from pathlib import Path

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    sys.stdout.flush()
    if not cond:
        FAILS.append(name)


# ============================================================
# 假 AI 服务器(OpenAI 兼容;同 _ai_test 的回显协议)
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            user = body["messages"][0]["content"]
            model = body.get("model", "?")
            self._reply(200, {
                "id": "x", "object": "chat.completion", "created": 1,
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "[%s] 处理了:%s" % (model, user)}}]})
        except Exception as e:
            self._reply(400, {"error": {"message": str(e)}})

    def _reply(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 0), FakeAI)
BASE = "http://127.0.0.1:%d/v1" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ============================================================
# 1. 动作注册表
# ============================================================
reg = ai.init()
A = reg.all()
check("registry.builtin-count", len(A) == 6, str(len(A)))
_ids = {d["id"] for d in A}
check("registry.builtin-ids",
      {"code_explain", "code_optimize", "code_comment",
       "summarize", "translate", "to_markdown"} <= _ids, str(_ids))
check("registry.fields", all(
    all(isinstance(d.get(k), str) and d[k].strip()
        for k in ("name", "id", "workflow")) for d in A))
check("registry.order", [d["id"] for d in A] == [
    "code_explain", "code_optimize", "code_comment",
    "summarize", "translate", "to_markdown"], str([d["id"] for d in A]))
check("registry.for-text", len(reg.for_record({"type": "text"})) == 6)
check("registry.for-image", reg.for_record({"type": "image"}) == [])
check("registry.for-files", reg.for_record({"type": "files"}) == [])
check("registry.get", reg.get("translate") is not None
      and reg.get("nope") is None)

# 校验:缺字段/坏类型不入册
r2 = ai.ActionRegistry()
check("registry.reject-missing", not r2.register({"name": "x"}))
check("registry.reject-empty-id", not r2.register(
    {"name": "x", "id": " ", "workflow": "w"}))
check("registry.reject-bad-applies", not r2.register(
    {"name": "x", "id": "x", "workflow": "w", "applies": "text"}))
check("registry.reject-non-dict", not r2.register(["x"]))
check("registry.errors-recorded", len(r2.errors) == 4, str(r2.errors))

# 用户目录:合法装载 + 同 id 覆盖 + 坏文件跳过
UD = os.path.join(TMP, "user_actions")
os.makedirs(UD, exist_ok=True)
Path(os.path.join(UD, "my.json")).write_text(json.dumps({
    "id": "my_action", "name": "我的动作", "workflow": "summarize"},
    ensure_ascii=False), encoding="utf-8")
Path(os.path.join(UD, "override.json")).write_text(json.dumps({
    "id": "translate", "name": "翻译(我的)", "workflow": "translate"},
    ensure_ascii=False), encoding="utf-8")
Path(os.path.join(UD, "broken.json")).write_text("{not json", encoding="utf-8")
Path(os.path.join(UD, "bad.json")).write_text(
    json.dumps({"name": "没id"}), encoding="utf-8")
reg3 = ai.ActionRegistry()
reg3.scan_dir(ai.registry.BUILTIN_ACTIONS_DIR)
n3 = reg3.scan_dir(UD)
check("registry.user-loaded", n3 == 2, str(n3))
check("registry.user-custom", reg3.get("my_action") is not None)
check("registry.user-override", reg3.get("translate")["name"] == "翻译(我的)")
check("registry.user-bad-skipped", len(reg3.errors) == 2, str(reg3.errors))
check("registry.missing-dir", ai.ActionRegistry().scan_dir(
    os.path.join(TMP, "no_such_dir")) == 0)

# ============================================================
# 2. 执行器:解析优先级 / 深拷贝 / applies 守卫
# ============================================================
CFG = {
    "ai_services": [
        {"name": "A", "base_url": BASE, "model": "model-a", "api_key": "k"},
    ],
    "ai_workflows": [
        {"name": "解释代码", "steps": [
            {"kind": "ai", "service": "A", "prompt": "用户覆盖版:{input}"}]},
    ],
}
wf = ai.executor.resolve_workflow(CFG, "code_explain")
check("executor.user-first", wf["steps"][0]["prompt"].startswith("用户覆盖版"))
wf2 = ai.executor.resolve_workflow({"ai_workflows": []}, "summarize")
check("executor.builtin-fallback",
      wf2["steps"][0]["kind"] == "ai" and wf2["steps"][0]["service"] == "")
wf2["steps"][0]["prompt"] = "已变异"
check("executor.deepcopy",
      "已变异" not in ai.workflows.BUILTIN_WORKFLOWS["summarize"]["steps"][0]["prompt"])
check("executor.missing", ai.executor.resolve_workflow(CFG, "不存在的") is None)
check("executor.empty-ref", ai.executor.resolve_workflow(CFG, "") is None)


class Host:
    def __init__(self, cfg):
        self.cfg = cfg
        self.runs = []

    def run_workflow(self, wf, rec, auto=False):
        self.runs.append((wf, rec, auto))


h = Host(CFG)
ok, msg = ai.executor.run_action(h, {"workflow": "translate"},
                                 {"type": "text", "content": "hello"})
check("executor.run-ok", ok and h.runs and h.runs[0][0]["name"] == "翻译", msg)
ok, msg = ai.executor.run_action(h, {"workflow": "translate"},
                                 {"type": "image", "content": ""})
check("executor.run-non-text", not ok and len(h.runs) == 1, msg)
ok, msg = ai.executor.run_action(h, {"workflow": "nope"},
                                 {"type": "text", "content": "x"})
check("executor.run-missing-wf", not ok and "未找到" in msg, msg)
# applies 扩展位:非 text 动作放行 files 记录
ok, msg = ai.executor.run_action(
    h, {"workflow": "translate", "name": "文件动作", "applies": ["files"]},
    {"type": "files", "content": ["C:/a.txt"]})
check("executor.applies-files-ok", ok and h.runs[-1][1]["type"] == "files", msg)

# ============================================================
# 3. 契约:ActionRequest / ActionResult / run_request
# ============================================================
req0 = ai.ActionRequest(action_id="translate",
                        record={"type": "text", "content": "hi"})
check("contract.defaults",
      req0.context_pack is None and req0.params is None
      and req0.auto is False)
res0 = ai.executor.run_request(h, h.cfg and {"workflow": "translate"}, req0)
check("contract.run-ok",
      isinstance(res0, ai.ActionResult) and res0.ok
      and res0.workflow["name"] == "翻译"
      and "context_pack" not in res0.workflow)
req1 = ai.ActionRequest(action_id="summarize",
                        record={"type": "text", "content": "x"},
                        context_pack="@active", auto=True)
res1 = ai.executor.run_request(h, {"workflow": "summarize"}, req1)
check("contract.context-injected",
      res1.ok and h.runs[-1][0].get("context_pack") == "@active")
check("contract.auto-passed", h.runs[-1][2] is True)
check("contract.user-wf-untouched",
      CFG["ai_workflows"][0].get("context_pack") is None)   # 副本注入
res2 = ai.executor.run_request(h, {"workflow": "nope"}, ai.ActionRequest(
    action_id="nope", record={"type": "text", "content": "x"}))
check("contract.guard-missing-wf", not res2.ok and "未找到" in res2.detail)

# ============================================================
# 4. 默认服务解析(cm.ai_service_by_name 空名回退)
# ============================================================
two = {"ai_services": [{"name": "A"}, {"name": "B"}]}
check("svc.empty-first", cm.ai_service_by_name(two, "") is two["ai_services"][0])
check("svc.none-first", cm.ai_service_by_name(two, None)["name"] == "A")
check("svc.empty-no-services",
      cm.ai_service_by_name({"ai_services": []}, "") is None)
check("svc.exact", cm.ai_service_by_name(two, "B")["name"] == "B")
check("svc.unknown", cm.ai_service_by_name(two, "Z") is None)
check("svc.no-key", cm.ai_service_by_name({}, "A") is None)

# ============================================================
# 5. Step Contract:AiStep 空服务 = 默认服务
# ============================================================
ctx = wfs.WorkflowContext(
    input_text="print(1)",
    find_service=lambda name: cm.ai_service_by_name(CFG, name))
step = wfs.AiStep("", "解释:{input}")
check("aistep.default-validate", step.validate(ctx) is None)
svc_seen = []


def _start(svc, prompt, done):
    svc_seen.append(svc)
    done(True, prompt)
    return None


ctx.start_ai = _start
got = []
res = step.run(ctx)
res.start(lambda ok, text: got.append((ok, text)))
check("aistep.default-service", svc_seen and svc_seen[0]["name"] == "A",
      str([s.get("name") for s in svc_seen]))
check("aistep.default-prompt", got and got[0] == (True, "解释:print(1)"), str(got))

# 空服务且无任何服务:校验给出可行动文案
ctx0 = wfs.WorkflowContext(
    input_text="x", find_service=lambda name: cm.ai_service_by_name({}, name))
step0 = wfs.AiStep("", "p:{input}")
err0 = step0.validate(ctx0)
check("aistep.no-service-msg", err0 is not None and "AI 服务" in err0, str(err0))

# ============================================================
# 6. 端到端:内置「解释代码」经运行台跑通(假 AI,空服务名)
# ============================================================
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QObject

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
import winclipboard_qt as wq
from qfluentwidgets import setTheme, Theme
setTheme(Theme.LIGHT)
wq.init_palette(False)


class FakeCore:
    pasted = []
    copied = []

    def paste_text(self, t, force=False):
        self.pasted.append(t)
        return True

    def copy_text(self, t):
        self.copied.append(t)
        return True


class FakeApp(QObject):
    """WorkflowRunner 所需的最小应用接口(同 _ai_test)。"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.core = FakeCore()
        self.icon = QIcon()
        self.ai_hub = None
        self._ai_runners = []

    def show_services(self):
        pass


fake = FakeApp({"ai_services": CFG["ai_services"], "ai_workflows": []})
wf_e2e = ai.executor.resolve_workflow(fake.cfg, "code_explain")
check("e2e.resolve", wf_e2e is not None and wf_e2e["steps"][0]["service"] == "")
runner = wq.WorkflowRunner(fake, wf_e2e, "print(2)")
fake._ai_runners.append(runner)
runner._on_auto()
deadline = time.time() + 15
while time.time() < deadline and runner.step_idx < 1:
    app.processEvents()
    time.sleep(0.02)
app.processEvents()
r = runner.results[0] if runner.results else ""
check("e2e.done", runner.step_idx == 1, "step_idx=%d" % runner.step_idx)
check("e2e.default-service-model", "[model-a]" in r, r)
check("e2e.prompt-has-input", "print(2)" in r and "资深工程师" in r, r)
runner.close()
app.processEvents()

# 端到端契约:run_request(auto=True) 同样经真实运行台路径
runner2 = None


class HostReal:
    """宿主 = FakeApp.run_workflow 的最小复刻(main 的三参签名)。"""

    def __init__(self, app_obj):
        self.cfg = app_obj.cfg
        self.app = app_obj

    def run_workflow(self, wf, rec, auto=False):
        global runner2
        runner2 = wq.WorkflowRunner(self.app, wf, rec["content"])
        self.app._ai_runners.append(runner2)
        if auto:
            runner2._on_auto()


hreal = HostReal(fake)
req_e2e = ai.ActionRequest(action_id="summarize",
                           record={"type": "text", "content": "概括我"},
                           auto=True)
res_e2e = ai.executor.run_request(
    hreal, reg.get("summarize"), req_e2e)
deadline = time.time() + 15
while time.time() < deadline and runner2.step_idx < 1:
    app.processEvents()
    time.sleep(0.02)
app.processEvents()
check("e2e.request-auto-ran",
      res_e2e.ok and runner2 is not None and runner2.step_idx == 1
      and runner2.results, str(res_e2e.detail))
runner2.close()
app.processEvents()

# ============================================================
# 7. ChatSession 会话核(PR3B-1)
# ============================================================
import threading as _th
from ai import session as _ses

# -- 生命周期:can_start/spawn/receive/迟到丢弃/停止接收/shutdown --
got = []
s = _ses.ChatSession(lambda rid, ok, text: got.append((rid, ok, text)))
check("session.idle", not s.busy and s.can_start() and not s.shutdown_flag)

blocked = _th.Event()


def _slow_chat(svc, prompt):
    blocked.wait(5)
    return True, "slow:" + prompt


s2 = _ses.ChatSession(lambda rid, ok, text: got.append((rid, ok, text)),
                      chat=_slow_chat)
rid1 = s2.spawn({"name": "X"}, "p1")
check("session.spawn-busy", s2.busy and rid1 in s2.pending
      and not s2.can_start())
rid2 = s2.spawn({"name": "X"}, "p2")     # 第二条在途(上限 2)
check("session.second-inflight", len(s2.pending) == 2 and not s2.can_start())
s2.stop_receiving()                       # 停止接收:active 清空,迟到丢弃
check("session.stop-receiving", not s2.busy and rid2 in s2.pending)
blocked.set()
_deadline = time.time() + 5
while time.time() < _deadline and len(got) < 2:
    time.sleep(0.02)
check("session.late-dropped", len(got) == 2
      and not s2.receive(got[0][0]) and not s2.receive(got[1][0])
      and not s2.pending, str(got))

s3 = _ses.ChatSession(lambda *a: None)
r = s3.spawn({}, "p")
s3.shutdown()
check("session.shutdown-drops", not s3.receive(r) and s3.shutdown_flag
      and not s3.busy and not s3.can_start())

s4 = _ses.ChatSession(lambda *a: None)
r4 = s4.spawn({}, "p")
check("session.receive-presents", s4.receive(r4) and not s4.busy
      and s4.can_start())

# -- chat 异常 → (False, sanitize) 不炸线程 --
def _boom(svc, prompt):
    raise RuntimeError("连接失败 secret-token-12345678")

s5 = _ses.ChatSession(lambda rid, ok, text: got.append((rid, ok, text)),
                      chat=_boom)
s5.spawn({}, "x")
_d = time.time() + 5
while time.time() < _d and not got:
    time.sleep(0.02)
check("session.chat-exception", got and got[-1][1] is False
      and isinstance(got[-1][2], str) and got[-1][2], str(got[-1]))

# -- authorize:无命中不弹窗;mask/raw/cancel 三义 --
_confirm_calls = []
def _confirm(choice):
    def f(hits, masked, endpoint):
        _confirm_calls.append((hits, endpoint))
        return choice
    return f

_card = "卡号 4111111111111111 查一下"
check("session.auth-no-hits",
      _ses.authorize("普通内容", {"base_url": "http://u"}, _confirm("mask"))
      == "普通内容" and not _confirm_calls)
check("session.auth-mask",
      _ses.authorize(_card, {"base_url": "http://u"}, _confirm("mask"))
      != _card and _confirm_calls)
check("session.auth-raw",
      _ses.authorize(_card, {"base_url": "http://u"}, _confirm("raw"))
      == _card)
check("session.auth-cancel",
      _ses.authorize(_card, {"base_url": "http://u"}, _confirm("cancel"))
      is None)

# -- resolve_service:副本隔离/越界 --
_svcs = [{"name": "A", "api_key": "k"}]
_r = _ses.resolve_service(_svcs, 0)
_r["name"] = "改写"
check("session.resolve-copy", _svcs[0]["name"] == "A"
      and _ses.resolve_service(_svcs, 5) is None
      and _ses.resolve_service([], 0) is None)

# -- assemble_context:停用/空包报错/警告透传/快照文本 --
check("session.ctx-disabled", _ses.assemble_context(False, lambda: None) == "")
try:
    _ses.assemble_context(True, lambda: None)
    check("session.ctx-empty-error", False)
except ValueError as e:
    check("session.ctx-empty-error", "没有可用" in str(e))
_warns = []
class _A:
    ok = True
    text = "拼装快照"
    warnings = ["w1", "w2"]
check("session.ctx-text",
      _ses.assemble_context(True, lambda: _A(),
                            lambda w: _warns.extend(w)) == "拼装快照")
check("session.ctx-warn-notify", _warns == ["w1", "w2"])

# ============================================================
# 8. ResultSink 结果出口(PR3B-2A)
# ============================================================
from types import SimpleNamespace as _NS
from ai.sink import ResultSink as _Sink

_core = Mock()
_core.copy_text.return_value = True
_core.paste_text.return_value = True
_sink_app = _NS(core=_core)
k = _Sink(_sink_app, pack_title="单测回答")

# -- copy:成功/失败/空 --
check("sink.copy-ok", k.copy("答案") == (True, ""))
_core.copy_text.return_value = False
check("sink.copy-fail", k.copy("答案") == (False, "clipboard"))
_core.copy_text.return_value = True
check("sink.copy-empty", k.copy("") == (False, "empty"))

# -- paste_back:目标失效三态 + 成功签名 --
check("sink.paste-zero-target",
      k.paste_back("答案", 0) == (False, "target_gone"))
_core.paste_text.reset_mock()
check("sink.paste-gone-no-write",
      k.paste_back("答案", 0)[1] == "target_gone"
      and not _core.paste_text.called)
with patch.object(cm.win32gui, "IsWindow", return_value=False):
    check("sink.paste-window-gone",
          k.paste_back("答案", 77) == (False, "target_gone"))
_fore = []
with patch.object(cm.win32gui, "IsWindow", return_value=True), \
        patch.object(cm.win32gui, "SetForegroundWindow",
                     side_effect=lambda h: _fore.append(h)):
    check("sink.paste-ok", k.paste_back("答案", 456) == (True, "")
          and _fore == [456])
    _core.paste_text.assert_called_once_with("答案", force=True, target_hwnd=456)
_core.paste_text.reset_mock()
with patch.object(cm.win32gui, "IsWindow", return_value=True), \
        patch.object(cm.win32gui, "SetForegroundWindow", lambda h: None):
    check("sink.paste-exclude-self",
          k.paste_back("答案", 456, exclude_hwnd=456) == (False, "target_gone"))
    _core.paste_text.return_value = False
    check("sink.paste-clipboard-fail",
          k.paste_back("答案", 456) == (False, "clipboard"))
_core.paste_text.return_value = True

# -- save_history:捕获管线元组 + 异常脱敏摘录 --
_core.add_item.return_value = None
okc, detc = k.save_history("答案")
check("sink.history-ok", okc and _core.add_item.call_args[0] == (("text", "答案"),))
_core.add_item.side_effect = OSError("disk full 私钥 sk-abcdef123456")
okc, detc = k.save_history("答案")
check("sink.history-fail", not okc and "disk full" in detc)
_core.add_item.side_effect = None

# -- save_pack:标题透传/空守卫 --
_pack_calls = []
with patch("ctxpack.ui_tray.add_text_to_pack",
           side_effect=lambda app, text, title=None, **kw:
           _pack_calls.append((text, title))):
    check("sink.pack-ok", k.save_pack("答案") == (True, ""))
    check("sink.pack-title", _pack_calls == [("答案", "单测回答")])
    check("sink.pack-empty", k.save_pack("") == (False, "empty"))
    check("sink.pack-title-override",
          k.save_pack("答案2", title="回答:自定义") == (True, "")
          and _pack_calls[-1] == ("答案2", "回答:自定义"))

# -- 出口绝不发起模型调用 --
with patch.object(cm, "ai_chat") as _no_chat:
    k.copy("答案")
    with patch.object(cm.win32gui, "IsWindow", return_value=True), \
            patch.object(cm.win32gui, "SetForegroundWindow", lambda h: None):
        k.paste_back("答案", 9)
    k.save_history("答案")
    k.save_pack("答案")
    _no_chat.assert_not_called()

# -- 窗口销毁后的迟到回执:emit 抛 RuntimeError 被会话核吞掉 --
def _dead_emit(rid, ok, text):
    raise RuntimeError("Internal C++ object (面板) already deleted.")

s6 = _ses.ChatSession(_dead_emit, chat=lambda svc, prompt: (True, "迟到"))
raised = False
try:
    s6.spawn({}, "x")     # 不应向调用方抛 RuntimeError
except RuntimeError:
    raised = True
# emit 失败 = 窗口已销毁,该 rid 无人 receive,留待 shutdown 收尾
check("sink.session-emits-after-destroy", not raised)
s6.shutdown()
check("sink.session-shutdown-final", s6.shutdown_flag and not s6.busy)

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
