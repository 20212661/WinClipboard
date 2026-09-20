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
from pathlib import Path as _Path

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
    from qfluentwidgets import setTheme, Theme
    setTheme(Theme.DARK if MODE == "dark" else Theme.LIGHT)
    wq.init_palette(MODE == "dark")


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
    _Path(p).write_bytes(b"x" * 20480)

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

    def thumb_cached(self, path):
        # 预览替身:直接同步返回缩略图(真实 app 查缓存+转后台线程,离屏渲染用不上)
        return self.thumb_for(path)

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

panel = wq.HistoryPage(fake)
panel.resize(560, 720)
panel.set_view(list(range(len(HISTORY))), HISTORY)
panel.select_index(0)
panel.show()
app.processEvents()
if os.environ.get("WCLB_DEBUG_GEOM"):
    def _g(w, name):
        gg = w.geometry()
        print("GEOM %-12s x=%d y=%d w=%d h=%d" % (name, gg.x(), gg.y(), gg.width(), gg.height()))
    _g(panel, "panel-win")
    _g(panel.search, "search")
    _g(panel.nav, "nav")
    _g(panel.scroll, "scroll")
    _g(panel.content, "content")
    for i, c in enumerate(panel._cards):
        _g(c, "card%d" % i)
composite(panel.grab(), os.path.join(TMP, "panel_%s.png" % MODE))

pw = wq.PrivatePage(fake)
pw.resize(560, 480)
pw.show()
app.processEvents()
pw.grab().save(os.path.join(TMP, "private_%s.png" % MODE))
print("saved", os.path.join(TMP, "private_%s.png" % MODE))

ed = wq.ImageEditor(fake, {"type": "image", "content": IMG1})
ed.show()
app.processEvents()
ed.grab().save(os.path.join(TMP, "editor_%s.png" % MODE))
print("saved", os.path.join(TMP, "editor_%s.png" % MODE))

# ---- 编辑器(QQ 风格工具条)几何断言 + 模拟标注 ----
from PySide6.QtGui import QMouseEvent                          # noqa: E402
from PySide6.QtCore import QEvent as _QE, QPointF as _QPointF  # noqa: E402
from qfluentwidgets import PrimaryToolButton as _PTB           # noqa: E402


def _ok(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    assert cond, name


bar = ed._bar
_ok("工具条为窗口子件且不越界",
    bar.parent() is ed and bar.geometry().bottom() <= ed.height()
    and bar.geometry().right() <= ed.width())
_ok("工具条底部居中",
    abs(bar.geometry().center().x() - ed.width() / 2) <= 2
    and bar.geometry().bottom() >= ed.height() - 60,
    str(bar.geometry()))
_ok("工具八件套齐全(含选择)",
    set(ed._tbtns) == {"select", "crop", "rect", "ellipse", "arrow",
                       "pen", "text", "mosaic"}, str(set(ed._tbtns)))
_ok("完成按钮为主题色主按钮", isinstance(ed._btn_done, _PTB))
_ok("状态角标不挡图(单行高度+鼠标穿透)",
    ed.lbl_status.height() <= 60
    and ed.lbl_status.testAttribute(Qt.WA_TransparentForMouseEvents),
    "%dx%d" % (ed.lbl_status.width(), ed.lbl_status.height()))
_gi = ed.grab().toImage()
# 源图自带白色网格线,单点采样会踩线:邻域 9 点蓝色占多数才算无遮挡
_blues = 0
for _dx in (-12, 0, 12):
    for _dy in (-12, 0, 12):
        _c = _gi.pixelColor(_gi.width() // 2 + _dx,
                            int(_gi.height() * 0.4) + _dy)
        if (_c.red(), _c.green(), _c.blue()) == (68, 128, 220):
            _blues += 1
_ok("图像区无遮挡(邻域蓝色占多数)", _blues >= 5, "blues=%d/9" % _blues)
_bf = ed._tbtns["rect"].grab().toImage()
ed._on_tool("rect")
app.processEvents()
_af = ed._tbtns["rect"].grab().toImage()
_df = sum(1 for _y in range(_bf.height()) for _x in range(_bf.width())
          if _bf.pixelColor(_x, _y) != _af.pixelColor(_x, _y))
_ok("工具选中态可见", _df > 100, "diff=%d" % _df)


def _mev(tp, x, y):
    return QMouseEvent(tp, _QPointF(x, y), _QPointF(x, y),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


center = ed.view.viewport().rect().center()
ed._press(_mev(_QE.MouseButtonPress, center.x() - 40, center.y() - 20))
ed._move(_mev(_QE.MouseMove, center.x() - 40, center.y() - 20))
ed._move(_mev(_QE.MouseMove, center.x() + 40, center.y() + 20))
app.processEvents()
_ok("拖动中出现形状预览", ed._preview is not None)
ed._release(_mev(_QE.MouseButtonRelease, center.x() + 40, center.y() + 20))
_ok("标注成为矢量项(可编辑)", len(ed.annos) == 1
    and isinstance(ed.annos[0], wq.AnnoRect), str(type(ed.annos[0]) if ed.annos else None))
_ok("底图未被栅格化污染", not any(
    ed.base.getpixel((x, y)) != (68, 128, 220)
    for x in range(0, ed.base.width, 7) for y in range(0, ed.base.height, 7)
    if y % 24 > 3 and x % 40 > 1))
_final = ed._final_image()
_ok("导出合成图含标注(红)", any(
    _final.getpixel((x, y)) in ((255, 64, 64), (253, 63, 63))
    for x in range(0, _final.width, 3) for y in range(0, _final.height, 3)))
_ok("QQ式:工具保持激活可连续标注", ed.tool == "rect")

# 选择模式:选中 → 移动 → 撤销
ed._on_tool("select")
_it = ed.annos[0]
_it.setSelected(True)
_p0 = _it.pos()
_it.setPos(_p0.x() + 60, _p0.y() + 40)
ed._record({"type": "move", "item": _it, "pos": _p0})
_ok("标注可拖动(位置已变)", _it.pos() != _p0)
ed._on_undo()
_ok("撤销 move 复位", _it.pos() == _p0)
ed._on_undo()                     # 撤销 add
_ok("撤销 add 移除标注", len(ed.annos) == 0)

# 画笔 + 椭圆 + 文字管线
ed._on_tool("pen")
p0 = ed.view.viewport().rect().center()
ed._press(_mev(_QE.MouseButtonPress, p0.x() - 30, p0.y()))
for dx in (-20, 0, 20, 40):
    ed._move(_mev(_QE.MouseMove, p0.x() + dx, p0.y() + (dx // 4) * 3))
ed._release(_mev(_QE.MouseButtonRelease, p0.x() + 40, p0.y() + 30))
ed._on_tool("ellipse")
ed._press(_mev(_QE.MouseButtonPress, center.x() - 50, center.y() - 30))
ed._move(_mev(_QE.MouseMove, center.x() + 50, center.y() + 30))
ed._release(_mev(_QE.MouseButtonRelease, center.x() + 50, center.y() + 30))
ed._on_tool("text")
ed._press(_mev(_QE.MouseButtonPress, center.x() - 100, center.y() - 100))
_ok("文字标注为场景项(可再编辑)", len(ed.annos) == 3
    and isinstance(ed.annos[-1], wq.AnnoText))
ed.annos[-1].setPlainText("标注测试")
_final2 = ed._final_image()
_ok("三种标注均进合成图", _final2.size == ed.base.size)
ed.grab().save(os.path.join(TMP, "editor_annot_%s.png" % MODE))
print("saved", os.path.join(TMP, "editor_annot_%s.png" % MODE))
ed.close()

from pinboard import PinBoardManager as _PBMgr   # 图钉画布子包
pbm = _PBMgr(fake)          # 置顶多图板;fake 无 cfg → 纯内存,不持久化
pb = pbm.window
pb.resize(720, 460)
pb.show()
app.processEvents()
import time as _t
for i, (w, h, col) in enumerate(((320, 200, (88, 140, 220)),
                                 (240, 160, (200, 120, 90)),
                                 (180, 240, (90, 170, 120)))):
    from PIL import Image  # 预览脚本仅在函数内导入过 PIL
    p = os.path.join(TMP, "pin%d_%s.png" % (i, MODE))
    Image.new("RGB", (w, h), col).save(p)
    pbm.pin_path(p, scene_pos=wq.QPointF(20 + i * 44, 26 + i * 32),
                 show=False)
_end = _t.time() + 4                    # 等后台解码装填再截图
while _t.time() < _end:
    app.processEvents()
    if pb.pin_items() and all(not it.pixmap().isNull()
                              for it in pb.pin_items()):
        break
    _t.sleep(0.02)
pb.pin_items()[1].setSelected(True)   # 看选中虚线框样式
pb.rotate_items([pb.pin_items()[2]], 90.0)   # 看旋转+锁定角标
pb.toggle_lock([pb.pin_items()[2]])
app.processEvents()
pb.grab().save(os.path.join(TMP, "pinboard_%s.png" % MODE))
print("saved", os.path.join(TMP, "pinboard_%s.png" % MODE))
