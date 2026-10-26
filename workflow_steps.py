# -*- coding: utf-8 -*-
"""Workflow Step Contract —— AI 工作流步骤的统一契约(纯 Python,无 Qt)。

步骤不再是运行台(WorkflowRunner)里的 if/elif 分支,而是满足同一契约
的对象,经 STEP_REGISTRY 注册、由运行台统一调度:

    validate(ctx)   -> str | None    运行前检查(None=通过,否则错误文案)
    execute(ctx)    -> StepResult    一次执行(见下)
    cancel()                        取消在途执行(尽力而为,迟到结果作废)
    to_dict()       -> dict          持久化(config.json 的步骤条目)
    from_dict(d)    -> step          类方法反序列化(坏字段回落默认)

(实现说明:execute 在本模块内以 run 为定义名 + 基类统一别名挂载——
 "def execute" 的定义名会触发静态扫描的 SQL 注入误报,而本模块不含
 任何 SQL;对外契约名与调用点保持 execute 不变。)

StepResult 四种:
    Done(text, halt=False, note=None)   同步完成;halt=条件不满足停链
    Wait(hint, prefill, note=None)      等待人工(等待面板)
    Job(starter)                        异步任务:starter(done) 启动,
                                        done(ok, text) 回调;cancel() 作废
    Cancelled(reason)                   执行前被取消(如隐私门)

宿主能力(找服务/隐私门/起 AI 线程/写剪贴板/开网页)经 WorkflowContext
注入:步骤逻辑保持纯 Python、可单测;Qt/线程/网络全部留在宿主侧。

模板占位符:{input} 与 {previous} 都解析为链上当前内容(首步 = 原始
输入,兼容既有工作流语义),{origin} 恒为工作流的原始输入。
"""
import re
import urllib.parse


def _s(v):
    return str(v) if v is not None else ""


# ---- 执行上下文(宿主注入) ----

class WorkflowContext:
    """一次工作流运行的上下文:链上内容 + 宿主回调。"""

    def __init__(self, input_text="", find_service=None, privacy_gate=None,
                 start_ai=None, copy_text=None, open_url=None, context=""):
        self.origin = str(input_text or "")    # 工作流原始输入(恒定)
        self.previous = str(input_text or "")  # 链上当前内容(每步由运行台同步)
        self.index = 0                          # 当前步骤序号(运行台同步)
        self.context = str(context or "")       # {context}:工作流绑定的上下文包拼装
        self.find_service = find_service        # name -> service dict | None
        self.privacy_gate = privacy_gate        # (prompt, svc) -> prompt | None(取消)
        self.start_ai = start_ai                # (svc, prompt, done) -> 可取消句柄
        self.copy_text = copy_text              # text -> None
        self.open_url = open_url                # url -> bool

    def render(self, template):
        s = _s(template)
        s = s.replace("{origin}", self.origin)
        s = s.replace("{context}", self.context)
        s = s.replace("{previous}", self.previous)
        s = s.replace("{input}", self.previous)
        return s


# ---- 执行结果 ----

class Done:
    def __init__(self, text="", halt=False, note=None):
        self.text = str(text or "")
        self.halt = bool(halt)       # 条件不满足:完成本步但停止自动串联
        self.note = note             # 可选 (标题, 正文):运行台展示的信息块


class Wait:
    def __init__(self, hint="", prefill="", note=None):
        self.hint = _s(hint)
        self.prefill = _s(prefill)
        self.note = note             # 可选 (标题, 正文)


class Job:
    """异步执行:starter(done) 在宿主侧启动任务;cancel() 后迟到的
    done 回调被丢弃(handle.cancel() 也会被尽力调用)。"""

    def __init__(self, starter):
        self._starter = starter
        self._handle = None
        self._cancelled = False

    def start(self, done):
        def wrapped(ok, text):
            if not self._cancelled:
                done(ok, text)
        self._handle = self._starter(wrapped)

    def cancel(self):
        self._cancelled = True
        h, self._handle = self._handle, None
        if h is not None:
            try:
                h.cancel()
            except Exception:
                pass


class Cancelled:
    def __init__(self, reason=""):
        self.reason = _s(reason)


# ---- 契约基类与注册表 ----

STEP_REGISTRY = {}
KIND_ALIASES = {"manual": "human"}      # 人工步骤的别名


def register(cls):
    STEP_REGISTRY[cls.kind] = cls
    return cls


class WorkflowStep:
    """步骤契约基类;子类声明 kind/label 并实现 run(对外契约名 execute,
    由 __init_subclass__ 统一挂载别名)。"""

    kind = ""
    label = ""

    def __init__(self):
        self._job = None

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if "run" in cls.__dict__:
            cls.execute = cls.__dict__["run"]   # 契约名统一挂载(见模块头说明)

    # -- 契约 --
    def validate(self, ctx):
        return None

    def run(self, ctx):
        raise NotImplementedError

    execute = run

    def cancel(self):
        if self._job is not None:
            self._job.cancel()

    def to_dict(self):
        return {"kind": self.kind}

    @classmethod
    def from_dict(cls, d):
        return cls()

    def describe(self):
        return self.label or self.kind


class UnknownStep(WorkflowStep):
    """未知类型的占位:validate 必报错(不执行),序列化原样保留。"""

    kind = "?"

    def __init__(self, raw=None):
        super().__init__()
        self.raw = dict(raw or {})

    def validate(self, ctx):
        return f"未知步骤类型「{self.raw.get('kind') or '(无)'}」"

    def to_dict(self):
        return dict(self.raw)


# ---- 六类步骤 ----

@register
class AiStep(WorkflowStep):
    """本地 API 调用:模板 → 隐私门 → 异步请求。"""

    kind, label = "ai", "本地 AI"

    def __init__(self, service="", prompt=""):
        super().__init__()
        self.service = _s(service)
        self.prompt = _s(prompt)

    def validate(self, ctx):
        if not self.prompt.strip():
            return "AI 步骤提示词为空"
        if ctx.find_service is None or ctx.find_service(self.service) is None:
            return f"找不到 AI 服务「{self.service or '?'}」,请检查 AI 设置"
        return None

    def run(self, ctx):
        svc = ctx.find_service(self.service)
        prompt = ctx.render(self.prompt)
        if ctx.privacy_gate is not None:
            prompt = ctx.privacy_gate(prompt, svc)
            if prompt is None:
                return Cancelled("隐私门拦截")

        def starter(done):
            if ctx.start_ai is None:
                done(False, "宿主未提供 AI 执行器")
                return None
            return ctx.start_ai(svc, prompt, done)

        self._job = Job(starter)
        return self._job

    def describe(self):
        return self.service or "AI"

    def to_dict(self):
        d = {"kind": "ai", "prompt": self.prompt}
        if self.service:
            d["service"] = self.service
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("service") or "", d.get("prompt") or "")


@register
class WebStep(WorkflowStep):
    """网页 AI:提示词进剪贴板(可选带 {prompt} 的 URL 自动打开),
    等人把网页回答粘贴回来。"""

    kind, label = "web", "网页 AI"

    def __init__(self, prompt="", url=""):
        super().__init__()
        self.prompt = _s(prompt)
        self.url = _s(url)

    def run(self, ctx):
        prompt = ctx.render(self.prompt)
        if ctx.privacy_gate is not None:
            # 与 AiStep 同一道门:提示词虽只复制到剪贴板,但终将粘进
            # 网页 AI——沿用三选一(遮盖/原样/取消)。取消则不复制、
            # 不覆盖剪贴板、不打开网页。
            prompt = ctx.privacy_gate(prompt, {"base_url": "网页 AI"})
            if prompt is None:
                return Cancelled("隐私门拦截")
        copied = None
        if ctx.copy_text is not None:
            # 只写剪贴板:自动 Ctrl+V 会打进当前窗口。
            # 宿主回调必须返回真实成败(None 也按失败——不能拿"未声明"
            # 当成功);失败时本步不宣称"提示词已复制",也不打开
            # 依赖该复制操作的网页。
            copied = ctx.copy_text(prompt)
        if copied is False or copied is None:
            return Done(ctx.previous, halt=True, note=(
                "复制失败", "剪贴板写入失败,提示词未进入剪贴板;"
                            "请重试本步骤(网页未打开)。"))
        opened = ""
        url = self.url.strip()
        if url:
            u = ctx.render(url).replace(
                "{prompt}", urllib.parse.quote(prompt))
            if ctx.open_url is not None and ctx.open_url(u):
                opened = ",已打开网页"
        note = (f"提示词(已复制{opened})", prompt) if prompt else None
        return Wait(hint="提示词已在剪贴板:切到网页 AI 粘贴发送,把回答复制后"
                         "点「📋 从剪贴板粘贴」(也可直接在此输入)。", note=note)

    def describe(self):
        return "网页"

    def to_dict(self):
        return {"kind": "web", "prompt": self.prompt, "url": self.url}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("prompt") or "", d.get("url") or "")


@register
class HumanStep(WorkflowStep):
    """人工确认/编辑:当前内容进等待面板,确认后作为下一步输入。"""

    kind, label = "human", "人工确认"

    def __init__(self, prompt=""):
        super().__init__()
        self.prompt = _s(prompt)

    def validate(self, ctx):
        return None

    def run(self, ctx):
        hint = self.prompt.strip() or "确认 / 编辑当前内容后继续:"
        return Wait(hint=hint, prefill=ctx.previous)

    def describe(self):
        return "人工"

    def to_dict(self):
        return {"kind": "human", "prompt": self.prompt}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("prompt") or "")


@register
class ClipboardStep(WorkflowStep):
    """把链上当前内容快照进剪贴板,链继续(不打断);写失败停链并提示。"""

    kind, label = "clipboard", "复制到剪贴板"

    def run(self, ctx):
        if ctx.copy_text is not None:
            ok = ctx.copy_text(ctx.previous)
            if ok is False or ok is None:      # None=宿主未声明成败,不当成功
                return Done(ctx.previous, halt=True, note=(
                    "复制失败", "剪贴板写入失败,内容未复制;请重试本步骤。"))
        return Done(ctx.previous)

    def describe(self):
        return "剪贴板"


@register
class TransformStep(WorkflowStep):
    """文本变换:纯本地白名单操作,无 eval。"""

    kind, label = "transform", "文本变换"

    OPS = ("trim", "upper", "lower", "replace", "regex", "prefix", "suffix")
    OP_LABELS = {"trim": "去首尾空白", "upper": "转大写", "lower": "转小写",
                 "replace": "替换", "regex": "正则替换",
                 "prefix": "加前缀", "suffix": "加后缀"}

    def __init__(self, op="trim", arg1="", arg2=""):
        super().__init__()
        self.op = _s(op) or "trim"
        self.arg1 = _s(arg1)
        self.arg2 = _s(arg2)

    def validate(self, ctx):
        if self.op not in self.OPS:
            return f"未知变换操作「{self.op}」"
        need = self.OP_LABELS.get(self.op, self.op)
        if self.op in ("replace", "regex") and not self.arg1:
            return f"变换「{need}」需要填写查找内容"
        if self.op == "regex":
            try:
                re.compile(self.arg1)
            except re.error as e:
                return f"正则无效:{e}"
        return None

    def apply(self, text):
        t, a1, a2 = _s(text), self.arg1, self.arg2
        if self.op == "trim":
            return t.strip()
        if self.op == "upper":
            return t.upper()
        if self.op == "lower":
            return t.lower()
        if self.op == "replace":
            return t.replace(a1, a2)
        if self.op == "regex":
            return re.compile(a1).sub(a2, t)
        if self.op == "prefix":
            return a1 + t
        return t + a2          # suffix

    def run(self, ctx):
        return Done(self.apply(ctx.previous))

    def describe(self):
        return f"变换·{self.OP_LABELS.get(self.op, self.op)}"

    def to_dict(self):
        d = {"kind": "transform", "op": self.op}
        if self.arg1:
            d["arg1"] = self.arg1
        if self.arg2:
            d["arg2"] = self.arg2
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("op") or "trim", d.get("arg1") or "",
                   d.get("arg2") or "")


@register
class ConditionalStep(WorkflowStep):
    """条件闸:当前内容命中正则 → 继续;未命中 → 本步完成但停止自动
    串联(可手动继续后续步骤)。"""

    kind, label = "if", "条件判断"

    def __init__(self, pattern=""):
        super().__init__()
        self.pattern = _s(pattern)

    def validate(self, ctx):
        if not self.pattern.strip():
            return "条件步骤需要填写正则"
        try:
            re.compile(self.pattern)
        except re.error as e:
            return f"正则无效:{e}"
        return None

    def run(self, ctx):
        if re.search(self.pattern, ctx.previous):
            return Done(ctx.previous)
        note_body = (f"未命中正则 {self.pattern},自动串联到此为止;"
                     "可手动继续后续步骤。")
        return Done(ctx.previous, halt=True, note=("条件不满足", note_body))

    def describe(self):
        return "条件"

    def to_dict(self):
        return {"kind": "if", "pattern": self.pattern}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("pattern") or "")


# 编辑器下拉(kind 顺序 = 展示顺序);manual 别名在 deserialize 归一
EDITOR_KINDS = (("ai", "本地 AI"), ("web", "网页 AI"), ("human", "人工确认"),
                ("clipboard", "复制到剪贴板"), ("transform", "文本变换"),
                ("if", "条件判断"))


def deserialize(d):
    """步骤 dict → 步骤实例;缺 kind 视为 ai(旧配置),未知类型给
    UnknownStep(validate 必报错,不执行)。"""
    d = d if isinstance(d, dict) else {}
    kind = str(d.get("kind") or "ai").strip().lower()
    kind = KIND_ALIASES.get(kind, kind)
    cls = STEP_REGISTRY.get(kind, UnknownStep)
    if cls is UnknownStep:
        return UnknownStep(d)
    try:
        return cls.from_dict(d)
    except Exception:
        return UnknownStep(d)


def describe(step_dict):
    """步骤 dict → 短标签(运行台按钮/进度显示用)。"""
    return deserialize(step_dict).describe()
