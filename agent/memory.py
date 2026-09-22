# -*- coding: utf-8 -*-
"""Agent 运行记忆(Memory):一轮 Agent 任务的上下文与记录。

- observe 快照:goal / 素材预览 / 已执行任务(成功失败)/ 轮次 ——
  规划器每轮看到的就是它(3.4 的 observe + update_context 落点)
- 结果链:上一步产出 = 下一步输入(失败不阻断链,内容原样下传)
- 落盘:save_run() 追加到 agent_runs.json(保留最近 30 次;
  只记元数据与大小,不记内容全文)
"""
import json
import time
from pathlib import Path

KEEP_RUNS = 30
RUNS_FILE = "agent_runs.json"
# 本地终结工具:产出是操作回执而非内容,last_output 应跳过取更早的内容产出
LOCAL_TOOLS = ("save_note", "copy")


class RunMemory:
    """单次 Agent 运行的工作记忆;宿主(UI/测试)持有并读取。"""

    def __init__(self, input_head=1200):
        self.input_head = int(input_head)
        self.goal = ""
        self.input_text = ""
        self.current = ""          # 链上当前内容(工具的输入)
        self.rounds = 0
        self.entries = []          # [{round, id, tool, ok, out, ms, note}]
        self.finished = False
        self.error = ""
        self.cancelled = False
        self._t0 = 0.0

    # ---- 生命周期 ----
    def begin(self, goal, input_text):
        self.goal = str(goal or "")
        self.input_text = str(input_text or "")
        self.current = self.input_text
        self._t0 = time.time()

    @property
    def last_output(self):
        """最新内容产出(复制/保存按钮取这里):跳过本地终结工具的
        操作回执;没有任何内容产出时退回链上当前内容。"""
        for e in reversed(self.entries):
            if e.get("ok") and e.get("out") \
                    and e.get("tool") not in LOCAL_TOOLS:
                return e["out"]
        return self.current

    # ---- 3.4 observe / update_context ----
    def observe(self):
        """给规划器的上下文快照(dict,进提示词)。"""
        return {
            "goal": self.goal,
            "input_head": self.input_text[:self.input_head],
            "rounds": self.rounds,
            "done": [{"tool": e.get("tool"), "ok": e.get("ok")}
                     for e in self.entries],
            "failed": [e.get("tool") for e in self.entries if not e.get("ok")],
        }

    def push_result(self, round_no, task, ok, out, ms):
        """update_context:记录一步执行结果并推进内容链。"""
        self.entries.append({
            "round": int(round_no), "id": task.id, "tool": task.tool,
            "ok": bool(ok), "out": str(out or ""), "ms": round(float(ms), 1),
            "note": task.note})
        if ok and out:
            self.current = str(out)

    def finish(self, ok=True, note=""):
        self.finished = True
        if not ok:
            self.error = str(note or "未完成")

    # ---- 展示 / 落盘 ----
    def summary_text(self):
        """人类可读运行纪要(窗口收尾展示 / 测试断言)。"""
        lines = ["目标:%s" % (self.goal or "(未填写)"),
                 "轮次:%d · 任务:%d(成功 %d / 失败 %d)"
                 % (self.rounds, len(self.entries),
                    sum(1 for e in self.entries if e.get("ok")),
                    sum(1 for e in self.entries if not e.get("ok")))]
        for e in self.entries:
            mark = "✓" if e.get("ok") else "✗"
            lines.append("  %s 任务%s · %s(%d 字, %.0fms)"
                         % (mark, e.get("id"), e.get("tool"),
                            len(e.get("out") or ""), e.get("ms") or 0.0))
        if self.cancelled:
            lines.append("已取消")
        elif self.error:
            lines.append("未完成:" + self.error)
        else:
            lines.append("已完成")
        return "\n".join(lines)

    def record(self):
        """落盘记录(只元数据;内容不进文件,与运行记录同一隐私立场)。"""
        return {
            "ts": int(time.time()), "goal": self.goal,
            "input_chars": len(self.input_text),
            "rounds": self.rounds,
            "finished": self.finished and not self.cancelled,
            "cancelled": self.cancelled,
            "duration_ms": round((time.time() - self._t0) * 1000, 1),
            "tasks": [{"id": e["id"], "tool": e["tool"], "ok": e["ok"],
                       "chars": len(e.get("out") or ""), "ms": e.get("ms", 0)}
                      for e in self.entries],
        }

    @staticmethod
    def save_run(record, app_dir):
        """追加一条运行记录,保留最近 KEEP_RUNS 条;返回 (ok, 路径|错误)。"""
        try:
            p = Path(app_dir) / RUNS_FILE
            runs = []
            if p.exists():
                try:
                    runs = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    runs = []
                if not isinstance(runs, list):
                    runs = []
            runs.append(record)
            p.write_text(json.dumps(runs[-KEEP_RUNS:], ensure_ascii=False,
                                    indent=1), encoding="utf-8")
            return True, str(p)
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)
