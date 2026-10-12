# -*- coding: utf-8 -*-
"""性能优化语义回归(2026-10-07):优化不得改变行为,只许改变成本。

覆盖:
  A. 捕获去重索引与线性扫描在完整变更序列下逐条等价
  B. _search_cache 有界失效 / 未置顶计数 / dbid 位置映射失效
  C. query_view 限量前缀一致(SQL+内存)、链接与子类型分批不遗漏
  D. 渲染批次代数:快速连续搜索不回填旧视图;切换分类再返回选择正常
  E. 滑动窗口:深滚动后控件数有界、滚动回顶补回、身份映射正确
  F. 缩略图:字节预算淘汰统一、可见优先队列、失败路径、预热限量
  G. 来源下拉集合未变跳过重建

运行: python _perf_regression_test.py     退出码 0 = 全部通过
"""
import os
import sys
import queue
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="wclb_perfr_")
import clipboard_manager as cm

cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.BLOB_DIR = os.path.join(TMP, "data", "blobs")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
cm.DB_FILE = os.path.join(TMP, "clipboard.db")
cm.BOOT_STATE_FILE = os.path.join(TMP, "boot_state.json")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

import pipeline

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def mini_core(store=None, history=None, max_items=10 ** 9):
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
    core._search_cache = {}
    return core


# ============================================================
# A. 指纹索引与线性扫描等价(完整变更序列)
# ============================================================
def part_a():
    core = mini_core(max_items=10 ** 9)
    hist = core.history

    def scan_outcome(item):
        """旧实现:不带索引的纯扫描决策。"""
        snap = pipeline.Snapshot.from_item(item)
        out, rec, _tr = pipeline.run_capture(snap, core.cfg, hist,
                                             sha1_fallback=None)
        return out

    def idx_outcome(item):
        snap = pipeline.Snapshot.from_item(item)
        core._fp_lookup(None) if getattr(core, "_fp_index", None) is None \
            else None
        out, rec, _tr = pipeline.run_capture(
            snap, core.cfg, hist, sha1_fallback=None,
            dedup_lookup=core._fp_lookup, dedup_kinds=core._dedup_kinds())
        return out

    items = [
        ("text", "hello world", {}, "a.exe"),
        ("text", "second entry", {}, ""),
        ("files", [r"C:\x\f1.txt"], "", ),
        ("text", "hello world", {}, "b.exe"),      # 重复(提升)
        ("text", "{\"json\": true}", {}, ""),
        ("files", [r"C:\x\f1.txt", r"C:\x\f2.txt"], "", ),
        ("text", "third", {}, ""),
    ]
    for it in items:
        o1, o2 = scan_outcome(it), idx_outcome(it)
        check("A 索引等价·%s → %s" % (it[1][:18], o1.kind),
              o1.kind == o2.kind
              and (o1.rec is None) == (o2.rec is None))
        core.add_item(it)

    # 置顶后重复 → PINNED_SKIP
    hist[0]["pinned"] = True
    o1 = scan_outcome(("text", "third", {}, ""))
    core._reset_hist_caches()          # 置顶不改成员,但显式重建保证干净
    o2 = idx_outcome(("text", "third", {}, ""))
    check("A 置顶重复跳过等价", o1.kind == o2.kind == pipeline.PINNED_SKIP,
          "%s/%s" % (o1.kind, o2.kind))

    # 删除后同内容再来 → KEEP(索引必须已除名)
    hist_dup = next(h for h in core.history
                    if h.get("content") == "hello world")
    core.history[:] = [h for h in core.history if h is not hist_dup]
    core._hist_removed([hist_dup])
    o1 = scan_outcome(("text", "hello world", {}, ""))
    o2 = idx_outcome(("text", "hello world", {}, ""))
    check("A 删除后索引除名", o1.kind == o2.kind == pipeline.KEEP,
          "%s/%s" % (o1.kind, o2.kind))
    check("A 变更序列后索引不超历史规模",
          len(core._fp_index) <= len(core.history),
          "%d/%d" % (len(core._fp_index), len(core.history)))


# ============================================================
# B. 检索缓存 / 未置顶计数 / 位置映射
# ============================================================
def part_b():
    core = mini_core(max_items=10 ** 9)
    for i in range(600):
        core.history.append({"type": "text", "content": "row-%04d" % i,
                             "pinned": i % 100 == 0, "ts": float(i)})
        core.history[-1]["_db"] = 10000 + i
    # _search_cache 有界:超 512 淘汰最旧的一半,不清空全部
    for h in core.history[:520]:
        core._search_text(h)
    check("B 检索缓存有界(≤512)", len(core._search_cache) <= 512,
          str(len(core._search_cache)))
    check("B 检索缓存保留最近条目", len(core._search_cache) >= 250)
    # 未置顶计数
    expect = sum(1 for h in core.history if not h.get("pinned"))
    check("B 未置顶计数正确", core._unpinned_total() == expect,
          "%d/%d" % (core._unpinned_total(), expect))
    removed = core.history[:10]
    core.history[:] = core.history[10:]
    core._hist_removed(removed)
    expect -= sum(1 for h in removed if not h.get("pinned"))
    check("B 移除后计数重算", core._unpinned_total() == expect,
          "%d/%d" % (core._unpinned_total(), expect))
    # dbid 位置映射 + 失效(经失效钩子;容器直接改不经钩子属契约外)
    m = core._dbid_pos()
    check("B 位置映射缓存命中", core._dbid_pos() is m)
    core.history.insert(0, {"type": "text", "content": "new", "ts": 1.0,
                            "_db": 999})
    core._hist_reordered()
    check("B 插入后映射失效重建",
          core._dbid_pos() is not m and core._dbid_pos()[999] == 0)


# ============================================================
# C. query_view 限量前缀一致 + 分批不遗漏
# ============================================================
def part_c():
    import store as st
    hist = []
    for i in range(1000):
        r = i % 5
        if r == 0:
            hist.append({"type": "text",
                         "content": "https://example.com/page-%04d" % i,
                         "ts": 1_000_000.0 + i, "src": "chrome.exe"})
        elif r == 3:
            hist.append({"type": "text", "content": '{"json_key": %d}' % i,
                         "ts": 1_000_000.0 + i})
        else:
            hist.append({"type": "text", "content": "普通行 %04d" % i,
                         "ts": 1_000_000.0 + i})
    core_mem = mini_core(history=list(hist))

    # 内存模式:limit = 全量结果前缀
    full = core_mem.query_view("all", "")
    lim = core_mem.query_view("all", "", limit=60)
    check("C 内存 limit 前缀一致", lim == full[:60])
    full_link = core_mem.query_view("link", "")
    lim_link = core_mem.query_view("link", "", limit=7)
    check("C 内存 link limit 前缀一致", lim_link == full_link[:7])

    # SQL 模式:种库 + _db 回填
    bs = st.ClipboardStore(os.path.join(TMP, "c.db"))
    bs.migrate_from_list(list(reversed(hist)))
    loaded = bs.load_all()
    for a, b in zip(hist, loaded):
        a["_db"] = b["_db"]
    core_sql = mini_core(store=bs, history=hist)

    full = core_sql.query_view("all", "")
    lim = core_sql.query_view("all", "", limit=60)
    check("C SQL limit 前缀一致", lim == full[:60],
          "full0=%s lim0=%s" % (full[:1], lim[:1]))
    # 链接分类(需内存二次过滤)分批读取不遗漏:
    # 全量 link 与 limit=60 相当于前 60;再验证 limit 很大时与全量一致
    full_link = core_sql.query_view("link", "")
    lim_link = core_sql.query_view("link", "", limit=60)
    check("C SQL link 分批 limit 前缀一致", lim_link == full_link[:60])
    big = core_sql.query_view("link", "", limit=10 ** 6)
    check("C SQL link 大 limit 与全量一致", big == full_link,
          "%d/%d" % (len(big), len(full_link)))
    # 子类型过滤(内存二次判断)同样不遗漏
    full_json = core_sql.query_view("all", "type:json")
    lim_json = core_sql.query_view("all", "type:json", limit=3)
    big_json = core_sql.query_view("all", "type:json", limit=10 ** 6)
    check("C SQL type:json 限量/全量一致",
          lim_json == full_json[:3] and big_json == full_json,
          "%d/%d" % (len(big_json), len(full_json)))
    # 交错稀疏场景:候选批次跨多批(链接占比 1/5,batch=400 → 每批 ~80)
    check("C SQL link 总数正确", len(full_link) == 200,
          str(len(full_link)))
    # 快捷浮层语义:限量结果 = 全量前缀(两种存储模式)
    for cat in ("all", "text", "link"):
        f = core_sql.query_view(cat, "")
        l = core_sql.query_view(cat, "", limit=60)
        check("C 浮层前缀一致·SQL %s" % cat, l == f[:60])
        f2 = core_mem.query_view(cat, "")
        l2 = core_mem.query_view(cat, "", limit=60)
        check("C 浮层前缀一致·内存 %s" % cat, l2 == f2[:60])
    bs.close()


# ============================================================
# D/E/F/G. UI 层(离屏)
# ============================================================
def ui_parts():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEventLoop, QPoint
    import winclipboard_qt as wq

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    wq.apply_fonts(app)

    N = 900
    hist = []
    for i in range(N):
        if i % 25 == 7:
            hist.append({"type": "text",
                         "content": "https://ex.com/%04d" % i,
                         "ts": 1_700_000.0 + i})
        else:
            hist.append({"type": "text", "content": "行 %04d 文本" % i,
                         "ts": 1_700_000.0 + i})
    hist[3]["pinned"] = True
    hist[50]["pinned"] = True
    core = mini_core(history=hist)

    class FakeApp(SimpleNamespace):
        pass

    fake = FakeApp(cfg={}, history=hist, core=core, _view=[], selected=0,
                   _card_widgets=[], _badge_cache={}, _thumbs={},
                   _thumbs_bytes=[0], _thumb_loader=None, src_filter="",
                   quick=None)
    fake.thumb_cached = lambda p: fake._thumbs.get(p)
    fake._thumbs_put = lambda p, pm: fake._thumbs.__setitem__(p, pm)
    fake.request_thumb = lambda p, visible=True: None
    for m in ("card_action", "select_card", "move_sel", "paste_selected",
              "set_category", "set_src_filter", "set_time_filter",
              "reset_filters", "on_search"):
        setattr(fake, m, lambda *a, k=m: None)

    def drain(p, timeout=30):
        t1 = time.perf_counter()
        while p._render_pending:
            app.processEvents(QEventLoop.AllEvents, 50)
            if time.perf_counter() - t1 > timeout:
                break

    panel = wq.HistoryPage(fake)
    panel.resize(760, 720)
    panel.show()
    app.processEvents()

    # D:快速连续搜索 → 旧批次不污染新视图
    view_a = core.filter_view("all", "行 00")
    panel.set_view(view_a, hist)               # 大视图,大量 pending
    assert panel._render_pending, "前置:批次中存在待建卡片"
    view_b = core.filter_view("all", "https")
    panel.set_view(view_b, hist)               # 立即切换 → 旧批次作废
    drain(panel)
    ids_b = {id(c.rec) for c in panel._cards}
    recs_b = {id(r) for r in [hist[i] for i in view_b][:len(panel._cards)]}
    leaked = [c for c in panel._cards if id(c.rec) not in recs_b
              and c.rec.get("content").startswith("行 ")]
    check("D 快速切换后无旧视图记录回填", not leaked, str(len(leaked)))
    check("D 切换后卡片全部属于新视图", ids_b <= recs_b or not leaked)

    # D2:切换分类再返回:条目与选择正常
    full = core.filter_view("all", "")
    panel.set_view(full, hist)
    drain(panel)
    fake._view = full
    panel.select_index(0)
    sel_rec = panel._cards[0].rec
    cat_view = core.filter_view("link", "")
    panel.set_view(cat_view, hist)
    drain(panel)
    panel.set_view(full, hist)
    drain(panel)
    check("D2 分类往返后首卡身份一致",
          any(c.rec is sel_rec for c in panel._cards))
    panel.select_index(5)
    vi = 5
    card = panel.card_at(vi)
    check("D2 card_at 按身份映射", card is not None
          and card.rec is hist[full[vi]])

    # E:滑动窗口——深滚动扩展后控件数有界
    sb = panel.scroll.verticalScrollBar()
    while panel._win_limit < N:
        panel._extend_window()
        drain(panel)
        sb.setValue(sb.maximum())
        app.processEvents()
    total_widgets = len(panel._cards) + len(panel._idle)
    check("E 深滚动后控件数有界(≤ WIN_MAX+IDLE_CAP+chunk)",
          total_widgets <= wq.HistoryPage.WIN_MAX
          + wq.HistoryPage.IDLE_CAP + panel._win_chunk + 60,
          "cards=%d idle=%d lo=%d" % (len(panel._cards), len(panel._idle),
                                      panel._win_lo))
    check("E 滑窗推进(_win_lo > 0)", panel._win_lo > 0,
          "lo=%d limit=%d N=%d" % (panel._win_lo, panel._win_limit, N))
    # 置顶卡片不被释放
    pinned_cards = [c for c in panel._cards if c.rec.get("pinned")]
    pinned_total = sum(1 for r in hist if r.get("pinned"))
    check("E 置顶卡片保留", len(pinned_cards) == pinned_total,
          "%d/%d" % (len(pinned_cards), pinned_total))
    # 滚动回顶 → prepend 补回
    sb.setValue(0)
    app.processEvents()
    lo_before = panel._win_lo
    panel._prepend_window()
    app.processEvents()
    check("E 回顶补回窗口(lo 减小)",
          panel._win_lo < lo_before or lo_before == 0,
          "lo %d → %d" % (lo_before, panel._win_lo))
    if lo_before > 0:
        back_recs = [hist[i] for i in
                     range(panel._win_lo, min(lo_before, panel._win_limit))]
        have = {id(c.rec) for c in panel._cards}
        check("E 补回段记录全部物化",
              all(id(r) in have for r in back_recs),
              "缺 %d/%d" % (sum(1 for r in back_recs if id(r) not in have),
                            len(back_recs)))

    # F:缩略图缓存字节预算(真实 QtApp 方法逻辑通过子类替身验证)
    from PySide6.QtGui import QPixmap

    class ThumbHost(SimpleNamespace):
        THUMB_CACHE_BUDGET = 48 * 1024 * 1024
        THUMB_CACHE_COUNT = 400

    host = ThumbHost(_thumbs={}, _thumbs_bytes=0)
    WApp = wq.WinClipboardApp
    WApp._thumbs_put(host, "a", QPixmap(256, 256))         # 256KB
    WApp._thumbs_put(host, "b", QPixmap(256, 256))
    big_budget = host._thumbs_bytes
    check("F 缓存字节记账", big_budget == 2 * 256 * 256 * 4,
          str(big_budget))
    host.THUMB_CACHE_BUDGET = 300 * 1024          # 实例级压小预算(测试用)
    try:
        for i in range(8):
            WApp._thumbs_put(host, "k%d" % i, QPixmap(256, 256))
        check("F 超预算触发淘汰", host._thumbs_bytes <= 300 * 1024,
              str(host._thumbs_bytes))
        check("F 条目数相应受限", len(host._thumbs) <= 3,
              str(len(host._thumbs)))
    finally:
        host.THUMB_CACHE_BUDGET = WApp.THUMB_CACHE_BUDGET
    # LRU 触碰:thumb_cached 命中移尾
    host._thumbs.clear()
    host._thumbs_bytes = 0
    WApp._thumbs_put(host, "x", QPixmap(64, 64))
    WApp._thumbs_put(host, "y", QPixmap(64, 64))
    fake._thumbs = host._thumbs
    WApp.thumb_cached(host, "x")
    check("F LRU 触碰(命中项移到尾部)",
          next(iter(host._thumbs)) == "y" and list(host._thumbs)[-1] == "x")

    # F2:ThumbLoader 可见优先 + 失败表 + 关停
    loader = wq.ThumbLoader(fake)
    fake._thumb_loader = loader
    from PIL import Image
    ok_png = os.path.join(TMP, "ok.png")
    Image.new("RGB", (60, 40), (10, 200, 30)).save(ok_png)
    bad = os.path.join(TMP, "bad.png")
    Path(bad).write_bytes(b"not an image")
    for i in range(300):                      # 预热超载(上限 240)
        loader.submit("prewarm-%03d" % i, visible=False)
    loader.submit(ok_png, visible=True)
    loader.submit(bad, visible=True)
    q_visible_first = loader._q.queue[0] if loader._q.qsize() else None
    check("F2 可见请求排最前", q_visible_first is not None
          and q_visible_first[2] == ok_png, str(q_visible_first))
    check("F2 预热超载被丢弃", loader._q.qsize() <= 240 + 2,
          str(loader._q.qsize()))
    t1 = time.time()
    while (ok_png in loader._pending or bad in loader._pending) \
            and time.time() - t1 < 10:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()          # 投递 worker 线程排队的 loaded 信号
    check("F2 成功解码进缓存", ok_png in fake._thumbs,
          "pending=%s failed=%s" % (ok_png in loader._pending,
                                    ok_png in loader._failed))
    check("F2 失败路径进失败表(不卡 pending)",
          bad in loader._failed and bad not in loader._pending)
    n_pending_before = loader._q.qsize()
    loader.shutdown()
    check("F2 shutdown 排空队列", loader._q.qsize() == 0,
          "before=%d" % n_pending_before)
    fake._thumb_loader = None

    # G:来源下拉集合未变跳过重建
    rebuilds = []
    orig_clear = panel.cmb_src.clear

    def counting_clear():
        rebuilds.append(1)
        orig_clear()

    panel.cmb_src.clear = counting_clear
    panel.sync_src_combo()
    panel.sync_src_combo()
    check("G 来源集合未变跳过重建", len(rebuilds) == 1, str(len(rebuilds)))
    core._src_apps_cache = None               # 强制集合变化
    hist[0]["src"] = "newapp.exe"
    core._src_apps_cache = None
    panel.sync_src_combo()
    check("G 集合变化触发重建", len(rebuilds) == 2)
    panel.cmb_src.clear = orig_clear


def main():
    t0 = time.time()
    part_a()
    part_b()
    part_c()
    ui_parts()
    print()
    print("RESULT:", "PASS" if not FAILS else "FAIL %d 项: %s"
          % (len(FAILS), ", ".join(FAILS)))
    print("耗时 %.1fs" % (time.time() - t0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
