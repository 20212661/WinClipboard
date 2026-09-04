# -*- coding: utf-8 -*-
"""ClipboardCore 核心逻辑单元测试(无 GUI;数据写入临时目录,不影响真实历史)。

运行: python _core_test.py   退出码 0 = 全部通过
"""
import os
import sys
import queue
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm

# ---- 隔离:所有持久化路径指到临时目录 ----
TMP = tempfile.mkdtemp(prefix="wclb_test_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

FAILS = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILS.append(name)


def make_core(history=None, private=None, cfg=None, max_items=100):
    """绕过 __init__(不启动托盘线程/热键),手工构造核心状态。"""
    core = cm.ClipboardCore.__new__(cm.ClipboardCore)
    core.cfg = dict(cm.DEFAULT_CFG)
    core.cfg.update(cfg or {})
    core.max_items = max_items
    core.history = history if history is not None else []
    core.private = private if private is not None else []
    core.signal_q = queue.Queue()
    core.on_change = lambda: None
    core.worker = SimpleNamespace(cmd_q=queue.Queue())   # 粘贴命令桩
    core._set_clipboard = lambda rec: None               # 不碰系统剪贴板
    return core


# ---- filter_view:分类 + 关键词 ----
HIST = [
    {"type": "text", "content": "hello world", "pinned": False, "ts": 4},
    {"type": "image", "content": "x.png", "pinned": False,
     "w": 10, "h": 20, "hash": "a", "ts": 3},
    {"type": "text", "content": "https://example.com/a", "pinned": False, "ts": 2},
    {"type": "files", "content": ["C:/tmp/a.txt"], "pinned": False, "ts": 1},
]
core = make_core(history=HIST)
check("filter all", core.filter_view("all") == [0, 1, 2, 3])
check("filter text", core.filter_view("text") == [0, 2])
check("filter image", core.filter_view("image") == [1])
check("filter link", core.filter_view("link") == [2])
check("filter files", core.filter_view("files") == [3])
check("filter 关键词", core.filter_view("all", "hello") == [0])
check("filter 图片尺寸", core.filter_view("all", "10×20") == [1])

# ---- add_item:去重 / 敏感隔离 / 淘汰 ----
core = make_core()
core.add_item(("text", "aaa"))
core.add_item(("text", "bbb"))
core.add_item(("text", "aaa"))                    # 重复 → 移前去重
check("去重置顶", [h["content"] for h in core.history] == ["aaa", "bbb"])
core.add_item(("text", "password: secret123"))    # 敏感 → 私密区
check("敏感隔离", len(core.history) == 2 and core.private == ["password: secret123"])

core = make_core(max_items=3)
for i in range(6):
    core.add_item(("text", "n%d" % i))
check("超量淘汰", [h["content"] for h in core.history] == ["n5", "n4", "n3"])
core.toggle_pin_at(2)                             # 置顶 n3(最旧)
core.add_item(("text", "new"))                    # 超量时淘汰非置顶的 n4,n3 保留
check("置顶豁免淘汰", [h["content"] for h in core.history] == ["new", "n5", "n3"]
      and core.history[-1]["pinned"] is True)

# ---- paste_record:重排 / 文件丢失 ----
core = make_core()
core.add_item(("text", "x1"))
core.add_item(("text", "x2"))
rec = core.history[1]
check("paste_record", core.paste_record(rec) is True and core.history[0] is rec)
check("paste 排队", core.worker.cmd_q.get_nowait()[0] == "paste")
check("丢失文件拒绝", core.paste_record(
    {"type": "files", "content": ["Z:/不存在/1.txt"]}) is False)

# ---- 条目操作 / 清空 ----
core.toggle_pin_at(0)
check("置顶切换", core.history[0]["pinned"] is True)
core.delete_at(0)
check("删除", len(core.history) == 1)
core.add_item(("text", "keep"))
core.toggle_pin_at(0)
core.add_item(("text", "gone"))
core.clear_unpinned()
check("清空保留置顶", [h["content"] for h in core.history] == ["keep"])

# ---- 私密区持久化往返 ----
cm.save_private(["甲", "b&c", "✓ 多行\n文本"])
check("private 往返", cm.load_private() == ["甲", "b&c", "✓ 多行\n文本"])

# ---- 敏感识别(顺带回归) ----
check("识别 API key", cm.is_sensitive("sk-" + "a" * 30))
check("识别银行卡", cm.is_sensitive("6222 0210 0110 2581888"[:0] or "6222600260001072444"))
check("不误判普通文本", not cm.is_sensitive("hello world 123"))

print()
print("RESULT:", "PASS" if not FAILS else "FAIL %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
