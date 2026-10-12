# -*- coding: utf-8 -*-
"""私密存储与 DPAPI 密封服务(阶段 8 拆分,自 clipboard_manager.py 迁出,
行为不变):DPAPI 原语、配置密钥字段封装(阶段 4 失败保留策略)、私密区
读取状态机(ok/missing/recovered/unreadable)、原子保存与 bak1~3 备份链、
损坏留档。路径常量与可打桩原语经 _cm() 回查 cm,测试语义不变。
"""
import os
import json
import base64
from collections import namedtuple
from pathlib import Path

try:
    import win32crypt   # 私密区 DPAPI 加密(pywin32 自带)
except ImportError:
    win32crypt = None

def _cm():
    """运行期回查 cm 模块(路径常量/测试打桩的解析点)。"""
    import clipboard_manager as cm
    return cm


def log(msg):
    """日志出口经 cm.log(同一文件,测试改写 LOG_FILE 后仍同一路径)。"""
    _cm().log(msg)

def _dec_field(value):
    """解密一个密文字段({"dpapi":bool,"data":<b64>});明文原样返回,解密失败返回 None。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "data" in value:
        try:
            blob = base64.b64decode(value["data"])
        except Exception:
            return None
        if value.get("dpapi"):
            blob = _dpapi_unprotect(blob)
            if blob is None:
                return None
        try:
            return blob.decode("utf-8")
        except Exception:
            return None
    return None


def _enc_field(text):
    """把明文字段封装为密文 dict;DPAPI 保护不可用/失败返回 None。

    不再降级为编码明文(阶段 4 策略):调用方按失败处理——保留原文件、
    不把秘密推进备份,并向用户如实说明未保存。"""
    raw = str(text).encode("utf-8")
    blob = _cm()._dpapi_protect(raw)
    if blob is None:
        log("密钥加密失败:DPAPI 保护不可用,按失败保留处理(不写明文)")
        return None
    return {"dpapi": True,
            "data": base64.b64encode(blob).decode("ascii")}

_DPAPI_ENTROPY = b"WinClipboard.private"


def _dpapi_protect(data):
    """DPAPI(CurrentUser 作用域)加密;win32crypt 不可用或失败返回 None。"""
    if win32crypt is None:
        return None
    try:
        out = win32crypt.CryptProtectData(data, None, _DPAPI_ENTROPY, None, None, 0)
        return out[-1] if isinstance(out, tuple) else out   # 兼容 (数据, 描述) 返回形式
    except Exception:
        return None


def _dpapi_unprotect(blob):
    if win32crypt is None:
        return None
    try:
        # 注意:此版 pywin32 的 CryptUnprotectData 无 DataDescr 参数,只收 5 个
        out = win32crypt.CryptUnprotectData(blob, _DPAPI_ENTROPY, None, None, 0)
        return out[-1] if isinstance(out, tuple) else out
    except Exception:
        return None


def dpapi_available():
    """当前环境能否用 DPAPI 加密(win32crypt 可用即 True)。前端据此如实
    标注私密区的存储强度,DPAPI 缺位时不得宣称“已加密”。"""
    return win32crypt is not None


# ---- 私密区存取(带状态;读取失败不覆写可恢复原件) ----
# 主文件 private.json;可读备份链 private.json.bak1~3(新→旧)。
# 状态语义:
#   ok          主文件正常(加密或旧明文,mode 区分)
#   missing     主文件不存在(全新环境)
#   recovered   主文件损坏,已从备份恢复;损坏原件改名 .corrupt 留档(仅一份)
#   unreadable  主文件与全部备份均无法读取:原文件原样保留,本会话禁止自动覆写,
#               只有用户显式的破坏性清空(设置页「清空全部数据」/私密区清空)
#               才允许覆盖不可读原文件
PrivateLoadResult = namedtuple("PrivateLoadResult",
                               "entries status mode source error")
PrivateSaveResult = namedtuple("PrivateSaveResult", "ok encrypted error")
PRIVATE_BACKUPS = 3


def _private_bak_paths():
    return tuple(_cm().PRIVATE_FILE + ".bak%d" % i
                 for i in range(1, PRIVATE_BACKUPS + 1))


def _private_parse_file(path):
    """读单个私密区文件 → (条目列表, 模式);不可解析/解密失败返回 None。
    错误信息不携带正文(调用方只记录通用摘要)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        return ([s for s in data if isinstance(s, str)], "plaintext")
    if isinstance(data, dict) and "data" in data:
        try:
            blob = base64.b64decode(data["data"])
            if data.get("dpapi"):
                blob = _cm()._dpapi_unprotect(blob)
                if blob is None:
                    return None
            return ([s for s in json.loads(blob.decode("utf-8"))
                     if isinstance(s, str)],
                    "dpapi" if data.get("dpapi") else "plaintext")
        except Exception:
            return None
    return None


def load_private_status():
    """带状态读取私密区(核心一律用这个,不能用空/非空判断)。"""
    got = _private_parse_file(_cm().PRIVATE_FILE)
    if got is not None:
        return PrivateLoadResult(got[0], "ok", got[1], "", "")
    if not os.path.exists(_cm().PRIVATE_FILE):
        return PrivateLoadResult([], "missing", "none", "", "")
    # 主文件在但读不出:按新→旧找可用备份
    for bak in _private_bak_paths():
        got = _private_parse_file(bak)
        if got is not None:
            _private_quarantine_corrupt()
            log("private.json 无法读取,已从 %s 恢复;损坏原件保留为 %s.corrupt"
                % (os.path.basename(bak), os.path.basename(_cm().PRIVATE_FILE)))
            return PrivateLoadResult(got[0], "recovered", got[1],
                                     os.path.basename(bak),
                                     "主文件损坏,已从备份恢复")
    log("private.json 及全部备份均无法读取:原文件保留待修复,本会话不覆写")
    return PrivateLoadResult([], "unreadable", "none", "",
                             "主文件与备份均无法读取(可能解密失败)")


def _private_quarantine_corrupt():
    """损坏主文件改名留档(固定一个 .corrupt 名,重复启动不无限复制)。"""
    try:
        if os.path.exists(_cm().PRIVATE_FILE):
            os.replace(_cm().PRIVATE_FILE, _cm().PRIVATE_FILE + ".corrupt")
    except Exception:
        pass


def load_private():
    """兼容包装:只取条目列表(状态感知路径请用 load_private_status)。"""
    return load_private_status().entries


def _private_rotate_backups():
    """把当前**有效加密**主文件轮转入备份链(bak3 删,bak2→3,bak1→2,主→bak1)。

    损坏主文件与旧明文主文件不进备份链:前者无有效内容,后者会把明文
    残留进备份(阶段 1 要求 10)。返回 (ok, 脱敏错误)。"""
    got = _private_parse_file(_cm().PRIVATE_FILE)
    if got is None or got[1] != "dpapi":
        return True, ""
    try:
        baks = _private_bak_paths()
        if os.path.exists(baks[-1]):
            os.remove(baks[-1])
        for i in range(len(baks) - 1, 0, -1):
            if os.path.exists(baks[i - 1]):
                os.replace(baks[i - 1], baks[i])
        raw = Path(_cm().PRIVATE_FILE).read_bytes()
        Path(baks[0]).write_bytes(raw)
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _private_write_payload(path, payload):
    """临时文件写入(独立函数=测试故障注入点)。"""
    Path(path).write_text(payload, encoding="utf-8")


def _private_cleanup_tmp(tmp):
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass


def save_private(priv):
    """写私密区:DPAPI 加密 → 临时文件 → 备份轮转 → 原子替换主文件。

    返回 PrivateSaveResult;任何一步失败都保留原文件(可重试),只有
    全部成功才返回 ok。DPAPI 保护不可用/失败时**不降级明文**——直接失败
    保留原文件(策略与 README 一致:不无告知写明文)。
    """
    items = [s for s in priv if isinstance(s, str)]
    data = json.dumps(items, ensure_ascii=False).encode("utf-8")
    blob = _cm()._dpapi_protect(data)   # 经 cm 回查:测试打桩语义保持
    if blob is None:
        log("私密区保存失败:DPAPI 保护不可用,原文件保持不变")
        return PrivateSaveResult(False, False, "DPAPI 保护不可用,原文件保持不变")
    payload = json.dumps({"dpapi": True,
                          "data": base64.b64encode(blob).decode("ascii")})
    tmp = _cm().PRIVATE_FILE + ".tmp"
    try:
        _cm()._private_write_payload(tmp, payload)
    except Exception as e:
        _private_cleanup_tmp(tmp)
        log("私密区保存失败(临时文件):%s" % e)
        return PrivateSaveResult(False, True, "临时文件写入失败:%s" % e)
    ok, err = _cm()._private_rotate_backups()
    if not ok:
        _private_cleanup_tmp(tmp)
        log("私密区保存失败(备份轮转):%s" % err)
        return PrivateSaveResult(False, True, "备份轮转失败:%s" % err)
    try:
        os.replace(tmp, _cm().PRIVATE_FILE)
    except Exception as e:
        _private_cleanup_tmp(tmp)
        log("私密区保存失败(替换主文件):%s" % e)
        return PrivateSaveResult(False, True, "替换主文件失败:%s" % e)
    return PrivateSaveResult(True, True, "")

