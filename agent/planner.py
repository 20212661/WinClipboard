# -*- coding: utf-8 -*-
"""Agent 规划器(Planner,路线图 3.2/3.4 的 plan 环)。

LLMPlanner 是"可注入的规划函数":observe() 给出的上下文快照 → 提示词 →
严格 JSON → TaskGraph。测试/宿主可用任意 (obs) -> dict|TaskGraph|None
的可调用替换规划器(脚本化测试不碰网络)。

协议(模型只需遵守):
    首轮   {"goal":..., "tasks":[{"id":1,"tool":...},...], "finished":false}
    后续轮 {"finished":true} 收尾;或给修补任务继续;或不输出 → 规划失败
"""

from .task_graph import TaskGraph

_MARK = "你是任务规划器"


def extract_json(text):
    """从模型回复里抠 JSON:整串 → ```json 围栏 → 首尾大括号截取。"""
    s = str(text or "").strip()
    if not s:
        return None
    import json
    try:
        return json.loads(s)
    except Exception:
        pass
    if "```" in s:                       # 围栏代码块
        for seg in s.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{") and seg.endswith("}"):
                try:
                    return json.loads(seg)
                except Exception:
                    continue
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            return None
    return None


class LLMPlanner:
    """把观察快照变成任务计划的规划函数(经 chat 回调调用 LLM)。"""

    def __init__(self, chat, tools=None, input_head=1200):
        self.chat = chat                    # (prompt) -> (ok, text)
        self.tools = tools                  # ToolRegistry(描述清单用)
        self.input_head = int(input_head)

    def __call__(self, obs):
        tools_block = self.tools.descriptions_for_prompt() \
            if self.tools is not None else "(未提供工具清单)"
        done_block = ""
        if obs.get("done"):
            done_block = "已执行记录:\n" + "\n".join(
                "- %s(%s)" % (d.get("tool"), "成功" if d.get("ok") else "失败")
                for d in obs["done"][-8:])
        elif obs.get("rounds"):
            done_block = "(尚未执行任何任务)"
        prompt = (
            _MARK + "。请为用户目标制定任务计划。\n\n"
            "可用工具:\n" + tools_block + "\n\n"
            "用户目标:" + str(obs.get("goal") or "") + "\n"
            "素材预览(前 %d 字):\n%s\n%s\n"
            "严格只输出如下格式的 JSON,不要任何其他文字:\n"
            '{"goal": "<目标>", "tasks": [{"id": 1, "tool": "<工具名>"}],'
            ' "finished": false}\n\n'
            "规则:工具名只能用上面列出的;任务顺序即执行顺序,上一步产出是"
            "下一步输入;通常 2-5 个任务,收尾常用 save_note 或 copy;"
            "若已有执行记录且目标已达成,输出 tasks 为空并给 "
            '"finished": true。'
            % (self.input_head,
               str(obs.get("input_head") or "")[:self.input_head],
               done_block))
        ok, text = self.chat(prompt)
        if not ok:
            return None
        data = extract_json(text)
        return TaskGraph.from_plan(data) if data is not None else None
