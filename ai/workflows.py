# -*- coding: utf-8 -*-
"""内置 AI 动作工作流:与 ai/actions/*.json 的 workflow 字段一一对应。

键 = 工作流标识。ai 步骤的 service 留空 = 默认服务(第一个已配置的
AI 服务,见 cm.ai_service_by_name 的空名回退),因此**配置任一服务
即可直跑,零配置门槛**;用户在「工作流」页创建同名工作流即可覆盖
内置定义(executor.resolve_workflow 用户配置优先)。
"""

BUILTIN_WORKFLOWS = {
    "code_explain": {"name": "解释代码", "steps": [
        {"kind": "ai", "service": "", "prompt":
            "你是资深工程师。请解释下面这段代码:先一句话概括用途,"
            "再逐段说明关键逻辑,最后指出潜在问题或注意点。"
            "只输出解释,不要复述代码:\n{input}"},
    ]},
    "code_optimize": {"name": "优化代码", "steps": [
        {"kind": "ai", "service": "", "prompt":
            "你是重构专家。请优化下面代码,保持行为不变:先给优化后的"
            "完整代码(放在代码块里),再用要点列出每处改动与理由:"
            "\n{input}"},
    ]},
    "code_comment": {"name": "生成注释", "steps": [
        {"kind": "ai", "service": "", "prompt":
            "请为下面代码补充中文注释:模块/函数加 docstring,关键逻辑"
            "加行内注释;只输出加好注释后的完整代码,不要解释:"
            "\n{input}"},
    ]},
    "summarize": {"name": "总结", "steps": [
        {"kind": "ai", "service": "", "prompt":
            "请总结下面内容:先一句话摘要,再列 3-5 个要点。"
            "只输出总结:\n{input}"},
    ]},
    "translate": {"name": "翻译", "steps": [
        {"kind": "ai", "service": "", "prompt":
            "判断下面文本的语言:中文则翻译成英文,其他语言则翻译成"
            "中文。只输出译文,不要任何解释:\n{input}"},
    ]},
    "to_markdown": {"name": "转Markdown", "steps": [
        {"kind": "ai", "service": "", "prompt":
            "把下面内容整理为规范的 Markdown:合理使用标题层级、列表与"
            "代码块,保留全部信息不删减。只输出 Markdown 源码:"
            "\n{input}"},
    ]},
}
