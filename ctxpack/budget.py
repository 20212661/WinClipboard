# -*- coding: utf-8 -*-
"""Token 估算与预算分配(纯离线启发式,无网络、无第三方依赖、无 Qt)。

不引入 tiktoken:估算刻意偏保守(宁高估)——CJK 每字符约 1 token,
其余约 3.5 字符/token;代码/JSON 符号密集按 3.0 字符/token。
UI 显示一律带「≈」并注明"估算值,误差约 ±20%"。
实际可用预算 = budget_tokens * 0.9(余量留给问题与回答格式)。

同一输入必须得到同一输出:分配无随机、无时间依赖(便于快照式测试)。
"""
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Tuple

# 可配置估算系数(将来注入精确分词器时改这里)
CJK_RATE = 1.0                       # 每个 CJK 字符的 token 数
OTHER_CHARS_PER_TOKEN = 3.5          # 其余文本:字符/token
CODE_CHARS_PER_TOKEN = 3.0           # 代码/JSON:符号密集,字符/token 更少
SAFETY_MARGIN = 0.9                  # 实际可用 = budget * 0.9
DENSE_KINDS = frozenset({"code", "json"})

# 大条目阈值:est > budget*0.3 才考虑头尾截断,小条目直接丢弃
BIG_ITEM_RATIO = 0.3
# 截断保留比例:头 70% / 尾 30%
HEAD_RATIO = 0.7
# 可用于截断的最小 token 余量(放不下定界与省略标记就不截)
MIN_TRUNC_TOKENS = 16

DROP_OVER_BUDGET = "over_budget"
DROP_DISABLED = "disabled"
DROP_SENSITIVE = "sensitive_blocked"

# CJK 区段:汉字(扩展A/基本/兼容)、假名、谚文、全角形式(含全角标点)
_CJK_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
    (0xFF00, 0xFFEF),
)
# C 级正则删串实现:len(text) - len(删后) = CJK 字符数
# (实测 20 万字符 ~2.5ms,str.translate 逐字符查表要 ~17ms)
_CJK_CLASS = "".join(chr(lo) + "-" + chr(hi) for lo, hi in _CJK_RANGES)
_CJK_RE = re.compile("[" + _CJK_CLASS + "]+")


def count_cjk(text: str) -> int:
    """CJK 字符数(正则删串,C 速度;20 万字符约 2.5ms)。"""
    if not text:
        return 0
    return len(text) - len(_CJK_RE.sub("", text))


class TokenEstimator(Protocol):
    """估算器协议:将来可注入精确分词器(同一签名即可替换)。"""

    def estimate(self, text: str, kind: Optional[str] = None) -> int:
        ...


class DefaultEstimator:
    """离线启发式估算器(保守,宁高估)。"""

    def estimate(self, text: str, kind: Optional[str] = None) -> int:
        if not text:
            return 0
        cjk = count_cjk(text)
        other = len(text) - cjk
        div = CODE_CHARS_PER_TOKEN if kind in DENSE_KINDS else OTHER_CHARS_PER_TOKEN
        return int(math.ceil(cjk * CJK_RATE + other / div))


DEFAULT_ESTIMATOR = DefaultEstimator()


def estimate_tokens(text: str, kind: Optional[str] = None) -> int:
    return DEFAULT_ESTIMATOR.estimate(text, kind)


def refresh_estimates(items, estimator: Optional[TokenEstimator] = None) -> None:
    """就地刷新条目的 est_tokens 缓存(内容变更后由 service 调用)。"""
    est = estimator or DEFAULT_ESTIMATOR
    for it in items:
        it.est_tokens = est.estimate(it.content, it.kind)


class BudgetError(Exception):
    """预算无法满足的确定性错误。

    code:INSTRUCTIONS_OVER_BUDGET(指令条目自身超预算,不拼装)。
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__("%s%s" % (code, (": " + detail) if detail else ""))
        self.code = code
        self.detail = detail


@dataclass
class AllocationEntry:
    """一条入选条目及其拼装用内容(截断后)。"""
    item: object                      # ContextItem(鸭子类型,便于测试替身)
    content: str
    truncated: bool
    omitted_chars: int = 0            # 截断省略的字符数(0=未截断)
    est_tokens: int = 0               # 截断后内容的估算(含条目级开销外)


@dataclass
class Allocation:
    entries: List[AllocationEntry] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)   # (iid, reason)
    overhead_tokens: int = 0          # 已扣除的固定开销(preamble/框架/问题)

    @property
    def included(self) -> List[str]:
        return [e.item.iid for e in self.entries]


def usable_budget(budget_tokens: int) -> int:
    """实际可用预算(0.9 安全边际,向下取整)。"""
    return int(int(budget_tokens) * SAFETY_MARGIN)


def truncate_head_tail(content: str, token_budget: int, kind: Optional[str] = None,
                       estimator: Optional[TokenEstimator] = None) -> Tuple[str, int]:
    """头尾保留截断:保留头 70% / 尾 30%,中间插省略标记。

    返回 (截断后内容, 省略字符数);预算放不下最小截断时返回 ("", -1)。
    按内容自身密度换算字符配额,超预算则按 5% 收缩重试(确定性循环)。
    """
    est = estimator or DEFAULT_ESTIMATOR
    if token_budget < MIN_TRUNC_TOKENS or not content:
        return "", -1
    density = max(est.estimate(content, kind) / float(len(content)), 1e-6)
    keep = int(token_budget / density)
    marker_tpl = "\n[…已省略 %d 字符…]\n"
    while keep >= 8:
        head = max(1, int(math.ceil(keep * HEAD_RATIO)))
        tail = max(1, keep - head) if keep - head >= 1 else 0
        marker = marker_tpl % max(0, len(content) - head - tail)
        out = content[:head] + marker + (content[len(content) - tail:] if tail else "")
        if est.estimate(out, kind) <= token_budget:
            return out, max(0, len(content) - head - tail)
        keep = int(keep * 0.95)
    return "", -1


def allocate(items, budget_tokens: int, overhead_tokens: int = 0,
             per_item_overhead: int = 0,
             estimator: Optional[TokenEstimator] = None,
             include_sensitive: bool = False) -> Allocation:
    """预算分配(确定性)。

    - preamble/框架/问题的开销由调用方估好后经 overhead_tokens 传入;
    - role=instruction 条目不可丢弃,仅它们就超预算 → BudgetError
      (INSTRUCTIONS_OVER_BUDGET,不拼装);
    - 其余按 (priority desc, captured_at desc, 原始顺序) 依次放入;
    - 放不下且 est > budget*0.3 的大条目头尾截断,仍放不下/小条目丢弃
      (over_budget);disabled 与(未放行的)sensitive 条目各有原因码。
    """
    est = estimator or DEFAULT_ESTIMATOR
    rem = usable_budget(budget_tokens) - int(overhead_tokens)
    alloc = Allocation(overhead_tokens=int(overhead_tokens))

    active = []
    for it in items:
        if not it.enabled:
            alloc.dropped.append((it.iid, DROP_DISABLED))
        elif it.sensitive and not include_sensitive:
            alloc.dropped.append((it.iid, DROP_SENSITIVE))
        else:
            active.append(it)
            if it.est_tokens <= 0:              # 缓存缺失就地补(不改内容)
                it.est_tokens = est.estimate(it.content, it.kind)

    instructions = [it for it in active if it.role == "instruction"]
    others = [it for it in active if it.role != "instruction"]

    need_instr = sum(it.est_tokens + per_item_overhead for it in instructions)
    if instructions and need_instr > rem:
        raise BudgetError(
            "INSTRUCTIONS_OVER_BUDGET",
            "指令条目共约 %d tokens,可用 %d" % (need_instr, max(0, rem)))
    rem -= need_instr

    # 选取顺序:(priority desc, captured_at desc, 原始顺序兜底)——
    # 保证同一输入同一输出;呈现顺序由拼装器按包内顺序负责。
    order = sorted(range(len(others)),
                   key=lambda i: (-others[i].priority, -others[i].captured_at, i))
    selected = []                 # (item, content, truncated, omitted, est)
    for i in order:
        it = others[i]
        need = it.est_tokens + per_item_overhead
        if need <= rem:
            selected.append((it, it.content, False, 0, it.est_tokens))
            rem -= need
            continue
        if it.est_tokens > budget_tokens * BIG_ITEM_RATIO:
            piece, omitted = truncate_head_tail(
                it.content, max(0, rem - per_item_overhead), it.kind, est)
            if piece:
                piece_est = est.estimate(piece, it.kind)
                selected.append((it, piece, True, omitted, piece_est))
                rem -= piece_est + per_item_overhead
                continue
        alloc.dropped.append((it.iid, DROP_OVER_BUDGET))

    picked = {id(t[0]): t for t in selected}
    # 入选条目按包内顺序呈现(尊重用户拖拽排序)
    for it in instructions:
        if it.est_tokens <= 0:
            it.est_tokens = est.estimate(it.content, it.kind)
        picked[id(it)] = (it, it.content, False, 0, it.est_tokens)
    for it in items:
        t = picked.get(id(it))
        if t is None:
            continue
        alloc.entries.append(AllocationEntry(
            item=t[0], content=t[1], truncated=t[2],
            omitted_chars=t[3], est_tokens=t[4]))
    return alloc
