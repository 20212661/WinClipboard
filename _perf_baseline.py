# -*- coding: utf-8 -*-
"""性能优化基线/回归测量(隔离临时数据,不碰真实剪贴板与用户数据)。

运行:
    python _perf_baseline.py [tag]      tag 写进输出行首,如 before/after

测量场景(与 2026-10-07 性能优化任务的验收指标一一对应):
  U1 首批卡片构建(冷,60 卡窗口)        —— 首次打开是否秒级阻塞
  U2 热缓存重复刷新                       —— 无效刷新成本
  U3 搜索切换到新视图                     —— 过滤+重排成本
  U4 连续扩展到 500 / 2000 卡             —— 控件数量与刷新成本
  U5 快捷浮层刷新(query_view limit=60)   —— 候选读取量
  U6 来源下拉同步(source_apps 全扫)
  C1 内存模式 query_view 全量/限量(N 条)
  C2 SQL 模式 query_view 全量/限量(N 条)  —— pos 映射重建成本
  C3 满历史新增 / 重复提升 / 容量淘汰
  T1 缩略图预热队列深度 / 缓存无上限增长
"""
import os
import sys
import queue
import statistics
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEventLoop   # noqa: E402  (后台批次排空用)

TAG = sys.argv[1] if len(sys.argv) > 1 else "run"

# ---- 隔离沙箱(先改路径再 import cm) ----
TMP = tempfile.mkdtemp(prefix="wclb_perf_%s_" % TAG)
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

N_HIST = 4000            # UI 场景历史条数
N_BIG = 20000            # 核心查询场景条数
N_IMG = 300              # 独立图片数(预热队列压力)

RESULTS = []


def rec_line(name, value, unit="", note=""):
    line = "%-8s %-44s %12s %-4s %s" % (TAG, name, value, unit, note)
    print(line)
    RESULTS.append(line)


def bench(fn, repeat=7, warmup=2):
    """计时:先预热再重复,返回 (样本数, 中位ms, p95ms, 最大ms)。"""
    for _ in range(warmup):
        fn()
    xs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000.0)
    xs.sort()
    p95 = xs[min(len(xs) - 1, max(0, int(round(len(xs) * 0.95)) - 1))]
    return len(xs), statistics.median(xs), p95, xs[-1]


def make_images(n):
    """生成 n 张真实 PNG(小图,解码单张 ~1-3ms)。"""
    from PIL import Image, ImageDraw
    paths = []
    for i in range(n):
        p = os.path.join(cm.IMAGE_DIR, "perf%04d.png" % i)
        if not os.path.exists(p):
            img = Image.new("RGB", (160, 120),
                            (40 + i % 200, 90 + i % 120, 160))
            d = ImageDraw.Draw(img)
            d.rectangle([8, 8, 150, 100], outline=(255, 255, 255), width=2)
            img.save(p, "PNG")
        paths.append(p)
    return paths


def make_history(n, img_paths):
    """混合历史:文本为主,穿插链接/图片/文件,含少量置顶。"""
    hist = []
    for i in range(n):
        r = i % 20
        if r == 5 and img_paths:
            hist.append({"type": "image",
                         "content": img_paths[i % len(img_paths)],
                         "w": 160, "h": 120, "ts": 1_700_000.0 + i})
        elif r == 11:
            hist.append({"type": "text", "content":
                         "https://example.com/docs/page-%06d?tab=main" % i,
                         "ts": 1_700_000.0 + i})
        elif r == 17:
            hist.append({"type": "files",
                         "content": [os.path.join(TMP, "doc%04d.txt" % i)],
                         "ts": 1_700_000.0 + i})
        else:
            hist.append({"type": "text",
                         "content": "普通剪贴板条目 %06d:一段中等长度的"
                                    "日常中文文本,用于撑起列表与搜索。" % i,
                         "ts": 1_700_000.0 + i, "src": "notePad.exe"
                         if i % 3 else "chrome.exe"})
    hist[3]["pinned"] = True
    hist[23]["pinned"] = True
    return hist


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
# UI 场景(离屏 QWidget)
# ============================================================
def ui_scenarios(hist, img_paths):
    from PySide6.QtWidgets import QApplication
    import winclipboard_qt as wq

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    wq.apply_fonts(app)

    core = mini_core(history=hist)

    class FakeApp(SimpleNamespace):
        pass

    fake = FakeApp(
        cfg={}, history=hist, core=core, _view=[], selected=0,
        _card_widgets=[], _badge_cache={}, _thumbs={}, _thumb_loader=None,
        src_filter="", icon=None, quick=None)

    def thumb_cached(path):
        return fake._thumbs.get(path)

    def thumbs_put(path, pm):
        old = fake._thumbs.pop(path, None)
        fake._thumbs[path] = pm

    fake.thumb_cached = thumb_cached
    fake._thumbs_put = thumbs_put
    fake.request_thumb = lambda path, visible=True: None
    fake.card_action = lambda *a: None
    fake.select_card = lambda *a: None
    fake.move_sel = lambda *a: None
    fake.on_search = lambda text: None
    fake.open_viewer = lambda *a: None
    fake.delete_selected = lambda *a: None
    fake.paste_selected = lambda *a: None
    fake.set_category = lambda *a: None
    fake.set_src_filter = lambda *a: None
    fake.set_time_filter = lambda *a: None
    fake.reset_filters = lambda *a: None

    panel = wq.HistoryPage(fake)
    panel.resize(760, 720)
    panel.show()
    app.processEvents()

    view = core.filter_view("all", "")

    def drain(p, timeout=30):
        """排空后台渲染批次(0ms 定时器逐批推进)。"""
        t1 = time.perf_counter()
        while p._render_pending:
            app.processEvents(QEventLoop.AllEvents, 50)
            if time.perf_counter() - t1 > timeout:
                break

    def u1_once():
        p = wq.HistoryPage(fake)
        p.resize(760, 720)
        t0 = time.perf_counter()
        p.set_view(view, hist)          # 同步阻塞段(首屏可见区)
        dt = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        while p._render_pending:        # 后台批次排空(0ms 定时器逐批)
            app.processEvents(QEventLoop.AllEvents, 50)
            if time.perf_counter() - t1 > 30:
                break
        dt_total = (time.perf_counter() - t0) * 1000
        n_mat = len(p._cards)
        p.hide()
        p.deleteLater()
        app.processEvents()
        return dt, dt_total, n_mat

    cold = [u1_once() for _ in range(5)]
    rec_line("U1 首屏同步阻塞 ms(可见区优先)", "%.1f"
             % statistics.median([c[0] for c in cold]),
             note="max=%.1f n=%d" % (max(c[0] for c in cold), len(cold)))
    rec_line("U1b 冷构建总完成 ms(含后台批次)", "%.1f"
             % statistics.median([c[1] for c in cold]),
             note="物化卡片=%d" % cold[-1][2])

    n, med, p95, mx = bench(lambda: (panel.set_view(view, hist), drain(panel)))
    rec_line("U2 热缓存重复刷新 ms(含批次排空)", "%.2f" % med,
             note="p95=%.2f max=%.2f n=%d" % (p95, mx, n))

    # U3 搜索过滤切换
    def u3():
        v = core.filter_view("all", "普通剪贴板条目 001")
        panel.set_view(v, hist)
        drain(panel)
        panel.set_view(view, hist)
        drain(panel)

    n, med, p95, mx = bench(u3, repeat=5)
    rec_line("U3 搜索切换往返 ms(过滤+两次重排)", "%.1f" % med,
             note="p95=%.1f n=%d" % (p95, n))

    # U4 扩展窗口到 500 / 2000(模拟用户滚动到底:滑窗上限生效)
    panel.set_view(view, hist)
    drain(panel)

    def scroll_bottom():
        sb = panel.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())       # 模拟滚动到底(触发自动扩展与滑窗)
        app.processEvents()

    t0 = time.perf_counter()
    while panel._win_limit < 500:
        panel._extend_window()
        drain(panel)
        scroll_bottom()
    dt500 = (time.perf_counter() - t0) * 1000
    n_widgets_500 = len(panel._cards) + len(panel._idle)
    app.processEvents()
    t0 = time.perf_counter()
    while panel._win_limit < 2000:
        panel._extend_window()
        drain(panel)
        scroll_bottom()
    dt2000 = (time.perf_counter() - t0) * 1000
    n_widgets_2000 = len(panel._cards) + len(panel._idle)
    rec_line("U4 扩展到500卡 累计ms(滚动到底+批次)", "%.1f" % dt500,
             note="控件=%d" % n_widgets_500)
    rec_line("U4b 扩展到2000卡 累计ms(500→2000)", "%.1f" % dt2000,
             note="控件=%d(物化=%d 滑窗上限=%d)"
             % (n_widgets_2000, len(panel._cards), panel.WIN_MAX))

    # 单次 extend(+80)成本:同步阻塞段
    t0 = time.perf_counter()
    panel._extend_window()
    rec_line("U4c 单次 _extend_window(+80) 同步ms", "%.1f"
             % ((time.perf_counter() - t0) * 1000),
             note="控件=%d 待建=%d" % (len(panel._cards) + len(panel._idle),
                                       len(panel._render_pending)))
    while panel._render_pending:
        app.processEvents(QEventLoop.AllEvents, 50)

    # U5 快捷浮层刷新成本(模型重建 + query_view limit=60)
    qp = wq.QuickPasteWindow(fake)
    qp.resize(520, 500)
    n, med, p95, mx = bench(lambda: qp.refresh(), repeat=5)
    qp.search.setText("普通")
    n2, med2, p952, mx2 = bench(lambda: qp.refresh(), repeat=5)
    rec_line("U5 浮层刷新 ms(空查询,limit=60)", "%.1f" % med,
             note="p95=%.1f n=%d" % (p95, n))
    rec_line("U5b 浮层刷新 ms(关键词)", "%.1f" % med2,
             note="p95=%.1f n=%d" % (p952, n2))

    # U6 来源下拉
    n, med, p95, mx = bench(lambda: panel.sync_src_combo(), repeat=5)
    rec_line("U6 sync_src_combo ms(全历史扫)", "%.1f" % med,
             note="p95=%.1f n=%d" % (p95, n))
    return panel, fake, app, view


# ============================================================
# 核心查询场景
# ============================================================
def core_scenarios(img_paths):
    hist_big = make_history(N_BIG, img_paths[:20])
    core_mem = mini_core(history=hist_big)

    n, med, p95, mx = bench(
        lambda: core_mem.filter_view("all", ""), repeat=5)
    rec_line("C1 内存 query_view 全量 ms(%d条)" % N_BIG, "%.1f" % med,
             note="p95=%.1f n=%d" % (p95, n))
    n, med, p95, mx = bench(
        lambda: core_mem.query_view("all", "", limit=60), repeat=5)
    rec_line("C1b 内存 query_view limit=60 ms", "%.1f" % med,
             note="p95=%.1f n=%d(限量未减全扫成本)" % (p95, n))

    # SQL 模式:种子库
    import store as st
    db_path = os.path.join(TMP, "big.db")
    bs = st.ClipboardStore(db_path)
    bs.migrate_from_list(list(reversed(hist_big)))
    # 把 _db 回填进内存条目(load_all 顺序 = seq desc = hist_big 序)
    loaded = bs.load_all()
    for a, b in zip(hist_big, loaded):
        a["_db"] = b["_db"]
    core_sql = mini_core(store=bs, history=hist_big)

    n, med, p95, mx = bench(lambda: core_sql.query_view("all", ""), repeat=5)
    rec_line("C2 SQL query_view 全量 ms(%d条)" % N_BIG, "%.1f" % med,
             note="p95=%.1f n=%d" % (p95, n))
    n, med, p95, mx = bench(
        lambda: core_sql.query_view("all", "", limit=60), repeat=5)
    rec_line("C2b SQL query_view limit=60 ms", "%.1f" % med,
             note="p95=%.1f n=%d(limit 未下推→全量ID+pos全映射)" % (p95, n))
    n, med, p95, mx = bench(
        lambda: core_sql.query_view("link", "", limit=60), repeat=5)
    rec_line("C2c SQL link 分类 limit=60 ms(内存二次过滤)", "%.1f" % med,
             note="p95=%.1f n=%d" % (p95, n))
    return bs, core_sql, hist_big


# ============================================================
# 捕获场景(满历史)
# ============================================================
def capture_scenarios(core_full, hist_full):
    n, med, p95, mx = bench(
        lambda: core_full.add_item(
            ("text", "全新唯一条目 %f" % time.time(), {}, "")), repeat=15)
    rec_line("C3 满历史新增条目 ms(%d条)" % len(hist_full), "%.2f" % med,
             note="p95=%.2f n=%d" % (p95, n))

    dup = hist_full[10]["content"]
    n, med, p95, mx = bench(
        lambda: core_full.add_item(("text", dup, {}, "")), repeat=15)
    rec_line("C3b 重复条目提升 ms", "%.2f" % med,
             note="p95=%.2f n=%d" % (p95, n))

    core_ev = mini_core(history=list(hist_full), max_items=3900)
    n, med, p95, mx = bench(lambda: core_ev._evict(), repeat=10)
    rec_line("C3c _evict 完整计数 ms(超限100条)", "%.2f" % med,
             note="p95=%.2f n=%d" % (p95, n))


# ============================================================
# 缩略图预热/缓存
# ============================================================
def thumb_scenarios(app, fake, img_paths):
    import winclipboard_qt as wq

    img_hist = [{"type": "image", "content": p, "w": 160, "h": 120,
                 "ts": 1_700_000.0 + i} for i, p in enumerate(img_paths)]
    loader = wq.ThumbLoader(fake)
    fake._thumb_loader = loader

    t0 = time.perf_counter()
    n = 0
    cap = getattr(wq.WinClipboardApp, "PREWARM_THUMBS", 10 ** 9)
    for h in img_hist:                  # 复刻 _prewarm(优化后限量提交)
        loader.submit(h["content"], visible=False)
        n += 1
        if n >= cap:
            break
    submit_ms = (time.perf_counter() - t0) * 1000
    qdepth = loader._q.qsize()
    rec_line("T1 预热提交 ms / 张数(复刻 _prewarm)", "%.1f / %d"
             % (submit_ms, n), note="提交后队列深度=%d 上限=%s"
             % (qdepth, cap if cap < 10 ** 8 else "无(旧行为)"))

    # 等后台线程把队列做完(有界等待)
    deadline = time.time() + 60
    while loader._q.unfinished_tasks:
        app.processEvents()
        if time.time() > deadline:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    app.processEvents()
    rec_line("T1b 缓存条目数(无淘汰路径)", len(fake._thumbs),
             unit="项", note="300 张全留存=无上限增长证据" if
             len(fake._thumbs) >= N_IMG else "")
    bytes_total = sum(pm.width() * pm.height() * 4
                      for pm in fake._thumbs.values())
    rec_line("T1c 缓存像素占用 MB", "%.1f" % (bytes_total / 1048576.0),
             note="仅统计 256px 档位像素字节")
    return loader


def main():
    t_all = time.perf_counter()
    img_paths = make_images(N_IMG)
    rec_line("环境", "img=%d" % len(img_paths),
             note="N_HIST=%d N_BIG=%d TMP=%s" % (N_HIST, N_BIG, TMP))

    panel, fake, app, view = ui_scenarios(make_history(N_HIST, img_paths),
                                          img_paths)
    bs, core_sql, hist_big = core_scenarios(img_paths)
    capture_scenarios(core_sql, hist_big[:N_HIST])
    thumb_scenarios(app, fake, img_paths)

    bs.close()
    rec_line("总耗时 s", "%.1f" % (time.perf_counter() - t_all))

    out = Path(__file__).with_name("_perf_baseline_%s.txt" % TAG)
    out.write_text("\n".join(RESULTS) + "\n", encoding="utf-8")
    print("\nsaved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
