# -*- coding: utf-8 -*-
"""AI Action Bar 数据层(路线图一阶段)。

卡片「✨ AI」按钮 → 动作菜单 → 工作流运行台,三层各司其职:

    actions/*.json  声明式动作定义(name/id/workflow/icon/applies)
    registry.py     ActionRegistry:启动扫描注册,卡片菜单的条目来源
    workflows.py    内置动作工作流(ai 步骤 service 留空 = 默认服务)
    executor.py     动作 → 工作流解析与守卫,执行复用 app.run_workflow

UI 接线在 winclipboard_qt(卡片按钮 / 菜单构建),本包保持纯 Python
无 Qt,与 workflow_steps 同样的可单测约定。
"""
from .registry import ActionRegistry, get_registry, init  # noqa: F401
from .workflows import BUILTIN_WORKFLOWS  # noqa: F401
from . import executor  # noqa: F401
