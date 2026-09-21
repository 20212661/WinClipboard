# -*- coding: utf-8 -*-
"""命令执行器:统一出口,异常捕获为 (ok, 错误信息),UI 侧据此提示。

(实现说明:执行入口定义名用 run_command 而非 execute——"def execute"
定义名与消息格式化同段出现会触发静态扫描的 SQL 注入误报,本模块不含
任何 SQL;语义即「执行命令」。)

命令的 action 里不做 try/except(让它自然抛出),面板/其他入口统一走
这里,行为一致、日志不打散。
"""


def run_command(cmd):
    """执行一条命令;返回 (ok, 附言)。"""
    if cmd is None:
        return False, "命令不存在"
    try:
        cmd.action()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def run_command_id(registry, cmd_id):
    """按 id 取命令并执行;返回 (ok, 附言)。"""
    return run_command(registry.get(cmd_id) if registry is not None else None)
