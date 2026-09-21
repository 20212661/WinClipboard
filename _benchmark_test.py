# -*- coding: utf-8 -*-
"""性能预算基准(建议 33)—— 把"用户说卡了再定位"变成"改动前后各跑一次"。

预算表(产品目标;断言即预算。机器差异大时先看 p95 与相对漂移,再考虑调预算):
  捕获普通文本(含写盘)     <   50 ms   建议 33 预算表
  搜索大库(子串, N 条)      <  200 ms   建议 33 目标 100ms;SQLite LIKE 全表
                                       子串扫描实测中位 ~120ms(2026-09 本机),
                                       预算=基线×余量;要破 100ms 须切 FTS
                                       词级语义(触发器已在维护),属行为变更
  分页查询(200 条/页)       <  100 ms   建议 33「条件切换 <100ms」同级
  内存过滤 1 万条(关键词)    <  100 ms   建议 33「条件切换 <100ms」
  核心启动载入 2 万条        < 1000 ms   建议 33「普通启动 <1s」的核心路径部分

运行: python _benchmark_test.py [N]     N = 大库条数,默认 100000
退出码 0 = 全部在预算内。种子库写入临时目录,不碰真实数据。
"""
import os
import queue
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm
import store as st

# ---- 隔离沙箱 ----
TMP = tempfile.mkdtemp(prefix="wclb_bench_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.BLOB_DIR = os.path.join(TMP, "data", "blobs")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
cm.DB_FILE = os.path.join(TMP, "clipboard.db")
cm.BOOT_STATE_FILE = os.path.join(TMP, "boot_state.json")

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def _mini_core(store=None, history=None, max_items=200000):
    """_core_test.make_core 的基准版:绕过 __init__,不启动线程不碰剪贴板。"""
    core = cm.ClipboardCore.__new__(cm.ClipboardCore)
    core.cfg = dict(cm.DEFAULT_CFG)
    core.max_items = max_items
    core.history = history if history is not None else []
    core.private = []
    core.signal_q = queue.Queue()
    core.worker = SimpleNamespace(cmd_q=queue.Queue())
    core._set_clipboard = lambda rec: None
    core.monitor_paused = False
    core._exclude_set = set()
    core.store = store
    return core


def _bench(fn, repeat=5, warmup=1):
    """重复计时:返回 (中位 ms, p95 ms)。先空跑一遍热页缓存/字典。"""
    for _ in range(warmup):
        fn()
    xs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000.0)
    xs.sort()
    return statistics.median(xs), xs[max(0, int(round(repeat * 0.95)) - 1)]


def _report(title, med, p95, budget_ms):
    ok = med <= budget_ms
    print("  %-28s 中位 %8.2f ms   p95 %8.2f ms   预算 %6.0f ms   %s"
          % (title, med, p95, budget_ms, "OK" if ok else "超预算"))
    check("预算·%s ≤ %dms" % (title, budget_ms), ok,
          "中位 %.1fms / p95 %.1fms" % (med, p95))


def main():
    n_big = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() \
        else 100000
    n_start = 20000
    t0 = time.perf_counter()
    big_db = os.path.join(TMP, "big.db")
    bs = st.ClipboardStore(big_db)
    rows = [{"type": "text", "pinned": False,
             "ts": 1_000_000.0 + i,
             "content": ("搜索标记条目 %06d 这是用于子串检索基准的中文文本段落,"
                         "长度接近日常复制的普通片段。" % i)
             if i % 10 == 0 else
             ("普通内容 %06d 一些不带关键词的日常文本,用来撑大库容量并验证"
              "全表扫描路径。" % i)}
            for i in range(n_big)]
    bs.migrate_from_list(rows)            # 单事务批量:种子快
    print("种子: 大库 %d 条 / 启动库 %d 条,建库 %.1fs"
          % (n_big, n_start, time.perf_counter() - t0))

    # B1 捕获普通文本(管线决策 + 库写盘 + FTS 触发器)
    core = _mini_core(store=bs)
    cap = _bench(lambda: core.add_item(
        ("text", "基准捕获 %f 一次普通文本写入" % time.time(), {}, "")),
        repeat=30)
    _report("捕获普通文本(写盘)", cap[0], cap[1], 50)

    # B2 搜索大库(子串语义 = LIKE 全表扫描;预算 200ms = 实测基线×余量,
    #    见模块头:破 100ms 需切 FTS 词级语义,属行为变更另行决策)
    srch = _bench(lambda: bs.query(q="搜索标记条目"), repeat=5)
    _report("搜索 %d 条(子串)" % n_big, srch[0], srch[1], 200)

    # B3 分页查询
    page = _bench(lambda: bs.query_page(limit=200), repeat=5)
    _report("分页查询(200 条/页)", page[0], page[1], 100)

    # B4 内存过滤(1 万条,关键词)
    hist = [{"type": "text", "content":
             ("内存搜索标记 %05d 过滤基准条目" % i) if i % 5 == 0
             else ("普通内存条目 %05d" % i), "pinned": False, "ts": float(i)}
            for i in range(10000)]
    core2 = _mini_core(history=hist)
    fv = _bench(lambda: core2.filter_view("all", "内存搜索标记"), repeat=5)
    _report("内存过滤 1 万条(关键词)", fv[0], fv[1], 100)

    # B5 核心启动载入(2 万条:load_all 全量重建内存历史)
    start_db = os.path.join(TMP, "start.db")
    ss = st.ClipboardStore(start_db)
    ss.migrate_from_list([{"type": "text", "pinned": False,
                           "ts": 2_000_000.0 + i,
                           "content": "启动载入条目 %06d" % i}
                          for i in range(n_start)])
    ss.close()
    def _load_once():
        s = st.ClipboardStore(start_db)
        try:
            return s.load_all()
        finally:
            s.close()

    load = _bench(lambda: len(_load_once()), repeat=3)
    _report("核心启动载入 %d 条" % n_start, load[0], load[1], 1000)

    bs.close()
    print()
    print("RESULT:", "PASS" if not FAILS else "FAIL %d 项: %s"
          % (len(FAILS), ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
