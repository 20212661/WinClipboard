# -*- coding: utf-8 -*-
"""AI 动作执行器:动作 → 工作流定义 → 交宿主运行台执行。

PR3A 移植自 feature/ai-action-bar(2026-09-22),按 main 现状适配:

- 守卫按动作 applies 列表放行(原版硬编码仅 text;applies 缺省
  ["text"],对既有动作行为不变,为图片/文件类动作留扩展位);
- 新增 ActionRequest / ActionResult 契约与 run_request:支持
  context_pack 注入(交给运行台在启动时经 ctxpack 拼装恒定
  {context} 快照)与 auto 自动开跑(main 的 run_workflow 第三参);
- 隐私门、运行记录(Run Inspector)、逐步人工把关、取消等能力全部
  由 WorkflowRunner 继承,不另造执行路径。

宿主只需提供 cfg 与 run_workflow(真实 WinClipboardApp 与测试
FakeApp 都满足);图标 FluentIcon 枚举解析留在宿主 UI 层白名单,
本包不依赖 Qt。
"""
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .workflows import BUILTIN_WORKFLOWS


@dataclass
class ActionRequest:
    """一次动作执行的输入契约。

    record 沿用剪贴板记录 dict 形状(type/content/pinned/…):文本即
    {"type": "text", "content": str};未来图片/富文本/文件引用继续以
    记录形状承载,不另造内容模型。params 预留动作级参数(模板变量等);
    取消执行由宿主运行台承接(WorkflowRunner 的 Job.cancel 语义)。"""

    action_id: str
    record: Dict[str, Any]
    context_pack: Optional[str] = None   # None=不改工作流自带绑定;"@active"/包 id
    params: Optional[Dict[str, Any]] = None   # 预留;当前执行器不解释
    auto: bool = False                   # True=运行台自动开跑


@dataclass
class ActionResult:
    """动作执行结果:执行本体交宿主运行台,取消/失败在其内部呈现;
    这里只报告解析与守卫阶段。workflow 回传解析出的定义便于调用方
    展示/记录。"""

    ok: bool
    detail: str = ""
    workflow: Optional[Dict[str, Any]] = None


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
    """是否配置了任一 AI 服务(未配置时宿主引导去「AI 服务」页)。"""
    return bool((cfg or {}).get("ai_services"))


def _execute(host, defn, rec, context_pack=None, auto=False) -> ActionResult:
    applies = (defn or {}).get("applies") or ["text"]
    if (rec or {}).get("type") not in applies:
        return ActionResult(
            False, "动作「%s」不支持 %s 类条目(支持:%s)"
            % ((defn or {}).get("name") or "?",
               (rec or {}).get("type") or "?", "/".join(applies)))
    ref = (defn or {}).get("workflow") if defn is not None else None
    wf = resolve_workflow(host.cfg, ref)
    if wf is None:
        return ActionResult(False, "未找到工作流「%s」" % (ref or "?"))
    if context_pack:
        # 副本上注入,不改用户工作流模板;显式请求优先于工作流自带绑定
        wf["context_pack"] = context_pack
    if auto:
        host.run_workflow(wf, rec, auto=True)
    else:
        host.run_workflow(wf, rec)
    return ActionResult(True, workflow=wf)


def run_action(host, defn, rec):
    """执行一个 AI 动作(兼容入口);返回 (ok, 附言)。"""
    res = _execute(host, defn, rec)
    return res.ok, res.detail


def run_request(host, defn, req):
    """按契约执行:defn 来自 registry.get(req.action_id),执行路径与
    run_action 完全一致,仅多出 context_pack / auto 两项语义。"""
    return _execute(host, defn, req.record,
                    context_pack=req.context_pack, auto=req.auto)
