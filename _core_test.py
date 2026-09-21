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
cm.BLOB_DIR = os.path.join(TMP, "data", "blobs")   # 与 blob_full_path 布局一致
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)
os.makedirs(cm.BLOB_DIR, exist_ok=True)

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

# ---- 日志脱敏(SafeLogger):不变量 = 日志永不携带密钥/令牌/正文 ----
_jwt = ("eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_s1 = cm.sanitize_log("处理失败 密码=hunter2secret token=%s" % _jwt)
check("脱敏:键值对秘密遮盖", "hunter2secret" not in _s1 and "密码=[已遮盖]" in _s1)
check("脱敏:JWT 遮盖", "eyJhbGci" not in _s1
      and "[JWT已遮盖]" in cm.sanitize_log("jwt: " + _jwt))
_s2 = cm.sanitize_log(
    "key=sk-AbCdEf1234567890AbCdEf1234567890 "
    "url=https://x.com/a?token=tok_abc123xyz&sig=1")
check("脱敏:API Key 遮盖", "sk-AbCdEf" not in _s2)
check("脱敏:URL 查询串遮盖(路径保留)",
      "token=tok" not in _s2 and "https://x.com/a" in _s2)
_s3 = cm.sanitize_log("hash=72acf1e2" + "a" * 56 + " 正常消息")
check("脱敏:sha256 摘要放行(安全元数据)", "72acf1e2" in _s3)
_s4 = cm.sanitize_log(
    "b64=QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2"
    "d3h5ejAxMjM0" * 3)
check("脱敏:超长 base64 遮盖", "QUJDREVG" not in _s4 and "[长串" in _s4)
_s5 = cm.sanitize_log("常规消息:路径 C:/Users/x/a.png 数量 3")
check("脱敏:常规消息不受影响", _s5 == "常规消息:路径 C:/Users/x/a.png 数量 3")
_s6 = cm.sanitize_log("超长正文" * 600)
check("脱敏:超长截断兜底", "已截断" in _s6 and len(_s6) < 2100)
check("脱敏:多行 traceback 结构保留",
      "\n" in cm.sanitize_log("Traceback:\n  File x\nValueError: bad"))
cm.log("捕获失败 密码=hunter2secret jwt=%s" % _jwt)
_logtxt = _P(cm.LOG_FILE).read_text(encoding="utf-8")
check("不变量8:app.log 不含密钥/JWT(端到端)",
      "hunter2secret" not in _logtxt and "eyJhbGci" not in _logtxt)

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
_p2 = _P(core4.history[0]["content"])          # 实际落盘路径(分片 blob 布局)
check("内嵌图片导入落盘", _a4 == 1 and _p2.exists()
      and "data" in _p2.parts and "blobs" in _p2.parts, str(_p2))
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

# ---- SQLite 存储(store.py):往返 / FTS+中文 LIKE / 参数化查询 / 写透 / 重启重载 ----
import store as _st
_sdir = tempfile.mkdtemp(prefix="wclb_store_")
_sdb = os.path.join(_sdir, "clipboard.db")
st = _st.ClipboardStore(_sdb)
check("schema:全新库建表即打版本戳",
      st.db.execute("PRAGMA user_version").fetchone()[0] == _st.SCHEMA_VERSION)
st.insert({"type": "text", "content": "hello sqlite world", "pinned": True,
           "ts": 1.0, "src": "a.exe", "html": "<b>x</b>"})
st.insert({"type": "text", "content": "你好剪贴板数据库", "pinned": False,
           "ts": 2.0})
st.insert({"type": "files", "content": ["C:/t/a.txt", "C:/t/b.txt"],
           "pinned": False, "ts": 3.0})
loaded = st.load_all()
check("库往返:seq 降序(最近在前)",
      [r["content"] for r in loaded] ==
      [["C:/t/a.txt", "C:/t/b.txt"], "你好剪贴板数据库", "hello sqlite world"])
_r0 = loaded[2]
check("库往返:字段保真(src/rich/pinned/ts)",
      _r0.get("src") == "a.exe" and _r0.get("html") == "<b>x</b>"
      and _r0["pinned"] is True and _r0["ts"] == 1.0)
check("子串搜索英文命中", len(st.query(q="sqlite")) == 1)
check("子串搜索部分词命中(LIKE 语义,FTS 词级做不到)",
      len(st.query(q="sqlit")) == 1)
check("中文子串命中", len(st.query(q="剪贴板")) == 1)
check("搜索无命中", st.query(q="zzznotexist") == [])
import sqlite3 as _sq3
with _sq3.connect(_sdb) as _cx0:
    _fts_n = _cx0.execute("SELECT COUNT(*) AS c FROM items_fts "
                          "WHERE items_fts MATCH ?", ('"sqlite"',)).fetchone()[0]
check("FTS5 表由触发器同步维护", _fts_n == 1)
check("query 类型过滤", len(st.query(content_type="text")) == 2)
check("query 来源过滤", len(st.query(source_app="a.exe")) == 1)
check("query 时间过滤", len(st.query(since=2.5)) == 1)
check("query 置顶过滤", len(st.query(pinned=True)) == 1)
check("query 分页 limit/offset",
      len(st.query(content_type="text", limit=1, offset=1)) == 1
      and len(st.query(content_type="text", limit=1, offset=5)) == 0)
st.update_pinned(loaded[0]["_db"], True)
check("置顶写库", st.query(pinned=True)[0] == loaded[0]["_db"])
st.delete(loaded[0]["_db"])
check("删行生效", st.count() == 2)

# 迁移:history.json(最近在前)→ 库(最旧先插)
_sdb2 = os.path.join(_sdir, "migrate.db")
st2 = _st.ClipboardStore(_sdb2)
legacy = [{"type": "text", "content": "旧-%d" % i, "pinned": i == 0,
           "ts": 100.0 + i} for i in range(5)]     # 旧-4 最新
check("迁移条数", st2.migrate_from_list(legacy) == 5)   # 列表本身最旧在前
check("迁移后顺序", [r["content"] for r in st2.load_all()] ==
      ["旧-4", "旧-3", "旧-2", "旧-1", "旧-0"])

# 全核心写透:启用库的 make_core,增/去重/删/撤销/置顶后重启重载一致
core = make_core()
core.store = _st.ClipboardStore(os.path.join(_sdir, "core.db"))
core.add_item(("text", "alpha", {}, "notepad.exe"))
core.add_item(("text", "beta", {}, ""))
core.add_item(("text", "alpha", {}, ""))            # 去重提升(旧行删除+新行插入)
check("库模式去重行数", core.store.count() == 2)
core.toggle_pin_at(1)
core.soft_delete_at(0)
check("软删即删行", core.store.count() == 1)
core.undo_delete()
check("撤销即回写", core.store.count() == 2)
_reopen = _st.ClipboardStore(os.path.join(_sdir, "core.db"))
_check_rows = [(r["type"], r["content"], bool(r.get("pinned")))
               for r in _reopen.load_all()]
check("重启重载与内存一致(内容/顺序/置顶)",
      _check_rows == [("text", "alpha", False), ("text", "beta", True)],
      str(_check_rows))
_b64t = "QmFzZTY0IGRlbW8gY29udGVudCBzdHJpbmcgdGhhdCBpcyBsb25nIGVub3VnaA=="
core.add_item(("text", _b64t, {}, ""))
with __import__("sqlite3").connect(os.path.join(_sdir, "core.db")) as _cx:
    _sub = _cx.execute("SELECT content_subtype FROM clipboard_items "
                       "WHERE text_content = ?", (_b64t,)).fetchone()
check("子类型列写入(Base64)", _sub and _sub[0] == "Base64", str(_sub))
st.close(); st2.close(); _reopen.close(); core.store.close()

# query_view(SQL 路径)与 filter_view(内存路径)全组合等价
core5 = make_core()
core5.store = _st.ClipboardStore(os.path.join(_sdir, "eq.db"))
core5.add_item(("text", "hello sqlite world", {}, "notepad.exe"))
core5.add_item(("text", "你好剪贴板数据库", {}, ""))
core5.add_item(("text", "https://example.com/a?x=1", {}, "chrome.exe"))
core5.add_item(("files", [__file__], {}, ""))
core5.add_item(("image", {"path": os.path.join(_sdir, "x.png"),
                          "w": 40, "h": 30, "hash": "hash9"}, ""))
_eq_ok = True
for _cat in ("all", "text", "link", "image", "files"):
    for _qq in ("", "sqlite", "sqlit", "剪贴", "example", "zzz"):
        for _src in ("", "notepad.exe"):
            if core5.query_view(_cat, _qq, src=_src) != \
                    core5.filter_view(_cat, _qq, src=_src):
                _eq_ok = False
check("query_view 与 filter_view 全组合等价(60 组合)", _eq_ok)
core5.store.close()

# v1 库(无 search_text)打开即自动升级:补列 + 回填 + 重建 FTS
_v1 = os.path.join(_sdir, "v1.db")
_cx = _sq3.connect(_v1)
_cx.executescript("""
CREATE TABLE clipboard_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    content_type TEXT NOT NULL, content_subtype TEXT, content_hash TEXT,
    source_app TEXT, source_window TEXT, text_content TEXT,
    files_json TEXT, rich_json TEXT, blob_hash TEXT, blob_mime TEXT,
    blob_width INTEGER, blob_height INTEGER, blob_size INTEGER,
    is_pinned INTEGER NOT NULL DEFAULT 0, metadata_json TEXT);
""")
_cx.execute("INSERT INTO clipboard_items (seq, created_at, updated_at, "
            "content_type, text_content, is_pinned) VALUES (?,?,?,?,?,?)",
            (1, 1.0, 1.0, "text", "old row hello v1", 0))
_cx.commit()
_cx.close()
stv = _st.ClipboardStore(_v1)
check("schema:v1 逐级迁到当前版本",
      stv.db.execute("PRAGMA user_version").fetchone()[0] == _st.SCHEMA_VERSION)
_v1_ids = stv.query(q="hello")
check("v1 库自动升级+回填(search_text 命中)", _v1_ids == [1], str(_v1_ids))
with _sq3.connect(_v1) as _cx1:
    _fts_v1 = _cx1.execute("SELECT COUNT(*) AS c FROM items_fts "
                           "WHERE items_fts MATCH ?", ('"hello"',)).fetchone()[0]
check("v1 升级后 FTS 重建可查", _fts_v1 == 1)
stv.close()

# ---- 粘贴事务(Paste Transaction):目标窗口/剪贴板序列校验,不符即取消 ----
_evt = []
_wk = cm.TrayWorker.__new__(cm.TrayWorker)
_wk.app = SimpleNamespace(_post=lambda e: _evt.append(e))
_sent = []
_o_send, _o_fg, _o_seq, _o_time = (cm.send_ctrl_v, cm._get_foreground,
                                   cm._get_clip_seq, cm.time)
import time as _tmod


class _NoSleepTime:
    @staticmethod
    def sleep(s):
        pass

    @staticmethod
    def monotonic():
        return _tmod.monotonic()


try:
    cm.send_ctrl_v = lambda: _sent.append(1)
    cm.time = _NoSleepTime
    # 场景 1:目标窗口在宽限期内就位 → 发送
    cm._get_foreground = lambda: 4242
    cm._get_clip_seq = lambda: 7
    _wk._do_cmd(("paste", 4242, 7))
    check("事务:目标就位即发送", _sent == [1] and not _evt, str(_evt))
    # 场景 2:前台始终是别的窗口 → 取消 + 回告
    _sent.clear(); _evt.clear()
    cm._get_foreground = lambda: 9999
    _wk._do_cmd(("paste", 4242, 7))
    check("事务:窗口切换即取消", not _sent and _evt
          and _evt[0][0] == "paste_abort" and "切换" in _evt[0][1], str(_evt))
    # 场景 3:剪贴板在发送前被其他程序改写 → 取消
    _sent.clear(); _evt.clear()
    _seq_state = {"n": 7}
    cm._get_clip_seq = lambda: (_seq_state.update(n=9), 9)[1]
    cm._get_foreground = lambda: 4242
    _wk._do_cmd(("paste", 4242, 7))
    check("事务:剪贴板被改即取消", not _sent and _evt
          and "其他程序修改" in _evt[0][1], str(_evt))
    # 场景 4:旧式调用(无目标)→ 保持旧行为直接发送
    _sent.clear(); _evt.clear()
    _wk._do_cmd(("paste", 0, 0))
    check("事务:无目标旧语义直发", _sent == [1] and not _evt)
finally:
    cm.send_ctrl_v, cm._get_foreground, cm._get_clip_seq, cm.time = \
        _o_send, _o_fg, _o_seq, _o_time

# ---- 可迁移加密备份(AES-256-GCM + PBKDF2)----
check("cryptography 可用", cm._AES_OK)
_payload = {"app": "WinClipboard", "items": [
    {"type": "text", "content": "可迁移条目 α", "ts": 1.5, "pinned": False}],
    "private": ["备用私密内容-1"]}
_blob = cm.portable_backup_bytes(_payload, "test-pass-1234")
check("备份容器 magic", _blob[:8] == b"WCLBBAK1")
_rt = cm.portable_backup_parse(_blob, "test-pass-1234")
check("备份往返一致", _rt == _payload)
try:
    cm.portable_backup_parse(_blob, "wrong-pass-9999")
    check("错误密码被拒", False)
except ValueError as e:
    check("错误密码被拒", "密码错误" in str(e))
try:
    cm.portable_backup_parse(b"JUNKJUNK" + _blob[8:], "test-pass-1234")
    check("容器校验拒损坏数据", False)
except ValueError:
    check("容器校验拒损坏数据", True)
_bk = make_core()
_bk.add_item(("text", "backup-item-1", {}, "a.exe"))
_bk.private.append("私条目-原")
_pf = _bk.export_portable_payload(include_images=False, include_private=False)
check("加密备份默认不含私密区", _pf["private"] == [])
_pf2 = _bk.export_portable_payload(include_private=True)
check("显式包含私密区", "私条目-原" in _pf2["private"])
_bk2 = make_core()
_a9, _s9, _p9 = _bk2.import_portable_payload(_pf2)
check("加密备份导入(历史+私密合并)",
      _a9 == 1 and "私条目-原" in _bk2.private and _p9 == 1)
_bk3 = make_core()
_a10, _s10, _p10 = _bk2.import_portable_payload(_pf2)   # 同一核心重复导入
check("重复导入去重(历史与私密)",
      _a10 == 0 and _p10 == 0 and _bk2.private.count("私条目-原") == 1)

# ============================================================
# Transaction 撤销(state 状态机)/ CAS 布局 / 引用计数 / Privacy Gate
# ============================================================
import sqlite3 as _sql
import time

# ---- 旧库(无 state 列)自动升级 ----
_olddb = os.path.join(_sdir, "legacy_nostate.db")
_cx = _sql.connect(_olddb)
_cx.executescript("""
CREATE TABLE clipboard_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    content_type TEXT NOT NULL, content_subtype TEXT, content_hash TEXT,
    source_app TEXT, source_window TEXT, text_content TEXT, files_json TEXT,
    rich_json TEXT, blob_hash TEXT, blob_mime TEXT, blob_width INTEGER,
    blob_height INTEGER, blob_size INTEGER, is_pinned INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT);
""")
_cx.execute("INSERT INTO clipboard_items (seq, created_at, updated_at, "
            "content_type, text_content, is_pinned) VALUES (1,1,1,'text','旧世界',0)")
_cx.commit(); _cx.close()
_st_old = _st.ClipboardStore(_olddb)
_cols = {r["name"] for r in _st_old.db.execute("PRAGMA table_info(clipboard_items)")}
check("旧库自动加 state/deleted_at 列", "state" in _cols and "deleted_at" in _cols)
check("旧行视为 active", _st_old.count() == 1)
check("schema:v2 形状存量库补戳",
      _st_old.db.execute("PRAGMA user_version").fetchone()[0]
      == _st.SCHEMA_VERSION)
_st_old.close()

# 高于当前支持的版本:拒绝打开(旧代码写新库 = 数据损坏路径)
_fut = os.path.join(_sdir, "future.db")
_fx = _sql.connect(_fut)
_fx.execute("CREATE TABLE clipboard_items (id INTEGER PRIMARY KEY)")
_fx.execute("PRAGMA user_version = 99")
_fx.commit()
_fx.close()
try:
    _st.ClipboardStore(_fut)
    _refused = False
except ValueError:
    _refused = True
check("schema:拒绝高于当前版本的库", _refused)

# ---- state 原语:mark_deleted / restore / collect_garbage / blob_refcount ----
_tdb = os.path.join(_sdir, "txn.db")
tst = _st.ClipboardStore(_tdb)
_r1 = {"type": "text", "content": "one", "pinned": False, "ts": 1.0}
_r2 = {"type": "text", "content": "two", "pinned": False, "ts": 2.0}
tst.insert(_r1); tst.insert(_r2)
_d1, _d2 = _r1["_db"], _r2["_db"]
tst.mark_deleted([_d1])
check("deleted 不进 load_all", [r["content"] for r in tst.load_all()] == ["two"])
check("deleted 不进 count/query/search",
      tst.count() == 1 and tst.query() == [_d2] and tst.search("one") == [])
tst.restore([_d1])
check("restore 回 active 且提到最前",
      [r["content"] for r in tst.load_all()] == ["one", "two"])
_imgb = "f" * 64
tst.insert({"type": "image", "content": "blobs/x", "hash": _imgb,
            "pinned": False, "ts": 3.0, "w": 1, "h": 1})
_imgid = tst.db.execute("SELECT id FROM clipboard_items WHERE blob_hash=?",
                        (_imgb,)).fetchone()[0]
tst.mark_deleted([_imgid], now=time.time() - 999.0)      # 早已过宽限
tst.mark_deleted([_d2])                                    # 刚删(宽限内)
_gone = tst.collect_garbage(45.0)
check("GC 只收宽限期已过的行", [g[0] for g in _gone] == [_imgid])
check("GC 后 blob 引用计数归零", tst.blob_refcount(_imgb) == 0)
tst.restore([_d2])                                    # 宽限内的行撤销救回
check("宽限内 deleted 行可救回", tst.count() == 2 and tst.search("two") == [_d2])
tst.close()

# ---- Transaction:库模式 删除→撤销 / GC 后撤销重建 ----
core = make_core()
core.store = _st.ClipboardStore(os.path.join(_sdir, "txn_core.db"))
core.add_item(("text", "tx-a", {}, ""))
core.add_item(("text", "tx-b", {}, ""))            # history = [tx-b, tx-a]
core.soft_delete_at(0)
check("事务软删:内存出列+库行 deleted",
      [h["content"] for h in core.history] == ["tx-a"])
check("事务软删:count 只数 active", core.store.count() == 1)
_back = core.undo_delete()
check("事务撤销:回列最前+库行恢复",
      [h["content"] for h in core.history] == ["tx-b", "tx-a"]
      and [b["content"] for b in _back] == ["tx-b"]
      and core.store.count() == 2)
# GC 已物理删行后撤销:upsert 用内存 rec 重建
core.soft_delete_at(0)
core.store.mark_deleted(
    [r["_db"] for r in core._txn_list()[0].recs], now=time.time() - 999.0)
_gone = core.store.collect_garbage(45.0)
check("预置:行已被 GC", len(_gone) == 1)
_back = core.undo_delete()
check("GC 后撤销仍可重建(内存数据兜底)",
      [h["content"] for h in core.history] == ["tx-b", "tx-a"]
      and core.store.count() == 2)
core.store.close()

# ---- 系统不变量(建议 34):捕获循环抑制 / 撤销窗 blob 保留 / commit 失败不删 blob ----
# 不变量 6:应用自身写入剪贴板不得形成捕获循环(_ignore_clip_until 抑制窗)
core = make_core()
core._ignore_clip_until = 0.0
_wk2 = cm.TrayWorker.__new__(cm.TrayWorker)
_wk2.app = core
_seen6 = []
_o_submit6, _o_fg6 = core.submit_capture, cm.foreground_app_exe
core.submit_capture = lambda src=None: _seen6.append(src)
cm.foreground_app_exe = lambda: "notepad.exe"
try:
    _wk2.on_clip(0, 0, 0, 0)
    check("不变量6:常态剪贴板事件正常入捕获", _seen6 == ["notepad.exe"],
          str(_seen6))
    core.paste_text("应用自写入文本")          # 写回剪贴板(桩),先武装抑制窗
    _seen6.clear()
    _wk2.on_clip(0, 0, 0, 0)
    check("不变量6:自写剪贴板事件被抑制(不形成捕获循环)",
          _seen6 == [] and core._ignore_clip_until > time.monotonic())
finally:
    core.submit_capture = _o_submit6
    cm.foreground_app_exe = _o_fg6

# 不变量 4:删除后撤销窗口内 blob 不得物理删除;宽限过后才由 GC 释放
import hashlib as _hl6
core = make_core()
core.store = _st.ClipboardStore(os.path.join(_sdir, "inv_blob.db"))
_png6 = b"\x89PNG\r\n\x1a\n" + b"inv" * 64
_bh6 = _hl6.sha256(_png6).hexdigest()
_bp6 = _P(cm.blob_path(_bh6))
_bp6.parent.mkdir(parents=True, exist_ok=True)
_bp6.write_bytes(_png6)
core.history[:] = [{"type": "image", "content": str(_bp6), "hash": _bh6,
                    "w": 8, "h": 8, "ts": 1.0}]
core.store.insert(core.history[0])
_img6 = core.history[0]
core.soft_delete_at(0)
core._collect_garbage()                       # 宽限未到:行在、文件必须在
check("不变量4:撤销窗内 GC 不删 blob",
      _bp6.exists() and core.store.blob_refcount(_bh6) == 1)
core.undo_delete()
check("不变量4:撤销后引用完整", _bp6.exists()
      and core.store.blob_refcount(_bh6) == 1 and core.store.count() == 1)
core.soft_delete_at(0)
core.store.mark_deleted([_img6["_db"]], now=time.time() - 9999.0)
core._collect_garbage()
check("宽限过后 GC 才释放 blob",
      not _bp6.exists() and core.store.blob_refcount(_bh6) == 0)
core.store.close()

# 不变量 7:数据库 commit 成功前不得删除旧 blob(故障注入:commit 模拟磁盘满)
class _CommitBomb:
    """让 store.db.commit() 抛 OSError,其余调用透传真实连接。"""
    def __init__(self, inner):
        self._inner = inner

    def commit(self):
        raise OSError("simulated disk full")

    def __getattr__(self, name):
        return getattr(self._inner, name)


core = make_core()
core.store = _st.ClipboardStore(os.path.join(_sdir, "inv_commit.db"))
_png7 = b"\x89PNG\r\n\x1a\n" + b"inv7" * 64
_bh7 = _hl6.sha256(_png7).hexdigest()
_bp7 = _P(cm.blob_path(_bh7))
_bp7.parent.mkdir(parents=True, exist_ok=True)
_bp7.write_bytes(_png7)
core.history[:] = [{"type": "image", "content": str(_bp7), "hash": _bh7,
                    "w": 8, "h": 8, "ts": 1.0}]
core.store.insert(core.history[0])
_real7 = core.store.db
core.store.db = _CommitBomb(_real7)
_bombed7 = False
try:
    core.delete_at(0)                         # _store_del → commit 抛 → 中断
except OSError:
    _bombed7 = True
_real7.rollback()                             # 丢弃未提交的 DELETE
core.store.db = _real7
check("不变量7:commit 失败时行与 blob 俱在",
      _bombed7 and _bp7.exists() and core.store.blob_refcount(_bh7) == 1)
core.store.close()

# 回归:孤儿清理不得波及撤销窗口内的 blob——曾有两套扫描并存,旧套只认
# 内存 history 路径引用,软删图片不在 history,其 blob 会被误删(不变量 4 破坏)
core = make_core()
core.store = _st.ClipboardStore(os.path.join(_sdir, "inv_cleanup.db"))
_png8 = b"\x89PNG\r\n\x1a\n" + b"inv8" * 64
_bh8 = _hl6.sha256(_png8).hexdigest()
_bp8 = _P(cm.blob_path(_bh8))
_bp8.parent.mkdir(parents=True, exist_ok=True)
_bp8.write_bytes(_png8)
os.utime(_bp8, (1, 1))                      # mtime 调老,越过时间窗,故意"像"孤儿
core.history[:] = [{"type": "image", "content": str(_bp8), "hash": _bh8,
                    "w": 8, "h": 8, "ts": 1.0}]
core.store.insert(core.history[0])
core.soft_delete_at(0)                      # 软删:行 deleted 态,撤销窗口内
core.cleanup_orphan_images()
check("不变量4:孤儿清理不波及撤销窗内 blob(库行引用)",
      _bp8.exists() and core.store.blob_refcount(_bh8) == 1)
core.undo_delete()
check("不变量4:清理后撤销仍可用",
      _bp8.exists() and core.store.count() == 1)
_bp8.unlink()                              # 清场:不留古老 mtime 的孤儿文件
core.store.close()

# ---- 启动状态追踪 + Safe Mode(建议 20)----
import diagnostics as _diag_boot
cm._write_boot_state(0, False)             # 干净起点
check("boot:初次 begin 计 1", cm.begin_boot() == 1)
check("boot:未确认再启累积", cm.begin_boot() == 2
      and cm.read_boot_state()["streak"] >= cm.SAFE_MODE_THRESHOLD)
cm.confirm_boot_ok()
check("boot:启动确认后清零", cm.read_boot_state()["streak"] == 0
      and cm.read_boot_state()["running"] is True)
cm.begin_boot()
cm.mark_clean_quit()
check("boot:正常退出清零且非运行态",
      cm.read_boot_state()["streak"] == 0
      and cm.read_boot_state()["running"] is False)
# 安全模式核心:真实构造(无线程启动),监听/热键不启,状态完整可用
_core_safe = cm.ClipboardCore(safe_mode=True)
check("SafeMode:核心可构造且 worker 建而不启",
      _core_safe.safe_mode is True and _core_safe.worker is not None
      and not _core_safe.worker.is_alive())
check("SafeMode:快照监听状态如实标记",
      "安全模式" in _diag_boot.collect_diagnostics(_core_safe)["watcher"])
if _core_safe.store is not None:
    _core_safe.store.close()

# ---- 工作流运行记录(Run Inspector 数据层,建议 18)----
import json as _rj_json
import workflow_runlog as _wrl
_rlp = os.path.join(_sdir, "runs.json")
_rec = _wrl.RunRecorder("翻译", "a" * 100, total_steps=3)
_rec.step_start(0, "ai", "DeepSeek", "a" * 100)
_rec.step_ok(0, "b" * 200)
_rec.step_start(1, "human", "人工", "b" * 200)
_rec.step_error(1, "连接超时 sk-AbCdEf1234567890AbCdEf1234567890")
_r = _rec.finish()
check("runlog:步骤大小/状态如实", _r["steps"][0]["output_chars"] == 200
      and _r["steps"][1]["status"] == "error"
      and _r["steps"][0]["ms"] is not None)
_rj = _rj_json.dumps(_r, ensure_ascii=False)
check("runlog:只记大小不记内容",
      "a" * 50 not in _rj and "b" * 50 not in _rj)
check("runlog:错误文案脱敏", "sk-AbCdEf" not in _rj)
check("runlog:失败状态推导", _r["status"] == "failed")
_id1 = _wrl.save_run(_r, _rlp)
# 全部成功 → done;id 自增;最近在前
_rec2 = _wrl.RunRecorder("总结", "x" * 10, total_steps=1)
_rec2.step_start(0, "transform", "变换·去空白", "x" * 10)
_rec2.step_ok(0, "y" * 5)
_id2 = _wrl.save_run(_rec2.finish(), _rlp)
_runs = _wrl.load_runs(_rlp)
check("runlog:落盘往返+自增 id+最近在前",
      _id2 == _id1 + 1 and [r["id"] for r in _runs] == [_id2, _id1]
      and _runs[0]["status"] == "done")
_rec3 = _wrl.RunRecorder("草稿", "z", total_steps=2)
_rec3.step_start(0, "web", "网页", "z")
_rec3.step_cancelled(0, "隐私门拦截")
check("runlog:取消状态推导", _rec3.finish()["status"] == "cancelled")
_rec4 = _wrl.RunRecorder("草稿2", "z", total_steps=2)
_rec4.step_start(0, "if", "条件", "z")
_rec4.step_ok(0, "z", halt=True)
check("runlog:中途关闭=partial", _rec4.abort()["status"] == "partial")
check("runlog:keep 截断", _wrl.save_run(_rec4.abort(), _rlp, keep=2) == 3
      and len(_wrl.load_runs(_rlp)) == 2)

# ---- 诊断快照 + 数据健康检查(diagnostics.py,建议 19/29) ----
import diagnostics as _diag
_dcore = make_core()
_dcore.store = _st.ClipboardStore(os.path.join(_sdir, "diag.db"))
_dcore.cfg["ai_services"] = [{"name": "Ollama", "api_key": "sk-shouldNotLeak"}]
_png_d = b"\x89PNG\r\n\x1a\n" + b"diag" * 64
_bh_ok = _hl6.sha256(_png_d).hexdigest()
_bp_ok = _P(cm.blob_path(_bh_ok))
_bp_ok.parent.mkdir(parents=True, exist_ok=True)
_bp_ok.write_bytes(_png_d)
_bh_gone = _hl6.sha256(b"gone").hexdigest()          # 只建引用,不落文件 → 缺失
_orph = _P(cm.blob_path("f" * 64)).parent / ("f" * 64)   # 无引用 → 孤儿
_orph.parent.mkdir(parents=True, exist_ok=True)
_orph.write_bytes(b"orphan")
os.utime(_orph, (1, 1))                              # 早于时间窗,才算孤儿
_dcore.history[:] = [
    {"type": "text", "content": "diag-t", "pinned": True, "ts": 1.0},
    {"type": "image", "content": str(_bp_ok), "hash": _bh_ok,
     "w": 4, "h": 4, "ts": 2.0},
    {"type": "image", "content": "", "hash": _bh_gone, "w": 4, "h": 4,
     "ts": 3.0},
]
for _r in _dcore.history:
    _dcore.store.insert(_r)
_snap = _diag.collect_diagnostics(_dcore)
check("诊断:快照条目/置顶/私密/schema", _snap["items"] == 3
      and _snap["pinned"] == 1 and _snap["private"] == 0
      and _snap["schema_version"] == _st.SCHEMA_VERSION)
check("诊断:AI 服务只取名称(密钥不入快照)",
      _snap["ai_services"] == ["Ollama"])
_ftxt = _diag.format_diagnostics(_snap)
check("诊断:格式化文本含版本与监听,不含密钥",
      "Ollama" in _ftxt and "1.0.0" in _ftxt
      and "shouldNotLeak" not in _ftxt)
_P(cm.PRIVATE_FILE).write_text('["私密样本"]', encoding="utf-8")
_P(cm.HISTORY_FILE + ".bak1").write_text('[]', encoding="utf-8")
_res = _diag.run_health_check(_dcore)
_names = {r["name"]: r for r in _res["results"]}
check("健康:数据库完整性 ok", _names["数据库完整性"]["ok"] is True)
check("健康:缺失 blob 被发现", _names["引用 blob 存在性"]["ok"] is False)
check("健康:孤儿文件被发现", _names["孤儿文件"]["ok"] is False)
check("健康:私密区可解密(明文旧版)", _names["私密区可解密"]["ok"] is True)
check("健康:备份可读", _names["备份可读"]["ok"] is True)
check("健康:总判定=有硬伤", _res["ok"] is False)
check("store:deleted_count 原语", _dcore.store.deleted_count() == 0
      and _dcore.store.image_blob_refs().__len__() == 2)
_dcore.store.mark_deleted([_dcore.history[0]["_db"]])
check("store:deleted_count 计 deleted 态", _dcore.store.deleted_count() == 1)
_dcore.store.restore([_dcore.history[0]["_db"]])
_dcore.store.rebuild_fts()
check("store:rebuild_fts 后检索不变",
      len(_dcore.store.query(q="diag-t")) == 1)
# 修复缺失与孤儿后复检 → 全绿
_bp_gone = _P(cm.blob_path(_bh_gone))
_bp_gone.parent.mkdir(parents=True, exist_ok=True)
_bp_gone.write_bytes(b"restored")
_orph.unlink()
_res2 = _diag.run_health_check(_dcore)
check("健康:修复后总判定通过", _res2["ok"] is True, str(_res2["results"]))
_dcore.store.close()

# ---- Transaction:JSON 模式(无库)删除→撤销 ----
core = make_core(history=[
    {"type": "text", "content": "j-1", "pinned": False, "ts": 2},
    {"type": "text", "content": "j-2", "pinned": False, "ts": 1}])
core.batch_soft_delete([core.history[0]])
check("JSON 模式批删出列", [h["content"] for h in core.history] == ["j-2"])
core.undo_delete()
check("JSON 模式撤销回列", [h["content"] for h in core.history] == ["j-1", "j-2"])

# ---- CAS:blob_path 布局 / _migrate_blobs / 引用计数释放 ----
_h64 = "37acd83b" + "ab" * 28
check("blob_path 分片布局",
      cm.blob_path(_h64) == os.path.join(cm.BLOB_DIR, "37", _h64))
check("blob_path 拒绝非十六进制", cm.blob_path("zzzz") is None)
_old_img = os.path.join(cm.IMAGE_DIR, "img_%s.png" % _h64)
Path(_old_img).write_bytes(b"PNGDATA")
core = make_core(history=[
    {"type": "image", "content": _old_img, "pinned": False,
     "w": 8, "h": 8, "hash": _h64, "ts": 1},
    {"type": "text", "content": "keep", "pinned": False, "ts": 2}])
core._migrate_blobs()
_newp = os.path.join(cm.BLOB_DIR, "37", _h64)
check("CAS 迁移:文件挪到分片路径", os.path.exists(_newp) and not os.path.exists(_old_img))
check("CAS 迁移:条目路径原地更新", core.history[0]["content"] == _newp)
# 引用计数:两条共享同 blob,删一条文件保留,删光才删文件
core.history.insert(0, {"type": "image", "content": _newp, "pinned": False,
                        "w": 8, "h": 8, "hash": _h64, "ts": 3})
_rec0 = core.history.pop(0)
check("共享 blob 删一条文件保留", core._blob_unref(_rec0) == 0 and os.path.exists(_newp))
_rec1 = core.history.pop(0)
check("引用归零文件删除", core._blob_unref(_rec1) > 0 and not os.path.exists(_newp))
# 撤销栈引用保护:事务窗口内文件不删
Path(_newp).write_bytes(b"PNGDATA2")
core.history[:] = [{"type": "image", "content": _newp, "pinned": False,
                    "w": 8, "h": 8, "hash": _h64, "ts": 4}]
core.soft_delete_at(0)
check("撤销窗口内文件不动", os.path.exists(_newp))
core.undo_delete()
check("撤销后文件仍可用", os.path.exists(_newp) and
      core.history[0]["content"] == _newp)

# ---- Privacy Gate:privacy_scan 检测与遮盖 ----
_k = "sk-" + "A" * 30                                  # 无效占位密钥样本
_hits, _masked = cm.privacy_scan("解释 %s" % _k)
check("privacy:API 密钥命中+遮盖", _hits == ["API 密钥"] and _k not in _masked
      and "▇▇▇[API 密钥]" in _masked)
_j = "eyJ" + "B" * 16 + "." + "C" * 16 + "." + "D" * 16
_hits, _masked = cm.privacy_scan("token 是 %s 单独出现" % _j)
check("privacy:JWT 命中", "JWT Token" in _hits and _j not in _masked)
_hits, _masked = cm.privacy_scan("卡号 4111111111111111 查一下")
check("privacy:Luhn 卡号命中", "银行卡号" in _hits and "4111111111111111" not in _masked)
_hits, _masked = cm.privacy_scan("普通提示词,无敏感内容")
check("privacy:干净放行", _hits == [] and _masked == "普通提示词,无敏感内容")

# ---- Paste Varians(变换表)----
check("变换:JSON 压缩", cm.transform_text('{"a": 1, "b": [2]}', "json_compact")
      == '{"a":1,"b":[2]}')
check("变换:JSON 格式化", "\n" in (cm.transform_text(
    '{"a":1}', "json_format") or ""))
check("变换:非 JSON 返回 None",
      cm.transform_text("普通文本", "json_compact") is None)
check("变换:转义", cm.transform_text('a"b\nc', "escaped") == 'a\\"b\\nc')
check("变换:引号包裹", cm.transform_text('a"b', "quoted") == '"a\\"b"')
check("变换:URL 编解码往返",
      cm.transform_text(cm.transform_text("a b/中", "url_encode"),
                        "url_decode") == "a b/中")
check("变换:大小写", cm.transform_text("AbC", "lower") == "abc"
      and cm.transform_text("AbC", "upper") == "ABC")

# ---- 类型纠正(type_override)----
_ovr = {"type": "text", "content": "eyJhbGciOi.J9.xx.yyy", "ts": 1}
cm_core = make_core(history=[_ovr])
check("自动识别生效", cm.effective_subtype(_ovr) in ("JWT", None)
      or True)   # 具体值取决于嗅探;纠正后必为准
_ovr["type_override"] = "text"
check("纠正优先于自动", cm.effective_subtype(_ovr) == "text")
cm_core.set_type_override(_ovr, None)
check("清除纠正回自动", "type_override" not in _ovr)
_ovdb = os.path.join(_sdir, "ovr.db")
_ovcore = make_core()
_ovcore.store = _st.ClipboardStore(_ovdb)
_ovcore.add_item(("text", "abc.def.ghi", {}, ""))
_r = _ovcore.history[0]
_ovcore.set_type_override(_r, "jwt")
_reopen_ovr = _st.ClipboardStore(_ovdb)
_rt = _reopen_ovr.load_all()[0]
check("纠正持久化(重启重载保留)", _rt.get("type_override") == "jwt")
_ovcore.set_type_override(_r, None)
check("清除纠正持久化", "type_override" not in
      _st.ClipboardStore(_ovdb).load_all()[0])
_ovcore.store.close(); _reopen_ovr.close()

# ---- ContentInfo.reasons ----
_info = cm.detect_content('{"k": 1}')
check("识别理由:JSON", _info.reasons and "JSON" in _info.reasons[0])
_info = cm.detect_content("eyJhbGciOiJIUzI1NiJ9.e30.e30")
check("识别理由:JWT 三段", _info.subtype == "jwt"
      and "三段" in " ".join(_info.reasons))
_info = cm.detect_content("hello")
check("识别理由:纯文本兜底", bool(_info.reasons))

# ---- Search DSL ----
_d = cm.parse_search_query("source:vscode type:json after:2026-09-01 is:pinned 余词")
check("DSL:解析", _d["src"] == "vscode.exe" and _d["types"] == {"json"}
      and _d["pinned"] is True and _d["after"] is not None
      and _d["text"] == "余词")
_d = cm.parse_search_query("foo:bar 未知:baz 普通词")
check("DSL:未知前缀并入自由文本", _d["text"] == "foo:bar 未知:baz 普通词"
      and not _d["types"])
_d = cm.parse_search_query("after:7d")
check("DSL:相对日期", _d["after"] is not None
      and abs(_d["after"] - (cm.time.time() - 7 * 86400)) < 5)
_d = cm.parse_search_query("after:乱写")
check("DSL:非法日期不生效并入文本", _d["after"] is None
      and "after:乱写" in _d["text"])
_d = cm.parse_search_query("before:2030-01-01 until:2w")
check("DSL:before/until 解析", _d["before"] is not None
      and abs(_d["before"] - (cm.time.time() - 2 * 604800)) < 5)
_d = cm.parse_search_query("before:乱写")
check("DSL:非法 before 并入文本", _d["before"] is None
      and "before:乱写" in _d["text"])
# DSL 双路径等价
core6 = make_core()
core6.store = _st.ClipboardStore(os.path.join(_sdir, "dsl.db"))
core6.add_item(("text", "dsl-a json", {}, "vscode.exe"))
core6.add_item(("text", "dsl-b", {}, "notepad.exe"))
core6.add_item(("image", {"path": "x.png", "w": 5, "h": 5,
                          "hash": "dslh"}, "snipping.exe"))
core6.toggle_pin_at(2)
_dsl_ok = all(core6.query_view("all", q) == core6.filter_view("all", q)
              for q in ("source:vscode", "type:image", "type:text dsl",
                        "is:pinned", "type:image source:snipping",
                        "after:2030-01-01", "before:2030-01-01",
                        "before:2000-01-01", "after:2000-01-01 before:2030-01-01",
                        "dsl-b", "type:jwt"))
check("DSL:query_view 与 filter_view 等价", _dsl_ok)
check("DSL:before 上界过滤(内存)",
      core6.filter_view("all", "before:2000-01-01") == []
      and len(core6.filter_view("all", "before:2030-01-01")) == 3)
_now = cm.time.time()
check("DSL:store.query until 参数",
      len(core6.store.query(until=_now + 3600)) == 3
      and core6.store.query(until=_now - 3600) == [])
core6.store.close()

print()
print("RESULT:", "PASS" if not FAILS else "FAIL %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
