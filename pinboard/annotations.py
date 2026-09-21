# -*- coding: utf-8 -*-
"""标注层(Annotation Layer)——非破坏编辑模型的数据与渲染。

模型三要素:
- Original asset:pins/images 下的原图,内容寻址,永不被标注改写;
- Annotation layer:本模块的 schema(list[dict],图像像素坐标),
  存于 PinRecord.annos / board.json,纯矢量参数——马赛克也只存
  区域+因子,不存任何派生像素,渲染时从底图现算;
- Transform:PinRecord 的 x/y/scale/rotation/opacity(本模块不涉及)。

复制/导出时才 compose() 合成像素;画布显示用 build_items() 生成
跟随钉图变换的覆盖子项。本模块只依赖 PySide6 + PIL,不 import
主程序(避免环:winclipboard_qt → pinboard → annotations)。
"""
import math
import re

from PIL import Image
from PySide6.QtCore import Qt, QRect, QRectF, QPointF
from PySide6.QtGui import (QPen, QColor, QFont, QPainterPath, QPixmap,
                           QImage, QPainter, QPolygonF)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsRectItem,
                               QGraphicsEllipseItem, QGraphicsPathItem,
                               QGraphicsTextItem, QGraphicsPixmapItem,
                               QGraphicsScene)

KINDS = ("rect", "ellipse", "arrow", "pen", "text", "mosaic")
MAX_ANNOS = 2000            # 单图标注数上限(防脏状态文件拖垮渲染)
MAX_PEN_POINTS = 4000
DEFAULT_COLOR = "#FF4040"    # 与编辑器默认红一致
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _hex(v):
    return v if isinstance(v, str) and _HEX_COLOR.match(v) else DEFAULT_COLOR


def sanitize(lst):
    """外部来源(board.json/编辑器产物)标注列表 → 干净 dict 列表。
    结构/数值非法的条目整条丢弃;返回新列表,不修改入参。"""
    if not isinstance(lst, (list, tuple)):
        return []
    out = []
    for d in lst[:MAX_ANNOS]:
        if not isinstance(d, dict):
            continue
        t = str(d.get("t") or "")
        c = _hex(d.get("c"))
        pw = min(64.0, max(0.5, _f(d.get("pw"), 4.0)))
        if t in ("rect", "ellipse"):
            w, h = _f(d.get("w")), _f(d.get("h"))
            if w <= 0 or h <= 0:
                continue
            out.append({"t": t, "x": _f(d.get("x")), "y": _f(d.get("y")),
                        "w": w, "h": h, "c": c, "pw": pw})
        elif t == "arrow":
            out.append({"t": t, "x1": _f(d.get("x1")), "y1": _f(d.get("y1")),
                        "x2": _f(d.get("x2")), "y2": _f(d.get("y2")),
                        "c": c, "pw": pw})
        elif t == "pen":
            raw = d.get("pts")
            if not isinstance(raw, (list, tuple)):
                continue
            pts = [[_f(p[0]), _f(p[1])] for p in raw[:MAX_PEN_POINTS]
                   if isinstance(p, (list, tuple)) and len(p) == 2]
            if len(pts) < 2:
                continue
            out.append({"t": t, "pts": pts, "c": c, "pw": pw})
        elif t == "text":
            s = str(d.get("s") or "")
            if not s.strip():
                continue
            out.append({"t": t, "x": _f(d.get("x")), "y": _f(d.get("y")),
                        "s": s, "c": c,
                        "px": min(400, max(6, int(_f(d.get("px"), 26))))})
        elif t == "mosaic":
            w, h = _f(d.get("w")), _f(d.get("h"))
            if w <= 0 or h <= 0:
                continue
            out.append({"t": t, "x": _f(d.get("x")), "y": _f(d.get("y")),
                        "w": w, "h": h,
                        "f": min(64, max(2, int(_f(d.get("f"), 12))))})
    return out


# ---- 序列化(编辑器活图元 → dict) ----

def serialize_items(items):
    """编辑器标注项列表 → 可持久化 dict 列表(创建序即 z 序)。
    项缺 _kind 标记(非本编辑器产物)时跳过。"""
    out = []
    for it in items:
        k = getattr(it, "_kind", None)
        try:
            d = _serialize_one(it, k)
        except Exception:
            d = None
        if d:
            out.append(d)
    return out


def _serialize_one(it, kind):
    if kind in ("rect", "ellipse"):
        r = it.rect().translated(it.pos())
        pen = it.pen()
        return {"t": kind, "x": r.x(), "y": r.y(), "w": r.width(),
                "h": r.height(), "c": pen.color().name().upper(),
                "pw": float(pen.widthF() or 4.0)}
    if kind == "arrow":
        path = it.path()
        if path.elementCount() < 2:
            return None
        p0, e1 = it.pos(), path.elementAt(1)
        pen = it.pen()
        return {"t": "arrow", "x1": p0.x(), "y1": p0.y(),
                "x2": p0.x() + e1.x, "y2": p0.y() + e1.y,
                "c": pen.color().name().upper(),
                "pw": float(pen.widthF() or 4.0)}
    if kind == "pen":
        path, p0 = it.path(), it.pos()
        pts = [[p0.x() + path.elementAt(i).x,
                p0.y() + path.elementAt(i).y]
               for i in range(path.elementCount())]
        if len(pts) < 2:
            return None
        pen = it.pen()
        return {"t": "pen", "pts": pts, "c": pen.color().name().upper(),
                "pw": float(pen.widthF() or 4.0)}
    if kind == "text":
        px = it.font().pixelSize()
        return {"t": "text", "x": it.pos().x(), "y": it.pos().y(),
                "s": it.toPlainText(), "c": it.defaultTextColor().name().upper(),
                "px": px if px and px > 0 else 26}
    if kind == "mosaic":
        return {"t": "mosaic", "x": it.pos().x(), "y": it.pos().y(),
                "w": float(it.pixmap().width()), "h": float(it.pixmap().height()),
                "f": int(getattr(it, "_factor", 12))}
    return None


# ---- 箭头几何(与编辑器同源的唯一实现) ----

def arrow_path(p0, p1, head_len=14.0, head_angle=0.45):
    path = QPainterPath(p0)
    path.lineTo(p1)
    ang = math.atan2(p1.y() - p0.y(), p1.x() - p0.x())
    path.addPolygon(QPolygonF([
        p1,
        QPointF(p1.x() - head_len * math.cos(ang - head_angle),
                p1.y() - head_len * math.sin(ang - head_angle)),
        QPointF(p1.x() - head_len * math.cos(ang + head_angle),
                p1.y() - head_len * math.sin(ang + head_angle))]))
    path.closeSubpath()
    return path


def _display_pen(d):
    pen = QPen(QColor(d["c"]), d["pw"])
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def build_items(annos, mosaic_patch=None):
    """干净标注列表 → 展示用 Qt 图元(图像像素坐标,无交互)。
    mosaic_patch(d) → QPixmap 回调由调用方提供(画布用预览像素,
    compose 用原图像素);返回 None 的马赛克跳过。"""
    out = []
    for d in sanitize(annos):
        t = d["t"]
        if t in ("rect", "ellipse"):
            r = QRectF(d["x"], d["y"], d["w"], d["h"])
            it = QGraphicsRectItem(r) if t == "rect" else QGraphicsEllipseItem(r)
            it.setPen(_display_pen(d))
            it.setBrush(Qt.NoBrush)
        elif t == "arrow":
            it = QGraphicsPathItem(arrow_path(
                QPointF(0, 0), QPointF(d["x2"] - d["x1"], d["y2"] - d["y1"])))
            it.setPos(d["x1"], d["y1"])
            pen = _display_pen(d)
            pen.setCosmetic(False)
            it.setPen(pen)
            it.setBrush(QColor(d["c"]))
        elif t == "pen":
            pts = d["pts"]
            path = QPainterPath(QPointF(0, 0))
            for p in pts[1:]:
                path.lineTo(p[0] - pts[0][0], p[1] - pts[0][1])
            it = QGraphicsPathItem(path)
            it.setPos(pts[0][0], pts[0][1])
            it.setPen(_display_pen(d))
            it.setBrush(Qt.NoBrush)
        elif t == "text":
            it = QGraphicsTextItem(d["s"])
            f = QFont()
            f.setPixelSize(d["px"])
            f.setBold(True)
            it.setFont(f)
            it.setDefaultTextColor(QColor(d["c"]))
            it.setPos(d["x"], d["y"])
            it.setTextInteractionFlags(Qt.NoTextInteraction)
        elif t == "mosaic":
            pm = mosaic_patch(d) if mosaic_patch is not None else None
            if pm is None or pm.isNull():
                continue
            it = QGraphicsPixmapItem(pm)
            it.setTransformationMode(Qt.FastTransformation)
            it.setPos(d["x"], d["y"])
        else:
            continue
        it.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        it.setFlag(QGraphicsItem.ItemIsSelectable, False)
        out.append(it)
    return out


# ---- 像素桥(QImage ↔ PIL;与 winclipboard_qt 同思路,本地实现避免反向依赖) ----

def pil_to_qimage(img):
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, "PNG")
    return QImage.fromData(buf.getvalue(), "PNG")


def qimage_to_pil(qimg):
    """QImage(ARGB32) → PIL RGB(白底合成)。"""
    if qimg.format() != QImage.Format_ARGB32:
        qimg = qimg.convertToFormat(QImage.Format_ARGB32)
    w, h = qimg.width(), qimg.height()
    rgba = Image.frombuffer("RGBA", (w, h), bytes(qimg.constBits()),
                            "raw", "BGRA", qimg.bytesPerLine(), 1)
    out = Image.new("RGB", (w, h), (255, 255, 255))
    out.paste(rgba, mask=rgba.split()[3])
    return out


def mosaic_from_pil(base_pil, d):
    """原图像素生成马赛克贴片(缩小→近邻放大,块状)。"""
    f = max(2, int(d.get("f", 12)))
    rect = QRectF(d["x"], d["y"], d["w"], d["h"]).toAlignedRect().intersected(
        QRect(0, 0, base_pil.width, base_pil.height))
    if rect.width() < 2 or rect.height() < 2:
        return None
    region = base_pil.crop((rect.x(), rect.y(),
                            rect.right() + 1, rect.bottom() + 1))
    small = region.resize((max(1, region.width // f),
                           max(1, region.height // f)), Image.BILINEAR)
    return QPixmap.fromImage(pil_to_qimage(
        small.resize(region.size, Image.NEAREST)))


def compose(base_pil, annos):
    """导出/复制时合成:原图 + 标注层 → PIL RGB。无标注返回原图。"""
    clean = sanitize(annos)
    if not clean:
        return base_pil
    base = base_pil.convert("RGB")
    w, h = base.size
    scene = QGraphicsScene()
    scene.addItem(QGraphicsPixmapItem(QPixmap.fromImage(pil_to_qimage(base))))
    for it in build_items(clean, mosaic_patch=lambda d: mosaic_from_pil(base, d)):
        scene.addItem(it)
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.white)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    scene.render(p, QRectF(0, 0, w, h), QRectF(0, 0, w, h))
    p.end()
    return qimage_to_pil(img)
