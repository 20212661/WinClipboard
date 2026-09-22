# -*- coding: utf-8 -*-
"""Agent Runtime(路线图三阶段)测试:纯层 + 脚本化规划器 + 假 AI 端到端。

运行: python _agent_test.py   退出码 0 = 全部通过
覆盖:
  1. 工具注册表:七工具/别名/描述清单;LLM 工具经 chat 注入;save_note
     默认落盘;copy 工具
  2. TaskGraph:归一化(补号/截断/坏条目丢弃)/别名校验/空计划
  3. 规划器:extract_json(整串/围栏/前后缀噪音)/LLMPlanner 经假 chat
     出计划/坏输出 None
  4. 运行循环:两轮收敛(计划→确认)/失败重规划恢复/规划失败中止/
     取消/轮次上限/任务上限
  5. 记忆:观察快照/结果链(失败不断链)/落盘保留 30 条
  6. 端到端(离屏 + 假 AI):FakeAI 带规划模式 → AgentWorker 真线程跑通
     summary→keywords→save_note → 窗口纪要/笔记文件/运行记录
"""
import os
import sys
import json
import time
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 假 AI 在 127.0.0.1:测试进程绕开系统代理(openai SDK/httpx 默认读代理)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_agent_test_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

import agent
from agent import run_once, RunMemory

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def scripted(plans):
    """(obs) -> 计划;按调用次序吐出 plans 里的内容,耗尽后收尾。"""
    it = iter(plans)

    def _plan(obs):
        try:
            return next(it)
        except StopIteration:
            return {"finished": True, "tasks": []}
    return _plan


def echo_chat(tag="[AI]"):
    def _chat(prompt):
        return True, tag + "处理了:" + prompt[:40].replace("\n", " ")
    return _chat


# ============================================================
# 1. 工具注册表
# ============================================================
tools = agent.build_default_tools()
check("tools.count", len(tools) == 7, str(len(tools)))
check("tools.names", {t.name for t in tools.all()} == {
    "summary", "translate", "code_explain", "markdown", "keywords",
    "save_note", "copy"})
check("tools.alias", tools.get("markdown_export").name == "markdown"
      and tools.get("keyword").name == "keywords"
      and tools.get("note").name == "save_note")
check("tools.unknown", tools.get("nope") is None and tools.get("") is None)
block = tools.descriptions_for_prompt()
check("tools.prompt-block", "summary" in block and "save_note" in block
      and all(line.startswith("- ") for line in block.splitlines()))
ok, out = tools.get("summary").run("正文", {"chat": echo_chat()})
check("tools.llm-via-chat", ok and "[AI]处理了:" in out and "正文" in out, out)
ok, out = tools.get("summary").run("正文", {})
check("tools.llm-no-chat", not ok and "LLM" in out, out)

NOTES = os.path.join(TMP, "agent_notes.md")
ok, msg = agent.tools.default_save_note(NOTES, "整理论文", "结果 A")
ok2, msg2 = agent.tools.default_save_note(NOTES, "另一目标", "结果 B")
text = open(NOTES, encoding="utf-8").read()
check("tools.save-note", ok and ok2 and "结果 A" in text and "结果 B" in text
      and "整理论文" in text and "另一目标" in text, text[:120])

copied = []
ok, out = tools.get("copy").run("待复制", {"copy_text": copied.append})
check("tools.copy", ok and copied == ["待复制"] and "3 字" in out, out)
ok, out = tools.get("copy").run("x", {})
check("tools.copy-no-host", not ok, out)

# ============================================================
# 2. TaskGraph
# ============================================================
g = agent.TaskGraph.from_plan({
    "goal": "论文整理",
    "tasks": [{"id": 1, "tool": "summary"}, {"tool": "keyword"},
              {"id": 3, "tool": "markdown_export"},
              "坏条目", {"note": "没有工具名"}, {"id": 6, "tool": "save_note"}]})
check("graph.normalize", [t.tool for t in g.tasks] ==
      ["summary", "keyword", "markdown_export", "save_note"]
      and [t.id for t in g.tasks] == [1, 2, 3, 6],   # 缺号补序号,显式号保留
      str(g.to_dict()))
check("graph.goal", g.goal == "论文整理")
check("graph.validate-alias", g.validate(tools) == [])
bad = agent.TaskGraph.from_plan({"tasks": [{"tool": "summary"},
                                           {"tool": "不存在"}]})
errs = bad.validate(tools)
check("graph.validate-unknown", len(errs) == 1 and "不存在" in errs[0], str(errs))
check("graph.empty-plan", agent.TaskGraph.from_plan("不是dict").tasks == [])
check("graph.cap", len(agent.TaskGraph.from_plan({
    "tasks": [{"tool": "summary"}] * 20}).tasks) == agent.task_graph.MAX_TASKS)
check("graph.finished-flag",
      agent.TaskGraph.from_plan({"finished": True, "tasks": []}).finished)

# ============================================================
# 3. 规划器
# ============================================================
EJ = agent.extract_json
check("parse.whole", EJ('{"a": 1}') == {"a": 1})
check("parse.fenced", EJ('好的:\n```json\n{"a": [1,2]}\n```') == {"a": [1, 2]})
check("parse.noisy", EJ('前言 {"goal": "x", "tasks": []} 后记')
      == {"goal": "x", "tasks": []})
check("parse.broken", EJ("完全不是 JSON") is None and EJ("") is None)

llm_planner = agent.LLMPlanner(echo_chat(), tools)
g2 = llm_planner({"goal": "G", "input_head": "素材", "rounds": 0, "done": []})
check("planner.echo-not-json", g2 is None)     # 回显文本抠不出 JSON

def plan_chat(prompt):
    assert "任务规划器" in prompt and "- summary:" in prompt
    return True, '{"goal": "整理论文", "tasks": [{"id": 1, "tool": "summary"},' \
                 ' {"id": 2, "tool": "keyword"}, {"id": 3, "tool": "save_note"}]}'
g3 = agent.LLMPlanner(plan_chat, tools)({"goal": "整理论文", "input_head": "素材"})
check("planner.llm-plan", g3 is not None and len(g3.tasks) == 3
      and g3.tasks[2].tool == "save_note")
g4 = agent.LLMPlanner(lambda p: (False, "网络错误"), tools)({})
check("planner.chat-fail", g4 is None)

# ============================================================
# 4. 运行循环
# ============================================================
CTX = {"chat": echo_chat(),
       "save_note": lambda g, t: agent.tools.default_save_note(NOTES, g, t),
       "copy_text": lambda t: None}

# 4a. 两轮收敛:计划(3 任务)→ 确认收尾
mem = run_once("论文素材。", "整理论文", tools, scripted([
    {"goal": "整理论文", "tasks": [{"id": 1, "tool": "summary"},
                                  {"id": 2, "tool": "keywords"},
                                  {"id": 3, "tool": "save_note"}]},
    {"finished": True, "tasks": []},
]), ctx=CTX)
check("loop.done", mem.finished and not mem.error and mem.rounds == 2
      and len(mem.entries) == 3, mem.summary_text())
check("loop.chain", mem.current.startswith("已保存到")      # 链尾=save_note 产出
      and "总结下面内容" in mem.entries[0]["out"]
      and "关键词" in mem.entries[1]["out"])
check("loop.observe", mem.observe()["done"]
      == [{"tool": "summary", "ok": True},
          {"tool": "keywords", "ok": True},
          {"tool": "save_note", "ok": True}])

# 4b. 失败 → 重规划恢复:首轮坏工具失败,第二轮修正后收尾
mem_b = run_once("素材", "目标B", tools, scripted([
    {"tasks": [{"id": 1, "tool": "不存在"}]},
    {"tasks": [{"id": 1, "tool": "summary"}]},
    {"finished": True, "tasks": []},
]), ctx=CTX)
check("loop.replan-recover", mem_b.finished and mem_b.rounds == 3
      and [e["ok"] for e in mem_b.entries] == [False, True],
      mem_b.summary_text())

# 4c. 规划失败中止(finished=终局标记,成败看 error)
mem_c = run_once("素材", "目标C", tools, lambda obs: None, ctx=CTX)
check("loop.planner-fail", mem_c.finished and "规划器" in mem_c.error)

# 4d. 取消:任务间协作取消
cancel_at = {"n": 0}


def cancel_soon():
    cancel_at["n"] += 1
    return cancel_at["n"] > 1


mem_d = agent.AgentRuntime(
    tools, scripted([{"tasks": [{"tool": "summary"}, {"tool": "summary"},
                                {"tool": "summary"}]}]),
    ctx=CTX, should_cancel=cancel_soon).run("素材", "目标D")
check("loop.cancel", mem_d.cancelled and len(mem_d.entries) <= 2,
      mem_d.summary_text())

# 4e. 轮次上限
mem_e = agent.AgentRuntime(
    tools, lambda obs: {"tasks": [{"tool": "summary"}]},
    ctx=CTX, max_rounds=3).run("素材", "目标E")
check("loop.max-rounds", mem_e.finished and "轮次上限" in mem_e.error
      and mem_e.rounds == 3, mem_e.summary_text())

# 4f. 任务上限:单轮计划被截到 MAX_TASKS
mem_f = run_once("素材", "目标F", tools, scripted([
    {"tasks": [{"tool": "summary"}] * 20},
    {"finished": True, "tasks": []},
]), ctx=CTX)
check("loop.task-cap", len(mem_f.entries) == agent.task_graph.MAX_TASKS)

# ============================================================
# 5. 记忆
# ============================================================
check("memory.last-output",
      mem.last_output.startswith("[AI]处理了:"))   # 跳过 save_note 回执
mem_g = RunMemory()
mem_g.begin("G", "输入")
mem_g.push_result(1, agent.Task(1, "bad"), False, "失败产出", 5)
check("memory.fail-keeps-chain", mem_g.current == "输入")
mem_g.push_result(1, agent.Task(2, "summary"), True, "成功产出", 5)
check("memory.success-advances", mem_g.current == "成功产出")
s = mem_g.summary_text()
check("memory.summary", "1 / 失败 1" in s and "✗" in s and "✓" in s, s)

runs_path = os.path.join(TMP, "agent_runs.json")
for i in range(33):
    m = RunMemory()
    m.begin("目标%d" % i, "x" * 10)
    m.push_result(1, agent.Task(1, "summary"), True, "y" * 20, 3.2)
    m.finish()
    RunMemory.save_run(m.record(), TMP)
saved = json.loads(open(runs_path, encoding="utf-8").read())
check("memory.persist-cap", len(saved) == agent.memory.KEEP_RUNS
      and saved[-1]["goal"] == "目标32" and "out" not in json.dumps(saved[-1]))
rec = saved[-1]
check("memory.record-meta", rec["tasks"][0]["chars"] == 20
      and rec["input_chars"] == 10 and rec["finished"])

# ============================================================
# 6. 端到端(离屏 + 假 AI 规划模式 + 真线程)
# ============================================================
from http.server import BaseHTTPRequestHandler, HTTPServer

PLAN_JSON = json.dumps({
    "goal": "整理论文", "finished": False,
    "tasks": [{"id": 1, "tool": "summary"}, {"id": 2, "tool": "keywords"},
              {"id": 3, "tool": "save_note"}]}, ensure_ascii=False)
PLAN_DONE = json.dumps({"goal": "整理论文", "finished": True, "tasks": []},
                       ensure_ascii=False)


class FakeAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        user = body["messages"][0]["content"]
        model = body.get("model", "?")
        if "任务规划器" in user:          # 规划模式:无执行记录给计划,
            content = PLAN_DONE if "已执行记录" in user else PLAN_JSON
        else:                             # 有则确认收尾;工具模式回显
            content = "[%s] 处理了:%s" % (model, user[:60])
        d = json.dumps({"id": "x", "object": "chat.completion", "created": 1,
                        "model": model,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant",
                                                 "content": content}}]}
                       ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)


srv = HTTPServer(("127.0.0.1", 0), FakeAI)
BASE = "http://127.0.0.1:%d/v1" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

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
        return True

    def copy_text(self, t):
        self.copied.append(t)


class FakeApp(QObject):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.core = FakeCore()
        self.icon = QIcon()
        self._agents = []


fake = FakeApp({"ai_services": [
    {"name": "A", "base_url": BASE, "model": "model-a", "api_key": "k"}],
    "ai_workflows": []})

# 规划器协议:首轮回 3 任务计划;带「已执行记录」的后续轮确认收尾
win = wq.AgentRunner(fake, rec={"type": "text", "content": "论文正文素材"},
                     goal="整理论文")
check("e2e.window-construct", win.e_input.toPlainText() == "论文正文素材"
      and win.e_goal.text() == "整理论文")
win._start()
deadline = time.time() + 30
while time.time() < deadline and win._worker is not None:
    app.processEvents()
    time.sleep(0.02)
app.processEvents()
txt = win.transcript.toPlainText()
check("e2e.finished", win._worker is None and win.lbl_status.text() == "完成",
      win.lbl_status.text() + " | " + txt[-200:])
check("e2e.plan-shown", "第 1 轮计划" in txt and "1.summary" in txt
      and "3.save_note" in txt, txt[:300])
check("e2e.tasks-done", txt.count("✓ 任务") >= 3
      and "[model-a] 处理了:" in txt, txt)
check("e2e.result-ready", win._last_out.startswith("[model-a]"))
notes_txt = open(os.path.join(TMP, "agent_notes.md"), encoding="utf-8").read()
check("e2e.note-saved", "[model-a] 处理了:" in notes_txt
      and "整理论文" in notes_txt)
runs = json.loads(open(os.path.join(TMP, "agent_runs.json"),
                       encoding="utf-8").read())
check("e2e.run-record", runs and runs[-1]["goal"] == "整理论文"
      and len(runs[-1]["tasks"]) >= 3, str(runs[-1] if runs else None))
win.close()
app.processEvents()

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
