# -*- coding: utf-8 -*-
"""ClipboardCore 核心逻辑单元测试(无 GUI;数据写入临时目录,不影响真实历史)。

运行: python _core_test.py   退出码 0 = 全部通过
"""
import os
import sys
import json as _json
import queue
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm

# ---- 隔离:所有持久化路径指到临时目录 ----
TMP = tempfile.mkdtemp(prefix="wclb_test_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
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
    core.worker = SimpleNamespace(cmd_q=queue.Queue())   # 粘贴命令桩
    core._set_clipboard = lambda rec: None               # 不碰系统剪贴板
    core.monitor_paused = False                          # 捕获门(暂停/排除)测试前置
    core._exclude_set = set()
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

# ---- paste_record:重排 / 文件丢失 / auto_paste 默认关 ----
core = make_core(cfg={"auto_paste": True})
core.add_item(("text", "x1"))
core.add_item(("text", "x2"))
rec = core.history[1]
check("paste_record", core.paste_record(rec) is True and core.history[0] is rec)
check("paste 排队(auto_paste 开)", core.worker.cmd_q.get_nowait()[0] == "paste")
core.cfg["auto_paste"] = False
core.paste_record(rec)
check("auto_paste 关不排队", core.worker.cmd_q.empty())
core2 = make_core()                     # 默认配置(未显式开 auto_paste)
core2.add_item(("text", "d1"))
core2.paste_record(core2.history[0])
check("默认配置不自动粘贴", core2.worker.cmd_q.empty())
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

# ---- 可撤销删除(批次撤销栈) ----
core = make_core()
for t in ("a", "b", "c"):
    core.add_item(("text", t))                  # history = [c, b, a]
core.toggle_pin_at(0)                           # c 置顶
removed = core.soft_clear_unpinned()
check("软清空置顶保留", [h["content"] for h in core.history] == ["c"]
      and [h["content"] for h in removed] == ["b", "a"])
core.undo_delete()
check("撤销恢复整批原序", [h["content"] for h in core.history] == ["b", "a", "c"])
core.soft_delete_at(0)
check("软删除出列", [h["content"] for h in core.history] == ["a", "c"])
core.undo_delete()
check("撤销单条回最前", [h["content"] for h in core.history] == ["b", "a", "c"])
check("空撤销栈返回空", core.undo_delete() == [])
from pathlib import Path as _P
_img = _P(cm.HISTORY_FILE).parent / "undo_keep.png"
_img.write_bytes(b"\x89PNG\r\n\x1a\n")         # 占位文件,验证软删除不删盘
core.history.insert(0, {"type": "image", "content": str(_img), "w": 1, "h": 1, "ts": 9})
core.soft_delete_at(0)
check("软删除保留图片文件", _img.exists())
core.undo_delete()
check("图片条目撤销回归", core.history[0]["content"] == str(_img) and _img.exists())
_img.unlink()

# ---- 热键可读标签(设置页卡片 / 托盘提示 / 录制对话框共用) ----
check("hotkey_label 默认组合", cm.hotkey_label(6, 0x56) == "Ctrl+Shift+V")
check("hotkey_label 全修饰+F5",
      cm.hotkey_label(15, 0x74) == "Alt+Ctrl+Shift+Win+F5")
check("hotkey_label 录制中间态", cm.hotkey_label(2, None) == "Ctrl+…")
check("hotkey_label 空格/未映射键",
      cm.hotkey_label(1, 0x20) == "Alt+空格"
      and "VK" in cm.hotkey_label(1, 0x92))

# ---- 按应用排除(捕获门同一隐私线)----
core = make_core()
core.set_exclude_apps(["KeePass.exe", " keepass.exe ",
                       r"C:\x\1Password.exe", "", "notepad.exe"])
check("排除名单归一化(文件名/小写/去重)",
      core.cfg["exclude_apps"] == ["keepass.exe", "1password.exe", "notepad.exe"],
      str(core.cfg["exclude_apps"]))
check("命中排除不捕获(大小写无关)",
      core.should_capture("keepass.exe") is False
      and core.should_capture("KeePass.EXE") is False)
check("未命中可捕获", core.should_capture("chrome.exe") is True)
check("空来源不参与名单匹配", core.should_capture("") is True)
core.monitor_paused = True
check("暂停优先于名单", core.should_capture("chrome.exe") is False)
core.monitor_paused = False
check("配置往返带名单", cm.load_config().get("exclude_apps")
      == ["keepass.exe", "1password.exe", "notepad.exe"])
core.set_exclude_apps([])
check("清空名单恢复捕获", core.should_capture("keepass.exe") is True)

# 来源应用随捕获入史(第三元素透传)
core.add_item(("text", "带来源", "Notepad.exe"))
check("来源应用入史(小写)", core.history[0].get("src") == "notepad.exe")
core.add_item(("text", "无来源"))
check("无来源不留字段", "src" not in core.history[0])
core.add_item(("text", "来源去重仍按内容", "chrome.exe"))
core.add_item(("text", "来源去重仍按内容", "notepad.exe"))
check("来源不影响内容去重", len([h for h in core.history
                              if h["content"] == "来源去重仍按内容"]) == 1)

# ---- 自动清理(cleanup_days / disk_cap_mb,置顶豁免)----
import time as _time
core = make_core(cfg={"cleanup_days": 7})
_now = _time.time()
core.history[:] = [
    {"type": "text", "content": "新", "ts": _now - 100},
    {"type": "text", "content": "旧", "ts": _now - 30 * 86400},
    {"type": "text", "content": "旧但置顶", "pinned": True, "ts": _now - 30 * 86400},
]
n, freed = core.apply_cleanup()
check("按天数清理+置顶豁免", n == 1
      and [h["content"] for h in core.history] == ["新", "旧但置顶"], str(n))

core = make_core()                          # 两档默认关闭 → no-op
core.history[:] = [{"type": "text", "content": "旧", "ts": 1}]
check("清理策略默认关闭", core.apply_cleanup() == (0, 0)
      and len(core.history) == 1)

core = make_core(cfg={"disk_cap_mb": 0.0001})
_fa = _P(cm.HISTORY_FILE).parent / "cap_a.png"
_fb = _P(cm.HISTORY_FILE).parent / "cap_b.png"
_fc = _P(cm.HISTORY_FILE).parent / "cap_c.png"
_fa.write_bytes(b"a" * 100)
_fb.write_bytes(b"b" * 100)
_fc.write_bytes(b"c" * 100)
core.history[:] = [
    {"type": "image", "content": str(_fa), "ts": 1},                 # 最旧 → 删
    {"type": "image", "content": str(_fb), "pinned": True, "ts": 2},  # 置顶 → 留
    {"type": "image", "content": str(_fc), "ts": 3},                  # 仍超限 → 删
    {"type": "text", "content": "文本不参与", "ts": 4},
]
n, freed = core.apply_cleanup()
check("容量上限:最旧先删+置顶豁免+图片文件删除",
      [h["content"] for h in core.history] == [str(_fb), "文本不参与"]
      and n == 2 and not _fa.exists() and not _fc.exists() and _fb.exists()
      and freed >= 200, "n=%s freed=%s" % (n, freed))
_fb.unlink()

# ---- 历史加密(history.json 可选 DPAPI)----
_hist = [{"type": "text", "content": "机密内容一二三", "ts": 5}]
cm.save_history(_hist, encrypt=True)
_raw_h = _P(cm.HISTORY_FILE).read_text(encoding="utf-8")
check("加密历史文件不含明文", "机密内容一二三" not in _raw_h
      and '"dpapi": true' in _raw_h, _raw_h[:60])
check("加密历史可读回", cm.load_history() == _hist)
cm.save_history(_hist)                      # 关闭加密 → 明文回写
check("明文历史往返(兼容旧格式)",
      "机密内容一二三" in _P(cm.HISTORY_FILE).read_text(encoding="utf-8")
      and cm.load_history() == _hist)

# ---- 富文本格式随条目(html/rtf + src 同批)----
core = make_core()
core.add_item(("text", "带格式",
               {"html": "<b>x</b>", "rtf": "{\\\\rtf1}", "junk": "被过滤"},
               "word.exe"))
_r = core.history[0]
check("富文本字段入史+未知键过滤", _r.get("html") == "<b>x</b>"
      and _r.get("rtf") == "{\\\\rtf1}" and "junk" not in _r
      and _r.get("src") == "word.exe")
core.add_item(("text", "纯文本", "notepad.exe"))
check("无富文本不受影响", "html" not in core.history[0]
      and core.history[0].get("src") == "notepad.exe")

# ---- 来源 / 时间筛选(filter_view 扩展)----
core = make_core()
core.history[:] = [
    {"type": "text", "content": "a", "ts": 100, "src": "notepad.exe"},
    {"type": "text", "content": "b", "ts": _time.time(), "src": "chrome.exe"},
    {"type": "text", "content": "c", "ts": _time.time() - 90000, "src": ""},
]
check("按来源筛选", core.filter_view("all", src="notepad.exe") == [0]
      and core.filter_view("all", src="chrome.exe") == [1]
      and core.filter_view("all") == [0, 1, 2])
_cut = core.time_scope_cutoff("24h")
check("时间档位过滤", _cut > 0
      and core.filter_view("all", since=_cut) == [1]
      and core.time_scope_cutoff("all") == 0.0)
check("今天档只留新鲜", core.filter_view(
    "all", since=core.time_scope_cutoff("today")) == [1])
check("source_apps 随历史序(新在前)",
      core.source_apps() == ["notepad.exe", "chrome.exe"])

# ---- 批量软删(段选 Delete 的核心路径)----
core = make_core()
core.history[:] = [{"type": "text", "content": c, "ts": i}
                   for i, c in enumerate("abcde")]
_n = core.batch_soft_delete([core.history[0], core.history[2],
                             {"type": "text", "content": "不在历史"}])
check("批量软删按身份", _n == 2 and len(core.history) == 3
      and [h["content"] for h in core.history] == ["b", "d", "e"])
check("批量删除整批撤销", len(core.undo_delete()) == 2
      and len(core.history) == 5)

# ---- 导出 / 导入 ----
core = make_core()
core.history[:] = [
    {"type": "text", "content": "导出条目", "ts": 123.0, "src": "a.exe",
     "pinned": True, "html": "<i>x</i>"},
    {"type": "files", "content": ["Z:/不存在.txt"], "ts": 5},   # 文件丢失 → 跳过
]
_pl = core.export_payload()
check("导出载荷(丢文件条目剔除)", len(_pl["items"]) == 1
      and _pl["items"][0]["content"] == "导出条目"
      and _pl["items"][0]["html"] == "<i>x</i>")
core2 = make_core()
_a2, _s2 = core2.import_payload(_pl)
check("导入还原字段", _a2 == 1 and _s2 == 0
      and core2.history[0]["ts"] == 123.0
      and core2.history[0]["pinned"] is True
      and core2.history[0].get("src") == "a.exe")
_a3, _s3 = core2.import_payload(_pl)
check("重复导入去重", _a3 == 0 and _s3 == 1)
_ok = False
try:
    core2.import_payload({"no": 1})
except ValueError:
    _ok = True
check("非法载荷报错", _ok)

from PIL import Image as _PILImage
_px = _P(cm.HISTORY_FILE).parent / "exp_img.png"
_PILImage.new("RGB", (8, 6), (9, 9, 9)).save(_px)
core3 = make_core()
core3.history[:] = [{"type": "image", "content": str(_px), "w": 8, "h": 6,
                     "hash": "h9", "ts": 9}]
check("图片默认不导出", core3.export_payload()["items"] == [])
_pl4 = core3.export_payload(include_images=True)
core4 = make_core()
_a4, _ = core4.import_payload(_pl4)
_p2 = _P(cm.IMAGE_DIR) / os.path.basename(core4.history[0]["content"])
check("内嵌图片导入落盘", _a4 == 1 and _p2.exists())
_px.unlink()
_p2.unlink()

# ---- 版本比较(更新检查用)----
check("version_tuple 基本与容错", cm.version_tuple("1.2.10") == (1, 2, 10)
      and cm.version_tuple("v1.2") == (1, 2, 0)
      and cm.version_tuple("1.2.3-beta") == (1, 2, 3)
      and cm.version_tuple("") == (0, 0, 0)
      and cm.version_tuple(None) == (0, 0, 0))
check("version_tuple 排序语义", cm.version_tuple("1.0.10") > cm.version_tuple("1.0.9")
      and cm.version_tuple("2.0") > cm.version_tuple("1.99.99")
      and cm.version_tuple("1.0.0") == cm.version_tuple("1.0"))
check("APP_VERSION 可解析且非零", cm.version_tuple(cm.APP_VERSION) >= (1, 0, 0))

# ---- 私密区持久化往返 + DPAPI 加密 ----
cm.save_private(["甲", "b&c", "✓ 多行\n文本"])
check("private 往返", cm.load_private() == ["甲", "b&c", "✓ 多行\n文本"])
with open(cm.PRIVATE_FILE, "r", encoding="utf-8") as f:
    raw = f.read()
stored = _json.loads(raw)
check("private 已加密", stored.get("dpapi") is True and "甲" not in raw and "b&c" not in raw)
# 旧版明文格式兼容(返回内容,下次保存自动升级)
Path(cm.PRIVATE_FILE).write_text(_json.dumps(["旧明文1", "旧明文2"], ensure_ascii=False),
                                  encoding="utf-8")
check("旧明文兼容", cm.load_private() == ["旧明文1", "旧明文2"])
# 解密失败不覆盖原文件(伪造密文)
Path(cm.PRIVATE_FILE).write_text('{"dpapi": true, "data": "AAAAzw=="}', encoding="utf-8")
check("坏密文返回空", cm.load_private() == [])
# 启动迁移:明文文件 → 立即升级加密
Path(cm.PRIVATE_FILE).write_text(_json.dumps(["迁移项"], ensure_ascii=False),
                                  encoding="utf-8")
core = cm.ClipboardCore.__new__(cm.ClipboardCore)
core.private = cm.load_private()
core._migrate_private()
with open(cm.PRIVATE_FILE, "r", encoding="utf-8") as f:
    check("明文自动升级", _json.load(f).get("dpapi") is True)

# ---- 敏感识别(顺带回归) ----
check("识别 API key", cm.is_sensitive("sk-" + "a" * 30))
check("识别银行卡", cm.is_sensitive("6222 0210 0110 2581888"[:0] or "6222600260001072444"))
check("不误判普通文本", not cm.is_sensitive("hello world 123"))

# ---- config.json:原子写 + 密钥 DPAPI 加密 ----
SECRET = "sk-proj-SUPERSECRET-0123456789abcdef"
cm.save_config({"ai_services": [{"name": "GLM", "base_url": "http://x/v1",
                                 "model": "m", "api_key": SECRET}],
                "max_items": 42})
with open(cm.CONFIG_FILE, "r", encoding="utf-8") as f:
    raw_cfg = f.read()
check("config 密钥不落明文", SECRET not in raw_cfg)
stored = _json.loads(raw_cfg)
check("config 密钥已标记 dpapi", stored["ai_services"][0]["api_key"].get("dpapi") is True)
check("config 保留其它键", stored.get("max_items") == 42)
check("config 往返可解密", cm.load_config()["ai_services"][0]["api_key"] == SECRET)

# 旧版明文 config 自动升级(启动迁移)
Path(cm.CONFIG_FILE).write_text(
    _json.dumps({"ai_services": [{"name": "DeepSeek", "base_url": "http://y/v1",
                                  "model": "m", "api_key": SECRET}]}),
    encoding="utf-8")
core = cm.ClipboardCore.__new__(cm.ClipboardCore)
core.cfg, core._cfg_need_save = cm._load_config_raw()
check("明文 config 标记待升级", core._cfg_need_save is True)
core._migrate_config_secrets()
with open(cm.CONFIG_FILE, "r", encoding="utf-8") as f:
    migrated = f.read()
check("明文 config 已升级加密", SECRET not in migrated
      and _json.loads(migrated)["ai_services"][0]["api_key"].get("dpapi") is True)
# 关键回归:迁移不得把明文残留在任何滚动备份里
leaked = [f for f in os.listdir(TMP)
          if f.startswith("config.json") and SECRET in open(os.path.join(TMP, f),
                                                            encoding="utf-8").read()]
check("备份无明文残留", not leaked, "泄漏于 %s" % leaked)
check("二次启动不再回写", cm._load_config_raw()[1] is False)

# 坏密文不丢配置、不炸(解密失败置空但不覆盖原文件)
Path(cm.CONFIG_FILE).write_text(
    '{"ai_services":[{"name":"X","base_url":"u","model":"m",'
    '"api_key":{"dpapi":true,"data":"AAAAzw=="}}]}', encoding="utf-8")
cfg_bad, stale_bad = cm._load_config_raw()
check("坏密文置空且不回写", cfg_bad["ai_services"][0]["api_key"] == "" and stale_bad is False)

# config.json 损坏 → 从备份恢复
cm.save_config({"ai_services": [], "max_items": 42})         # 本代
cm.save_config({"ai_services": [], "max_items": 99})         # 新一代:42 那份轮转到 .bak1
check("备份已轮转", _json.load(open(cm.CONFIG_FILE + ".bak1", encoding="utf-8"))["max_items"] == 42)
Path(cm.CONFIG_FILE).write_text("{ 这不是 JSON", encoding="utf-8")
check("config 损坏回退备份", cm.load_config().get("max_items") == 42)

# ---- 历史变化事件走队列(不再跨线程直调 UI) ----
core = make_core()
core.add_item(("text", "notify"))
kinds = []
while not core.signal_q.empty():
    kinds.append(core.signal_q.get_nowait()[0])
check("add_item 投递 change 信号", kinds == ["change"])

# ---- 查看器:语言识别 + JSON 重排 ----
_lang_cases = [
    ('{"a": 1, "b": [1,2]}', "json"),
    ('{"broken": ', "text"),                     # 不合法 JSON 不应误报
    ('def hello():\n    import os\n    return 1', "python"),
    ('from collections import OrderedDict\n', "python"),
    ('SELECT id FROM users WHERE x > 1;', "sql"),
    ('const x = [1,2].map(v => v * 2);', "javascript"),
    ('function add(a, b) {\n  return a + b;\n}', "javascript"),
    ('.btn {\n  color: red;\n  margin: 0;\n}', "css"),
    ('<?xml version="1.0"?><root/>', "xml"),
    ('<html><body>hi</body></html>', "html"),
    ('# 标题\n\n```python\nprint(1)\n```', "markdown"),
    ('#!/bin/bash\necho hi', "shell"),
    ('今天天气不错,适合摸鱼\n- 议题一', "text"),   # 普通中文不应误报为代码
]
for _t, _want in _lang_cases:
    got = cm.detect_lang(_t)
    check("语言识别 %r→%s" % (_t[:18], _want), got == _want, "got=%s" % got)

check("JSON 美化", cm.json_reformat('{"a":1,"b":[2,3]}') ==
      '{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}')
check("JSON 紧凑", cm.json_reformat('{\n "a": 1,\n "b": 2\n}', compact=True) ==
      '{"a":1,"b":2}')
check("JSON 重排保留中文", cm.json_reformat('{"名":"值"}', compact=True) == '{"名":"值"}')
check("非法 JSON 返回 None", cm.json_reformat("not json") is None)
check("空文本识别为纯文本", cm.detect_lang("") == "text")

# ---- ContentInfo 检测管线(detect_content)----
import base64 as _b64
import json as _json2


def _b64e(s):
    return _b64.b64encode(s.encode("utf-8")).decode("ascii")


def _b64url(s):
    return _b64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


_JWT = (_b64url('{"alg":"HS256","typ":"JWT"}') + "." +
        _b64url('{"sub":"1234567890","name":"张三"}') + "." +
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_ci = cm.detect_content(_JWT)
check("JWT 识别", (_ci.category, _ci.subtype) == ("encoded", "jwt"),
      "got=%s/%s" % (_ci.category, _ci.subtype))
check("JWT 不误报语言", _ci.lang == "text")
_h, _p = cm._sniff_jwt(_JWT)
check("JWT 解出头与载荷", _h.get("alg") == "HS256" and _p.get("name") == "张三")
check("两段式不是 JWT", cm.detect_content("abc.def").subtype != "jwt")
check("URL 不是 JWT", cm._sniff_jwt("https://a.example.com/x.y.z") is None)

_ci = cm.detect_content("https://example.com/api/users?page=2&token=abc")
check("URL 识别", (_ci.category, _ci.subtype) == ("url", "web_url"))
check("多行文本不算 URL", cm.detect_content("看这个\nhttps://a.com/b").category != "url")

_ci = cm.detect_content(_b64e("一段需要足够长才能够被嗅探出来的中文文本内容,补足四十个字符以上"))
check("Base64 文本识别", (_ci.category, _ci.subtype, _ci.metadata.get("b64_kind"))
      == ("encoded", "base64", "text"))
_png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")
_ci = cm.detect_content(_b64e("x" * 0) + _b64.b64encode(_png * 4).decode())
check("Base64 图片魔数识别", _ci.subtype == "base64"
      and _ci.metadata.get("b64_kind") == "image")
check("data:URI 前缀可识别", cm.detect_content(
    "data:image/png;base64," + _b64.b64encode(_png * 4).decode()).subtype == "base64")
check("短词不是 Base64", cm.detect_content("hello").category != "encoded")
check("十六进制哈希不是 Base64", cm.detect_content("a" * 64).category != "encoded")
check("无空格长单词不是 Base64",
      cm.detect_content("thequickbrownfoxjumpsoverthelazydogandrunaway").category
      != "encoded")
check("许可证密钥样式不是 Base64",
      cm.detect_content("AB3JK-9LK2M-7QQW1-PLZ4C-9D2EFGHI").category != "encoded")

_ci = cm.detect_content('{"k": [1,2]}')
check("JSON ContentInfo", (_ci.category, _ci.subtype, _ci.confidence)
      == ("structured_text", "json", 1.0))
_ci = cm.detect_content("a,b\nc,d\ne,f\ng,h")
check("CSV metadata 带分隔符", _ci.subtype == "csv" and _ci.metadata.get("delim") == ",")
_ci = cm.detect_content("def f():\n    pass")
check("代码走 code 类", _ci.category == "code" and _ci.confidence == 0.8)
_ci = cm.detect_content("普通中文文本,没有任何结构。")
check("纯文本", (_ci.category, _ci.subtype, _ci.confidence) == ("text", "plain", 1.0))
check("空文本 ContentInfo", cm.detect_content("").subtype == "plain")
check("包装保持 lang 语义", cm.detect_lang('{"a":1}') == "json"
      and cm.detect_lang(_JWT) == "text")

# ---- Diff:unified + JSON 语义对比 ----
_dl = cm.unified_diff_lines("timeout: 30\n", "timeout: 60\nretry: 3\n")
check("unified diff 有增删行", any(l.startswith("-") and "30" in l for l in _dl)
      and any(l.startswith("+") and "60" in l for l in _dl)
      and any(l.startswith("+") and "retry" in l for l in _dl), str(_dl))
check("unified diff 截断护栏",
      len(cm.unified_diff_lines("", "x\n" * 9999, cap=100)) <= 101)

_ok, _ch = cm.json_semantic_diff('{"name":"A","timeout":30}',
                                 '{"timeout": 30, "name": "A"}')
check("JSON 键序不同=无差异", _ok and _ch == [], str(_ch))
_ok, _ch = cm.json_semantic_diff('{"timeout":30,"name":"A"}',
                                 '{"timeout":60,"name":"A","retry":3}')
_paths = {(p, k) for p, k, *_ in _ch}
check("JSON 语义找出改/增", ("$.timeout", "change") in _paths
      and ("$.retry", "add") in _paths, str(_ch))
check("JSON 值变更带新旧值",
      next((o for p, k, o, n in _ch if p == "$.timeout"), None) == "30"
      and next((n for p, k, o, n in _ch if p == "$.timeout"), None) == "60")
_ok, _ch = cm.json_semantic_diff('{"a":[1,2,3]}', '{"a":[1,2]}')
check("JSON 数组删项", ("$.a[2]", "del") in {(p, k) for p, k, *_ in _ch})
_ok, _ch = cm.json_semantic_diff('{"ok":true}', '{"ok":1}')
check("布尔与数字不同型", ("$.ok", "change") in {(p, k) for p, k, *_ in _ch})
_ok, _ch = cm.json_semantic_diff('{"n":1}', '{"n":1.0}')
check("int/float 数值等价", _ok and _ch == [], str(_ch))
check("格式化差异语义相等",
      cm.json_semantic_diff('{"a":1,"b":[1, 2]}', '{\n  "a": 1,\n  "b": [1,2]\n}')[1] == [])
check("非法 JSON 返回不可比", cm.json_semantic_diff("{bad", "{}")[0] is False)

# ---- 卡片子类型徽章(轻量探测,不跑 pygments) ----
check("徽章:JWT", cm.content_badge(_JWT) == "JWT")
check("徽章:Base64", cm.content_badge(_b64e("足够长的一段文本用于触发 Base64 嗅探的下限要求")) == "Base64")
check("徽章:普通文本无", cm.content_badge("今天天气不错") is None)
check("徽章:JSON 不占用", cm.content_badge('{"a":1}') is None)
check("徽章:URL 不占用(有 link 卡片)", cm.content_badge("https://a.com/b") is None)
check("徽章:空文本无", cm.content_badge("") is None)

# ---- 快捷粘贴浮层(后端配合项) ----
core = make_core(cfg={"auto_paste": False})
core.add_item(("text", "quick-force", {}, ""))
rec = core.history[0]
core.paste_record(rec)
check("paste_record 默认不回贴(auto_paste 关)", core.worker.cmd_q.empty())
core.paste_record(rec, force=True)
check("paste_record force 无条件回贴",
      core.worker.cmd_q.get_nowait()[0] == "paste")
check("快捷粘贴默认热键(Alt+`)",
      cm.DEFAULT_CFG.get("quick_mod") == cm.MOD_ALT
      and cm.DEFAULT_CFG.get("quick_key") == 0xC0)
check("hotkey_label OEM 键名", cm.hotkey_label(cm.MOD_ALT, 0xC0) == "Alt+`")
check("hotkey_label OEM 负号", cm.hotkey_label(cm.MOD_CONTROL, 0xBD) == "Ctrl+-")

# ---- 捕获管线(pipeline.py):阶段化决策 + 可追踪性 ----
import pipeline as _pp
_s4 = _pp.Snapshot.from_item(("text", "x", {"html": "<b>"}, "Abc.EXE"))
check("Snapshot 4 元协议(src 归一小写)", _s4.src == "abc.exe" and _s4.rich == {"html": "<b>"})
_s2 = _pp.Snapshot.from_item(("files", ["a", "b"]))
check("Snapshot 2 元协议兼容", _s2.kind == "files" and _s2.src == "")
check("fingerprint 三类",
      _pp.fingerprint(_pp.Snapshot.from_item(("text", "t", {}, ""))) == "t"
      and _pp.fingerprint(_pp.Snapshot.from_item(("image", {"path": "p", "hash": "h"})))
      == ("img", "h")
      and _pp.fingerprint(_s2) == ("files", ("a", "b")))
core = make_core()
core.add_item(("text", "password: secret123", {}, ""))
_tr = core.last_capture_trace
check("trace 敏感路径含原因",
      any(s == "sensitive" and "密码" in d for s, d in _tr.stages), str(_tr))
check("敏感文本不入史入私密区", len(core.history) == 0 and len(core.private) == 1)
core2 = make_core(cfg={"sensitive_guard": False})
core2.add_item(("text", "password: secret123", {}, ""))
check("guard 关时入史", len(core2.history) == 1 and len(core2.private) == 0)
check("trace 标注 guard 已关闭",
      any("guard 已关闭" in d for s, d in core2.last_capture_trace.stages))
core3 = make_core()
core3.add_item(("text", "dup-me", {}, ""))
core3.add_item(("text", "dup-me", {"html": "<i>x</i>"}))
check("重复提升保留新富文本", core3.history[0].get("html") == "<i>x</i>"
      and len(core3.history) == 1)
check("trace 重复提升", any(s == "dedup" and "提升" in d
                            for s, d in core3.last_capture_trace.stages))
core3.toggle_pin_at(0)
core3.add_item(("text", "dup-me", {}, ""))
check("置顶重复跳过", len(core3.history) == 1
      and any("置顶" in d for s, d in core3.last_capture_trace.stages))
check("trace 可读化含各阶段", "snapshot:" in str(core3.last_capture_trace)
      and "fingerprint:" in str(core3.last_capture_trace))

print()
print("RESULT:", "PASS" if not FAILS else "FAIL %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
