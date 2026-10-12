# -*- coding: utf-8 -*-
"""普通历史容器与模式切换助手(阶段 8 拆分,自 clipboard_manager.py 迁出,
行为不变):JSON/DPAPI 容器解析、备份链回退读取、加密/明文原子落盘、
加密切换后的明文残留清理与旧库移除。路径常量经 _cm() 回查 cm。
"""
import os
import json
import base64
import traceback
from pathlib import Path

def _cm():
    """运行期回查 cm 模块(路径常量/测试打桩的解析点)。"""
    import clipboard_manager as cm
    return cm


def log(msg):
    """日志出口经 cm.log(同一文件,测试改写 LOG_FILE 后仍同一路径)。"""
    _cm().log(msg)

def _history_from_container(data):
    """历史容器解包:旧版明文 list,或新版 {"dpapi":bool,"data":<b64>} 整体
    DPAPI 加密(与私密区同一机制);解析/解密失败返回 None(交给上层试备份)。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        try:
            blob = base64.b64decode(data["data"])
            if data.get("dpapi"):
                blob = _cm()._dpapi_unprotect(blob)
                if blob is None:
                    log("history.json 解密失败(DPAPI),尝试备份恢复")
                    return None
            out = json.loads(blob.decode("utf-8"))
            return out if isinstance(out, list) else None
        except Exception:
            return None
    return None


def load_history(strict=False):
    # 主文件损坏时依次回退到最近备份(.bak1/.bak2/.bak3);明文/加密两种
    # 格式都会被 _history_from_container 识别(备份里两种混存也各自可用)
    found = False
    for fp in (_cm().HISTORY_FILE, _cm().HISTORY_FILE + ".bak1",
               _cm().HISTORY_FILE + ".bak2", _cm().HISTORY_FILE + ".bak3"):
        found = found or os.path.exists(fp)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = _history_from_container(json.load(f))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        # 清理文件已丢失的图片/文件条目
        cleaned = []
        for h in data:
            if not isinstance(h, dict):
                continue
            if h.get("type") == "image":
                if not os.path.exists(h.get("content", "")):
                    continue
            elif h.get("type") == "files":
                if not any(os.path.exists(p) for p in h.get("content", [])):
                    continue
            cleaned.append(h)
        if fp != _cm().HISTORY_FILE:
            log("history.json 不可用,已从备份恢复: %s" % os.path.basename(fp))
        return cleaned
    if strict and found:
        raise OSError("历史文件及备份均无法读取，保留原文件，停止转换存储")
    return []


def _rotate_history_backup():
    """滚动备份:旧 history.json -> .bak1 -> .bak2 -> .bak3(最多保留 3 份)。"""
    try:
        if not os.path.exists(_cm().HISTORY_FILE):
            return
        for i in (2, 1):
            src = "%s.bak%d" % (_cm().HISTORY_FILE, i)
            dst = "%s.bak%d" % (_cm().HISTORY_FILE, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(_cm().HISTORY_FILE, _cm().HISTORY_FILE + ".bak1")
    except Exception:
        pass


def save_history(hist, encrypt=False):
    """原子写 + 滚动备份。encrypt=True 时整体 DPAPI 加密成
    {"dpapi":true,"data":<b64>} 容器(与私密区同一机制,绑定当前用户);
    读取两种格式自动识别(load_history)。失败返回 False；加密失败不写明文。"""
    payload = hist
    if encrypt:
        blob = _cm()._dpapi_protect(json.dumps(hist, ensure_ascii=False).encode("utf-8"))
        if blob is None:
            log("历史加密失败，保留原存储")
            return False
        else:
            payload = {"dpapi": True,
                       "data": base64.b64encode(blob).decode("ascii")}
    tmp = _cm().HISTORY_FILE + ".tmp"
    try:
        Path(tmp).write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
        _rotate_history_backup()
        os.replace(tmp, _cm().HISTORY_FILE)
        return True
    except Exception:
        log("历史写入失败:\n%s" % traceback.format_exc())
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def _encrypt_history_residuals():
    """旧 JSON 备份/迁移副本原子转换为密文；失败时保留原副本并报错。"""
    for suffix in (".bak1", ".bak2", ".bak3", ".migrated", ".tmp"):
        path = Path(_cm().HISTORY_FILE + suffix)
        if not path.exists():
            continue
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("dpapi") is True:
                continue
        except (ValueError, UnicodeError):
            pass  # 损坏的备份仍保留原字节，不能因格式错误留下明文。
        blob = _cm()._dpapi_protect(raw)
        if blob is None:
            raise OSError("历史副本加密失败，保留原副本")
        tmp = path.with_name(path.name + ".encrypting")
        try:
            tmp.write_text(json.dumps({"dpapi": True,
                                       "data": base64.b64encode(blob).decode("ascii")}),
                           encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def _remove_history_database():
    """密文与模式配置落盘、数据库关闭后，移除明文库及 SQLite 边车。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(_cm().DB_FILE + suffix)
        if path.exists():
            path.unlink()


# ---- 私密区 DPAPI 加密(绑定当前 Windows 用户账号,同机其他用户/复制文件均不可读) ----

