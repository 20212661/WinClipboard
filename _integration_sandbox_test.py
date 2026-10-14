"""Legacy UI integration suite with isolated data and system clipboard stand-ins.

This checks UI/core integration, not real OS clipboard or physical hotkeys.
"""
import os
import queue
import runpy
import tempfile

with tempfile.TemporaryDirectory(prefix="wclb_integration_sandbox_") as sandbox:
    os.environ["APPDATA"] = sandbox
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import clipboard_manager as cm

    clipboard = {}

    class ControlQueue(queue.Queue):
        def put(self, command, *args, **kwargs):
            # The disabled native worker would ordinarily consume control
            # commands before the later paste assertion. Keep only data jobs.
            if command[0] != "paste":
                return
            return super().put(command, *args, **kwargs)

    def worker_start(worker):
        worker.cmd_q = ControlQueue()

    cm.TrayWorker.start = worker_start
    cm.send_ctrl_v = lambda: None
    cm.win32clipboard.OpenClipboard = lambda *args: None
    cm.win32clipboard.CloseClipboard = lambda: None
    cm.win32clipboard.EmptyClipboard = lambda: clipboard.clear()
    cm.win32clipboard.SetClipboardData = lambda kind, value: clipboard.__setitem__(kind, value)
    cm.win32clipboard.IsClipboardFormatAvailable = lambda kind: kind in clipboard
    cm.win32clipboard.GetClipboardData = lambda kind: clipboard[kind]
    runpy.run_path("_integration_test.py", run_name="__main__")
