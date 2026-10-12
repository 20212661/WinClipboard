# -*- coding: utf-8 -*-
"""上下文包服务门面(纯,无 Qt):增删改查、活动包、快照入包、拼装。

敏感探测可注入(默认不拦,宿主接 cm._sensitive_reason);DPAPI 原语
经 store 走 cm 路由(测试打桩语义与 privacy_store 一致)。
任何变更成功后**自动落盘**;保存失败保留在 last_save_error 供 UI
如实呈现(内存态保留、待重试,绝不静默丢)。语义要点:

- unreadable 状态下禁止自动覆写(与私密区同规则),只有宿主显式调用
  destructive_clear(「清空全部数据」路径)才允许动原文件;
- 快照与历史解耦:清空历史/容量淘汰不影响包(独立文件独立资产);
- origin_fp 只用于「该条已在包内」提示,不作引用(允许重复快照)。
"""
import time
from collections import namedtuple
from typing import Callable, List, Optional

from . import store
from .assemble import Assembled, assemble
from .budget import BudgetError, refresh_estimates
from .model import (ContextItem, ContextPack, clamp_budget, make_item,
                    new_pack)

AddResult = namedtuple("AddResult", "ok item reason dup_of")
# reason: "" 成功 / "sensitive:<类别>" 被敏感规则拦下(可确认放行) /
#         "no_pack" 无包 / "save_failed:<错误>" 已入内存但未落盘


class ContextPackService:

    def __init__(self, path: str, sensitive_check: Optional[Callable[[str], object]] = None):
        self.path = path
        self.packs: List[ContextPack] = []
        self.active_pid: Optional[str] = None
        self.status = "missing"         # ok / missing / recovered / unreadable
        self.mode = "none"              # dpapi / plaintext / none
        self.load_source = ""
        self.load_error = ""
        self.last_save_error = ""
        self.encrypt = False            # 宿主按 cfg["encrypt_history"] 同步
        self._sensitive_check = sensitive_check

    # ---- 加载/保存 ----

    def load(self) -> "ContextPackService":
        r = store.load_packs_status(self.path)
        self.packs, self.active_pid = r.packs, r.active_pid
        self.status, self.mode = r.status, r.mode
        self.load_source, self.load_error = r.source, r.error
        if self.active_pid and not self.get(self.active_pid):
            self.active_pid = None      # 活动包指向已不存在的包:置空
        return self

    def save(self):
        """落盘;unreadable 状态拒绝自动覆写(数据仍在原文件,待修复)。"""
        if self.status == "unreadable":
            self.last_save_error = "包文件不可读,本会话不覆写(数据仍在原文件)"
            return store.SaveResult(False, self.encrypt, self.last_save_error)
        res = store.save_packs(self.path, self.packs, self.active_pid,
                               self.encrypt)
        self.last_save_error = "" if res.ok else res.error
        if res.ok:
            self.status, self.mode = "ok", ("dpapi" if self.encrypt else "plaintext")
        return res

    def set_encrypt(self, on: bool):
        """宿主在「历史加密」开关切换后调用(migrate 由 cm 钩子做,这里只对齐)。"""
        self.encrypt = bool(on)

    def destructive_clear(self):
        """「清空全部数据」路径:删除文件与备份/留档,内存清空。"""
        store.wipe_packs(self.path)
        self.packs, self.active_pid = [], None
        self.status, self.mode, self.last_save_error = "missing", "none", ""

    # ---- 包级操作 ----

    def list_packs(self) -> List[ContextPack]:
        return list(self.packs)

    def get(self, pid: str) -> Optional[ContextPack]:
        for p in self.packs:
            if p.pid == pid:
                return p
        return None

    def _pack_or_active(self, pid: Optional[str]) -> Optional[ContextPack]:
        return self.get(pid) if pid else self.active_pack()

    def create(self, name: str, budget_tokens: int = 4000,
               preamble: str = "", activate: bool = True) -> ContextPack:
        p = new_pack(name, budget_tokens=budget_tokens, preamble=preamble)
        self.packs.append(p)
        if activate or self.active_pid is None:
            self.active_pid = p.pid
        self._touch(p)
        return p

    def rename(self, pid: str, name: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        p.name = (str(name or "")).strip() or p.name
        self._touch(p)
        return True

    def delete(self, pid: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        self.packs.remove(p)
        if self.active_pid == pid:
            self.active_pid = self.packs[0].pid if self.packs else None
        self._save_after_mutation()
        return True

    def set_active(self, pid: str) -> bool:
        if pid is not None and self.get(pid) is None:
            return False
        self.active_pid = pid
        self._save_after_mutation()
        return True

    def active_pack(self) -> Optional[ContextPack]:
        return self.get(self.active_pid) if self.active_pid else None

    # ---- 条目级操作 ----

    def add_snapshot(self, kind: str, content: str, pid: Optional[str] = None,
                     title: Optional[str] = None, src_app: str = "",
                     captured_at: Optional[float] = None,
                     origin_fp: str = "", role: str = "reference",
                     priority: int = 5,
                     allow_sensitive: bool = False) -> AddResult:
        """内容快照入包。敏感命中默认拦截(reason 带类别),确认放行后
        (allow_sensitive=True)入包并置 sensitive=True。"""
        p = self._pack_or_active(pid)
        if p is None:
            return AddResult(False, None, "no_pack", None)
        hit = self._sensitive_check(content) if self._sensitive_check is not None else None
        if hit and not allow_sensitive:
            return AddResult(False, None, "sensitive:%s" % hit, None)
        it = make_item(kind, content, title=title, src_app=src_app,
                       captured_at=captured_at, origin_fp=origin_fp,
                       role=role, priority=priority, sensitive=bool(hit))
        refresh_estimates([it])
        dup = next((x for x in p.items
                    if origin_fp and x.origin_fp == origin_fp), None)
        p.items.append(it)
        self._touch(p)
        return AddResult(True, it, "", dup.iid if dup else None)

    def remove_item(self, pid: str, iid: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        for it in p.items:
            if it.iid == iid:
                p.items.remove(it)
                self._touch(p)
                return True
        return False

    def update_item(self, pid: str, iid: str, **changes) -> bool:
        """可改:title / role / priority / enabled / sensitive / content /
        kind。content 变更重算 est_tokens。未知字段忽略。"""
        p = self.get(pid)
        if p is None:
            return False
        it = next((x for x in p.items if x.iid == iid), None)
        if it is None:
            return False
        if "title" in changes and str(changes["title"]).strip():
            it.title = str(changes["title"])[:100]
        if "role" in changes:
            it.role = changes["role"] if changes["role"] in ("reference",
                                                             "instruction",
                                                             "example") else it.role
        if "priority" in changes:
            try:
                it.priority = max(0, min(9, int(changes["priority"])))
            except (TypeError, ValueError):
                pass
        for flag in ("enabled", "sensitive"):
            if flag in changes:
                setattr(it, flag, bool(changes[flag]))
        if "kind" in changes and changes["kind"] in (
                "text", "code", "json", "markdown", "csv", "url",
                "file_list", "image_ref"):
            it.kind = changes["kind"]
        if "content" in changes and isinstance(changes["content"], str):
            it.content = changes["content"]
            refresh_estimates([it])
        self._touch(p)
        return True

    def move_item(self, pid: str, iid: str, to_index: int) -> bool:
        """包内排序(拖拽)。"""
        p = self.get(pid)
        if p is None:
            return False
        idx = next((i for i, x in enumerate(p.items) if x.iid == iid), None)
        if idx is None:
            return False
        to = max(0, min(len(p.items) - 1, int(to_index)))
        p.items.insert(to, p.items.pop(idx))
        self._touch(p)
        return True

    # ---- 拼装 ----

    def assemble_active(self, question: Optional[str] = None,
                        fmt: str = "xml",
                        include_sensitive: bool = False) -> Optional[Assembled]:
        """拼装活动包(隐私闸确认的发送路径才传 include_sensitive=True)。

        无活动包返回 None;指令条目超预算抛 BudgetError(调用方如实呈现)。"""
        p = self.active_pack()
        if p is None:
            return None
        return assemble(p, fmt=fmt, question=question,
                        include_sensitive=include_sensitive)

    # ---- 内部 ----

    def _touch(self, p: ContextPack):
        p.updated = time.time()
        self._save_after_mutation()

    def _save_after_mutation(self):
        res = self.save()
        if not res.ok:
            # 内存态保留待重试;错误留在 last_save_error,由 UI 如实呈现
            store.log("上下文包变更未落盘:%s" % res.error)
        return res


def snapshot_args_from_record(rec: dict, sha1_fallback=None) -> dict:
    """历史记录 dict → add_snapshot 的关键字参数(纯映射,pipeline 延迟导入)。

    文本子类型映射到 ContextItem.kind(jwt/base64/xml 归 text);
    图片只存 image_ref 元信息(标题+尺寸),不存像素。"""
    from pipeline import fingerprint_of_rec
    t = rec.get("type")
    subtype = rec.get("type_override")
    src = rec.get("src") or rec.get("src_app") or ""
    ts = float(rec.get("ts") or 0.0)
    if t == "files":
        paths = [str(x) for x in (rec.get("content") or [])]
        return {"kind": "file_list",
                "content": "\n".join(paths),
                "title": (paths[0].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                          if paths else "[文件列表]"),
                "src_app": src, "captured_at": ts,
                "origin_fp": repr(fingerprint_of_rec(rec, sha1_fallback))}
    if t == "image":
        meta = rec.get("content") or {}
        w, h = meta.get("w", "?"), meta.get("h", "?")
        path = meta.get("path") or ""
        name = (path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                if path else "图片")
        return {"kind": "image_ref",
                "content": "%s × %s%s" % (w, h, (" · " + name) if name else ""),
                "title": "%s(%s×%s)" % (name, w, h),
                "src_app": src, "captured_at": ts,
                "origin_fp": repr(fingerprint_of_rec(rec, sha1_fallback))}
    kind_map = {"json": "json", "url": "url", "markdown": "markdown",
                "code": "code", "csv": "csv"}
    text = rec.get("content") if isinstance(rec.get("content"), str) else ""
    return {"kind": kind_map.get(subtype, "text"),
            "content": text,
            "src_app": src, "captured_at": ts,
            "origin_fp": repr(fingerprint_of_rec(rec, sha1_fallback))}
