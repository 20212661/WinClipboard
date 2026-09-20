# -*- coding: utf-8 -*-
"""图钉画布数据模型:PinRecord 是一张钉图的全部持久化状态。

模型只描述「什么图(asset 文件名)+ 怎么摆(几何/外观)」,不持有任何 Qt 部件;
重启恢复、状态落盘、条目重建都围绕它进行(见 storage.PinBoardStorage)。
"""
import uuid
from dataclasses import dataclass, field

# 条目缩放护栏:下限 5%;上限与整板缩放下限联动(1:1 实际像素 =
# 条目缩放 × 整板缩放,整板缩到 4% 时 1:1 需要条目 25×)
MIN_SCALE, MAX_SCALE = 0.05, 25.0
# 透明度护栏 10% ~ 100%
MIN_OPACITY, MAX_OPACITY = 0.1, 1.0


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def new_id():
    return uuid.uuid4().hex[:12]


@dataclass
class PinRecord:
    """一张钉图的可持久化状态;runtime 字段(如双击适应记忆)不在这里。"""

    asset: str                 # pins/images/ 下的文件名(sha256.ext);相对存储
    x: float = 0.0             # 场景坐标(画布左上角为原点,随 view 缩放)
    y: float = 0.0
    scale: float = 1.0         # 相对原始像素尺寸
    rotation: float = 0.0      # 顺时针角度
    opacity: float = 1.0       # 0.1 ~ 1.0
    z: float = 0.0
    locked: bool = False
    visible: bool = True
    note: str = ""               # 单图备注(空串=无)
    w: int = 0                 # 原始像素宽高(占位框/适应/1:1 计算用)
    h: int = 0
    id: str = field(default_factory=new_id)

    def to_dict(self):
        return {"id": self.id, "path": self.asset, "x": self.x, "y": self.y,
                "scale": self.scale, "rotation": self.rotation,
                "opacity": self.opacity, "z": self.z, "locked": self.locked,
                "visible": self.visible, "note": self.note,
                "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d):
        """健壮反序列化:单字段损坏回落默认值,缺 path 返回 None(条目丢弃)。"""
        if not isinstance(d, dict):
            return None
        asset = str(d.get("path") or d.get("asset") or "").strip()
        if not asset:
            return None
        try:
            w = max(0, int(d.get("w") or 0))
            h = max(0, int(d.get("h") or 0))
        except (TypeError, ValueError):
            w = h = 0
        scale = _f(d.get("scale"), 1.0) or 1.0
        opacity = _f(d.get("opacity"), 1.0) or 1.0
        rid = str(d.get("id") or "").strip() or new_id()
        return cls(asset=asset, x=_f(d.get("x")), y=_f(d.get("y")),
                   scale=max(MIN_SCALE, min(MAX_SCALE, scale)),
                   rotation=_f(d.get("rotation")) % 360.0,
                   opacity=max(MIN_OPACITY, min(MAX_OPACITY, opacity)),
                   z=_f(d.get("z")), locked=bool(d.get("locked")),
                   visible=bool(d.get("visible", True)), w=w, h=h, id=rid,
                   note=str(d.get("note") or ""))
