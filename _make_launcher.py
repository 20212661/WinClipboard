# -*- coding: utf-8 -*-
"""Emit 开发启动.vbs as UTF-16 (BOM) so WScript parses it correctly
regardless of the system codepage. ASCII-only source; paths are built
at runtime, no literals."""
import os
from pathlib import Path

root = Path(__file__).resolve().parent
exe = root / ".venv" / "Scripts" / "pythonw.exe"
assert exe.exists(), exe

rt_appdata = root / ".runtime_appdata"
vbs_lines = [
    "' WinClipboard dev launcher: isolated APPDATA (workspace copy of the",
    "' real profile) because this supervised session cannot write the real",
    "' %APPDATA%\\WinClipboard. Double-clicking this file works anywhere.",
    "Set sh = CreateObject(\"WScript.Shell\")",
    'sh.Environment("PROCESS")("APPDATA") = "%s"' % str(rt_appdata),
    'sh.CurrentDirectory = "%s"' % str(root).replace('"', '""'),
    'sh.Run "%s", 0, False'
    % (('"%s" -X utf8 winclipboard_qt.py' % exe).replace('"', '""')),
]
out = root / "\u5f00\u53d1\u542f\u52a8.vbs"          # 开发启动.vbs
out.write_text("\r\n".join(vbs_lines) + "\r\n", encoding="utf-16")
raw = out.read_bytes()
print("written:", out.name, len(raw), "bytes, BOM:", raw[:2] == b"\xff\xfe")
