#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinClipboard - Windows 桌面剪贴板管理器
=========================================
功能:
  - 后台常驻系统托盘,自动记录文本 / 图片 / 文件剪贴历史
  - 全局热键 Ctrl+Shift+V 显示/隐藏主窗口
  - 双击/回车 复制并自动粘贴到当前窗口
  - 关键词搜索(文本 + 图片尺寸)、图片缩略图预览
  - 置顶(📌)、删除、清空,图片按内容像素 SHA-256 去重
  - 图片简单处理:裁剪 / 旋转 / 缩放 / 标注(方框、箭头、文字、马赛克)/ 压缩导出
  - 历史持久化(原子写入,重启后保留),开机自启动
依赖: pywin32, Pillow(界面在 winclipboard_qt.py,基于 PySide6)

作者: 自动生成   日期: 2026-06-29
"""
import os
import sys
import io
import json
import re
import time
import math
import struct
import base64
import queue
import ctypes
import hashlib
import subprocess
import threading
import traceback
import pipeline                      # 捕获管线(纯阶段;cm → pipeline 单向)
from store import ClipboardStore, blob_full_path   # SQLite 历史存储 + blob 分片路径
from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple
from ctypes import wintypes
from pathlib import Path

from perf_probe import PerfSpan          # 性能埋点(纯标准库,见 perf_probe.py)

from urllib.parse import urlsplit

try:
    import win32clipboard, win32con, win32gui, win32api
except ImportError:
    raise SystemExit("缺少 pywin32,请运行: pip install pywin32")
try:
    import win32crypt   # 私密区 DPAPI 加密(pywin32 自带)
except ImportError:
    win32crypt = None
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("缺少 Pillow,请运行: pip install Pillow")

# ============================================================
# 路径 / 常量
# ============================================================
APP_NAME = "WinClipboard"
APP_VERSION = "1.0.0-rc1"  # 产品化首个发布候选(关于/导出载荷/更新检查共用)
REPO_URL = "https://github.com/haoshangyang/WinClipboard"
RELEASE_API = "https://api.github.com/repos/haoshangyang/WinClipboard/releases/latest"
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
APP_DIR = os.path.join(_APPDATA, APP_NAME)
IMAGE_DIR = os.path.join(APP_DIR, "images")      # 旧布局(迁移期兼容)
BLOB_DIR = os.path.join(APP_DIR, "data", "blobs")   # CAS:data/blobs/<2位>/<哈希>
ICON_FILE = os.path.join(APP_DIR, "icon.ico")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
DB_FILE = os.path.join(APP_DIR, "clipboard.db")   # SQLite 事实存储(默认模式)
PRIVATE_FILE = os.path.join(APP_DIR, "private.json")   # 敏感内容隔离区
LOG_FILE = os.path.join(APP_DIR, "app.log")            # 崩溃/运行日志
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 消息
WM_TASKBAR = win32con.WM_USER + 20
WM_HOTKEY = 0x0312
WM_CLIPBOARDUPDATE = 0x031D
WM_TIMER = 0x0113
WM_COMMAND = 0x0111

# 热键修饰键
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008

# 托盘菜单项 ID
M_SHOW, M_CLEAR, M_PRIVATE, M_AUTOSTART, M_ABOUT, M_QUIT = 1001, 1002, 1003, 1004, 1005, 1006
M_CTT_OFF = 1007                # 退出图钉画布鼠标穿透(穿透中唯一可点入口之一)
M_PAUSE = 1008                   # 暂停/恢复剪贴板监听(临时隐私模式,不落盘)
ID_TIMER = 2001
ID_TIMER_CAPTURE = 2002      # 启动剪贴板捕获(单次)
ID_HOTKEY = 100              # 主热键(Ctrl+Shift+V)
ID_HOTKEY_PIN = 102          # 图钉画布:直接钉剪贴板图片(Ctrl+Shift+P)
ID_HOTKEY_CTT = 103          # 图钉画布:鼠标穿透切换(Ctrl+Alt+T)
ID_HOTKEY_QP = 104           # 快捷粘贴浮层(默认 Alt+`)

# 部分 win32con 构建缺失的托盘常量,这里显式定义(避免 AttributeError)
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

# Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# OEM 键的 VK → 显示名(快捷粘贴默认 Alt+`,录制对话框也允许这些键)
_VK_OEM_NAMES = {
    0xC0: "`", 0xBD: "-", 0xBB: "=", 0xBC: ",", 0xBE: ".",
    0xBF: "/", 0xBA: ";", 0xDE: "'", 0xDB: "[", 0xDD: "]",
    0xDC: "\\",
}

DEFAULT_CFG = {
    "hotkey_mod": MOD_CONTROL | MOD_SHIFT,
    "hotkey_key": 0x56,          # 'V'
    "quick_mod": MOD_ALT,
    "quick_key": 0xC0,           # '`'(VK_OEM_3):快捷粘贴浮层,Win+V 式轻入口
    "auto_paste": False,
    "sensitive_guard": True,
    "exclude_apps": [],         # 按应用排除:名单内程序的复制不入史(exe 名小写)
    "max_items": 100,
    "cleanup_days": 0,          # 自动清理:删除 N 天前的未置顶条目(0 = 关闭)
    "disk_cap_mb": 0,           # 图片缓存上限 MB,超限从最旧删起(0 = 关闭)
    "encrypt_history": False,   # history.json 用 DPAPI 加密(关闭 = 明文,便于备份迁移)
    "window_w": 600,
    "window_h_max": 720,
    # ---- AI 工作流(Qt 版 UI 使用;两版前端共享配置) ----
    # 服务: {"name","base_url","model","api_key"}  base_url 需兼容 OpenAI /chat/completions
    # 工作流: {"name","steps":[{"service","prompt"}]}  prompt 中 {input} 为上一步输出
    "ai_services": [],
    "ai_workflows": [],
}


def hotkey_label(mod, key=None):
    """热键位掩码+虚拟键码 → "Ctrl+Shift+V" 可读文本。
    key=None 表示只按了修饰键(录制过程中的中间态),结尾显示 "…"。"""
    parts = []
    if int(mod) & MOD_ALT:
        parts.append("Alt")
    if int(mod) & MOD_CONTROL:
        parts.append("Ctrl")
    if int(mod) & MOD_SHIFT:
        parts.append("Shift")
    if int(mod) & MOD_WIN:
        parts.append("Win")
    if key is None:
        parts.append("…")
        return "+".join(parts)
    k = int(key)
    if 0x30 <= k <= 0x39 or 0x41 <= k <= 0x5A:
        parts.append(chr(k))
    elif 0x70 <= k <= 0x7B:                     # VK_F1..VK_F12
        parts.append("F%d" % (k - 0x6F))
    elif k == 0x20:
        parts.append("空格")
    elif k in _VK_OEM_NAMES:
        parts.append(_VK_OEM_NAMES[k])
    else:
        parts.append("VK 0x%02X" % k)
    return "+".join(parts)


# ---- 面板外观(Apple 风格)----
SHD = 26                # 窗口四周阴影边距
PANEL_R = 20            # 面板圆角半径
KEY_RGB = (254, 0, 254)         # 透明色键
BG_RGB = (245, 245, 247)        # Apple 浅灰背景

# 面板内图片缩略图尺寸 / 高质量缩放采样器
THUMB_W, THUMB_H = 88, 44
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

# ---- ctypes 原型 ----
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE


def apply_rounded_corners(hwnd):
    """Win11 窗口圆角(DWMWA_WINDOW_CORNER_PREFERENCE);旧系统自动忽略。"""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              wintypes.LPCVOID, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        pref = wintypes.DWORD(2)   # DWMWC_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


# ============================================================
# 持久化 / 配置
# ============================================================
def ensure_dirs():
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(BLOB_DIR, exist_ok=True)


def blob_path(blob_id):
    """CAS 落位路径:data/blobs/<hash前2位>/<hash>(256 桶,单目录文件数可控)。
    与 store.blob_full_path 同一布局;blob_id 为空或格式异常时返回 None——
    调用方按无文件处理。"""
    b = str(blob_id or "").strip().lower()
    if len(b) < 4 or not re.fullmatch(r"[0-9a-f]+", b):
        return None
    return blob_full_path(APP_DIR, b)


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
    """把明文字段封装为密文 dict;DPAPI 不可用时退化为编码明文(并记日志)。"""
    raw = str(text).encode("utf-8")
    blob = _dpapi_protect(raw)
    if blob is None:
        log("win32crypt 不可用,AI 服务密钥退化为明文存储")
    return {"dpapi": blob is not None,
            "data": base64.b64encode(blob if blob is not None else raw).decode("ascii")}


def _load_config_raw():
    """读 config.json,返回 (配置, 是否需要回写)。

    主文件损坏时依次回退 .bak1~.bak3(与 history.json 同策略);
    旧版明文密钥在加载时即标记回写,由 ClipboardCore 落盘时加密。
    """
    cfg = dict(DEFAULT_CFG)
    upgraded = False
    data = None
    for fp in (CONFIG_FILE, CONFIG_FILE + ".bak1",
               CONFIG_FILE + ".bak2", CONFIG_FILE + ".bak3"):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            if fp != CONFIG_FILE:
                log("config.json 不可用,已从备份恢复: %s" % os.path.basename(fp))
            break
        data = None
    if not isinstance(data, dict):
        data = {}

    try:
        cfg.update(data)
    except Exception:
        pass
    # 列表键深拷贝,避免与 DEFAULT_CFG 共享可变对象(改配置时污染默认值)
    for k in ("ai_services", "ai_workflows"):
        if isinstance(cfg.get(k), list):
            cfg[k] = [dict(x) if isinstance(x, dict) else x for x in cfg[k]]
    # AI 服务密钥:明文 → 内存明文 + 标记回写(下次写入即加密);
    # 密文解不开(换用户 / 换机器)时保留原文件不动,只把该字段置空提示重填。
    for s in cfg.get("ai_services") or []:
        if not isinstance(s, dict):
            continue
        v = s.get("api_key")
        if isinstance(v, str):
            upgraded = True          # 明文格式(旧版 / 手工编辑)
        elif isinstance(v, dict) and "data" in v:
            plain = _dec_field(v)
            if plain is None:
                # 换 Windows 用户 / 换机器后 DPAPI 解不开:本次置空提示重填,
                # 但不置位回写标志,原文件保持原样便于排查
                s["api_key"] = ""
                log("AI 服务「%s」的密钥解密失败(DPAPI),本次置空,原文件已保留,请重新填写"
                    % s.get("name"))
            else:
                s["api_key"] = plain
    # 无配置文件 / 存在明文密钥 → 需要落盘一次
    return cfg, (upgraded or not data)


def _load_config_dict():
    return _load_config_raw()[0]


def load_config():
    """读配置(config.json 损坏时回退备份);字段缺失用 DEFAULT_CFG 补齐。"""
    return _load_config_dict()


def _migrate_config_file():
    """就地把 config.json 里的明文密钥封装为密文(不轮转备份)。

    启动迁移必须先跑这一步再 save_config:否则旧的明文文件会被轮转进
    config.json.bak1,密钥以明文形式残留在备份里,等于没加密。
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        changed = False
        for s in data.get("ai_services") or []:
            if isinstance(s, dict) and isinstance(s.get("api_key"), str) and s["api_key"]:
                s["api_key"] = _enc_field(s["api_key"])
                changed = True
        if not changed:
            return
        tmp = CONFIG_FILE + ".tmp"
        Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass


def _rotate_config_backup():
    """config.json 滚动备份(旧 → .bak1 → .bak2 → .bak3)。

    备份内容与主文件同为密文(密钥已在 load 阶段封装),不会留下明文副本。
    """
    try:
        if not os.path.exists(CONFIG_FILE):
            return
        for i in (2, 1):
            src = "%s.bak%d" % (CONFIG_FILE, i)
            dst = "%s.bak%d" % (CONFIG_FILE, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(CONFIG_FILE, CONFIG_FILE + ".bak1")
    except Exception:
        pass


def save_config(cfg):
    """原子写配置:滚动备份 + 临时文件替换;密钥字段落盘前一律 DPAPI 加密。"""
    out = dict(cfg)
    try:
        svcs = []
        for s in out.get("ai_services") or []:
            s = dict(s) if isinstance(s, dict) else s
            if isinstance(s, dict) and s.get("api_key") and \
                    not (isinstance(s["api_key"], dict) and "data" in s["api_key"]):
                s["api_key"] = _enc_field(s["api_key"])
            svcs.append(s)
        out["ai_services"] = svcs
    except Exception:
        log("配置密钥加密失败:\n%s" % traceback.format_exc())
    tmp = CONFIG_FILE + ".tmp"
    try:
        _rotate_config_backup()
        Path(tmp).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        log("配置写入失败:\n%s" % traceback.format_exc())
        try:
            os.remove(tmp)
        except Exception:
            pass


def _history_from_container(data):
    """历史容器解包:旧版明文 list,或新版 {"dpapi":bool,"data":<b64>} 整体
    DPAPI 加密(与私密区同一机制);解析/解密失败返回 None(交给上层试备份)。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        try:
            blob = base64.b64decode(data["data"])
            if data.get("dpapi"):
                blob = _dpapi_unprotect(blob)
                if blob is None:
                    log("history.json 解密失败(DPAPI),尝试备份恢复")
                    return None
            out = json.loads(blob.decode("utf-8"))
            return out if isinstance(out, list) else None
        except Exception:
            return None
    return None


def load_history():
    # 主文件损坏时依次回退到最近备份(.bak1/.bak2/.bak3);明文/加密两种
    # 格式都会被 _history_from_container 识别(备份里两种混存也各自可用)
    for fp in (HISTORY_FILE, HISTORY_FILE + ".bak1",
               HISTORY_FILE + ".bak2", HISTORY_FILE + ".bak3"):
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
        if fp != HISTORY_FILE:
            log("history.json 不可用,已从备份恢复: %s" % os.path.basename(fp))
        return cleaned
    return []


def _rotate_history_backup():
    """滚动备份:旧 history.json -> .bak1 -> .bak2 -> .bak3(最多保留 3 份)。"""
    try:
        if not os.path.exists(HISTORY_FILE):
            return
        for i in (2, 1):
            src = "%s.bak%d" % (HISTORY_FILE, i)
            dst = "%s.bak%d" % (HISTORY_FILE, i + 1)
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(HISTORY_FILE, HISTORY_FILE + ".bak1")
    except Exception:
        pass


def save_history(hist, encrypt=False):
    """原子写 + 滚动备份。encrypt=True 时整体 DPAPI 加密成
    {"dpapi":true,"data":<b64>} 容器(与私密区同一机制,绑定当前用户);
    读取两种格式自动识别(load_history)。DPAPI 不可用时本次退化为明文并记日志。"""
    payload = hist
    if encrypt:
        blob = _dpapi_protect(json.dumps(hist, ensure_ascii=False).encode("utf-8"))
        if blob is None:
            log("win32crypt 不可用,history.json 本次以明文保存")
        else:
            payload = {"dpapi": True,
                       "data": base64.b64encode(blob).decode("ascii")}
    tmp = HISTORY_FILE + ".tmp"
    try:
        _rotate_history_backup()
        Path(tmp).write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ---- 私密区 DPAPI 加密(绑定当前 Windows 用户账号,同机其他用户/复制文件均不可读) ----
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


def load_private():
    """读私密区:新版 {"dpapi":true,"data":<b64>} 自动解密;旧版明文列表直接兼容
    (下次保存时自动升级为加密);解密失败不覆盖原文件,数据不丢。"""
    try:
        with open(PRIVATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, list):
        # 旧版明文格式
        return [s for s in data if isinstance(s, str)]
    if isinstance(data, dict) and "data" in data:
        try:
            blob = base64.b64decode(data["data"])
            if data.get("dpapi"):
                blob = _dpapi_unprotect(blob)
                if blob is None:
                    log("private.json 解密失败(DPAPI),内容暂不可用,原文件已保留")
                    return []
            return [s for s in json.loads(blob.decode("utf-8")) if isinstance(s, str)]
        except Exception:
            log("private.json 读取失败:\n%s" % traceback.format_exc())
            return []
    return []


def save_private(priv):
    """写私密区:整体 JSON 序列化后 DPAPI 加密落盘(原子替换);
    win32crypt 不可用时退化为 base64 编码明文并记日志。"""
    data = json.dumps([s for s in priv if isinstance(s, str)],
                      ensure_ascii=False).encode("utf-8")
    blob = _dpapi_protect(data)
    if blob is None:
        log("win32crypt 不可用,私密区退化为明文存储")
    payload = {"dpapi": blob is not None,
               "data": base64.b64encode(blob if blob is not None else data).decode("ascii")}
    tmp = PRIVATE_FILE + ".tmp"
    try:
        Path(tmp).write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, PRIVATE_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def version_tuple(v):
    """版本串 → 可比较元组:"1.2.10"→(1,2,10)。容错 v 前缀、不完整版本
    ("1.2"→(1,2,0))与非数字段(取前导数字,无则 0)。"""
    parts = str(v or "").strip().lstrip("vV").split(".")
    out = []
    for p in parts[:4]:
        d = ""
        for ch in p:
            if ch.isdigit():
                d += ch
            else:
                break
        out.append(int(d) if d else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def fetch_latest_release():
    """查 GitHub 最新 Release;返回 (状态, 版本, 链接)。

    状态: "ok"=有新版 / "latest"=已是最新 / "norelease"=仓库尚无 Release /
    "error"=网络失败(链接位放原因)。同步阻塞 8s 上限,调用方放后台线程。
    只查不装:发现新版由前端打开发布页,下载安装交给用户。"""
    import http.client
    try:
        conn = http.client.HTTPSConnection("api.github.com", timeout=8)
        conn.request("GET", "/repos/haoshangyang/WinClipboard/releases/latest",
                     headers={"User-Agent": "WinClipboard-Updater",
                              "Accept": "application/vnd.github+json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        if resp.status == 404:
            return ("norelease", "", REPO_URL + "/releases")
        if resp.status != 200:
            return ("error", "", "HTTP %d" % resp.status)
        info = json.loads(body)
        tag = str(info.get("tag_name") or "").strip()
        url = str(info.get("html_url") or "").strip() or REPO_URL + "/releases"
        if not tag:
            return ("norelease", "", url)
        if version_tuple(tag) > version_tuple(APP_VERSION):
            return ("ok", tag.lstrip("vV"), url)
        return ("latest", tag.lstrip("vV"), url)
    except Exception as e:
        return ("error", "", repr(e)[:120])


def single_instance():
    mutex = kernel32.CreateMutexW(None, False, "Global\\WinClipboard_Mutex_7hasj2k")
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return (not already), mutex


def is_remote_session():
    """当前是否在远程会话(RDP 等)——兼容性开关(如派发器 WM_NULL 保活)
    的 auto 档依据。注意部分远控工具(网易UU等)不置 RDP 标志,测不出来,
    那类环境靠 UiHealthMonitor 看门狗兜底,或用户手动把开关设为 on。"""
    try:
        if os.environ.get("SESSIONNAME", "").upper().startswith("RDP-"):
            return True
        return bool(user32.GetSystemMetrics(0x1000))   # SM_REMOTESESSION
    except Exception:
        return False


def foreground_app_exe():
    """前台窗口所属进程的 exe 文件名(小写,如 "chrome.exe";失败返回 "")。

    剪贴板事件到达瞬间的前台窗口 ≈ 复制来源应用(用户刚在那儿按了 Ctrl+C),
    必须在托盘线程立即取——延迟到捕获线程时前台多半已经换窗口。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        # PROCESS_QUERY_LIMITED_INFORMATION:最小权限,连 UWP/提权进程也能问
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


# ============================================================
# 日志 / 异常兜底(提升稳定性)
# ============================================================
_log_lock = threading.Lock()


def log(msg):
    """追加运行/崩溃日志,超过 512KB 自动轮转(app.log.1/.2)。"""
    try:
        ensure_dirs()
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with _log_lock:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 512 * 1024:
                try:
                    if os.path.exists(LOG_FILE + ".2"):
                        os.remove(LOG_FILE + ".2")
                    if os.path.exists(LOG_FILE + ".1"):
                        os.replace(LOG_FILE + ".1", LOG_FILE + ".2")
                    os.replace(LOG_FILE, LOG_FILE + ".1")
                except Exception:
                    pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _log_exception(ctx, exc, val, tb):
    try:
        log("%s: %s: %s\n%s" % (ctx, exc.__name__, val,
                                "".join(traceback.format_exception(exc, val, tb))))
    except Exception:
        pass


def install_excepthooks():
    """未捕获异常(主线程/工作线程)全部落盘,避免静默崩溃。"""
    def eh(t, v, tb):
        _log_exception("未捕获异常", t, v, tb)
    sys.excepthook = eh
    try:
        threading.excepthook = lambda a: _log_exception(
            "线程未捕获异常", a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


# ============================================================
# 敏感内容识别(密码/银行卡/身份证/密钥,自动隔离)
# ============================================================
_RE_KEYVAL = re.compile(
    r"(?i)(password|passwd|pwd|密码|口令|token|secret|api[_-]?key|access[_-]?key|验证码|校验码)"
    r"\s*[:=]\s*(\S{4,80})")
_RE_CARD = re.compile(r"^\s*(\d[\d\s-]{12,23}\d)\s*$")
_RE_IDCARD = re.compile(r"^\s*\d{17}[\dXx]\s*$")
_RE_KEYS = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
_RE_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")


# ---- 可迁移加密备份(密码 → KDF → AES-256-GCM 认证加密)----
# 与 DPAPI(本机/当前用户绑定)互不相关:该容器可跨机器恢复,
# 前提是持有密码。cryptography 库缺失时功能禁用并如实提示。
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _AES_OK = True
except Exception:
    _AES_OK = False

_BAK_MAGIC = b"WCLBBAK1"
_BAK_KDF_ITER = 200000


def portable_backup_bytes(payload, password):
    """载荷 → 加密备份字节流(WCLBBAK1 容器)。
    PBKDF2-HMAC-SHA256(20 万次)派生密钥,AES-256-GCM 认证加密
    (密文自带认证标签,密码错误/文件被改都会解密失败)。"""
    if not _AES_OK:
        raise RuntimeError("未安装 cryptography 库,无法生成加密备份")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                              salt, _BAK_KDF_ITER)
    ct = AESGCM(key).encrypt(
        nonce, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    head = json.dumps({"v": 1, "kdf": "pbkdf2-sha256",
                       "iter": _BAK_KDF_ITER,
                       "salt": base64.b64encode(salt).decode("ascii"),
                       "nonce": base64.b64encode(nonce).decode("ascii"),
                       "cipher": "aes-256-gcm"}).encode("utf-8")
    return _BAK_MAGIC + len(head).to_bytes(4, "little") + head + ct


def portable_backup_parse(raw, password):
    """加密备份字节流 → 载荷 dict。magic 不符抛 ValueError;
    密码错误或数据被篡改,GCM 认证失败同样抛 ValueError(统一文案)。"""
    if not _AES_OK:
        raise RuntimeError("未安装 cryptography 库,无法读取加密备份")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 16 \
            or bytes(raw[:8]) != _BAK_MAGIC:
        raise ValueError("不是有效的 WinClipboard 加密备份文件")
    hlen = int.from_bytes(bytes(raw[8:12]), "little")
    head = json.loads(bytes(raw[12:12 + hlen]).decode("utf-8"))
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        base64.b64decode(head["salt"]), int(head["iter"]))
    try:
        plain = AESGCM(key).decrypt(
            base64.b64decode(head["nonce"]), bytes(raw[12 + hlen:]), None)
    except Exception:
        raise ValueError("密码错误或备份文件已损坏")
    return json.loads(plain.decode("utf-8"))


def _luhn_ok(s):
    digits = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total, alt = 0, False
    for d in reversed(digits):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _idcard_ok(s):
    s = s.strip().upper()
    if len(s) != 18 or not s[:17].isdigit():
        return False
    w = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    m = "10X98765432"
    return m[sum(int(s[i]) * w[i] for i in range(17)) % 11] == s[17]


def _sensitive_reason(text):
    """is_sensitive 的带原因版本(管线 trace / 排障用):返回命中类别或 None。
    正则与判定顺序与 is_sensitive 完全一致。"""
    t = text.strip()
    if not t:
        return None
    if _RE_KEYVAL.search(text):
        return "密码键值对"
    if _RE_KEYS.search(t):
        return "API 密钥格式"
    if _RE_JWT.match(t):
        return "JWT"
    m = _RE_CARD.match(t)
    if m and _luhn_ok(m.group(1)):
        return "银行卡号(Luhn 通过)"
    if _RE_IDCARD.match(t) and _idcard_ok(t):
        return "身份证号(校验位通过)"
    return None


def is_sensitive(text):
    """识别疑似敏感内容:密码上下文、银行卡(Luhn)、身份证(校验位)、API 密钥、JWT。"""
    return _sensitive_reason(text) is not None


# ============================================================
# AI Privacy Gate(发送前敏感检测与遮盖)
# 与 is_sensitive 同一套判定,但要求子串级命中(整条 ^$ 匹配在渲染后
# 的长 prompt 里永远不触发),并给出逐段遮盖文本。
# ============================================================
_RE_JWT_SUB = re.compile(
    r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
_RE_CARD_SUB = re.compile(r"(?<![\d-])(\d(?:[\d\s-]{11,24}\d))(?![\d-])")
_RE_IDCARD_SUB = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")


def privacy_scan(text):
    """AI 发送前隐私扫描:返回 (命中类别列表, 遮盖后文本)。

    命中子串替换为 ▇▇▇[类别](AI 仍知道"此处有被遮盖的密钥",解释类
    任务不受影响);重叠命中按起点先到先得,替换从后往前避免位移。
    银行卡(Luhn)/身份证(校验位)与 is_sensitive 判定一致,通过才算命中。
    无命中时原样返回 ([], text)。"""
    t = str(text or "")
    spans = []                                  # (start, end, label)
    for m in _RE_KEYS.finditer(t):
        spans.append((m.start(), m.end(), "API 密钥"))
    for m in _RE_JWT_SUB.finditer(t):
        spans.append((m.start(), m.end(), "JWT Token"))
    for m in _RE_KEYVAL.finditer(t):
        spans.append((m.start(), m.end(), "密码键值对"))
    for m in _RE_CARD_SUB.finditer(t):
        if _luhn_ok(m.group(1)):
            spans.append((m.start(), m.end(), "银行卡号"))
    for m in _RE_IDCARD_SUB.finditer(t):
        if _idcard_ok(m.group(1)):
            spans.append((m.start(), m.end(), "身份证号"))
    if not spans:
        return [], t
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))   # 同起点取最长
    keep, last_end = [], -1
    for s, e, lb in spans:
        if s >= last_end:
            keep.append((s, e, lb))
            last_end = e
    out = t
    for s, e, lb in reversed(keep):
        out = out[:s] + "▇▇▇[%s]" % lb + out[e:]
    labels = []                               # 含被重叠吞并的类别(同样告知用户)
    for _, _, lb in spans:
        if lb not in labels:
            labels.append(lb)
    return labels, out


# ============================================================
# 图标
# ============================================================
def make_icon(path):
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([30, 32, 98, 110], radius=12, fill=(60, 130, 235, 255))
    d.rounded_rectangle([37, 42, 91, 104], radius=6, fill=(255, 255, 255, 255))
    d.rounded_rectangle([50, 22, 78, 38], radius=5, fill=(200, 212, 230, 255))
    d.rounded_rectangle([55, 26, 73, 34], radius=3, fill=(120, 140, 170, 255))
    for yy in (54, 66, 78):
        d.rounded_rectangle([44, yy, 84, yy + 5], radius=2, fill=(60, 130, 235, 255))
    img.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])


# ============================================================
# DIB <-> PNG 转换(用于图片剪贴)
# ============================================================
def _dib_to_bmp_bytes(dib):
    """CF_DIB 前面补 14 字节 BITMAPFILEHEADER,拼成 PIL 可读的 BMP 字节流。"""
    bisize = struct.unpack("<I", dib[:4])[0]
    bitcount = struct.unpack("<H", dib[14:16])[0]
    compression = struct.unpack("<I", dib[16:20])[0] if len(dib) >= 20 else 0
    clrused = struct.unpack("<I", dib[32:36])[0] if len(dib) >= 36 else 0
    palette = 0
    if bitcount <= 8:
        palette = (clrused if clrused else (1 << bitcount)) * 4
    masks = 12 if (compression == 3 and bisize == 40) else 0  # BI_BITFIELDS 的掩码
    pixel_offset = 14 + bisize + palette + masks
    return b"BM" + struct.pack("<IHHI", len(dib) + 14, 0, 0, pixel_offset) + dib


def dib_to_png_bytes(dib):
    """把剪贴板的 CF_DIB 原始字节转成 PNG 字节流。"""
    buf = io.BytesIO()
    Image.open(io.BytesIO(_dib_to_bmp_bytes(dib))).save(buf, "PNG")
    return buf.getvalue()


def pixel_sha256(img):
    """对规范化像素字节做 SHA-256(内容寻址文件名用)。

    输入是「宽 + 高 + 模式 + 原始像素」而非编码后的 PNG 字节:后者会随
    PNG 编码器参数 / Pillow 版本 / 元数据漂移,同一张图重装环境后就对不上,
    去重会静默失效。哈希在解码之后做,去重命中时可以完全跳过 PNG 编码。
    """
    h = hashlib.sha256()
    h.update(struct.pack("<II", img.width, img.height))
    h.update(img.mode.encode("ascii"))
    h.update(img.tobytes())     # 触发懒解码;后续 save 复用已解码数据
    return h.hexdigest()


def png_to_dib(path):
    """把 PNG 文件转回 CF_DIB 原始字节(BITMAPINFOHEADER + 像素,24bit)。"""
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    return buf.getvalue()[14:]  # 去掉 14 字节 BITMAPFILEHEADER 即为 DIB


def pil_to_dib(img):
    """PIL 图像 -> CF_DIB 原始字节(24bit)。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    return buf.getvalue()[14:]  # 去掉 14 字节 BITMAPFILEHEADER 即为 DIB


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- ShareX 集成(成熟截图标注工具;未安装时前端回退内置编辑器) ----
def find_sharex():
    """定位 ShareX.exe;未安装返回 None。优先级:环境变量 WCLB_SHAREX >
    注册表 App Paths > 常见安装目录。"""
    env = os.environ.get("WCLB_SHAREX")
    if env and os.path.exists(env):
        return env
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                            r"\App Paths\ShareX.exe") as k:
            p, _ = winreg.QueryValueEx(k, None)
            if p and os.path.exists(p):
                return p
    except OSError:
        pass
    except Exception:
        pass
    for p in (os.path.expandvars(r"%ProgramFiles%\ShareX\ShareX.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\ShareX\ShareX.exe"),
              os.path.expandvars(r"%LocalAppData%\Programs\ShareX\ShareX.exe")):
        if os.path.exists(p):
            return p
    return None


def open_in_sharex(path):
    """用 ShareX 编辑器打开图片(官方 CLI:-ImageEditor <文件>)。
    用户在 ShareX 里保存/复制后,结果经系统剪贴板自动回流本工具历史。"""
    exe = find_sharex()
    if not exe or not os.path.exists(path):
        return False
    try:
        subprocess.Popen([exe, "-ImageEditor", os.path.abspath(path)],
                         shell=False)
        return True
    except Exception:
        log("调起 ShareX 失败:\n%s" % traceback.format_exc())
        return False


def find_ksnip():
    """定位 ksnip.exe(开源 Qt 截图标注器);未安装返回 None。
    优先级:环境变量 WCLB_KSNIP > 常见安装目录 > PATH。"""
    env = os.environ.get("WCLB_KSNIP")
    if env and os.path.exists(env):
        return env
    for p in (os.path.expandvars(r"%ProgramFiles%\ksnip\ksnip.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\ksnip\ksnip.exe"),
              os.path.expandvars(r"%LocalAppData%\Programs\ksnip\ksnip.exe")):
        if os.path.exists(p):
            return p
    try:
        import shutil
        p = shutil.which("ksnip")
        if p:
            return p
    except Exception:
        pass
    return None


def open_in_ksnip(path):
    """用 ksnip 编辑器打开图片(CLI:--editor <文件>;其 issue #103 起支持)。
    未装 ksnip / 文件缺失返回 False。"""
    exe = find_ksnip()
    if not exe or not os.path.exists(path):
        return False
    try:
        subprocess.Popen([exe, "--editor", os.path.abspath(path)],
                         shell=False)
        return True
    except Exception:
        log("调起 ksnip 失败:\n%s" % traceback.format_exc())
        return False


def open_file_default(path):
    """用系统默认关联程序打开文件(Windows 原生体验,同资源管理器双击)。"""
    try:
        os.startfile(path)
        return True
    except Exception:
        log("默认程序打开失败 %s:\n%s" % (path, traceback.format_exc()))
        return False


def open_url(url):
    """系统默认浏览器打开链接(标准库 webbrowser,走 OS 关联)。"""
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        log("打开链接失败 %s:\n%s" % (url, traceback.format_exc()))
        return False


def reveal_in_explorer(path):
    """在资源管理器中定位该文件(选中显示)。
    用 ShellExecuteW 原生 API;路径中的引号字符已剔除,参数不可被拆分。"""
    try:
        import ctypes
        target = os.path.abspath(path).replace('"', "")
        folder = os.path.dirname(target) or target
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "open", "explorer.exe", '/select,"%s"' % target, folder, 5)  # SW_SHOW
        return int(rc) > 32
    except Exception:
        log("资源管理器定位失败 %s:\n%s" % (path, traceback.format_exc()))
        return False


def set_dib_to_clipboard(hwnd, dib):
    """把 DIB 字节写入剪贴板(带重试)。"""
    for _ in range(6):
        try:
            win32clipboard.OpenClipboard(hwnd)
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.03)
    return False


class DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", ctypes.c_uint32),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
                ("fNC", ctypes.c_int),
                ("fWide", ctypes.c_int)]


def build_dropfiles(paths):
    """构造 CF_HDROP 的 DROPFILES 结构字节(宽字符路径列表,双 NUL 结尾)。"""
    df = DROPFILES()
    df.pFiles = ctypes.sizeof(DROPFILES)
    df.fWide = 1
    wide = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in paths) + b"\x00\x00"
    return ctypes.string_at(ctypes.addressof(df), ctypes.sizeof(DROPFILES)) + wide


# ============================================================
# SendInput(模拟 Ctrl+V)
# ============================================================
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def send_ctrl_v():
    arr = (INPUT * 4)()
    arr[0] = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_CONTROL, 0, 0, 0, None))
    arr[1] = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_V, 0, 0, 0, None))
    arr[2] = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_V, 0, KEYEVENTF_KEYUP, 0, None))
    arr[3] = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, None))
    user32.SendInput(4, ctypes.byref(arr), ctypes.sizeof(INPUT))


# ---- 粘贴事务(Paste Transaction)----
# 目标:防止「用户按 Enter 的瞬间窗口切换 → 密码/API Key 被贴到错误窗口」。
# 前台窗口与剪贴板序列号在发送前轮询校验,任一不符立即取消。
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD


def _get_foreground():
    """当前前台窗口句柄(int;失败 0)。测试可替换。"""
    try:
        return int(user32.GetForegroundWindow())
    except Exception:
        return 0


def _get_clip_seq():
    """系统剪贴板序列号(每次写板自增;0 表示不可用)。"""
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


# ============================================================
# 托盘 / 后台线程(独立 Win32 消息循环)
# ============================================================
class TrayWorker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.cmd_q = queue.Queue()
        self.hwnd = None
        self.hicon = None
        self._nid = None
        self._last_toggle = 0.0   # 托盘左键防抖

    # ---- 线程入口 ----
    def run(self):
        ensure_dirs()
        if not os.path.exists(ICON_FILE):
            try:
                make_icon(ICON_FILE)
            except Exception:
                pass
        self.hinst = win32api.GetModuleHandle(None)

        # 图标
        try:
            self.hicon = win32gui.LoadImage(
                self.hinst, ICON_FILE, win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        except Exception:
            self.hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

        message_map = {
            win32con.WM_DESTROY: self.on_destroy,
            WM_TASKBAR: self.on_taskbar,
            WM_CLIPBOARDUPDATE: self.on_clip,
            WM_HOTKEY: self.on_hotkey,
            WM_TIMER: self.on_timer,
            WM_COMMAND: self.on_command,
        }
        wc = win32gui.WNDCLASS()
        wc.hInstance = self.hinst
        wc.lpszClassName = "WinClipboardHostWnd"
        wc.lpfnWndProc = message_map
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        try:
            class_atom = win32gui.RegisterClass(wc)
        except win32gui.error:
            class_atom = wc.lpszClassName

        # 标题不能叫 WinClipboard:否则 FindWindow 会先命中这个隐藏宿主窗口,
        # 盖过真正的主窗口(外部诊断/_gui_test 全被误导)
        self.hwnd = win32gui.CreateWindowEx(
            0, class_atom, "WinClipboardTrayHost", 0, 0, 0, 0, 0, 0, 0,
            self.hinst, None)

        # 托盘图标
        # 托盘图标(提示文本跟随实际热键,可在设置页录制更改)
        _cfg0 = self.app.cfg
        _hk_tip = "WinClipboard  (%s)" % hotkey_label(
            _cfg0.get("hotkey_mod", MOD_CONTROL | MOD_SHIFT),
            _cfg0.get("hotkey_key", 0x56))
        self._nid = (self.hwnd, 0,
                     NIF_ICON | NIF_MESSAGE | NIF_TIP,
                     WM_TASKBAR, self.hicon, _hk_tip)
        win32gui.Shell_NotifyIcon(NIM_ADD, self._nid)

        # 剪贴板监听 + 全局热键 + 命令定时器
        user32.AddClipboardFormatListener(self.hwnd)
        cfg = self.app.cfg
        if not user32.RegisterHotKey(self.hwnd, ID_HOTKEY,
                                     cfg["hotkey_mod"], cfg["hotkey_key"]):
            # 热键注册失败:更新托盘提示并通知主线程弹一次警告
            try:
                nid = (self.hwnd, 0, NIF_ICON | NIF_MESSAGE | NIF_TIP, WM_TASKBAR,
                       self.hicon, "WinClipboard (热键注册失败!)")
                win32gui.Shell_NotifyIcon(NIM_MODIFY, nid)
            except Exception:
                pass
            self.app._post(("tray", "hotkey_fail"))
            log("全局热键注册失败(hotkey_mod=%d key=%d)" % (cfg["hotkey_mod"], cfg["hotkey_key"]))
        # 图钉画布热键:注册失败仅记日志/禁用对应功能,不弹模态
        pin_ok = user32.RegisterHotKey(self.hwnd, ID_HOTKEY_PIN,
                                       MOD_CONTROL | MOD_SHIFT, 0x50)   # 'P'
        ctt_ok = user32.RegisterHotKey(self.hwnd, ID_HOTKEY_CTT,
                                       MOD_CONTROL | MOD_ALT, 0x54)     # 'T'
        # 快捷粘贴浮层热键(默认 Alt+`):失败降级为仅设置页提示,不弹模态
        qp_ok = user32.RegisterHotKey(
            self.hwnd, ID_HOTKEY_QP,
            int(cfg.get("quick_mod", MOD_ALT)), int(cfg.get("quick_key", 0xC0)))
        if not pin_ok:
            log("图钉热键(Ctrl+Shift+P)被占用,直钉剪贴板不可用")
        if not ctt_ok:
            log("穿透热键(Ctrl+Alt+T)被占用,鼠标穿透已禁用")
        if not qp_ok:
            log("快捷粘贴热键(%s)被占用,浮层入口不可用(可在设置页录制新组合)"
                % hotkey_label(cfg.get("quick_mod", MOD_ALT),
                               cfg.get("quick_key", 0xC0)))
        self.app._post(("ctt_hotkey_state", bool(ctt_ok)))
        self.app._post(("qp_hotkey_state", bool(qp_ok)))
        user32.SetTimer(self.hwnd, ID_TIMER, 80, None)

        # 启动捕获延后到消息循环内:剪贴板里若是大图,PNG 编码可耗时数秒,
        # 同步做会卡住 PumpMessages 进入循环,热键/托盘在启动初期全部失联
        user32.SetTimer(self.hwnd, ID_TIMER_CAPTURE, 1, None)

        try:
            win32gui.PumpMessages()
        except Exception:
            log("工作线程消息循环异常退出: %r" % sys.exc_info()[1])
        finally:
            # 收尾
            self._cleanup()

    def _cleanup(self):
        for fn in (
            lambda: user32.UnregisterHotKey(self.hwnd, ID_HOTKEY),
            lambda: user32.UnregisterHotKey(self.hwnd, ID_HOTKEY_PIN),
            lambda: user32.UnregisterHotKey(self.hwnd, ID_HOTKEY_CTT),
            lambda: user32.UnregisterHotKey(self.hwnd, ID_HOTKEY_QP),
            lambda: user32.RemoveClipboardFormatListener(self.hwnd),
        ):
            try:
                fn()
            except Exception:
                pass
        if self._nid:
            try:
                win32gui.Shell_NotifyIcon(NIM_DELETE, self._nid)
            except Exception:
                pass

    # ---- 消息处理 ----
    def on_destroy(self, hwnd, msg, wp, lp):
        win32gui.PostQuitMessage(0)
        return 0

    def on_taskbar(self, hwnd, msg, wp, lp):
        if lp in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            # 一次双击会连续产生 UP+DBLCLK+UP,做防抖避免面板被切换两次
            now = time.time()
            if now - self._last_toggle < 0.45:
                return 1
            self._last_toggle = now
            self.app._post(("tray", "show"))
        elif lp == win32con.WM_RBUTTONUP:
            self._popup_menu()
        return 1

    def on_clip(self, hwnd, msg, wp, lp):
        # 我们自己写入剪贴板时跳过(时间窗比一次性标志更抗竞态)
        if time.monotonic() < self.app._ignore_clip_until:
            return 0
        # 来源应用在事件到达瞬间取(前台窗口进程),延迟到捕获线程就换窗口了;
        # 暂停监听/按应用排除在此门控,命中即不入史
        src = foreground_app_exe()
        if not self.app.should_capture(src):
            return 0
        self.app.submit_capture(src)
        return 0

    def on_hotkey(self, hwnd, msg, wp, lp):
        if wp == ID_HOTKEY_PIN:
            self.app._post(("pin_hotkey",))
        elif wp == ID_HOTKEY_CTT:
            self.app._post(("ctt_hotkey",))
        elif wp == ID_HOTKEY_QP:
            self.app._post(("qp_hotkey",))
        else:
            self.app._post(("hotkey",))
        return 0

    def on_timer(self, hwnd, msg, wp, lp):
        if wp == ID_TIMER_CAPTURE:
            # 单次:进入消息循环后立刻提交启动捕获(编码在捕获线程做)
            user32.KillTimer(hwnd, ID_TIMER_CAPTURE)
            self.app.submit_capture()
            return 0
        while True:
            try:
                cmd = self.cmd_q.get_nowait()
            except queue.Empty:
                break
            self._do_cmd(cmd)
        return 0

    def on_command(self, hwnd, msg, wp, lp):
        mid = wp & 0xFFFF
        actions = {M_SHOW: "show", M_CLEAR: "clear", M_PRIVATE: "private",
                   M_AUTOSTART: "autostart", M_ABOUT: "about", M_QUIT: "quit",
                   M_CTT_OFF: "ctt_off", M_PAUSE: "pause"}
        act = actions.get(mid)
        if act:
            self.app._post(("tray", act))
        return 0

    # ---- 命令处理 ----
    def _do_cmd(self, cmd):
        if cmd[0] == "paste":
            # 粘贴事务:expected = 意图目标窗口(唤出我们窗口之前的那个),
            # want_seq = 写板完成后的剪贴板序列号。二者在宽限期内校验,
            # 不符立即取消(防止内容被贴进切换后的错误窗口)。
            expected = int(cmd[1]) if len(cmd) > 1 and cmd[1] else 0
            want_seq = int(cmd[2]) if len(cmd) > 2 and cmd[2] else 0
            if not expected:
                time.sleep(0.07)          # 旧调用/测试:无目标校验
                try:
                    send_ctrl_v()
                except Exception:
                    pass
                return
            deadline = time.monotonic() + 0.45
            while time.monotonic() < deadline:
                time.sleep(0.04)
                if want_seq and _get_clip_seq() != want_seq:
                    self.app._post(("paste_abort",
                                    "剪贴板内容在粘贴前被其他程序修改"))
                    return
                if _get_foreground() == expected:
                    try:
                        send_ctrl_v()
                    except Exception:
                        pass
                    return
            self.app._post(("paste_abort", "目标窗口已切换,已取消粘贴"))
        elif cmd[0] == "hotkey":
            # 设置页热键录制:重注册即冲突检测,失败回滚旧组合并如实上报
            mod, key = int(cmd[1]), int(cmd[2])
            cfg = self.app.cfg
            try:
                user32.UnregisterHotKey(self.hwnd, ID_HOTKEY)
            except Exception:
                pass
            ok = bool(user32.RegisterHotKey(self.hwnd, ID_HOTKEY, mod, key))
            if ok:
                try:
                    nid = (self.hwnd, 0, NIF_ICON | NIF_MESSAGE | NIF_TIP,
                           WM_TASKBAR, self.hicon,
                           "WinClipboard  (%s)" % hotkey_label(mod, key))
                    win32gui.Shell_NotifyIcon(NIM_MODIFY, nid)
                    self._nid = nid
                except Exception:
                    pass
            else:
                # 被占用:把旧热键注册回去,保持可用
                user32.RegisterHotKey(self.hwnd, ID_HOTKEY,
                                      cfg.get("hotkey_mod", MOD_CONTROL | MOD_SHIFT),
                                      cfg.get("hotkey_key", 0x56))
            self.app._post(("hotkey_change", ok, mod, key))
        elif cmd[0] == "hotkey_qp":
            # 快捷粘贴热键录制:同主热键套路,重注册即冲突检测,失败回滚
            mod, key = int(cmd[1]), int(cmd[2])
            cfg = self.app.cfg
            try:
                user32.UnregisterHotKey(self.hwnd, ID_HOTKEY_QP)
            except Exception:
                pass
            ok = bool(user32.RegisterHotKey(self.hwnd, ID_HOTKEY_QP, mod, key))
            if not ok:
                user32.RegisterHotKey(
                    self.hwnd, ID_HOTKEY_QP,
                    int(cfg.get("quick_mod", MOD_ALT)),
                    int(cfg.get("quick_key", 0xC0)))
            self.app._post(("qp_hotkey_change", ok, mod, key))
        elif cmd[0] == "quit":
            self._cleanup()
            win32gui.PostQuitMessage(0)

    # ---- 托盘菜单 ----
    def _popup_menu(self):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_SHOW,
                            "显示剪贴板  (Ctrl+Shift+V)")
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_CLEAR, "清空历史")
        pflag = win32con.MF_CHECKED if getattr(self.app, "monitor_paused", False) \
            else win32con.MF_UNCHECKED
        win32gui.AppendMenu(menu, win32con.MF_STRING | pflag, M_PAUSE,
                            "暂停监听(临时隐私模式)")
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_PRIVATE,
                            "私密内容 (%d)" % len(self.app.private))
        flag = win32con.MF_CHECKED if self.app.is_autostart() else win32con.MF_UNCHECKED
        win32gui.AppendMenu(menu, win32con.MF_STRING | flag, M_AUTOSTART, "开机自启动")
        # 穿透中的画布自身不收鼠标,托盘菜单是保底关闭入口之一(非穿透时点击无效无害)
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_CTT_OFF,
                            "退出画布鼠标穿透")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_ABOUT, "关于 WinClipboard")
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_QUIT, "退出")
        pos = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.hwnd)  # 必须,否则菜单不消失
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN,
                                pos[0], pos[1], 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)


# ============================================================
# 事务(可撤销操作的统一抽象;Command 模式)
# 设计:删除类事务条目只进 deleted 态(库模式)或仅出列(JSON 模式),
# 图片文件一律不动——真正的文件释放只发生在引用计数归零
# (GC 之后 / 硬删路径复检),撤销窗口内的引用始终成立。
# 扩展点:unpin / clear / 画布删除等可按同骨架加子类。
# ============================================================
class Transaction:
    """一次可整体撤销的操作:commit 落库,rollback 原样回滚。"""

    label = "操作"

    def __init__(self, core, recs):
        self.core = core
        self.recs = list(recs)
        self.at = time.time()

    def commit(self):
        raise NotImplementedError

    def rollback(self):
        """撤销:恢复条目;默认实现按记录身份防重复恢复。"""
        live = {id(h) for h in self.core.history}
        return [r for r in self.recs if id(r) not in live]


class DeleteTransaction(Transaction):
    """删除一批条目:出列 + 库行置 deleted 态;撤销 = 回列最前 + restore。"""

    label = "删除"

    def commit(self):
        live = {id(h) for h in self.core.history}
        gone = [r for r in self.recs if id(r) in live]
        self.core.history[:] = [h for h in self.core.history
                                if id(h) not in {id(r) for r in gone}]
        if self.core.store is not None:
            self.core.store.mark_deleted(
                [r.get("_db") for r in gone if r.get("_db") is not None])
        self.core._mark_dirty()
        return gone

    def rollback(self):
        back = super().rollback()
        self.core.history[0:0] = back
        if self.core.store is not None:
            for r in back:
                # 行仍存在(仅 deleted 态)→置回 active 并提升最前;
                # 行已被 GC→upsert 用内存 rec 重建。两种路径同一入口。
                self.core.store.upsert_promote(r)
        self.core._mark_dirty()
        return back


# ============================================================
# 核心后端(与 UI 无关;由 winclipboard_qt.py 的 Qt 前端调用)
# ============================================================
class ClipboardCore:
    """剪贴板核心状态机:配置 / 历史 / 私密区 / 捕获 / 回写 / 淘汰 / 自启。

    约定:
    - history / private / cfg 容器始终原地修改(不重新绑定),
      前端在初始化后可安全持有同名别名;
    - 所有事件(剪贴捕获 / 历史变化 / 热键 / 托盘)一律经 _post 投递给前端
      (queue 传数据,on_event 唤醒),由前端在自己的 UI 线程排空处理;
      核心不会直接调用任何 UI 代码;
    - 托盘/热键/监听线程(worker)由本类创建并启动。
    """

    store = None        # SQLite 仓库(类级默认:__new__ 直造的核心/测试为 None→JSON 路径)
    _txn_stack = None   # 撤销栈(类级默认同上;访问一律经 _txn_list)

    def __init__(self):
        self.cfg, _cfg_stale = _load_config_raw()
        self._cfg_need_save = _cfg_stale
        self.max_items = int(self.cfg.get("max_items", 100))
        # SQLite 事实存储(默认模式):启动加载 + 逐操作写透。
        # encrypt_history 开启时库不做写透(库文件无法整体 DPAPI 加密,
        # 不静默降级)——但若数据已在库里,仍从库只读加载并导出加密
        # history.json,杜绝「开加密 → 重启 → 历史消失」的丢失路径。
        self.store = None
        _want_db = not self.cfg.get("encrypt_history")
        _preload = None
        try:
            _preload = ClipboardStore(DB_FILE)
        except Exception:
            _preload = None
            log("SQLite 存储打开失败,本次会话走 JSON 持久化:\n%s"
                % traceback.format_exc())
        if _preload is not None and _preload.count() > 0:
            self.history = _preload.load_all()
            json_world = False
            if _want_db:
                self.store = _preload
            else:
                _preload.close()
                save_history(self.history, encrypt=True)
                log("加密模式开启而数据在 clipboard.db(%d 条):"
                    "已导出为加密 history.json;关闭加密即回到库模式"
                    % len(self.history))
        else:
            if _preload is not None:
                if _want_db:
                    self.store = _preload
                else:
                    _preload.close()
            self.history = load_history()
            json_world = True
        self.private = load_private()   # 敏感内容隔离区(不进普通面板)
        self.signal_q = queue.Queue()
        # 事件唤醒回调(前端注入):queue 只管数据,回调负责把"该排空了"
        # 通知到 UI 线程(如 Qt Signal 的 emit);无 UI / 测试环境保持 None
        self.on_event = None
        # 临时隐私模式:True 时剪贴板事件不入史(托盘/设置页切换,重启即恢复监听)
        self.monitor_paused = False
        # 按应用排除:命中名单的来源应用复制不入史(热集,名单变更时重建)
        self._exclude_set = self._build_exclude_set()
        # 剪贴板捕获线程(读板 + 大图 PNG 编码都放这):曾同步跑在托盘线程,
        # 大图编码数秒期间热键/托盘消息全部排队失联(启动期尤其明显)
        self._capture_exec = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="capture")
        # 防抖落盘:变更只置脏,由前端定时器调 flush_history 统一写盘
        # (全量 JSON 落盘在大历史上可达几十毫秒,此前每次剪贴事件都同步执行)
        self._dirty = False
        self._gen = 0                 # 变更代数:落盘期间再有变更则不能算已持久化
        self._search_cache = {}       # id(rec) → casefold 检索串(条目内容不可变)
        self.on_dirty = None            # 前端回调(启动防抖定时器)
        self._ignore_clip_until = 0.0   # 抑制自写入剪贴板事件的截止时间(单调钟)
        # 可撤销操作栈(Transaction;上限批数见 _TXN_MAX)
        self._txn_stack = []
        if json_world:
            # 历史迁移:补齐哈希、合并重复图片条目(孤儿文件清理延迟到启动后,
            # 见 cleanup_orphan_images;条目迁移仍须在后台线程启动前完成)
            self._migrate_images()
            # 自动清理(天数/容量策略,置顶豁免):在首笔落盘前做,裁剪结果直接持久化
            self.apply_cleanup()
            # 修正与裁剪先在 JSON 世界完成,再单事务迁入空库;原文件改名
            # .migrated 保留(回滚 = 改回原名即可)
            if self.store is not None:
                if self.history:
                    self.store.migrate_from_list(list(reversed(self.history)))
                    try:
                        os.replace(HISTORY_FILE, HISTORY_FILE + ".migrated")
                    except Exception:
                        pass
                    log("history.json → clipboard.db 迁移完成(%d 条;"
                        "原文件保留为 history.json.migrated)"
                        % len(self.history))
                self.history = self.store.load_all()   # 统一带 _db
        else:
            # 库模式:图片修正已在首次迁移前做过;启动清理走库写透
            self.apply_cleanup()
        # CAS 布局迁移(images/img_<hash>.png → blobs/<h2>/<hash>;两种模式都要)
        self._migrate_blobs()
        # 上次会话残留的 deleted 态行(异常退出没等到 GC)在此清账
        self._collect_garbage()
        # 旧版明文私密区/密钥自动升级为 DPAPI 加密
        self._migrate_private()
        self._migrate_config_secrets()
        # 后台线程(托盘 / 全局热键 / 剪贴板监听)
        self.worker = TrayWorker(self)
        self.worker.start()

    # ---- 事件投递(任意线程可调) ----
    def _store_del(self, recs):
        """批量删除条目对应的库行(软删除/淘汰/清理共用)。"""
        if self.store is not None:
            self.store.delete_many(
                [r.get("_db") for r in recs if r.get("_db") is not None])

    def _post(self, event):
        """向后端事件队列投递一条:queue 负责数据,on_event 负责唤醒。

        on_event 由前端注入(Qt Signal 的 emit,队列连接):托盘/捕获线程
        emit 后 Qt 把排空任务排进主线程事件循环;主线程内 emit(如 add_item
        触发的 change)同样只是排队不会重入。测试里可为 None。
        """
        self.signal_q.put(event)
        cb = getattr(self, "on_event", None)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _build_exclude_set(self):
        return {os.path.basename(str(s).strip()).lower()
                for s in self.cfg.get("exclude_apps") or [] if str(s).strip()}

    def should_capture(self, src=""):
        """捕获门(与暂停监听同一条隐私控制线):暂停中,或来源应用命中
        排除名单 → 本次剪贴板事件不入史。"""
        if self.monitor_paused:
            return False
        if src and src.lower() in self._exclude_set:
            return False
        return True

    def set_exclude_apps(self, names):
        """更新按应用排除名单:归一化(取文件名/小写/去空/去重保序)→
        写配置 → 重建匹配热集;返回归一化后的列表。"""
        norm, seen = [], set()
        for n in names or []:
            v = os.path.basename(str(n).strip()).lower()
            if v and v not in seen:
                seen.add(v)
                norm.append(v)
        self.cfg["exclude_apps"] = norm
        self._exclude_set = set(norm)
        save_config(self.cfg)
        return norm

    # ---- 剪贴板读取(在捕获线程调用) ----
    # 富文本格式保留:浏览器/Office 复制时剪贴板同时带 HTML Format / RTF,
    # 随文本条目存储、回写时多格式并放,粘贴到 Word/表格不再退化成纯文本。
    # 用 latin-1 解码/编码做字节级无损往返:HTML Format 头里的
    # StartHTML/EndHTML 是字节偏移,utf-8"replace"解码会移动偏移致消费端解析错乱。
    RICH_FORMATS = (("html", "HTML Format"), ("rtf", "Rich Text Format"))
    _RICH_CAP = 512 * 1024       # 单格式捕获上限,超大只存纯文本

    @staticmethod
    def _reg_fmt(name):
        try:
            return win32clipboard.RegisterClipboardFormat(name)
        except Exception:
            return 0

    def _read_rich_formats(self):
        """富文本格式捕获(剪贴板已打开时调用):返回 {"html":…,"rtf":…} 子集。"""
        rich = {}
        for key, fmt in self.RICH_FORMATS:
            try:
                cf = self._reg_fmt(fmt)
                if not cf or not win32clipboard.IsClipboardFormatAvailable(cf):
                    continue
                raw = win32clipboard.GetClipboardData(cf)
                if not raw:
                    continue
                txt = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
                if txt and len(txt) <= self._RICH_CAP:
                    rich[key] = txt
            except Exception:
                continue
        return rich

    def submit_capture(self, src=""):
        """把一次捕获(读板 + 可能的大图 PNG 编码)提交到后台线程。
        托盘线程调用后立即返回,不再被编码耗时拖住消息循环。
        src = 来源应用 exe 名(已在托盘线程取好,这里只透传给捕获线程)。"""
        exec_ = getattr(self, "_capture_exec", None)
        if exec_ is None:                    # 兼容测试里 __new__ 构造的核心
            self._capture_once(src)
            return
        try:
            exec_.submit(self._capture_once, src)
        except Exception:
            pass

    def _capture_once(self, src=""):
        try:
            item = self.read_clipboard_now()
            if item:
                self._post(("clip", item + (src,)))
        except Exception:
            pass

    def read_clipboard_now(self):
        hwnd = self.worker.hwnd
        for _ in range(6):
            try:
                win32clipboard.OpenClipboard(hwnd)
                try:
                    # 文件(资源管理器复制文件/文件夹)优先
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_HDROP):
                        files = win32clipboard.GetClipboardData(win32clipboard.CF_HDROP)
                        files = [str(f) for f in (files or [])][:50]
                        if files:
                            return ("files", files)
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
                        dib = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
                        return self._store_image(dib)
                    if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        t = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                        if t:
                            t = t.rstrip("\r\n")   # 统一尾随换行,便于去重
                            if t.strip("\r\n\t "):
                                rich = self._read_rich_formats()
                                if rich:
                                    return ("text", t, rich)
                                return ("text", t)
                finally:
                    win32clipboard.CloseClipboard()
                return None
            except Exception:
                time.sleep(0.03)
        return None

    def _store_image(self, dib):
        try:
            img = Image.open(io.BytesIO(_dib_to_bmp_bytes(dib)))
            hsh = pixel_sha256(img)
            # 新捕获走分片 blob 布局(data/blobs/<2位>/<哈希>);旧
            # images/img_*.png 由绝对路径继续引用,互不冲突
            path = blob_full_path(APP_DIR, hsh)
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                # 去重命中时完全跳过 PNG 编码(4K 图编码秒级,像素哈希只要百毫秒级)
                with PerfSpan("png_encode"):
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                # 原子落盘:崩溃不留半截文件(否则下次命中坏文件无法自愈)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                Path(path + ".tmp").write_bytes(buf.getvalue())
                os.replace(path + ".tmp", path)
            w = struct.unpack("<i", dib[4:8])[0]
            h = abs(struct.unpack("<i", dib[8:12])[0])
            return ("image", {"path": path, "w": w, "h": h, "hash": hsh})
        except Exception:
            return None

    _BLOB_NAME_RE = re.compile(r"^img_([0-9a-f]{64})\.png$")

    def _migrate_blobs(self):
        """CAS 布局一次性迁移:images/img_<hash>.png → blobs/<h2>/<hash>。

        只认像素哈希命名的旧文件;其他文件(外部/测试放置)原地不动,
        条目路径不变照常工作。目标已存在(内容相同)→ 直接删源文件。
        条目 content 原地更新;库模式同步 update_image_ref 写透。
        迁移后 images/ 若已空则移除。"""
        try:
            moved = 0
            by_path = {}
            # 历史与撤销栈里的条目都可能引用旧路径(撤销窗口内文件挪走后
            # 仍要能显示),迁移统一原地更新两处引用
            live_recs = list(self.history)
            for t in self._txn_list():
                live_recs.extend(t.recs)
            for h in live_recs:
                if h.get("type") == "image" and h.get("content"):
                    by_path[os.path.normcase(h["content"])] = h
            if not os.path.isdir(IMAGE_DIR):
                return
            for name in os.listdir(IMAGE_DIR):
                m = self._BLOB_NAME_RE.match(name)
                if not m:
                    continue
                hsh = m.group(1)
                src = os.path.join(IMAGE_DIR, name)
                dst = blob_path(hsh)
                if dst is None:
                    continue
                try:
                    if os.path.exists(dst):
                        os.remove(src)          # 内容相同,保留一份即可
                    else:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.replace(src, dst)
                    moved += 1
                    rec = by_path.get(os.path.normcase(src))
                    if rec is not None:
                        rec["content"] = dst
                        if self.store is not None and rec.get("_db") is not None:
                            self.store.update_image_ref(rec["_db"], hsh, dst)
                except Exception:
                    continue
            if moved:
                log("CAS 迁移:%d 个图片文件移入 blobs/ 分片布局" % moved)
                if self.store is None:
                    save_history(self.history,
                                 encrypt=bool(self.cfg.get("encrypt_history")))
                else:
                    self._mark_dirty()
                # images/ 空了(只剩非托管文件则不动目录)→ 尝试清掉
                try:
                    with os.scandir(IMAGE_DIR) as it:
                        empty = not any(it)
                    if empty:
                        os.rmdir(IMAGE_DIR)
                except Exception:
                    pass
        except Exception:
            pass

    def _migrate_images(self):
        """图片历史迁移:补齐哈希、合并重复内容(只动条目,不碰磁盘文件)。

        孤儿文件清理由 cleanup_orphan_images 延迟执行,不占启动关键路径。
        """
        try:
            changed = False
            for h in self.history:
                if h.get("type") == "image" and not h.get("hash"):
                    p = h.get("content", "")
                    if os.path.exists(p):
                        try:
                            h["hash"] = file_sha1(p)
                            changed = True
                        except Exception:
                            h["hash"] = ""
            # 内容去重:同一哈希只保留第一条
            seen, keep = set(), []
            for h in self.history:
                if h.get("type") != "image":
                    keep.append(h)
                    continue
                p, hsh = h.get("content", ""), h.get("hash", "")
                if not p or not os.path.exists(p):
                    continue            # 丢弃文件已丢失的条目
                if hsh and hsh in seen:
                    continue            # 重复内容,丢弃条目
                seen.add(hsh)
                keep.append(h)
            if len(keep) != len(self.history):
                changed = True
            self.history[:] = keep      # 原地替换,保持前端别名有效
            if changed:
                save_history(self.history,
                             encrypt=bool(self.cfg.get("encrypt_history")))
        except Exception:
            pass

    def _free_record_file(self, rec):
        """清理条目时释放其磁盘资产;文件删除经引用计数复检
        (同内容多条目共享 CAS blob,不再直接删)。返回释放的字节数。"""
        return self._blob_unref(rec)

    def apply_cleanup(self):
        """按 cleanup_days / disk_cap_mb 自动清理(置顶条目一律豁免)。

        - cleanup_days>0:删除 N 天前的未置顶条目(含图片文件);
        - disk_cap_mb>0:图片缓存总量超上限时,从最旧的未置顶图片条目删起。
        启动时与设置页变更时调用;返回 (清理条数, 释放字节)。"""
        n, freed = 0, 0
        days = float(self.cfg.get("cleanup_days", 0) or 0)
        if days > 0:
            cutoff = time.time() - days * 86400
            keep = []
            gone = []
            for h in self.history:
                if not h.get("pinned") and float(h.get("ts", 0) or 0) < cutoff:
                    gone.append(h)
                    n += 1
                else:
                    keep.append(h)
            if gone:
                self.history[:] = keep            # 先出列,引用计数才不含自身
                self._store_del(gone)
                for h in gone:
                    freed += self._free_record_file(h)
        cap = float(self.cfg.get("disk_cap_mb", 0) or 0)
        if cap > 0:
            cap_bytes = cap * 1024 * 1024
            total = 0
            for h in self.history:
                if h.get("type") == "image":
                    try:
                        total += os.path.getsize(h.get("content", ""))
                    except Exception:
                        pass
            imgs = sorted((h for h in self.history
                           if h.get("type") == "image" and not h.get("pinned")),
                          key=lambda h: float(h.get("ts", 0) or 0))
            for h in imgs:
                if total <= cap_bytes:
                    break
                try:
                    self.history.remove(h)      # 先出列(引用计数不含自身)
                except ValueError:
                    continue
                self._store_del([h])
                sz = self._free_record_file(h)
                total -= sz
                freed += sz
                n += 1
        if n:
            self._mark_dirty()
            log("自动清理:删除 %d 条,释放 %s" % (n, _fmt_size(freed)))
        return n, freed

    def cleanup_orphan_images(self, min_age=120.0):
        """删除引用计数为零的孤儿 blob(前端延迟调度,不占启动路径)。

        线程约定:与历史变更同线程(UI 线程)调用,引用快照才一致;
        min_age 秒内新建的文件一律跳过——捕获线程刚落盘、事件尚未入史的
        瞬间文件恰好"无引用",靠时间窗兜底而不是加锁。

        引用计数:库模式按表内全部行(含 deleted 态——撤销还救得回);
        JSON 模式按内存 history + 撤销栈。blobs/ 只认哈希命名的文件;
        旧 images/ 目录仍按整目录托管清理(迁移后残留即孤儿)。
        """
        try:
            refs_path, refs_blob = set(), set()
            if self.store is not None:
                with self.store._lock:
                    rows = self.store.db.execute(
                        "SELECT blob_hash, metadata_json FROM clipboard_items "
                        "WHERE content_type='image'").fetchall()
                for r in rows:
                    if r["blob_hash"]:
                        refs_blob.add(r["blob_hash"])
                    try:
                        p = json.loads(r["metadata_json"] or "{}").get("path")
                        if p:
                            refs_path.add(os.path.normcase(p))
                    except Exception:
                        pass
            live_recs = list(self.history)
            for t in self._txn_list():
                live_recs.extend(t.recs)
            for h in live_recs:
                if h.get("type") == "image":
                    if h.get("hash"):
                        refs_blob.add(h["hash"])
                    if h.get("content"):
                        refs_path.add(os.path.normcase(h["content"]))
            now = time.time()
            removed = 0

            def _unlink_orphan(fp, mtime):
                try:
                    if now - mtime < min_age:
                        return False              # 可能正被捕获流程写入
                except OSError:
                    return False
                try:
                    os.remove(fp)
                    return True
                except Exception:
                    return False

            if os.path.isdir(BLOB_DIR):
                with os.scandir(BLOB_DIR) as buckets:
                    for b in buckets:             # 256 个哈希前缀桶
                        if not b.is_dir():
                            continue
                        with os.scandir(b.path) as it:
                            for e in it:
                                if e.is_dir() or "." in e.name:
                                    continue      # 只认纯哈希名;*.tmp 等跳过
                                if e.name in refs_blob:
                                    continue
                                try:
                                    mt = e.stat().st_mtime
                                except OSError:
                                    continue
                                if _unlink_orphan(e.path, mt):
                                    removed += 1
                        try:
                            os.rmdir(b.path)      # 空桶顺手回收
                        except Exception:
                            pass
            if os.path.isdir(IMAGE_DIR):
                with os.scandir(IMAGE_DIR) as it:     # 扁平目录,无需 os.walk
                    for e in it:
                        if e.is_dir() or not e.name.lower().endswith((".png", ".tmp")):
                            continue
                        fp = os.path.join(IMAGE_DIR, e.name)
                        if os.path.normcase(fp) in refs_path:
                            continue
                        try:
                            mt = e.stat().st_mtime
                        except OSError:
                            continue
                        if _unlink_orphan(fp, mt):
                            removed += 1
            if removed:
                log("孤儿 blob 清理:删除 %d 个引用计数为零的文件" % removed)
        except Exception:
            pass

        # 分片 blob 目录(data/blobs/<2位>/<哈希>)同样清理
        try:
            refs = {h.get("content") for h in self.history
                    if h.get("type") == "image"}
            now = time.time()
            removed = 0
            blob_root = os.path.join(APP_DIR, "data", "blobs")
            if os.path.isdir(blob_root):
                for d in os.scandir(blob_root):
                    if not d.is_dir():
                        continue
                    for e in os.scandir(d.path):
                        if not e.is_file():
                            continue
                        fp = os.path.join(d.path, e.name)
                        if fp in refs:
                            continue
                        try:
                            if now - e.stat().st_mtime < min_age:
                                continue
                        except OSError:
                            continue
                        try:
                            os.remove(fp)
                            removed += 1
                        except Exception:
                            pass
            if removed:
                log("孤儿 blob 清理:删除 %d 个未引用文件" % removed)
        except Exception:
            pass

    def _migrate_private(self):
        """旧版明文私密区在启动时立即升级为 DPAPI 加密(不等待下次写入)。"""
        try:
            if not self.private:
                return
            with open(PRIVATE_FILE, "r", encoding="utf-8") as f:
                if f.read(1) != "[":    # 已是新版加密格式({"dpapi":...})
                    return
            save_private(self.private)
            log("私密区已自动升级为 DPAPI 加密")
        except Exception:
            pass

    def _migrate_config_secrets(self):
        """config.json 需要落盘时写一次:无配置文件(补齐默认值)或存在明文密钥
        (立即封装为 DPAPI 密文,不留明文副本);损坏/备份恢复时不覆盖原文件,
        便于排查——配置会在下次正常保存时自愈。"""
        if not self._cfg_need_save:
            return
        had_key = any(isinstance(s, dict) and s.get("api_key")
                      for s in (self.cfg.get("ai_services") or []))
        try:
            # 先把明文文件就地加密,再轮转/落盘,避免明文残留进 .bak1
            if had_key:
                _migrate_config_file()
            save_config(self.cfg)
            if had_key and win32crypt is None:
                log("config.json 已写入(AI 服务密钥仍为明文:DPAPI 不可用)")
            elif had_key:
                log("config.json 已写入,AI 服务密钥已升级为 DPAPI 密文")
            else:
                log("config.json 已写入")
        except Exception:
            log("config.json 升级写入失败:\n%s" % traceback.format_exc())

    # ---- 历史维护 ----
    def _mark_dirty(self):
        """历史已变:置脏并通知前端(getattr 兜底兼容测试里 __new__ 构造的核心)。"""
        self._dirty = True
        self._gen = getattr(self, "_gen", 0) + 1
        cb = getattr(self, "on_dirty", None)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def flush_history(self):
        """脏历史落盘(原子写);防抖到期 / 退出时调用。

        generation 守卫:序列化/写盘期间若又有变更(_gen 前进),保持脏标记,
        下一轮防抖再写——否则新状态会被误判为已持久化而丢失到下次变更。
        """
        if not getattr(self, "_dirty", False):
            return
        if self.store is not None:
            # 库模式:逐操作写透已落盘(WAL),无需全量序列化
            self._dirty = False
            return
        gen = getattr(self, "_gen", 0)
        with PerfSpan("history_save"):
            save_history(self.history,
                         encrypt=bool(self.cfg.get("encrypt_history")))
        if gen == getattr(self, "_gen", 0):
            self._dirty = False

    def add_item(self, item):
        """捕获入口 = 管线编排器:纯决策都在 pipeline.run_capture,
        本方法只执行副作用(写私密区 / 提升置顶 / 入库 + 落盘 + 通知)。
        每次捕获的阶段轨迹挂 self.last_capture_trace(排障用,见 pipeline.py)。"""
        snap = pipeline.Snapshot.from_item(item)
        outcome, record, trace = pipeline.run_capture(
            snap, self.cfg, self.history,
            sensitive_probe=_sensitive_reason,
            badge_probe=content_badge,
            sha1_fallback=file_sha1)
        self.last_capture_trace = trace
        if outcome.kind == pipeline.SENSITIVE:
            self._add_private(snap.content)
            return
        if outcome.kind == pipeline.PINNED_SKIP:
            return
        if outcome.kind == pipeline.DUPLICATE and outcome.index is not None:
            old = self.history.pop(outcome.index)
            self._store_del([old])
        if self.store is not None:
            if record["type"] == "text":
                badge = content_badge(record["content"])
                if badge:
                    record["_subtype"] = badge      # 子类型缓存(仅库模式写入)
            self.store.insert(record)
        self.history.insert(0, record)
        self._evict()
        self._mark_dirty()
        self._notify_change()

    def _notify_change(self):
        """通知前端历史已变。

        只走事件投递(_post),不在本方法内直接碰 UI:add_item 由前端的
        排空回调触发,而 TrayWorker 也会经同一队列投递,统一走队列可避免
        跨线程访问部件。
        """
        self._post(("change",))

    def _evict(self):
        excess = len(self.history) - self.max_items
        if excess <= 0:
            return
        removed = 0
        to_del = []
        evicted = []
        for i in range(len(self.history) - 1, -1, -1):
            if removed >= excess:
                break
            if not self.history[i].get("pinned"):
                if self.history[i]["type"] == "image":
                    try:
                        os.remove(self.history[i]["content"])
                    except Exception:
                        pass
                to_del.append(i)
                evicted.append(self.history[i])
                removed += 1
        for i in sorted(to_del, reverse=True):
            self.history.pop(i)
        self._store_del(evicted)

    def _add_private(self, content):
        """敏感内容写入私密区(去重、上限 100 条);投递 private 信号供前端
        提示(只带 is_jwt 布尔,不把原文再送一遍队列)。"""
        try:
            for i, s in enumerate(self.private):
                if s == content:
                    self.private.pop(i)
                    break
            self.private.insert(0, content)
            del self.private[100:]
            save_private(self.private)
            log("敏感内容已隔离(仅入私密区,不写入普通历史)")
            self._post(("private", _sniff_jwt(content) is not None))
        except Exception:
            log("私密区写入失败:\n%s" % traceback.format_exc())

    # ---- 列表过滤 ----
    def _search_text(self, h):
        """条目的检索串:casefold 一次后缓存,搜索每敲一个字符不再对全部
        条目重复 str.lower/join(大文本条目尤甚;条目内容入史后不可变)。"""
        cache = getattr(self, "_search_cache", None)
        if cache is None:
            cache = self._search_cache = {}
        key = id(h)
        blob = cache.get(key)
        if blob is None:
            t = h["type"]
            if t == "files":
                blob = os.path.basename(h["content"][0]) + " " \
                    + " ".join(h["content"])
            elif t == "image":
                blob = "图片 截图 %s×%s" % (h.get("w", 0), h.get("h", 0))
            else:
                blob = h["content"]
            blob = blob.casefold()
            if len(cache) > 512:      # 条目淘汰后 id 不至于这么快复用,先整清
                cache.clear()
            cache[key] = blob
        return blob

    @staticmethod
    def time_scope_cutoff(scope):
        """时间筛选档位 → 截止时间戳;"all"/未知档返回 0(不过滤)。
        "today" 取本地当日 00:00。"""
        if scope == "today":
            lt = time.localtime()
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                0, 0, 0, 0, 0, -1))
        secs = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}.get(scope)
        return (time.time() - secs) if secs else 0.0

    def source_apps(self):
        """历史中出现过的来源应用 exe 名(最近在前,去重);供筛选下拉。"""
        seen, out = set(), []
        for h in self.history:
            s = h.get("src")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def filter_view(self, category, query="", src="", since=0.0):
        """按分类 + 关键词 + 来源应用 + 时间范围过滤,返回命中条目在
        history 中的索引列表(内存路径;SQL 路径见 query_view)。
        查询串支持 Search DSL(source:/type:/after:/before:/is:pinned),
        与 query_view 语义逐条一致(parse_search_query 共用)。"""
        dsl = parse_search_query(query)
        q = dsl["text"].strip().casefold()
        src_q = (src or "").strip().lower() or dsl["src"] or ""
        since_eff = max(float(since or 0.0), dsl["after"] or 0.0)
        before_eff = dsl["before"]
        view = []
        for i, h in enumerate(self.history):
            t = h["type"]
            if category == "text" and t != "text":
                continue
            if category == "image" and t != "image":
                continue
            if category == "files" and t != "files":
                continue
            if category == "link" and not (t == "text" and url_info(h["content"])):
                continue
            if dsl["types"] and not self._match_dsl_types(h, dsl["types"]):
                continue
            if dsl["pinned"] is not None and \
                    bool(h.get("pinned")) != dsl["pinned"]:
                continue
            if src_q and (h.get("src") or "").lower() != src_q:
                continue
            if since_eff and float(h.get("ts", 0) or 0) < since_eff:
                continue
            if before_eff and float(h.get("ts", 0) or 0) >= before_eff:
                continue
            if q:
                if q not in self._search_text(h):
                    continue
            view.append(i)
        return view

    _DSL_CATS = {"text", "link", "image", "files"}

    @classmethod
    def _match_dsl_types(cls, rec, types):
        """type: token 组的判定(分类值看条目类型,子类型值看生效子类型)。
        多个 token 全部满足(与 UI 筛选叠加语义一致)。"""
        for t in types:
            if t in cls._DSL_CATS:
                if t == "link":
                    if not (rec.get("type") == "text"
                            and url_info(rec.get("content") or "")):
                        return False
                elif rec.get("type") != t:
                    return False
            elif effective_subtype(rec) != t:
                return False
        return True

    def query_view(self, category, query="", src="", since=0.0, limit=400):
        """视图过滤(SQL 优先):组合过滤在库上执行,返回命中条目在
        self.history 中的下标列表——与 filter_view 同序同义(UI 索引语义
        不变)。查询串支持 Search DSL(source:/type:/after:/before:
        /is:pinned,见 parse_search_query);无库或存在未入库条目时整体
        回退内存路径。"""
        if getattr(self, "store", None) is None:
            return self.filter_view(category, query, src=src, since=since)
        if any(h.get("_db") is None for h in self.history):
            return self.filter_view(category, query, src=src, since=since)
        dsl = parse_search_query(query)
        src_q = (src or "").strip().lower() or dsl["src"] or ""
        since_eff = max(float(since or 0.0), dsl["after"] or 0.0)
        ctype = {"text": "text", "image": "image",
                 "files": "files"}.get(category)
        recs, _total = self.store.query_page(
            content_type=ctype, source_app=src_q or None,
            since=since_eff, until=dsl["before"] or None,
            q=dsl["text"] or None,
            pinned=dsl["pinned"], limit=max(int(limit), 1))
        if category == "link":
            recs = [r for r in recs
                    if r.get("type") == "text" and url_info(r["content"])]
        if dsl["types"]:
            recs = [r for r in recs if self._match_dsl_types(r, dsl["types"])]
        pos = {h["_db"]: i for i, h in enumerate(self.history)}
        out, seen = [], set()
        for r in recs:
            i = pos.get(r.get("_db"))
            if i is not None and i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def set_type_override(self, rec, value):
        """用户纠正条目类型(None=恢复自动)。仅当前记录,不做任何学习;
        生效优先级:type_override > 自动识别(effective_subtype)。"""
        if value is not None and value not in TYPE_OVERRIDES:
            return
        if value:
            rec["type_override"] = value
        else:
            rec.pop("type_override", None)
        if self.store is not None and rec.get("_db") is not None:
            self.store.set_type_override(rec["_db"], value or None)
        self._mark_dirty()

    # ---- 回写 / 粘贴 ----
    def copy_record(self, rec):
        """仅复制原始内容,不模拟粘贴、不移动历史条目。"""
        if rec["type"] == "files":
            existing = [p for p in rec["content"] if os.path.exists(p)]
            if not existing:
                return False
            rec = dict(rec, content=existing)
        self._ignore_clip_until = time.monotonic() + 0.5
        return self._set_clipboard(rec)

    def paste_record(self, rec, force=False, target_hwnd=None):
        """写回剪贴板 + 自动粘贴排队 + 置顶到最近。文件全部丢失时返回 False。
        force=True 无条件派发回贴(快捷粘贴浮层:用户按 Enter 的意图就是粘贴)。
        target_hwnd = 意图目标窗口(粘贴事务校验用,None = 旧语义不校验)。"""
        if rec["type"] == "files":
            existing = [p for p in rec["content"] if os.path.exists(p)]
            if not existing:
                return False
            rec["content"] = existing
        # 时间窗抑制本次自写入触发的剪贴板回调
        self._ignore_clip_until = time.monotonic() + 0.5
        self._set_clipboard(rec)
        if force or self.cfg.get("auto_paste", False):
            self.worker.cmd_q.put(
                ("paste", int(target_hwnd or 0), _get_clip_seq()))
        try:
            self.history.remove(rec)
            self.history.insert(0, rec)
            if self.store is not None:
                self.store.upsert_promote(rec)    # 最近使用 → seq 提升到最前
            self._mark_dirty()
        except Exception:
            pass
        return True

    def paste_text(self, content, force=False, target_hwnd=None):
        """文本写回剪贴板;force=True 无条件派发回贴(用户点了明确的
        「复制并粘贴」类按钮),否则跟随 auto_paste 开关(默认关:模拟
        Ctrl+V 会把内容打进当前聚焦窗口,不应是复制动作的默认副作用)。"""
        self._ignore_clip_until = time.monotonic() + 0.5
        self._set_clipboard({"type": "text", "content": content})
        if force or self.cfg.get("auto_paste", False):
            self.worker.cmd_q.put(
                ("paste", int(target_hwnd or 0), _get_clip_seq()))

    def copy_text(self, content):
        """仅写剪贴板(不排回贴队列):查看器分段复制等小动作用,
        不往用户此刻聚焦的窗口里塞 Ctrl+V。"""
        self._ignore_clip_until = time.monotonic() + 0.5
        self._set_clipboard({"type": "text", "content": content})

    def _set_clipboard(self, rec):
        for _ in range(12):
            try:
                win32clipboard.OpenClipboard(self.worker.hwnd)
                try:
                    win32clipboard.EmptyClipboard()
                    if rec["type"] == "text":
                        win32clipboard.SetClipboardData(
                            win32clipboard.CF_UNICODETEXT, rec["content"])
                        # 富文本格式并放(HTML Format / RTF):粘贴到 Word/浏览器/
                        # Excel 保留排版;单格式失败不影响主文本
                        for key, fmt in self.RICH_FORMATS:
                            val = rec.get(key)
                            if not val:
                                continue
                            try:
                                cf = self._reg_fmt(fmt)
                                if cf:
                                    win32clipboard.SetClipboardData(
                                        cf, val.encode("latin-1"))
                            except Exception:
                                pass
                    elif rec["type"] == "files":
                        win32clipboard.SetClipboardData(
                            win32clipboard.CF_HDROP, build_dropfiles(rec["content"]))
                    else:
                        win32clipboard.SetClipboardData(
                            win32clipboard.CF_DIB, png_to_dib(rec["content"]))
                finally:
                    win32clipboard.CloseClipboard()
                return True
            except Exception:
                time.sleep(0.06)
        return False

    # ---- 条目状态操作 ----
    def toggle_pin_at(self, hi):
        rec = self.history[hi]
        rec["pinned"] = not rec.get("pinned", False)
        if self.store is not None and rec.get("_db") is not None:
            self.store.update_pinned(rec["_db"], rec["pinned"])
        self._mark_dirty()

    def delete_at(self, hi):
        """硬删除单条:行立即物理删;图片文件经引用计数复检(refcount==0 才删)。"""
        rec = self.history.pop(hi)
        self._store_del([rec])
        self._blob_unref(rec)
        self._mark_dirty()
        return rec

    def clear_unpinned(self):
        """清空未置顶条目(置顶保留);文件删除走引用计数。"""
        removed = [h for h in self.history if not h.get("pinned")]
        self.history[:] = [h for h in self.history if h.get("pinned")]
        self._store_del(removed)
        for h in removed:
            self._blob_unref(h)
        self._mark_dirty()

    # ---- 可撤销删除(Transaction 栈) ----
    _TXN_MAX = 20                  # 撤销栈最多保留 20 笔事务(弹底即不可再撤)
    _TXN_GC_GRACE = 45.0           # deleted 态行的 GC 宽限(秒);须远大于 UI 6s 撤销窗

    def _txn_list(self):
        """撤销栈(惰性初始化,__new__ 直造的核心同样可用)。"""
        if self._txn_stack is None:
            self._txn_stack = []
        return self._txn_stack

    def _txn_run(self, txn):
        """执行一笔事务并入撤销栈;栈满弹底(最老的批次先失去撤销机会)。"""
        stack = self._txn_list()
        gone = txn.commit()
        if gone:
            stack.append(txn)
            while len(stack) > self._TXN_MAX:
                stack.pop(0)
            self._schedule_gc()
        return gone

    def _txn_stack_refs(self, path, blob):
        """撤销栈中的事务是否仍引用该文件/哈希(撤销窗口内文件不能删)。"""
        for t in self._txn_list():
            for r in t.recs:
                if r.get("type") != "image":
                    continue
                if (blob and r.get("hash") == blob) or r.get("content") == path:
                    return True
        return False

    def _blob_unref(self, rec):
        """条目被物理移除后释放其磁盘资产:图片文件只在引用计数归零时删除
        (同内容多条目共享一个 CAS blob;库模式按表内行计数,含 deleted 态
        ——那些行还可能被撤销救回;JSON 模式按内存引用计数)。返回释放字节数。"""
        if rec.get("type") != "image":
            return 0
        path = rec.get("content") or ""
        blob = rec.get("hash") or ""
        if self.store is not None:
            if blob and self.store.blob_refcount(blob) > 0:
                return 0
        else:
            live = sum(1 for h in self.history
                       if h.get("type") == "image"
                       and ((blob and h.get("hash") == blob)
                            or h.get("content") == path))
            if live or self._txn_stack_refs(path, blob):
                return 0
        freed = 0
        try:
            freed = os.path.getsize(path)
        except Exception:
            freed = 0
        try:
            os.remove(path)
        except Exception:
            pass
        return freed

    def _schedule_gc(self):
        """删除后安排一次延迟 GC:宽限期略大于调度延迟,时点过了自然幂等。"""
        if self.store is None:
            return
        t = threading.Timer(self._TXN_GC_GRACE + 5.0, self._collect_garbage)
        t.daemon = True
        t.start()

    def _collect_garbage(self):
        """物理删除宽限期已过的 deleted 行,再对涉及 blob 复检引用计数。"""
        if self.store is None:
            return
        try:
            gone = self.store.collect_garbage(self._TXN_GC_GRACE)
        except Exception:
            return
        freed = 0
        for _dbid, bhash in gone:
            if not bhash:
                continue
            if self.store.blob_refcount(bhash) == 0:
                p = blob_path(bhash)
                if p and os.path.exists(p):
                    try:
                        freed += os.path.getsize(p)
                        os.remove(p)
                    except Exception:
                        pass
        # 撤销栈里已全被 GC 的旧事务清掉(rollback 有重建防御,这里只是收尾)
        gone_ids = {g[0] for g in gone}
        self._txn_stack = [t for t in self._txn_list()
                           if any(r.get("_db") not in gone_ids for r in t.recs)]
        if gone and freed:
            log("事务 GC:回收 %d 行,释放 %s" % (len(gone), _fmt_size(freed)))

    def soft_delete_at(self, hi):
        """可撤销的单条删除(行进 deleted 态,文件不动)。"""
        txn = DeleteTransaction(self, [self.history[hi]])
        self._txn_run(txn)
        return txn.recs[0]

    def batch_soft_delete(self, recs):
        """可撤销的批量删除(按记录身份匹配);返回实际删除条数。"""
        ids = {id(r) for r in recs}
        removed = [h for h in self.history if id(h) in ids]
        if not removed:
            return 0
        self._txn_run(DeleteTransaction(self, removed))
        return len(removed)

    # ---- 导出 / 导入(备份与迁移) ----
    def export_payload(self, include_images=False):
        """历史导出为可 JSON 序列化的载荷。图片默认跳过(勾选则 base64 内嵌);
        文件条目只导出仍存在的路径。"""
        items = []
        for h in self.history:
            it = {"type": h["type"], "ts": h.get("ts", 0),
                  "src": h.get("src", ""), "pinned": bool(h.get("pinned"))}
            if h["type"] == "image":
                if not include_images:
                    continue
                try:
                    blob = Path(h["content"]).read_bytes()
                except Exception:
                    continue
                it["name"] = os.path.basename(h["content"])
                it["image_b64"] = base64.b64encode(blob).decode("ascii")
                it["w"], it["h"] = h.get("w", 0), h.get("h", 0)
            elif h["type"] == "files":
                live = [p for p in (h.get("content") or []) if os.path.exists(p)]
                if not live:
                    continue
                it["content"] = live
            else:
                if not str(h.get("content") or "").strip():
                    continue
                it["content"] = h["content"]
                for k in ("html", "rtf"):
                    if h.get(k):
                        it[k] = h[k]
            items.append(it)
        return {"app": "WinClipboard", "version": 1,
                "exported_at": time.time(), "include_images": include_images,
                "items": items}

    def import_payload(self, payload):
        """导入 export_payload 载荷:合并去重(add_item 同款键),图片按像素
        哈希落盘重建,ts/置顶随条目还原。返回 (新增, 跳过)。"""
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("不是有效的 WinClipboard 导出文件")
        added = skipped = 0
        for it in items:
            if not isinstance(it, dict):
                skipped += 1
                continue
            t, ts = it.get("type"), float(it.get("ts") or time.time())
            if t == "text" and isinstance(it.get("content"), str) and it["content"].strip():
                rich = {k: it[k] for k in ("html", "rtf") if it.get(k)}
                item = ("text", it["content"], rich, it.get("src", ""))
            elif t == "files" and isinstance(it.get("content"), list):
                live = [p for p in it["content"] if os.path.exists(p)]
                if not live:
                    skipped += 1
                    continue
                item = ("files", live, it.get("src", ""))
            elif t == "image" and it.get("image_b64"):
                try:
                    raw = base64.b64decode(it["image_b64"])
                    img = Image.open(io.BytesIO(raw))
                    hsh = pixel_sha256(img)
                    # CAS 分片布局;已存在(同内容)→ 跳过落盘
                    path = blob_full_path(APP_DIR, hsh)
                    if not os.path.exists(path):
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        tmp = path + ".tmp"
                        Path(tmp).write_bytes(raw)
                        os.replace(tmp, path)
                    item = ("image", {"path": path, "w": it.get("w", 0),
                                      "h": it.get("h", 0), "hash": hsh},
                            it.get("src", ""))
                except Exception:
                    skipped += 1
                    continue
            else:
                skipped += 1
                continue
            before = len(self.history)
            self.add_item(item)     # 去重置顶;敏感文本按护栏进私密区
            if len(self.history) != before:
                added += 1
                self.history[0]["ts"] = ts          # 还原原始时间
                if it.get("pinned"):
                    self.history[0]["pinned"] = True
            else:
                skipped += 1
        return added, skipped

    def export_portable_payload(self, include_images=False,
                                include_private=False):
        """可迁移备份载荷:普通导出条目 + 可选私密区(默认不含)。
        私密内容只在**加密容器**里才允许导出(见 portable_backup_bytes)。"""
        payload = self.export_payload(include_images=include_images)
        payload["private"] = list(self.private) if include_private else []
        return payload

    def import_portable_payload(self, payload):
        """导入加密备份载荷:历史走 import_payload(自动去重),
        私密条目合并入私密区(去重)。返回 (新增, 跳过, 私密新增)。"""
        priv = payload.get("private")
        payload = dict(payload)
        payload.pop("private", None)
        added, skipped = self.import_payload(payload)
        n_priv = 0
        if isinstance(priv, list):
            for s in priv:
                if isinstance(s, str) and s and s not in self.private:
                    self.private.append(s)
                    n_priv += 1
            if n_priv:
                save_private(self.private)
        return added, skipped, n_priv

    # ---- 可迁移加密备份(AES-256-GCM + PBKDF2,与 DPAPI 无关)----

    def soft_clear_unpinned(self):
        """可撤销的清空(置顶保留,行进 deleted 态);返回被移除的条目。"""
        removed = [h for h in self.history if not h.get("pinned")]
        if not removed:
            return removed
        self._txn_run(DeleteTransaction(self, removed))
        return removed

    def undo_delete(self):
        """撤销最近一笔删除事务:条目按原相对顺序回到列表最前。"""
        stack = self._txn_list()
        if not stack:
            return []
        return stack.pop().rollback()

    def save_all(self):
        self.flush_history()
        save_private(self.private)

    def set_history_encryption(self, on):
        """设置页「历史加密」开关的即时双向切换。

        开启:当前历史立即写为加密 history.json,停用库写透(库文件保留,
              关闭加密时无损切回);
        关闭:启用库;库为空则把当前历史单事务迁入,并把逐条 _db 回填到
              现有记录对象(UI 引用的记录身份不变),随后删除明文 JSON
              快照避免双源。"""
        on = bool(on)
        self.cfg["encrypt_history"] = on
        if on:
            save_history(self.history, encrypt=True)
            self._txn_stack = []      # 库已停用,旧事务按 JSON 语义无意义
            if self.store is not None:
                try:
                    self.store.close()
                except Exception:
                    pass
                self.store = None
            return
        if self.store is None:
            try:
                st = ClipboardStore(DB_FILE)
                # 加密期间历史可能已变化:整库重建,以当前内存历史为准
                st.reset()
                st.migrate_from_list(list(reversed(self.history)))
                with st._lock:
                    ids = [r["id"] for r in st.db.execute(
                        "SELECT id FROM clipboard_items "
                        "WHERE state='active' ORDER BY seq DESC").fetchall()]
                for rec, dbid in zip(self.history, ids):
                    rec["_db"] = dbid
                self.store = st
                # 重建后行 id 全变,指向旧行的撤销事务一并作废
                self._txn_stack = []
                try:
                    os.remove(HISTORY_FILE)
                except Exception:
                    pass
            except Exception:
                self.store = None
                log("切回库模式失败,继续 JSON 持久化:\n%s"
                    % traceback.format_exc())

    # ---- 开机自启 ----
    def is_autostart(self):
        try:
            key = win32api.RegOpenKeyEx(
                win32con.HKEY_CURRENT_USER, RUN_KEY, 0, win32con.KEY_READ)
            try:
                win32api.RegQueryValueEx(key, APP_NAME)
                return True
            finally:
                win32api.RegCloseKey(key)
        except Exception:
            return False

    def toggle_autostart(self):
        if self.is_autostart():
            try:
                key = win32api.RegOpenKeyEx(
                    win32con.HKEY_CURRENT_USER, RUN_KEY, 0, win32con.KEY_SET_VALUE)
                win32api.RegDeleteValue(key, APP_NAME)
                win32api.RegCloseKey(key)
            except Exception:
                pass
        else:
            # sys.argv[0] 是实际运行的入口脚本(两版各自正确)
            val = '"{}" "{}"'.format(self._pythonw(), os.path.abspath(sys.argv[0]))
            try:
                key = win32api.RegOpenKeyEx(
                    win32con.HKEY_CURRENT_USER, RUN_KEY, 0, win32con.KEY_SET_VALUE)
                win32api.RegSetValueEx(key, APP_NAME, 0, win32con.REG_SZ, val)
                win32api.RegCloseKey(key)
            except Exception:
                pass

    @staticmethod
    def _pythonw():
        d = os.path.dirname(sys.executable)
        cand = os.path.join(d, "pythonw.exe")
        return cand if os.path.exists(cand) else sys.executable


# ============================================================
# 图片标注辅助(模块级函数,供 winclipboard_qt.py 的 Qt 版图片编辑器使用)
# ============================================================
def _load_font(size):
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _draw_text(img, pos, text, font, color=(255, 255, 255)):
    # 光色配深描边、深色配浅描边,保证任何底图上可读
    halo = (0, 0, 0) if sum(color[:3]) >= 384 else (255, 255, 255)
    d = ImageDraw.Draw(img)
    x, y = pos
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        d.text((x + dx, y + dy), text, font=font, fill=halo)
    d.text((x, y), text, font=font, fill=color)


def _draw_arrow(img, p0, p1, color=(255, 40, 40), width=4):
    d = ImageDraw.Draw(img)
    d.line((p0, p1), fill=color, width=width)
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    L, a = max(12, width * 4), 0.5
    x1, y1 = p1
    p2 = (x1 - L * math.cos(ang - a), y1 - L * math.sin(ang - a))
    p3 = (x1 - L * math.cos(ang + a), y1 - L * math.sin(ang + a))
    d.polygon((p1, p2, p3), fill=color)


def _mosaic(img, box):
    region = img.crop(box)
    if region.width < 1 or region.height < 1:
        return
    small = region.resize((max(1, region.width // 14), max(1, region.height // 14)),
                          RESAMPLE)
    img.paste(small.resize(region.size, Image.NEAREST), (box[0], box[1]))


# ============================================================
# Apple 风格 UI 基础(圆角绘制 / 时间 / 面板底图 / 导航 / 底栏)
# ============================================================
def _rel_time(ts):
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % int(d // 60)
    if d < 86400:
        return "%d 小时前" % int(d // 3600)
    if d < 172800:
        return "昨天"
    if d < 86400 * 7:
        return "%d 天前" % int(d // 86400)
    return time.strftime("%m月%d日", time.localtime(ts))


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%d B" % n


_URL_RE = re.compile(r"^https?://", re.I)


def url_info(text):
    """识别 URL 条目,返回 (host, url) 或 None。"""
    t = text.strip()
    if not _URL_RE.match(t):
        return None
    try:
        p = urlsplit(t)
        host = p.hostname or t
    except Exception:
        return None
    return (host, t)


def url_inspect(text):
    """URL 结构化拆解(Inspector 数据源):返回 dict 或 None。

    {"scheme","host","port","path","query":[(k,v),…],"fragment"} —— query
    保持出现顺序,值保持原文(不解码,复制出来与所见一致);解码动作由
    查看器按需对整体做 unquote。"""
    t = text.strip()
    if "\n" in t or not _URL_RE.match(t):
        return None
    try:
        p = urlsplit(t)
        if not p.netloc:
            return None
        query = []
        import urllib.parse as _up
        for k, v in _up.parse_qsl(p.query, keep_blank_values=True):
            query.append((k, v))
        return {"scheme": p.scheme, "host": p.hostname or "",
                "port": p.port, "path": p.path or "/",
                "query": query, "fragment": p.fragment}
    except Exception:
        return None


def jwt_inspect(text):
    """JWT 深度拆解(Inspector 数据源):在 _sniff_jwt 之上补充时间字段的
    本地化解析。返回 {"header","payload","sig_chars","iat","exp","nbf"} 或
    None;iat/exp/nbf 为 int 时间戳(缺省 None)。不验签。"""
    got = _sniff_jwt(text or "")
    if got is None:
        return None
    head, pay = got
    segs = text.strip().split(".")

    def _ts(key):
        v = pay.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return int(v)

    return {"header": head, "payload": pay, "sig_chars": len(segs[2]),
            "iat": _ts("iat"), "exp": _ts("exp"), "nbf": _ts("nbf")}


def b64_decode_auto(text):
    """Base64 解码(Inspector 数据源):复用 _sniff_base64 的全部护栏。
    返回 (kind, 解码字节) 或 None;kind ∈ text/image。"""
    return _sniff_base64((text or "").strip())


def content_badge(text):
    """卡片徽章轻量探测:只走廉价分支(JSON/JWT/Base64 嗅探),不跑
    pygments 兜底——卡片绑定是每次刷新的全量路径,贵分支进不来。
    返回 "JWT"/"Base64"/None(URL 已有 link 专属卡片,不在此列)。"""
    if not isinstance(text, str):         # files 条目 content 是路径列表
        return None
    t = text.lstrip()
    if not t:
        return None
    try:
        json.loads(t)
        return None                       # JSON 在卡片 meta 已有既有呈现路径
    except Exception:
        pass
    if _sniff_jwt(t):
        return "JWT"
    if _sniff_base64(t):
        return "Base64"
    return None


# ---- Diff(内容对比;纯逻辑,查看器渲染) ----
def unified_diff_lines(a, b, n=3, cap=4000):
    """unified diff 行列表(difflib;含 @@ 头)。超 cap 行截断并标注,
    防十万行日志把渲染层拖死。"""
    import difflib
    lines = list(difflib.unified_diff((a or "").splitlines(),
                                      (b or "").splitlines(),
                                      lineterm="", n=n))
    if len(lines) > cap:
        lines = lines[:cap] + ["…(差异行数过多,已截断)"]
    return lines


def json_semantic_diff(a, b, max_changes=200):
    """JSON 语义对比:忽略键序/空白/缩进,只比语义。

    返回 (ok, changes):ok=False = 任一方不是合法 JSON(调用方回退文本
    diff);changes 为空列表 = 语义相等。每条 (path, kind, old, new),
    kind ∈ change/add/del(add 时 old=None,del 时 new=None);
    path 形如 $.users[0].name;值序列化截断到 120 字符。"""
    try:
        oa, ob = json.loads(a), json.loads(b)
    except Exception:
        return False, []
    out = []

    def fmt(v):
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 120 else s[:120] + "…"

    def kp(key):
        if isinstance(key, str) and re.match(r"\w+$", key):
            return "." + key
        return "[%s]" % json.dumps(key, ensure_ascii=False)

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def walk(x, y, path):
        if len(out) > max_changes:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in x:
                if k not in y:
                    out.append((path + kp(k), "del", fmt(x[k]), None))
                else:
                    walk(x[k], y[k], path + kp(k))
            for k in y:
                if k not in x:
                    out.append((path + kp(k), "add", None, fmt(y[k])))
        elif isinstance(x, list) and isinstance(y, list):
            for i in range(max(len(x), len(y))):
                if i >= len(x):
                    out.append(("%s[%d]" % (path, i), "add", None, fmt(y[i])))
                elif i >= len(y):
                    out.append(("%s[%d]" % (path, i), "del", fmt(x[i]), None))
                else:
                    walk(x[i], y[i], "%s[%d]" % (path, i))
        else:
            # 布尔与数字不同型(bool 是 int 子类,单列);数值跨 int/float 视为同型
            same = (x == y and isinstance(x, bool) == isinstance(y, bool)) \
                or (num(x) and num(y) and x == y)
            if not same:
                out.append((path, "change", fmt(x), fmt(y)))

    walk(oa, ob, "$")
    if len(out) > max_changes:
        out.append(("…", "note", "差异超过 %d 处,其余已省略" % max_changes,
                    None))
    return True, out


# ============================================================
# 文本语言识别 + JSON 重排(查看器用;pygments 为可选依赖,缺失时退化为纯文本)
# ============================================================
try:
    from pygments.lexers import guess_lexer as _pg_guess_lexer
except Exception:
    _pg_guess_lexer = None

# 查看器语言菜单:key 兼容 pygments lexer 别名,直接用于 get_lexer_by_name
VIEW_LANGS = [
    ("auto", "自动识别"), ("text", "纯文本"), ("json", "JSON"),
    ("python", "Python"), ("javascript", "JavaScript"), ("typescript", "TypeScript"),
    ("html", "HTML"), ("xml", "XML"), ("css", "CSS"), ("sql", "SQL"),
    ("markdown", "Markdown"), ("csv", "CSV 表格"), ("yaml", "YAML"), ("ini", "INI"),
    ("shell", "Shell"), ("bat", "批处理"), ("java", "Java"), ("c", "C/C++"),
    ("csharp", "C#"), ("go", "Go"), ("rust", "Rust"), ("php", "PHP"),
    ("diff", "Diff"),
]

# pygments lexer 别名 → VIEW_LANGS key(guess_lexer 返回值归一化用)
_LANG_ALIAS = {
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "json": "json", "json5": "json", "jsonc": "json",
    "html": "html", "xml": "xml", "css": "css", "scss": "css", "less": "css",
    "sql": "sql", "markdown": "markdown", "yaml": "yaml", "ini": "ini",
    "shell": "shell", "bash": "shell", "powershell": "bat", "batch": "bat",
    "java": "java", "c": "c", "cpp": "c", "csharp": "csharp", "go": "go",
    "rust": "rust", "php": "php", "diff": "diff", "udiff": "diff",
}


ContentInfo = namedtuple(
    "ContentInfo", "category subtype lang confidence metadata reasons",
    defaults=({}, ()))
# (category, subtype, lang, confidence, metadata, reasons) —— 见 detect_content
# reasons:判定依据列表(排障/测试用;UI 只显示 subtype,不展示置信度细节)


def _sniff_jwt(t):
    """JWT 嗅探:恰好三段 base64url(兼容混用标准字母表的 + /),头段可解出
    含 alg 的 JSON dict。返回 (header, payload) 或 None。只解码不验签——
    剪贴板场景拿不到密钥,验签语义由查看器 UI 明示。"""
    s = t.strip()
    parts = s.split(".")
    if len(parts) != 3 or len(s) < 24:
        return None
    for seg in parts:
        if not seg or not re.match(r"\A[A-Za-z0-9_+/-]+\Z", seg):
            return None

    def _dec(seg):
        try:
            return json.loads(
                base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
                .decode("utf-8"))
        except Exception:
            return None

    head = _dec(parts[0])
    if not isinstance(head, dict) or "alg" not in head:
        return None
    pay = _dec(parts[1])
    if not isinstance(pay, dict):
        return None
    return head, pay


_B64_STD_RE = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")
_B64URL_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]+\Z")
_DATAURI_RE = re.compile(r"\Adata:[^;,]*;base64,", re.I)
# 解码上限:32MB 编码文本(≈24MB 原始);再大不做嗅探(宁可漏判不卡 UI)
_B64_SNIFF_CAP = 32 * 1024 * 1024


def _sniff_base64(t):
    """Base64 嗅探:返回 (kind, 解码字节) 或 None;kind ∈ text/image。

    护栏:去空白/data:前缀后 ≥40 字符;纯十六进制(哈希/长数字)排除;
    只认「解码出合法 UTF-8 文本(无 NUL)或图片魔数」两种,其余按非
    Base64 处理——许可证密钥/无空格长单词解码通常是乱码二进制,宁可漏判。"""
    s = t.strip()
    m = _DATAURI_RE.match(s)
    if m:
        s = s[m.end():]
    s = "".join(s.split())
    if len(s) < 40 or len(s) > _B64_SNIFF_CAP or _HEX_RE.match(s):
        return None
    if "-" in s or "_" in s:
        if not _B64URL_RE.match(s):
            return None
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            return None
    else:
        if not _B64_STD_RE.match(s):
            return None
        try:
            raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
        except Exception:
            return None
    if raw[:4] in (b"\x89PNG", b"GIF8") or raw[:3] == b"\xff\xd8\xff" or \
            (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        return "image", raw
    if b"\x00" in raw:
        return None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text", raw


def detect_content(text):
    """内容识别管线(Content Intelligence 核心)。

    返回 ContentInfo(category, subtype, lang, confidence, metadata):
      category      text / code / structured_text / markup / url / encoded
      subtype       具体形态:json / csv / jwt / base64 / web_url / python …
      lang          兼容旧 detect_lang 的语言 key(VIEW_LANGS 成员)
      confidence    1.0 解析验证 / ≥0.99 强形态 / 0.9 特征正则 /
                    0.8 代码特征 / 0.6 pygments 猜测;不足 0.9 的智能视图
                    不自动启用(查看器回退原文,防「聪明过头」)
      metadata      识别过程免费得到的附加信息(csv: {"delim": …} 等)

    优先级:严格 JSON > URL(单行) > JWT > Base64 > XML/HTML 前缀 >
    Markdown 围栏/标题 > CSV 嗅探 > 廉价代码强特征 > pygments 采样猜测。
    """
    t = (text or "").lstrip()
    if not t:
        return ContentInfo("text", "plain", "text", 1.0, {}, ["空内容"])
    try:
        json.loads(t)
        return ContentInfo("structured_text", "json", "json", 1.0,
                           {"valid": True}, ["完整 JSON 解析通过"])
    except Exception:
        pass
    stripped = text.strip()
    if "\n" not in stripped and _URL_RE.match(stripped):
        return ContentInfo("url", "web_url", "text", 0.99, {},
                           ["单行且匹配 URL 形态"])
    jwt = _sniff_jwt(t)
    if jwt is not None:
        return ContentInfo("encoded", "jwt", "text", 0.99, {},
                           ["三段结构", "base64url 有效", "header JSON 有效"])
    b64 = _sniff_base64(t)
    if b64 is not None:
        kind, raw = b64
        return ContentInfo("encoded", "base64", "text", 0.95,
                           {"b64_kind": kind, "b64_len": len(raw)},
                           ["字符集/长度/填充符合 Base64"])
    head = t[:4000]
    if head[:5].lower() == "<?xml":
        return ContentInfo("structured_text", "xml", "xml", 1.0, {}, ["<?xml 声明"])
    if re.match(r"<(!doctype|html)\b", head, re.I):
        return ContentInfo("markup", "html", "html", 0.9, {}, ["<!doctype/html> 前缀"])
    if "\n```" in t or t.startswith("```") or re.search(r"(?m)^#{1,6} \S", head):
        return ContentInfo("markup", "markdown", "markdown", 0.9, {},
                           ["围栏代码块或 ATX 标题"])
    delim = csv_sniff(text)
    if delim:
        return ContentInfo("structured_text", "csv", "csv", 0.93,
                           {"delim": delim}, ["csv_sniff 定界符一致"])
    # 廉价强特征:剪贴板里多是短片段,pygments 对短文本置信度低,先用正则兜住
    if t[:2] == "#!":
        if "python" in t[:80]:
            return ContentInfo("code", "python", "python", 0.8, {}, ["廉价代码强特征"])
        if re.search(r"\b(bash|zsh|sh|dash)\b", t[:80]):
            return ContentInfo("code", "shell", "shell", 0.8, {}, ["廉价代码强特征"])
    if re.search(r"(?m)^(import\s+[\w.]+|from\s+[\w.]+\s+import\b)", head) and \
            not re.search(r"(?m)^import\s+[\w{}\s,]+\s+from\s+['\"]", head):
        return ContentInfo("code", "python", "python", 0.8, {}, ["廉价代码强特征"])
    if re.search(r"(?mi)\b(const|let|var)\s+[\w$]+\s*=|function\s*[\w$]*\s*\(|=>"
                 r"|console\.(log|warn|error)\b|document\.getElement", head):
        return ContentInfo("code", "javascript", "javascript", 0.8, {}, ["廉价代码强特征"])
    if re.search(r"(?m)^[\w\s.,#>-]+\{[^{}]*[\w-]+\s*:[^{}]*;[^{}]*\}", head):
        return ContentInfo("code", "css", "css", 0.8, {}, ["廉价代码强特征"])
    if re.search(r"(?mi)\b(select\s+.+\s+from\b|insert\s+into\b|update\s+\w+\s+set\b"
                 r"|delete\s+from\b|create\s+(table|database|index)\b|alter\s+table\b"
                 r"|with\s+\w+\s+as\s*\()", head):
        return ContentInfo("code", "sql", "sql", 0.8, {}, ["廉价代码强特征"])
    if re.search(r"(?m)^\s*(def\s+\w+\s*\(|class\s+\w+[:(]|@\w+(\.[\w.]+)?\s*$)",
                 head):
        return ContentInfo("code", "python", "python", 0.8, {}, ["廉价代码强特征"])
    if _pg_guess_lexer is not None:
        try:
            # 采样 2000 字符:guess_lexer 对 20KB 输入需 ~36ms(预览面板每次
            # 选中都会跑),语言判断只需开头特征,采样小窗足够
            lex = _pg_guess_lexer(t[:2000])
            for a in getattr(lex, "aliases", None) or []:
                if a in _LANG_ALIAS:
                    return ContentInfo("code", _LANG_ALIAS[a],
                                       _LANG_ALIAS[a], 0.6, {},
                                       ["pygments 采样猜测(低置信)"])
        except Exception:
            pass
    return ContentInfo("text", "plain", "text", 1.0, {},
                       ["无强形态特征,按纯文本"])


# ============================================================
# Paste Variants(粘贴变换)
# ============================================================
def _tx_json_compact(text):
    try:
        return json.dumps(json.loads(text), ensure_ascii=False,
                          separators=(",", ":"))
    except Exception:
        return None


def _tx_json_format(text):
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        return None


def _tx_url_decode(text):
    from urllib.parse import unquote
    return unquote(text)


def _tx_url_encode(text):
    from urllib.parse import quote
    return quote(text, safe="")


def _tx_escaped(text):
    return json.dumps(text)[1:-1]          # JSON 字符串转义(不带引号)


def _tx_quoted(text):
    return json.dumps(text, ensure_ascii=False)   # 引号包裹 + 转义


# (键, 菜单名, 变换函数);函数返回 None = 该内容不适用(菜单禁用)
TRANSFORMS = (
    ("plain", "纯文本", lambda t: t),
    ("escaped", "转义(\\n \\\" …)", _tx_escaped),
    ("quoted", "引号包裹", _tx_quoted),
    ("json_compact", "JSON 压缩", _tx_json_compact),
    ("json_format", "JSON 格式化", _tx_json_format),
    ("url_decode", "URL 解码", _tx_url_decode),
    ("url_encode", "URL 编码", _tx_url_encode),
    ("lower", "小写", lambda t: t.lower()),
    ("upper", "大写", lambda t: t.upper()),
)
TRANSFORM_BY_KEY = {k: (name, fn) for k, name, fn in TRANSFORMS}


def transform_text(text, key):
    """按变换键转换文本;不适用(如非 JSON 走 JSON 变换)返回 None。"""
    entry = TRANSFORM_BY_KEY.get(key)
    if entry is None or text is None:
        return None
    try:
        return entry[1](text)
    except Exception:
        return None


# ============================================================
# 类型纠正(用户手动覆盖自动识别;仅当前记录,不学习)
# ============================================================
# 允许的覆盖值(None = 自动)
TYPE_OVERRIDES = ("text", "json", "jwt", "base64", "url", "csv", "xml",
                  "markdown", "code")


def effective_subtype(rec):
    """条目的生效子类型:用户纠正优先,否则轻量探测(content_badge)。"""
    ov = rec.get("type_override")
    if ov:
        return ov
    if rec.get("type") != "text":
        return None
    return content_badge(rec.get("content") or "")


# ============================================================
# Search DSL:source:vscode type:json after:2026-09-01 is:pinned 关键词
# ============================================================
DSL_TYPE_VALUES = {
    "text", "link", "image", "files",           # 分类
    "json", "jwt", "base64", "url", "csv", "xml", "markdown", "code",  # 子类型
}


def _dsl_date_to_ts(v):
    """after:/before: 的值 → 时间戳:YYYY-MM-DD / YYYY/MM/DD / Nd / Nw;
    不识别返回 None。"""
    v = v.strip().lower().rstrip("s")
    m = re.fullmatch(r"(\d+)\s*(d|w)", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return time.time() - n * (604800 if unit == "w" else 86400)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return time.mktime(time.strptime(v, fmt))
        except ValueError:
            continue
    return None


def parse_search_query(q):
    """搜索串 → 结构化过滤。识别 token:source:/src: type:/kind:
    after:/since: before:/until: is:pinned is:unpinned;不认识的
    xxx:yyy 与普通词一样并入自由文本(宁漏判不误判)。返回 dict:
    {src, types:set, after:float, before:float, pinned:bool|None, text}"""
    out = {"src": None, "types": set(), "after": None, "before": None,
           "pinned": None, "text": []}
    for tok in (q or "").split():
        low = tok.lower()
        if low == "is:pinned":
            out["pinned"] = True
            continue
        if low == "is:unpinned":
            out["pinned"] = False
            continue
        m = re.fullmatch(r"(source|src|type|kind|after|since):(.+)", low)
        if m:
            key, val = m.group(1), m.group(2)
            if key in ("source", "src"):
                out["src"] = val if val.endswith(".exe") else val + ".exe"
                continue
            if key in ("type", "kind"):
                if val in DSL_TYPE_VALUES:
                    out["types"].add(val)
                    continue
                # 未知类型值:当自由词(不吞用户的词)
            if key in ("after", "since"):
                ts = _dsl_date_to_ts(val)
                if ts is not None:
                    out["after"] = ts
                    continue
            if key in ("before", "until"):
                ts = _dsl_date_to_ts(val)
                if ts is not None:
                    out["before"] = ts
                    continue
        out["text"].append(tok)
    out["text"] = " ".join(out["text"])
    return out


def detect_lang(text):
    """旧接口:语言 key(detect_content 的薄包装;预览面板/历史缓存沿用)。"""
    return detect_content(text).lang


def json_reformat(text, compact=False):
    """JSON 美化(2 空格缩进)/紧凑;不是合法 JSON 返回 None。"""
    try:
        obj = json.loads(text)
        if compact:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return None


def json_error(text):
    """JSON 语法错误定位(查看器元信息用);合法或探测不出返回 None。"""
    try:
        json.loads(text)
        return None
    except json.JSONDecodeError as e:
        return "第 %d 行第 %d 列 %s" % (e.lineno, e.colno, e.msg)
    except Exception:
        return None


def csv_sniff(text):
    """CSV/TSV 嗅探,返回分隔符(\\t / , / ;)或 None。

    条件:≥3 个非空行、采样行分隔符数完全一致且 ≥1、至少 2 列;
    护栏:含括号/花括号(代码特征)直接否决,防止把 Go/JS 等多行调用
    误判成表格。带引号内嵌分隔符的 CSV 会因计数不一致退回纯文本,
    宁可漏判不误判。
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) < 3:
        return None
    sample = "\n".join(lines[:200])
    if re.search(r"[(){}]", sample):
        return None
    for delim in ("\t", ",", ";"):
        counts = [ln.count(delim) for ln in lines[:200]]
        if counts[0] >= 1 and len(set(counts)) == 1:
            if len(lines[0].split(delim)) >= 2:
                return delim
    return None


def parse_csv_rows(text, delim, max_rows=2001):
    """按分隔符解析 CSV(支持引号字段),最多返回 max_rows 行(查看器防爆)。"""
    import csv as _csv
    import io as _io
    rows = []
    try:
        for i, row in enumerate(_csv.reader(_io.StringIO(text), delimiter=delim)):
            if i >= max_rows:
                break
            rows.append(row)
    except Exception:
        return []
    return rows


def xml_pretty(text):
    """XML 美化(2 空格缩进);含 DOCTYPE/ENTITY(XXE 面)或解析失败返回 None。"""
    if not text or re.search(r"(?i)<!DOCTYPE|<!ENTITY", text):
        return None
    try:
        import xml.dom.minidom as _md
        out = _md.parseString(text.strip()).toprettyxml(indent="  ")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if lines and lines[0].startswith("<?xml"):
            lines = lines[1:]           # toprettyxml 自加的声明,原文视图另有留底
        return "\n".join(lines)
    except Exception:
        return None


# ============================================================
# AI 工作流(OpenAI 兼容 /chat/completions;标准库 urllib,零新依赖)
# 服务:  {"name","base_url","model","api_key"}
# 工作流:{"name","steps":[{"service","prompt"}]}  prompt 中 {input} 为上一步输出
# ============================================================
def ai_service_by_name(cfg, name):
    for s in cfg.get("ai_services") or []:
        if s.get("name") == name:
            return s
    return None


def render_prompt(prompt, content, previous=None):
    """提示词模板渲染:{input} 占位符替换为输入内容;给了 previous 时
    先插 {previous}(工作流 Step Contract 的占位符,见 workflow_steps)。"""
    s = str(prompt or "")
    if previous is not None:
        s = s.replace("{previous}", str(previous))
    return s.replace("{input}", str(content or ""))


# ---- AI 调用引擎:openai SDK 优先(统一 OpenAI 兼容端点的标准客户端),
# 标准库 urllib 零依赖兜底(裸环境/测试同样可用)。 ----
_openai_sdk_cache = []


def _openai_sdk():
    """惰性探测 openai SDK;结果缓存(单次 import 开销)。"""
    if not _openai_sdk_cache:
        try:
            from openai import OpenAI as _OAI
            _openai_sdk_cache.append(_OAI)
        except Exception:
            _openai_sdk_cache.append(None)
    return _openai_sdk_cache[0]


def _fmt_ai_error(e):
    code = getattr(e, "status_code", None)
    msg = str(getattr(e, "message", None) or e)
    return ("HTTP %s %s" % (code, msg)) if code else ("%s: %s" % (type(e).__name__, msg))


def ai_chat(service, prompt, timeout=90):
    """调用 OpenAI 兼容接口,返回 (ok, 回复文本或错误信息)。阻塞函数,请在后台线程调用。"""
    if not str(service.get("base_url") or "").rstrip("/"):
        return False, "该服务未配置 base_url"
    OpenAI = _openai_sdk()
    if OpenAI is not None:
        return _chat_via_sdk(OpenAI, service, prompt, timeout)
    return _chat_via_urllib(service, prompt, timeout)


def _chat_via_sdk(OpenAI, service, prompt, timeout):
    try:
        client = OpenAI(
            base_url=str(service.get("base_url")).rstrip("/") + "/",
            api_key=str(service.get("api_key") or "") or "EMPTY",  # Ollama 等无密钥端点
            timeout=timeout,
            max_retries=1,
        )
        resp = client.chat.completions.create(
            model=str(service.get("model") or ""),
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        if getattr(resp, "choices", None):
            text = (resp.choices[0].message.content or "").strip()
        if not text:
            return False, "AI 返回为空(响应格式异常)"
        return True, text
    except Exception as e:
        return False, _fmt_ai_error(e)


def _endpoint_url(base, path):
    """拼接用户自配 AI 端点;仅放行 http(s) 且带主机名,防误配 file:/c: 等本地
    协议被发起请求。非法返回 None,调用方按配置错误处理。"""
    try:
        parts = urlsplit(str(base or ""))
    except Exception:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return parts.geturl().rstrip("/") + path


def _http_json(service, method, path, body, timeout):
    """按用户自配端点发一次 JSON 请求,返回 (ok, 解析后的 dict 或 错误信息)。

    走 http.client 直连(不跟随重定向;OpenAI 兼容端点不重定向),URL 一律经
    _endpoint_url 校验,仅 http(s) 可达。"""
    import http.client
    url = _endpoint_url(service.get("base_url"), path)
    if url is None:
        return False, "接口地址无效(仅支持 http:// 或 https://)"
    parts = urlsplit(url)
    headers = {"Content-Type": "application/json"}
    if service.get("api_key"):
        headers["Authorization"] = "Bearer " + str(service["api_key"])
    cls = (http.client.HTTPSConnection if parts.scheme == "https"
           else http.client.HTTPConnection)
    conn = cls(parts.netloc, timeout=timeout)
    try:
        conn.request(method, parts.path or "/", body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        if r.status >= 400:
            detail = raw.decode("utf-8", "replace")[:300]
            return False, "HTTP %s %s" % (r.status, detail or r.reason)
        return True, json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _chat_via_urllib(service, prompt, timeout):
    body = json.dumps({
        "model": service.get("model") or "",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    ok, data = _http_json(service, "POST", "/chat/completions", body, timeout)
    if not ok:
        return False, data
    choices = data.get("choices") or [{}]
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        return False, "AI 返回为空(响应格式异常)"
    return True, text


def list_models(service, timeout=15):
    """模型发现:GET base_url + /models(OpenAI 标准端点,Ollama / 各家兼容网关均支持)。
    返回 (ok, [模型 id 列表](小写排序) 或 错误信息);供配置表单「发现模型」使用。"""
    base = str(service.get("base_url") or "").rstrip("/")
    if not base:
        return False, "请先填写接口地址"
    ok, data = _http_json(service, "GET", "/models", None, timeout)
    if not ok:
        return False, data
    ids = []
    for m in data.get("data") or []:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            ids.append(str(mid))
    if not ids:
        return False, "端点未返回模型清单(响应缺少 data[].id)"
    ids.sort(key=str.lower)
    return True, ids


# 常用 AI 供应商预设(均为 OpenAI 兼容接口;「添加提供方」菜单与配置表单共用)。
# models 为配置表单的模型下拉清单,note 是模型下方的信息行(上下文/特性;
# 价格随时会变,只写稳定的特性描述,免费档除外)。
AI_PRESET_SERVICES = [
    {"name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash", "api_key": "",
     "models": [
         {"id": "glm-4-flash", "note": "免费 · 128K 上下文"},
         {"id": "glm-4-air", "note": "轻量快速 · 128K 上下文"},
         {"id": "glm-4-plus", "note": "旗舰 · 128K 上下文"}]},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat", "api_key": "",
     "models": [
         {"id": "deepseek-chat", "note": "通用对话 V3 · 64K 上下文"},
         {"id": "deepseek-reasoner", "note": "深度思考 R1 · 64K 上下文"}]},
    {"name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-8k", "api_key": "",
     "models": [
         {"id": "moonshot-v1-8k", "note": "8K 上下文"},
         {"id": "moonshot-v1-32k", "note": "32K 上下文"},
         {"id": "moonshot-v1-128k", "note": "128K 长文"},
         {"id": "kimi-latest", "note": "最新版模型"}]},
    {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen-plus", "api_key": "",
     "models": [
         {"id": "qwen-turbo", "note": "快速 · 128K 上下文"},
         {"id": "qwen-plus", "note": "均衡 · 128K 上下文"},
         {"id": "qwen-max", "note": "旗舰 · 32K 上下文"}]},
    {"name": "豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "model": "doubao-pro-32k", "api_key": "",
     "models": [
         {"id": "doubao-pro-32k", "note": "Pro · 32K 上下文"},
         {"id": "doubao-pro-128k", "note": "Pro · 128K 上下文"},
         {"id": "doubao-lite-32k", "note": "Lite · 32K 上下文"}]},
    {"name": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1",
     "model": "Qwen/Qwen2.5-7B-Instruct", "api_key": "",
     "models": [
         {"id": "Qwen/Qwen2.5-7B-Instruct", "note": "免费"},
         {"id": "deepseek-ai/DeepSeek-V3", "note": "旗舰开源"},
         {"id": "THUDM/glm-4-9b-chat", "note": "轻量开源"}]},
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o-mini", "api_key": "",
     "models": [
         {"id": "gpt-4o-mini", "note": "便宜快速"},
         {"id": "gpt-4o", "note": "旗舰"},
         {"id": "gpt-4.1-mini", "note": "新一代轻量"}]},
    {"name": "Ollama 本地", "base_url": "http://localhost:11434/v1",
     "model": "qwen2.5", "api_key": "",
     "models": [
         {"id": "qwen2.5", "note": "本地运行,无需密钥"},
         {"id": "llama3.1", "note": "本地运行,无需密钥"},
         {"id": "deepseek-r1", "note": "本地运行,无需密钥"}]},
]

# 内置示例工作流(首次"填入示例"时写入 config)
# 步骤类型 kind:web=网页 AI(复制提示词/打开网页) / human=人工确认 / ai=本地 API(可选)
# {input} 为输入内容或上一步输出;web 步骤的 url 模板中 {prompt} 为渲染后的提示词
AI_SEED_SERVICES = [
    {"name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash", "api_key": ""},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat", "api_key": ""},
]
AI_SEED_WORKFLOWS = [
    {"name": "网页 AI 串联(评审→修改)", "steps": [
        {"kind": "web", "prompt": "你是严格的审稿人。请从逻辑、结构、表达三个维度评审下面内容,指出问题并给出修改建议:\n{input}", "url": ""},
        {"kind": "human", "prompt": "切到网页 AI 粘贴提示词(Ctrl+V),等回答;复制回答后回到这里点「从剪贴板粘贴」"},
        {"kind": "web", "prompt": "你是作者。请根据下面这份审稿意见,逐条回应并输出修改后的完整版本:\n{input}", "url": "https://chatgpt.com/?q={prompt}"},
        {"kind": "human", "prompt": "粘贴修改稿,可先编辑再继续"},
    ]},
    {"name": "概念解释器(网页版)", "steps": [
        {"kind": "web", "prompt": "你是概念解释器。请用通俗语言解释下面概念,先一句话定义,再给一个直观类比,最后指出常见误解:\n{input}", "url": ""},
        {"kind": "human", "prompt": "粘贴网页 AI 的解释"},
    ]},
    {"name": "苏格拉底追问(网页版)", "steps": [
        {"kind": "web", "prompt": "扮演苏格拉底。针对我给出的想法,从至少 4 个不同角度连续追问,帮助我把模糊的想法变清晰。只输出问题:\n{input}", "url": ""},
        {"kind": "human", "prompt": "粘贴追问,逐一回答后再继续下一步"},
    ]},
    {"name": "翻译(G)→润色(D)[本地 API]", "steps": [
        {"kind": "ai", "service": "GLM", "prompt": "把下面的文本翻译成英文,只输出译文:\n{input}"},
        {"kind": "ai", "service": "DeepSeek", "prompt": "润色以下英文,使其更自然地道、符合书面表达,只输出结果:\n{input}"},
    ]},
]


# ============================================================
# 入口(tkinter 前端已移除:本项目只保留 Qt + Fluent 版界面)
# ============================================================
if __name__ == "__main__":
    print("WinClipboard 后端模块:界面请运行 winclipboard_qt.py(需 PySide6 + PySide6-Fluent-Widgets)。")
