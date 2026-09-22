# -*- coding: utf-8 -*-
"""Task Graph(路线图 3.2):规划器产出的任务计划,归一化为可执行图。

计划 JSON(规划器协议):
    {"goal": "论文整理",
     "tasks": [{"id": 1, "tool": "summary"}, ...],
     "finished": false}

归一化规则:id 缺失按顺序补 1..n;工具名小数去空白;超出 max_tasks
截断(防规划器失控);坏条目(非 dict/无 tool)丢弃。执行模型是线性链:
tasks 顺序执行,上一步产出作为下一步输入。校验(未知工具等)在
validate() 单独给出,不阻止归一化本身。
"""

MAX_TASKS = 8


class Task:
    __slots__ = ("id", "tool", "note")

    def __init__(self, id, tool, note=""):
        self.id = int(id)
        self.tool = str(tool or "").strip()
        self.note = str(note or "")

    def to_dict(self):
        d = {"id": self.id, "tool": self.tool}
        if self.note:
            d["note"] = self.note
        return d


class TaskGraph:
    """归一化后的任务计划:goal + 有序 Task 列表 + finished 标记。"""

    def __init__(self, goal="", tasks=(), finished=False, raw=None):
        self.goal = str(goal or "")
        self.tasks = list(tasks)
        self.finished = bool(finished)
        self.raw = raw          # 规划器原始输出(诊断用)

    @classmethod
    def from_plan(cls, data, max_tasks=MAX_TASKS):
        """规划器输出 → 归一化任务图;坏结构尽力而为(空图不算错)。"""
        if not isinstance(data, dict):
            return cls(raw=data)
        raw_tasks = data.get("tasks")
        tasks = []
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if not isinstance(item, dict):
                    continue
                tool = str(item.get("tool") or item.get("name") or "").strip()
                if not tool:
                    continue
                try:
                    tid = int(item.get("id") or len(tasks) + 1)
                except (TypeError, ValueError):
                    tid = len(tasks) + 1
                tasks.append(Task(tid, tool, str(item.get("note") or "")))
        return cls(goal=str(data.get("goal") or ""),
                   tasks=tasks[:max(0, int(max_tasks))],
                   finished=bool(data.get("finished")), raw=data)

    def validate(self, registry):
        """按工具注册表校验;返回错误文案列表(空 = 通过)。"""
        errs = []
        if not self.tasks and not self.finished:
            errs.append("计划里没有可执行的任务")
        for t in self.tasks:
            if registry is None or registry.get(t.tool) is None:
                errs.append("任务 %d 用了未知工具「%s」" % (t.id, t.tool))
        return errs

    def to_dict(self):
        return {"goal": self.goal, "finished": self.finished,
                "tasks": [t.to_dict() for t in self.tasks]}
