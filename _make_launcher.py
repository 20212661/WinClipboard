# -*- coding: utf-8 -*-
"""Emit a UTF-16 (BOM) launcher vbs so WScript parses it correctly
regardless of the system codepage. ASCII-only source; paths are built
at runtime, no literals.

Usage: _make_launcher.py [script.py] [vbs-name]
  script.py  target script inside the repo (default winclipboard_qt.py)
  vbs-name   output vbs file name (default 开发启动.vbs)

The vbs sets PROCESS APPDATA to .runtime_appdata (isolated copy of the
real profile — the supervised session cannot write the real %APPDATA%)."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
exe = root / ".venv" / "Scripts" / "python.exe"
assert exe.exists(), exe
script = sys.argv[1] if len(sys.argv) > 1 else "winclipboard_qt.py"
vbs_name = sys.argv[2] if len(sys.argv) > 2 else "\u5f00\u53d1\u542f\u52a8.vbs"
rt_appdata = root / ".runtime_appdata"

vbs_lines = [
    "' WinClipboard dev launcher: isolated APPDATA (workspace copy of the",
    "' real profile). Double-clicking this file works anywhere.",
    "Set sh = CreateObject(\"WScript.Shell\")",
    'sh.Environment("PROCESS")("APPDATA") = "%s"' % str(rt_appdata),
    "sh.Environment(\"PROCESS\")(\"WCLB_SHOW_ON_START\") = \"1\"",
    'sh.CurrentDirectory = "%s"' % str(root).replace('"', '""'),
    'sh.Run "%s", 0, False'
    % (('"%s" -X utf8 %s' % (exe, script)).replace('"', '""')),
]
out = root / vbs_name
out.write_text("\r\n".join(vbs_lines) + "\r\n", encoding="utf-16")
raw = out.read_bytes()
print("written:", out.name, len(raw), "bytes, BOM:", raw[:2] == b"\xff\xfe")
