# -*- coding: utf-8 -*-
"""上下文包数据模型(纯 dataclass,无 IO、无 Qt)。

快照而非引用:条目在"加入"瞬间固化内容与指纹,之后与历史解耦——
清空历史、容量淘汰、切换加密模式都不会让包内条目失效(与图钉画布
"画布为独立资产"同构)。图片只存 image_ref 元信息,不进拼装文本。
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ContextItem.kind 词表(固定;接入映射时其它子类型归 text)
ITEM_KINDS = ("text", "code", "json", "markdown", "csv", "url",
              "file_list", "image_ref")
ITEM_ROLES = ("reference", "instruction", "example")

MAX_ITEM_CONTENT = 64 * 1024        # 单条内容快照上限(字符),超限截断入库
TITLE_MAX = 40                      # 默认标题取首行前 40 字
DEFAULT_BUDGET_TOKENS = 4000
MIN_BUDGET_TOKENS = 500
MAX_BUDGET_TOKENS = 128000
PACK_VERSION = 1                    # 存储格式版本


def clamp_budget(value: Any, fallback: int = DEFAULT_BUDGET_TOKENS) -> int:
    """预算值收敛到 [MIN, MAX];非法值回退默认。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(MIN_BUDGET_TOKENS, min(MAX_BUDGET_TOKENS, v))


def default_title(kind: str, content: str) -> str:
    """默认标题:首条非空行去空白截 40 字;无内容按类型兜底。"""
    for line in (content or "").splitlines():
        s = line.strip()
        if s:
            return s[:TITLE_MAX]
    return {"image_ref": "[图片]", "file_list": "[文件列表]"}.get(kind, "[空]")


@dataclass
class ContextItem:
    iid: str
    kind: str
    title: str
    content: str
    src_app: str = ""
    captured_at: float = 0.0
    origin_fp: str = ""              # 来源指纹(仅用于"已在包内"去重提示)
    role: str = "reference"
    enabled: bool = True
    priority: int = 5                # 0-9,预算紧张时高者优先保留
    sensitive: bool = False          # 入包时由敏感探测置位(已含用户放行语义)
    est_tokens: int = 0              # 缓存:内容变更时由 service 重算
    truncated_at_ingest: bool = False  # 入包时超 64KB 截断标记

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iid": self.iid, "kind": self.kind, "title": self.title,
            "content": self.content, "src_app": self.src_app,
            "captured_at": self.captured_at, "origin_fp": self.origin_fp,
            "role": self.role, "enabled": self.enabled,
            "priority": self.priority, "sensitive": self.sensitive,
            "est_tokens": self.est_tokens,
            "truncated_at_ingest": self.truncated_at_ingest,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContextItem":
        # 版本容忍:未知字段忽略,缺失字段用默认(存储迁移的读取侧)
        return cls(
            iid=str(data.get("iid") or uuid.uuid4().hex),
            kind=_safe_kind(data.get("kind")),
            title=str(data.get("title") or "")[:TITLE_MAX + 60],
            content=data.get("content") if isinstance(data.get("content"), str) else "",
            src_app=str(data.get("src_app") or ""),
            captured_at=float(data.get("captured_at") or 0.0),
            origin_fp=str(data.get("origin_fp") or ""),
            role=_safe_role(data.get("role")),
            enabled=bool(data.get("enabled", True)),
            priority=max(0, min(9, int(data.get("priority") or 5))),
            sensitive=bool(data.get("sensitive", False)),
            est_tokens=max(0, int(data.get("est_tokens") or 0)),
            truncated_at_ingest=bool(data.get("truncated_at_ingest", False)),
        )


@dataclass
class ContextPack:
    pid: str
    name: str
    created: float = 0.0
    updated: float = 0.0
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    preamble: str = ""               # 包级前言/系统指令(可空)
    items: List[ContextItem] = field(default_factory=list)
    version: int = PACK_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid, "name": self.name,
            "created": self.created, "updated": self.updated,
            "budget_tokens": self.budget_tokens, "preamble": self.preamble,
            "items": [it.to_dict() for it in self.items],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContextPack":
        return cls(
            pid=str(data.get("pid") or uuid.uuid4().hex),
            name=str(data.get("name") or "未命名"),
            created=float(data.get("created") or 0.0),
            updated=float(data.get("updated") or 0.0),
            budget_tokens=clamp_budget(data.get("budget_tokens")),
            preamble=data.get("preamble") if isinstance(data.get("preamble"), str) else "",
            items=[ContextItem.from_dict(d)
                   for d in (data.get("items") or []) if isinstance(d, dict)],
            version=int(data.get("version") or PACK_VERSION),
        )


def _safe_kind(value: Any) -> str:
    k = str(value or "text")
    return k if k in ITEM_KINDS else "text"


def _safe_role(value: Any) -> str:
    r = str(value or "reference")
    return r if r in ITEM_ROLES else "reference"


def make_item(kind: str, content: str, title: Optional[str] = None,
              src_app: str = "", captured_at: Optional[float] = None,
              origin_fp: str = "", role: str = "reference",
              priority: int = 5, sensitive: bool = False,
              enabled: bool = True) -> ContextItem:
    """构造条目并做入包规范化:kind/role 归一、标题默认、64KB 截断、
    优先级钳制。est_tokens 由调用方(service)用估算器刷新。"""
    kind = _safe_kind(kind)
    role = _safe_role(role)
    content = content if isinstance(content, str) else str(content or "")
    truncated = False
    if len(content) > MAX_ITEM_CONTENT:
        content = content[:MAX_ITEM_CONTENT]
        truncated = True
    if title is None or not str(title).strip():
        title = default_title(kind, content)
    return ContextItem(
        iid=uuid.uuid4().hex,
        kind=kind,
        title=str(title)[:TITLE_MAX + 60],
        content=content,
        src_app=src_app or "",
        captured_at=float(captured_at if captured_at is not None else time.time()),
        origin_fp=origin_fp or "",
        role=role,
        enabled=bool(enabled),
        priority=max(0, min(9, int(priority))),
        sensitive=bool(sensitive),
        est_tokens=0,
        truncated_at_ingest=truncated,
    )


def new_pack(name: str, budget_tokens: int = DEFAULT_BUDGET_TOKENS,
             preamble: str = "") -> ContextPack:
    now = time.time()
    return ContextPack(
        pid=uuid.uuid4().hex,
        name=(str(name or "未命名")).strip() or "未命名",
        created=now, updated=now,
        budget_tokens=clamp_budget(budget_tokens),
        preamble=preamble or "",
        items=[],
        version=PACK_VERSION,
    )
