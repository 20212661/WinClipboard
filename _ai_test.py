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
                self._reply(200, {"choices": []})
                return
            reply = "[%s] 处理了:%s" % (model, user)
            self._reply(200, {"choices": [{"message": {"role": "assistant",
                                                       "content": reply}}]})
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
wq.init_palette(False)
from qfluentwidgets import setTheme, Theme
setTheme(Theme.LIGHT)
wq.setThemeColor(wq.ACCENT)


class FakeCore:
    pasted = []

    def paste_text(self, t):
        self.pasted.append(t)
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
sw = wq.ServicesWindow(fake)
check("ServicesWindow.construct", sw.lst.count() == 3)

ww = wq.WorkflowsWindow(fake)
check("WorkflowsWindow.construct", ww.lst.count() == 1)

we = wq.WorkflowEditorWindow(fake, CFG["ai_workflows"][0], None)
check("WorkflowEditor.construct", len(we._rows) == 3)
we.close()

d = wq.ServiceEditorDialog(sw, None)
d.e_name.setText("X")
d.e_url.setText(BASE)
d.e_model.setText("m")
d.e_key.setText("k")
check("ServiceEditor.validate", d.validate() and d.result["name"] == "X")

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

# ---- 复制走 core.paste_text ----
runner._copy_latest()
check("Runner.copy", fake.core.pasted and fake.core.pasted[-1] == "人工改写后的结果")

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
check("Web.prompt-copied", any("评审:原始输入" in t for t in fake.core.pasted))
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
hub = wq.WorkflowsWindow(fake)
hub._seed()
names = [w["name"] for w in CFG["ai_workflows"]]
svcs = [s["name"] for s in CFG["ai_services"]]
check("Seed.workflows", any("网页" in n for n in names) and any("串联" in n for n in names), str(names))
check("Seed.web-steps", any(st.get("kind") == "web" for w in CFG["ai_workflows"] for st in w["steps"]))
check("Seed.no-dup-services", svcs.count("A") == 1 and "GLM" in svcs, str(svcs))
hub._seed()   # 幂等
check("Seed.idempotent", len([w for w in CFG["ai_workflows"] if w["name"] == names[0]]) == 1)

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
