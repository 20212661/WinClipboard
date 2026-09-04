# 自动化 GUI 测试:验证热键弹出面板
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

def count_visible(pid):
    res = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if _pid_of(hwnd) == pid:
            res.append(hwnd)
        return True
    win32gui.EnumWindows(cb, None)
    return len(res)

pid = int(sys.argv[1])
print("before (popup应隐藏):", count_visible(pid))
send_combo([0x11, 0x10], 0x56)          # Ctrl+Shift+V

def wait_ge(n, timeout=4.0):
    """轮询等待可见窗口数 >= n(注入热键的分发可能有延迟)。"""
    t0 = time.time()
    last = 0
    while time.time() - t0 < timeout:
        last = count_visible(pid)
        if last >= n:
            return last
        time.sleep(0.25)
    return last

after = wait_ge(1)
print("after Ctrl+Shift+V (应>=1):", after)
send_combo([], 0x1B)                     # Esc

def wait_le(n, timeout=4.0):
    t0 = time.time()
    last = after
    while time.time() - t0 < timeout:
        last = count_visible(pid)
        if last <= n:
            return last
        time.sleep(0.25)
    return last

final = wait_le(0)
print("after Esc (应回0):", final)
print("RESULT:", "PASS" if after >= 1 else "FAIL_NO_POPUP")
