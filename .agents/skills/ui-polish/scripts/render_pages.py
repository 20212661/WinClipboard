# -*- coding: utf-8 -*-
"""WinClipboard UI 离屏渲染器(ui-polish skill 捆绑脚本)。

用法:
    python render_pages.py                    # 全部页面 × 浅色
    python render_pages.py dark               # 全部页面 × 深色
    python render_pages.py light history settings   # 指定页面
    python render_pages.py light history _after     # 文件名带后缀(前后对比)

页面名:history / services / settings / private
输出:%TEMP%\\wclb_ui_polish\\<页面>_<主题>[_after].png

数据全部写入临时目录,不碰真实历史与配置。离屏平台必须显式设字体,
否则中文渲染成豆腐块(主程序 main() 里有设,离屏脚本常忘)。
"""
import os
import sys
import json
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 离屏平台没有系统字体数据库,不指字体目录所有字符都渲染成占位框;
# 本机 FreeType 数据库还枚举不到雅黑(msyh.ttc 读不出),回退链含 SimHei
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")))

TMP = os.path.join(tempfile.gettempdir(), "wclb_ui_polish")
os.makedirs(TMP, exist_ok=True)

import clipboard_manager as cm   # noqa: E402

cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
cm.ICON_FILE = os.path.join(TMP, "icon.ico")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

from PySide6.QtWidgets import QApplication       # noqa: E402
from PySide6.QtGui import QFont, QPixmap, QIcon, QColor  # noqa: E402
from PySide6.QtCore import Qt, QObject           # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("_") else "light"
REST = [a for a in sys.argv[2:] if not a.startswith("_")] or \
       [a for a in sys.argv[1:] if not a.startswith("_") and a != MODE]
PAGES = REST or ["history", "services", "settings", "private"]
SUFFIX = next((a for a in sys.argv if a.startswith("_")), "")

app = QApplication([])
app.setQuitOnLastWindowClosed(False)

import winclipboard_qt as wq   # noqa: E402
from qfluentwidgets import setTheme, setThemeColor, Theme   # noqa: E402

setTheme(Theme.DARK if MODE == "dark" else Theme.LIGHT)
setThemeColor(QColor("#D97757"))   # 与主程序 apply_atelier_zero 一致(否则库默认青色)
wq.init_palette(MODE == "dark")
wq.apply_fonts(app)           # 中文回退字族(qfluentwidgets 字族表一并覆盖)


# ---- 素材 ----
def make_img(path, w, h, color):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 24):
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=2)
    img.save(path, "PNG")


IMG1 = os.path.join(cm.IMAGE_DIR, "polish1.png")
make_img(IMG1, 820, 500, (68, 128, 220))
TS = 1757040000
HISTORY = [
    {"type": "text", "content": "2026 年第三季度 OKR 进展\n华东区增长 23%,超额完成目标", "ts": TS},
    {"type": "image", "content": IMG1, "w": 820, "h": 500, "ts": TS - 300},
    {"type": "text", "content": "https://github.com/haoshangyang/WinClipboard", "ts": TS - 900},
    {"type": "text", "content": "pip install PySide6 PySide6-Fluent-Widgets pywin32 Pillow", "ts": TS - 172800},
    {"type": "text", "content": "会议纪要(置顶示例):周五 16:00 评审新版本 UI", "pinned": True, "ts": TS - 86400},
]
with open(cm.HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(HISTORY, f, ensure_ascii=False)
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
        {"kind": "web", "prompt": "{input}",
         "url": "https://chat.openai.com/?q={prompt}"},
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
    """最小应用替身:只实现各页面构造用到的接口。"""

    def __init__(self):
        super().__init__()
        self.icon = QIcon(IMG1)
        self._card_widgets = []
        self._badge_cache = {}
        self.private = ["银行卡 6222 **** **** 3456"]
        self.history = HISTORY
        self.cfg = CFG
        self.core = self

    def thumb_for(self, path):
        pm = QPixmap(path)
        if pm.isNull():
            return None
        return pm.scaled(*wq.Card.THUMB, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

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
fake._viewers = []
SIZE = (908, 620)

if "history" in PAGES:
    p = wq.HistoryPage(fake)
    p.resize(*SIZE)
    p.set_view(list(range(len(HISTORY))), HISTORY)
    p.select_index(0)
    p.show()
    app.processEvents()
    p.grab().save(os.path.join(TMP, "history_%s%s.png" % (MODE, SUFFIX)))
    print("saved history_%s%s.png" % (MODE, SUFFIX))

if "services" in PAGES:
    p = wq.ServicesPage(fake)
    p.resize(*SIZE)
    p.show()
    app.processEvents()
    p.grab().save(os.path.join(TMP, "services_%s%s.png" % (MODE, SUFFIX)))
    print("saved services_%s%s.png" % (MODE, SUFFIX))

if "settings" in PAGES:
    p = wq.SettingsPage(fake)
    p.resize(*SIZE)
    p.show()
    app.processEvents()
    p.grab().save(os.path.join(TMP, "settings_%s%s.png" % (MODE, SUFFIX)))
    print("saved settings_%s%s.png" % (MODE, SUFFIX))

if "workflow" in PAGES:
    p = wq.WorkflowPage(fake)
    p.resize(*SIZE)
    p.show()
    app.processEvents()
    p.grab().save(os.path.join(TMP, "workflow_%s%s.png" % (MODE, SUFFIX)))
    print("saved workflow_%s%s.png" % (MODE, SUFFIX))

if "private" in PAGES:
    p = wq.PrivatePage(fake)
    p.resize(*SIZE)
    p.show()
    app.processEvents()
    p.grab().save(os.path.join(TMP, "private_%s%s.png" % (MODE, SUFFIX)))
    print("saved private_%s%s.png" % (MODE, SUFFIX))

if "viewer" in PAGES:
    VS = (960, 640)
    for tag, rec in [
        ("viewer", {"type": "text", "content":
         "import json\n\n\ndef load(path):\n    \"\"\"读取配置\"\"\"\n    with open(path, encoding=\"utf-8\") as f:\n"
         "        return json.load(f)\n\n\nif __name__ == \"__main__\":\n    print(load(\"config.json\"))",
         "ts": TS - 600}),
        ("viewer_files", {"type": "files", "content":
         ["C:/Users/demo/Desktop/报告.docx", "C:/Users/demo/Pictures/截图 0912.png",
          "C:/Users/demo/Downloads/发票.pdf"], "ts": TS - 1200}),
        ("viewer_json", {"type": "text", "content":
         '{"name":"WinClipboard","version":3,"features":["history","viewer",'
         '"workflow"],"meta":{"stars":128,"oss":true}}', "ts": TS - 1800}),
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
        app.processEvents()
        w.grab().save(os.path.join(TMP, "%s_%s%s.png" % (tag, MODE, SUFFIX)))
        print("saved %s_%s%s.png" % (tag, MODE, SUFFIX))
        w.close()
        app.processEvents()

print("OUTDIR", os.path.realpath(TMP))
