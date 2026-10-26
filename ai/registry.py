# -*- coding: utf-8 -*-
"""AI 动作注册表:ai/actions/*.json 声明式动作,启动时扫描注册。

JSON 字段:
    name        菜单显示名(必填,非空)
    id          动作标识(必填,唯一;用户自定义动作与内置同 id 时覆盖内置)
    workflow    对应工作流标识(必填):先查用户配置 ai_workflows(name 或
                id 相等者),再查 ai.workflows.BUILTIN_WORKFLOWS 内置定义
    description 描述(可选,菜单条目悬停提示)
    icon        FluentIcon 枚举名(可选,缺失时菜单不显示图标)
    applies     适用内容类型列表(可选,默认 ["text"];卡片 type 属于
                列表内才出现在菜单里)
    order       菜单排序权重(可选,小者在前;缺省排同名之后)

扫描顺序:内置动作包目录 → 用户动作目录(APP_DIR/ai_actions,不存在则
跳过);坏文件记入 errors 不拖垮启动。
"""
import json
from pathlib import Path

BUILTIN_ACTIONS_DIR = Path(__file__).resolve().parent / "actions"
REQUIRED_FIELDS = ("name", "id", "workflow")
_DEFAULT_ORDER = 10 ** 6


class ActionRegistry:
    """动作注册表:register 校验入册,scan_dir 批量装载。纯 Python 无 Qt。"""

    def __init__(self):
        self._acts = {}          # id -> 动作 dict
        self.errors = []         # [(来源, 原因)]

    def _fail(self, source, reason):
        self.errors.append((source, reason))

    def register(self, defn, source=""):
        """校验并登记一条动作;返回是否成功(失败记 errors)。"""
        if not isinstance(defn, dict):
            self._fail(source, "动作必须是 JSON 对象")
            return False
        for k in REQUIRED_FIELDS:
            v = defn.get(k)
            if not isinstance(v, str) or not v.strip():
                self._fail(source, "缺少必填字段「%s」(非空字符串)" % k)
                return False
        d = dict(defn)
        applies = d.get("applies", ["text"])
        if not isinstance(applies, list) or \
                not all(isinstance(x, str) and x for x in applies):
            self._fail(source, "applies 须为内容类型字符串列表(如 [\"text\"])")
            return False
        try:
            d["order"] = int(d.get("order", _DEFAULT_ORDER))
        except (TypeError, ValueError):
            d["order"] = _DEFAULT_ORDER
        self._acts[d["id"]] = d
        return True

    def get(self, action_id):
        return self._acts.get(action_id)

    def all(self):
        """全部动作,按 (order, name) 排序(菜单展示序)。"""
        return sorted(self._acts.values(), key=lambda d: (d["order"], d["name"]))

    def for_record(self, rec):
        """按记录类型过滤适用动作(rec["type"]:text/image/files…)。"""
        t = (rec or {}).get("type") or ""
        return [d for d in self.all() if t in (d.get("applies") or ["text"])]

    def scan_dir(self, folder):
        """装载目录下全部 *.json;返回成功数(坏文件记 errors)。"""
        p = Path(folder)
        if not p.is_dir():
            return 0
        n = 0
        for f in sorted(p.glob("*.json")):
            try:
                defn = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                self._fail(str(f), "JSON 解析失败:%s" % e)
                continue
            if self.register(defn, str(f)):
                n += 1
        return n


_REGISTRY = None


def init(user_dir=None):
    """构建全局注册表:内置动作目录 + 用户动作目录(同 id 后者覆盖)。"""
    global _REGISTRY
    reg = ActionRegistry()
    reg.scan_dir(BUILTIN_ACTIONS_DIR)
    if user_dir:
        reg.scan_dir(user_dir)
    _REGISTRY = reg
    return reg


def get_registry():
    """取全局注册表;未显式 init 时自动装载内置动作(测试/宿主最小路径)。"""
    if _REGISTRY is None:
        init()
    return _REGISTRY
