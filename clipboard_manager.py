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
from app_meta import (APP_NAME, APP_VERSION, REPO_URL, RELEASE_API,
                      RELEASE_API_HOST, RELEASE_API_PATH)
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
_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
APP_DIR = os.path.join(_APPDATA, APP_NAME)
IMAGE_DIR = os.path.join(APP_DIR, "images")      # 旧布局(迁移期兼容)
BLOB_DIR = os.path.join(APP_DIR, "data", "blobs")   # CAS:data/blobs/<2位>/<哈希>
ICON_FILE = os.path.join(APP_DIR, "icon.ico")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
DB_FILE = os.path.join(APP_DIR, "clipboard.db")   # SQLite 事实存储(默认模式)
PRIVATE_FILE = os.path.join(APP_DIR, "private.json")   # 敏感内容隔离区
CTXPACK_FILE = os.path.join(APP_DIR, "context_packs.json")  # AI 上下文包(独立资产)
LOG_FILE = os.path.join(APP_DIR, "app.log")            # 崩溃/运行日志
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ---- 阶段 8 拆分:Windows 适配层迁至 win32_host(以下名兼容重导出) ----
from win32_host import (
    WM_TASKBAR, WM_HOTKEY, WM_CLIPBOARDUPDATE, WM_TIMER, WM_COMMAND,
    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN,
    M_SHOW, M_CLEAR, M_PRIVATE, M_AUTOSTART, M_ABOUT, M_QUIT,
    M_CTT_OFF, M_PAUSE, ID_TIMER, ID_TIMER_CAPTURE,
    ID_HOTKEY, ID_HOTKEY_PIN, ID_HOTKEY_CTT, ID_HOTKEY_QP, ID_HOTKEY_AI,
    _VK_OEM_NAMES, hotkey_label, single_instance, foreground_app_exe,
    png_to_dib, DROPFILES, build_dropfiles,
    MOUSEINPUT, HARDWAREINPUT, INPUT_UNION, INPUT, INPUT_KEYBOARD,
    KEYEVENTF_KEYUP, VK_CONTROL, VK_V, KEYBDINPUT, send_ctrl_v,
    _get_foreground, _get_clip_seq, TrayWorker)



user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# OEM 键的 VK → 显示名(快捷粘贴默认 Alt+`,录制对话框也允许这些键)


DEFAULT_CFG = {
    "hotkey_mod": MOD_CONTROL | MOD_SHIFT,
    "hotkey_key": 0x56,          # 'V'
    "quick_mod": MOD_ALT,
    "quick_key": 0xC0,           # '`'(VK_OEM_3):快捷粘贴浮层,Win+V 式轻入口
    "ai_hotkey_mod": MOD_CONTROL | MOD_ALT,
    "ai_hotkey_key": 0x20,      # Space: quick AI action wheel
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


# 面板内图片缩略图尺寸 / 高质量缩放采样器
THUMB_W, THUMB_H = 88, 44
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS



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


# ---- 启动状态追踪(Safe Mode 判据,建议 20)----
# 语义:begin_boot 在 main() 最早处记 streak+1(本次启动"未确认");
# 启动完整落地后 confirm_boot_ok 清零;正常退出 mark_clean_quit 再落一笔。
# 崩溃(启动中或运行中)到不了 confirm/quit——streak 由此累积,连续
# 未确认达到阈值即视为"连续启动崩溃",由前端提供安全模式选择。
BOOT_STATE_FILE = os.path.join(APP_DIR, "boot_state.json")
SAFE_MODE_THRESHOLD = 2     # 连续第 N 次未确认启动 → 提供安全模式


def read_boot_state():
    """读启动状态 {streak, running, last_boot};文件缺失/损坏视为全新。"""
    try:
        d = json.loads(Path(BOOT_STATE_FILE).read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    try:
        streak = max(0, int(d.get("streak", 0) or 0))
    except (TypeError, ValueError):
        streak = 0
    try:
        last_boot = float(d.get("last_boot", 0.0) or 0.0)
    except (TypeError, ValueError):
        last_boot = 0.0
    return {"streak": streak, "running": bool(d.get("running")),
            "last_boot": last_boot}


def _write_boot_state(streak, running, last_boot=None):
    Path(BOOT_STATE_FILE).write_text(json.dumps(
        {"streak": int(streak), "running": bool(running),
         "last_boot": last_boot if last_boot is not None
         else read_boot_state()["last_boot"]}, ensure_ascii=False),
        encoding="utf-8")


def begin_boot():
    """启动即记一笔:streak+1(本次尚未确认成功),返回累计值。"""
    streak = min(read_boot_state()["streak"] + 1, 999)
    _write_boot_state(streak, True, time.time())
    return streak


def confirm_boot_ok():
    """启动完整落地(核心/窗口/托盘全就绪)后清零——本次启动已成功。"""
    _write_boot_state(0, True)


def mark_clean_quit():
    """正常退出时落一笔;崩溃退出到不了这里,这正是判据。"""
    _write_boot_state(0, False)


# ---- 阶段 8 拆分:存储服务层迁至 privacy_store / history_store(兼容重导出) ----
from privacy_store import (
    _DPAPI_ENTROPY, _dpapi_protect, _dpapi_unprotect, dpapi_available,
    _enc_field, _dec_field,
    PrivateLoadResult, PrivateSaveResult, PRIVATE_BACKUPS,
    _private_bak_paths, _private_parse_file, load_private_status,
    _private_quarantine_corrupt, load_private, _private_rotate_backups,
    _private_write_payload, _private_cleanup_tmp, save_private)
from history_store import (
    _history_from_container, load_history, save_history,
    _encrypt_history_residuals, _remove_history_database)



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
    """就地将 config.json 里的明文密钥封装为密文(不轮转备份)。

    启动迁移必须先跑这一步再 save_config:否则旧的明文文件会被轮转进
    config.json.bak1,密钥以明文形式残留在备份里,等于没加密。
    任何密钥加密失败(DPAPI 不可用)→ 整体放弃迁移:明文原件保持原样
    (可恢复来源),不产生半加密状态。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        changed = False
        for s in data.get("ai_services") or []:
            if isinstance(s, dict) and isinstance(s.get("api_key"), str) and s["api_key"]:
                enc = _enc_field(s["api_key"])
                if enc is None:
                    log("config.json 明文密钥迁移失败(DPAPI 不可用),明文原件保留待修复")
                    return
                s["api_key"] = enc
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
    """原子写配置:滚动备份 + 临时文件替换;密钥字段落盘前一律 DPAPI 加密。

    任何密钥加密失败(DPAPI 不可用)→ 整体放弃本次保存(在轮转备份与
    临时文件写入之前):原文件与备份保持不变(已有有效配置仍可恢复),
    返回 False 供调用方如实反馈"未保存"。"""
    out = dict(cfg)
    try:
        svcs = []
        for s in out.get("ai_services") or []:
            s = dict(s) if isinstance(s, dict) else s
            if isinstance(s, dict) and s.get("api_key") and \
                    not (isinstance(s["api_key"], dict) and "data" in s["api_key"]):
                enc = _enc_field(s["api_key"])
                if enc is None:
                    log("配置保存失败:AI 服务密钥加密不可用,原文件与备份保持不变")
                    return False
                s["api_key"] = enc
            svcs.append(s)
        out["ai_services"] = svcs
    except Exception:
        log("配置密钥加密失败:\n%s" % traceback.format_exc())
        return False
    tmp = CONFIG_FILE + ".tmp"
    try:
        _rotate_config_backup()
        Path(tmp).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        return True
    except Exception:
        log("配置写入失败:\n%s" % traceback.format_exc())
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False






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
        conn = http.client.HTTPSConnection(RELEASE_API_HOST, timeout=8)
        conn.request("GET", RELEASE_API_PATH,
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




# 日志 / 异常兜底(提升稳定性)
# ============================================================
_log_lock = threading.Lock()

# ---- 日志脱敏(SafeLogger 兜底层)----
# app.log 是明文落盘的诊断通道:任何剪贴板正文、密钥、令牌、URL 查询串
# 都不允许进入。调用方纪律仍是第一道防线(只记元数据 id/type/size/hash),
# 本层在 log() 出口做正则遮盖 + 长度截断,保证一条手滑不会泄漏 payload。
_LOG_MAX_CHARS = 2000          # 单条上限:容得下 traceback,挡得住正文倾倒

_RE_LOG_JWT = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_RE_LOG_KEY = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)")
_RE_LOG_KEYVAL = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key"
    r"|密码|口令|验证码|授权码)\b(\s*[:=]\s*)\S{3,}")
_RE_LOG_URL_QUERY = re.compile(r"(\bhttps?://[^\s?'\"#]+)\?[^\s'\"#]+")
# 64+ 连续 base64 形字符:几乎不可能是自然语言/常规日志,遮盖;
# 纯十六进制 40/64 位(sha1/sha256)是安全的元数据,放行。
_RE_LOG_OPAQUE = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{64,}={0,2}(?![A-Za-z0-9+/])")


def _mask_opaque(m):
    s = m.group(0)
    body = s.rstrip("=")
    if len(body) in (40, 64) and all(c in "0123456789abcdef" for c in body):
        return s                        # sha1/sha256 摘要:诊断友好的安全形式
    return "[长串%d字符已遮盖]" % len(s)


def sanitize_log(msg):
    """日志出必经此脱敏:JWT/密钥/键值对秘密/URL 查询串/超长不透明串遮盖,
    单条超长截断。脱敏自身异常时整条遮盖(宁废勿漏)。"""
    try:
        s = str(msg)
        s = _RE_LOG_JWT.sub("[JWT已遮盖]", s)
        s = _RE_LOG_KEY.sub("[密钥已遮盖]", s)
        s = _RE_LOG_KEYVAL.sub(
            lambda m: m.group(1) + m.group(2) + "[已遮盖]", s)
        s = _RE_LOG_URL_QUERY.sub(
            lambda m: m.group(1) + "?[查询串已遮盖]", s)
        s = _RE_LOG_OPAQUE.sub(_mask_opaque, s)
        if len(s) > _LOG_MAX_CHARS:
            s = s[:_LOG_MAX_CHARS] + "…[已截断,原%d字符]" % len(s)
        return s
    except Exception:
        return "[日志脱敏失败,整条遮盖]"


def log(msg):
    """追加运行/崩溃日志(出口统一过 sanitize_log 脱敏),超 512KB 自动轮转。"""
    try:
        ensure_dirs()
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                            sanitize_log(msg))
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
        self.core._hist_removed(gone)
        if self.core.store is not None:
            self.core.store.mark_deleted(
                [r.get("_db") for r in gone if r.get("_db") is not None])
        self.core._mark_dirty()
        return gone

    def rollback(self):
        back = super().rollback()
        self.core.history[0:0] = back
        for r in back:
            self.core._hist_added(r)     # 指纹/映射/计数恢复(幂等)
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

    def __init__(self, safe_mode=False):
        # 安全模式(Safe Mode):不启动托盘/热键/剪贴板监听线程——核心状态、
        # 历史浏览、导出与设置恢复仍完整可用,给"连续启动失败"留一条退路。
        self.safe_mode = bool(safe_mode)
        self.cfg, _cfg_stale = _load_config_raw()
        self._cfg_need_save = _cfg_stale
        self.max_items = int(self.cfg.get("max_items", 100))
        # 加密模式以 JSON（含空历史）为唯一事实源。仅在没有任何 JSON
        # 文件/备份时从旧库迁移；损坏/无法解密不能拿旧库覆盖当前数据。
        self.store = None
        _want_db = not self.cfg.get("encrypt_history")
        _preload = None
        if not _want_db:
            has_json = any(os.path.exists(HISTORY_FILE + suffix)
                           for suffix in ("", ".bak1", ".bak2", ".bak3"))
            if has_json:
                self.history = load_history(strict=True)
            elif os.path.exists(DB_FILE):
                _preload = ClipboardStore(DB_FILE)
                try:
                    self.history = _preload.load_all()
                finally:
                    _preload.close()
            else:
                self.history = []
            json_world = True
        else:
            try:
                _preload = ClipboardStore(DB_FILE)
            except Exception:
                log("SQLite 存储打开失败,本次会话走 JSON 持久化:\n%s"
                    % traceback.format_exc())
        if _want_db and _preload is not None and _preload.count() > 0:
            self.history = _preload.load_all()
            json_world = False
            self.store = _preload
        elif _want_db:
            if _preload is not None:
                self.store = _preload
            self.history = load_history()
            json_world = True
        self._private_load = load_private_status()   # 状态感知(不可空/非空判断)
        self.private = self._private_load.entries   # 敏感内容隔离区(不进普通面板)
        self._private_dirty = False                 # 有未成功落盘的私密区变更
        self.private_save_error = ""
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
        # 任何模式转换保存配置前先迁移明文密钥，防止被轮转进备份。
        self._migrate_config_secrets()
        if not _want_db:
            # 同时修复旧版本留下的明文副本；迁移失败停止启动，保留数据。
            self.set_history_encryption(True)
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
        # 后台线程(托盘 / 全局热键 / 剪贴板监听)。
        # 安全模式只建对象不启线程:cmd_q 等字段仍可用(粘贴命令静默排队,
        # 无消费者),窗口句柄为 None 的剪贴板写入是合法的(OpenClipboard(NULL))。
        self.worker = TrayWorker(self)
        if not self.safe_mode:
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
            self._reset_hist_caches()   # 哈希补齐+成员变化:全部缓存重建
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
                self._hist_removed(gone)
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
                self._hist_removed([h])
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
            return removed
        except Exception:
            return 0

    def _migrate_private(self):
        """旧明文私密区在启动时立即升级为 DPAPI 加密。

        主文件与备份链都不得留明文(明文不进备份链,成功后明文原件被
        加密副本替换);迁移失败保留明文原件(可恢复来源)并如实记录。
        __new__ 直造的核心(测试)缺加载状态时现查一次。"""
        st = getattr(self, "_private_load", None)
        if st is None:
            st = self._private_load = load_private_status()
            self.private = st.entries
        if st.status != "ok" or st.mode != "plaintext":
            return
        res = self._private_save()
        if res.ok:
            log("私密区已自动升级为 DPAPI 加密(明文不再保留)")
        else:
            log("私密区加密迁移失败,明文原件保留:%s" % res.error)

    # ---- 私密区状态与安全写 ----
    def private_state(self):
        """当前加载状态(阶段 4 的界面据此展示实际加密情况)。"""
        return getattr(self, "_private_load", None)

    def _private_can_save(self):
        """不可读状态下禁止自动覆写原文件(破坏性清空走 clear_private)。"""
        st = getattr(self, "_private_load", None)
        if st is not None and st.status == "unreadable":
            return False, ("原私密区数据无法读取,已禁止自动覆写;"
                           "如需放弃原数据,请在设置页执行「清空全部数据」"
                           "或私密区「清空」的破坏性流程")
        return True, ""

    def _private_save(self):
        """统一保存出口:成功才清 dirty;失败保持待重试并返回结果。
        读取失败(unreadable)时不会写文件,原密文/原件保持原样。"""
        ok, why = self._private_can_save()
        if not ok:
            self._private_dirty = True
            self.private_save_error = why
            return PrivateSaveResult(False, False, why)
        res = save_private(self.private)
        if res.ok:
            self._private_load = PrivateLoadResult(
                list(self.private), "ok", "dpapi", "", "")
            self._private_dirty = False
            self.private_save_error = ""
        else:
            self._private_dirty = True
            self.private_save_error = res.error
        return res

    def delete_private(self, i):
        """删除一条私密条目;失败恢复原列表(与磁盘一致)并返回错误。"""
        if not 0 <= i < len(self.private):
            return False, "索引无效"
        removed = self.private.pop(i)
        res = self._private_save()
        if not res.ok:
            self.private.insert(i, removed)
            return False, res.error
        return True, ""

    def clear_private(self):
        """清空私密区(用户显式确认后的破坏性流程)。

        允许覆写不可读原文件(这是唯一获准的路径);成功后连同备份链与
        损坏留档一并清除,否则"清空"会把秘密留在备份里。失败恢复原列表。"""
        backup = list(self.private)
        st = getattr(self, "_private_load", None)
        if st is not None and st.status == "unreadable":
            self._private_load = PrivateLoadResult([], "missing", "none", "", "")
        self.private.clear()
        res = self._private_save()
        if not res.ok:
            self.private[:] = backup
            return False, res.error
        for p in _private_bak_paths() + (PRIVATE_FILE + ".corrupt",):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return True, ""

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
            if not save_config(self.cfg):
                log("config.json 写入失败(密钥加密或磁盘问题),原文件保持不变;"
                    "配置变更保留在内存,下次保存重试")
                return
            if had_key:
                log("config.json 已写入,AI 服务密钥已封装为 DPAPI 密文")
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
            saved = save_history(self.history,
                                 encrypt=bool(self.cfg.get("encrypt_history")))
        if not saved:
            return  # 失败仍保持脏状态，后续刷新/退出可重试。
        if gen == getattr(self, "_gen", 0):
            self._dirty = False

    def add_item(self, item):
        """捕获入口 = 管线编排器:纯决策都在 pipeline.run_capture,
        本方法只执行副作用(写私密区 / 提升置顶 / 入库 + 落盘 + 通知)。
        每次捕获的阶段轨迹挂 self.last_capture_trace(排障用,见 pipeline.py)。
        去重走指纹索引(O(1) 查键,不再逐条扫全历史;缺 hash 的旧图片
        条目自动回退线性扫描,语义与旧实现一致)。"""
        snap = pipeline.Snapshot.from_item(item)
        # 惰性建索引(首捕一次 O(N);此后增量维护,见 _hist_added/_hist_removed)
        if getattr(self, "_fp_index", None) is None:
            self._fp_lookup(None)
        outcome, record, trace = pipeline.run_capture(
            snap, self.cfg, self.history,
            sensitive_probe=_sensitive_reason,
            badge_probe=content_badge,
            sha1_fallback=file_sha1,
            dedup_lookup=self._fp_lookup,
            dedup_kinds=self._dedup_kinds())
        self.last_capture_trace = trace
        if outcome.kind == pipeline.SENSITIVE:
            self._add_private(snap.content)
            return
        if outcome.kind == pipeline.PINNED_SKIP:
            return
        if outcome.kind == pipeline.DUPLICATE and outcome.rec is not None:
            old = outcome.rec
            for i, h in enumerate(self.history):   # 按身份出列(勿用值相等 remove)
                if h is old:
                    self.history.pop(i)
                    break
            self._hist_removed([old])
            self._store_del([old])
        if self.store is not None:
            if record["type"] == "text":
                badge = outcome.badge
                if badge:
                    record["_subtype"] = badge      # 子类型缓存(仅库模式写入;
                    # 复用管线结果,不再二次分类)
            self.store.insert(record)
        self.history.insert(0, record)
        self._hist_added(record)
        self._evict()
        self._mark_dirty()
        self._notify_change()

    # ---- 历史缓存集中维护(指纹索引 / dbid 映射 / 来源集 / 未置顶计数) ----
    def _fp_lookup(self, key):
        """指纹键 → 最近一条同指纹活跃记录(无则 None)。首次调用惰性建索引:
        文本按内容、文件按路径元组、图片按像素 hash;缺 hash 的旧图片条目
        不进索引(这类捕获自动回退线性扫描,见 _dedup_kinds)。"""
        idx = getattr(self, "_fp_index", None)
        if idx is None:
            idx = self._fp_index = {}
            nohash = False
            for h in self.history:
                k = pipeline.fingerprint_of_rec(h)
                if k == ("img", ""):
                    nohash = True
                    continue
                idx.setdefault(k, h)      # 历史序=最近在前,首见即最近
            self._fp_has_nohash_img = nohash
        return idx.get(key)

    def _dedup_kinds(self):
        """索引完备的内容类型:图片存在缺 hash 旧条目时不含 image。"""
        if getattr(self, "_fp_index", None) is None:
            self._fp_lookup(None)
        return (("text", "files", "image")
                if not self._fp_has_nohash_img else ("text", "files"))

    def _hist_added(self, rec):
        """新条目入史后的增量维护(热路径:捕获)。"""
        idx = getattr(self, "_fp_index", None)
        if idx is not None:
            k = pipeline.fingerprint_of_rec(rec)
            if k != ("img", ""):
                idx[k] = rec
        if getattr(self, "_unpinned_count", None) is not None \
                and not rec.get("pinned"):
            self._unpinned_count += 1
        self._dbid_pos_cache = None
        self._src_apps_cache = None

    def _hist_removed(self, recs):
        """条目离开历史后的集中维护(删除/淘汰/清理/撤销入栈等冷路径)。"""
        if not recs:
            return
        idx = getattr(self, "_fp_index", None)
        sc = getattr(self, "_search_cache", None)
        for r in recs:
            if idx is not None:
                k = pipeline.fingerprint_of_rec(r)
                if idx.get(k) is r:
                    del idx[k]
            if sc:
                sc.pop(id(r), None)
        self._dbid_pos_cache = None
        self._src_apps_cache = None
        self._unpinned_count = None

    def _hist_reordered(self):
        """成员不变仅顺序变化(粘贴提升):只需失效位置映射。"""
        self._dbid_pos_cache = None

    def _reset_hist_caches(self):
        """整表级变化(清空/模式切换/迁移合并):全部缓存作废重建。"""
        self._fp_index = None
        self._fp_has_nohash_img = False
        self._dbid_pos_cache = None
        self._src_apps_cache = None
        self._unpinned_count = None
        if getattr(self, "_search_cache", None):
            self._search_cache.clear()

    def _dbid_pos(self):
        """库行 id → history 下标 映射(query_view 用;集中失效,变更后重建)。
        2 万条约 3ms,避免每次查询全量重建。"""
        m = getattr(self, "_dbid_pos_cache", None)
        if m is None:
            m = self._dbid_pos_cache = {
                h["_db"]: i for i, h in enumerate(self.history)
                if h.get("_db") is not None}
        return m

    def _unpinned_total(self):
        """未置顶条数(_evict 容量判定用;缓存 + 变更失效,免每次全量计数)。"""
        c = getattr(self, "_unpinned_count", None)
        if c is None:
            c = self._unpinned_count = \
                sum(1 for h in self.history if not h.get("pinned"))
        return c

    def _notify_change(self):
        """通知前端历史已变。

        只走事件投递(_post),不在本方法内直接碰 UI:add_item 由前端的
        排空回调触发,而 TrayWorker 也会经同一队列投递,统一走队列可避免
        跨线程访问部件。
        """
        self._post(("change",))

    def _evict(self):
        """容量淘汰(README 语义):max_items 限制**未置顶**条数,置顶不占预算;
        从最旧的普通记录淘汰(置顶永不淘汰),库删除与 blob 引用保护照走。"""
        excess = self._unpinned_total() - self.max_items
        if excess <= 0:
            return
        removed = 0
        to_del = []
        evicted = []
        for i in range(len(self.history) - 1, -1, -1):
            if removed >= excess:
                break
            if not self.history[i].get("pinned"):
                to_del.append(i)
                evicted.append(self.history[i])
                removed += 1
        for i in sorted(to_del, reverse=True):
            self.history.pop(i)
        self._hist_removed(evicted)       # 索引/映射/计数集中维护
        self._store_del(evicted)
        for rec in evicted:
            self._blob_unref(rec)

    def _add_private(self, content):
        """敏感内容写入私密区(去重、上限 100 条);投递 private 信号供前端
        提示(只带 is_jwt 布尔,不把原文再送一遍队列)。保存失败时条目保留
        在内存并标记待重试,不覆盖原文件。"""
        try:
            for i, s in enumerate(self.private):
                if s == content:
                    self.private.pop(i)
                    break
            self.private.insert(0, content)
            del self.private[100:]
            res = self._private_save()
            if res.ok:
                log("敏感内容已隔离(仅入私密区,不写入普通历史)")
            else:
                log("敏感内容已隔离在内存,但尚未保存(%s)" % res.error)
            self._post(("private", _sniff_jwt(content) is not None))
        except Exception:
            log("私密区写入失败:\n%s" % traceback.format_exc())

    # ---- 列表过滤 ----
    def _search_text(self, h):
        """条目的检索串:casefold 一次后缓存,搜索每敲一个字符不再对全部
        条目重复 str.lower/join(大文本条目尤甚;条目内容入史后不可变)。
        缓存有界:超 512 项时淘汰最旧的一半(插入序 FIFO),不再整表清空
        引发批量重算;条目离开历史时按身份定点清除(见 _hist_removed)。"""
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
            if len(cache) >= 512:
                for stale in list(cache)[:256]:
                    del cache[stale]
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
        """历史中出现过的来源应用 exe 名(最近在前,去重);供筛选下拉。
        结果缓存(成员变更时失效,见 _hist_added/_hist_removed),免得每次
        刷新都全量扫描大历史。"""
        out = getattr(self, "_src_apps_cache", None)
        if out is None:
            seen = set()
            out = []
            for h in self.history:
                s = h.get("src")
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
            self._src_apps_cache = out
        return out

    def filter_view(self, category, query="", src="", since=0.0, limit=None):
        """按分类 + 关键词 + 来源应用 + 时间范围过滤,返回命中条目在
        history 中的索引列表(内存路径;SQL 路径见 query_view)。
        查询串支持 Search DSL(source:/type:/after:/before:/is:pinned),
        与 query_view 语义逐条一致(parse_search_query 共用)。
        limit:凑够即停(限量调用方如快捷浮层不再承担全量扫描成本;
        返回值与旧实现 out[:limit] 逐项一致——迭代序即历史序)。"""
        dsl = parse_search_query(query)
        q = dsl["text"].strip().casefold()
        src_q = (src or "").strip().lower() or dsl["src"] or ""
        since_eff = max(float(since or 0.0), dsl["after"] or 0.0)
        before_eff = dsl["before"]
        want = None if limit is None else max(int(limit), 1)
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
            if want is not None and len(view) >= want:
                break
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
            else:
                subtype = effective_subtype(rec)
                if subtype is None and rec.get("type") == "text":
                    # 卡片徽章只识别 JWT/Base64，完整 DSL 子类型还需要
                    # JSON/URL/CSV 等内容分类；仅子类型过滤时执行。
                    info = detect_content(rec.get("content") or "")
                    subtype = "code" if info.category == "code" else info.subtype
                subtype = str(subtype or "").lower()
                if subtype == "web_url":
                    subtype = "url"
                if subtype != t:
                    return False
        return True

    def query_view(self, category, query="", src="", since=0.0, limit=None):
        """视图过滤(SQL 优先):组合过滤在库上执行,返回命中条目在
        self.history 中的下标列表——与 filter_view 同序同义(UI 索引语义
        不变)。查询串支持 Search DSL(source:/type:/after:/before:
        /is:pinned,见 parse_search_query);无库或存在未入库条目时整体
        回退内存路径。

        limit 语义与旧实现一致(返回前 limit 条):
        - 纯 SQL 可表达的过滤:limit 直接下推到查询(LIMIT 提前截断,
          不再读取全部匹配 ID);
        - 需要内存补充判断的分类/子类型(链接、type: 子类型):分批读取
          候选持续过滤,凑够 limit 或遍历结束才停——不会先 LIMIT 后过滤
          而漏掉后面的正确结果;
        - dbid→下标 映射集中缓存(变更失效),不再每次查询全量重建。"""
        lim = None if limit is None else max(int(limit), 1)
        if getattr(self, "store", None) is None:
            return self.filter_view(category, query, src=src, since=since,
                                    limit=lim)
        if any(h.get("_db") is None for h in self.history):
            return self.filter_view(category, query, src=src, since=since,
                                    limit=lim)
        dsl = parse_search_query(query)
        src_q = (src or "").strip().lower() or dsl["src"] or ""
        since_eff = max(float(since or 0.0), dsl["after"] or 0.0)
        ctype = {"text": "text", "image": "image",
                 "files": "files"}.get(category)
        needs_mem = category == "link" or bool(dsl["types"])
        pos = self._dbid_pos()

        def _sql_ids(sql_limit, offset):
            return self.store.query(
                content_type=ctype, source_app=src_q or None,
                since=since_eff, until=dsl["before"] or None,
                q=dsl["text"] or None,
                pinned=dsl["pinned"],
                limit=sql_limit, offset=offset)

        def _keep(r):
            if category == "link" and not (r.get("type") == "text"
                                           and url_info(r["content"])):
                return False
            if dsl["types"] and not self._match_dsl_types(r, dsl["types"]):
                return False
            return True

        if not needs_mem:
            # 完整过滤交给 SQL:limit 下推(无缝隙,直接截断)
            dbids = _sql_ids(lim, 0)
            out, seen = [], set()
            for dbid in dbids:
                i = pos.get(dbid)
                if i is not None and i not in seen:
                    seen.add(i)
                    out.append(i)
            return out

        # 需要内存补充判断:分批读取候选,持续过滤直到凑够 limit 或读完
        batch = 400 if lim is not None else 10 ** 9
        out, seen, offset = [], set(), 0
        while True:
            dbids = _sql_ids(batch, offset)
            if not dbids:
                break
            for dbid in dbids:
                i = pos.get(dbid)
                if i is None or i in seen:
                    continue
                r = self.history[i]
                if _keep(r):
                    seen.add(i)
                    out.append(i)
                    if lim is not None and len(out) >= lim:
                        return out
            if len(dbids) < batch:
                break               # 库已读完
            offset += len(dbids)
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
    def _write_clip_guarded(self, rec):
        """写剪贴板;成功才设自捕获抑制窗,失败还原(不留多余抑制窗)。
        _set_clipboard 自身抛异常也按写入失败处理(不向上扩散)。"""
        prev = getattr(self, "_ignore_clip_until", 0.0)
        try:
            ok = self._set_clipboard(rec)
        except Exception:
            ok = False
        if ok:
            self._ignore_clip_until = time.monotonic() + 0.5
        else:
            self._ignore_clip_until = prev
        return ok

    def copy_record(self, rec):
        """仅复制原始内容,不模拟粘贴、不移动历史条目;写失败返回 False。"""
        if rec["type"] == "files":
            existing = [p for p in rec["content"] if os.path.exists(p)]
            if not existing:
                return False
            rec = dict(rec, content=existing)
        return self._write_clip_guarded(rec)

    def paste_record(self, rec, force=False, target_hwnd=None):
        """写回剪贴板 + 自动粘贴排队 + 置顶到最近。

        写入失败即返回 False:不派发 Ctrl+V、不提升历史、不更新库 seq,
        也不把操作报告为成功。文件过滤用临时副本(失败不修改原记录)。
        force=True 仅在写入成功后越过 auto_paste 开关(用户按 Enter 的
        意图就是粘贴)。target_hwnd = 意图目标窗口(粘贴事务校验用)。"""
        if rec["type"] == "files":
            existing = [p for p in rec["content"] if os.path.exists(p)]
            if not existing:
                return False
            write_rec = dict(rec, content=existing)
        else:
            existing = None
            write_rec = rec
        if not self._write_clip_guarded(write_rec):
            return False
        if existing is not None:
            rec["content"] = existing        # 成功后才把过滤结果落到原记录
            sc = getattr(self, "_search_cache", None)   # 文件列表变了,检索串失效
            if sc:
                sc.pop(id(rec), None)
        if force or self.cfg.get("auto_paste", False):
            self.worker.cmd_q.put(
                ("paste", int(target_hwnd or 0), _get_clip_seq()))
        try:
            self.history.remove(rec)
            self.history.insert(0, rec)
            self._hist_reordered()           # 成员未变,仅顺序:失效位置映射
            if self.store is not None:
                self.store.upsert_promote(rec)    # 最近使用 → seq 提升到最前
            self._mark_dirty()
        except Exception:
            pass
        return True

    def paste_text(self, content, force=False, target_hwnd=None):
        """文本写回剪贴板;写入失败返回 False 且不派发回贴。

        force=True 无条件派发回贴(用户点了明确的「复制并粘贴」类按钮),
        否则跟随 auto_paste 开关(默认关:模拟 Ctrl+V 会把内容打进当前
        聚焦窗口,不应是复制动作的默认副作用)。"""
        if not self._write_clip_guarded({"type": "text", "content": content}):
            return False
        if force or self.cfg.get("auto_paste", False):
            self.worker.cmd_q.put(
                ("paste", int(target_hwnd or 0), _get_clip_seq()))
        return True

    def copy_text(self, content):
        """仅写剪贴板(不排回贴队列):查看器分段复制等小动作用,
        不往用户此刻聚焦的窗口里塞 Ctrl+V。返回是否写入成功。"""
        return self._write_clip_guarded({"type": "text", "content": content})

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
        if getattr(self, "_unpinned_count", None) is not None:
            self._unpinned_count += -1 if rec["pinned"] else 1
        if not rec["pinned"]:
            # 取消置顶即回到容量预算:超限立即淘汰最旧普通记录
            self._evict()

    def delete_at(self, hi):
        """硬删除单条:行立即物理删;图片文件经引用计数复检(refcount==0 才删)。"""
        rec = self.history.pop(hi)
        self._hist_removed([rec])
        self._store_del([rec])
        self._blob_unref(rec)
        self._mark_dirty()
        return rec

    def clear_unpinned(self):
        """清空未置顶条目(置顶保留);文件删除走引用计数。"""
        removed = [h for h in self.history if not h.get("pinned")]
        self.history[:] = [h for h in self.history if h.get("pinned")]
        self._hist_removed(removed)
        self._store_del(removed)
        for h in removed:
            self._blob_unref(h)
        self._mark_dirty()

    def wipe_all_data(self, wipe_private=True, reset_config=False):
        """一键清空全部数据(设置页「清空全部数据」;不可撤销)。

        逐步执行、任一步失败即中止(已完成步骤照实返回,便于报告与复跑):
          1. 撤销栈清空(条目即将全灭,撤销已无意义)
          2. 历史全清(含置顶):内存出列 + 库整表物理删除;JSON 镜像
             (加密模式导出的 history.json)写空;历史备份链 .bak1~3
             一并删除(备份含旧数据,不清等于没清)
          3. 图片资产全清:blobs/ 分片与旧 images/ 目录全部文件——引用已
             全灭,无需引用计数与时间窗
          4. 私密区清空(wipe_private=True):原地清空 + 落盘空加密容器
          5. 上下文包清空:context_packs.json 及备份链/留档一并删除
             (独立资产,设置页确认文案已列明)
          6. 配置重置(reset_config=True):配置与备份删除,内存 cfg 原地
             换回默认值并立即落盘(容器身份不变,前端别名仍有效)
        不动:app.log、boot_state、workflow_runs.json(诊断与运行记录)、
        图钉画布资产(pins/ 由画布自行管理)。
        返回 (已完成步骤说明列表, 是否全部成功)。
        """
        done = []
        try:
            self._txn_list().clear()
            done.append("撤销栈清空")

            self.history.clear()
            if self.store is not None:
                self.store.reset()          # 全行物理删除(FTS 由触发器同步)
            save_history([], encrypt=bool(self.cfg.get("encrypt_history")))
            for i in (1, 2, 3):
                fp = "%s.bak%d" % (HISTORY_FILE, i)
                if os.path.exists(fp):
                    os.remove(fp)
            self._reset_hist_caches()       # 指纹/映射/来源/计数/检索串全部重建
            self._dirty = False
            done.append("历史全清(含置顶)")

            removed_files = 0
            for base in (BLOB_DIR, IMAGE_DIR):
                if not os.path.isdir(base):
                    continue
                for root, _dirs, files in os.walk(base):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                            removed_files += 1
                        except Exception:
                            pass
            done.append("图片文件全清(%d 个)" % removed_files)

            if wipe_private:
                # 用户显式的破坏性清空:唯一允许覆写不可读原文件的路径;
                # 备份链与损坏留档一并清除(否则清空会把秘密留在备份里)
                st = getattr(self, "_private_load", None)
                if st is not None and st.status == "unreadable":
                    self._private_load = PrivateLoadResult(
                        [], "missing", "none", "", "")
                self.private.clear()
                res = self._private_save()
                if not res.ok:
                    raise RuntimeError("私密区清空失败:%s" % res.error)
                for p in _private_bak_paths() + (PRIVATE_FILE + ".corrupt",
                                                 PRIVATE_FILE + ".tmp"):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
                self._private_dirty = False
                done.append("私密区清空(含备份)")

            # 上下文包一并清空(独立资产,备份链与留档同删;确认文案已列明)
            from ctxpack.store import wipe_packs
            wipe_packs(CTXPACK_FILE)
            done.append("上下文包清空(含备份)")

            if reset_config:
                for suffix in ("", ".bak1", ".bak2", ".bak3"):
                    fp = CONFIG_FILE + suffix
                    if os.path.exists(fp):
                        os.remove(fp)
                self.cfg.clear()
                self.cfg.update(DEFAULT_CFG)
                save_config(self.cfg)
                done.append("配置重置")
            log("清空全部数据:完成(%s)" % "; ".join(done))
            return done, True
        except Exception:
            log("清空全部数据中途失败(已完成: %s):\n%s"
                % ("; ".join(done), traceback.format_exc()))
            return done, False

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
        if self._txn_stack_refs(path, blob):
            return 0
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
        payload["ctxpacks"] = self._ctxpack_portable_snapshot()
        return payload

    def _ctxpack_portable_snapshot(self):
        """上下文包快照进入可迁移备份(容器整体 AES-GCM 加密;包随备份走)。"""
        try:
            from ctxpack.store import load_packs_status
            r = load_packs_status(CTXPACK_FILE)
            if r.status in ("ok", "recovered"):
                return {"version": 1, "active": r.active_pid,
                        "packs": [p.to_dict() for p in r.packs]}
        except Exception:
            log("上下文包导出快照失败(本次备份不含包):\n%s"
                % traceback.format_exc())
        return None

    def import_portable_payload(self, payload):
        """导入加密备份载荷:历史走 import_payload(自动去重),
        私密条目合并入私密区(去重),上下文包按 pid 合并(本地优先)。
        返回 (新增, 跳过, 私密新增)。"""
        priv = payload.get("private")
        packs_data = payload.get("ctxpacks")
        payload = dict(payload)
        payload.pop("private", None)
        payload.pop("ctxpacks", None)
        added, skipped = self.import_payload(payload)
        n_priv = 0
        if isinstance(priv, list):
            for s in priv:
                if isinstance(s, str) and s and s not in self.private:
                    self.private.append(s)
                    n_priv += 1
            if n_priv:
                res = self._private_save()
                if not res.ok:
                    log("导入的私密条目已并入内存,但尚未保存(%s)" % res.error)
        if isinstance(packs_data, dict) and packs_data.get("packs"):
            try:
                from ctxpack.store import merge_packs_payload
                n_packs, res = merge_packs_payload(
                    CTXPACK_FILE, packs_data,
                    encrypt=bool(self.cfg.get("encrypt_history")))
                if res is not None and not res.ok:
                    log("导入的上下文包已读取,但尚未落盘(%s)" % res.error)
                elif n_packs:
                    log("上下文包导入:新增 %d 个包" % n_packs)
            except Exception:
                log("上下文包导入失败(其余导入不受影响):\n%s"
                    % traceback.format_exc())
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
        """退出/手动全量保存。

        私密区只在「成功加载过 且 发生过变更」时才写:读取失败(unreadable)
        绝不覆写原文件;保存失败保持脏标记并把错误留在
        private_save_error 供退出反馈(调用方不能当作已成功保存)。"""
        self.flush_history()
        st = getattr(self, "_private_load", None)
        if st is not None and st.status == "unreadable":
            log("私密区原文件不可读,退出时不覆写(数据仍留在原文件中)")
            return
        if getattr(self, "_private_dirty", False):
            self._private_save()

    def set_history_encryption(self, on):
        """先持久化新事实源与模式配置，再清理旧存储；失败抛错供 UI 呈现。

        开启时仅加密历史正文/元数据；独立图片资产不在此开关的保护范围。
        关闭时用当前历史重建库，保留加密备份供恢复。
        """
        on = bool(on)
        # 上下文包与历史同步迁移(独立文件;开启失败=隐私倒退→中止切换,
        # 关闭失败保留密封原文件=安全方向,不阻塞历史切换)
        from ctxpack.store import migrate_packs_file
        _ok, _err = migrate_packs_file(CTXPACK_FILE, encrypt=on)
        if on and not _ok:
            raise OSError("上下文包加密迁移失败:%s" % _err)
        if not _ok:
            log("上下文包解密迁移失败(密封原文件保留):%s" % _err)
        if on:
            if not save_history(self.history, encrypt=True):
                raise OSError("无法保存加密历史，原存储模式保持不变")
            _encrypt_history_residuals()
            old_mode = self.cfg.get("encrypt_history", False)
            self.cfg["encrypt_history"] = True
            if not old_mode and not save_config(self.cfg):
                self.cfg["encrypt_history"] = old_mode
                raise OSError("无法保存加密模式配置，原数据库保留")
            if self.store is not None:
                self.store.close()
                self.store = None
            self._txn_stack = []
            self._dirty = False
            # 配置已指向 JSON：即使清理被文件占用打断，重启也不会加载旧库。
            try:
                _remove_history_database()
            except OSError as exc:
                raise OSError("历史已加密，但旧明文数据库清理失败，请关闭占用程序后重试") from exc
            return
        if self.store is None:
            st = None
            old_mode = self.cfg.get("encrypt_history", False)
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
                self._hist_reordered()      # 行 id 全变:位置映射重建
                self.cfg["encrypt_history"] = False
                if not save_config(self.cfg):
                    self.cfg["encrypt_history"] = old_mode
                    raise OSError("无法保存 SQLite 模式配置，加密历史仍保留")
                self.store = st
                self._dirty = False
                # 重建后行 id 全变,指向旧行的撤销事务一并作废
                self._txn_stack = []
                try:
                    os.remove(HISTORY_FILE)
                except Exception:
                    pass
            except Exception:
                if st is not None and self.store is not st:
                    st.close()
                    if old_mode:
                        # 转换未完成时仍处于加密模式，不能遗留新建的明文库。
                        try:
                            _remove_history_database()
                        except OSError:
                            log("转换失败后的明文数据库清理失败:\n%s"
                                % traceback.format_exc())
                raise

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
# Search DSL:source:vscode type:json after:2026-09-01
# before:2026-09-10 is:pinned 关键词
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
        m = re.fullmatch(r"(source|src|type|kind|after|since|before|until)"
                         r":(.+)", low)
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
    # 空名 = 默认服务(第一个已配置):与 AI 动作栏「空服务名」约定一致,
    # 工作流步骤/模板不写死服务名即可用(显式名称仍按精确匹配)
    if not name:
        services = cfg.get("ai_services") or []
        return services[0] if services else None
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
    # {context} 联动示例:绑定当前活动上下文包(收进「上下文篮」的代码/资料
    # 自动注入);service 留空 = 第一个已配置的服务,无需写死名称
    {"name": "结合上下文包:审阅代码 → 生成单元测试", "context_pack": "@active",
     "steps": [
        {"kind": "ai", "service": "",
         "prompt": "你是一名严格的代码审阅者。请结合下面的相关代码上下文:\n{context}\n\n审阅这段代码,指出正确性、边界条件与可读性问题,逐条给出具体修改建议:\n{input}"},
        {"kind": "human", "prompt": "可先修改审阅意见(或原样确认),继续生成单元测试"},
        {"kind": "ai", "service": "",
         "prompt": "你是测试工程师。请结合下面的相关代码上下文:\n{context}\n\n依据上面的审阅意见,为这段代码编写可直接运行的单元测试(覆盖正常路径与边界用例),只输出测试代码:\n{input}"},
    ]},
]


# ============================================================
# 入口(tkinter 前端已移除:本项目只保留 Qt + Fluent 版界面)
# ============================================================
if __name__ == "__main__":
    print("WinClipboard 后端模块:界面请运行 winclipboard_qt.py(需 PySide6 + PySide6-Fluent-Widgets)。")
