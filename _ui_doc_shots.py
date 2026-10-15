# -*- coding: utf-8 -*-
"""为 UI 说明文档生成全套离屏截图(开发用,不碰真实数据/剪贴板/热键)。

用法:
    python _ui_doc_shots.py light
    python _ui_doc_shots.py dark

输出 docs/ui/2026-10-15/<名称>_<主题>.png:
    main_history / main_workflow / main_private / main_settings(滚至 AI 模型与服务区)
    quickpaste / viewer_code / viewer_json / viewer_md / viewer_csv
    viewer_files / viewer_image / viewer_diff / quick_ai / wheel

面板/编辑器/图钉画布截图由 _ui_preview.py 生成(它自带 17 项断言)。
"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path as _Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MODE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("light", "dark") else "light"
OUT = os.path.join(HERE, "docs", "ui", "2026-10-15")
os.makedirs(OUT, exist_ok=True)

TMP = os.path.join(tempfile.gettempdir(), "wclb_ui_doc")
os.makedirs(TMP, exist_ok=True)

import clipboard_manager as cm   # noqa: E402

cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.DB_FILE = os.path.join(TMP, "clipboard.db")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
cm.ICON_FILE = os.path.join(TMP, "icon.ico")
# 上下文包路径在 cm 导入时已按原 APP_DIR 固化:显式重定向到隔离目录,
# 避免托盘 1200ms 定时刷新把真实用户的上下文包装进文档截图
cm.CTXPACK_FILE = os.path.join(TMP, "context_packs.json")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

from PySide6.QtWidgets import QApplication   # noqa: E402
from PySide6.QtGui import QPixmap, QIcon, QColor  # noqa: E402
from PySide6.QtCore import Qt, QObject       # noqa: E402

app = QApplication([])
app.setQuitOnLastWindowClosed(False)

import winclipboard_qt as wq                 # noqa: E402
from qfluentwidgets import setTheme, setThemeColor, Theme   # noqa: E402

setTheme(Theme.DARK if MODE == "dark" else Theme.LIGHT)
setThemeColor(QColor("#D97757"))   # Claude 陶土橙(qfw 默认是青色,必须显式覆盖)
wq.init_palette(MODE == "dark")
wq.apply_fonts(app)


# ---- 演示素材 ----
def make_img(path, w, h, color):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 24):
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=2)
    for x in range(0, w, 40):
        d.line([(x, 0), (x, h)], fill=(255, 255, 255), width=1)
    img.save(path, "PNG")


IMG1 = os.path.join(cm.IMAGE_DIR, "doc1.png")
IMG2 = os.path.join(cm.IMAGE_DIR, "doc2.png")
make_img(IMG1, 820, 500, (68, 128, 220))
make_img(IMG2, 640, 420, (222, 128, 88))
FILE1 = os.path.join(TMP, "年度总结.docx")
FILE2 = os.path.join(TMP, "数据表.xlsx")
for p in (FILE1, FILE2):
    _Path(p).write_bytes(b"x" * 20480)

TS = 1757040000
HISTORY = [
    {"type": "text", "content":
     "2026 年第三季度 OKR 进展\n华东区增长 23%,超额完成目标\n华南区持平,下周重点跟进渠道合作",
     "src": "WeChat.exe", "ts": TS},
    {"type": "image", "content": IMG1, "w": 820, "h": 500, "src": "SnippingTool.exe",
     "ts": TS - 300},
    {"type": "text", "content":
     '{"name":"WinClipboard","version":3,"features":["history","viewer","workflow"],'
     '"meta":{"stars":128,"oss":true}}', "src": "chrome.exe", "ts": TS - 900},
    {"type": "files", "content": [FILE1, FILE2], "src": "explorer.exe",
     "ts": TS - 3600},
    {"type": "text", "content": "会议纪要(置顶示例):周五 16:00 评审新版本 UI",
     "pinned": True, "ts": TS - 86400},
    {"type": "text", "content":
     "pip install PySide6 PySide6-Fluent-Widgets pywin32 Pillow",
     "src": "WindowsTerminal.exe", "ts": TS - 172800},
    {"type": "image", "content": IMG2, "w": 640, "h": 420, "src": "mspaint.exe",
     "ts": TS - 259200},
]
CFG = {"ai_services": [
    {"name": "GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash", "api_key": "sk-demo"},
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat", "api_key": ""},
], "ai_workflows": [
    {"name": "翻译 → 润色", "steps": [
        {"kind": "ai", "service": "GLM", "prompt": "把下面的内容翻译成英文:\n{input}"},
        {"kind": "ai", "service": "DeepSeek", "prompt": "润色以下英文,使其更自然:\n{previous}"},
        {"kind": "human", "prompt": "确认译文后继续"},
    ]},
    {"name": "网页问答", "steps": [
        {"kind": "web", "prompt": "{input}", "url": "https://chat.openai.com/?q={prompt}"},
        {"kind": "human", "prompt": "粘贴网页回答后继续"},
        {"kind": "clipboard"},
    ]},
    {"name": "会议纪要整理", "steps": [
        {"kind": "transform", "op": "trim"},
        {"kind": "ai", "service": "GLM", "prompt": "把纪要整理成要点列表:\n{input}"},
        {"kind": "if", "pattern": "^- "},
    ]},
]}


class FakeApp(QObject):
    """最小应用替身:实现各页面/窗口构造与渲染用到的接口。"""

    def __init__(self):
        super().__init__()
        self.icon = QIcon(IMG1)
        self._card_widgets = []
        self._badge_cache = {}
        self._viewers = []
        self._view = list(range(len(HISTORY)))
        self.selected = 0
        self.private = ["银行卡 6222 0210 0412 3456",
                        "API Key sk-proj-9d8f7g6h5j4k3l2m1n0"]
        self.history = HISTORY
        self.cfg = CFG
        self.core = self

    def query_view(self, cat="all", q="", limit=None):
        view = [i for i, r in enumerate(self.history)
                if cat == "all" or r.get("type") == cat]
        if q:
            view = [i for i in view if q.lower()
                    in str(self.history[i].get("content", "")).lower()]
        return view[:limit] if limit else view

    def thumb_for(self, path):
        pm = QPixmap(path)
        if pm.isNull():
            return None
        return pm.scaled(*wq.Card.THUMB, Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation)

    def thumb_cached(self, path):
        return self.thumb_for(path)

    def is_autostart(self):
        return False

    def request_thumb(self, path):
        pass

    def card_action(self, act, card):
        pass

    def select_card(self, card):
        pass

    def on_search(self, text):
        pass

    def set_category(self, cat):
        pass

    def show_ai_hub(self):
        pass

    def paste_selected(self):
        pass

    def move_sel(self, delta):
        pass

    def open_files(self, rec):
        pass

    def reveal_first(self, rec):
        pass

    def paste_text(self, text):
        pass

    def show_shortcut_help(self):
        pass

    def show_services(self):
        pass


fake = FakeApp()

# 种子演示上下文包(隔离目录):让右栏上下文篮在主界面截图里呈现真实状态
from ctxpack.service import ContextPackService   # noqa: E402

_ctx_svc = ContextPackService(cm.CTXPACK_FILE).load()
if not _ctx_svc.packs:
    _ctx_svc.create("示例包 · 项目资料")
    _ctx_svc.add_snapshot("text", "def add(a, b):\n    return a + b\n",
                          title="add 函数", captured_at=TS)
    _ctx_svc.add_snapshot(
        "text", "上下文包:把零散资料按需勾选、拼装成一段可直接发给 AI 的上下文。",
        title="上下文包说明", captured_at=TS - 60)


def pump(seconds=1.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def shot(w, name, settle=0.6):
    pump(settle)
    path = os.path.join(OUT, "%s_%s.png" % (name, MODE))
    w.grab().save(path)
    print("saved", os.path.basename(path))


# ---- 1) 主窗口(FluentWindow 导航壳)五页 ----
win = wq.MainWindow(fake)
win.resize(1000, 680)
win.show()
app.processEvents()
win.history_page.set_view(list(range(len(HISTORY))), HISTORY)
win.history_page.lbl_count.setText("%d 个项目" % len(HISTORY))   # 真实 app 的 refresh 职责
win.history_page.select_index(0)
win.switchTo(win.history_page)
shot(win, "main_history", settle=2.0)

win.switchTo(win.workflow_page)
shot(win, "main_workflow")

# AI 服务已并入设置页:设置页截图滚动到「AI 模型与服务」区块再拍
win.switchTo(win.settings_page)
win.settings_page.scroll.ensureWidgetVisible(win.services_page)
shot(win, "main_settings")

win.switchTo(win.private_page)
shot(win, "main_private")

# ---- 2) 快捷粘贴浮层(Alt+`) ----
from quickpaste import QuickPasteWindow   # noqa: E402

qp = QuickPasteWindow(fake)
qp.resize(560, 420)
qp.show()
qp.refresh()
app.processEvents()
qp.move_sel(1)
shot(qp, "quickpaste")
qp.hide()

# ---- 3) 内容查看器:九视图中的代表 ----
VS = (960, 640)
for tag, rec in [
    ("viewer_code", {"type": "text", "content":
     "import json\n\n\ndef load(path):\n    \"\"\"读取配置\"\"\"\n"
     "    with open(path, encoding=\"utf-8\") as f:\n        return json.load(f)\n\n\n"
     "if __name__ == \"__main__\":\n    print(load(\"config.json\"))", "ts": TS - 600}),
    ("viewer_files", {"type": "files", "content":
     ["C:/Users/demo/Desktop/报告.docx", "C:/Users/demo/Pictures/截图 0912.png",
      "C:/Users/demo/Downloads/发票.pdf"], "ts": TS - 1200}),
    ("viewer_md", {"type": "text", "content":
     "# 发布说明\n\n## 2.3 新特性\n\n- 全新 **内容查看器**:自动识别格式\n"
     "- JSON 结构树 / Markdown 预览 / CSV 表格\n- 支持浮动查找与字号缩放\n\n"
     "> 引用:像代码阅读器一样阅读剪贴板。\n", "ts": TS - 2400}),
    ("viewer_csv", {"type": "text", "content":
     "区域,销售额,同比,负责人\n华东,1280,23%,张三\n华南,960,11%,李四\n"
     "华北,842,7%,王五\n西南,415,19%,赵六\n", "ts": TS - 3000}),
]:
    w = wq.ViewerWindow(fake, rec)
    w.resize(*VS)
    w.show()
    shot(w, tag)
    w.close()
    app.processEvents()

# JSON 查看器:等结构树懒加载展开
wv = wq.ViewerWindow(fake, {"type": "text", "content":
     '{"name":"WinClipboard","version":3,"features":["history","viewer",'
     '"workflow"],"meta":{"stars":128,"oss":true}}', "ts": TS - 1800})
wv.resize(*VS)
wv.show()
pump(1.5)
wv.grab().save(os.path.join(OUT, "viewer_json_%s.png" % MODE))
print("saved viewer_json_%s.png" % MODE)
wv.close()
app.processEvents()

# 图片查看器
iv = wq.ImageViewerWindow(fake, {"type": "image", "content": IMG1,
                                 "w": 820, "h": 500, "ts": TS - 400})
iv.resize(*VS)
iv.show()
shot(iv, "viewer_image")

# Diff 对比窗口
dw = wq.DiffWindow(fake,
                   {"type": "text", "content":
                    "v1:捕获文本与图片\n支持置顶与搜索\n导出 JSON",
                    "ts": TS - 5000},
                   {"type": "text", "content":
                    "v2:捕获文本/图片/文件/富文本\n支持置顶/搜索/批量删除/撤销\n导出 JSON 与 Markdown",
                    "ts": TS - 4600})
dw.resize(*VS)
dw.show()
shot(dw, "viewer_diff")

# ---- 4) 快速 AI:结果浮窗与动作轮盘 ----
import ai_quick as qa                              # noqa: E402

qaw = qa.QuickAIWindow(fake)
qaw._snapshot = "2026 年第三季度 OKR 进展:华东区增长 23%,超额完成目标;华南区持平。"
qaw.select_action("explain")
qaw._shutdown = True        # 只为截图:拦掉 select_action 排的 0ms 自动发送
qaw._answer = ("华东区已完成季度增长目标(23%),渠道合作是当前重点;"
               "华南区持平,建议下周跟进。")
qaw.output.setPlainText(qaw._answer)
qaw._sync_buttons()
shot(qaw, "quick_ai")
qaw.wheel._hover = 0
pump(0.3)
qaw.wheel.grab().save(os.path.join(OUT, "wheel_%s.png" % MODE))
print("saved wheel_%s.png" % MODE)
qaw.shutdown()

print("OUTDIR", os.path.realpath(OUT))
