# -*- coding: utf-8 -*-
"""Command 对象:命令面板的一条命令(纯数据 + 谓词,不引 Qt)。"""


class Command:
    """面板命令。

    id        唯一标识(点分命名,如 clipboard.copy)
    name      显示名(搜索主命中域)
    action    可调用(无参);执行经 executor 统一捕获异常
    shortcut  展示用快捷键提示;str 或 ()->str(主热键这类可变的传可调用)
    when      可选谓词()->bool;False 时面板不显示该条(如"需要选中条目")
    category  分组名(见 registry.CATEGORY_ORDER)
    keywords  额外搜索词(空格分隔;中英别名都放这里)
    """

    __slots__ = ("id", "name", "shortcut", "action", "when", "category",
                 "keywords")

    def __init__(self, id, name, action, shortcut="", when=None,
                 category="其他", keywords=""):
        self.id = str(id)
        self.name = str(name)
        self.shortcut = shortcut
        self.action = action
        self.when = when
        self.category = str(category or "其他")
        self.keywords = str(keywords or "")

    def available(self):
        """上下文谓词;谓词自身抛异常按不可用处理(面板不显示)。"""
        try:
            return self.when is None or bool(self.when())
        except Exception:
            return False

    def shortcut_text(self):
        try:
            return self.shortcut() if callable(self.shortcut) else self.shortcut
        except Exception:
            return ""

    def match_text(self):
        return ("%s %s %s" % (self.name, self.id, self.keywords)).lower()

    def __repr__(self):
        return "<Command %s %s>" % (self.id, self.name)
