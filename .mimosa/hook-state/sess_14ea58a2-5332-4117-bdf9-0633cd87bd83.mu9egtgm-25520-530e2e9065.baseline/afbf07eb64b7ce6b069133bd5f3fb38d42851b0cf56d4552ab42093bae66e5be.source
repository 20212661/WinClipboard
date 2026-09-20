# -*- coding: utf-8 -*-
"""图钉画布(pinboard 包)单元 + 集成自检(离屏;数据写入临时目录)。

运行: python _pinboard_test.py   退出码 0 = 全部通过

覆盖:PinRecord 序列化 / 资产去重与路径安全 / board.json 损坏回退 /
孤儿清扫 / 布局算法 / 旋转锚点 / 锁定 / 持久化往返 / 文件缺失恢复 /
config.json 旧格式迁移 / 全图 LRU。
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_pin_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

from PIL import Image
from PySide6.QtWidgets import QApplication, QGraphicsItem
from PySide6.QtCore import Qt as _Qt, QRectF

app = QApplication([])

from pinboard import PinBoardManager, PinRecord
from pinboard.storage import PinBoardStorage, _safe_child
from pinboard import layout
from pinboard.window import _ensure_onscreen
from pinboard.items import PinImageItem

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def pump(seconds=1.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_previews(mgr, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if mgr.window.pin_items() and all(
                not it.pixmap().isNull() for it in mgr.window.pin_items()):
            return True
        time.sleep(0.02)
    return False


def make_png(name, size=(120, 60), color=(10, 120, 220)):
    p = os.path.join(TMP, name)
    Image.new("RGB", size, color).save(p)
    return p


# 文件读写统一走 pathlib 辅助函数(测试基建;所有路径都是本测试创建的临时文件)
def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def write_text(path, s):
    Path(path).write_text(s, encoding="utf-8")


def read_json(path):
    return json.loads(read_text(path))


def write_json(path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False))


def read_bytes(path):
    return Path(path).read_bytes()


class FakeApp:
    """持久化模式替身:cfg 存在 → manager 走真实 pins/ 目录。"""
    icon = None

    def __init__(self):
        self.cfg = {}


# ============ 1. PinRecord 序列化 ============
r = PinRecord(asset="abc.png", x=1.5, y=2.5, scale=0.8, rotation=90,
              opacity=0.5, z=3.0, locked=True, w=640, h=480, id="rid001")
r2 = PinRecord.from_dict(r.to_dict())
check("record 往返一致", r2 is not None and
      (r2.asset, r2.x, r2.y, r2.scale, r2.rotation, r2.opacity, r2.z,
       r2.locked, r2.w, r2.h, r2.id) ==
      (r.asset, 1.5, 2.5, 0.8, 90.0, 0.5, 3.0, True, 640, 480, "rid001"))
lo = PinRecord.from_dict({"path": "a.png", "scale": 99, "opacity": 5.0})
check("record scale/opacity 夹紧", lo.scale == 25.0 and lo.opacity == 1.0,
      "scale=%s opacity=%s" % (lo.scale, lo.opacity))
lo2 = PinRecord.from_dict({"path": "a.png", "scale": 0.001,
                           "opacity": 0.05})
check("record 下界夹紧", abs(lo2.scale - 0.05) < 1e-9
      and abs(lo2.opacity - 0.1) < 1e-9)
check("record 0 值回落默认(防脏数据)",
      PinRecord.from_dict({"path": "a.png", "scale": 0,
                           "opacity": 0}).scale == 1.0)
check("record 缺 path → None", PinRecord.from_dict({"x": 1}) is None)
check("record 非 dict → None", PinRecord.from_dict("junk") is None)
check("record 坏数字回落", PinRecord.from_dict(
    {"path": "a.png", "x": "bad", "scale": "x", "w": "??", "h": 5}).x == 0.0)

# ============ 2. 存储:资产去重 / 白名单 / 路径安全 ============
st = PinBoardStorage(TMP)
p1 = make_png("s1.png", color=(1, 2, 3))
p1b = make_png("s1_copy.png", color=(1, 2, 3))      # 同内容不同文件名
n1, w1, h1 = st.import_asset(p1)
n2, _, _ = st.import_asset(p1b)
check("同内容资产去重", n1 == n2 and bool(n1) and n1.endswith(".png"))
check("资产尺寸读取", (w1, h1) == (120, 60), "%s %s" % (w1, h1))
check("资产文件唯一", len([f for f in os.listdir(st.img_dir)
                       if f.endswith(".png")]) == 1)
check("不存在文件拒绝",
      st.import_asset(os.path.join(TMP, "nope.png"))[0] is None)
write_text(os.path.join(TMP, "junk.txt"), "x")
check("txt 不收编",
      st.import_asset(os.path.join(TMP, "junk.txt"))[0] is None)
# 路径安全(_safe_child 净化):任意形状输入,输出必须落在目录内
check("绝对路径关进目录内",
      _safe_child(st.img_dir, os.path.join(TMP, "evil.png"))
      == os.path.join(st.img_dir, "evil.png"))
check("盘符路径关进目录内",
      os.path.dirname(_safe_child(st.img_dir, "C:\\x\\y.png")) == st.img_dir)
check("嵌套名只取末段",
      os.path.dirname(_safe_child(st.img_dir, os.path.join("s", "d.png")))
      == st.img_dir)
check("asset_path 输出必在 img_dir",
      all(os.path.dirname(st.asset_path(x)) == st.img_dir
          for x in ("ok.png", os.path.join(TMP, "abs.png"))))

p3 = make_png("s3.png", color=(9, 9, 9))
st.import_asset(p3)
removed = st.sweep([n1])                    # 只保留 n1
check("孤儿清扫", removed == 1
      and sorted(os.listdir(st.img_dir)) == [n1])

# ============ 3. board.json:保存 / 损坏回退 / 双损坏 ============
state = {"version": 1, "geo": [10, 20, 300, 200], "open": True,
         "zoom": 1.5, "items": [PinRecord(asset=n1).to_dict()]}
st.save_state(state)
check("状态落盘可读", st.load_state()["zoom"] == 1.5)
os.replace(st.board_file, st.board_file + ".bak1")     # 模拟主文件损坏
write_text(st.board_file, "{corrupted!!")
check("主文件损坏回退 .bak1", st.load_state()["zoom"] == 1.5)
write_text(st.board_file + ".bak1", "also bad")
check("双损坏 → None(空板重启)", st.load_state() is None)

# ============ 4. 布局算法(真实 item) ============
scene_items = []
for i in range(5):
    it = PinImageItem(PinRecord(asset="x%d.png" % i, w=80 + i * 30,
                                h=50 + i * 10), None)
    it.setScale(1.0)
    it.setPos(i * 7, i * 5)
    scene_items.append(it)


def boxes(its):
    return [it.mapRectToScene(it.boundingRect()) for it in its]


rect = QRectF(0, 0, 400, 1000)
layout.grid(scene_items, rect, gap=10)
b = boxes(scene_items)
check("grid 两两不重叠",
      all(not b[i].intersects(b[j]) for i in range(5) for j in range(i + 1, 5)))
check("grid 落在矩形内", all(rect.contains(bb) for bb in b))
layout.row(scene_items, rect, gap=10)
b = boxes(scene_items)
check("row 单调递增 x",
      all(b[i].right() <= b[i + 1].left() + 0.01 for i in range(4)))
layout.column(scene_items, rect, gap=10)
b = boxes(scene_items)
check("column 单调递增 y",
      all(b[i].bottom() <= b[i + 1].top() + 0.01 for i in range(4)))
layout.compact(scene_items, rect, gap=6)
b = boxes(scene_items)
check("compact 不重叠",
      all(not b[i].intersects(b[j]) for i in range(5) for j in range(i + 1, 5)))
# 对齐 / 等间距
scene_items[0].setPos(0, 0)
scene_items[1].setPos(90, 7)
scene_items[2].setPos(200, -3)
layout.align(scene_items[:3], "left")
b = boxes(scene_items)
check("左对齐", abs(b[0].left() - b[1].left()) < 0.01
      and abs(b[1].left() - b[2].left()) < 0.01)
layout.distribute(scene_items[:3], "h")
b = boxes(scene_items)
gaps = [b[1].left() - b[0].right(), b[2].left() - b[1].right()]
check("水平等间距", abs(gaps[0] - gaps[1]) < 0.01, str(gaps))

# ============ 5. 窗口:标志 / 交互 / 几何护栏 ============
class FA:                                   # 无 cfg → 纯内存模式
    icon = None


mgr = PinBoardManager(FA())
w = mgr.window
check("无边框+置顶+Tool 标志",
      all(bool(w.windowFlags() & f) for f in
          (_Qt.WindowStaysOnTopHint, _Qt.FramelessWindowHint, _Qt.Tool)))

pngs = [make_png("m%d.png" % i, color=(i, 99, 200)) for i in range(3)]
its = [mgr.pin_path(p, show=False) for p in pngs]
check("钉入三张", all(i is not None for i in its)
      and len(w.pin_items()) == 3)
check("后台 preview 全部装填", wait_previews(mgr))

# 旋转中心锚定
it = its[0]
c0 = it.mapToScene(it.boundingRect().center())
w.rotate_items([it], 90.0)
c1 = it.mapToScene(it.boundingRect().center())
check("旋转中心不动", (c0 - c1).manhattanLength() < 0.5, "%s→%s" % (c0, c1))

# 锁定:拒绝缩放/旋转,仍可选中删除
w.toggle_lock([it])
s0 = it.scale_value()
w.scale_item(it, 2.0)
w.rotate_items([it], 90.0)
check("锁定拒绝缩放/旋转", abs(it.scale_value() - s0) < 1e-6
      and abs(it.rotation() - 90.0) < 1e-6)
check("锁定清 Movable",
      not (it.flags() & QGraphicsItem.ItemIsMovable))
it.setSelected(True)
w.remove_selected()
check("锁定项仍可删除", len(w.pin_items()) == 2)

# 屏幕外窗口位置修正
w.resize(400, 300)
w.move(-5000, -5000)
_ensure_onscreen(w)
scr = app.primaryScreen().availableGeometry()
check("屏幕外位置拉回", scr.contains(w.frameGeometry().center())
      or scr.intersects(w.frameGeometry()))

# 大画板:远处自由放置不被拉回,场景扩张,fit_all 找回
it3 = w.pin_items()[1]
w.show()
app.processEvents()
r0 = w.scene.sceneRect()
it3.setPos(r0.right() + 2500, r0.bottom() + 2500)
w.after_items_moved()
check("画布外自由放置", it3.pos().x() > r0.right() + 2000)
check("场景随内容扩张", w.scene.sceneRect().contains(
    it3.mapToScene(it3.boundingRect().center())))
w.fit_all()
app.processEvents()
check("适应后全部可见", all(w.visible_rect().contains(
    i.mapToScene(i.boundingRect().center())) for i in w.pin_items()))

# 双击两态:actual ↔ fit
it2 = w.pin_items()[0]
w.show()
app.processEvents()
w.toggle_fit_item(it2)
check("双击→1:1", it2._mode == "actual")
z = w.view.transform().m11() or 1.0
check("1:1 屏幕像素对齐", abs(it2.scale_value() * z - 1.0) < 0.02,
      "s=%s z=%s" % (it2.scale_value(), z))
w.toggle_fit_item(it2)
check("双击→适应(可预测两态)", it2._mode == "fit")
w.close()

# ============ 6. 持久化往返(真实 pins/ 目录) ============
cm.APP_DIR = os.path.join(TMP, "persist_a")
mgr2 = PinBoardManager(FakeApp())
w2 = mgr2.window
pins = [make_png("pp%d.png" % i, color=(50 + i, 3, 4)) for i in range(2)]
for p in pins:
    mgr2.pin_path(p, show=False)
w2.pin_items()[0].setPos(33, 44)
w2.rotate_items([w2.pin_items()[1]], 45.0)
w2.nudge_opacity([w2.pin_items()[1]], -0.3)
mgr2.save_now(open_=True)
board_json = os.path.join(cm.APP_DIR, "pins", "board.json")
data = read_json(board_json)
moved = next(it for it in data["items"] if abs(it["x"] - 33) < 0.01)
rot = next(it for it in data["items"] if abs(it["rotation"]) > 0.01)
check("board.json 落盘字段", data["open"] is True and len(data["items"]) == 2
      and moved["y"] == 44
      and abs(rot["rotation"] - 45.0) < 1e-6
      and abs(rot["opacity"] - 0.7) < 1e-6,
      str(data.get("items")))
check("资产独立于剪贴板缓存",
      all(os.path.exists(os.path.join(cm.APP_DIR, "pins", "images",
                                      it["path"]))
          for it in data["items"]))

# 模拟重启:新 manager 读回
mgr3 = PinBoardManager(FakeApp())
check("重启恢复条目", len(mgr3.records) == 2
      and len(mgr3.window.pin_items()) == 2)
rit = mgr3.window.pin_items()
check("重启恢复几何", abs(rit[0].x() - 33) < 0.01 and abs(rit[0].y() - 44) < 0.01
      and abs(rit[1].rotation() - 45.0) < 1e-6
      and abs(rit[1].opacity() - 0.7) < 1e-6)
check("视图位置恢复(缩放+平移)",
      isinstance(data.get("view"), list) and len(data["view"]) == 3
      and abs((mgr3.window.view.transform().m11() or 1.0)
              - float(data["view"][0])) < 0.01,
      str(data.get("view")))
mgr3.window.close()

# 单文件丢失:跳过该条目,不整体失败
lost = os.path.join(cm.APP_DIR, "pins", "images", data["items"][0]["path"])
os.remove(lost)
mgr4 = PinBoardManager(FakeApp())
check("单文件缺失跳过", len(mgr4.records) == 1)
mgr4.window.close()

# ============ 6.5 编辑回写:标注后原地替换 ============
cm.APP_DIR = os.path.join(TMP, "replace_a")
mgrR = PinBoardManager(FakeApp())
itR = mgrR.pin_path(make_png("r1.png", color=(1, 2, 3)), show=False)
old_asset = itR.rec.asset
pos0, s0 = (itR.x(), itR.y()), itR.scale_value()
ok = mgrR.replace_asset_from(itR, Image.new("RGB", (300, 200), (250, 10, 10)))
check("编辑回写成功", ok and itR.rec.asset != old_asset)
check("回写保留几何与缩放", (itR.x(), itR.y()) == pos0
      and abs(itR.scale_value() - s0) < 1e-6)
check("新资产已入库", os.path.exists(os.path.join(
    cm.APP_DIR, "pins", "images", itR.rec.asset)))
check("新尺寸生效", itR.rec.w == 300 and itR.rec.h == 200)
mgrR.save_now()
check("旧资产被清扫", not os.path.exists(os.path.join(
    cm.APP_DIR, "pins", "images", old_asset)))
check("新 pixmap 已装填", not itR.pixmap().isNull())
mgrR.window.close()

# ============ 7. config.json 旧格式迁移 ============
cm.APP_DIR = os.path.join(TMP, "migrate_a")
old_png = make_png("old_pin.png", color=(7, 7, 250))
old_hash = hashlib.sha256(read_bytes(old_png)).hexdigest()
fa = FakeApp()
fa.cfg = {"pinboard": {
    "items": [{"path": old_png, "x": 5, "y": 6, "scale": 0.9, "z": 2}],
    "geo": [8, 9, 500, 400], "open": True}}
os.makedirs(cm.APP_DIR, exist_ok=True)
write_json(os.path.join(cm.APP_DIR, "config.json"), fa.cfg)
mgr5 = PinBoardManager(fa)
check("旧格式迁移收编", len(mgr5.records) == 1
      and mgr5.records[0].asset.startswith(old_hash[:8]))
check("迁移后 board.json 生成",
      os.path.exists(os.path.join(cm.APP_DIR, "pins", "board.json")))
check("迁移保留几何", abs(mgr5.records[0].x - 5) < 0.01
      and abs(mgr5.records[0].scale - 0.9) < 0.01)
# 源缓存删除后画布不受影响(场景 F)
os.remove(old_png)
mgr5.save_now()
mgr6 = PinBoardManager(FakeApp())
check("源文件删除后仍恢复(资产独立)", len(mgr6.records) == 1)
mgr5.window.close()
mgr6.window.close()

# ============ 8. 全图缓存按字节预算淘汰 ============
mgr7 = PinBoardManager(FA())
for i in range(5):
    mgr7.pin_path(make_png("f%d.png" % i, (900, 700)), show=False)
wait_previews(mgr7)
import pinboard.manager as pm
# 900×700 ARGB32 = 900*700*4 = 2,520,000B/张;预算给整 2 张,钉 5 张必被淘汰
pm.FULL_CACHE_BYTES_LIMIT = 2 * (900 * 700 * 4)
for wi in mgr7.window.pin_items():
    mgr7.request_full(wi)
check("全图缓存字节不超预算",
      mgr7._full_cache_bytes <= pm.FULL_CACHE_BYTES_LIMIT,
      "%d vs %d" % (mgr7._full_cache_bytes, pm.FULL_CACHE_BYTES_LIMIT))
check("超预算条目被 LRU 淘汰", len(mgr7._full_cache) <= 2,
      str(len(mgr7._full_cache)))
check("字节账本与条目一致",
      mgr7._full_cache_bytes ==
      sum(e.bytes for e in mgr7._full_cache.values()),
      str(mgr7._full_cache_bytes))
check("被淘汰项退回 preview",
      any(not i._full for i in mgr7.window.pin_items()))
mgr7.window.close()

# ============ 9. 穿透开关可双向切换(Bug1) ============
mgr8 = PinBoardManager(FA())
w8 = mgr8.window
from PySide6.QtCore import Qt as _Qt2
mgr8.toggle_click_through(True)
check("穿透开启=窗口忽略输入", bool(
    w8.windowFlags() & _Qt2.WindowTransparentForInput) and mgr8._ctt)
mgr8.toggle_click_through(False)
check("穿透关闭=恢复接收输入", not (
    w8.windowFlags() & _Qt2.WindowTransparentForInput) and not mgr8._ctt)
mgr8.toggle_click_through(True)     # 连续两次开→关→开
mgr8.toggle_click_through(None)     # None=取反
check("穿透状态变量与窗口标志同步", mgr8._ctt == bool(
    w8.windowFlags() & _Qt2.WindowTransparentForInput))
mgr8.toggle_click_through(False)
w8.close()

# ============ 10. 工具栏删除=选中感知(Bug2) ============
mgr9 = PinBoardManager(FA())
w9 = mgr9.window
for i in range(3):
    mgr9.pin_path(make_png("d%d.png" % i, color=(i, 88, 250)), show=False)
items9 = w9.pin_items()
items9[1].setSelected(True)                 # 只选中第 2 张
w9.delete_smart()
check("删除只作用于选中的那一张", len(w9.pin_items()) == 2
      and items9[1] not in w9.pin_items()
      and items9[0] in w9.pin_items()
      and items9[2] in w9.pin_items())
w9.close()

# ============ 11. 单图备注:跟随 + 独立 + 持久化(Bug3) ============
cm.APP_DIR = os.path.join(TMP, "note_a")
mgrN = PinBoardManager(FakeApp())
itA = mgrN.pin_path(make_png("n1.png", color=(9, 99, 199)), show=False)
itB = mgrN.pin_path(make_png("n2.png", color=(99, 9, 199)), show=False)
itA.set_note("这是 A 的备注")
check("备注入库(rec.note)", itA.rec.note == "这是 A 的备注"
      and itA._note_item is not None)
note_before = itA._note_item.mapRectToScene(
    itA._note_item.boundingRect()).topLeft()
itA.setPos(itA.x() + 150, itA.y() + 90)     # 移动图片
note_after = itA._note_item.mapRectToScene(
    itA._note_item.boundingRect()).topLeft()
check("备注跟随图片移动", (note_after - note_before).manhattanLength() > 200,
      "%s -> %s" % (note_before, note_after))
itA.set_scale_value(2.0)                    # 缩放图片
check("备注随图片缩放", abs(itA._note_item.mapRectToScene(
    itA._note_item.boundingRect()).height()
    - note_after.y() * 0 - itA._note_item.boundingRect().height() * 2) < 1.5)
check("备注互相独立", itB.rec.note == "" and itB._note_item is None)
mgrN.save_now()
dataN = read_json(os.path.join(cm.APP_DIR, "pins", "board.json"))
check("备注落盘", dataN["items"][0].get("note") == "这是 A 的备注",
      str(dataN["items"][0].get("note")))
mgrN2 = PinBoardManager(FakeApp())
itA2 = next(i for i in mgrN2.window.pin_items()   # 场景序≠记录序,按 rec 找
            if i.rec is mgrN2.records[0])
check("备注重启恢复", mgrN2.records[0].note == "这是 A 的备注"
      and itA2._note_item is not None)
itA2.set_note("")                                 # 清空=删除
check("清空即删除备注", mgrN2.records[0].note == ""
      and itA2._note_item is None)
mgrN.window.close()
mgrN2.window.close()

# ============ 结果 ============
print()
if FAILS:
    print("FAILED: %d -> %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("ALL PINBOARD TESTS PASSED")
