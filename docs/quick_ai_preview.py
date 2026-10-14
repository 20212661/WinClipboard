"""Native desktop preview with isolated data and no background capture.

No AI service is preconfigured and no request is made automatically.
The app remains running so its real hotkey and new UI can be tried.
"""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.environ["APPDATA"] = str(root / ".runtime_quick_ai_preview")
os.environ["WCLB_SHOW_ON_START"] = "1"
os.environ["WCLB_NO_MICA"] = "1"
os.environ.pop("QT_QPA_PLATFORM", None)

import clipboard_manager as cm
import winclipboard_qt as wq
from PySide6.QtCore import QTimer

# Keep the native tray/WM_HOTKEY loop; do not record personal clipboard data.
cm.ClipboardCore.read_clipboard_now = lambda self: None
original_app = wq.WinClipboardApp


def create_app(qapp, safe_mode=False):
    app = original_app(qapp, safe_mode=safe_mode)
    def show_preview():
        app.open_quick_ai({"type": "text", "content": "复制文字后，按 Ctrl＋Alt＋空格选择 AI 动作。"})
        app._quick_ai.wheel.hide()
        app._quick_ai.select_action("ask")
        (root / ".runtime_quick_ai_preview" / "ready.txt").write_text(str(os.getpid()))
    QTimer.singleShot(800, show_preview)
    return app


wq.WinClipboardApp = create_app
if __name__ == "__main__":
    sys.exit(wq.main())
