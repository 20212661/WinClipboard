# -*- coding: utf-8 -*-
"""自动化 GUI 测试:验证全局热键唤出/收起面板(需 WinClipboard 已在运行)。

运行: python _gui_test.py [PID]     退出码 0 = 通过
PID 可选且仅用于提示(启动器链路会多一层进程,传入的 PID 未必是拥有窗口的那个);
测试按窗口标题定位,不依赖 PID。

注意:面板以 Qt.Tool 创建(WS_EX_TOOLWINDOW),而 EnumWindows **不枚举 Tool 窗口**,
因此不能用「统计可见顶层窗口数」来判断面板是否弹出 —— 必须用 FindWindow 定位。
"""
import ctypes, time, sys
from ctypes import wintypes
import win32gui

user32 = ctypes.WinDLL("user32")
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

def _pid_of(hwnd):
    p = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return p.value

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]
class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]
class INPUT_UNION(ctypes.Union):
    # 必须包含最大的 MOUSEINPUT,否则 x64 下 sizeof(INPUT)=32 而非 40,SendInput 报错 87
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]
class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]
INPUT_KEYBOARD = 1
KEYUP = 0x0002

def send_combo(mods, main):
    seq = [(m, 0) for m in mods] + [(main, 0), (main, KEYUP)] + [(m, KEYUP) for m in mods]
    arr = (INPUT * len(seq))()
    for i, (vk, fl) in enumerate(seq):
        arr[i] = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, fl, 0, None))
    user32.SendInput(len(seq), ctypes.byref(arr), ctypes.sizeof(INPUT))

def find_panel():
    """按标题定位面板窗口。

    注意:面板以 Qt.Tool 创建(带 WS_EX_TOOLWINDOW),EnumWindows **不会**枚举
    Tool 窗口,所以早期用 EnumWindows 统计可见窗口的写法永远测得 0 —— 必须
    直接用 FindWindow 按标题/类名定位。
    """
    hwnd = win32gui.FindWindow(None, "WinClipboard")
    if not hwnd:
        return None, None
    return hwnd, _pid_of(hwnd)


def panel_visible():
    hwnd, _ = find_panel()
    return bool(hwnd) and bool(win32gui.IsWindowVisible(hwnd))


def wait_for(target, timeout=5.0):
    """轮询等待可见性变为 target(注入热键的分发有延迟)。"""
    t0 = time.time()
    last = panel_visible()
    while time.time() - t0 < timeout:
        last = panel_visible()
        if last == target:
            return last, time.time() - t0
        time.sleep(0.2)
    return last, time.time() - t0


pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
hwnd, real_pid = find_panel()
print("面板窗口: hwnd=%s 真实PID=%s (传入PID=%s)" % (hwnd, real_pid, pid))
if not hwnd:
    print("RESULT: FAIL_NO_WINDOW")
    sys.exit(1)
if pid is not None and real_pid != pid:
    print("提示: 传入的 PID 与实际拥有面板窗口的进程不一致(启动器链路会多一层进程);")
    print("      本测试按窗口定位,不依赖 PID。")

# 面板初态未知(可能已开着),因此用「切换」语义判定,而不是假设初态
start = panel_visible()
print("初始 visible:", start)
send_combo([0x11, 0x10], 0x56)          # Ctrl+Shift+V
after, dt = wait_for(not start)
print("after Ctrl+Shift+V (应变为 %s): %s  (%.1fs)" % (not start, after, dt))

send_combo([], 0x1B)                     # Esc
final, dt2 = wait_for(False)
print("after Esc (应收起): %s  (%.1fs)" % (final, dt2))

ok = (after == (not start)) and (final is False)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
