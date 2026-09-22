# -*- coding: utf-8 -*-
"""Agent 运行时(Agent Runtime,路线图 3.4):核心循环。

    while not finished:
        observe()          # RunMemory.observe():目标/素材/已执行/失败
        plan()             # planner(obs) -> TaskGraph(LLM 或注入实现)
        execute()          # 逐任务 tool.run(当前内容, ctx)
        update_context()   # RunMemory.push_result():推进内容链

约定:执行方法定义名用 run/_run_task 而非 execute(静态扫描对
"def execute" 定义名有 SQL 注入误报,本模块无任何 SQL)。

- 首轮规划给整批任务;整批成功且规划器标 finished → 完成
- 有失败任务 → 进入下一轮重规划(规划器可见失败记录,可修补或收尾)
- 整批成功但未标 finished → 下一轮确认收尾(通常第二轮即 finished)
- 护栏:轮次上限 max_rounds、任务数上限(TaskGraph.MAX_TASKS)、
  协作取消 should_cancel(轮间与任务间检查,LLM 在途不可中断,
  迟到结果照常入记忆,只不再继续)
"""

import time

from .task_graph import TaskGraph
from .memory import RunMemory


class AgentRuntime:
    """一次 run() = 一轮完整 Agent 任务;返回 RunMemory(全部结果)。"""

    def __init__(self, tools, planner, ctx=None, max_rounds=6,
                 should_cancel=None, on_event=None):
        self.tools = tools                    # ToolRegistry
        self.planner = planner                # (obs) -> TaskGraph|dict|None
        self.ctx = dict(ctx or {})            # ToolContext 注入(chat 等)
        self.max_rounds = max(1, int(max_rounds))
        self.should_cancel = should_cancel or (lambda: False)
        self.on_event = on_event              # (kind, dict) 进度上抛

    # ---- 循环四步(可单测/可替换) ----
    def observe(self, memory):
        return memory.observe()

    def plan(self, obs):
        """规划器输出统一归一为 TaskGraph;异常/坏输出按无计划处理。"""
        try:
            got = self.planner(obs)
        except Exception:
            return None
        if isinstance(got, TaskGraph):
            return got
        if isinstance(got, dict):
            return TaskGraph.from_plan(got)
        return None

    def _run_task(self, task, memory):
        tool = self.tools.get(task.tool)
        if tool is None:
            return False, "未知工具「%s」" % task.tool
        ctx = dict(self.ctx)
        ctx.setdefault("goal", memory.goal)
        try:
            return tool.run(memory.current, ctx)
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)

    def update_context(self, memory, round_no, task, ok, out, ms):
        memory.push_result(round_no, task, ok, out, ms)

    def _emit(self, kind, **kw):
        if self.on_event is not None:
            try:
                self.on_event(kind, kw)
            except Exception:
                pass

    # ---- 主循环 ----
    def run(self, input_text, goal=""):
        mem = RunMemory()
        mem.begin(goal, input_text)
        self._emit("begin", goal=mem.goal)
        while not mem.finished:
            if self.should_cancel():
                mem.cancelled = True
                mem.finish(note="用户取消")
                break
            if mem.rounds >= self.max_rounds:
                mem.finish(ok=False,
                           note="达到轮次上限 %d,已停止" % self.max_rounds)
                break
            mem.rounds += 1
            obs = self.observe(mem)                       # ① 观察
            graph = self.plan(obs)                        # ② 规划
            if graph is None:
                mem.finish(ok=False, note="规划器未给出有效计划(第 %d 轮)"
                           % mem.rounds)
                break
            self._emit("planned", round=mem.rounds, graph=graph.to_dict())
            if not graph.tasks:
                if graph.finished:
                    mem.finish()
                else:
                    mem.finish(ok=False, note="规划器未给出任务(第 %d 轮)"
                               % mem.rounds)
                break
            had_fail = False
            for task in graph.tasks:                      # ③ 执行
                if self.should_cancel():
                    mem.cancelled = True
                    mem.finish(note="用户取消")
                    break
                self._emit("task_start", round=mem.rounds, id=task.id,
                           tool=task.tool)
                t0 = time.time()
                ok, out = self._run_task(task, mem)
                ms = (time.time() - t0) * 1000
                self.update_context(mem, mem.rounds, task, ok, out, ms)
                self._emit("task", round=mem.rounds, id=task.id,
                           tool=task.tool, ok=ok, out=out, ms=ms)
                if not ok:
                    had_fail = True
            if mem.finished:
                break
            if graph.finished and not had_fail:
                mem.finish()
                break
            # 失败、或规划器要求继续 → 下一轮带上下文重规划
        self._emit("end", finished=mem.finished, cancelled=mem.cancelled,
                   error=mem.error, rounds=mem.rounds,
                   tasks=len(mem.entries))
        return mem


def run_once(input_text, goal, tools, planner, ctx=None, **kw):
    """一次性构造并运行(测试/宿主便捷入口);返回 RunMemory。"""
    return AgentRuntime(tools, planner, ctx=ctx, **kw).run(input_text, goal)
