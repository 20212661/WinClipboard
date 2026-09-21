# -*- coding: utf-8 -*-
"""AI 动作执行器:动作 → 工作流定义 → 交宿主运行台执行(路线图 1.3:
复用既有 WorkflowRunner,不另造执行路径)。

隐私门、运行记录(Run Inspector)、逐步人工把关、取消等能力全部由
运行台继承;这里只做解析与守卫:

    resolve_workflow(cfg, ref)   用户配置(ai_workflows 按 name/id)>
                                 内置(BUILTIN_WORKFLOWS),深拷贝隔离
    run_action(host, defn, rec)  守卫(文本条目/工作流可解析)→
                                 host.run_workflow(wf, rec)

宿主只需提供 cfg 与 run_workflow(真实 WinClipboardApp 与测试
FakeApp 都满足)。
"""
import json

from .workflows import BUILTIN_WORKFLOWS


def _copy(w):
    """深拷贝工作流定义:运行台/编辑器的原地修改不回写模板。"""
    return json.loads(json.dumps(w, ensure_ascii=False))


def resolve_workflow(cfg, ref):
    """工作流标识 → 工作流定义 dict;未找到返回 None。

    用户配置优先:ai_workflows 里 name 或 id 命中 ref、或命中该内置
    动作的显示名(用户按「解释代码」命名即覆盖 code_explain),否则
    回落内置定义。"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    builtin = BUILTIN_WORKFLOWS.get(ref)
    names = {ref, builtin["name"]} if builtin else {ref}
    for w in (cfg or {}).get("ai_workflows") or []:
        if w.get("name") in names or w.get("id") in names:
            return _copy(w)
    return _copy(builtin) if builtin is not None else None


def has_service(cfg):
    """是否配置了任一 AI 服务(未配置时菜单引导去「AI 服务」页)。"""
    return bool((cfg or {}).get("ai_services"))


def run_action(host, defn, rec):
    """执行一个 AI 动作;返回 (ok, 附言)。"""
    if (rec or {}).get("type") != "text":
        return False, "AI 动作仅支持文本类条目"
    ref = (defn or {}).get("workflow") if defn is not None else None
    wf = resolve_workflow(host.cfg, ref)
    if wf is None:
        return False, "未找到工作流「%s」" % (ref or "?")
    host.run_workflow(wf, rec)
    return True, ""
