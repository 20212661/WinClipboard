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
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
APP_DIR = os.path.join(_APPDATA, APP_NAME)
IMAGE_DIR = os.path.join(APP_DIR, "images")
ICON_FILE = os.path.join(APP_DIR, "icon.ico")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
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
ID_TIMER = 2001
ID_TIMER_CAPTURE = 2002      # 启动剪贴板捕获(单次)
ID_HOTKEY = 100              # 主热键(Ctrl+Shift+V)
ID_HOTKEY_PIN = 102          # 图钉画布:直接钉剪贴板图片(Ctrl+Shift+P)
ID_HOTKEY_CTT = 103          # 图钉画布:鼠标穿透切换(Ctrl+Alt+T)

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

DEFAULT_CFG = {
    "hotkey_mod": MOD_CONTROL | MOD_SHIFT,
    "hotkey_key": 0x56,          # 'V'
    "auto_paste": True,
    "sensitive_guard": True,
    "max_items": 100,
    "window_w": 600,
    "window_h_max": 720,
    # ---- AI 工作流(Qt 版 UI 使用;两版前端共享配置) ----
    # 服务: {"name","base_url","model","api_key"}  base_url 需兼容 OpenAI /chat/completions
    # 工作流: {"name","steps":[{"service","prompt"}]}  prompt 中 {input} 为上一步输出
    "ai_services": [],
    "ai_workflows": [],
}

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


def load_history():
    # 主文件损坏时依次回退到最近备份(.bak1/.bak2/.bak3)
    for fp in (HISTORY_FILE, HISTORY_FILE + ".bak1",
               HISTORY_FILE + ".bak2", HISTORY_FILE + ".bak3"):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
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


def save_history(hist):
    # 原子写:滚动备份 + 临时文件替换,避免写入中途崩溃损坏历史
    tmp = HISTORY_FILE + ".tmp"
    try:
        _rotate_history_backup()
        Path(tmp).write_text(json.dumps(hist, ensure_ascii=False),
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


def is_sensitive(text):
    """识别疑似敏感内容:密码上下文、银行卡(Luhn)、身份证(校验位)、API 密钥、JWT。"""
    t = text.strip()
    if not t:
        return False
    if _RE_KEYVAL.search(text):
        return True
    if _RE_KEYS.search(t):
        return True
    if _RE_JWT.match(t):
        return True
    m = _RE_CARD.match(t)
    if m and _luhn_ok(m.group(1)):
        return True
    if _RE_IDCARD.match(t) and _idcard_ok(t):
        return True
    return False


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
        self._nid = (self.hwnd, 0,
                     NIF_ICON | NIF_MESSAGE | NIF_TIP,
                     WM_TASKBAR, self.hicon, "WinClipboard  (Ctrl+Shift+V)")
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
        if not pin_ok:
            log("图钉热键(Ctrl+Shift+P)被占用,直钉剪贴板不可用")
        if not ctt_ok:
            log("穿透热键(Ctrl+Alt+T)被占用,鼠标穿透已禁用")
        self.app._post(("ctt_hotkey_state", bool(ctt_ok)))
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
        self.app.submit_capture()
        return 0

    def on_hotkey(self, hwnd, msg, wp, lp):
        if wp == ID_HOTKEY_PIN:
            self.app._post(("pin_hotkey",))
        elif wp == ID_HOTKEY_CTT:
            self.app._post(("ctt_hotkey",))
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
                   M_CTT_OFF: "ctt_off"}
        act = actions.get(mid)
        if act:
            self.app._post(("tray", act))
        return 0

    # ---- 命令处理 ----
    def _do_cmd(self, cmd):
        if cmd[0] == "paste":
            time.sleep(0.07)
            try:
                send_ctrl_v()
            except Exception:
                pass
        elif cmd[0] == "quit":
            self._cleanup()
            win32gui.PostQuitMessage(0)

    # ---- 托盘菜单 ----
    def _popup_menu(self):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_SHOW,
                            "显示剪贴板  (Ctrl+Shift+V)")
        win32gui.AppendMenu(menu, win32con.MF_STRING, M_CLEAR, "清空历史")
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

    def __init__(self):
        self.cfg, _cfg_stale = _load_config_raw()
        self._cfg_need_save = _cfg_stale
        self.max_items = int(self.cfg.get("max_items", 100))
        self.history = load_history()
        self.private = load_private()   # 敏感内容隔离区(不进普通面板)
        self.signal_q = queue.Queue()
        # 事件唤醒回调(前端注入):queue 只管数据,回调负责把"该排空了"
        # 通知到 UI 线程(如 Qt Signal 的 emit);无 UI / 测试环境保持 None
        self.on_event = None
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
        # 历史迁移:补齐哈希、合并重复图片条目(孤儿文件清理延迟到启动后,
        # 见 cleanup_orphan_images;条目迁移仍须在后台线程启动前完成)
        self._migrate_images()
        # 旧版明文私密区/密钥自动升级为 DPAPI 加密
        self._migrate_private()
        self._migrate_config_secrets()
        # 后台线程(托盘 / 全局热键 / 剪贴板监听)
        self.worker = TrayWorker(self)
        self.worker.start()

    # ---- 事件投递(任意线程可调) ----
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

    # ---- 剪贴板读取(在捕获线程调用) ----
    def submit_capture(self):
        """把一次捕获(读板 + 可能的大图 PNG 编码)提交到后台线程。
        托盘线程调用后立即返回,不再被编码耗时拖住消息循环。"""
        exec_ = getattr(self, "_capture_exec", None)
        if exec_ is None:                    # 兼容测试里 __new__ 构造的核心
            self._capture_once()
            return
        try:
            exec_.submit(self._capture_once)
        except Exception:
            pass

    def _capture_once(self):
        try:
            item = self.read_clipboard_now()
            if item:
                self._post(("clip", item))
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
            path = os.path.join(IMAGE_DIR, "img_%s.png" % hsh)
            if not os.path.exists(path):
                # 去重命中时完全跳过 PNG 编码(4K 图编码秒级,像素哈希只要百毫秒级)
                with PerfSpan("png_encode"):
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                # 原子落盘:崩溃不留半截文件(否则下次命中坏文件无法自愈)
                Path(path + ".tmp").write_bytes(buf.getvalue())
                os.replace(path + ".tmp", path)
            w = struct.unpack("<i", dib[4:8])[0]
            h = abs(struct.unpack("<i", dib[8:12])[0])
            return ("image", {"path": path, "w": w, "h": h, "hash": hsh})
        except Exception:
            return None

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
                save_history(self.history)
        except Exception:
            pass

    def cleanup_orphan_images(self, min_age=120.0):
        """删除不被任何历史条目引用的孤儿图片(前端延迟调度,不占启动路径)。

        线程约定:与历史变更同线程(UI 线程)调用,引用快照才一致;
        min_age 秒内新建的文件一律跳过——捕获线程刚落盘、事件尚未入史的
        瞬间文件恰好"无引用",靠时间窗兜底而不是加锁。
        """
        try:
            refs = {h.get("content") for h in self.history
                    if h.get("type") == "image"}
            now = time.time()
            removed = 0
            if os.path.isdir(IMAGE_DIR):
                with os.scandir(IMAGE_DIR) as it:     # 扁平目录,无需 os.walk
                    for e in it:
                        if e.is_dir() or not e.name.lower().endswith((".png", ".tmp")):
                            continue
                        fp = os.path.join(IMAGE_DIR, e.name)
                        if fp in refs:
                            continue
                        try:
                            if now - e.stat().st_mtime < min_age:
                                continue               # 可能正被捕获流程写入
                        except OSError:
                            continue
                        try:
                            os.remove(fp)
                            removed += 1
                        except Exception:
                            pass
            if removed:
                log("孤儿图片清理:删除 %d 个未引用文件" % removed)
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
        gen = getattr(self, "_gen", 0)
        with PerfSpan("history_save"):
            save_history(self.history)
        if gen == getattr(self, "_gen", 0):
            self._dirty = False

    def add_item(self, item):
        kind, content = item
        ts = time.time()
        if kind == "text":
            # 敏感内容自动隔离:进私密区,不写入普通历史
            if self.cfg.get("sensitive_guard", True) and is_sensitive(content):
                self._add_private(content)
                return
            rec = {"type": "text", "content": content, "pinned": False, "ts": ts}
            key = content
        elif kind == "files":
            paths = tuple(content)
            rec = {"type": "files", "content": list(paths), "pinned": False, "ts": ts}
            key = ("files", paths)
        else:
            rec = {"type": "image", "content": content["path"], "pinned": False,
                   "w": content.get("w", 0), "h": content.get("h", 0),
                   "hash": content.get("hash", ""), "ts": ts}
            key = ("img", content.get("hash", ""))

        # 去重:已存在则移除旧的,放到最前(置顶的不动)
        for i, h in enumerate(self.history):
            if h["type"] == "text":
                k = h["content"]
            elif h["type"] == "files":
                k = ("files", tuple(h.get("content", [])))
            else:
                k = ("img", h.get("hash") or file_sha1(h["content"]))
            if k == key:
                if h.get("pinned"):
                    return
                self.history.pop(i)
                break
        self.history.insert(0, rec)
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
                removed += 1
        for i in sorted(to_del, reverse=True):
            self.history.pop(i)

    def _add_private(self, content):
        """敏感内容写入私密区(去重、上限 100 条)。"""
        try:
            for i, s in enumerate(self.private):
                if s == content:
                    self.private.pop(i)
                    break
            self.private.insert(0, content)
            del self.private[100:]
            save_private(self.private)
            log("敏感内容已隔离(仅入私密区,不写入普通历史)")
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

    def filter_view(self, category, query=""):
        """按分类 + 关键词过滤,返回命中条目在 history 中的索引列表。"""
        q = (query or "").strip().casefold()
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
            if q:
                if q not in self._search_text(h):
                    continue
            view.append(i)
        return view

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

    def paste_record(self, rec):
        """写回剪贴板 + 自动粘贴排队 + 置顶到最近。文件全部丢失时返回 False。"""
        if rec["type"] == "files":
            existing = [p for p in rec["content"] if os.path.exists(p)]
            if not existing:
                return False
            rec["content"] = existing
        # 时间窗抑制本次自写入触发的剪贴板回调
        self._ignore_clip_until = time.monotonic() + 0.5
        self._set_clipboard(rec)
        if self.cfg.get("auto_paste", True):
            self.worker.cmd_q.put(("paste",))
        try:
            self.history.remove(rec)
            self.history.insert(0, rec)
            self._mark_dirty()
        except Exception:
            pass
        return True

    def paste_text(self, content):
        """私密区文本:写回剪贴板 + 自动粘贴排队。"""
        self._ignore_clip_until = time.monotonic() + 0.5
        self._set_clipboard({"type": "text", "content": content})
        if self.cfg.get("auto_paste", True):
            self.worker.cmd_q.put(("paste",))

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
        self.history[hi]["pinned"] = not self.history[hi].get("pinned", False)
        self._mark_dirty()

    def delete_at(self, hi):
        rec = self.history.pop(hi)
        if rec["type"] == "image":
            try:
                os.remove(rec["content"])
            except Exception:
                pass
        self._mark_dirty()
        return rec

    def clear_unpinned(self):
        """清空未置顶条目(连同其图片文件);置顶保留。"""
        for h in self.history:
            if not h.get("pinned") and h["type"] == "image":
                try:
                    os.remove(h["content"])
                except Exception:
                    pass
        self.history[:] = [h for h in self.history if h.get("pinned")]
        self._mark_dirty()

    def save_all(self):
        self.flush_history()
        save_private(self.private)

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
    "ContentInfo", "category subtype lang confidence metadata")
# (category, subtype, lang, confidence, metadata) —— 见 detect_content 文档


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
        return ContentInfo("text", "plain", "text", 1.0, {})
    try:
        json.loads(t)
        return ContentInfo("structured_text", "json", "json", 1.0,
                           {"valid": True})
    except Exception:
        pass
    stripped = text.strip()
    if "\n" not in stripped and _URL_RE.match(stripped):
        return ContentInfo("url", "web_url", "text", 0.99, {})
    jwt = _sniff_jwt(t)
    if jwt is not None:
        return ContentInfo("encoded", "jwt", "text", 0.99, {})
    b64 = _sniff_base64(t)
    if b64 is not None:
        kind, raw = b64
        return ContentInfo("encoded", "base64", "text", 0.95,
                           {"b64_kind": kind, "b64_len": len(raw)})
    head = t[:4000]
    if head[:5].lower() == "<?xml":
        return ContentInfo("structured_text", "xml", "xml", 1.0, {})
    if re.match(r"<(!doctype|html)\b", head, re.I):
        return ContentInfo("markup", "html", "html", 0.9, {})
    if "\n```" in t or t.startswith("```") or re.search(r"(?m)^#{1,6} \S", head):
        return ContentInfo("markup", "markdown", "markdown", 0.9, {})
    delim = csv_sniff(text)
    if delim:
        return ContentInfo("structured_text", "csv", "csv", 0.93,
                           {"delim": delim})
    # 廉价强特征:剪贴板里多是短片段,pygments 对短文本置信度低,先用正则兜住
    if t[:2] == "#!":
        if "python" in t[:80]:
            return ContentInfo("code", "python", "python", 0.8, {})
        if re.search(r"\b(bash|zsh|sh|dash)\b", t[:80]):
            return ContentInfo("code", "shell", "shell", 0.8, {})
    if re.search(r"(?m)^(import\s+[\w.]+|from\s+[\w.]+\s+import\b)", head) and \
            not re.search(r"(?m)^import\s+[\w{}\s,]+\s+from\s+['\"]", head):
        return ContentInfo("code", "python", "python", 0.8, {})
    if re.search(r"(?mi)\b(const|let|var)\s+[\w$]+\s*=|function\s*[\w$]*\s*\(|=>"
                 r"|console\.(log|warn|error)\b|document\.getElement", head):
        return ContentInfo("code", "javascript", "javascript", 0.8, {})
    if re.search(r"(?m)^[\w\s.,#>-]+\{[^{}]*[\w-]+\s*:[^{}]*;[^{}]*\}", head):
        return ContentInfo("code", "css", "css", 0.8, {})
    if re.search(r"(?mi)\b(select\s+.+\s+from\b|insert\s+into\b|update\s+\w+\s+set\b"
                 r"|delete\s+from\b|create\s+(table|database|index)\b|alter\s+table\b"
                 r"|with\s+\w+\s+as\s*\()", head):
        return ContentInfo("code", "sql", "sql", 0.8, {})
    if re.search(r"(?m)^\s*(def\s+\w+\s*\(|class\s+\w+[:(]|@\w+(\.[\w.]+)?\s*$)",
                 head):
        return ContentInfo("code", "python", "python", 0.8, {})
    if _pg_guess_lexer is not None:
        try:
            # 采样 2000 字符:guess_lexer 对 20KB 输入需 ~36ms(预览面板每次
            # 选中都会跑),语言判断只需开头特征,采样小窗足够
            lex = _pg_guess_lexer(t[:2000])
            for a in getattr(lex, "aliases", None) or []:
                if a in _LANG_ALIAS:
                    return ContentInfo("code", _LANG_ALIAS[a],
                                       _LANG_ALIAS[a], 0.6, {})
        except Exception:
            pass
    return ContentInfo("text", "plain", "text", 1.0, {})


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


def render_prompt(prompt, content):
    """提示词模板渲染:{input} 占位符替换为输入内容。"""
    return str(prompt or "").replace("{input}", str(content or ""))


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
