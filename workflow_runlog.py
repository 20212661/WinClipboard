# -*- coding: utf-8 -*-
"""Workflow 运行记录(Run Inspector 数据层,建议 18)—— 纯 Python,无 Qt。

运行台(WorkflowRunner)把每步的 input/output 大小、耗时、状态喂给
RunRecorder;结束时 finish() 出整条 run 记录,save_run 落进
workflow_runs.json(最近在前,默认保留 50 条)。查看入口在工作流页
「运行记录」。

隐私约定(与 SafeLogger 同一红线):
- 只记大小,不记内容——input/output 一律 len(),正文永不落盘;
- 错误文案经 cm.sanitize_log 脱敏后截断 220 字符(服务端报错可能回显
  请求内容,不能整段进记录)。
"""
import json
import os
import time
from pathlib import Path

import clipboard_manager as cm

KEEP_RUNS = 50          # 历史上限(超出丢最旧)
_ERR_MAX = 220          # 错误文案截断


def _runs_path(path=None):
    """运行历史文件路径。调用时解析(不固化为模块常量):测试改写
    cm.APP_DIR 后导入顺序不影响落盘位置。"""
    return str(path or os.path.join(cm.APP_DIR, "workflow_runs.json"))


class RunRecorder:
    """一次工作流运行的现场记录器(内存态;落盘走 save_run)。"""

    def __init__(self, workflow, input_text, total_steps):
        self.workflow = str(workflow or "")
        self.total_steps = int(total_steps or 0)
        self.started_at = time.time()
        self.finished_at = None
        self.status = "running"
        self.input_chars = len(str(input_text or ""))
        self.steps = {}      # idx -> 步骤记录 dict
        self._t0 = {}        # idx -> time.monotonic()

    # -- 步骤事件(运行台调用;同 idx 重跑即覆盖 = 只留最后一次尝试) --
    def step_start(self, idx, kind, label, input_text):
        self.steps[idx] = {"idx": int(idx), "kind": str(kind or ""),
                           "label": str(label or ""), "status": "running",
                           "input_chars": len(str(input_text or "")),
                           "output_chars": 0, "ms": None, "error": ""}
        self._t0[int(idx)] = time.monotonic()

    def step_ok(self, idx, output_text, halt=False):
        self._end(idx, "halt" if halt else "ok", output_text, "")

    def step_error(self, idx, message):
        self._end(idx, "error", "", message)

    def step_cancelled(self, idx, reason=""):
        self._end(idx, "cancelled", "", reason)

    def _end(self, idx, status, output_text, error):
        st = self.steps.get(int(idx))
        if st is None:
            return
        t0 = self._t0.pop(int(idx), None)
        st["status"] = status
        st["output_chars"] = len(str(output_text or ""))
        if error:
            st["error"] = cm.sanitize_log(str(error))[:_ERR_MAX]
        if t0 is not None:
            st["ms"] = round((time.monotonic() - t0) * 1000.0, 1)

    # -- 整条运行收尾 --
    def finish(self):
        """正常走完(或停链后关闭)收尾;状态由步骤状态推导。"""
        self.finished_at = time.time()
        sts = [s["status"] for s in self._ordered()]
        if "error" in sts:
            self.status = "failed"
        elif "cancelled" in sts:
            self.status = "cancelled"
        elif len(sts) == self.total_steps \
                and all(s in ("ok", "halt") for s in sts):
            self.status = "done"
        else:
            self.status = "partial"
        return self.record()

    def abort(self):
        """中途关闭运行台:未到终点的运行记 partial。"""
        self.finished_at = time.time()
        self.status = "partial"
        return self.record()

    def _ordered(self):
        return [self.steps[i] for i in sorted(self.steps)]

    def record(self):
        out_chars = 0
        for s in self._ordered():
            if s["status"] in ("ok", "halt"):
                out_chars = s["output_chars"]
        return {"id": None, "workflow": self.workflow,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "status": self.status, "total_steps": self.total_steps,
                "input_chars": self.input_chars, "output_chars": out_chars,
                "steps": [dict(s) for s in self._ordered()]}


def load_runs(path=None):
    """读运行历史(最近在前);文件缺失/损坏返回 [](损坏不覆盖,人工可查)。"""
    try:
        runs = json.loads(Path(_runs_path(path)).read_text(encoding="utf-8"))
        return [r for r in runs if isinstance(r, dict)]
    except Exception:
        return []


def save_run(run, path=None, keep=KEEP_RUNS):
    """追加一条运行记录(自动分配递增 id,最近在前,截断 keep 条);
    返回分配的 id。"""
    runs = load_runs(path)
    run["id"] = max((int(r.get("id") or 0) for r in runs), default=0) + 1
    runs.insert(0, run)
    del runs[max(0, int(keep)):]
    Path(_runs_path(path)).write_text(
        json.dumps(runs, ensure_ascii=False), encoding="utf-8")
    return run["id"]


def clear_runs(path=None):
    """清空运行历史(查看对话框「清空」用)。"""
    Path(_runs_path(path)).write_text("[]", encoding="utf-8")
