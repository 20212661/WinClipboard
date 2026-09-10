# -*- coding: utf-8 -*-
"""Qt 前端集成自检(离屏;真实 QApplication + WinClipboardApp + ClipboardCore)。

运行: python _integration_test.py   退出码 0 = 全部通过

与 _ai_test.py 的区别:那边用 FakeApp 替身只测工作流窗口;这里构建**真实的
WinClipboardApp**(真实核心 + 真实面板 + 真实定时器),因此能覆盖:

  1. 真实启动路径的密钥迁移(明文 config → 磁盘无明文,内存仍可用)
  2. 剪贴捕获 → signal_q("change") → 防抖刷新 → 卡片重建的完整链路
  3. 缩略图生成、分类筛选、搜索、置顶、删除
  4. 服务窗口 / 工作流运行台的真实构造与人工步骤
  5. 运行期 app.log 无异常

数据写入临时目录,不影响真实历史与配置。
"""
import os
import sys
import json
import time
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_integ_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
cm.ICON_FILE = os.path.join(TMP, "icon.ico")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)
# 预置明文密钥,验证真实启动路径下的升级
SECRET = "sk-live-SECRET-1234567890"
with open(cm.CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump({"ai_services": [{"name": "GLM", "base_url": "https://x/v1",
                                "model": "m", "api_key": SECRET}]}, f)

from PIL import Image
from PySide6.QtWidgets import QApplication

app = QApplication([])
app.setQuitOnLastWindowClosed(False)

import winclipboard_qt as wq
from qfluentwidgets import setTheme, Theme

FAKE_PNG = os.path.join(cm.IMAGE_DIR, "img_test.png")

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def pump(seconds=1.2):
    """跑 Qt 事件循环,让 30ms 轮询与 180ms 刷新防抖都走完。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


dark = wq.resolve_theme(cm.load_config())
wq.init_palette(dark)
setTheme(Theme.DARK if dark else Theme.LIGHT)
wq.setThemeColor(wq.ACCENT)

a = wq.WinClipboardApp(app)          # 真实核心 + 真实面板
check("真实 app 构造", a.core is not None and a.panel is not None)
check("启动已升级密钥(磁盘无明文)",
      SECRET not in open(cm.CONFIG_FILE, encoding="utf-8").read())
check("运行时密钥可用", a.cfg["ai_services"][0]["api_key"] == SECRET)

# 面板可见时:捕获 → 必须经 signal_q("change") 走到防抖刷新
a.panel.show_panel()
app.processEvents()
a.refresh_view()
# 注:图片须在 core 启动后再生成 —— _migrate_images 启动时会清理未被任何
# 历史条目引用的孤儿 PNG(该行为本身是对的)
Image.new("RGB", (40, 30), (10, 120, 220)).save(FAKE_PNG)
# TrayWorker 启动时会捕获「当前剪贴板」并投递一条,先排空再取基线
pump()
check("启动捕获已排空", a.signal_q.empty(), str(list(a.signal_q.queue)))
before = len(a.history)
print("      (启动基线 %d 条,含 TrayWorker 捕获的当前剪贴板)" % before)
a.signal_q.put(("clip", ("text", "集成自检条目")))
a.signal_q.put(("clip", ("image", {"path": FAKE_PNG, "w": 40, "h": 30, "hash": "h1"})))
a.signal_q.put(("clip", ("files", [__file__])))
pump()

check("三条捕获入史", len(a.history) == before + 3,
      "before=%d after=%d" % (before, len(a.history)))
check("change 信号已消费", a.signal_q.empty())
check("卡片已重建", len(a._card_widgets) == len(a.history),
      "%d vs %d" % (len(a._card_widgets), len(a.history)))
check("缩略图生成", a.thumb_for(FAKE_PNG) is not None)

# 分类 / 搜索 / 选择 / 删除 / 置顶(走真实核心)
a.set_category("image")
check("分类筛选", all(a.history[i]["type"] == "image" for i in a._view), str(a._view))
a.set_category("all")
a.on_search("集成自检")
pump(0.5)
check("搜索命中", len(a._view) == 1, str(a._view))
a.selected = 0
a.toggle_pin()
check("置顶生效", any(h.get("pinned") for h in a.history))
a.selected = 0
a.delete_selected()
check("删除生效", not any(h.get("pinned") for h in a.history))

# 服务窗口 / 工作流运行台真实构造
sw = wq.ServicesWindow(a)
check("ServicesWindow 真实构造", sw.lst.count() == 1)
wf = {"name": "自检流程", "steps": [{"kind": "human", "prompt": "{input}"}]}
runner = wq.WorkflowRunner(a, wf, "种子")
a._ai_runners.append(runner)
runner._run_step()
app.processEvents()
check("运行台等待面板", runner._waiting is not None)
runner._wait_body.setPlainText("人工确认内容")
runner._wait_confirm()
app.processEvents()
check("运行台完成", runner.step_idx == 1 and runner.results == ["人工确认内容"])
runner.close()
sw.hide()

a.quit_app()
pump(0.2)

log = open(cm.LOG_FILE, encoding="utf-8").read()
bad = [l for l in log.splitlines() if "Traceback" in l or "失败" in l or "异常" in l]
check("运行期无异常日志", not bad, str(bad[:3]))

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
