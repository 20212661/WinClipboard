# -*- coding: utf-8 -*-
"""上下文包持久化:独立文件 context_packs.json(一个文件装所有包)。

独立文件的原因:绕开 store.py schema(避免与 cas 分支三方冲突);
数据量小(几十包 × 几十条)JSON 足够。写入语义与私密区完全一致
(privacy_store 已验证模式):临时文件 → 备份轮转 → 原子替换;
任何一步失败保留原文件;DPAPI 失败不降级明文;损坏 → .corrupt 留档、
禁止自动覆写;旧明文不进备份链;加密跟随「历史加密」开关并同步迁移。

与指导文档的一处偏差:活动包 id 存在包文件自身(随包一起加密/
迁移/备份),不写 config.json——少一次配置写盘,避免两处事实源。
"""
import base64
import json
import os
from collections import namedtuple
from pathlib import Path

FILE_VERSION = 1                     # 文件布局版本(顶层 ctxpack_version)
CTXPACK_BACKUPS = 3

PackLoadResult = namedtuple("PackLoadResult",
                            "packs active_pid status mode source error")
SaveResult = namedtuple("SaveResult", "ok encrypted error")


def _cm():
    """运行期回查 cm 模块(DPAPI 原语/日志的打桩解析点,与 privacy_store 一致)。"""
    import clipboard_manager as cm
    return cm


def log(msg):
    try:
        _cm().log(msg)
    except Exception:
        pass


def _bak_paths(path):
    return tuple(path + ".bak%d" % i for i in range(1, CTXPACK_BACKUPS + 1))


def _parse_file(path):
    """读单个包文件 → (packs, active_pid, mode);不可解析/解密失败返回 None。

    错误信息不携带正文(调用方只记录通用摘要,与私密区同纪律)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "data" in data:      # 密封容器
        sealed_dpapi = bool(data.get("dpapi"))
        try:
            blob = base64.b64decode(data["data"])
            if sealed_dpapi:
                blob = _cm()._dpapi_unprotect(blob)
                if blob is None:
                    return None
            data = json.loads(blob.decode("utf-8"))
            mode = "dpapi" if sealed_dpapi else "plaintext"
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
    elif isinstance(data, dict):
        mode = "plaintext"
    else:
        return None
    from .model import ContextPack
    packs = [ContextPack.from_dict(d)
             for d in (data.get("packs") or []) if isinstance(d, dict)]
    active = data.get("active")
    return packs, (active if isinstance(active, str) else None), mode


def load_packs_status(path):
    """带状态读取(核心一律用这个):ok / missing / recovered / unreadable。"""
    got = _parse_file(path)
    if got is not None:
        return PackLoadResult(got[0], got[1], "ok", got[2], "", "")
    if not os.path.exists(path):
        return PackLoadResult([], None, "missing", "none", "", "")
    for bak in _bak_paths(path):
        got = _parse_file(bak)
        if got is not None:
            _quarantine_corrupt(path)
            log("context_packs.json 无法读取,已从 %s 恢复;损坏原件保留为 .corrupt"
                % os.path.basename(bak))
            return PackLoadResult(got[0], got[1], "recovered", got[2],
                                  os.path.basename(bak), "主文件损坏,已从备份恢复")
    log("context_packs.json 及全部备份均无法读取:原文件保留待修复,本会话不覆写")
    return PackLoadResult([], None, "unreadable", "none", "",
                          "主文件与备份均无法读取(可能解密失败)")


def _quarantine_corrupt(path):
    """损坏主文件改名留档(固定一个 .corrupt 名,重复启动不无限复制)。"""
    try:
        if os.path.exists(path):
            os.replace(path, path + ".corrupt")
    except Exception:
        pass


def _rotate_backups(path):
    """把当前**有效加密**主文件轮转入备份链(bak3 删,bak2→3,bak1→2,主→bak1)。

    损坏主文件与明文主文件不进备份链(与私密区同规则:前者无有效内容,
    后者会把明文残留进备份)。返回 (ok, 脱敏错误)。"""
    got = _parse_file(path)
    if got is None or got[2] != "dpapi":
        return True, ""
    try:
        baks = _bak_paths(path)
        if os.path.exists(baks[-1]):
            os.remove(baks[-1])
        for i in range(len(baks) - 1, 0, -1):
            if os.path.exists(baks[i - 1]):
                os.replace(baks[i - 1], baks[i])
        raw = Path(path).read_bytes()
        Path(baks[0]).write_bytes(raw)
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _write_payload(path, payload):
    """临时文件写入(独立函数 = 测试故障注入点)。"""
    Path(path).write_text(payload, encoding="utf-8")


def _cleanup_tmp(tmp):
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass


def save_packs(path, packs, active_pid, encrypt):
    """写包文件:DPAPI 密封(encrypt=True)→ 临时文件 → 备份轮转 → 原子替换。

    任何一步失败保留原文件(可重试);DPAPI 失败**不降级明文**。"""
    inner = {"ctxpack_version": FILE_VERSION,
             "active": active_pid or None,
             "packs": [p.to_dict() for p in packs]}
    plain = json.dumps(inner, ensure_ascii=False)
    if encrypt:
        blob = _cm()._dpapi_protect(plain.encode("utf-8"))
        if blob is None:
            log("上下文包保存失败:DPAPI 保护不可用,原文件保持不变")
            return SaveResult(False, False, "DPAPI 保护不可用,原文件保持不变")
        payload = json.dumps({"dpapi": True,
                              "data": base64.b64encode(blob).decode("ascii")})
    else:
        payload = plain
    tmp = path + ".tmp"
    try:
        _write_payload(tmp, payload)
    except Exception as e:
        _cleanup_tmp(tmp)
        log("上下文包保存失败(临时文件):%s" % e)
        return SaveResult(False, encrypt, "临时文件写入失败:%s" % e)
    ok, err = _rotate_backups(path)
    if not ok:
        _cleanup_tmp(tmp)
        log("上下文包保存失败(备份轮转):%s" % err)
        return SaveResult(False, encrypt, "备份轮转失败:%s" % err)
    try:
        os.replace(tmp, path)
    except Exception as e:
        _cleanup_tmp(tmp)
        log("上下文包保存失败(替换主文件):%s" % e)
        return SaveResult(False, encrypt, "替换主文件失败:%s" % e)
    return SaveResult(True, encrypt, "")


def migrate_packs_file(path, encrypt):
    """加密模式切换时与历史同步迁移(决策 D1:跟随「历史加密」开关)。

    - encrypt=True:明文 → 密封;成功后清理明文残留(.corrupt/.tmp/.bak);
    - encrypt=False:密封 → 明文;加密备份链保留(可恢复),失败保留
      密封原文件(安全方向,可重试)。
    返回 (ok, 脱敏错误)。"""
    st = load_packs_status(path)
    if st.status == "unreadable":
        return False, "包文件不可读,不迁移(原文件保留)"
    if st.status == "missing":
        return True, ""                       # 没有包文件:无需迁移
    want = "dpapi" if encrypt else "plaintext"
    if st.mode == want:
        return True, ""                       # 已是目标模式:不重写
    res = save_packs(path, st.packs, st.active_pid, encrypt)
    if not res.ok:
        return False, res.error
    if encrypt:
        # 明文残留清理:留档/临时/备份链兜底清空(明文本就不进链,
        # 这里清的是历史遗留;解密方向保留加密备份供恢复)
        for leftover in (path + ".corrupt", path + ".tmp") + _bak_paths(path):
            try:
                if os.path.exists(leftover):
                    os.remove(leftover)
            except Exception:
                pass
    return True, ""


def wipe_packs(path):
    """破坏性清空(设置页「清空全部数据」):主文件 + 备份链 + 留档全删。

    唯一允许覆写/删除不可读原件的路径(用户显式清空)。"""
    for p in (path,) + _bak_paths(path) + (path + ".corrupt", path + ".tmp"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def merge_packs_payload(path, data, encrypt):
    """导入可迁移备份:按 pid 合并(已存在的 pid 保留本地版本)。

    导入方的活动包不变;备份里的活动包只在本地没有活动包时采用。
    返回 (新增包数, SaveResult 或 None=无需写盘)。"""
    if not isinstance(data, dict) or not isinstance(data.get("packs"), list):
        return 0, None
    from .model import ContextPack
    st = load_packs_status(path)
    if st.status == "unreadable":
        return 0, SaveResult(False, False, "包文件不可读,导入中止(原文件保留)")
    known = {p.pid for p in st.packs}
    added = 0
    for d in data["packs"]:
        if not isinstance(d, dict):
            continue
        p = ContextPack.from_dict(d)
        if p.pid in known:
            continue
        st.packs.append(p)
        known.add(p.pid)
        added += 1
    if not added:
        return 0, None
    active = st.active_pid
    if not active:
        imp_active = data.get("active")
        active = imp_active if isinstance(imp_active, str) and \
            any(p.pid == imp_active for p in st.packs) else None
    res = save_packs(path, st.packs, active, encrypt)
    return added, res
