# -*- coding: utf-8 -*-
"""AI Action Bar(卡片 ✨ AI)数据层与接线测试。

运行: python _ai_actions_test.py   退出码 0 = 全部通过
覆盖:
  1. 动作注册表:内置装载/排序/字段校验/for_record 过滤/用户目录扫描(覆盖与坏文件)
  2. 执行器:工作流解析(用户配置优先/内置回落/深拷贝隔离/守卫)
  3. 默认服务:cm.ai_service_by_name 空名 = 第一个已配置服务
  4. Step Contract:AiStep 空服务经 find_service 回退到默认服务
  5. 端到端:假 AI 服务器 + WorkflowRunner 跑通内置「解释代码」(空服务名)
  6. 工作流编辑器:空服务步骤显示「(默认服务)」且可保存为空服务名
"""
import os
import sys
import json
import time
import tempfile
import threading

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
# 2. 执行器:解析优先级 / 深拷贝 / 守卫
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

    def run_workflow(self, wf, rec):
        self.runs.append((wf, rec))


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

# ============================================================
# 3. 默认服务解析(cm.ai_service_by_name 空名回退)
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
# 4. Step Contract:AiStep 空服务 = 默认服务
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
# 5. 端到端:内置「解释代码」经运行台跑通(假 AI,空服务名)
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
    """WorkflowRunner / 编辑器所需的最小应用接口(同 _ai_test)。"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.core = FakeCore()
        self.icon = QIcon()
        self.ai_hub = None
        self._ai_runners = []


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

# ============================================================
# 6. 工作流编辑器:空服务 = (默认服务),可保存
# ============================================================
we = wq.WorkflowEditorWindow(
    fake, {"name": "t", "steps": [{"kind": "ai", "service": "", "prompt": "x"}]},
    None)
row = we._rows[0]
check("editor.default-selected", row["combo"].currentText() == "(默认服务)",
      row["combo"].currentText())
check("editor.combo-count", row["combo"].count() == 2,   # (默认服务) + A
      str(row["combo"].count()))
we.close()

fake.cfg = {"ai_services": CFG["ai_services"], "ai_workflows": []}
we2 = wq.WorkflowEditorWindow(fake, None, None)
we2.e_name.setText("草稿")
we2._save()
_saved = fake.cfg.get("ai_workflows") or []
check("editor.save-empty-service",
      _saved and _saved[0]["steps"][0]["service"] == "", str(_saved))
we2.close()

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
