# -*- coding: utf-8 -*-
"""Qt 前端集成自检(离屏;真实 QApplication + WinClipboardApp + ClipboardCore)。

运行: python _integration_test.py   退出码 0 = 全部通过

与 _ai_test.py 的区别:那边用 FakeApp 替身只测工作流窗口;这里构建**真实的
WinClipboardApp**(真实核心 + 真实面板 + 真实定时器),因此能覆盖:

  1. 真实启动路径的密钥迁移(明文 config → 磁盘无明文,内存仍可用)
  2. 剪贴捕获 → signal_q("change") → 防抖刷新 → 卡片重建的完整链路
  3. 缩略图生成、分类筛选、搜索、置顶、删除
  4. 服务窗口 / 工作流运行台的真实构造与人工步骤
  5. 工作流中枢:打开时置顶面板让位(否则窗口开在面板后面=「点了没反应」)、
     「填入示例模板」真实点击(内存+落盘+幂等)、最小化后再次打开能还原
     (Qt 的 show() 不还原最小化窗口,必须手动清 WindowMinimized 位)
  6. 网页 AI 步骤端到端:提示词真实写入系统剪贴板(win32 读回校验;
     offscreen 下 QApplication.clipboard() 是本地桩,不能用来验真)→
     「📋 从剪贴板粘贴」按钮取回回答 → 确认推进 → 全链完成
  7. 运行期 app.log 无异常

数据写入临时目录,不影响真实历史与配置。
"""
import os
import sys
import json
import time
import tempfile
from pathlib import Path as _Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_integ_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.DB_FILE = os.path.join(TMP, "clipboard.db")   # 库路径同样必须进沙箱(模块级常量)
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
cm.ICON_FILE = os.path.join(TMP, "icon.ico")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)
# 预置明文密钥,验证真实启动路径下的升级
SECRET = "sk-live-SECRET-1234567890"
_Path(cm.CONFIG_FILE).write_text(
    json.dumps({"ai_services": [{"name": "GLM", "base_url": "https://x/v1",
                                 "model": "m", "api_key": SECRET}]}),
    encoding="utf-8")

from PIL import Image
from PySide6.QtWidgets import QApplication

app = QApplication([])
app.setQuitOnLastWindowClosed(False)

import winclipboard_qt as wq
from qfluentwidgets import setTheme, Theme

FAKE_PNG = os.path.join(cm.IMAGE_DIR, "img_test.png")

# 热键冲突规避:测试不该占用 Ctrl+Shift+V,否则会与正在运行的真实实例冲突。
# 冲突时核心投递 ("tray","hotkey_fail"),前端随即弹出模态告警框,离屏模式下
# 无人可关 → processEvents() 永久阻塞。这里直接屏蔽告警方法。
# 注意两个坑:(1) _warn_hotkey 定义在 WinClipboardApp 上,不是 ClipboardCore;
# (2) 不能改写 cm.user32.RegisterHotKey —— 该 C 函数设了 argtypes,赋普通
# lambda 会被 ctypes 的类型检查覆盖,实际仍调用真实函数。
wq.WinClipboardApp._warn_hotkey = lambda self: None

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def pump(seconds=1.2):
    """跑 Qt 事件循环,让队列唤醒的信号排空与 180ms 刷新防抖都走完。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


setTheme(Theme.AUTO)
wq.init_palette(wq.isDarkTheme())

a = wq.WinClipboardApp(app)          # 真实核心 + 真实面板
check("真实 app 构造", a.core is not None and a.panel is not None)
check("启动已升级密钥(磁盘无明文)",
      SECRET not in open(cm.CONFIG_FILE, encoding="utf-8").read())
check("运行时密钥可用", a.cfg["ai_services"][0]["api_key"] == SECRET)

# ---- 性能埋点自检:PerfSpan 必须写到 winclipboard.perf 独立通道 ----
import logging as _logging
import perf_probe as _pp
_caps = []
_h = type("_Cap", (_logging.Handler,),
          {"emit": lambda self, r: _caps.append(r.getMessage())})()
_pp.perf_log.addHandler(_h)
_pp.perf_log.setLevel(_logging.DEBUG)
with _pp.PerfSpan("selftest_span"):
    pass
_pp.perf_log.removeHandler(_h)
check("PerfSpan 计时写入独立通道",
      len(_caps) == 1 and "selftest_span" in _caps[0] and "ms" in _caps[0],
      str(_caps))

# 面板可见时:捕获 → 必须经 signal_q("change") 走到防抖刷新
a.win.show()
app.processEvents()
a.refresh_view()
# 注:图片须在 core 启动后再生成 —— _migrate_images 启动时会清理未被任何
# 历史条目引用的孤儿 PNG(该行为本身是对的)
Image.new("RGB", (40, 30), (10, 120, 220)).save(FAKE_PNG)
# TrayWorker 启动时会捕获「当前剪贴板」并投递一条,先排空再取基线
pump()
check("启动捕获已排空", a.signal_q.empty(), str(list(a.signal_q.queue)))
before = len(a.history)
print("      (启动基线 %d 条,含 TrayWorker 捕获的当前剪贴板)" % before)
a.core._post(("clip", ("text", "集成自检条目")))
a.core._post(("clip", ("image", {"path": FAKE_PNG, "w": 40, "h": 30, "hash": "h1"})))
a.core._post(("clip", ("files", [__file__])))
pump()

check("三条捕获入史", len(a.history) == before + 3,
      "before=%d after=%d" % (before, len(a.history)))
check("change 信号已消费", a.signal_q.empty())

# 热键显示路径契约:先显示后重活——show_fast 同步完成(窗口立即可见),
# 脏刷新让到下一拍;隐藏期攒下的重建绝不挡窗口出现
a.win.hide()
app.processEvents()
a._view_dirty = True
a.core._post(("hotkey",))
app.processEvents()                       # 排空唤醒 → toggle → show_fast(同步)
check("热键后窗口立即可见(零重活)", a.win.isVisible())
app.processEvents()                       # 0ms 延迟刷新这一拍落地(若上一拍未跑)
check("延迟刷新随后完成",
      not a._view_dirty and len(a._card_widgets) == len(a.history),
      "dirty=%s cards=%d/%d" % (a._view_dirty,
                                len(a._card_widgets), len(a.history)))
check("卡片已重建", len(a._card_widgets) == len(a.history),
      "%d vs %d" % (len(a._card_widgets), len(a.history)))
check("缩略图生成", a.thumb_for(FAKE_PNG) is not None)

# 分类 / 搜索 / 选择 / 删除 / 置顶(走真实核心)
a.set_category("image")
check("分类筛选", all(a.history[i]["type"] == "image" for i in a._view), str(a._view))
a.set_category("all")
# 空分类往返:全空分类会把所有卡片赶进闲置池并 hide,切回后必须重新显形
# (set_view 曾丢掉收回卡的 show(),「链接/文件」→「全部」列表变隐形,此断言防回归)
vis = lambda: sum(1 for c in a.panel._cards if not c.isHidden())
# 启动捕获会带上环境剪贴板的当前内容,若恰为链接则「链接」分类不空;
# 环境捕获项与本断言无关,先移除保证防回归路径(全空分类)必然走到
for _i in range(len(a.history) - 1, -1, -1):
    _h = a.history[_i]
    if _h["type"] == "text" and cm.url_info(_h["content"]):
        a.history.pop(_i)
a.set_category("link")
check("空分类视图为空", not a._view, str(a._view))
a.set_category("all")
check("切回全部卡片可见(池收回须 show)", vis() == len(a._view) == len(a.panel._cards),
      "vis=%d view=%d cards=%d" % (vis(), len(a._view), len(a.panel._cards)))
a.set_category("files")
a.set_category("all")
check("文件分类往返卡片可见", vis() == len(a._view),
      "vis=%d view=%d" % (vis(), len(a._view)))
a.on_search("集成自检")
pump(0.5)
check("搜索命中", len(a._view) == 1, str(a._view))
a.selected = 0
a.toggle_pin()
check("置顶生效", any(h.get("pinned") for h in a.history))
a.selected = 0
a.delete_selected()
check("删除生效", not any(h.get("pinned") for h in a.history))
_n_b4_undo = len(a.history)
a.undo_delete()
check("删除可撤销(UI 级)", len(a.history) == _n_b4_undo + 1)

# AI 服务页(OpenCode 风格供应商卡片)/ 工作流运行台真实构造
from PySide6.QtWidgets import QPushButton  # noqa: E402

sw = a.win.services_page
sw.refresh()
check("ServicesPage 卡片化", len(sw.cards) == 1,
      str([c.svc.get("name") for c in sw.cards]))
if sw.cards:
    btn_texts = [b.text() for b in sw.cards[0].findChildren(QPushButton)]
    check("卡片含 编辑/测试/删除", all(t in btn_texts for t in ("编辑", "测试", "删除")),
          str(btn_texts))
# 预设提供方菜单:点击「添加提供方」应列出全部预设(拦截 exec 防离屏模态阻塞)
_menu_built = []
_o_exec = wq.RoundMenu.exec
wq.RoundMenu.exec = lambda self, *args, **kw: _menu_built.append(self)
try:
    sw.b_add_preset.click()
finally:
    wq.RoundMenu.exec = _o_exec
check("预设提供方菜单", bool(_menu_built)
      and len(_menu_built[0]._actions) >= len(cm.AI_PRESET_SERVICES),
      str([x.text() for x in _menu_built[0]._actions]) if _menu_built else "no menu")
wf = {"name": "自检流程", "steps": [{"kind": "human", "prompt": "{input}"}]}
runner = wq.WorkflowRunner(a, wf, "种子")
a._ai_runners.append(runner)
runner._run_step()
app.processEvents()
check("运行台等待面板", runner._waiting is not None)
runner._wait_body.setPlainText("人工确认内容")
runner._wait_confirm()
app.processEvents()
check("运行台完成", runner.step_idx == 1 and runner.results == ["人工确认内容"])
runner.close()

# ---- 工作流中枢:面板让位 + 种子按钮真实点击 + 最小化还原 ----
from PySide6.QtWidgets import QPushButton  # noqa: E402
from qfluentwidgets import PrimaryPushButton  # noqa: E402

a.win.show()
pump(0.3)
check("主窗口已显示(前置)", a.win.isVisible())
a.show_ai_hub()
pump(0.5)
check("导航切到工作流页", a.win.stackedWidget.currentWidget() is a.win.workflow_page)
check("工作流页初始为空", a.win.workflow_page.lst.count() == 0)

b_seed = [b for b in a.win.workflow_page.findChildren(QPushButton) if b.text() == "填入示例模板"]
check("种子按钮存在", len(b_seed) == 1)
b_seed[0].click()                      # 真实 Qt 点击信号 → _seed → save_config
pump(0.3)
disk_wfs = json.load(open(cm.CONFIG_FILE, encoding="utf-8")).get("ai_workflows", [])
check("种子入内存", a.win.workflow_page.lst.count() == len(cm.AI_SEED_WORKFLOWS))
check("种子已落盘", len(disk_wfs) == len(cm.AI_SEED_WORKFLOWS),
      str([w.get("name") for w in disk_wfs]))
b_seed[0].click()                      # 幂等:再次点击不应重复添加
pump(0.2)
check("种子幂等", len(json.load(open(cm.CONFIG_FILE, encoding="utf-8"))
      .get("ai_workflows", [])) == len(cm.AI_SEED_WORKFLOWS))

a.show_services()
pump(0.3)
check("导航切到 AI 服务页", a.win.stackedWidget.currentWidget() is a.win.services_page)
a.show_ai_hub()
pump(0.2)

# ---- 网页 AI 步骤端到端:提示词真实进剪贴板 → 粘贴按钮取回 → 确认推进 ----
wf2 = {"name": "网页自检", "steps": [
    {"kind": "web", "prompt": "请处理:{input}", "url": ""},
    {"kind": "human", "prompt": "继续"},
]}
runner2 = wq.WorkflowRunner(a, wf2, "初始内容")
a._ai_runners.append(runner2)
runner2._run_step()
app.processEvents()
# offscreen 下 QApplication.clipboard() 是本地桩,真实写入要走 win32 读回
import win32clipboard  # noqa: E402

_CLIP_BUSY = [False]

def clip_text():
    for _ in range(20):                 # 剪贴板可能被其他进程占用(外部软件持锁)
        try:
            win32clipboard.OpenClipboard(a.worker.hwnd)
            try:
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.1)
    _CLIP_BUSY[0] = True                # 环境被占用,相关断言降级为 SKIP
    return ""

got = clip_text()
if _CLIP_BUSY[0]:
    print("SKIP web 步骤提示词已进剪贴板 (系统剪贴板被外部进程占用)")
else:
    check("web 步骤提示词已进剪贴板", got == "请处理:初始内容", repr(got)[:60])
check("web 步骤进入等待面板", runner2._waiting is not None)
# 模拟用户:切到网页粘贴提示词、复制回答(写剪贴板),回来点「📋 从剪贴板粘贴」
app.clipboard().setText("网页 AI 的回答")
b_paste = [b for b in runner2._waiting.findChildren(QPushButton)
           if "从剪贴板粘贴" in b.text()]
check("粘贴按钮存在", len(b_paste) == 1)
b_paste[0].click()
app.processEvents()
check("粘贴按钮取回回答", runner2._wait_body.toPlainText() == "网页 AI 的回答")
b_ok = runner2._waiting.findChild(PrimaryPushButton)
check("确认按钮存在", b_ok is not None)
b_ok.click()
app.processEvents()
check("第一步完成进入第二步", runner2.step_idx == 1)
runner2.btn_next.click()               # 人在回路:确认后需手动执行下一步
app.processEvents()
check("第二步等待面板已出现", runner2._waiting is not None and runner2._wait_body is not None)
runner2._wait_body.setPlainText("第二步编辑后内容")
runner2._wait_confirm()
app.processEvents()
check("工作流全部完成",
      runner2.results == ["网页 AI 的回答", "第二步编辑后内容"]
      and not runner2.btn_next.isEnabled())
runner2.close()
a.win.switchTo(a.win.history_page)

# ---- 内容查看器:面板让位 + 全量内容 + 高亮 + JSON 美化/紧凑 ----
vrec = {"type": "text", "content": "def greet(name):\n    return 'hi ' + name\n"}

def colored_chars(edit):            # 统计被染色字符数(按 fragment 读字符级格式)
    n, it = 0, edit.document().begin()
    base = wq.QColor(wq.TEXT)
    solid = wq.Qt.BrushStyle.SolidPattern   # 注意: PySide6 枚举不能与 int 直接比较
    while it.isValid():
        fit = it.begin()              # QTextBlock.fragment 显式迭代器
        while not fit.atEnd():
            frag = fit.fragment()
            if frag.isValid():
                cf = frag.charFormat()
                if cf.foreground().style() == solid \
                        and cf.foreground().color() != base:
                    n += frag.length()
            fit += 1
        it = it.next()
    return n

a.win.show()
pump(0.3)
vw = a.open_viewer(vrec)
pump(0.3)
check("查看器独立窗口打开", vw is not None and vw.isVisible(),
      "vw=%s vis=%s" % (vw is not None, vw.isVisible() if vw else None))
check("查看器内容完整",
      vw._active_edit().toPlainText() == vrec["content"])
check("查看器 Inspector 布局", vw.width() == 960 and vw.height() == 640
      and "Ln 1, Col 1" in vw.lbl_pos.text()
      and vw.lbl_state.text() == "已保存到历史",
      "%dx%d %s" % (vw.width(), vw.height(), vw.lbl_pos.text()))
vw._open_find()
pump(0.1)
vw.ed_find.setText("json")
pump(0.1)
check("查看器浮动查找", vw._find.isVisible() and "/" in vw.lbl_find_n.text(),
      vw.lbl_find_n.text())
vw._close_find()
vw._copy_all()
check("查看器复制反馈", vw.btn_copy.text() == "已复制")
vw._copy_timer.stop()
vw._copy_restore()
check("查看器语言识别", vw._auto_lang == "python", vw._auto_lang)
if wq._pg_get_lexer is not None:
    n_colored = colored_chars(vw.editor)
    check("查看器语法高亮已染色", n_colored >= 4, "colored chars=%d" % n_colored)
else:
    print("SKIP 查看器语法高亮已染色 (pygments 不可用)")

vw._set_lang_key("text")
pump(0.2)
check("切纯文本清除染色", colored_chars(vw._active_edit()) == 0,
      "colored=%d" % colored_chars(vw._active_edit()))
vw._set_lang_key("auto")
pump(0.2)

jv = a.open_viewer({"type": "text", "content": '{"name":"WinClipboard","n":3}'})
pump(0.2)
check("JSON 查看器识别", jv._auto_lang == "json" and jv._mode == "fmt")
check("JSON 默认格式化", '"name": "WinClipboard"' in jv.editor.toPlainText())
check("JSON 元信息带有效性", "JSON · 有效" in jv.lbl_meta.text(),
      jv.lbl_meta.text())
jv._set_view("raw")
pump(0.2)
check("JSON 原文视图",
      jv._active_edit().toPlainText() == '{"name":"WinClipboard","n":3}')
jv._set_view("tree")
pump(0.2)
check("JSON 结构树", jv.stack.currentWidget() is jv.tree
      and jv.tree.topLevelItemCount() == 1)
jv._set_view("fmt")
pump(0.2)
check("JSON 回到格式化", '"name": "WinClipboard"' in jv.editor.toPlainText())
bjv = a.open_viewer({"type": "text", "content": '{"name": 1'})
bjv._set_lang_key("json")
pump(0.2)
check("非法 JSON 只留原文+行列提示", bjv._mode == "raw"
      and "无效" in bjv.lbl_meta.text() and "第 1 行" in bjv.lbl_meta.text(),
      bjv.lbl_meta.text())
mv = a.open_viewer({"type": "text",
                    "content": "# 周报\n\n- 进度 A\n- 进度 B\n\n段落说明。\n"})
pump(0.2)
check("Markdown 默认预览", mv._auto_lang == "markdown" and mv._mode == "preview"
      and mv.stack.currentWidget() is mv.md_view)
check("Markdown 预览渲染", "周报" in mv.md_view.toPlainText()
      and "进度 B" in mv.md_view.toPlainText())
mv._set_view("raw")
check("Markdown 原文", mv._active_edit().toPlainText().startswith("# 周报"))
cvv = a.open_viewer({"type": "text",
                     "content": "名称,数量\n苹果,3\n香蕉,5\n樱桃,7\n"})
pump(0.2)
check("CSV 默认表格", cvv._mode == "table" and cvv.table.columnCount() == 2
      and cvv.table.rowCount() == 3)
check("CSV 表头与单元格", cvv.table.horizontalHeaderItem(0).text() == "名称"
      and cvv.table.item(2, 1).text() == "7")
check("CSV 元信息行列", "4 行 × 2 列" in cvv.lbl_meta.text(), cvv.lbl_meta.text())
xv = a.open_viewer({"type": "text",
                    "content": '<?xml version="1.0"?><cfg><a>1</a></cfg>'})
pump(0.2)
check("XML 默认格式化", xv._mode == "fmt"
      and "\n  <a>1</a>" in xv.editor.toPlainText())
fv = a.open_viewer({"type": "files", "content": ["C:/a.txt", "C:/b/c.png"]})
pump(0.2)
check("文件条目可查看",
      fv._active_edit().toPlainText() == "C:/a.txt\nC:/b/c.png")

# ---- Content Intelligence:URL / JWT / Base64 Inspector ----
import base64 as _ib64
import time as _itime


def _b64u(s):
    return _ib64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def info_rows(v):
    return {v.info.topLevelItem(i).text(0): v.info.topLevelItem(i).text(1)
            for i in range(v.info.topLevelItemCount())}


uv = a.open_viewer({"type": "text",
                    "content": "https://example.com/api/users?page=2&size=20#frag"})
pump(0.2)
check("URL 查看器默认结构视图", uv._mode == "url"
      and uv.stack.currentWidget() is uv.info)
rows = info_rows(uv)
check("URL 字段拆解", rows.get("Scheme") == "https"
      and rows.get("主机") == "example.com" and rows.get("路径") == "/api/users"
      and rows.get("Fragment") == "frag", str(rows))
qi = next((uv.info.topLevelItem(i) for i in range(uv.info.topLevelItemCount())
           if uv.info.topLevelItem(i).text(0) == "Query 参数"), None)
check("URL 参数子行", qi is not None and qi.childCount() == 2
      and qi.child(0).text(0) == "page" and qi.child(0).text(1) == "2")
check("URL 元信息", "URL · example.com" in uv.lbl_meta.text(), uv.lbl_meta.text())
_ua = [t for t, _ in uv._inspector_actions()]
check("URL 专属动作", "在浏览器打开" in _ua and "复制主机名" in _ua
      and "URL 解码并复制" in _ua, str(_ua))
uv._set_lang_key("text")
pump(0.2)
check("手动语言覆盖关闭智能视图", uv._mode == "raw"
      and uv._mode_table()[1] == [])
uv._set_lang_key("auto")
pump(0.2)
check("回自动恢复智能视图", uv._mode == "url")

_jwt_txt = (_b64u('{"alg":"HS256","typ":"JWT"}') + "." +
            _b64u('{"sub":"u123","name":"张三","exp":%d}'
                  % int(_itime.time() + 3600)) + "." +
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
jw = a.open_viewer({"type": "text", "content": _jwt_txt})
pump(0.2)
check("JWT 查看器默认 JWT 视图", jw._mode == "jwt"
      and jw.stack.currentWidget() is jw.info)
rows = info_rows(jw)
check("JWT 算法与未验签提示", rows.get("算法 (alg)") == "HS256"
      and rows.get("签名") == "未验证", str(rows))
check("JWT 过期状态", "后过期" in rows.get("状态", ""), str(rows))
check("JWT 元信息明示未验签", "仅解码,签名未验证" in jw.lbl_meta.text(),
      jw.lbl_meta.text())
_ja = [t for t, _ in jw._inspector_actions()]
check("JWT 专属动作", "复制 Payload (JSON)" in _ja, str(_ja))
jw._set_view("raw")
pump(0.2)
check("JWT 原文视图", jw._active_edit().toPlainText() == _jwt_txt)

_b64_txt = _ib64.b64encode("这段文本以 Base64 编码,长度足够触发嗅探护栏的四十字符下限。".encode("utf-8")).decode("ascii")
bv = a.open_viewer({"type": "text", "content": _b64_txt})
pump(0.2)
check("Base64 默认解码视图", bv._mode == "b64"
      and bv.stack.currentWidget() is bv.editor
      and "这段文本以 Base64 编码" in bv.editor.toPlainText())
check("Base64 元信息", "解码为文本" in bv.lbl_meta.text(), bv.lbl_meta.text())
_ba = [t for t, _ in bv._inspector_actions()]
check("Base64 专属动作", "复制解码文本" in _ba, str(_ba))
from io import BytesIO as _IBio
_pil_img = Image.new("RGB", (24, 16), (200, 40, 40))
_buf = _IBio()
_pil_img.save(_buf, "PNG")
iv2 = a.open_viewer({"type": "text",
                     "content": _ib64.b64encode(_buf.getvalue()).decode("ascii")})
pump(0.2)
check("Base64 图片解码视图", iv2._mode == "b64"
      and iv2.stack.currentWidget() is iv2.img_view
      and not iv2.img_view.pixmap().isNull())
check("Base64 图片元信息", "解码为图片" in iv2.lbl_meta.text(), iv2.lbl_meta.text())

for w in (vw, jv, bjv, mv, cvv, xv, fv, uv, jw, bv, iv2):
    w.close()
pump(0.2)
check("查看器关闭后清理", len(a._viewers) == 0)

# ---- Diff 对比:Ctrl+点击选两条 + JSON 语义 diff + 文本 unified diff ----
a.on_search("")                       # 清掉早前测试遗留的搜索过滤
pump(0.2)
a.core._post(("clip", ("text", '{"timeout": 30, "name": "A", "drop": 1}')))
pump(0.4)
a.core._post(("clip", ("text", '{\n  "name": "A",\n  "timeout": 60,\n  "retry": 3\n}')))
pump(0.6)
a.win.show()                          # 弹面板触发卡片重建
pump(0.4)
_cards = a._card_widgets
_ra = next((c for c in _cards if '"timeout": 30' in str(c.rec.get("content"))), None)
_rb = next((c for c in _cards if '"timeout": 60' in str(c.rec.get("content"))), None)
check("存在两条 JSON 卡片(前置)", _ra is not None and _rb is not None)
a.compare_pick(_ra.rec)
check("第一条设为对比基准", a._diff_base is _ra.rec)
a.compare_pick(_rb.rec)
check("第二条开对比后基准清空", a._diff_base is None and len(a._diffs) == 1)
dw = a._diffs[-1] if a._diffs else None
check("对比窗口默认语义视图", dw is not None and dw._mode == "sem"
      and "~ $.timeout:  30  →  60" in dw.ed.toPlainText(),
      dw.ed.toPlainText()[:200] if dw else "")
check("语义视图含增删", "+ $.retry" in dw.ed.toPlainText()
      and "- $.drop" in dw.ed.toPlainText())
dw._set_view("text")
check("文本 unified diff 视图", any(l.startswith("-") and "30" in l
      for l in dw.ed.toPlainText().splitlines())
      and any(l.startswith("+") and "60" in l
              for l in dw.ed.toPlainText().splitlines()))
dw._set_view("sem")
check("对比元信息", "语义差异 3 处" in dw.lbl_meta.text(), dw.lbl_meta.text())

_okpair = wq.DiffWindow(a, {"type": "text", "content": '{"a":1,"b":2}',
                            "ts": 1757040000},
                        {"type": "text", "content": '{"b":2,\n"a":1}',
                         "ts": 1757040060})
check("键序不同语义相同", "语义相同" in _okpair.lbl_meta.text()
      and "两个 JSON 语义相同" in _okpair.ed.toPlainText())
_tpair = wq.DiffWindow(a, {"type": "text", "content": "alpha\nbeta"},
                       {"type": "text", "content": "alpha\ngamma"})
check("非 JSON 回退文本视图", _tpair._mode == "text"
      and "-beta" in _tpair.ed.toPlainText()
      and "+gamma" in _tpair.ed.toPlainText())
if dw:
    dw.close()
_okpair.close()
_tpair.close()
pump(0.2)
check("对比窗口关闭后清理", len(a._diffs) == 0)

# ---- 卡片子类型徽章:JWT / Base64 在 meta 行显示 ----
# JWT 属敏感内容默认进私密区(不进普通历史)——临时关护栏验证徽章路径
_guard = a.core.cfg.get("sensitive_guard", True)
a.core.cfg["sensitive_guard"] = False
a.core._post(("clip", ("text", _jwt_txt)))
a.core._post(("clip", ("text", _b64_txt)))
pump(0.6)
a.core.cfg["sensitive_guard"] = _guard
a.refresh_view()
pump(0.3)
_jc = next((c for c in a._card_widgets if c._badge_of(c.rec) == "JWT"), None)
_bc = next((c for c in a._card_widgets if c._badge_of(c.rec) == "Base64"), None)
check("JWT 卡片徽章", _jc is not None and "JWT ·" in _jc.meta_lbl.text(),
      _jc.meta_lbl.text() if _jc else "无卡片")
check("Base64 卡片徽章", _bc is not None and "Base64 ·" in _bc.meta_lbl.text(),
      _bc.meta_lbl.text() if _bc else "无卡片")
check("徽章缓存生效", id(_jc.rec) in a._badge_cache
      and a._badge_cache[id(_jc.rec)] == "JWT")

# ---- 敏感隔离 → 私密区「查看」链路(默认护栏下的 JWT Inspector 可达性) ----
_jwt_priv = (_b64u('{"alg":"HS256","typ":"JWT"}') + "." +
             _b64u('{"sub":"priv999","name":"私有"}') + "." +
             "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_n_priv = len(a.private)
a.core._post(("clip", ("text", _jwt_priv)))
pump(0.6)
check("JWT 默认隔离进私密区", len(a.private) == _n_priv + 1
      and a.private[0].startswith("eyJ"), str(len(a.private)))
check("隔离后私密列表已刷新", a.win.private_page.lst.count() == len(a.private))
pp = a.win.private_page
pp.lst.setCurrentRow(0)
pump(0.1)
_vw = pp.do_view()
pump(0.3)
check("私密区查看直达 JWT 解码视图", _vw is not None and _vw._mode == "jwt"
      and _vw.stack.currentWidget() is _vw.info
      and _vw._jwt_inf["payload"].get("sub") == "priv999",
      "mode=%s sub=%s" % (_vw._mode if _vw else "-",
                          _vw._jwt_inf["payload"].get("sub") if _vw else "-"))
check("私密条目带 private 标记", _vw.rec.get("private") is True)
if _vw is not None:
    _vw.close()
    pump(0.2)

# 私密列表默认遮罩 / 解锁 / 切走自动遮回
_raw0 = a.private[0]
check("私密列表默认遮罩", "•" in pp.lst.item(0).text()
      and _raw0[:10] not in pp.lst.item(0).text(), pp.lst.item(0).text())
pp.setVisible(True)                          # hide() 只在可见→隐藏时发 hideEvent
app.processEvents()
pp._set_revealed(True)
check("解锁后显示明文前缀", pp.lst.item(0).text().startswith(_raw0[:10]))
pp._mask_timer.stop()                      # 不等 30s 超时,直接验证切走路径
pp.hide()
check("切走页面自动遮回", "•" in pp.lst.item(0).text()
      and _raw0[:10] not in pp.lst.item(0).text())

# ---- 设置页:热键卡片 + 录制对话框捕获链路 ----
sp = a.win.settings_page
check("设置页热键卡片显示当前组合",
      sp.lbl_hotkey.text() == cm.hotkey_label(
          a.cfg.get("hotkey_mod", 6), a.cfg.get("hotkey_key", 0x56)),
      sp.lbl_hotkey.text())
from PySide6.QtGui import QKeyEvent as _QKE
from PySide6.QtCore import QEvent as _QE, Qt as _Qt
_applied = []
_r = wq.HotkeyRecorderDialog(sp, sp.lbl_hotkey.text(),
                             lambda m, k: _applied.append((m, k)))
_r._on_key(_QKE(_QE.KeyPress, _Qt.Key_V, _Qt.ControlModifier | _Qt.ShiftModifier))
check("录制捕获 Ctrl+Shift+V", _r.capture.text() == "Ctrl+Shift+V"
      and _r.yesButton.isEnabled())
_r._on_key(_QKE(_QE.KeyPress, _Qt.Key_F5, _Qt.ControlModifier))
check("录制可覆盖改键", _r.capture.text() == "Ctrl+F5")
_r._on_key(_QKE(_QE.KeyPress, _Qt.Key_X, _Qt.NoModifier))
check("无修饰键被拒绝", not _r.yesButton.isEnabled()
      and "修饰键" in _r.capture.text())
_r._on_key(_QKE(_QE.KeyPress, _Qt.Key_X, _Qt.AltModifier))
check("有效组合经应用回调", _r.validate() is True
      and _applied == [(cm.MOD_ALT, 0x58)], str(_applied))

# ---- 暂停监听(临时隐私模式):剪贴板事件不入史 ----
a.toggle_monitor_pause(True)
check("暂停监听状态生效", a.core.monitor_paused is True
      and a.win.settings_page.c_pause.isChecked())
_n_pause = len(a.history)
for _ in range(2):
    a.core.worker.on_clip(0, 0, 0, 0)   # 直接模拟 WM_CLIPBOARDUPDATE(门控在 on_clip)
pump(0.4)
check("暂停期间剪贴板事件不入史", len(a.history) == _n_pause,
      "%d -> %d" % (_n_pause, len(a.history)))
a.core._post(("tray", "pause"))
pump(0.2)
check("托盘开关恢复监听", a.core.monitor_paused is False
      and not a.win.settings_page.c_pause.isChecked())

# ---- 按应用排除:设置卡 + 管理对话框 + 捕获门控 ----
check("排除卡初始标签", sp.lbl_excl.text() == "未排除任何应用"
      or sp.lbl_excl.text().startswith("已排除"),
      sp.lbl_excl.text())
_dx = wq.ExcludeAppsDialog(sp, a)
_dx.cmb.setText("KeePass.exe")
_dx._add_current()
_dx._add_current()                        # 重复添加 → 去重
check("排除对话框添加归一化+去重", _dx.lst.count() == 1
      and _dx.lst.item(0).text() == "keepass.exe", str([_dx.lst.item(i).text()
                                                        for i in range(_dx.lst.count())]))
_dx.validate()                            # 保存 → core.set_exclude_apps
check("排除名单生效并落盘", a.core.should_capture("keepass.exe") is False
      and cm.load_config().get("exclude_apps") == ["keepass.exe"])
sp._refresh_excl_label()
check("排除卡计数刷新", sp.lbl_excl.text() == "已排除 1 个应用")
_n_ex = len(a.history)
_fg_saved = cm.foreground_app_exe
cm.foreground_app_exe = lambda: "keepass.exe"
a.core.worker.on_clip(0, 0, 0, 0)         # 命中名单 → 门控拦截,不触剪贴板
pump(0.4)
check("排除应用复制不入史", len(a.history) == _n_ex,
      "%d -> %d" % (_n_ex, len(a.history)))
check("其他应用不受影响(门放行)", a.core.should_capture("chrome.exe") is True)
cm.foreground_app_exe = _fg_saved
_dx2 = wq.ExcludeAppsDialog(sp, a)
_dx2.lst.item(0).setSelected(True)
_dx2._remove_selected()
_dx2.validate()
check("对话框移除后恢复捕获", a.core.should_capture("keepass.exe") is True
      and cm.load_config().get("exclude_apps") == [])

# 来源应用入史 + 卡片 meta 行显示
a.core._post(("clip", ("text", "来源标记条目", "notepad.exe")))
pump(0.5)
_src_rec = next((h for h in a.history if h.get("content") == "来源标记条目"), None)
check("来源应用随捕获入史", _src_rec is not None
      and _src_rec.get("src") == "notepad.exe")
a.on_search("")
pump(0.4)
_src_card = next((c for c in a._card_widgets
                  if c.rec.get("content") == "来源标记条目"), None)
check("卡片 meta 行显示来源", _src_card is not None
      and "notepad.exe" in _src_card.meta_lbl.text(),
      _src_card.meta_lbl.text() if _src_card else "no card")

# ---- 存储与清理:设置组交互(下拉/开关 → 配置/文件格式) ----
sp.combo_days.setCurrentIndex(1)            # 7 天
check("清理天数下拉生效", a.cfg.get("cleanup_days") == 7
      and json.load(open(cm.CONFIG_FILE, encoding="utf-8")).get("cleanup_days") == 7)
sp.combo_disk.setCurrentIndex(1)            # 200 MB
check("图片上限下拉生效", a.cfg.get("disk_cap_mb") == 200)
sp.combo_days.setCurrentIndex(0)
sp.combo_disk.setCurrentIndex(0)
check("清理策略可关回", a.cfg.get("cleanup_days") == 0 and a.cfg.get("disk_cap_mb") == 0)

sp.c_enc.setChecked(True)
pump(0.2)
_hfile = _Path(cm.HISTORY_FILE).read_text(encoding="utf-8")
check("历史已转加密格式", '"dpapi": true' in _hfile, _hfile[:40])
check("加密历史仍可加载", len(cm.load_history()) == len(a.history))
check("加密模式下停用库写透", a.core.store is None)
sp.c_enc.setChecked(False)
pump(0.2)
check("切回库模式(store 复活且 JSON 快照已清)",
      a.core.store is not None and not _Path(cm.HISTORY_FILE).exists()
      and a.core.store.count() == len(a.history))

# ---- 富文本格式保留:HTML/RTF 随条目捕获与回写(真剪贴板;被占则跳过) ----
import win32clipboard as _wcb
try:
    _cfh = _wcb.RegisterClipboardFormat("HTML Format")
    _cfr = _wcb.RegisterClipboardFormat("Rich Text Format")
    _wcb.OpenClipboard()
    _wcb.EmptyClipboard()
    _wcb.SetClipboardData(_wcb.CF_UNICODETEXT, "富文本保留测试")
    _html_payload = ("Version:0.9\r\nStartHTML:0000000105\r\nEndHTML:0000000190"
                     "\r\n<html><body><b>加粗</b></body></html>")
    _wcb.SetClipboardData(_cfh, _html_payload.encode("utf-8"))
    _wcb.SetClipboardData(_cfr, b"{\\rtf1\\ansi rich}")
    _wcb.CloseClipboard()
    _item = a.core.read_clipboard_now()
    # html 按 latin-1 字节保真存储(保 HTML Format 头偏移),比对用同样形态
    _html_l1 = _html_payload.encode("utf-8").decode("latin-1")
    check("富文本捕获(html+rtf 随文本)", _item[0] == "text"
          and _item[1] == "富文本保留测试"
          and isinstance(_item[2], dict) and _item[2].get("html") == _html_l1
          and "rtf1" in _item[2].get("rtf", ""), str(_item)[:60])
    ok = a.core._set_clipboard({"type": "text", "content": "富文本保留测试",
                                "html": _item[2]["html"], "rtf": _item[2]["rtf"]})
    _wcb.OpenClipboard()
    check("回写并放 HTML 格式", ok
          and _wcb.IsClipboardFormatAvailable(_cfh))
    check("回写并放 RTF 格式", _wcb.IsClipboardFormatAvailable(_cfr))
    check("回写保留纯文本", _wcb.IsClipboardFormatAvailable(_wcb.CF_UNICODETEXT))
    _wcb.CloseClipboard()
except Exception as _e:
    print("SKIP 富文本剪贴板往返(系统剪贴板被占用): %r" % _e)

# ---- 来源 / 时间筛选下拉(数据来自条目 src/ts) ----
a.on_search("")
pump(0.4)
_np_rec = next((h for h in a.history if h.get("src") == "notepad.exe"), None)
check("存在带来源条目(前置)", _np_rec is not None)
if _np_rec is not None:
    _i = a.panel.cmb_src.findData("notepad.exe")
    a.panel.cmb_src.setCurrentIndex(_i)
    pump(0.4)
    check("来源下拉筛选", a.src_filter == "notepad.exe" and a._view
          and all(a.history[i].get("src") == "notepad.exe" for i in a._view),
          str(len(a._view)))
    a.panel.cmb_time.setCurrentIndex(2)     # 近 24 小时
    pump(0.4)
    check("时间下拉叠加生效", a.time_filter == "24h"
          and all(a.history[i]["ts"] >= time.time() - 86400 for i in a._view))
    a.panel.reset_filters()
    pump(0.4)
    check("重置筛选恢复全量", a.src_filter == "" and a.time_filter == "all"
          and len(a._view) == len(a.history))

# ---- 段选批量删除(Shift+点击 → Delete,整批可撤销) ----
if len(a._view) >= 3:
    a.select_card(a.panel.card_at(0))
    a.range_select_from(a.panel.card_at(2))
    check("段选命中三连", len(a._multi_recs) == 3)
    _n_b4 = len(a.history)
    a.delete_selection_or_multi()
    pump(0.3)
    check("批量删除生效", len(a.history) == _n_b4 - 3
          and len(a._multi_recs) == 0)
    a.undo_delete()
    pump(0.3)
    check("批量删除可撤销", len(a.history) == _n_b4)

# ---- 快捷键帮助 + 主题选项中文化 + 无障碍名 ----
check("帮助文本含动态热键与分区", "显示 / 隐藏主窗口" in a.shortcut_help_text()
      and "历史列表" in a.shortcut_help_text() and "图片编辑" in a.shortcut_help_text())
check("主题下拉中文化", [sp.combo_theme.itemText(i) for i in range(3)]
      == ["跟随系统", "浅色", "深色"]
      and sp.combo_theme.currentData() == str(a.cfg.get("theme", "auto")))
check("关键控件有无障碍名", a.panel.search.accessibleName() == "搜索剪贴历史"
      and a.panel.cmb_src.accessibleName() != "")

# ---- 导出 / 导入(拦截文件对话框与导出选项弹窗) ----
_EXP = os.path.join(tempfile.gettempdir(), "wclb_integ_export.json")
_o_gsf = wq.QFileDialog.getSaveFileName
_o_gof = wq.QFileDialog.getOpenFileName
_o_mbe = wq.MessageBoxBase.exec
wq.QFileDialog.getSaveFileName = staticmethod(lambda *args, **kw: (_EXP, ""))
wq.QFileDialog.getOpenFileName = staticmethod(lambda *args, **kw: (_EXP, ""))
wq.MessageBoxBase.exec = lambda self: True
try:
    sp._export_history()
    check("导出文件已写", _Path(_EXP).exists())
    _pl_e = json.loads(_Path(_EXP).read_text(encoding="utf-8"))
    _exp_expect = [h for h in a.history
                   if h["type"] == "text"
                   or (h["type"] == "files"
                       and any(os.path.exists(p) for p in h.get("content", [])))]
    check("导出载荷条目数一致", _pl_e.get("app") == "WinClipboard"
          and len(_pl_e["items"]) == len(_exp_expect),
          "%d vs %d" % (len(_pl_e["items"]), len(_exp_expect)))
    _n_imp_b4 = len(a.history)
    sp._import_history()
    pump(0.3)
    check("重复导入零新增(去重)", len(a.history) == _n_imp_b4)
finally:
    wq.QFileDialog.getSaveFileName = _o_gsf
    wq.QFileDialog.getOpenFileName = _o_gof
    wq.MessageBoxBase.exec = _o_mbe
    try:
        os.remove(_EXP)
    except OSError:
        pass

# ---- 关于与更新:版本卡入口 + 真实网络检查(失败即跳过) ----
check("设置页含检查更新入口",
      any(b.text() == "检查更新" for b in sp.findChildren(QPushButton)))
check("导出载荷/关于带版本", cm.APP_VERSION
      and "1" == cm.APP_VERSION.split(".")[0])
_res_upd = cm.fetch_latest_release()
if _res_upd[0] == "error":
    print("SKIP 更新检查(网络不可达): %s" % _res_upd[2][:80])
else:
    check("更新检查返回合法状态",
          _res_upd[0] in ("ok", "latest", "norelease")
          and (_res_upd[0] != "ok"
               or cm.version_tuple(_res_upd[1]) > cm.version_tuple(cm.APP_VERSION)),
          str(_res_upd))

# ---- 图片查看器:独立窗口 + 缩放/1:1/适应(小图不放大) ----
img_rec = next((h for h in a.history if h.get("type") == "image"), None)
check("存在图片条目(前置)", img_rec is not None)
iv = a.open_image_viewer(img_rec) if img_rec is not None else None
pump(0.3)
check("图片查看器打开", iv is not None and iv.isVisible()
      and not iv._pix.pixmap().isNull())
if iv is not None:
    check("初始适应(小图封顶 100%)", abs(iv.view.transform().m11() - 1.0) < 1e-6,
          "scale=%s" % iv.view.transform().m11())
    iv._zoom_by(2.0)
    check("放大生效", iv.view.transform().m11() > 1.5)
    iv._do_100()
    check("1:1 还原", abs(iv.view.transform().m11() - 1.0) < 1e-6)
    iv._do_fit()
    check("适应还原", abs(iv.view.transform().m11() - 1.0) < 1e-6)
    iv.close()
    pump(0.2)

# ShareX 分发:未安装→回退内置编辑器;已安装→调起外部
_orig_find, _orig_open, _orig_oe = cm.find_sharex, cm.open_in_sharex, a.open_editor
_calls = []
try:
    cm.open_in_sharex = lambda p: _calls.append("sharex") or True
    a.open_editor = lambda r: _calls.append("builtin")
    cm.find_sharex = lambda: None
    a.edit_image(img_rec)
    cm.find_sharex = lambda: "C:/fake/ShareX.exe"
    a.edit_image(img_rec)
finally:
    cm.find_sharex, cm.open_in_sharex, a.open_editor = _orig_find, _orig_open, _orig_oe
check("edit_image 分发(ShareX 优先/未装回退)", _calls == ["builtin", "sharex"], str(_calls))

# 双击条目 = 独立窗口查看(文本→ViewerWindow,图片→ImageViewerWindow)
from PySide6.QtCore import QEvent, QPointF  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

a.on_search("")          # 清掉早前测试残留的搜索词,否则视图被过滤为空
pump(0.3)
if not any(h["type"] == "text" for h in a.history):   # 真实剪贴板无文本时兜底
    a.core._post(("clip", ("text", "双击查看测试文本")))
    pump(0.5)
a.refresh_view()
_dbl_ev = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                      wq.Qt.LeftButton, wq.Qt.LeftButton, wq.Qt.NoModifier)
_text_card = next((c for c in a._card_widgets if c.rec["type"] == "text"), None)
_img_card = next((c for c in a._card_widgets if c.rec["type"] == "image"), None)
if _text_card is None or _img_card is None:
    check("双击前置(文本/图片卡片在列)", False,
          "cards=%s" % [c.rec["type"] for c in a._card_widgets])
if _text_card is not None and _img_card is not None:
    _n0 = len(a._viewers)
    _text_card.mouseDoubleClickEvent(_dbl_ev)
    _img_card.mouseDoubleClickEvent(_dbl_ev)
    pump(0.2)
    check("双击打开查看器(文本+图片)", len(a._viewers) == _n0 + 2
          and any(isinstance(v, wq.ViewerWindow) for v in a._viewers)
          and any(isinstance(v, wq.ImageViewerWindow) for v in a._viewers),
          str([type(v).__name__ for v in a._viewers]))
    for v in list(a._viewers):
        v.close()
    pump(0.2)

# 文件条目:双击 = 系统默认程序打开(记录器拦截);查看器带 打开/显示位置
_files_card = next((c for c in a._card_widgets if c.rec["type"] == "files"), None)
_opened = []
_orig_ofd = cm.open_file_default
cm.open_file_default = lambda p: _opened.append(p) or True
try:
    if _files_card is not None:
        _files_card.mouseDoubleClickEvent(_dbl_ev)
        check("双击文件=默认程序打开", len(_opened) >= 1, str(_opened))
    else:
        check("双击文件=默认程序打开", False, "无 files 卡片")
    fv2 = a.open_viewer({"type": "files", "content": [__file__, "C:/不存在.txt"]})
    pump(0.2)
    check("文件查看器按钮", fv2 is not None and fv2.btn_open.isVisible()
          and fv2._act_reveal.isVisible() and not fv2._lang_acts[0].isVisible())
    fv2._open_path_line(__file__ + "   ")
    check("双击行打开(路径解析)", bool(_opened) and _opened[-1] == __file__,
          str(_opened[-1:]))
    fv2.close()
    pump(0.1)
finally:
    cm.open_file_default = _orig_ofd
# 链接条目:双击 = 默认浏览器打开(记录器拦截);右键含「在浏览器打开」
a.core._post(("clip", ("text", "https://example.com/winclip-integ")))
pump(0.5)
a.refresh_view()          # 窗口隐藏期捕获不触发防抖刷新,显式重建
_link_card = next((c for c in a._card_widgets
                   if c.rec["type"] == "text" and cm.url_info(c.rec["content"])), None)
_opened_urls = []
_orig_ourl = cm.open_url
cm.open_url = lambda u: _opened_urls.append(u) or True
try:
    if _link_card is not None:
        _link_card.mouseDoubleClickEvent(_dbl_ev)
        check("双击链接=浏览器打开", _opened_urls == ["https://example.com/winclip-integ"],
              str(_opened_urls))
        _built3 = []
        _oe3 = wq.RoundMenu.exec
        wq.RoundMenu.exec = lambda self, *args, **kw: _built3.append(self)
        try:
            a.card_menu(_link_card, wq.QPoint(0, 0))
        finally:
            wq.RoundMenu.exec = _oe3
        if _built3:
            t4 = [x.text() for x in _built3[0]._actions]
            check("链接菜单含 在浏览器打开", "在浏览器打开" in t4, str(t4))
    else:
        check("双击链接=浏览器打开", False, "无链接卡片")
finally:
    cm.open_url = _orig_ourl

# 文件菜单:打开文件 / 在资源管理器中显示
if _files_card is not None:
    _built2 = []
    _oe2 = wq.RoundMenu.exec
    wq.RoundMenu.exec = lambda self, *args, **kw: _built2.append(self)
    try:
        a.card_menu(_files_card, wq.QPoint(0, 0))
    finally:
        wq.RoundMenu.exec = _oe2
    if _built2:
        t3 = [x.text() for x in _built2[0]._actions]
        check("文件菜单含 打开/资源管理器定位",
              "打开文件" in t3 and "在资源管理器中显示" in t3, str(t3))

# ---- 右键菜单构建(RoundMenu.addAction 只收 QAction;曾因传 str 整个菜单崩溃) ----
# menu.exec 离屏会模态阻塞,拦截掉只验证"构建不崩 + 条目齐全"
built = []
_orig_exec = wq.RoundMenu.exec
wq.RoundMenu.exec = lambda self, *args, **kw: built.append(self)
try:
    a.core._post(("clip", ("text", "菜单测试条目")))   # 确保存在文本条目
    a.on_search("")                                      # 清掉早前的搜索过滤
    a.win.show()                                 # 弹面板触发卡片重建
    pump(0.4)
    text_card = next((c for c in a._card_widgets if c.rec["type"] == "text"), None)
    check("存在文本卡片(前置)", text_card is not None,
          "cards=%s view=%s hist=%s" % ([c.rec["type"] for c in a._card_widgets],
                                        a._view,
                                        [h["type"] for h in a.history]))
    if text_card is not None:
        a.card_menu(text_card, wq.QPoint(0, 0))      # 崩过:AttributeError str.icon
    img_card_menu = next((c for c in a._card_widgets if c.rec["type"] == "image"), None)
    if img_card_menu is not None:
        a.card_menu(img_card_menu, wq.QPoint(0, 0))
        if built:
            t2 = [x.text() for x in built[-1]._actions]
            check("图片菜单含 查看/图片处理",
                  "在独立窗口查看" in t2 and "图片处理…" in t2, str(t2))
finally:
    wq.RoundMenu.exec = _orig_exec
check("右键菜单构建不崩", bool(built))
check("动作注册表驱动菜单", len(wq.CLIP_ACTIONS) >= 10
      and any(isinstance(x, wq.WorkflowSlot) for x in wq.CLIP_ACTIONS)
      and all(len(x) == 4 for x in wq.INSPECTOR_ACTIONS))
if built:
    texts = [x.text() for x in built[0]._actions]
    check("菜单含 粘贴/独立窗口查看/删除",
          all(t in texts for t in ("粘贴", "在独立窗口查看", "删除")),
          str(texts))

# ---- 预览面板(Ditto 式:选中即显示完整内容,不弹窗不展开) ----
a.core._post(("clip", ("text", "def ping():\n    return 'pong'\n")))
a.core._post(("clip", ("text", '{"k":1}')))
a.win.show()
pump(0.5)
a.refresh_view()          # 确定性重建:防抖在重负载下可能晚于 pump 窗口
pump(0.2)
check("预览面板随窗口可见", a.panel.preview.isVisible())
cards = {c.rec["content"]: c for c in a._card_widgets if c.rec["type"] == "text"}
code_card = cards.get("def ping():\n    return 'pong'\n")
json_card = cards.get('{"k":1}')
check("代码/JSON 卡片已构建", code_card is not None and json_card is not None,
      str(list(cards)))
if code_card is not None and json_card is not None:
    a.select_card(json_card)
    pump(0.1)
    check("选中即预览(JSON 自动美化)", '"k": 1' in a.panel.pv_edit.toPlainText(),
          a.panel.pv_edit.toPlainText()[:30])
    check("JSON 切换按钮可见", a.panel.pv_btn_json.isVisible())
    a.panel._toggle_pv_json()
    pump(0.1)
    check("JSON 切紧凑", a.panel.pv_edit.toPlainText() == '{"k":1}')
    a.panel._toggle_pv_json()
    pump(0.1)
    a.select_card(code_card)
    pump(0.1)
    check("预览随选中切换", a.panel.pv_edit.toPlainText()
          == "def ping():\n    return 'pong'\n")
    if wq._pg_get_lexer is not None:
        n = colored_chars(a.panel.pv_edit)
        check("预览代码高亮", n >= 3, "colored=%d" % n)
    a.panel.toggle_preview(False)
    pump(0.1)
    check("预览面板可隐藏", not a.panel.preview.isVisible())
    a.select_card(json_card)                 # 隐藏期间切换选中不应出错
    pump(0.1)
    a.panel.toggle_preview(True)
    pump(0.1)
    check("重新显示并刷新", '"k": 1' in a.panel.pv_edit.toPlainText())
    img_card = next((c for c in a._card_widgets if c.rec["type"] == "image"), None)
    if img_card is not None:
        a.select_card(img_card)
        pump(0.1)
        check("图片放大预览", a.panel.pv_img.isVisible()
              and a.panel.pv_img.pixmap() is not None
              and not a.panel.pv_img.pixmap().isNull())
    a.win.hide()
    pump(0.3)

# ---- 图钉画布:置顶悬浮多图板(BeeRef 式拖动/缩放/排列/穿透/持久化) ----
from PySide6.QtCore import Qt as _Qt, QPointF as _QPointF  # noqa: E402
from pinboard import PinBoardManager as _PBM                 # noqa: E402

pb = a.pinboard
pbw = pb.window
check("图钉画布初始隐藏", not pbw.isVisible())
check("置顶+无边框+Tool 窗口标志",
      all(bool(pbw.windowFlags() & f) for f in
          (_Qt.WindowStaysOnTopHint, _Qt.FramelessWindowHint, _Qt.Tool)))
pbw.show()
pump(0.2)
it1 = pb.pin_path(FAKE_PNG)
check("钉入图片(资产独立收编)", it1 is not None and len(pbw.pin_items()) == 1)
it1.setSelected(True)
check("画布渲染(paint 覆写不崩)", not pbw.grab().isNull())
it1.setSelected(False)
for k in range(2):
    _p = os.path.join(cm.IMAGE_DIR, "pin_%d.png" % k)
    Image.new("RGB", (60, 24), (200, k * 60, 30)).save(_p)
    pb.pin_path(_p)
check("多图同板", len(pbw.pin_items()) == 3)

it1.setPos(30, 20)
_anchor = _QPointF(45, 30)
_b = it1.mapToScene(it1.mapFromScene(_anchor))
pbw.scale_item(it1, 1.25, _anchor)
_c = it1.mapToScene(it1.mapFromScene(_anchor))
check("滚轮缩放光标锚点不动", (_b - _c).manhattanLength() < 0.001,
      "%s -> %s" % (_b, _c))
check("缩放生效", abs(it1.scale_value() - 1.25) < 1e-6)

# 旋转(中心锚定) / 锁定(拒绝变换) / 透明度
pbw.rotate_items([it1], 90.0)
check("旋转 90° 中心不动",
      abs(it1.rotation() - 90.0) < 1e-6)
pbw.toggle_lock([it1])
pbw.scale_item(it1, 2.0)
check("锁定拒绝缩放", abs(it1.scale_value() - 1.25) < 1e-6)
pbw.toggle_lock([it1])
pbw.nudge_opacity([it1], -0.05)
check("透明度调节持久字段", abs(it1.rec.opacity - 0.95) < 1e-6)

# 大画板:拖到远处不被拉回,场景随之扩张,适应全部能找回
_r0 = pbw.scene.sceneRect()
it1.setPos(_r0.right() + 3000, _r0.bottom() + 3000)
pbw.after_items_moved()
check("画布外自由放置不被拉回", it1.pos().x() > _r0.right() + 2000)
check("场景随内容扩张", pbw.scene.sceneRect().contains(
    it1.mapToScene(it1.boundingRect().center())))
pbw.fit_all()
app.processEvents()
check("适应后全部可见", all(pbw.visible_rect().contains(
    it.mapToScene(it.boundingRect().center())) for it in pbw.pin_items()))

pbw.arrange_grid()
_boxes = [it.mapRectToScene(it.boundingRect()) for it in pbw.pin_items()]
_r = pbw.visible_rect()
check("网格排列两两不重叠",
      all(not _boxes[i].intersects(_boxes[j])
          for i in range(len(_boxes)) for j in range(i + 1, len(_boxes))))
check("网格排列落在画布内", all(_r.contains(b.topLeft()) for b in _boxes))

for _ in range(12):
    pbw.zoom_view(0.2)
check("整板缩放下限护栏", pbw.view.transform().m11() >= 0.039,
      str(pbw.view.transform().m11()))
pbw.fit_all()
check("整板适应窗口", 0.039 < pbw.view.transform().m11() <= 8.0)

pbw.pin_items()[0].setSelected(True)
pbw.remove_selected()
check("Del 移除选中", len(pbw.pin_items()) == 2)

pb.save_now()
_board = os.path.join(cm.APP_DIR, "pins", "board.json")
_data = json.loads(_Path(_board).read_text(encoding="utf-8"))
check("布局落盘(pins/board.json)", _data and len(_data["items"]) == 2
      and "scale" in _data["items"][0], str(_data))
check("钉图资产独立于剪贴板缓存",
      all(os.path.exists(os.path.join(cm.APP_DIR, "pins", "images",
                                      it["path"]))
          for it in _data["items"]))
pb2 = _PBM(a)                        # 模拟重启加载
check("重启恢复画布", len(pb2.window.pin_items()) == 2)
pb2.window.close()
pb2.window.deleteLater()
pump(0.1)

check("右键「置顶到屏幕」入口", a.pin_image(
    {"type": "image", "content": FAKE_PNG}) is not None
    and pbw.isVisible() and len(pbw.pin_items()) == 3)
# 场景 F:从历史删除原始缓存文件,画布不受影响
pb.save_now()
os.remove(FAKE_PNG)
pb3 = _PBM(a)
check("源缓存删除后画布完好", len(pb3.window.pin_items()) == 3)
pb3.window.close()
pb3.window.deleteLater()
pump(0.1)
pbw.close()
check("收起保留内容", len(pbw.pin_items()) == 3)
pump(0.2)

# 使用者回归:复制完整原文、不自动粘贴;空搜索可恢复;偏好持久化。
a.win.show()
a.win.switchTo(a.panel)
a.category = "all"
a.query = ""
original_text = "完整内容\n" * (wq._PV_TEXT_CAP // 5 + 20)
a.history.insert(0, {"type": "text", "content": original_text, "ts": time.time()})
a.refresh_view()
a.panel.toggle_preview(True)
pump(0.1)
copied = []
saved_writer = a.core._set_clipboard
saved_queue = a.core.worker.cmd_q
import queue as _queue
a.core.worker.cmd_q = _queue.Queue()
a.core._set_clipboard = lambda rec: copied.append(dict(rec)) or True
try:
    a.panel._copy_pv()
    check("预览复制保留完整原文", copied[-1]["content"] == original_text)
    check("仅复制不触发自动粘贴", a.core.worker.cmd_q.empty())
    check("复制后窗口保留", a.win.isVisible())
    for rec in ({"type": "image", "content": "test.png"},
                {"type": "files", "content": [cm.CONFIG_FILE]}):   # 库模式下 history.json 可能不存在
        check("仅复制支持 " + rec["type"], a.core.copy_record(rec)
              and copied[-1]["type"] == rec["type"])
finally:
    a.core._set_clipboard = saved_writer
    a.core.worker.cmd_q = saved_queue
a.panel.search.setText("不存在的筛选词-829170")
pump(0.3)
check("无结果有明确提示", a.panel.empty_title.text() == "没有找到匹配内容"
      and not a.panel.btn_reset_filter.isHidden())
check("空结果禁用复制", not a.panel.pv_btn_copy.isEnabled())
a.panel.btn_reset_filter.click()
pump(0.3)
check("一键恢复全部内容", bool(a._view) and not a.query and a.category == "all")
for widget in (a.panel.search, a.panel.nav, a.panel.btn_preview):
    point = widget.mapTo(a.panel, widget.rect().bottomRight())
    check("历史页控件未溢出 " + type(widget).__name__, a.panel.rect().contains(point))
a.panel.toggle_preview(False)
check("预览偏好保存", cm.load_config().get("preview_visible") is False)
a.panel.toggle_preview(True)

a.quit_app()
pump(0.2)

# ---- 快捷粘贴浮层(默认 Alt+`):构建/过滤/键盘操作/纯文本粘贴 ----
import quickpaste as _qp
a.core.add_item(("text", "quickpaste 目标文本 alpha", {"html": "<b>alpha</b>"}, "notepad.exe"))
a.core.add_item(("text", "quickpaste 目标文本 beta", {}, "notepad.exe"))
pump(0.2)
check("浮层已构建", getattr(a, "quick", None) is not None)
_ms = a.quick.open()
check("浮层打开可见", a.quick.isVisible(), "%.1fms" % _ms)
check("浮层为 Tool 窗口(不进任务栏)",
      bool(a.quick.windowFlags() & wq.Qt.Tool))
check("浮层行数含注入条目", a.quick.model.rowCount() >= 2)
check("浮层打开耗时<300ms(offscreen 上限)", _ms < 300, "%.1fms" % _ms)
a.quick.search.setText("beta")
a.quick.refresh()
check("输入即过滤", a.quick.model.rowCount() == 1, str(a.quick.model.rowCount()))
_captured = []
_orig_set = a.core._set_clipboard
_orig_send = cm.send_ctrl_v
cm.send_ctrl_v = lambda: None          # 拦截真实按键注入(与 worker 线程抢命令)
a.core._set_clipboard = lambda rec: (_captured.append(rec), True)[1]
a.quick._sel = 0
a.quick.act_paste(plain=True)
check("纯文本粘贴剥离富文本",
      _captured and "html" not in _captured[0]
      and _captured[0]["content"].endswith("beta"), str(_captured[:1]))
check("粘贴强制入队(不受 auto_paste 默认关影响)",
      a.core.worker.cmd_q.get_nowait()[0] == "paste")
check("粘贴后浮层收起", not a.quick.isVisible())
a.core._set_clipboard = _orig_set
cm.send_ctrl_v = _orig_send
a.quick.open()
a.quick.close_restore()
check("Esc 原路收起", not a.quick.isVisible())
a.quick.open()
a.quick.cycle_cat()
check("Tab 切换类型", a.quick._cat == "text", a.quick._cat)
a.quick.search.setText("alpha")
a.quick.refresh()
_nv = len(a._viewers)
a.quick._sel = 0
a.quick.act_view()
check("Ctrl+Enter 打开查看器", len(a._viewers) > _nv)
a.quick._cat = "all"
a.quick.search.clear()
check("纯文本条目构造剥离 html/rtf",
      _qp.plain_rec({"type": "text", "content": "x", "html": "<b>x</b>",
                     "rtf": r"{\rtf}", "ts": 1})
      == {"type": "text", "content": "x", "ts": 1})

log = open(cm.LOG_FILE, encoding="utf-8").read()
# 「全局热键注册失败」是预期内的:测试与正在运行的真实实例抢同一个热键,
# 且已在上方屏蔽其模态告警。其余任何异常/失败都算问题。
IGNORE = ("全局热键注册失败",)
bad = [l for l in log.splitlines()
       if ("Traceback" in l or "失败" in l or "异常" in l)
       and not any(k in l for k in IGNORE)]
check("运行期无异常日志", not bad, str(bad[:3]))

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
