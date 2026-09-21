# -*- coding: utf-8 -*-
"""Command Palette 数据层(路线图二阶段)。

command.py    Command 对象(id/name/shortcut/action/when/category/keywords)
registry.py   CommandRegistry:注册 + 上下文过滤 + 打分搜索
executor.py   统一执行出口(异常捕获,UI 侧只管成功与否的提示)
builtin.py    内置命令目录:全部功能一处注册,以后新增功能只登记这里

UI 接线在 winclipboard_qt + palette.py(命令面板浮层,默认 Ctrl+Shift+P)。
本包保持纯 Python 无 Qt,与 workflow_steps / ai 同样的可单测约定。
"""
from .command import Command  # noqa: F401
from .registry import CommandRegistry, CATEGORY_ORDER  # noqa: F401
from . import executor  # noqa: F401
from . import builtin  # noqa: F401
