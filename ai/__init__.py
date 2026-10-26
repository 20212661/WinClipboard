# -*- coding: utf-8 -*-
"""AI Action Registry 数据层(PR3A,移植自 feature/ai-action-bar)。

    actions/*.json  声明式动作定义(name/id/workflow/icon/applies)
    registry.py     ActionRegistry:启动扫描注册(内置目录 → 用户
                    APP_DIR/ai_actions,同 id 用户覆盖内置)
    workflows.py    内置动作工作流(ai 步骤 service 留空 = 默认服务,
                    配置任一服务即可直跑;用户同名工作流可覆盖)
    executor.py     动作 → 工作流解析与守卫 + ActionRequest/Result
                    契约(context_pack 注入 / auto 开跑),执行复用
                    宿主 run_workflow(生产 = WorkflowRunner)

与 main 的对接:cm.ai_chat 仍是唯一模型调用出口(经 WorkflowRunner
继承);context_pack 走运行台启动时的 ctxpack 拼装快照。图标
FluentIcon 枚举解析留在宿主 UI 层白名单,本包保持纯 Python 无 Qt,
与 workflow_steps 同样的可单测约定。agent/、command/ 不在本阶段范围。
"""
from .registry import ActionRegistry, get_registry, init  # noqa: F401
from .workflows import BUILTIN_WORKFLOWS  # noqa: F401
from .executor import (ActionRequest, ActionResult,  # noqa: F401
                       resolve_workflow, run_action, run_request)
from . import session  # noqa: F401
from .sink import ResultSink  # noqa: F401
from . import executor  # noqa: F401
