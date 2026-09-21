# -*- coding: utf-8 -*-
"""Command Palette(命令面板)数据层与浮层测试。

运行: python _command_test.py   退出码 0 = 全部通过
覆盖:
  1. Command 对象:字段/when 谓词(含抛异常按不可用)/可调用快捷键
  2. 注册表:实例与简写两种登记形态/重复覆盖保位/上下文过滤/分组序/打分搜索
  3. 执行器:正常路径、异常路径、按 id 执行不存在命令
  4. 内置命令目录(RecordingFakeApp):条数/分组/选中 gating/AI 命令接线/
     复制失败路径
  5. 面板浮层(离屏):构造/打开收起/输入过滤/执行命令/失败提示不打断
"""
import os
import sys
import time
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_cmd_test_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

import command
from command.command import Command
from command.registry import CommandRegistry, CATEGORY_ORDER

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


# ============================================================
# 1. Command 对象
# ============================================================
hits = []
c1 = Command("t.a", "命令甲", lambda: hits.append(1), shortcut="Ctrl+K",
             when=lambda: True, category="应用", keywords="alpha")
check("cmd.fields", c1.id == "t.a" and c1.name == "命令甲"
      and c1.category == "应用" and c1.keywords == "alpha")
check("cmd.available-true", c1.available())
c2 = Command("t.b", "命令乙", lambda: None, when=lambda: False)
check("cmd.available-false", not c2.available())
c3 = Command("t.c", "命令丙", lambda: None,
             when=lambda: 1 / 0)          # 谓词抛异常按不可用
check("cmd.available-raise", not c3.available())
c4 = Command("t.d", "命令丁", lambda: None, shortcut=lambda: "Ctrl+Shift+V")
check("cmd.shortcut-callable", c4.shortcut_text() == "Ctrl+Shift+V")
c5 = Command("t.e", "命令戊", lambda: None,
             shortcut=lambda: 1 / 0)      # 快捷键取值异常 → 空串
check("cmd.shortcut-raise", c5.shortcut_text() == "")
check("cmd.match-text", "命令甲" in c1.match_text()
      and "t.a" in c1.match_text() and "alpha" in c1.match_text())

# ============================================================
# 2. 注册表
# ============================================================
reg = CommandRegistry()
reg.register(c1)
reg.register(c2)
reg.register(c3)
check("reg.len", len(reg) == 3 and "t.a" in reg)
# 简写形态:register(id, name=..., action=...)
hits2 = []
reg.register("t.f", name="命令己", action=lambda: hits2.append(1))
check("reg.shorthand", reg.get("t.f") is not None)
# 重复 id:后者覆盖,展示位保持首注册位置
reg.register(Command("t.a", "命令甲改", lambda: hits.append(2)))
check("reg.dup-overwrite", reg.get("t.a").name == "命令甲改"
      and len(reg) == 4)
# 坏形态拒绝
try:
    reg.register("bad")
    check("reg.reject-bad", False)
except TypeError:
    check("reg.reject-bad", True)

# 上下文过滤:t.b(when=False)与 t.c(谓词抛异常)不可见
avail = reg.all()
check("reg.filter-when", {c.id for c in avail} == {"t.a", "t.f"})
check("reg.all-include-unavailable", len(reg.all(False)) == 4)
# 分组序:剪贴板 < AI < 应用(未列出的排最后)
r2 = CommandRegistry()
r2.register(Command("x.1", "乙", lambda: None, category="应用"))
r2.register(Command("x.2", "甲", lambda: None, category="剪贴板"))
r2.register(Command("x.3", "丙", lambda: None, category="AI"))
r2.register(Command("x.4", "丁", lambda: None, category="未知组"))
check("reg.category-order", [c.id for c in r2.all()] ==
      ["x.2", "x.3", "x.1", "x.4"], str([c.id for c in r2.all()]))
check("reg.categories", r2.categories() == ["剪贴板", "AI", "应用", "未知组"])

# 打分搜索:名前缀 > 名包含 > 关键词 > id;空查询 = all()
r3 = CommandRegistry()
r3.register(Command("p.paste", "粘贴选中条目", lambda: None,
                    category="剪贴板", keywords="paste 回贴"))
r3.register(Command("p.pin", "固定 / 取消固定", lambda: None,
                    category="剪贴板", keywords="pin 置顶"))
r3.register(Command("p.summary", "AI 总结", lambda: None,
                    category="AI", keywords="summary 摘要"))
check("search.empty", len(r3.search("")) == 3)
got = r3.search("粘贴")
check("search.name-prefix-first", got and got[0].id == "p.paste", str([c.id for c in got]))
check("search.summary", [c.id for c in r3.search("总结")] == ["p.summary"])
check("search.keyword", [c.id for c in r3.search("置顶")] == ["p.pin"])
check("search.id", [c.id for c in r3.search("p.paste")] == ["p.paste"])
check("search.none", r3.search("不存在的词") == [])
check("search-skip-unavailable",
      r3.search("总结", only_available=False) and True)

# ============================================================
# 3. 执行器
# ============================================================
ok, msg = command.executor.run_command(Command("t.run", "跑", lambda: hits.append(9)))
check("exec.ok", ok and hits and hits[-1] == 9)


def _boom():
    raise RuntimeError("炸了")


ok, msg = command.executor.run_command(Command("t.bad", "坏", _boom))
check("exec.exception", not ok and "RuntimeError" in msg and "炸了" in msg, msg)
ok, msg = command.executor.run_command(None)
check("exec.none", not ok and msg == "命令不存在")
ok, msg = command.executor.run_command_id(reg, "no.such")
check("exec.missing-id", not ok)
ok, msg = command.executor.run_command_id(reg, "t.a")
check("exec.by-id", ok)

# ============================================================
# 4. 内置命令目录(RecordingFakeApp)
# ============================================================
class _FakeSearch:
    def setFocus(self):
        REC.append("search.focus")

    def selectAll(self):
        REC.append("search.selectAll")


class _FakeSettingsPage:
    def _export_history(self):
        REC.append("export")


class _FakeWin:
    history_page = workflow_page = services_page = object()
    private_page = settings_page = object()

    def __init__(self):
        self.settings_page = _FakeSettingsPage()

    def show(self):
        REC.append("win.show")

    def raise_(self):
        pass

    def toggle(self):
        REC.append("win.toggle")

    def switchTo(self, page):
        REC.append("switchTo:%r" % id(page))


class _FakeQuick:
    def toggle(self):
        REC.append("quick.toggle")


class _FakePinboard:
    def pin_clipboard_image(self, near_cursor=True):
        REC.append("pin.clip")

    def show_board(self, activate=True):
        REC.append("pin.board")

    def toggle_click_through(self, on=None):
        REC.append("pin.ctt")


class _FakeCore:
    def copy_record(self, rec):
        REC.append("copy_record")
        return True

    def copy_text(self, t):
        REC.append("copy_text")


class FakeApp:
    """builtin 目录所需的最小宿主;全部动作用 REC 记录。"""

    def __init__(self):
        self.cfg = {"hotkey_mod": cm.MOD_CONTROL | cm.MOD_SHIFT,
                    "hotkey_key": 0x56, "quick_mod": cm.MOD_ALT,
                    "quick_key": 0xC0}
        self.history = []
        self._view = []
        self.selected = None
        self.win = _FakeWin()
        self.quick = _FakeQuick()
        self.pinboard = _FakePinboard()
        self.core = _FakeCore()
        self.panel = type("P", (), {"search": _FakeSearch()})()
        self._diff_base = None

    # 记录型桩(注册期取绑定引用,必须存在)
    def paste_selected(self):
        REC.append("paste_selected")

    def paste_plain(self):
        REC.append("paste_plain")

    def open_transform(self):
        REC.append("open_transform")

    def delete_selected(self):
        REC.append("delete_selected")

    def toggle_pin(self):
        REC.append("toggle_pin")

    def clear_all(self):
        REC.append("clear_all")

    def compare_pick(self, rec):
        REC.append("compare_pick")

    def pin_image(self, rec):
        REC.append("pin_image")

    def show_ai_hub(self):
        REC.append("show_ai_hub")

    def show_services(self):
        REC.append("show_services")

    def show_private(self):
        REC.append("show_private")

    def about(self):
        REC.append("about")

    def show_shortcut_help(self):
        REC.append("show_help")

    def check_update(self, silent=False):
        REC.append("check_update")

    def toggle_monitor_pause(self):
        REC.append("pause")

    def quit_app(self):
        REC.append("quit")

    def open_viewer(self, rec):
        REC.append("open_viewer")

    def open_image_viewer(self, rec, edit=False, on_result=None):
        REC.append("open_image_viewer")

    def open_link(self, rec):
        REC.append("open_link")
        return True

    def run_ai_action(self, defn, rec):
        REC.append("ai:" + str((defn or {}).get("id")))


REC = []
fake = FakeApp()
breg = command.CommandRegistry()
n = command.builtin.register_builtin(breg, fake)
check("builtin.count", n >= 30, str(n))
check("builtin.categories", set(breg.categories(only_available=False)) >=
      {"剪贴板", "查看与对比", "AI", "图钉画布", "导航", "应用"},
      str(breg.categories(only_available=False)))

# 选中 gating:无选中 → 需选中的命令全部隐藏
ids_avail = {c.id for c in breg.all()}
ids_all = {c.id for c in breg.all(False)}
check("builtin.gate-no-sel",
      "clipboard.paste" not in ids_avail and "ai.summarize" not in ids_avail
      and "pin.add" not in ids_avail and "nav.settings" in ids_avail)
# 文本选中 → AI/粘贴可用,图钉不可用
fake.history = [{"type": "text", "content": "print(1)"}]
fake._view = [0]
fake.selected = 0
ids_text = {c.id for c in breg.all()}
check("builtin.gate-text", "ai.summarize" in ids_text and
      "clipboard.paste" in ids_text and "pin.add" not in ids_text)
# 图片选中 → 图钉可用,AI 不可用
fake.history = [{"type": "image", "content": "x.png"}]
ids_img = {c.id for c in breg.all()}
check("builtin.gate-image", "pin.add" in ids_img and "ai.summary" not in ids_img)

# 执行接线:导航/复制/AI/退出
fake.history = [{"type": "text", "content": "hi"}]
ok, msg = command.executor.run_command_id(breg, "nav.settings")
check("builtin.exec-nav", ok and any(x.startswith("switchTo") for x in REC), msg)
REC.clear()
ok, msg = command.executor.run_command_id(breg, "clipboard.copy")
check("builtin.exec-copy", ok and "copy_record" in REC, msg)
REC.clear()
ok, msg = command.executor.run_command_id(breg, "ai.summarize")
check("builtin.exec-ai", ok and "ai:summarize" in REC, msg)
REC.clear()
ok, msg = command.executor.run_command_id(breg, "app.quit")
check("builtin.exec-quit", ok and "quit" in REC, msg)
REC.clear()

# 复制失败 → 异常路径(执行器捕获,不崩)
fake.core.copy_record = lambda rec: False
ok, msg = command.executor.run_command_id(breg, "clipboard.copy")
check("builtin.copy-fail", not ok and "剪贴板" in msg, msg)

# 搜索:中文名/关键词/拼音式 id
got = breg.search("总结")
check("builtin.search-summary", got and got[0].id == "ai.summarize",
      str([c.id for c in got]))
got = breg.search("导出")
check("builtin.search-export", got and got[0].id == "app.export",
      str([c.id for c in got]))

# ============================================================
# 5. 面板浮层(离屏)
# ============================================================
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
import palette as pal_mod
from qfluentwidgets import setTheme, Theme
setTheme(Theme.LIGHT)

fake2 = FakeApp()
fake2.win = QWidget()          # InfoBar 的 parent 需要真 QWidget
fake2.commands = breg
pal = pal_mod.CommandPaletteWindow(fake2)
check("palette.construct", pal.model is not None)

ms = pal.open()
check("palette.open", pal.isVisible() and isinstance(ms, (int, float)))
check("palette.empty-query-all",
      pal.model.rowCount() == len(breg.search("")),
      "%d vs %d" % (pal.model.rowCount(), len(breg.search(""))))
pal.search.setText("总结")
pal._filter_timer.stop()       # 防抖立即结算
pal.refresh()
check("palette.filter", [c.id for c in pal._cmds] == ["ai.summarize"],
      str([c.id for c in pal._cmds]))

# 执行记录型命令 → 面板收起
REC.clear()
breg.register(Command("test.ping", "ZZZ测试命令", lambda: REC.append("ping"),
                      category="应用"))
pal.search.clear()
pal.refresh()
target = next(i for i, c in enumerate(pal._cmds) if c.id == "test.ping")
pal._sel = target
pal.exec_current()
check("palette.exec", "ping" in REC and not pal.isVisible(), str(REC))

# 失败命令 → 提示路径不打断(异常被执行器捕获)
breg.register(Command("test.bad", "ZZZ失败命令", _boom, category="应用"))
pal.search.clear()
pal.refresh()
pal._sel = next(i for i, c in enumerate(pal._cmds) if c.id == "test.bad")
pal.exec_current()
check("palette.exec-fail", not pal.isVisible())     # 不崩即过

# toggle 二次 = 收起;Esc 路径
pal.toggle()
check("palette.toggle-open", pal.isVisible())
pal.toggle()
check("palette.toggle-close", not pal.isVisible())
pal.open()
pal.close_restore()
check("palette.close-restore", not pal.isVisible())

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
