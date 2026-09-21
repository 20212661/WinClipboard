# -*- coding: utf-8 -*-
"""命令注册表:注册(重复 id 后者覆盖,保持原展示位)+ 上下文过滤 + 打分搜索。"""

from .command import Command

# 分组展示序(未列出的类别排在最后,按名称排)
CATEGORY_ORDER = ("剪贴板", "查看与对比", "AI", "图钉画布", "导航", "应用")


class CommandRegistry:

    def __init__(self):
        self._cmds = {}          # id -> Command
        self._order = []         # 注册序(重复覆盖时保持首注册位置)

    def register(self, cmd, name=None, action=None, **kw):
        """登记一条命令;既可传 Command 实例,也可按
        register(id, name=..., action=..., ...) 的简写形态。"""
        if isinstance(cmd, Command):
            c = cmd
        elif isinstance(cmd, str) and name is not None and action is not None:
            c = Command(cmd, name, action, **kw)
        else:
            raise TypeError("register 需要 Command 实例,或 (id, name=, action=)")
        if c.id not in self._cmds:
            self._order.append(c.id)
        self._cmds[c.id] = c
        return c

    def get(self, cmd_id):
        return self._cmds.get(cmd_id)

    def __contains__(self, cmd_id):
        return cmd_id in self._cmds

    def __len__(self):
        return len(self._cmds)

    def all(self, only_available=True):
        """全部命令:按 (分组序, 名称) 排;only_available 时过滤 when 不满足者。"""
        cmds = [self._cmds[i] for i in self._order]
        if only_available:
            cmds = [c for c in cmds if c.available()]
        return sorted(cmds, key=lambda c: (
            CATEGORY_ORDER.index(c.category)
            if c.category in CATEGORY_ORDER else len(CATEGORY_ORDER),
            c.name))

    def categories(self, only_available=True):
        seen = []
        for c in self.all(only_available):
            if c.category not in seen:
                seen.append(c.category)
        return seen

    def search(self, query, only_available=True):
        """按查询词过滤 + 打分排序;空查询 = all()。

        打分:显示名前缀 100 > 显示名包含 60 > 关键词包含 40 > id 包含 30;
        同分按 (分组序, 名称) 稳定排序。"""
        q = str(query or "").strip().lower()
        pool = self.all(only_available)
        if not q:
            return pool
        scored = []
        for c in pool:
            m = c.match_text()
            name = c.name.lower()
            if name.startswith(q):
                s = 100
            elif q in name:
                s = 60
            elif q in c.keywords.lower():
                s = 40
            elif q in c.id.lower():
                s = 30
            else:
                continue
            scored.append((s, c))
        scored.sort(key=lambda t: (-t[0], CATEGORY_ORDER.index(t[1].category)
                                   if t[1].category in CATEGORY_ORDER
                                   else len(CATEGORY_ORDER), t[1].name))
        return [c for _s, c in scored]
