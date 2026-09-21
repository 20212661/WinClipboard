# -*- coding: utf-8 -*-
"""诊断快照 + 数据健康检查(建议 19/29)—— 纯数据收集与校验,无 UI。

分工:
- collect_diagnostics(core) → dict:轻量快照(版本/条目/存储/监听状态),
  诊断页即时展示与「复制诊断信息」共用;全部为元数据,不含任何剪贴板正文
  或密钥(ai_services 只取 name)。
- run_health_check(core) → dict:深度校验,手动触发——数据库完整性、
  引用 blob 存在性、孤儿文件、私密区可解密、备份可读、schema 版本。
  只读不修;修复动作(重建索引/清理孤儿)由调用方另行执行。

依赖方向:diagnostics → clipboard_manager/store(单向);不反过来被依赖。
"""
import json
import os
import platform
import sys
import time
from pathlib import Path

import clipboard_manager as cm

# 孤儿判定的新文件时间窗:与 cleanup_orphan_images(min_age=120)同一规则,
# 避免把捕获线程正在落盘的文件误报为孤儿。
ORPHAN_MIN_AGE = 120.0


def _dir_stats(path):
    """目录内文件数与总字节(walk;目录不存在返回 (0, 0))。"""
    n = nbytes = 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            try:
                fp = os.path.join(root, f)
                nbytes += os.path.getsize(fp)
                n += 1
            except Exception:
                pass
    return n, nbytes


def _file_size(path):
    try:
        return os.path.getsize(str(path))
    except Exception:
        return 0


def collect_diagnostics(core):
    """轻量快照:全部为元数据,可安全复制/粘贴给问题反馈。"""
    store = core.store
    pinned = 0
    if store is not None:
        pinned = len(store.query(pinned=True))
    else:
        pinned = sum(1 for h in core.history if h.get("pinned"))
    blob_files, blob_bytes = _dir_stats(cm.BLOB_DIR)
    img_files, img_bytes = _dir_stats(cm.IMAGE_DIR)
    if getattr(core, "safe_mode", False):
        watcher = "安全模式(未启动)"
    elif callable(getattr(getattr(core, "worker", None), "is_alive", None)) \
            and core.worker.is_alive():
        watcher = "运行中"
    else:
        watcher = "未运行"
    return {
        "app_version": cm.APP_VERSION,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "os": platform.platform(),
        "store_mode": store is not None,
        "encrypt_history": bool(core.cfg.get("encrypt_history")),
        "schema_version": store._user_version() if store is not None else None,
        "items": store.count() if store is not None else len(core.history),
        "pinned": pinned,
        "deleted_pending": store.deleted_count() if store is not None else 0,
        "private": len(core.private),
        "db_bytes": _file_size(cm.DB_FILE),
        "blob_files": blob_files,
        "blob_bytes": blob_bytes,
        "image_files": img_files,
        "image_bytes": img_bytes,
        "watcher": watcher,
        "monitor_paused": bool(getattr(core, "monitor_paused", False)),
        "ai_services": [str(s.get("name") or "?").strip()
                        for s in (core.cfg.get("ai_services") or [])
                        if isinstance(s, dict)],
        "log_file": cm.LOG_FILE,
    }


def format_diagnostics(snap):
    """快照 → 复制用文本(中文键;顺序即展示顺序)。"""
    store_txt = "SQLite(clipboard.db)" if snap["store_mode"] \
        else "JSON(history.json)"
    ai_txt = "、".join(snap["ai_services"]) if snap["ai_services"] else "未配置"
    lines = [
        "WinClipboard 诊断信息",
        "版本: %s" % snap["app_version"],
        "运行环境: Python %s / %s" % (snap["python"], snap["os"]),
        "存储模式: %s%s" % (store_txt,
                            "(加密)" if snap["encrypt_history"] else ""),
        "条目: %d(置顶 %d,待回收 %d,私密区 %d)" % (
            snap["items"], snap["pinned"], snap["deleted_pending"],
            snap["private"]),
        "存储占用: 数据库 %s / 图片 blob %d 个 %s / 旧图目录 %d 个 %s" % (
            cm._fmt_size(snap["db_bytes"]), snap["blob_files"],
            cm._fmt_size(snap["blob_bytes"]), snap["image_files"],
            cm._fmt_size(snap["image_bytes"])),
        "剪贴板监听: %s" % snap["watcher"],
        "AI 服务: %s" % ai_txt,
        "日志: %s" % snap["log_file"],
    ]
    if snap.get("schema_version") is not None:
        lines.insert(3, "schema 版本: v%s" % snap["schema_version"])
    return "\n".join(lines)


def _image_refs(core):
    """图片引用集合:(blob_hash, 记录路径) 列表,库模式取全部行(含
    deleted 态——宽限期内撤销仍要求 blob 在),JSON 模式取内存历史。"""
    if core.store is not None:
        return core.store.image_blob_refs()
    refs = []
    for h in core.history:
        if h.get("type") == "image":
            refs.append((str(h.get("hash") or ""), str(h.get("content") or "")))
    return refs


def _blob_candidates(hsh, path):
    """一个引用可能的落位:记录路径(绝对)→ 新布局分片 → 旧布局 images/。"""
    out = []
    if path:
        out.append(path)
    if hsh:
        p = cm.blob_path(hsh)
        if p:
            out.append(p)
            out.append(os.path.join(cm.IMAGE_DIR, "img_%s" % hsh))
    return out


def _check_missing_blobs(refs):
    missing = []
    for hsh, path in refs:
        if not any(os.path.exists(c) for c in _blob_candidates(hsh, path)):
            missing.append(hsh or path)
    return missing


def _find_orphans(refs, min_age=ORPHAN_MIN_AGE):
    """未被任何行引用、且早于时间窗的 blob/图片文件(只清点,不删除)。"""
    hashes = {h.lower() for h, _p in refs if h}
    paths = {os.path.normcase(os.path.abspath(p)) for _h, p in refs if p}
    cutoff = time.time() - min_age
    orphans = []
    for base in (cm.BLOB_DIR, cm.IMAGE_DIR):
        in_legacy = os.path.normcase(str(base)) == \
            os.path.normcase(str(cm.IMAGE_DIR))
        for root, _dirs, files in os.walk(str(base)):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if os.path.getmtime(fp) > cutoff:
                        continue                        # 时间窗内:可能正在落盘
                    if os.path.normcase(os.path.abspath(fp)) in paths:
                        continue                        # 记录路径直接引用
                    stem = f.lower()
                    if in_legacy and stem.startswith("img_"):
                        stem = stem[4:]
                        if stem.endswith(".png"):
                            stem = stem[:-4]
                        if stem in hashes:
                            continue                    # 旧布局 img_<hash>.png
                    elif not in_legacy and stem in hashes:
                        continue                        # 新布局文件名即哈希
                    orphans.append(fp)
                except Exception:
                    pass
    return orphans


def _check_private_readable():
    """私密区可解密:文件缺失=未启用;旧版明文列表=直接可读;
    加密容器=解密并解析成功才算通过(与 load_private 同一套原语)。"""
    if not os.path.exists(cm.PRIVATE_FILE):
        return True, "未启用(无私密数据)"
    try:
        data = json.loads(Path(cm.PRIVATE_FILE).read_bytes().decode("utf-8"))
    except Exception as e:
        return False, "private.json 无法解析: %s" % e.__class__.__name__
    if isinstance(data, list):
        return True, "明文旧版,共 %d 条" % len(data)
    if isinstance(data, dict) and "data" in data:
        import base64
        try:
            blob = base64.b64decode(data["data"])
            if data.get("dpapi"):
                blob = cm._dpapi_unprotect(blob)
                if blob is None:
                    return False, "DPAPI 解密失败(原文件已保留)"
            n = len(json.loads(blob.decode("utf-8")))
            return True, "加密容器可解密,共 %d 条" % n
        except Exception:
            return False, "加密容器解析失败"
    return False, "未知格式"


def _check_backups_readable():
    """history.json 滚动备份(.bak1~3)容器可解析(JSON 层;内容是否加密
    不影响可读性判定)。"""
    ok = missing = bad = 0
    for i in (1, 2, 3):
        fp = "%s.bak%d" % (cm.HISTORY_FILE, i)
        if not os.path.exists(fp):
            missing += 1
            continue
        try:
            json.loads(Path(fp).read_bytes().decode("utf-8"))
            ok += 1
        except Exception:
            bad += 1
    if bad:
        return False, "%d 份备份损坏(可读 %d 份)" % (bad, ok)
    if ok == 0:
        return True, "暂无备份(首次运行或未写入)"
    return True, "%d 份备份可读" % ok


def run_health_check(core):
    """深度校验(只读)。返回 {ok, results:[{name, ok, detail}], checked_n}。"""
    results = []

    def add(name, ok, detail):
        results.append({"name": name, "ok": ok, "detail": detail})

    if core.store is not None:
        verdict = core.store.integrity_check()
        add("数据库完整性", verdict == "ok",
            "PRAGMA integrity_check: %s" % verdict)
        ver = core.store._user_version()
        add("schema 版本", ver <= _supported_schema(), "v%d(支持至 v%d)"
            % (ver, _supported_schema()))
    else:
        add("数据库完整性", None, "JSON 模式,无数据库")

    refs = _image_refs(core)
    missing = _check_missing_blobs(refs)
    add("引用 blob 存在性", not missing,
        "全部存在" if not missing else "缺少 %d 个: %s" % (
            len(missing), ", ".join(m[:12] for m in missing[:5])))

    orphans = _find_orphans(refs)
    orph_bytes = sum(_file_size(p) for p in orphans)
    add("孤儿文件", not orphans,
        "无孤儿" if not orphans else "%d 个,约 %s(可在诊断页清理)"
        % (len(orphans), cm._fmt_size(orph_bytes)))

    ok, detail = _check_private_readable()
    add("私密区可解密", ok, detail)
    ok, detail = _check_backups_readable()
    add("备份可读", ok, detail)

    hard = [r for r in results if r["ok"] is False]
    return {"ok": not hard, "results": results, "checked": len(results)}


def _supported_schema():
    import store
    return store.SCHEMA_VERSION
