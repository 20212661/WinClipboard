# -*- coding: utf-8 -*-
"""离屏渲染 UI 预览(开发用,不影响真实数据)。

运行:
    python _ui_preview.py            # 浅色主题
    python _ui_preview.py dark       # 深色主题
    python _ui_preview.py baseline   # 改版前基线(不经过新调色板)

输出 %TEMP%\\wclb_ui_preview\\ 下的 PNG:panel / private / editor。
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 隔离持久化路径(同 _core_test) ----
import clipboard_manager as cm

TMP = os.path.join(tempfile.gettempdir(), "wclb_ui_preview")
os.makedirs(TMP, exist_ok=True)
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtCore import Qt, QObject

MODE = sys.argv[1] if len(sys.argv) > 1 else "light"

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

import winclipboard_qt as wq

if MODE != "baseline":
    wq.init_palette(MODE == "dark")
    from qfluentwidgets import setTheme, Theme
    setTheme(Theme.DARK if MODE == "dark" else Theme.LIGHT)
    wq.setThemeColor(wq.ACCENT)


# ---- 测试素材 ----
def make_img(path, w, h, color):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 24):
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=2)
    for x in range(0, w, 40):
        d.line([(x, 0), (x, h)], fill=(255, 255, 255), width=1)
    img.save(path, "PNG")


IMG1 = os.path.join(cm.IMAGE_DIR, "shot1.png")
IMG2 = os.path.join(cm.IMAGE_DIR, "shot2.png")
make_img(IMG1, 820, 500, (68, 128, 220))
make_img(IMG2, 640, 420, (222, 128, 88))
FILE1 = os.path.join(TMP, "年度总结.docx")
FILE2 = os.path.join(TMP, "数据表.xlsx")
for p in (FILE1, FILE2):
    with open(p, "wb") as f:
        f.write(b"x" * 20480)

TS = 1757040000
HISTORY = [
    {"type": "text", "content": "2026 年第三季度 OKR 进展\n华东区增长 23%,超额完成目标\n华南区持平,下周重点跟进渠道合作",
     "ts": TS},
    {"type": "image", "content": IMG1, "w": 820, "h": 500, "ts": TS - 300},
    {"type": "text", "content": "https://github.com/haoshangyang/WinClipboard", "ts": TS - 900},
    {"type": "files", "content": [FILE1, FILE2], "ts": TS - 3600},
    {"type": "text", "content": "会议纪要(固定置顶示例):周五 16:00 评审新版本 UI", "pinned": True, "ts": TS - 86400},
    {"type": "text", "content": "pip install PySide6 PySide6-Fluent-Widgets pywin32 Pillow", "ts": TS - 172800},
    {"type": "image", "content": IMG2, "w": 640, "h": 420, "ts": TS - 259200},
]


class FakeApp(QObject):
    """最小化的应用替身:只实现 UI 构造/渲染用到的接口。"""

    def __init__(self):
        super().__init__()
        self.icon = QIcon(IMG1)
        self._card_widgets = []
        self.private = ["银行卡 6222 0210 0412 3456", "API Key sk-proj-9d8f7g6h5j4k3l2m1n0"]
        self.history = HISTORY
        self.core = self

    def thumb_for(self, path):
        pm = QPixmap(path)
        if pm.isNull():
            return None
        return pm.scaled(*wq.Card.THUMB, Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation)

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


def composite(pm, out):
    """把半透明面板合成到桌面色背景上,便于查看圆角与投影。"""
    from PySide6.QtGui import QImage, QPainter, QColor, QLinearGradient
    base = QImage(pm.width() + 40, pm.height() + 40, QImage.Format_ARGB32)
    g = QLinearGradient(0, 0, 0, base.height())
    g.setColorAt(0, QColor("#9DBBD4"))
    g.setColorAt(1, QColor("#6E8FAE"))
    p = QPainter(base)
    p.fillRect(base.rect(), g)
    p.drawPixmap(20, 20, pm)
    p.end()
    base.save(out)
    print("saved", out)


fake = FakeApp()

panel = wq.PanelWindow(fake)
panel.set_view(list(range(len(HISTORY))), HISTORY)
panel.select_index(0)
panel._resize_to_content()
panel.show()
app.processEvents()
if os.environ.get("WCLB_DEBUG_GEOM"):
    def _g(w, name):
        gg = w.geometry()
        print("GEOM %-12s x=%d y=%d w=%d h=%d" % (name, gg.x(), gg.y(), gg.width(), gg.height()))
    _g(panel, "panel-win")
    _g(panel.panel, "panel")
    _g(panel.search, "search")
    _g(panel.nav, "nav")
    _g(panel.scroll, "scroll")
    _g(panel.content, "content")
    for i, c in enumerate(panel._cards):
        _g(c, "card%d" % i)
composite(panel.grab(), os.path.join(TMP, "panel_%s.png" % MODE))

pw = wq.PrivateWindow(fake)
pw.show()
app.processEvents()
pw.grab().save(os.path.join(TMP, "private_%s.png" % MODE))
print("saved", os.path.join(TMP, "private_%s.png" % MODE))

ed = wq.ImageEditor(fake, {"type": "image", "content": IMG1})
ed.show()
app.processEvents()
ed.grab().save(os.path.join(TMP, "editor_%s.png" % MODE))
print("saved", os.path.join(TMP, "editor_%s.png" % MODE))
