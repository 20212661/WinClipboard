# -*- coding: utf-8 -*-
"""阶段 8 拆分回归:被搬 UI 子系统离屏构造 + 调色板多模块同步。

Run: .venv/Scripts/python.exe -X utf8 _module_split_test.py
覆盖:
  1. init_palette 同步 ui_base / winclipboard_qt / viewer / image_editor
     四个模块的调色板全局(浅色与深色两轮)
  2. ImageEditor + CompressDialog/ScaleDialog 构造(真实临时 PNG)
  3. ViewerWindow 九类内容构造 + 渲染像素非空 + INSPECTOR_ACTIONS 注册表
  4. DiffWindow(文本 unified + JSON 语义)构造与渲染
  5. ImageViewerWindow(查看态 + 编辑态内嵌 ImageEditor)
  6. 兼容重导出:wq 命名空间仍暴露全部被搬公开名
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_manager as cm

TMP = tempfile.mkdtemp(prefix="wclb_split_")
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.CONFIG_FILE = os.path.join(TMP, "config.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QIcon
from PySide6.QtCore import QObject

app = QApplication([])
app.setQuitOnLastWindowClosed(False)

import ui_base
import viewer
import image_editor
import winclipboard_qt as wq
from qfluentwidgets import setTheme, Theme
setTheme(Theme.LIGHT)

# ---- 1. 调色板多模块同步 ----
wq.init_palette(False)
check("palette.light-sync",
      wq.BG == ui_base.BG == viewer.BG == image_editor.BG
      and wq.ACCENT == viewer.ACCENT == image_editor.ACCENT
      and wq.IS_DARK is False)
setTheme(Theme.DARK)
wq.init_palette(True)
check("palette.dark-sync",
      wq.BG == ui_base.BG == viewer.BG == image_editor.BG == "#1e1e1e"
      and wq.DIFF_ADD == viewer.DIFF_ADD == image_editor.DIFF_ADD == "#5ec97f"
      and wq.IS_DARK is True and viewer.IS_DARK is True)
setTheme(Theme.LIGHT)
wq.init_palette(False)


class FakeCore:
    copied = []

    def copy_text(self, t):
        self.copied.append(t)
        return True

    def copy_record(self, rec):
        return True


class FakeApp(QObject):
    def __init__(self):
        super().__init__()
        self.cfg = {"ai_services": [], "ai_workflows": []}
        self.core = FakeCore()
        self.icon = QIcon()
        self.win = QWidget()
        self._viewers = []
        self._thumbs = {}
        self._diffs = []

    def show_services(self):
        pass


fake = FakeApp()

# ---- 2. 图片编辑器族 ----
from PIL import Image
png = os.path.join(TMP, "edit-src.png")
Image.new("RGB", (320, 200), "#3366aa").save(png)
ed = image_editor.ImageEditor(
    fake, {"type": "image", "content": png, "w": 320, "h": 200})
check("editor.construct", ed is not None)
check("editor.render", not ed.grab().isNull())
cd = image_editor.CompressDialog(QWidget())
check("editor.compress-dialog", cd is not None)

# ---- 3. 内容查看器(九类内容) ----
RECS = {
    "plain": {"type": "text", "content": "普通文本\n第二行", "ts": 1, "src": ""},
    "json": {"type": "text", "content": '{"k": [1, 2], "s": "v"}', "ts": 2},
    "code": {"type": "text", "content": "def f():\n    return 1\n", "ts": 3},
    "url": {"type": "text",
            "content": "https://example.com/p?a=1&b=2", "ts": 4},
    "jwt": {"type": "text", "content":
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.eyJzaWduIn0.x", "ts": 5},
    "base64": {"type": "text", "content":
               "data:text/plain;base64," +
               __import__("base64").b64encode("解码文本".encode()).decode(),
               "ts": 6},
    "markdown": {"type": "text", "content": "# 标题\n\n- 列表项\n", "ts": 7},
    "csv": {"type": "text", "content": "a,b\n1,2\n3,4\n", "ts": 8},
    "xml": {"type": "text", "content": "<r><a>1</a></r>", "ts": 9},
    "files": {"type": "files", "content": [png, "C:/无/2.txt"], "ts": 10},
}
for name, rec in RECS.items():
    try:
        v = viewer.ViewerWindow(fake, rec)
        ok = not v.grab().isNull()          # 未 show 也离屏可渲染;构造即验
        v.close()
        check("viewer.%s" % name, ok)
    except Exception as e:
        check("viewer.%s" % name, False, "%s: %s" % (type(e).__name__, e))
v = viewer.ViewerWindow(fake, RECS["url"])
acts = v._inspector_actions()
check("viewer.inspector-actions", len(acts) >= 2, str(acts))
v.close()
check("viewer.registry-same-object",
      wq.INSPECTOR_ACTIONS is viewer.INSPECTOR_ACTIONS
      and all(len(x) == 4 for x in wq.INSPECTOR_ACTIONS))
from PySide6.QtGui import QTextDocument
_doc = QTextDocument()
_doc.setPlainText("def f():\n    pass\n")   # 语义:调用方先设文本,助手只上色
wq.apply_pygments_format(_doc, "def f():\n    pass\n", "python")
check("viewer.pygments-helper", _doc.characterCount() > 10)

# ---- 4. DiffWindow ----
d = viewer.DiffWindow(fake, {"type": "text", "content": "a\nb\nc\n"},
                      {"type": "text", "content": "a\nx\nc\n"})
check("diff.text", not d.grab().isNull())
d2 = viewer.DiffWindow(fake, {"type": "text", "content": '{"a":1,"b":2}'},
                       {"type": "text", "content": '{"b":2,"a":3}'})
check("diff.json-semantic", not d2.grab().isNull())
d.close()
d2.close()

# ---- 5. ImageViewerWindow(查看态 + 编辑态) ----
iv = viewer.ImageViewerWindow(fake, {"type": "image", "content": png,
                                     "w": 320, "h": 200})
check("imageviewer.view", not iv.grab().isNull())
iv.close()
ive = viewer.ImageViewerWindow(
    fake, {"type": "image", "content": png, "w": 320, "h": 200}, edit=True)
check("imageviewer.embedded-editor", ive._editor is not None
      and not ive.grab().isNull())
ive.close()

# ---- 6. 兼容重导出 ----
for nm in ("ViewerWindow", "ImageViewerWindow", "DiffWindow",
           "INSPECTOR_ACTIONS", "_copy_or_warn", "apply_pygments_format",
           "ImageEditor", "CompressDialog", "ScaleDialog",
           "init_palette", "resolve_theme", "apply_fonts", "font", "elided",
           "icon_tile", "Badge", "ensure_onscreen", "rounded_pixmap",
           "ServicesPage", "WorkflowPage", "WorkflowEditorWindow",
           "WorkflowRunner", "WorkflowRunInspector", "ServiceEditorDialog",
           "ProviderCard", "PrivacyGateDialog", "AIWorker", "ModelWorker"):
    check("compat.%s" % nm, hasattr(wq, nm))

# ---- 7. workflow_ui 拆分:同一对象 + 构造 + 调色板同步 ----
import workflow_ui
check("workflowui.same-objects",
      wq.WorkflowRunner is workflow_ui.WorkflowRunner
      and wq.ServicesPage is workflow_ui.ServicesPage
      and wq.WorkflowPage is workflow_ui.WorkflowPage)
check("workflowui.palette-sync", workflow_ui.BG == wq.BG
      and workflow_ui.IS_DARK is False)
sp = wq.ServicesPage(fake)
check("workflowui.servicespage", sp is not None and not sp.grab().isNull())
wp = wq.WorkflowPage(fake)
check("workflowui.workflowpage", wp is not None and not wp.grab().isNull())
fake.cfg["ai_workflows"] = [
    {"name": "拆分自检", "steps": [
        {"kind": "ai", "service": "A", "prompt": "P:{input}"}]}]
wp.refresh()
check("workflowui.list-refresh", wp.lst.count() == 1)
we = wq.WorkflowEditorWindow(fake, fake.cfg["ai_workflows"][0], None)
check("workflowui.editor", len(we._rows) == 1)
we.close()

print()
print("RESULT:", "PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
