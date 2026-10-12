# -*- coding: utf-8 -*-
"""AI 上下文包(Context Pack):收集 → 预算 → 拼装 → 注入。

纯 Python 核心包:模型/预算/拼装/存储/服务不依赖 Qt;UI 挂接
(ui_tray / ui_page)由 winclipboard_qt 延迟 import,不影响启动路径。
小而稳:不做向量库、不做自动记忆、不做多轮会话管理。
"""
from .model import (ContextItem, ContextPack, ITEM_KINDS, ITEM_ROLES,
                    MAX_ITEM_CONTENT, TITLE_MAX, DEFAULT_BUDGET_TOKENS,
                    MIN_BUDGET_TOKENS, MAX_BUDGET_TOKENS, PACK_VERSION,
                    clamp_budget, default_title, make_item, new_pack)
from .budget import (Allocation, AllocationEntry, BudgetError,
                     DefaultEstimator, TokenEstimator, DEFAULT_ESTIMATOR,
                     DROP_DISABLED, DROP_OVER_BUDGET, DROP_SENSITIVE,
                     count_cjk, estimate_tokens, refresh_estimates,
                     truncate_head_tail, usable_budget, allocate)
from .assemble import (Assembled, FORMATS, FMT_XML, FMT_MARKDOWN, assemble)

__all__ = [
    "ContextItem", "ContextPack", "ITEM_KINDS", "ITEM_ROLES",
    "MAX_ITEM_CONTENT", "TITLE_MAX", "DEFAULT_BUDGET_TOKENS",
    "MIN_BUDGET_TOKENS", "MAX_BUDGET_TOKENS", "PACK_VERSION",
    "clamp_budget", "default_title", "make_item", "new_pack",
    "Allocation", "AllocationEntry", "BudgetError", "DefaultEstimator",
    "TokenEstimator", "DEFAULT_ESTIMATOR", "DROP_DISABLED",
    "DROP_OVER_BUDGET", "DROP_SENSITIVE", "count_cjk", "estimate_tokens",
    "refresh_estimates", "truncate_head_tail", "usable_budget", "allocate",
    "Assembled", "FORMATS", "FMT_XML", "FMT_MARKDOWN", "assemble",
]
