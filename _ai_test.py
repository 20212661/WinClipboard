# -*- coding: utf-8 -*-
"""AI 工作流端到端测试(无真实外部 API;本地起假 AI 服务器)。

运行: python _ai_test.py   退出码 0 = 全部通过
覆盖:
  1. cm.ai_chat 正常 / HTTP 错误 / 网络错误 / 空响应
  2. {input} 占位符渲染
  3. WorkflowRunner 自动模式 A→B→C 链式运行(结果逐步传递)
  4. 运行器"编辑最新结果"改写链上输入
  5. 服务找不到时的错误路径
  6. ServicesWindow / WorkflowsWindow / WorkflowEditorWindow / WorkflowRunner 离屏构造
  7. 示例模板填入(不重名、不覆盖)
"""
import os
import sys
import io
import json
import time
import queue
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 隔离持久化路径 ----
import clipboard_manager as cm

# 假 AI 服务器在 127.0.0.1:openai SDK(httpx)默认读系统代理,本地回环
# 被代理劫持时会偶发 502(测试进程内绕开全部代理,只影响本测试)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

TMP = tempfile.mkdtemp(prefix="wclb_ai_test_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


# ============================================================
# 假 AI 服务器(OpenAI 兼容 /chat/completions)
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # OpenAI 标准模型发现端点
        if self.path.rstrip("/").endswith("/models"):
            self._reply(200, {"data": [{"id": "model-a"}, {"id": "model-b"},
                                       {"id": "gpt-x"}]})
            return
        self._reply(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            user = body["messages"][0]["content"]
            model = body.get("model", "?")
            auth = self.headers.get("Authorization", "")
            if "badkey" in auth:
                self._reply(401, {"error": {"message": "Invalid API key"}})
                return
            if body.get("empty"):
                self._reply(200, {"id": "x", "object": "chat.completion",
                                  "created": 1, "model": model, "choices": []})
                return
            reply = "[%s] 处理了:%s" % (model, user)
            self._reply(200, {
                "id": "x", "object": "chat.completion", "created": 1, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": reply}}]})
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
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d/v1" % PORT

# ============================================================
# 1. cm.ai_chat 单元测试
# ============================================================
svc = {"name": "A", "base_url": BASE, "model": "model-a", "api_key": "sk-ok"}
ok, text = cm.ai_chat(svc, "你好")
check("ai_chat.ok", ok and "[model-a]" in text, text)

bad = dict(svc, api_key="badkey")
ok, text = cm.ai_chat(bad, "你好")
check("ai_chat.http-401", not ok and "401" in text, text)

ok, text = cm.ai_chat(dict(svc, base_url="http://127.0.0.1:1/v1"), "x")
check("ai_chat.network-refused", not ok, text)

ok, text = cm.ai_chat(dict(svc, base_url=""), "x")
check("ai_chat.no-base-url", not ok, text)

# 引擎:环境装有 openai SDK 时走 SDK 路径;强制禁用后 urllib 兜底同样可用
check("ai_chat.engine-sdk", cm._openai_sdk() is not None)
_orig_sdk = cm._openai_sdk
cm._openai_sdk = lambda: None
ok, text = cm.ai_chat(svc, "兜底引擎")
check("ai_chat.urllib-fallback", ok and "[model-a]" in text and "兜底引擎" in text, text)
cm._openai_sdk = _orig_sdk

# 模型发现:GET /models(OpenAI 标准端点)
ok, models = cm.list_models(svc)
check("list_models.ok", ok and models == ["gpt-x", "model-a", "model-b"], str(models))
ok, msg = cm.list_models(dict(svc, base_url=""))
check("list_models.no-url", not ok, msg)
ok, msg = cm.list_models(dict(svc, base_url="http://127.0.0.1:1/v1"))
check("list_models.refused", not ok, msg)

check("render_prompt", cm.render_prompt("前:{input}:后", "内容") == "前:内容:后")

# ============================================================
# Qt 离屏:GUI 构造 + 运行器链式执行
# ============================================================
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QObject, Qt

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
import winclipboard_qt as wq
import workflow_ui  # 阶段 8 拆分:运行台等在本模块命名空间(打桩须指向这里)
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
    """WorkflowRunner / 管理窗口所需的最小应用接口。"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.core = FakeCore()
        self.icon = QIcon()
        self.ai_hub = None
        self._ai_runners = []

    def show_services(self):
        pass


CFG = {
    "ai_services": [
        {"name": "A", "base_url": BASE, "model": "model-a", "api_key": "k"},
        {"name": "B", "base_url": BASE, "model": "model-b", "api_key": "k"},
        {"name": "C", "base_url": BASE, "model": "model-c", "api_key": "k"},
    ],
    "ai_workflows": [
        {"name": "A→B→C", "steps": [
            {"service": "A", "prompt": "S1({input})"},
            {"service": "B", "prompt": "S2({input})"},
            {"service": "C", "prompt": "S3({input})"},
        ]},
    ],
}
fake = FakeApp(CFG)

# ---- 管理窗口构造 ----
sw = wq.ServicesPage(fake)
check("ServicesWindow.construct", len(sw.cards) == 3,
      str([c.svc.get("name") for c in sw.cards]))

ww = wq.WorkflowPage(fake)
check("WorkflowsWindow.construct", ww.lst.count() == 1)

we = wq.WorkflowEditorWindow(fake, CFG["ai_workflows"][0], None)
check("WorkflowEditor.construct", len(we._rows) == 3)
we.close()

d = wq.ServiceEditorDialog(sw, None)
d.cmb_provider.setCurrentText("自定义")
d.e_name.setText("X")
d.e_url.setText(BASE)
d.cmb_model.setText("m")
d.e_key.setText("k")
check("ServiceEditor.validate", d.validate() and d.result["name"] == "X")

# 预设预选:供应商/模型/接口自动填充,模型信息行有内容
d2 = wq.ServiceEditorDialog(sw, None, preset=cm.AI_PRESET_SERVICES[1])   # DeepSeek
check("ServiceEditor.preset-preselect",
      d2.cmb_provider.currentText() == "DeepSeek"
      and d2.cmb_model.currentText() == "deepseek-chat"
      and d2.e_url.text() == cm.AI_PRESET_SERVICES[1]["base_url"]
      and bool(d2.lbl_model_note.text()), d2.cmb_model.currentText())

# 编辑既有自定义服务:供应商自动判为「自定义」,名称/接口/模型带出
d3 = wq.ServiceEditorDialog(sw, {"name": "我的中转", "base_url": BASE,
                                 "model": "m", "api_key": "k"})
check("ServiceEditor.edit-custom",
      d3.cmb_provider.currentText() == "自定义" and d3.e_name.text() == "我的中转"
      and d3.e_url.text() == BASE and d3.cmb_model.currentText() == "m")

# hermes 风格新字段:Provider ID 默认 openai 并随保存写入;发现模型结果填入下拉
check("ServiceEditor.provider-field",
      d2.cmb_pid.currentText() == "openai" and d.result.get("provider") == "openai")
d4 = wq.ServiceEditorDialog(sw, None)
d4.e_url.setText(BASE)
d4._apply_models(["m1", "m2", "m3"])
check("ServiceEditor.discover-apply",
      d4.cmb_model.count() == 3 and d4.cmb_model.currentText() == "m1",
      "%d items, cur=%s" % (d4.cmb_model.count(), d4.cmb_model.currentText()))

# ---- 运行器:自动模式 A→B→C ----
wf = CFG["ai_workflows"][0]
runner = wq.WorkflowRunner(fake, wf, "种子内容")
fake._ai_runners.append(runner)
check("Runner.construct", runner.step_idx == 0 and runner.btn_copy.isEnabled() is False)

runner._on_auto()
deadline = time.time() + 15
while time.time() < deadline and runner.step_idx < 3:
    app.processEvents()
    time.sleep(0.02)
app.processEvents()

check("Runner.chain-3-steps", runner.step_idx == 3, "step_idx=%d" % runner.step_idx)
r0, r1, r2 = runner.results
check("Runner.step1-input", "S1(种子内容)" in r0 and "[model-a]" in r0, r0)
check("Runner.step2-chained", "S2(" in r1 and r0 in r1 and "[model-b]" in r1, r1)
check("Runner.step3-chained", "S3(" in r2 and r1 in r2 and "[model-c]" in r2, r2)

# ---- 人机回路:编辑最新结果改写链上输入 ----
runner.results[-1] = "人工改写后的结果"
runner.cur = runner.results[-1]           # 模拟 do_save 的效果
check("Runner.edit-cur", runner.cur == "人工改写后的结果")

# ---- 复制只写剪贴板(core.copy_text,不派发 Ctrl+V) ----
runner._copy_latest()
check("Runner.copy", fake.core.copied and fake.core.copied[-1] == "人工改写后的结果")
check("Runner.copy-不回贴", not fake.core.pasted)

# ---- 错误路径:服务不存在 ----
wf_bad = {"name": "坏流程", "steps": [{"service": "不存在", "prompt": "{input}"}]}
rb = wq.WorkflowRunner(fake, wf_bad, "x")
rb._on_next()
app.processEvents()
check("Runner.missing-service", rb.step_idx == 0 and rb._worker is None)

# ---- 网页 + 人工步骤(网页端 AI 模式) ----
import webbrowser
URLS_OPENED = []
webbrowser.open = lambda u, *a, **k: (URLS_OPENED.append(u), True)[1]   # 不真开浏览器

CFG["ai_workflows"].append({"name": "网页链", "steps": [
    {"kind": "web", "prompt": "评审:{input}", "url": "https://example.com/?q={prompt}"},
    {"kind": "human", "prompt": "粘贴回答"},
]})
rw = wq.WorkflowRunner(fake, CFG["ai_workflows"][-1], "原始输入")
rw._run_step()                      # web 步骤
app.processEvents()
check("Web.wait-panel", rw._waiting is not None)
check("Web.prompt-copied", any("评审:原始输入" in t for t in fake.core.copied))
check("Web.prompt-只复制不回贴", not fake.core.pasted)
check("Web.url-opened", URLS_OPENED and "review" not in URLS_OPENED[0]
      and "%E8%AF%84%E5%AE%A1" in URLS_OPENED[0], str(URLS_OPENED))
app.clipboard().setText("网页AI的回答")
rw._wait_body.setPlainText(app.clipboard().text())
rw._wait_confirm()
app.processEvents()
check("Web.confirmed", rw.step_idx == 1 and rw.results[0] == "网页AI的回答")
rw._run_step()                      # human 步骤
check("Human.wait-prefill", rw._waiting is not None
      and rw._wait_body.toPlainText() == "网页AI的回答")
rw._wait_body.setPlainText("人工编辑后")
rw._wait_confirm()
app.processEvents()
check("Human.confirmed", rw.step_idx == 2 and rw.results[1] == "人工编辑后")
rw.close()

# ---- 示例模板填入 ----
cm.save_config(CFG)
hub = wq.WorkflowPage(fake)
hub._seed()
names = [w["name"] for w in CFG["ai_workflows"]]
svcs = [s["name"] for s in CFG["ai_services"]]
check("Seed.workflows", any("网页" in n for n in names) and any("串联" in n for n in names), str(names))
check("Seed.web-steps", any(st.get("kind") == "web" for w in CFG["ai_workflows"] for st in w["steps"]))
check("Seed.no-dup-services", svcs.count("A") == 1 and "GLM" in svcs, str(svcs))
hub._seed()   # 幂等
check("Seed.idempotent", len([w for w in CFG["ai_workflows"] if w["name"] == names[0]]) == 1)

# ---- Privacy Gate:AI 发送前敏感拦截(遮盖/原样/取消) ----
_host = wq.QWidget()          # 离屏宿主(MaskDialogBase 需要真 QWidget 定位遮罩)
gd = wq.PrivacyGateDialog(_host, ["API 密钥", "JWT Token"], "遮盖预览文本",
                          "https://api.example.com")
check("GateDialog.default-cancel", gd.choice == "cancel")
gd._pick("mask")
check("GateDialog.pick-mask", gd.choice == "mask")
gd._pick("raw")
check("GateDialog.pick-raw", gd.choice == "raw")
gd.close()

wf_gate = {"name": "隐私链", "steps": [{"service": "A", "prompt": "解释 {input}"}]}
gr = wq.WorkflowRunner(fake, wf_gate, "普通内容")
check("Gate.clean-passthrough",
      gr._privacy_gate("普通内容", CFG["ai_services"][0]) == "普通内容")

_secret = "密钥 sk-" + "A" * 30 + " 是什么"       # 无效占位密钥样本
_gate_seen = []


def _make_stub_gate():
    """离屏桩(动态构造,不弹窗):choice 由测试侧指定;任意 Qt 方法调用
    (exec/setModal 等)一律安静返回。"""
    def _init(self, parent, hits, masked, endpoint):
        _gate_seen.append((tuple(hits), masked, endpoint))
    return type("StubGate", (), {
        "choice": "mask",
        "__init__": _init,
        "__getattr__": lambda self, name: (lambda *a: 0),
    })


_StubGate = _make_stub_gate()


_orig_dlg = workflow_ui.PrivacyGateDialog   # 拆分后运行台在 workflow_ui 命名空间解析
workflow_ui.PrivacyGateDialog = _StubGate
try:
    out = gr._privacy_gate(_secret, CFG["ai_services"][0])
    check("Gate.mask-applied", "sk-" not in out and "▇▇▇[API 密钥]" in out)
    check("Gate.dialog-args", _gate_seen and _gate_seen[0][0] == ("API 密钥",)
          and _gate_seen[0][2] == BASE, str(_gate_seen[:1]))
    _StubGate.choice = "raw"
    check("Gate.raw-passthrough",
          gr._privacy_gate(_secret, CFG["ai_services"][0]) == _secret)
    _StubGate.choice = "cancel"
    check("Gate.cancel-blocks",
          gr._privacy_gate(_secret, CFG["ai_services"][0]) is None)

    # 端到端:链上敏感输入经门遮盖后发往假服务器(回显验证)
    _StubGate.choice = "mask"
    gr2 = wq.WorkflowRunner(fake, wf_gate, _secret)
    fake._ai_runners.append(gr2)
    gr2._on_next()
    _dl = time.time() + 15
    while time.time() < _dl and gr2.step_idx < 1:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    check("Gate.runner-e2e-masked",
          gr2.step_idx == 1 and gr2.results
          and "▇▇▇[API 密钥]" in gr2.results[0]
          and "sk-" not in gr2.results[0], str(gr2.results[:1]))
    gr2.close()
finally:
    workflow_ui.PrivacyGateDialog = _orig_dlg

# ---- Step Contract:步骤统一契约(workflow_steps) ----
import workflow_steps as wfs

check("Step.契约方法齐备",
      all(callable(getattr(wfs.WorkflowStep, m, None))
          for m in ("validate", "execute", "cancel", "to_dict", "from_dict")))
# 契约名 execute 与实现体 run 是同一函数(别名挂载,见 workflow_steps 模块头)
check("Step.execute 别名一致", all(
    c.execute is (c.__dict__.get("run") or wfs.WorkflowStep.run)
    for c in wfs.STEP_REGISTRY.values()))
check("Step.旧配置无 kind 视为 ai",
      isinstance(wfs.deserialize({"service": "A", "prompt": "p"}), wfs.AiStep))
check("Step.manual 别名归一",
      isinstance(wfs.deserialize({"kind": "manual", "prompt": ""}), wfs.HumanStep))
check("Step.未知类型拦在 validate",
      wfs.deserialize({"kind": "???"}).validate(None) is not None)
for _d in ({"kind": "ai", "service": "A", "prompt": "p"},
           {"kind": "web", "prompt": "p", "url": "u"},
           {"kind": "human", "prompt": "p"},
           {"kind": "clipboard"},
           {"kind": "transform", "op": "regex", "arg1": r"\d+", "arg2": "N"},
           {"kind": "if", "pattern": "x"}):
    check("Step.序列化往返·%s" % _d["kind"],
          wfs.deserialize(_d).to_dict() == _d, str(wfs.deserialize(_d).to_dict()))

_tctx = wfs.WorkflowContext(input_text="原始")
_tctx.previous = " 链上 42 号 "
_r = wfs.deserialize({"kind": "transform", "op": "trim"}).run(_tctx)
check("Transform.trim", isinstance(_r, wfs.Done) and _r.text == "链上 42 号", _r.text)
_r = wfs.deserialize({"kind": "transform", "op": "regex", "arg1": r"\d+",
                      "arg2": "N"}).run(_tctx)
check("Transform.regex", _r.text == " 链上 N 号 ", _r.text)
check("Transform.坏正则被拦", bool(
    wfs.deserialize({"kind": "transform", "op": "regex",
                     "arg1": "("}).validate(None)))
check("Transform.未知 op 被拦", bool(
    wfs.deserialize({"kind": "transform", "op": "eval"}).validate(None)))

# 运行台:同步步骤链(clipboard → transform → if 命中)自动推进
_copied0 = len(fake.core.copied)
wf_sync = {"name": "同步链", "steps": [
    {"kind": "clipboard"},
    {"kind": "transform", "op": "upper"},
    {"kind": "if", "pattern": "^AB"},
]}
rs = wq.WorkflowRunner(fake, wf_sync, "ab配额12")
fake._ai_runners.append(rs)
rs._on_auto()
app.processEvents()
check("Sync.auto-chain", rs.step_idx == 3
      and rs.results == ["ab配额12", "AB配额12", "AB配额12"], str(rs.results))
check("Sync.clipboard快照", fake.core.copied[_copied0:] == ["ab配额12"],
      str(fake.core.copied[_copied0:]))
rs.close()

# if 未命中:自动停链,可手动继续
wf_if = {"name": "条件停", "steps": [
    {"kind": "if", "pattern": "zzz"},
    {"kind": "clipboard"},
]}
rf = wq.WorkflowRunner(fake, wf_if, "正常内容")
fake._ai_runners.append(rf)
rf._on_auto()
app.processEvents()
check("If.未命中停链", rf.step_idx == 1 and rf._auto is False)
rf._on_next()                     # 手动继续后续步骤
app.processEvents()
check("If.手动继续", rf.step_idx == 2)
rf.close()

# {previous}/{origin} 占位符(AI 步骤走真实 FakeAI 服务)
wf_t = {"name": "占位符", "steps": [
    {"service": "A", "prompt": "P1[{input}]"},
    {"service": "A", "prompt": "P2[{previous}]+[{origin}]"},
]}
rt = wq.WorkflowRunner(fake, wf_t, "种子")
fake._ai_runners.append(rt)
rt._on_auto()
_dl = time.time() + 15
while time.time() < _dl and rt.step_idx < 2:
    app.processEvents()
    time.sleep(0.02)
app.processEvents()
check("Step.占位符链式+原始", rt.step_idx == 2 and "P2[" in rt.results[1]
      and rt.results[0] in rt.results[1] and "种子" in rt.results[1],
      str(rt.results))
rt.close()

# 编辑器:新种类行 + 保存归一
CFG["ai_workflows"].append({"name": "契约编辑", "steps": [
    {"kind": "transform", "op": "replace", "arg1": "a", "arg2": "b"},
    {"kind": "if", "pattern": "x"},
    {"kind": "clipboard"},
]})
we2 = wq.WorkflowEditorWindow(fake, CFG["ai_workflows"][-1], None)
check("Editor.契约行数", len(we2._rows) == 3)
check("Editor.transform 参数带出",
      we2._rows[0]["op"].currentText() == wfs.TransformStep.OP_LABELS["replace"]
      and we2._rows[0]["arg1"].text() == "a"
      and we2._rows[0]["arg2"].text() == "b")
check("Editor.if 模式带出", we2._rows[1]["arg1"].text() == "x")
we2._save()
_saved = CFG["ai_workflows"][-1]["steps"]
check("Editor.契约保存归一",
      {"kind": "transform", "op": "replace", "arg1": "a", "arg2": "b"} in _saved
      and {"kind": "if", "pattern": "x"} in _saved
      and {"kind": "clipboard"} in _saved, str(_saved))

# ---- WebStep 隐私门(PR2.5:复制提示词不再绕过敏感检查) ----
import workflow_steps as wfs


def _web_ctx(gate, copy_result=True):
    box = []
    ctx = wfs.WorkflowContext(
        input_text="材料",
        privacy_gate=gate,
        copy_text=(lambda t: box.append(t) or True) if copy_result else None)
    return ctx, box


_web_prompt = "请翻译:材料内容"
_gate_svcs = []


def _gate_pass(p, svc):
    _gate_svcs.append(svc)
    return p


ctx, box = _web_ctx(_gate_pass)
res = wfs.WebStep(prompt=_web_prompt).run(ctx)
check("web.gate-pass", isinstance(res, wfs.Wait) and box == [_web_prompt])
check("web.gate-svc-label",
      _gate_svcs and _gate_svcs[0].get("base_url") == "网页 AI")

ctx, box = _web_ctx(lambda p, svc: "MASKED")
res = wfs.WebStep(prompt=_web_prompt).run(ctx)
check("web.gate-mask", isinstance(res, wfs.Wait) and box == ["MASKED"])

ctx, box = _web_ctx(lambda p, svc: None)      # 用户取消
res = wfs.WebStep(prompt=_web_prompt).run(ctx)
check("web.gate-cancel", isinstance(res, wfs.Cancelled)
      and box == [] and "隐私门拦截" in res.reason)

ctx, box = _web_ctx(None)      # 宿主未提供门(旧宿主兼容):原样复制
res = wfs.WebStep(prompt=_web_prompt).run(ctx)
check("web.no-gate-host", isinstance(res, wfs.Wait) and box == [_web_prompt])

ctx, box = _web_ctx(_gate_pass, copy_result=None)   # 复制失败
res = wfs.WebStep(prompt=_web_prompt).run(ctx)
check("web.copy-fail-halt", isinstance(res, wfs.Done) and res.halt
      and "复制失败" in (res.note and res.note[0] or ""))

_hits, _masked = cm.privacy_scan("卡号 4111111111111111 查一下")
check("web.scan-detects-sensitive", bool(_hits) and "▇" in _masked,
      str(_hits))
_copied = []


def _gate_cancel_card(p, svc):
    hits, masked = cm.privacy_scan(p)
    return None if hits else p


ctx = wfs.WorkflowContext(
    input_text="x", privacy_gate=_gate_cancel_card,
    copy_text=lambda t: _copied.append(t) or True)
res = wfs.WebStep(prompt="卡号 4111111111111111 查一下").run(ctx)
check("web.sensitive-cancel-no-copy", isinstance(res, wfs.Cancelled)
      and _copied == [])

# ---- AIStepBridge 适配器(PR3B-3 提交 A:runner ←组合→ ChatSession) ----
import threading as _th_bridge
from PySide6.QtCore import QObject as _BridgeObj
from workflow_ui import AIStepBridge, _AIRequestHandle

_fr = _BridgeObj()
_fr._worker = None
_fr._dead = False
_fr._auto = False
_fr.steps = []                 # 空步骤表:dispatch 的链推分支永不触发
_fr.closes = 0
_fr.close = lambda: setattr(_fr, "closes", _fr.closes + 1)
_fr._update_ui = lambda: None
_fr._run_step = lambda: None


def _bridge_pump(cond, timeout=3.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def _mk_bridge(chat):
    return AIStepBridge(_fr, chat=chat)


# 1) 正常投递:on_done 一次、槽位清空、pending 归零、不触发 dead 收尾
_gate_b = _th_bridge.Event()
_seen = []


def _chat_block(svc, prompt):
    _seen.append(prompt)
    _gate_b.wait(3)
    return True, "答:" + prompt


br = _mk_bridge(_chat_block)
rec = []
h = br.start(0, {"name": "S"}, "P1", lambda ok, t: rec.append((ok, t)))
_fr._worker = h
check("bridge.slot-busy", isinstance(h, _AIRequestHandle) and h.isRunning()
      and _fr._worker is h and not br._session.can_start())
_gate_b.set()
check("bridge.deliver", _bridge_pump(lambda: not br._session.pending)
      and rec == [(True, "答:P1")])
check("bridge.slot-cleared", _fr._worker is None and not h.isRunning()
      and _fr.closes == 0)
check("bridge.prompt-frozen", _seen == ["P1"])

# 2) 契约取消:登记摘除,迟到结果被丢弃;槽位由迟到分发释放(时序同旧 QThread)
_gate_c = _th_bridge.Event()
br2 = _mk_bridge(lambda svc, prompt: (_gate_c.wait(3), (True, "迟到"))[1])
rec2 = []
h2 = br2.start(1, {"name": "S"}, "P2", lambda ok, t: rec2.append((ok, t)))
_fr._worker = h2
h2.cancel()
check("bridge.cancel-keeps-inflight-busy", h2.isRunning()
      and not br2._session.can_start())   # HTTP 在途期间不放开重发
_gate_c.set()
check("bridge.late-dropped", _bridge_pump(lambda: not h2.isRunning())
      and rec2 == [] and _fr._worker is None)

# 3) 重复迟到分发:同 rid 再到一次,零副作用
br.arrived.emit(1, True, "重复")           # rid=1 已在测试 1 分发过
app.processEvents()
check("bridge.duplicate-late-noop", rec == [(True, "答:P1")]
      and not br._session.pending and _fr._worker is None)

# 4) _dead 收尾:结果到达时若运行台在等待关闭,投递后 close()
_gate_d = _th_bridge.Event()
br3 = _mk_bridge(lambda svc, prompt: (_gate_d.wait(3), (True, "终"))[1])
rec3 = []
h3 = br3.start(2, {"name": "S"}, "P3", lambda ok, t: rec3.append((ok, t)))
_fr._worker = h3
_fr._dead = True                          # closeEvent 等待中
_gate_d.set()
check("bridge.dead-closes", _bridge_pump(lambda: _fr.closes >= 1)
      and rec3 == [(True, "终")])
_fr._dead = False

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
