# -*- coding: utf-8 -*-
"""Agent Runtime(路线图三阶段):用户 → LLM Planner → 任务计划 → 执行。

    planner.py     LLMPlanner:目标 + 工具清单 → 任务计划 JSON(可注入替换)
    task_graph.py  Task/TaskGraph:计划归一化(编号/别名/上限)与校验
    tools.py       Tool + 工具注册表(summary/translate/code_explain/
                   markdown/keywords/save_note/copy)
    memory.py      RunMemory:运行工作记忆(观察快照/任务记录/落盘)
    executor.py    AgentRuntime:observe → plan → execute → update_context
                   循环(取消协作、轮次与任务上限)

UI 接线在 winclipboard_qt(AgentRunner 窗口,后台线程跑本包)。
本包纯 Python 无 Qt;LLM 调用与本地副作用(写笔记/写剪贴板)全部经
ToolContext 注入,测试用假实现即可覆盖全流程。
"""
from .tools import Tool, ToolRegistry, build_default_tools  # noqa: F401
from .task_graph import Task, TaskGraph  # noqa: F401
from .planner import LLMPlanner, extract_json  # noqa: F401
from .memory import RunMemory  # noqa: F401
from .executor import AgentRuntime, run_once  # noqa: F401
