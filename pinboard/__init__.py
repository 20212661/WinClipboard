# -*- coding: utf-8 -*-
"""图钉画布(PinBoard)子包:置顶多图画布的完整实现。

分层:model(数据)/ storage(资产+状态持久化)/ items(图元)/
view+scene(视图交互)/ window(窗口与快捷键)/ layout(排列算法)/
loader(后台解码)/ manager(门面,主程序只与它交互)。
"""
from .manager import PinBoardManager
from .model import PinRecord
from .window import PinBoardWindow

__all__ = ["PinBoardManager", "PinBoardWindow", "PinRecord"]
