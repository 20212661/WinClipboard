#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinClipboard - Windows 桌面剪贴板管理器
=========================================
功能:
  - 后台常驻系统托盘,自动记录文本 / 图片剪贴历史
  - 全局热键 Ctrl+Shift+V 弹出历史面板(鼠标处)
  - 双击/回车 复制并自动粘贴到当前窗口
  - 关键词搜索(文本 + 图片尺寸)、图片缩略图预览
  - 置顶(📌)、删除、清空,图片按内容 SHA-1 去重
  - 图片简单处理:裁剪 / 旋转 / 缩放 / 标注(方框、箭头、文字、马赛克)/ 压缩导出
  - 历史持久化(原子写入,重启后保留),开机自启动
依赖: pywin32, Pillow, tkinter(均为已安装)

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
import threading
import traceback
from ctypes import wintypes

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog
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
    from PIL import Image, ImageDraw, ImageFont, ImageTk, ImageFilter, ImageOps, ImageChops
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
ID_TIMER = 2001
ID_HOTKEY = 100

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
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
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
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
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
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
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
def dib_to_png_bytes(dib):
    """把剪贴板的 CF_DIB 原始字节转成 PNG 字节流。"""
    bisize = struct.unpack("<I", dib[:4])[0]
    bitcount = struct.unpack("<H", dib[14:16])[0]
    compression = struct.unpack("<I", dib[16:20])[0] if len(dib) >= 20 else 0
    clrused = struct.unpack("<I", dib[32:36])[0] if len(dib) >= 36 else 0
    palette = 0
    if bitcount <= 8:
        palette = (clrused if clrused else (1 << bitcount)) * 4
    masks = 12 if (compression == 3 and bisize == 40) else 0  # BI_BITFIELDS 的掩码
    pixel_offset = 14 + bisize + palette + masks
    bmp = b"BM" + struct.pack("<IHHI", len(dib) + 14, 0, 0, pixel_offset) + dib
    buf = io.BytesIO()
    Image.open(io.BytesIO(bmp)).save(buf, "PNG")
    return buf.getvalue()


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

        self.hwnd = win32gui.CreateWindowEx(
            0, class_atom, "WinClipboard", 0, 0, 0, 0, 0, 0, 0, self.hinst, None)

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
            self.app.signal_q.put(("tray", "hotkey_fail"))
            log("全局热键注册失败(hotkey_mod=%d key=%d)" % (cfg["hotkey_mod"], cfg["hotkey_key"]))
        user32.SetTimer(self.hwnd, ID_TIMER, 80, None)

        # 启动时把当前剪贴板内容纳入历史
        try:
            item = self.app.read_clipboard_now()
            if item:
                self.app.signal_q.put(("clip", item))
        except Exception:
            pass

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
            self.app.signal_q.put(("tray", "show"))
        elif lp == win32con.WM_RBUTTONUP:
            self._popup_menu()
        return 1

    def on_clip(self, hwnd, msg, wp, lp):
        # 我们自己写入剪贴板时跳过(时间窗比一次性标志更抗竞态)
        if time.monotonic() < self.app._ignore_clip_until:
            return 0
        try:
            item = self.app.read_clipboard_now()
            if item:
                self.app.signal_q.put(("clip", item))
        except Exception:
            pass
        return 0

    def on_hotkey(self, hwnd, msg, wp, lp):
        self.app.signal_q.put(("hotkey",))
        return 0

    def on_timer(self, hwnd, msg, wp, lp):
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
                   M_AUTOSTART: "autostart", M_ABOUT: "about", M_QUIT: "quit"}
        act = actions.get(mid)
        if act:
            self.app.signal_q.put(("tray", act))
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
# 核心后端(与 UI 无关;tkinter / Qt 两版前端共用同一实例)
# ============================================================
class ClipboardCore:
    """剪贴板核心状态机:配置 / 历史 / 私密区 / 捕获 / 回写 / 淘汰 / 自启。

    约定:
    - history / private / cfg 容器始终原地修改(不重新绑定),
      前端在初始化后可安全持有同名别名;
    - 所有事件(剪贴捕获 / 历史变化 / 热键 / 托盘)一律经 signal_q 投递给前端,
      由前端在自己的 UI 线程轮询处理;核心不会直接调用任何 UI 代码;
    - 托盘/热键/监听线程(worker)由本类创建并启动。
    """

    def __init__(self):
        self.cfg, _cfg_stale = _load_config_raw()
        self._cfg_need_save = _cfg_stale
        self.max_items = int(self.cfg.get("max_items", 100))
        self.history = load_history()
        self.private = load_private()   # 敏感内容隔离区(不进普通面板)
        self.signal_q = queue.Queue()
        self._ignore_clip_until = 0.0   # 抑制自写入剪贴板事件的截止时间(单调钟)
        # 历史迁移:补齐哈希、合并重复图片、清理孤儿文件(须在后台线程启动前)
        self._migrate_images()
        # 旧版明文私密区/密钥自动升级为 DPAPI 加密
        self._migrate_private()
        self._migrate_config_secrets()
        # 后台线程(托盘 / 全局热键 / 剪贴板监听)
        self.worker = TrayWorker(self)
        self.worker.start()

    # ---- 剪贴板读取(在工作线程调用) ----
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
            data = dib_to_png_bytes(dib)
            hsh = hashlib.sha1(data).hexdigest()
            path = os.path.join(IMAGE_DIR, "img_%s.png" % hsh)
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(data)
            w = struct.unpack("<i", dib[4:8])[0]
            h = abs(struct.unpack("<i", dib[8:12])[0])
            return ("image", {"path": path, "w": w, "h": h, "hash": hsh})
        except Exception:
            return None

    def _migrate_images(self):
        """图片历史迁移:补齐 SHA-1 哈希、合并重复内容、清理孤儿文件。"""
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
            # 清理不被任何条目引用的孤儿图片文件
            refs = {h["content"] for h in keep if h.get("type") == "image"}
            if os.path.isdir(IMAGE_DIR):
                for fn in os.listdir(IMAGE_DIR):
                    fp = os.path.join(IMAGE_DIR, fn)
                    if fn.lower().endswith(".png") and fp not in refs:
                        try:
                            os.remove(fp)
                            changed = True
                        except Exception:
                            pass
            if changed:
                save_history(self.history)
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
        save_history(self.history)
        self._notify_change()

    def _notify_change(self):
        """通知前端历史已变。

        只投递队列信号,不在本方法内直接碰 UI:add_item 由前端的轮询回调触发,
        而 TrayWorker 也会经 signal_q 投递,统一走队列可避免跨线程访问部件。
        """
        self.signal_q.put(("change",))

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
    def filter_view(self, category, query=""):
        """按分类 + 关键词过滤,返回命中条目在 history 中的索引列表。"""
        q = (query or "").strip().lower()
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
                if t == "text":
                    if q not in h["content"].lower():
                        continue
                elif t == "files":
                    blob = (os.path.basename(h["content"][0]).lower() + " "
                            + " ".join(h["content"]).lower())
                    if q not in blob:
                        continue
                elif t == "image":
                    if q not in ("图片 截图 %s×%s" % (h.get("w", 0), h.get("h", 0))).lower():
                        continue
            view.append(i)
        return view

    # ---- 回写 / 粘贴 ----
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
            save_history(self.history)
        except Exception:
            pass
        return True

    def paste_text(self, content):
        """私密区文本:写回剪贴板 + 自动粘贴排队。"""
        self._ignore_clip_until = time.monotonic() + 0.5
        self._set_clipboard({"type": "text", "content": content})
        if self.cfg.get("auto_paste", True):
            self.worker.cmd_q.put(("paste",))

    def _set_clipboard(self, rec):
        for _ in range(6):
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
                return
            except Exception:
                time.sleep(0.03)

    # ---- 条目状态操作 ----
    def toggle_pin_at(self, hi):
        self.history[hi]["pinned"] = not self.history[hi].get("pinned", False)
        save_history(self.history)

    def delete_at(self, hi):
        rec = self.history.pop(hi)
        if rec["type"] == "image":
            try:
                os.remove(rec["content"])
            except Exception:
                pass
        save_history(self.history)
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
        save_history(self.history)

    def save_all(self):
        save_history(self.history)
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
# 图片标注辅助(模块级函数,供 ImageEditor 使用)
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


def _draw_text(img, pos, text, font):
    d = ImageDraw.Draw(img)
    x, y = pos
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        d.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
    d.text((x, y), text, font=font, fill=(255, 255, 255))


def _draw_arrow(img, p0, p1):
    d = ImageDraw.Draw(img)
    d.line((p0, p1), fill=(255, 40, 40), width=4)
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    L, a = 18, 0.5
    x1, y1 = p1
    p2 = (x1 - L * math.cos(ang - a), y1 - L * math.sin(ang - a))
    p3 = (x1 - L * math.cos(ang + a), y1 - L * math.sin(ang + a))
    d.polygon((p1, p2, p3), fill=(255, 40, 40))


def _mosaic(img, box):
    region = img.crop(box)
    if region.width < 1 or region.height < 1:
        return
    small = region.resize((max(1, region.width // 14), max(1, region.height // 14)),
                          RESAMPLE)
    img.paste(small.resize(region.size, Image.NEAREST), (box[0], box[1]))


# ============================================================
# 图片编辑器(裁剪/旋转/缩放 + 标注 + 压缩导出, 主线程)
# ============================================================
class ImageEditor:
    """图片简单处理:裁剪/旋转/缩放 + 标注(方框/箭头/文字/马赛克)+ 压缩导出。"""

    def __init__(self, app, rec):
        self.app = app
        self.rec = rec
        try:
            src = Image.open(rec["content"])
            if src.mode != "RGB":
                bg = Image.new("RGB", src.size, (255, 255, 255))
                if src.mode in ("RGBA", "LA"):
                    bg.paste(src, mask=src.split()[-1])
                else:
                    bg.paste(src)
                src = bg
            self.base = src
        except Exception:
            messagebox.showerror("WinClipboard", "无法打开图片文件。", parent=app.root)
            return

        self.undo_stack = []      # 已提交操作前的快照(最多 10 张)
        self.tool = None          # crop / rect / arrow / text / mosaic
        self.pending_text = None
        self.scale = 1.0
        self.ox = self.oy = 0
        self._drag_xy = None
        self._photo = None
        self._font = _load_font(26)

        top = tk.Toplevel(app.root)
        self.top = top
        top.title("图片处理 — " + os.path.basename(rec["content"]))
        top.configure(bg="#f7f9fc")
        top.attributes("-topmost", True)
        top.geometry("900x620")

        bar = tk.Frame(top, bg="#f7f9fc")
        bar.pack(fill="x", padx=6, pady=6)
        self._tbtn = {}
        for key, label in (("crop", "✂ 裁剪"), ("rotl", "⟲ 左旋"), ("rotr", "⟳ 右旋"),
                           ("scale", "🔍 缩放"), ("_sep_", None),
                           ("rect", "▭ 方框"), ("arrow", "➤ 箭头"),
                           ("text", "🅃 文字"), ("mosaic", "◼ 马赛克")):
            if key == "_sep_":
                tk.Frame(bar, width=1, bg="#e3e7ee").pack(side="left", fill="y",
                                                          padx=6, pady=2)
                continue
            b = tk.Button(bar, text=label, command=lambda k=key: self._on_tool(k),
                          relief="flat", borderwidth=0, bg="#ffffff",
                          fg="#1f2733", cursor="hand2",
                          activebackground="#e8efff", activeforeground="#1f2733",
                          highlightthickness=1, highlightbackground="#e3e7ee",
                          font=("Segoe UI", 9), padx=7, pady=3)
            b.pack(side="left", padx=2)
            if key in ("crop", "rect", "arrow", "text", "mosaic"):
                self._tbtn[key] = b
        for key, label in (("undo", "↶ 撤销"), ("compress", "📦 压缩导出"),
                           ("copy", "📋 复制"), ("save", "💾 保存")):
            b = tk.Button(bar, text=label, command=getattr(self, "_on_" + key),
                          relief="flat", borderwidth=0, bg="#ffffff",
                          fg="#1f2733", cursor="hand2",
                          activebackground="#e8efff", activeforeground="#1f2733",
                          highlightthickness=1, highlightbackground="#e3e7ee",
                          font=("Segoe UI", 9), padx=7, pady=3)
            b.pack(side="right", padx=2)

        self.canvas = tk.Canvas(top, bg="#3a3f47", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        self.lbl_status = tk.Label(top, text="", bg="#f7f9fc", fg="#8a94a6",
                                   anchor="w", font=("Segoe UI", 8))
        self.lbl_status.pack(fill="x", padx=10, pady=(0, 6))

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        top.protocol("WM_DELETE_WINDOW", self.close)
        self._redraw()

    # ---- 工具 ----
    def _on_tool(self, key):
        if key == "crop":
            self._set_tool("crop")
        elif key == "rotl":
            self._transform(lambda im: im.rotate(90, expand=True))
        elif key == "rotr":
            self._transform(lambda im: im.rotate(-90, expand=True))
        elif key == "scale":
            v = simpledialog.askinteger("缩放", "目标百分比(10–300):", parent=self.top,
                                        initialvalue=100, minvalue=10, maxvalue=300)
            if v and v != 100:
                self._transform(lambda im: im.resize(
                    (max(1, int(im.width * v / 100)), max(1, int(im.height * v / 100))),
                    RESAMPLE))
        elif key == "text":
            s = simpledialog.askstring("文字标注", "输入标注文字:", parent=self.top)
            if s:
                self._set_tool("text")
                self.pending_text = s
        else:
            self._set_tool(key)

    def _set_tool(self, key):
        self.tool = None if self.tool == key else key
        for k, b in self._tbtn.items():
            on = (k == self.tool)
            b.config(bg=self.app.ACCENT_SOFT if on else "#ffffff",
                     fg=self.app.ACCENT if on else "#1f2733",
                     highlightbackground=self.app.ACCENT_SOFT if on else "#e3e7ee")
        self._status()

    def _transform(self, fn):
        self._push()
        self.base = fn(self.base)
        self._redraw()

    def _push(self):
        self.undo_stack.append(self.base.copy())
        if len(self.undo_stack) > 10:
            self.undo_stack.pop(0)

    # ---- 鼠标交互 ----
    def _to_img(self, cx, cy):
        return (int((cx - self.ox) / self.scale), int((cy - self.oy) / self.scale))

    def _press(self, e):
        if self.tool in ("crop", "rect", "arrow", "mosaic"):
            self._drag_xy = (e.x, e.y)
        elif self.tool == "text" and self.pending_text:
            self._push()
            x, y = self._to_img(e.x, e.y)
            _draw_text(self.base, (max(0, x), max(0, y)), self.pending_text, self._font)
            self.pending_text = None
            self._set_tool("text")   # 放置一次后取消工具
            self._redraw()

    def _drag(self, e):
        if self._drag_xy:
            x0, y0 = self._drag_xy
            self.canvas.delete("rubber")
            self.canvas.create_rectangle(x0, y0, e.x, e.y, outline="#ff5252",
                                         width=2, dash=(5, 3), tags="rubber")

    def _release(self, e):
        if not self._drag_xy:
            return
        x0, y0 = self._drag_xy
        self._drag_xy = None
        self.canvas.delete("rubber")
        if abs(e.x - x0) < 4 or abs(e.y - y0) < 4:
            return
        (ix0, iy0), (ix1, iy1) = self._to_img(x0, y0), self._to_img(e.x, e.y)
        ix0, ix1 = sorted((ix0, ix1))
        iy0, iy1 = sorted((iy0, iy1))
        ix0, iy0 = max(0, ix0), max(0, iy0)
        ix1, iy1 = min(self.base.width, ix1), min(self.base.height, iy1)
        if ix1 - ix0 < 2 or iy1 - iy0 < 2:
            return
        self._push()
        if self.tool == "crop":
            self.base = self.base.crop((ix0, iy0, ix1, iy1))
        elif self.tool == "rect":
            ImageDraw.Draw(self.base).rectangle((ix0, iy0, ix1, iy1),
                                                outline=(255, 40, 40), width=4)
        elif self.tool == "arrow":
            _draw_arrow(self.base, (ix0, iy0), (ix1, iy1))
        elif self.tool == "mosaic":
            _mosaic(self.base, (ix0, iy0, ix1, iy1))
        self._set_tool(self.tool)   # 完成一次标注后取消工具选中
        self._redraw()

    # ---- 渲染 ----
    def _redraw(self):
        cw = max(self.canvas.winfo_width(), 120)
        ch = max(self.canvas.winfo_height(), 120)
        w, h = self.base.size
        self.scale = min(cw / w, ch / h, 2.0)
        if self.scale <= 0:
            self.scale = 0.05
        dw, dh = int(w * self.scale), int(h * self.scale)
        self.ox, self.oy = (cw - dw) // 2, (ch - dh) // 2
        self._photo = ImageTk.PhotoImage(self.base.resize((dw, dh), RESAMPLE))
        self.canvas.delete("all")
        self.canvas.create_image(self.ox, self.oy, image=self._photo, anchor="nw")
        self._status()

    def _status(self):
        w, h = self.base.size
        tip = {"crop": "拖动框选裁剪区域", "rect": "拖动绘制红色方框",
               "arrow": "拖动绘制箭头", "text": "点击放置文字",
               "mosaic": "拖动框选区域打马赛克"}.get(self.tool, "就绪")
        self.lbl_status.config(text="{}   |   尺寸: {}×{}   |   可撤销 {} 步".format(
            tip, w, h, len(self.undo_stack)))

    # ---- 操作 ----
    def _on_undo(self):
        if not self.undo_stack:
            return
        self.base = self.undo_stack.pop()
        self._redraw()

    def _on_copy(self):
        if set_dib_to_clipboard(self.app.worker.hwnd, pil_to_dib(self.base)):
            self.lbl_status.config(text="✅ 已复制到剪贴板")
        else:
            self.lbl_status.config(text="❌ 复制失败(剪贴板被占用)")

    def _on_save(self):
        p = filedialog.asksaveasfilename(
            parent=self.top, defaultextension=".png",
            filetypes=[("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg")],
            initialfile="clipboard_edit.png")
        if not p:
            return
        try:
            fmt = "JPEG" if p.lower().endswith((".jpg", ".jpeg")) else "PNG"
            self.base.save(p, fmt)
            self.lbl_status.config(text="✅ 已保存: " + p)
        except Exception:
            messagebox.showerror("WinClipboard", "保存失败。", parent=self.top)

    def _on_compress(self):
        dlg = tk.Toplevel(self.top)
        dlg.title("压缩 / 格式转换")
        dlg.configure(bg="white")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        dlg.transient(self.top)
        var_q = tk.IntVar(value=80)
        var_fmt = tk.StringVar(value="JPG")
        tk.Label(dlg, text="质量:", bg="white", font=("Segoe UI", 9)
                 ).grid(row=0, column=0, padx=(14, 6), pady=(14, 2), sticky="w")
        ttk.Scale(dlg, from_=10, to=95, variable=var_q, length=220
                  ).grid(row=0, column=1, pady=(14, 2))
        tk.Label(dlg, textvariable=var_q, bg="white", width=3, font=("Segoe UI", 9)
                 ).grid(row=0, column=2, padx=(6, 14))
        tk.Radiobutton(dlg, text="JPG(体积小)", variable=var_fmt, value="JPG", bg="white"
                       ).grid(row=1, column=0, columnspan=3, sticky="w", padx=14)
        tk.Radiobutton(dlg, text="PNG(无损)", variable=var_fmt, value="PNG", bg="white"
                       ).grid(row=2, column=0, columnspan=3, sticky="w", padx=14)
        btns = tk.Frame(dlg, bg="white")
        btns.grid(row=3, column=0, columnspan=3, pady=(8, 12))

        def do_copy():
            try:
                b = io.BytesIO()
                if var_fmt.get() == "JPG":
                    self.base.save(b, "JPEG", quality=int(var_q.get()))
                else:
                    self.base.save(b, "PNG")
                b.seek(0)
                ok = set_dib_to_clipboard(self.app.worker.hwnd,
                                          pil_to_dib(Image.open(b)))
                self.lbl_status.config(text="✅ 已压缩并复制到剪贴板" if ok else "❌ 复制失败")
                dlg.destroy()
            except Exception:
                messagebox.showerror("WinClipboard", "压缩失败。", parent=dlg)

        def do_save():
            ext = ".jpg" if var_fmt.get() == "JPG" else ".png"
            p = filedialog.asksaveasfilename(
                parent=dlg, defaultextension=ext,
                filetypes=[("JPEG 图片", "*.jpg")] if ext == ".jpg" else [("PNG 图片", "*.png")],
                initialfile="clipboard_compressed" + ext)
            if not p:
                return
            try:
                if ext == ".jpg":
                    self.base.save(p, "JPEG", quality=int(var_q.get()))
                else:
                    self.base.save(p, "PNG")
                self.lbl_status.config(text="✅ 已保存: " + p)
                dlg.destroy()
            except Exception:
                messagebox.showerror("WinClipboard", "保存失败。", parent=dlg)

        tk.Button(btns, text="复制到剪贴板", command=do_copy, bg=self.app.ACCENT,
                  fg="white", relief="flat", padx=12, pady=4).pack(side="left", padx=4)
        tk.Button(btns, text="保存文件", command=do_save, relief="flat",
                  padx=12, pady=4).pack(side="left", padx=4)
        tk.Button(btns, text="取消", command=dlg.destroy, relief="flat",
                  padx=12, pady=4).pack(side="left", padx=4)

    def close(self):
        try:
            self.top.destroy()
        except Exception:
            pass
        try:
            self.app._editors.remove(self)
        except Exception:
            pass


# ============================================================
# Apple 风格 UI 基础(圆角绘制 / 时间 / 面板底图 / 导航 / 底栏)
# ============================================================
def _rr(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形(smooth polygon)。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


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


def ai_chat(service, prompt, timeout=90):
    """调用 OpenAI 兼容接口,返回 (ok, 回复文本或错误信息)。阻塞函数,请在后台线程调用。"""
    import urllib.request
    import urllib.error
    base = str(service.get("base_url") or "").rstrip("/")
    if not base:
        return False, "该服务未配置 base_url"
    url = base + "/chat/completions"
    body = json.dumps({
        "model": service.get("model") or "",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(service.get("api_key") or ""),
    }
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        choices = data.get("choices") or [{}]
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return False, "AI 返回为空(响应格式异常)"
        return True, text
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return False, "HTTP %s %s" % (e.code, detail or e.reason)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


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


def make_panel_face(win_w, win_h):
    """生成窗口底图:浅灰面板 + 圆角发丝边框(圆角由窗口区域裁剪实现)。"""
    img = Image.new("RGB", (win_w, win_h), BG_RGB)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((SHD + 1, SHD + 1, win_w - SHD - 2, win_h - SHD - 2),
                        radius=PANEL_R, outline=(232, 232, 237), width=2)
    return img


def apply_round_region(hwnd, w, h, r=PANEL_R):
    """用 SetWindowRgn 裁剪窗口为圆角矩形(普通窗口,定位可靠)。"""
    try:
        gdi = ctypes.WinDLL("gdi32")
        user32 = ctypes.WinDLL("user32")
        rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def env_scale():
    """检测环境 DWM 位图缩放倍率(无感知进程的窗口会被系统放大)。

    正常桌面返回 1.0;虚拟化会话(如 RDP 缩放)返回 >1,用于坐标补偿。
    """
    try:
        a, b = wintypes.POINT(), wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(a))
        user32.GetPhysicalCursorPos(ctypes.byref(b))
        if b.x > 0 and abs(a.x - b.x) > 4:
            return max(1.0, a.x / float(b.x))
    except Exception:
        pass
    return 1.0


_measure_cache = {}


def _mmeasure(font, text):
    """带缓存的文本像素宽度测量(CJK 测量慢,避免重复)。"""
    key = (font, text)
    v = _measure_cache.get(key)
    if v is None:
        v = tkfont.Font(font=font).measure(text)
        if len(_measure_cache) > 3000:
            _measure_cache.clear()
        _measure_cache[key] = v
    return v


class SegNav(tk.Canvas):
    """分类导航:Apple 风格分段文字,选中项浅灰胶囊。"""

    ITEMS = ("全部", "文本", "图片", "链接", "文件")
    CATS = ("all", "text", "image", "link", "files")

    def __init__(self, parent, app):
        super().__init__(parent, bg=app.BG, highlightthickness=0, bd=0, height=30)
        self.app = app
        self._xpos = []
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._click)

    def _redraw(self):
        self.delete("all")
        self._xpos = []
        x = 0
        for i, label in enumerate(self.ITEMS):
            active = (self.app.category == self.CATS[i])
            font = (self.app.F_FAMILY, 12, "bold") if active else ("Segoe UI", 12)
            w = _mmeasure(font, label) + 26
            if active:
                _rr(self, x, 3, x + w, 27, 9, fill="#E8E8ED", outline="")
            self.create_text(x + w / 2, 15, text=label,
                             fill=self.app.TEXT if active else self.app.TER,
                             font=font)
            self._xpos.append((x, x + w, self.CATS[i]))
            x += w + 6

    def _click(self, e):
        for x1, x2, cat in self._xpos:
            if x1 <= e.x <= x2:
                self.app.set_category(cat)
                return


class FootBar(tk.Canvas):
    """极简底栏:左侧计数,右侧 Apple 键盘徽章。"""

    def __init__(self, parent, app):
        super().__init__(parent, bg=app.BG, highlightthickness=0, bd=0, height=34)
        self.app = app
        self._n = 0
        self._f = tkfont.Font(font=("Segoe UI", 10))
        self.bind("<Configure>", lambda e: self._redraw())

    def set_count(self, total, shown):
        self._n = total
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.create_text(2, h / 2, text="%d 个项目" % self._n, anchor="w",
                         fill=self.app.TER, font=("Segoe UI", 11))
        self.create_line(0, 0, w, 0, fill="#ECECEF")
        x = w - 2
        for b in ("Esc", "Enter", "↑ ↓"):
            bw = _mmeasure(("Segoe UI", 10), b) + 14
            x -= bw
            _rr(self, x, h / 2 - 9, x + bw, h / 2 + 9, 5, fill="#ECECEF", outline="")
            self.create_text(x + bw / 2, h / 2, text=b, fill=self.app.SEC,
                             font=("Segoe UI", 10))
            x -= 8


def make_thumb(path, size=(92, 68)):
    """生成圆角缩略图 PhotoImage(供卡片与预热共用)。"""
    try:
        im = Image.open(path).convert("RGB")
        im = ImageOps.fit(im, size, RESAMPLE, centering=(0.5, 0.5))
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size[0] - 1, size[1] - 1), radius=10, fill=255)
        rgba = Image.new("RGBA", size, (0, 0, 0, 0))
        rgba.paste(im, (0, 0), mask)
        return ImageTk.PhotoImage(rgba)
    except Exception:
        return None


# ============================================================
# 卡片流(自绘滚动列表,替换 Treeview)
# ============================================================
class CardList(tk.Canvas):
    """Apple 风格卡片流:分区标签 + 圆角卡片 + 悬停操作。"""

    R = 14                 # 卡片圆角
    GAP = 10               # 卡片间距
    THUMB = (92, 68)       # 图片缩略图尺寸

    def __init__(self, parent, app):
        super().__init__(parent, bg=app.BG, highlightthickness=0, bd=0,
                         yscrollincrement=1, confine=True)
        self.app = app
        self._view = []            # 过滤后的 history 索引
        self._rows = []            # [("label",y,text) | ("card",y,h,idx,vi)]
        self._total = 1
        self._sel = None
        self._hover = None
        self._btn_hover = None
        self._btn_rects = []
        self._thumbs = {}
        self._fobjs = {}
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Double-1>", self._on_dclick)
        self.bind("<Button-3>", self._on_rclick)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Up>", lambda e: self.app._move_sel(-1))
        self.bind("<Down>", lambda e: self.app._move_sel(1))
        self.bind("<Home>", lambda e: self.app._move_sel(-10 ** 6))
        self.bind("<End>", lambda e: self.app._move_sel(10 ** 6))
        self.bind("<Return>", lambda e: self.app.activate_selected())

    # ---- 外部接口 ----
    def set_view(self, view):
        self._view = list(view)
        self._sel = 0 if self._view else None
        self._hover = None
        self._btn_hover = None
        self.yview_moveto(0)
        self._redraw()

    def selected(self):
        return self._sel

    def select(self, vi):
        if vi is None or 0 <= vi < len(self._view):
            if vi != self._sel:
                self._sel = vi
                self._redraw()

    def view_index(self, vi):
        if vi is None or not (0 <= vi < len(self._view)):
            return None
        return self._view[vi]

    def move_sel(self, delta):
        if not self._view:
            return
        cur = self._sel if self._sel is not None else (0 if delta > 0 else -1)
        cur = max(0, min(len(self._view) - 1, cur + delta))
        if cur != self._sel:
            self._sel = cur
            self._redraw()
        self.see(cur)

    def see(self, vi):
        for r in self._rows:
            if r[0] == "card" and r[4] == vi:
                top = self.canvasy(0)
                bot = top + self.winfo_height()
                y, h = r[1], r[2]
                if y < top + 4:
                    self.yview_moveto(max(0, y - 4) / self._total)
                elif y + h > bot - 4:
                    self.yview_moveto((y + h - self.winfo_height() + 4) / self._total)
                self._draw_scroll_indicator()
                return

    def content_height(self):
        return self._total

    # ---- 测量 ----
    def _measure(self):
        self._rows = []
        y = 4
        pinned = [i for i in self._view if self.app.history[i].get("pinned")]
        rest = [i for i in self._view if not self.app.history[i].get("pinned")]
        vi = 0
        for label, idxs in (("已固定", pinned), ("最近项目", rest)):
            if not idxs:
                continue
            self._rows.append(("label", y, label))
            y += 26
            for idx in idxs:
                h = self._card_h(idx)
                self._rows.append(("card", y, h, idx, vi))
                y += h + self.GAP
                vi += 1
            y += 10
        self._total = max(y, 1)
        return self._total

    def _card_h(self, idx):
        h = self.app.history[idx]
        t = h["type"]
        if t == "image":
            return self.THUMB[1] + 24          # 92
        if t == "files":
            return 70
        if url_info(h["content"]):
            return 80
        lines = [l for l in h["content"].strip().splitlines() if l.strip()]
        return 86 if len(lines) > 1 else 58

    # ---- 渲染 ----
    def _f(self, font):
        f = self._fobjs.get(font)
        if f is None:
            f = tkfont.Font(font=font)
            self._fobjs[font] = f
        return f

    def _el(self, text, maxw, font):
        """按像素宽度截断文本(缓存测量 + 比例估算,避免慢速字体测量)。"""
        if not text:
            return ""
        s = text.replace("\n", "  ")
        full = _mmeasure(font, s)
        if full <= maxw:
            return s
        # 粗略按比例估算截断点,再局部微调(通常 ±2 次测量)
        n = max(1, int(len(s) * maxw / max(1, full)))
        while n > 1 and _mmeasure(font, s[:n] + "…") > maxw:
            n -= 1
        while n < len(s) and _mmeasure(font, s[:n + 1] + "…") <= maxw:
            n += 1
        return s[:n] + "…"

    def _redraw(self):
        self.delete("all")
        self._btn_rects = []
        w = max(self.winfo_width(), 1)
        if not self._view:
            self.configure(scrollregion=(0, 0, 1, 1))
            self.create_text(w / 2, 72, text="暂无剪贴内容",
                             fill=self.app.TER, font=("Segoe UI", 13))
            return
        total = self._measure()
        self.configure(scrollregion=(0, 0, w, total))
        cw = w - 4
        for r in self._rows:
            if r[0] == "label":
                self.create_text(6, r[1] + 12, text=r[2], anchor="w",
                                 fill=self.app.TER, font=self.app.fonts["section"])
            else:
                self._draw_card(r[4], r[1], cw, r[2])
        self._draw_scroll_indicator()

    def _draw_card(self, vi, y, w, h):
        app = self.app
        idx = self._view[vi]
        rec = app.history[idx]
        sel = (vi == self._sel)
        hov = (vi == self._hover)
        yy = y - 1 if hov else y
        x = 2
        if sel:
            _rr(self, x + 2, yy + 4, x + w - 2, yy + h + 2, self.R,
                fill="#E3E3E7", outline="")
        _rr(self, x, yy, x + w, yy + h, self.R,
            fill="#FFFFFF" if (sel or hov) else "#FDFDFE",
            outline=app.ACCENT if sel else ("#E4E4E9" if hov else ""),
            width=1.5 if sel else 1)
        if sel:
            self.create_oval(x + 10, yy + h / 2 - 3, x + 16, yy + h / 2 + 3,
                             fill=app.ACCENT, outline="")
        cx = x + 24
        if rec["type"] == "image":
            cx = self._draw_thumb(idx, x + 14, yy + (h - self.THUMB[1]) / 2)
            tx = cx + 14
            self.create_text(tx, yy + 16, text="截图", anchor="w",
                             fill=app.TEXT, font=app.fonts["title"])
            self.create_text(tx, yy + 38, anchor="w", fill=app.SEC,
                             font=app.fonts["body"],
                             text="%s × %s" % (rec.get("w", 0), rec.get("h", 0)))
            self.create_text(tx, yy + h - 16, text=self._meta(rec), anchor="w",
                             fill=app.TER, font=app.fonts["meta"])
        elif rec["type"] == "files":
            names = rec["content"]
            name = os.path.basename(names[0])
            ext = os.path.splitext(name)[1].lstrip(".").upper()[:4] or "FILE"
            bx, by = x + 14, yy + (h - 40) / 2
            _rr(self, bx, by, bx + 40, by + 40, 9, fill="#EFF1F5", outline="")
            self.create_text(bx + 20, by + 20, text=ext,
                             fill=app.SEC, font=("Segoe UI", 9, "bold"))
            tx = bx + 40 + 14
            title = name + (" 等 %d 个" % len(names) if len(names) > 1 else "")
            self.create_text(tx, yy + 18, anchor="w", fill=app.TEXT,
                             font=app.fonts["title"],
                             text=self._el(title, w - tx - 44, app.fonts["title"]))
            size = ""
            try:
                total = sum(os.path.getsize(p) for p in names if os.path.exists(p))
                size = _fmt_size(total)
            except Exception:
                pass
            self.create_text(tx, yy + h - 16, anchor="w", fill=app.TER,
                             font=app.fonts["meta"],
                             text=self._meta(rec, extra=(ext, size)))
        elif url_info(rec["content"]):
            host = url_info(rec["content"])[0]
            fx, fy = x + 14, yy + (h - 28) / 2
            self.create_oval(fx, fy, fx + 28, fy + 28, fill="#EFF1F5", outline="")
            self.create_text(fx + 14, fy + 14, text=host[0].upper(),
                             fill=app.SEC, font=("Segoe UI", 12, "bold"))
            tx = fx + 28 + 12
            self.create_text(tx, yy + 16, anchor="w", fill=app.TEXT,
                             font=app.fonts["title"],
                             text=self._el(host, w - tx - 46, app.fonts["title"]))
            self.create_text(tx, yy + 38, anchor="w", fill=app.SEC,
                             font=app.fonts["body"],
                             text=self._el(rec["content"].strip(), w - tx - 46,
                                           app.fonts["body"]))
            self.create_text(tx, yy + h - 15, anchor="w", fill=app.TER,
                             font=app.fonts["meta"],
                             text="链接 · " + _rel_time(rec.get("ts", 0)))
        else:
            lines = [l for l in rec["content"].strip().splitlines() if l.strip()]
            self.create_text(cx, yy + 17, anchor="w", fill=app.TEXT,
                             font=app.fonts["title"],
                             text=self._el(lines[0], w - cx - 46, app.fonts["title"]))
            if len(lines) > 1:
                self.create_text(cx, yy + 38, anchor="w", fill=app.SEC,
                                 font=app.fonts["body"],
                                 text=self._el("\n".join(lines[1:3]),
                                               w - cx - 46, app.fonts["body"]))
            self.create_text(cx, yy + h - 15, anchor="w", fill=app.TER,
                             font=app.fonts["meta"], text=self._meta(rec))
        if hov:
            self._draw_hover_btns(vi, x + w, yy + h / 2)

    def _meta(self, rec, extra=None):
        t = rec["type"]
        label = {"text": "文本", "image": "PNG", "files": (extra[0] if extra else "文件")}[t]
        parts = [label]
        if extra and extra[1]:
            parts.append(extra[1])
        parts.append(_rel_time(rec.get("ts", 0)))
        return " · ".join(p for p in parts if p)

    def _draw_thumb(self, idx, x, y):
        rec = self.app.history[idx]
        p = rec["content"]
        ph = self._thumbs.get(p)
        if ph is None:
            ph = make_thumb(p)
            if ph is None:
                return x + self.THUMB[0]
            self._thumbs[p] = ph
        self.create_image(x, y, image=ph, anchor="nw")
        return x + self.THUMB[0]

    def prewarm_thumbs(self):
        """后台预热缩略图缓存(避免首次弹出时卡顿)。"""
        for h in self.app.history:
            if h.get("type") == "image":
                p = h.get("content", "")
                if p and os.path.exists(p) and p not in self._thumbs:
                    ph = make_thumb(p)
                    if ph is not None:
                        self._thumbs[p] = ph

    def _draw_hover_btns(self, vi, right, cy):
        x = right - 18
        for act, glyph in (("more", "⋯"), ("del", "删"), ("pin", "钉"), ("paste", "贴")):
            hov = (self._btn_hover == act and self._hover == vi)
            _rr(self, x - 15, cy - 15, x + 15, cy + 15, 15,
                fill="#EDEFF3" if hov else "#FFFFFF", outline="#E4E4E9")
            self.create_text(x, cy, text=glyph, fill=self.app.SEC,
                             font=("Segoe UI", 10))
            self._btn_rects.append((act, x - 15, cy - 15, x + 15, cy + 15))
            x -= 36

    def _draw_scroll_indicator(self):
        self.delete("sb")
        h = self.winfo_height()
        if self._total <= h + 4:
            return
        frac = h / self._total
        bar_h = max(30, h * frac)
        span = self._total - h
        pos = self.canvasy(0) / span if span else 0
        x = self.winfo_width() - 5
        y = (h - bar_h) * pos
        _rr(self, x, y, x + 3, y + bar_h, 2, fill="#D9D9DE", outline="", tags=("sb",))

    # ---- 交互 ----
    def _hit(self, cx, cy):
        for act, x1, y1, x2, y2 in self._btn_rects:
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                return ("btn", act, self._hover)
        for r in self._rows:
            if r[0] != "card":
                continue
            y, h, vi = r[1], r[2], r[4]
            if y - 1 <= cy <= y + h + 1 and 2 <= cx <= self.winfo_width() - 2:
                return ("card", vi)
        return (None,)

    def _on_click(self, e):
        self.focus_set()
        hit = self._hit(e.x, e.y)
        if hit[0] == "btn":
            self.app.card_action(hit[1], hit[2])
        elif hit[0] == "card":
            if hit[1] != self._sel:
                self._sel = hit[1]
                self._redraw()
        elif self._sel is not None:
            self._sel = None
            self._redraw()

    def _on_dclick(self, e):
        hit = self._hit(e.x, e.y)
        if hit[0] == "card":
            if hit[1] != self._sel:
                self._sel = hit[1]
                self._redraw()
            self.app.activate_selected()

    def _on_rclick(self, e):
        hit = self._hit(e.x, e.y)
        if hit[0] == "card":
            if hit[1] != self._sel:
                self._sel = hit[1]
                self._redraw()
        self.app.show_card_menu(e.x_root, e.y_root)

    def _on_motion(self, e):
        hit = self._hit(e.x, e.y)
        hov = hit[1] if hit[0] == "card" else None
        btn = hit[1] if hit[0] == "btn" else None
        if hov != self._hover or btn != self._btn_hover:
            self._hover = hov
            self._btn_hover = btn
            self._redraw()

    def _set_hover(self, hv):
        if hv != self._hover or self._btn_hover is not None:
            self._hover = hv
            self._btn_hover = None
            self._redraw()

    def _on_wheel(self, e):
        self.yview_scroll(int(-e.delta / 120) * 3, "units")
        self._draw_scroll_indicator()


# ============================================================
# 主界面(tkinter, 主线程)
# ============================================================
class ClipboardApp:
    # ---- Apple 风格配色 ----
    ACCENT = "#0071E3"
    ACCENT_SOFT = "#E3EFFC"
    BG = "#F5F5F7"
    TEXT = "#1D1D1F"
    SEC = "#6E6E73"
    TER = "#86868B"
    DIV = "#E8E8ED"
    KEY = "#FE00FE"
    PLACEHOLDER = "搜索剪贴板内容"
    PANEL_W = 600
    PANEL_H_MAX = 720
    F_FAMILY = "Segoe UI"   # 实际字体在 __init__ 中按可用性解析(需根窗口)

    # ---- 核心状态别名(只读代理到 self.core;容器原地修改,引用稳定) ----
    @property
    def cfg(self):
        return self.core.cfg

    @property
    def history(self):
        return self.core.history

    @property
    def private(self):
        return self.core.private

    @property
    def worker(self):
        return self.core.worker

    @property
    def signal_q(self):
        return self.core.signal_q

    def _on_history_change(self):
        """核心投递的 "change" 信号:剪贴捕获入史后,面板可见时刷新列表。"""
        if self._visible():
            self.refresh_list(self._query())

    def __init__(self):
        self.category = "all"           # all/text/image/link/files
        self._view = []          # 历史索引视图(由 CardList 维护)
        self._editors = []       # 打开的图片编辑器
        self._warned_hotkey = False
        self._search_focused = False
        # 环境 DWM 缩放补偿(正常桌面为 1.0)
        self._scale = env_scale()

        # 共享核心(状态加载 + 历史迁移 + 后台线程启动)
        self.core = ClipboardCore()

        # 主窗口
        self.root = tk.Tk()
        self.root.title("WinClipboard")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.report_callback_exception = self._tk_exc
        try:
            if "Segoe UI Semibold" in set(tkfont.families(self.root)):
                self.F_FAMILY = "Segoe UI Semibold"
        except Exception:
            pass
        log("init: fonts done")
        self._build_ui()
        log("init: build ui done")
        self.root.withdraw()

        self.root.after(30, self._poll)
        # 后台预热文本测量与缩略图(把首次弹出成本移出热键路径)
        self.root.after(600, self._prewarm)
        log("init: mainloop start")
        self.root.mainloop()

    def _prewarm(self):
        try:
            t0 = time.time()
            for h in self.history:
                t = h.get("type")
                if t == "text":
                    for s in [l for l in h["content"].strip().splitlines() if l.strip()][:3]:
                        _mmeasure(self.fonts["title"], s)
                        _mmeasure(self.fonts["body"], s)
                    if url_info(h["content"]):
                        _mmeasure(self.fonts["body"], h["content"].strip())
                elif t == "files" and h.get("content"):
                    _mmeasure(self.fonts["title"], os.path.basename(h["content"][0]))
            self.cl.prewarm_thumbs()
            log("prewarm done %.0fms" % ((time.time() - t0) * 1000))
        except Exception:
            log("prewarm failed:\n%s" % traceback.format_exc())

    # ---- UI 构建 ----
    def _build_ui(self):
        self.root.configure(bg=self.BG)

        self.fonts = {
            "app": (self.F_FAMILY, 17),
            "title": (self.F_FAMILY, 14),
            "body": ("Segoe UI", 13),
            "meta": ("Segoe UI", 11),
            "section": (self.F_FAMILY, 12),
        }

        # 面板底图(发丝边框,圆角由窗口区域实现)
        self.face = tk.Label(self.root, bg=self.BG, bd=0)

        # 头部
        self.hdr = tk.Frame(self.root, bg=self.BG)
        tk.Label(self.hdr, text="WinClipboard", bg=self.BG, fg=self.TEXT,
                 font=self.fonts["app"]).pack(side="left")
        tk.Label(self.hdr, text="Ctrl + Shift + V", bg=self.BG, fg=self.TER,
                 font=("Segoe UI", 11), padx=10).pack(side="right")
        self.close_lbl = tk.Label(self.hdr, text="✕", bg=self.BG, fg=self.TER,
                                  font=("Segoe UI", 11), cursor="hand2", padx=8)
        self.close_lbl.pack(side="right")
        self.close_lbl.bind("<Button-1>", lambda e: self.hide())
        self.close_lbl.bind("<Enter>", lambda e: self.close_lbl.config(fg=self.TEXT))
        self.close_lbl.bind("<Leave>", lambda e: self.close_lbl.config(fg=self.TER))
        for w in (self.hdr,):
            w.bind("<ButtonPress-1>", self._start_move)
            w.bind("<B1-Motion>", self._do_move)

        # 搜索框(Spotlight 风格白色圆角壳 + 真实输入框)
        self.search_shell = tk.Frame(self.root, bg=self.BG)
        self.search_cv = tk.Canvas(self.search_shell, bg=self.BG,
                                   highlightthickness=0, bd=0)
        self.search_cv.place(x=0, y=0, relwidth=1, relheight=1)
        self.search_cv.bind("<Configure>", lambda e: self._draw_search())
        self.search_icon = tk.Label(self.search_shell, text="⌕", bg="#FFFFFF",
                                    fg=self.TER, font=("Segoe UI", 16))
        self.var_search = tk.StringVar()
        self.ent_search = tk.Entry(self.search_shell, textvariable=self.var_search,
                                   font=("Segoe UI", 15), relief="flat", bd=0,
                                   bg="#FFFFFF", fg=self.TER,
                                   insertbackground=self.ACCENT,
                                   highlightthickness=0)
        self.ent_search.bind("<FocusIn>", self._search_in)
        self.ent_search.bind("<FocusOut>", self._search_out)
        self.ent_search.bind("<KeyRelease>", lambda e: self._on_search())
        self.ent_search.bind("<Return>", lambda e: self.activate_selected())
        self.ent_search.bind("<Escape>", lambda e: self.hide())
        self.ent_search.bind("<Down>", lambda e: self._move_sel(1))
        self.ent_search.bind("<Up>", lambda e: self._move_sel(-1))

        # 分类导航 / 卡片流 / 底栏
        self.nav = SegNav(self.root, self)
        self.cl = CardList(self.root, self)
        self.foot = FootBar(self.root, self)
        self.menu = tk.Menu(self.root, tearoff=0)   # 内容在每次右键时动态重建

        self.root.bind("<FocusOut>", self._on_focusout)
        self.root.bind("<Escape>", lambda e: self.hide())
        self._search_dirty = False
        self._face_ph = None

    def _layout(self):
        """按窗口尺寸放置内容(窗口含阴影边距)。"""
        w, h = self.root.winfo_width(), self.root.winfo_height()
        pw = w - 2 * SHD
        ph = h - 2 * SHD
        pad = 20
        self.face.place(x=0, y=0, relwidth=1, relheight=1)
        self.face.lower()   # 底图置于所有内容之下
        self.hdr.place(x=SHD + pad, y=SHD + 16, width=pw - pad * 2, height=26)
        # 搜索子组件坐标相对 search_shell
        self.search_icon.place(x=14, y=12)
        self.ent_search.place(x=42, y=4, height=40, relwidth=1, width=-(42 + 14))
        self.search_shell.place(x=SHD + pad, y=SHD + 56, width=pw - pad * 2, height=48)
        self.nav.place(x=SHD + pad - 4, y=SHD + 116, width=pw - pad * 2, height=30)
        self.cl.place(x=SHD + pad - 2, y=SHD + 150, width=pw - pad * 2 + 4,
                      height=max(60, ph - 150 - 50))
        self.foot.place(x=SHD + pad, y=SHD + ph - 40, width=pw - pad * 2, height=34)

    def _draw_search(self):
        cv = self.search_cv
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 10 or h < 10:
            return
        focused = self._search_focused
        if focused:
            _rr(cv, 1.5, 1.5, w - 1.5, h - 1.5, 14, fill="#FFFFFF",
                outline="#B7D4F5", width=3)
        _rr(cv, 1, 1, w - 1, h - 1, 14, fill="#FFFFFF",
            outline=self.ACCENT if focused else "#E8E8ED", width=1)

    # ---- 搜索框行为 ----
    def _search_in(self, _=None):
        self._search_focused = True
        self._draw_search()
        if not self._search_dirty and self.var_search.get() == self.PLACEHOLDER:
            self.var_search.set("")
            self.ent_search.config(fg=self.TEXT)
            self._search_dirty = True

    def _search_out(self, _=None):
        self._search_focused = False
        self._draw_search()
        if self.var_search.get() == "":
            self.var_search.set(self.PLACEHOLDER)
            self.ent_search.config(fg=self.TER)
            self._search_dirty = False

    def _on_search(self):
        t = self.var_search.get()
        if t == self.PLACEHOLDER or t == "":
            if t == "" and self._search_dirty:
                self.var_search.set(self.PLACEHOLDER)
                self.ent_search.config(fg=self.TER)
                self._search_dirty = False
            self.refresh_list("")
            return
        self._search_dirty = True
        self.ent_search.config(fg=self.TEXT)
        self.refresh_list(t)

    def _query(self):
        t = self.var_search.get()
        return "" if t == self.PLACEHOLDER else t

    def set_category(self, cat):
        if cat == self.category:
            return
        self.category = cat
        self.refresh_list(self._query())

    # ---- 窗口拖动 ----
    def _start_move(self, e):
        self._mx, self._my = e.x_root, e.y_root

    def _do_move(self, e):
        x = self.root.winfo_x() + (e.x_root - self._mx)
        y = self.root.winfo_y() + (e.y_root - self._my)
        self._mx, self._my = e.x_root, e.y_root
        self.root.geometry(f"+{x}+{y}")

    # ---- 信号循环 ----
    def _poll(self):
        if not getattr(self, "_poll_logged", False):
            self._poll_logged = True
            log("poll: first tick")
        try:
            while True:
                kind, *rest = self.signal_q.get_nowait()
                try:
                    self._handle_signal(kind, rest)
                except Exception:
                    log("处理信号 %r 失败:\n%s" % (kind, traceback.format_exc()))
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def _handle_signal(self, kind, rest):
        if kind == "clip":
            item = rest[0]
            if item:
                self.core.add_item(item)
        elif kind == "change":
            self._on_history_change()
        elif kind == "hotkey":
            self.toggle()
        elif kind == "tray":
            act = rest[0]
            if act == "show":
                self.toggle()
            elif act == "clear":
                self.clear_all()
            elif act == "private":
                self.show_private()
            elif act == "autostart":
                self.core.toggle_autostart()
            elif act == "about":
                self.about()
            elif act == "hotkey_fail":
                self._warn_hotkey()
            elif act == "quit":
                self.quit_app()

    def _warn_hotkey(self):
        if self._warned_hotkey:
            return
        self._warned_hotkey = True
        messagebox.showwarning(
            "WinClipboard",
            "全局热键(Ctrl+Shift+V)注册失败,可能被其他软件占用。\n"
            "可在 {} 中修改 hotkey_mod / hotkey_key 后重启。".format(CONFIG_FILE),
            parent=self.root)

    # ---- 剪贴板读取 / 历史维护:见 ClipboardCore(共享后端) ----

    # ---- 列表显示 / 过滤 ----
    def refresh_list(self, query=""):
        view = self.core.filter_view(self.category, query)
        self.cl.set_view(view)
        self.foot.set_count(len(self.history), len(view))

    # ---- 选择 / 粘贴 ----
    def _selected_index(self):
        return self.cl.view_index(self.cl.selected())

    def _move_sel(self, delta):
        self.cl.move_sel(delta)

    def activate_selected(self):
        vi = self.cl.selected()
        hi = self.cl.view_index(vi)
        if hi is None:
            return
        self._paste_record(self.history[hi])

    def _paste_record(self, rec):
        if not self.core.paste_record(rec):
            messagebox.showwarning("WinClipboard", "文件已不存在,无法粘贴。",
                                   parent=self.root)
            return
        self.hide()

    def card_action(self, act, vi):
        """卡片悬停操作按钮:粘贴 / 固定 / 删除 / 更多。"""
        if vi is None:
            return
        self.cl.select(vi)
        if act == "paste":
            self.activate_selected()
        elif act == "pin":
            self.toggle_pin()
        elif act == "del":
            self.delete_selected()
        elif act == "more":
            self.show_card_menu(
                self.cl.winfo_rootx() + self.cl.winfo_width() - 60,
                self.cl.winfo_rooty() + 40)

    def show_card_menu(self, xr, yr):
        vi = self.cl.selected()
        self.menu.delete(0, "end")
        self.menu.add_command(label="粘贴", command=self.activate_selected)
        hi = self.cl.view_index(vi)
        if hi is not None:
            rec = self.history[hi]
            if rec["type"] == "image":
                self.menu.add_command(label="图片处理…", command=self.open_editor)
            self.menu.add_command(
                label="取消固定" if rec.get("pinned") else "固定",
                command=self.toggle_pin)
            self.menu.add_command(label="删除", command=self.delete_selected)
        try:
            self.menu.tk_popup(xr, yr)
        finally:
            self.menu.grab_release()

    def open_editor(self):
        hi = self._selected_index()
        if hi is None:
            return
        rec = self.history[hi]
        if rec["type"] != "image":
            return
        if not os.path.exists(rec.get("content", "")):
            messagebox.showwarning("WinClipboard", "图片文件不存在,可能已被清理。",
                                   parent=self.root)
            return
        self._editors.append(ImageEditor(self, rec))

    def toggle_pin(self):
        hi = self._selected_index()
        if hi is None:
            return
        self.core.toggle_pin_at(hi)
        self.refresh_list(self._query())

    def delete_selected(self):
        hi = self._selected_index()
        if hi is None:
            return
        self.core.delete_at(hi)
        self.refresh_list(self._query())

    # ---- 显隐 ----
    def _visible(self):
        # 分层透明窗口用 winfo_viewable 不可靠,以 Win32 IsWindowVisible 为准
        try:
            return bool(win32gui.IsWindowVisible(self.root.winfo_id()))
        except Exception:
            return False

    @staticmethod
    def _monitor_rect(x, y):
        try:
            mon = win32api.MonitorFromPoint((x, y), win32con.MONITOR_DEFAULTTONEAREST)
            info = win32api.GetMonitorInfo(mon)
            r = info["Monitor"]
            return r[0], r[1], r[2], r[3]
        except Exception:
            sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            return 0, 0, sw, sh

    def show(self):
        # 重置搜索框为占位符
        if self._search_dirty:
            self.var_search.set(self.PLACEHOLDER)
            self.ent_search.config(fg=self.TER)
            self._search_dirty = False
        self.refresh_list("")
        W = self.PANEL_W
        # 按内容高度计算面板高度(上限 720)
        H = min(max(420, 210 + self.cl.content_height()), self.PANEL_H_MAX)
        x, y = win32gui.GetCursorPos()
        scale = self._scale
        if scale == 1.0:
            ml, mt, mr, mb = self._monitor_rect(x, y)   # 按鼠标所在显示器 clamp(多屏)
        else:
            # 虚拟化环境:物理坐标计算,几何坐标按倍率换算补偿
            try:
                pt = wintypes.POINT()
                user32.GetPhysicalCursorPos(ctypes.byref(pt))
                x, y = pt.x, pt.y
            except Exception:
                pass
            sw = int(win32api.GetSystemMetrics(win32con.SM_CXSCREEN) / scale)
            sh = int(win32api.GetSystemMetrics(win32con.SM_CYSCREEN) / scale)
            ml, mt, mr, mb = 0, 0, sw, sh
        win_w, win_h = W + 2 * SHD, H + 2 * SHD
        wx, wy = x - SHD, y - SHD
        if wx + win_w > mr - 8:
            wx = mr - win_w - 8
        if wy + win_h > mb - 8:
            wy = mb - win_h - 8
        wx = max(ml, wx)
        wy = max(mt, wy)
        self.root.geometry("%dx%d+%d+%d" % (int(win_w / scale), int(win_h / scale),
                                            int(wx * scale), int(wy * scale)))
        # 面板底图(按实际窗口尺寸生成)
        try:
            fw = max(self.root.winfo_width(), win_w)
            fh = max(self.root.winfo_height(), win_h)
            self._face_ph = ImageTk.PhotoImage(make_panel_face(fw, fh))
            self.face.config(image=self._face_ph)
        except Exception:
            pass
        self._layout()
        self.root.deiconify()
        # 圆角窗口区域裁剪(普通窗口,定位可靠)
        try:
            apply_round_region(self.root.winfo_id(),
                               self.root.winfo_width(), self.root.winfo_height())
        except Exception:
            pass
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.ent_search.focus_set()
        self.cl.select(0)

    def hide(self):
        self.root.withdraw()

    def toggle(self):
        if self._visible():
            self.hide()
        else:
            self.show()

    def _on_focusout(self, _):
        self.root.after(160, self._check_focus)

    def _check_focus(self):
        if not self._visible():
            return
        try:
            f = self.root.focus_get()
        except Exception:
            f = None
        if f is None:
            self.hide()

    # ---- 菜单动作 ----
    def _flat_btn(self, parent, text, cmd, primary=False, side="left", padx=4):
        """扁平按钮:白底发丝边框,主按钮为纯色填充。"""
        b = tk.Button(parent, text=text, command=cmd, relief="flat",
                      borderwidth=0, cursor="hand2",
                      bg=self.ACCENT if primary else self.BG,
                      fg="white" if primary else self.TEXT,
                      activebackground=self.ACCENT if primary else self.ACCENT_SOFT,
                      activeforeground="white" if primary else self.TEXT,
                      highlightthickness=1,
                      highlightbackground=self.ACCENT if primary else self.DIV,
                      font=("Segoe UI", 9), padx=12, pady=4)
        b.pack(side=side, padx=padx)
        return b

    def show_private(self):
        """私密内容窗口:敏感信息隔离区,双击/回车复制并粘贴。"""
        if not self.private:
            messagebox.showinfo("私密内容", "暂无敏感内容。", parent=self.root)
            return
        top = tk.Toplevel(self.root)
        top.title("私密内容(敏感信息隔离区)")
        top.configure(bg=self.BG)
        top.attributes("-topmost", True)
        top.geometry("460x340")
        tk.Label(top, text="以下内容保存在本机私密区,不出现在剪贴板历史面板中。",
                 bg=self.BG, fg=self.TER, font=("Segoe UI", 8), anchor="w",
                 justify="left").pack(fill="x", padx=12, pady=(10, 4))
        body = tk.Frame(top, bg=self.BG)
        body.pack(fill="both", expand=True, padx=12)
        sb = tk.Scrollbar(body)
        sb.pack(side="right", fill="y")
        lb = tk.Listbox(body, font=("Segoe UI", 9), yscrollcommand=sb.set,
                        bg="#fafbfd", fg=self.TEXT,
                        selectbackground=self.ACCENT_SOFT, selectforeground=self.TEXT,
                        relief="flat", highlightthickness=1,
                        highlightbackground=self.DIV, borderwidth=0)
        lb.pack(side="left", fill="both", expand=True)
        sb.config(command=lb.yview)
        for s in self.private:
            one = s.replace("\n", " ").replace("\t", " ")
            if len(one) > 60:
                one = one[:60] + "…"
            lb.insert("end", one)
        foot = tk.Frame(top, bg=self.BG)
        foot.pack(fill="x", padx=12, pady=(6, 12))

        def sel():
            c = lb.curselection()
            return c[0] if c else None

        def do_copy():
            i = sel()
            if i is None:
                return
            self.core.paste_text(self.private[i])
            top.destroy()

        def do_delete():
            i = sel()
            if i is None:
                return
            self.private.pop(i)
            save_private(self.private)
            lb.delete(i)

        def do_clear():
            if not messagebox.askyesno("私密内容", "确定清空全部私密内容?", parent=top):
                return
            self.private.clear()   # 原地清空,保持核心引用有效
            save_private(self.private)
            lb.delete(0, "end")

        lb.bind("<Double-Button-1>", lambda e: do_copy())
        lb.bind("<Return>", lambda e: do_copy())
        self._flat_btn(foot, "复制并粘贴", do_copy, primary=True)
        self._flat_btn(foot, "删除", do_delete)
        self._flat_btn(foot, "清空", do_clear)
        self._flat_btn(foot, "关闭", top.destroy, side="right")

    def _tk_exc(self, exc, val, tb):
        _log_exception("Tk 回调异常", exc, val, tb)
        try:
            sys.__stderr__.write("Exception in Tkinter callback: %s\n" % val)
        except Exception:
            pass

    def clear_all(self):
        self.core.clear_unpinned()
        if self._visible():
            self.refresh_list(self._query())

    def about(self):
        top = tk.Toplevel(self.root)
        top.title("关于 WinClipboard")
        top.configure(bg=self.BG)
        top.attributes("-topmost", True)
        tk.Label(top, text="●  WinClipboard", font=("Segoe UI", 14, "bold"),
                 bg=self.BG, fg=self.ACCENT).pack(pady=(18, 0))
        tk.Label(top, bg=self.BG, justify="left", anchor="w",
                 fg=self.TEXT, font=("Segoe UI", 9),
                 text=("Windows 桌面剪贴板管理器\n"
                       "热键: Ctrl+Shift+V 弹出/隐藏\n"
                       "左键托盘图标: 切换面板\n"
                       "右键托盘图标: 菜单\n\n"
                       "数据目录:\n" + APP_DIR)).pack(padx=30, pady=10)
        foot = tk.Frame(top, bg=self.BG)
        foot.pack(pady=(0, 16))
        self._flat_btn(foot, "确定", top.destroy, primary=True, side="top")

    # ---- 退出 ----
    def quit_app(self):
        self.core.save_all()
        self.worker.cmd_q.put(("quit",))
        try:
            self.root.destroy()
        except Exception:
            pass


# ============================================================
# 入口
# ============================================================
def main():
    install_excepthooks()
    ensure_dirs()
    ok, mutex = single_instance()
    if not ok:
        ctypes.windll.user32.MessageBoxW(
            0, "WinClipboard 已经在运行中(检查系统托盘)。", APP_NAME, 0x40)
        return
    # 保持互斥量存活
    globals()["_MUTEX"] = mutex
    log("启动 pid=%d py=%s %s" % (os.getpid(), sys.executable, sys.version.split()[0]))
    try:
        ClipboardApp()
    except Exception:
        log("主线程异常退出:\n" + traceback.format_exc())
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "WinClipboard 发生错误,详情见:\n%s" % LOG_FILE, APP_NAME, 0x10)
        except Exception:
            pass
    log("退出")


if __name__ == "__main__":
    main()
