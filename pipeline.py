# -*- coding: utf-8 -*-
"""剪贴板捕获管线(Capture Pipeline)——阶段化、纯决策、可追踪。

    Snapshot(规范化) → SensitiveDetector → Deduplication
        → ContentClassifier(轻量徽章) → Repository/私密区(副作用收口在核心)

设计约束:
- 每个阶段 = 输入 → 输出,不直接碰 UI / 文件 / 剪贴板 / 全局状态;
  写历史、写私密区、提升置顶等副作用全部由 ClipboardCore 执行。
- 每次捕获产出 CaptureTrace(阶段名 → 结论/原因),挂在
  core.last_capture_trace:排障「这条为什么没进私密区 / 没入史 /
  被当成重复」直接看 trace,不用读代码。
- 与既有 tuple 协议(2/3/4 元)兼容:Snapshot.from_item;阶段对
  敏感/去重的判定顺序与旧实现一致(敏感优先——重复复制的敏感文本
  也必须继续隔离,不能被去重吞掉)。
- 本模块不 import clipboard_manager(方向单一:cm → pipeline),
  规则类探测(敏感正则、徽章)由调用方以 callable 注入。
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# Outcome.kind 常量(不用 enum,保持轻量与可序列化)
SENSITIVE = "sensitive"          # → 私密区,不入普通历史
DUPLICATE = "duplicate"          # → 已有条目提升到最前(替换式:新 ts/rich/src)
PINNED_SKIP = "pinned_skip"      # → 与置顶条目重复,不动
KEEP = "keep"                    # → 新条目入库


@dataclass
class Snapshot:
    """规范化后的剪贴板快照:一种输入形态,后续阶段只看它。"""
    kind: str                    # "text" | "files" | "image"
    content: object              # text: str;files: list[str];image: dict(path/w/h/hash)
    rich: dict = field(default_factory=dict)   # {"html":…, "rtf":…}
    src: str = ""                # 来源应用 exe 名(小写)
    ts: float = 0.0

    @classmethod
    def from_item(cls, item, now=None):
        """tuple 协议 → Snapshot。
        ("text", t) / ("text", t, rich) / ("text", t, rich, src) /
        ("files", paths[, …]) / ("image", dict[, …]);Snapshot 原样返回。"""
        if isinstance(item, cls):
            return item
        import time as _t
        kind, content = item[0], item[1]
        rich, src = {}, ""
        for extra in item[2:]:
            if isinstance(extra, dict):
                rich = {k: v for k, v in extra.items()
                        if k in ("html", "rtf") and v}
            elif extra:
                src = str(extra).lower()
        return cls(kind=kind, content=content, rich=rich, src=src,
                   ts=now if now is not None else _t.time())


def fingerprint(snap: Snapshot):
    """新快照的去重键(与旧实现语义一致):文本按内容,图片按像素哈希,
    文件按路径元组。"""
    if snap.kind == "text":
        return snap.content
    if snap.kind == "files":
        return ("files", tuple(snap.content))
    return ("img", (snap.content or {}).get("hash", ""))


def fingerprint_of_rec(rec: dict, sha1_fallback: Optional[Callable] = None):
    """历史条目的去重键(纯读)。旧图片条目可能没有 hash 字段,
    由调用方注入 sha1 回退(涉及磁盘 IO,不属于纯阶段)。"""
    t = rec.get("type")
    if t == "text":
        return rec["content"]
    if t == "files":
        return ("files", tuple(rec.get("content", [])))
    h = rec.get("hash")
    if h:
        return ("img", h)
    return ("img", sha1_fallback(rec["content"]) if sha1_fallback else "")


def make_record(snap: Snapshot) -> dict:
    """纯构造 history 记录(不动任何状态;持久化字段与旧格式逐字节一致)。"""
    if snap.kind == "files":
        rec = {"type": "files", "content": list(snap.content),
               "pinned": False, "ts": snap.ts}
    elif snap.kind == "image":
        c = snap.content or {}
        rec = {"type": "image", "content": c["path"], "pinned": False,
               "w": c.get("w", 0), "h": c.get("h", 0),
               "hash": c.get("hash", ""), "ts": snap.ts}
    else:
        rec = {"type": "text", "content": snap.content,
               "pinned": False, "ts": snap.ts}
    if snap.src:
        rec["src"] = snap.src
    rec.update(snap.rich)
    return rec


@dataclass
class CaptureTrace:
    """一次捕获的阶段轨迹:stage → 结论/原因,可读化打印。"""
    stages: List[Tuple[str, str]] = field(default_factory=list)

    def add(self, stage: str, detail: str):
        self.stages.append((stage, str(detail)))

    def __str__(self):
        return "\n".join("  %-12s %s" % (s + ":", d) for s, d in self.stages)


@dataclass
class Outcome:
    kind: str                    # SENSITIVE / DUPLICATE / PINNED_SKIP / KEEP
    index: Optional[int] = None  # DUPLICATE:历史中命中条目的下标
    key: object = None           # 去重键(排障用)


def run_capture(snap: Snapshot, cfg: dict, history: list,
                sensitive_probe: Optional[Callable[[str], Optional[str]]] = None,
                badge_probe: Optional[Callable[[str], Optional[str]]] = None,
                sha1_fallback: Optional[Callable] = None):
    """纯决策管线:输入快照与当前状态(只读),输出 (Outcome, record, trace)。
    不修改 history / cfg / 任何全局状态。"""
    trace = CaptureTrace()
    trace.add("snapshot", "%s src=%r rich=%s" % (snap.kind, snap.src,
                                                 sorted(snap.rich) if snap.rich else "-"))
    # ---- 阶段 1:敏感探测(先于去重:重复复制的敏感文本也必须隔离)----
    if snap.kind == "text":
        guard_on = bool(cfg.get("sensitive_guard", True))
        reason = sensitive_probe(snap.content) if (guard_on and sensitive_probe) \
            else None
        if not guard_on:
            trace.add("sensitive", "guard 已关闭(跳过探测)")
        elif reason:
            trace.add("sensitive", "命中 → 私密区(%s)" % reason)
            return Outcome(SENSITIVE), None, trace
        else:
            trace.add("sensitive", "未命中")
    # ---- 阶段 2:指纹 + 去重(纯读扫描,返回决策)----
    key = fingerprint(snap)
    trace.add("fingerprint", repr(key)[:80])
    for i, h in enumerate(history):
        if fingerprint_of_rec(h, sha1_fallback) == key:
            if h.get("pinned"):
                trace.add("dedup", "命中置顶条目 #%d,跳过" % i)
                return Outcome(PINNED_SKIP, index=i, key=key), None, trace
            trace.add("dedup", "命中 #%d,提升到最前(替换式,新 ts)" % i)
            return Outcome(DUPLICATE, index=i, key=key), make_record(snap), trace
    trace.add("dedup", "无重复,入库")
    # ---- 阶段 3:内容分类(轻量徽章;只进 trace,不写入持久化记录)----
    record = make_record(snap)
    if badge_probe is not None and snap.kind == "text":
        badge = badge_probe(snap.content)
        trace.add("classify", "徽章 %s" % badge if badge else "无子类型徽章")
    return Outcome(KEEP, key=key), record, trace
