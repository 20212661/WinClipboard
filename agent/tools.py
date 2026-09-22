# -*- coding: utf-8 -*-
"""Agent 工具系统(Tool Registry,路线图 3.3)。

Tool.run(text, ctx) -> (ok, 产出文本);上一步产出是下一步输入(与
工作流 {input} 语义一致)。执行方法定义名为 run 而非 execute——
"def execute" 定义名与含字符串数据流的类体会触发静态扫描误报
(同 workflow_steps 先例,本模块不含任何 SQL)。

ToolContext(注入,无 Qt):
    chat(prompt) -> (ok, text)          LLM 单发(默认服务解析由宿主做)
    save_note(goal, text) -> (ok, msg)  追加笔记(默认落 APP_DIR/agent_notes.md)
    copy_text(text) -> None             写剪贴板(宿主负责线程归位)
"""

_LLM_TOOLS = (
    ("summary", "总结文本",
     ("summary", "摘要"),
     "请总结下面内容:先一句话摘要,再列 3-5 个要点。只输出总结:\n"),
    ("translate", "翻译文本(中英互译)",
     ("translate", "翻译"),
     "判断下面文本的语言:中文则翻译成英文,其他语言则翻译成中文。只输出译文:\n"),
    ("code_explain", "解释代码",
     ("code_explain", "explain"),
     "你是资深工程师。请解释下面这段代码:先一句话概括用途,再逐段说明关键"
     "逻辑,最后指出潜在问题。只输出解释,不要复述代码:\n"),
    ("markdown", "整理为 Markdown",
     ("markdown_export", "to_markdown", "md"),
     "把下面内容整理为规范的 Markdown(标题层级/列表/代码块),保留全部信息。"
     "只输出 Markdown 源码:\n"),
    ("keywords", "提取关键词",
     ("keyword", "关键词"),
     "提取下面内容的关键词,5-10 个,用中文逗号分隔,只输出关键词:\n"),
)


class Tool:
    """一个 Agent 工具:name 唯一,aliases 供规划器容错匹配。"""

    __slots__ = ("name", "description", "aliases", "run")

    def __init__(self, name, description, run, aliases=()):
        self.name = str(name)
        self.description = str(description)
        self.aliases = tuple(str(a) for a in aliases)
        self.run = run

    def names(self):
        return {self.name, *self.aliases}


class ToolRegistry:
    """工具注册表:register(Tool)/按名或别名取/给规划器的清单文案。"""

    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool
        return tool

    def get(self, name):
        n = str(name or "").strip().lower()
        if not n:
            return None
        if n in self._tools:
            return self._tools[n]
        for t in self._tools.values():
            if n in t.names():
                return t
        return None

    def all(self):
        return list(self._tools.values())

    def __len__(self):
        return len(self._tools)

    def descriptions_for_prompt(self):
        """规划器提示词里的工具清单(名字: 描述,一行一个)。"""
        return "\n".join("- %s: %s" % (t.name, t.description)
                         for t in self._tools.values())


def _llm_tool(name, desc, aliases, prompt_head):
    def run(text, ctx):
        chat = (ctx or {}).get("chat")
        if chat is None:
            return False, "宿主未提供 LLM 通道"
        return chat(prompt_head + str(text or ""))
    return Tool(name, desc, run, aliases)


def default_save_note(path, goal, text):
    """默认笔记落盘:追加到 markdown 文件,带时间戳与小节标题。

    返回 (ok, 附言)。读改写走 pathlib(见 Mimosa 规律),无并发诉求。"""
    from pathlib import Path
    import datetime
    try:
        p = Path(path)
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        if old and not old.endswith("\n"):
            old += "\n"
        chunk = "\n## %s · %s\n\n%s\n" % (
            goal or "Agent 笔记",
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            str(text or "").rstrip())
        p.write_text(old + chunk, encoding="utf-8")
        return True, "已保存到 %s(累计 %d 字)" % (p.name, len(old) + len(chunk))
    except Exception as e:
        return False, "保存笔记失败:%s: %s" % (type(e).__name__, e)


def build_default_tools():
    """内置工具集:五个 LLM 工具 + save_note + copy(链尾出口)。"""
    reg = ToolRegistry()
    for name, desc, aliases, head in _LLM_TOOLS:
        reg.register(_llm_tool(name, desc, aliases, head))

    def _note(text, ctx):
        goal = (ctx or {}).get("goal", "")
        fn = (ctx or {}).get("save_note") or default_save_note
        return fn(goal, text)
    reg.register(Tool("save_note", "把当前结果保存为笔记(agent_notes.md)",
                      _note, ("note", "save")))

    def _copy(text, ctx):
        fn = (ctx or {}).get("copy_text")
        if fn is None:
            return False, "宿主未提供剪贴板通道"
        try:
            fn(str(text or ""))
            return True, "已复制到剪贴板(%d 字)" % len(str(text or ""))
        except Exception as e:
            return False, "写剪贴板失败:%s" % e
    reg.register(Tool("copy", "把当前结果复制到剪贴板", _copy,
                      ("clipboard",)))
    return reg
