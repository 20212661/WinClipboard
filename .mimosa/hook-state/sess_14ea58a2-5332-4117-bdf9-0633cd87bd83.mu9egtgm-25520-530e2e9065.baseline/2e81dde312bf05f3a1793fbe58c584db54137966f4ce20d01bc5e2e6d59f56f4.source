# -*- coding: utf-8 -*-
"""性能埋点:关键路径计时写独立的 perf.log,不混入 app.log。

用法:
    import perf_probe
    from perf_probe import PerfSpan

    with PerfSpan("history_filter"):
        ...

日志:%(APPDATA)s/WinClipboard/logs/perf.log(按天滚动,保留 5 份);
WCLB_PERF=0 环境变量可整体关闭。本模块只依赖标准库(clipboard_manager
与 pinboard 都要导入,不能引 Qt / cm),任何初始化失败都静默——
埋点绝不影响主流程。
"""
import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler

perf_log = logging.getLogger("winclipboard.perf")
perf_log.propagate = False        # 永不冒泡到根日志(即 app.log 所在通道)

_APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
LOG_DIR = os.path.join(_APPDATA, "WinClipboard", "logs")
LOG_FILE = os.path.join(LOG_DIR, "perf.log")


def setup():
    """挂接 perf.log 文件(幂等);main() 启动时调用一次。

    delay=True:首次写日志才建文件;未触发任何埋点的会话不产生空文件。
    """
    if os.environ.get("WCLB_PERF") == "0":
        perf_log.disabled = True
        return
    if perf_log.handlers:            # 已初始化
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = TimedRotatingFileHandler(
            LOG_FILE, when="midnight", backupCount=5,
            encoding="utf-8", delay=True)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s"))
        perf_log.addHandler(handler)
        perf_log.setLevel(logging.DEBUG)
    except Exception:
        perf_log.disabled = True     # 目录不可写等:静默关闭,不打扰主流程


class PerfSpan:
    """计时段上下文管理器;耗时以 <name> <ms>ms 记入 perf.log。"""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        perf_log.debug("%s %.2fms", self.name, ms)


def stamp():
    """跨点位计时的起点(配 since 用,如热键事件 → 首帧绘制)。"""
    return time.perf_counter()


def since(t0, name):
    """记录从 stamp() 到现在的耗时。"""
    perf_log.debug("%s %.2fms", name, (time.perf_counter() - t0) * 1000)
