# -*- coding: utf-8 -*-
"""Windows 适配层(阶段 8 拆分,自 clipboard_manager.py 迁出,行为不变):
Win32 消息/热键/托盘常量与原型、单实例互斥、前台进程识别、剪贴板格式
转换(CF_DIB/CF_HDROP)、SendInput 回贴、剪贴板序列号、TrayWorker
(托盘 + 全局热键 + 剪贴板监听的后台消息循环)。

依赖方向:本模块不 import cm(导入期);TrayWorker 持有 core 引用,
cm 属性(log/ICON_FILE/foreground_app_exe)经 _cm() 运行期回查 ——
测试改写 cm.* 即生效的既有打桩语义保持不变。
"""
import os
import io
import sys
import time
import queue
import ctypes
import hashlib
import threading
import traceback
from ctypes import wintypes

import win32api

import win32gui
import win32con
from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _cm():
    """运行期回查 cm 模块(避开导入期循环;改写 cm 属性立即生效)。"""
    import clipboard_manager as cm
    return cm


def log(msg):
    """日志出口经 cm.log(测试改写 LOG_FILE 后仍同一路径)。"""
    _cm().log(msg)

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
ID_HOTKEY_AI = 105           # 快速 AI(默认 Ctrl+Alt+Space)

# 部分 win32con 构建缺失的托盘常量,这里显式定义(避免 AttributeError)
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

# Win32

_VK_OEM_NAMES = {
    0xC0: "`", 0xBD: "-", 0xBB: "=", 0xBC: ",", 0xBE: ".",
    0xBF: "/", 0xBA: ";", 0xDE: "'", 0xDB: "[", 0xDD: "]",
    0xDC: "\\",
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

def single_instance():
    """单实例互斥:名字由数据目录派生——同一数据目录(APPDATA)只允许一个
    实例(两实例写同一份历史/配置会互相踩);不同数据目录的实例共存
    (打包冒烟的隔离 APPDATA 会话、开发 worktree 与正式安装互不干扰)。"""
    suffix = hashlib.sha256(str(_cm().APP_DIR).encode("utf-8")).hexdigest()[:8]
    mutex = kernel32.CreateMutexW(
        None, False, "Global\\WinClipboard_Mutex_7hasj2k_" + suffix)
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return (not already), mutex

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

def png_to_dib(path):
    """把 PNG 文件转回 CF_DIB 原始字节(BITMAPINFOHEADER + 像素,24bit)。"""
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    return buf.getvalue()[14:]  # 去掉 14 字节 BITMAPFILEHEADER 即为 DIB

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
        _cm().ensure_dirs()
        if not os.path.exists(_cm().ICON_FILE):
            try:
                _cm().make_icon(_cm().ICON_FILE)
            except Exception:
                pass
        self.hinst = win32api.GetModuleHandle(None)

        # 图标
        try:
            self.hicon = win32gui.LoadImage(
                self.hinst, _cm().ICON_FILE, win32con.IMAGE_ICON, 0, 0,
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
        try:
            win32gui.Shell_NotifyIcon(NIM_ADD, self._nid)
        except Exception as e:
            # 图标注册失败(受限会话 error 5 / Explorer 未就绪):托盘图标
            # 缺失,但热键/剪贴板监听/消息循环必须继续——图标是入口便利,
            # 不是核心功能。实测裸抛会拖死整条工作线程:宿主窗口成为死
            # 窗口,热键与捕获全部失联(2026-10-13 受监督会话实捕)。
            log("托盘图标注册失败(%r),以无图标模式继续(热键可用)" % e)
            self._nid = None

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
        ai_ok = user32.RegisterHotKey(
            self.hwnd, ID_HOTKEY_AI,
            int(cfg.get("ai_hotkey_mod", MOD_CONTROL | MOD_ALT)) | 0x4000,  # MOD_NOREPEAT
            int(cfg.get("ai_hotkey_key", 0x20)))
        self.app._post(("ai_hotkey_state", bool(ai_ok)))
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
            lambda: user32.UnregisterHotKey(self.hwnd, ID_HOTKEY_AI),
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
        src = _cm().foreground_app_exe()
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
        elif wp == ID_HOTKEY_AI:
            self.app._post(("ai_hotkey",))
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
            # 事务原语(send_ctrl_v / 序列号 / 前台 / time)经 _cm() 回查:
            # 测试对 cm.* 的打桩(免睡眠/假前台)保持拆分前同样生效。
            _cmm = _cm()
            expected = int(cmd[1]) if len(cmd) > 1 and cmd[1] else 0
            want_seq = int(cmd[2]) if len(cmd) > 2 and cmd[2] else 0
            if not expected:
                _cmm.time.sleep(0.07)      # 旧调用/测试:无目标校验
                try:
                    _cmm.send_ctrl_v()
                except Exception:
                    pass
                return
            deadline = _cmm.time.monotonic() + 0.45
            while _cmm.time.monotonic() < deadline:
                _cmm.time.sleep(0.04)
                if want_seq and _cmm._get_clip_seq() != want_seq:
                    self.app._post(("paste_abort",
                                    "剪贴板内容在粘贴前被其他程序修改"))
                    return
                if _cmm._get_foreground() == expected:
                    try:
                        _cmm.send_ctrl_v()
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
        elif cmd[0] == "hotkey_ai":
            mod, key = int(cmd[1]), int(cmd[2])
            cfg = self.app.cfg
            user32.UnregisterHotKey(self.hwnd, ID_HOTKEY_AI)
            ok = bool(user32.RegisterHotKey(self.hwnd, ID_HOTKEY_AI, mod | 0x4000, key))
            if not ok:
                user32.RegisterHotKey(self.hwnd, ID_HOTKEY_AI,
                                      int(cfg.get("ai_hotkey_mod", MOD_CONTROL | MOD_ALT)) | 0x4000,
                                      int(cfg.get("ai_hotkey_key", 0x20)))
            self.app._post(("ai_hotkey_change", ok, mod, key))
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

