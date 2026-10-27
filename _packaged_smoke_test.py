# -*- coding: utf-8 -*-
"""Start the packaged EXE in isolated APPDATA, then exit via its tray command.

Run after building: .venv/Scripts/python.exe -X utf8 _packaged_smoke_test.py
Does not install the app, write the clipboard, or touch real user data.
"""
import base64
import hashlib
import json
import marshal
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import win32gui
import win32process
import clipboard_manager as cm
from app_meta import APP_VERSION
from store import ClipboardStore


def code_signature(code):
    """Compare packaged bytecode with source, ignoring relocated filenames."""
    return (code.co_code, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_argcount, code.co_kwonlyargcount,
            tuple(code_signature(c) if isinstance(c, types.CodeType) else c
                  for c in code.co_consts))


def verify_archive(exe):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    root = Path(__file__).resolve().parent
    for name in ("clipboard_manager", "app_meta", "store", "pipeline",
                 "workflow_steps", "workflow_runlog", "quickpaste",
                 "diagnostics", "perf_probe", "ai_quick",
                 "ui_base", "viewer", "image_editor", "workflow_ui",
                 "win32_host", "privacy_store", "history_store",
                 "ctxpack.model", "ctxpack.budget", "ctxpack.assemble",
                 "ctxpack.store", "ctxpack.service", "ctxpack.ui_tray",
                 "ai.registry", "ai.executor", "ai.session", "ai.sink",
                 "ai.workflows"):
        source = compile((root / (name.replace(".", os.sep) + ".py")).read_text(encoding="utf-8-sig"),
                         name + ".py", "exec")
        if code_signature(pyz.extract(name)) != code_signature(source):
            raise AssertionError("Packaged module differs from source: " + name)
    source = compile((root / "winclipboard_qt.py").read_text(encoding="utf-8-sig"),
                     "winclipboard_qt.py", "exec")
    if code_signature(marshal.loads(archive.extract("winclipboard_qt"))) != code_signature(source):
        raise AssertionError("Packaged UI differs from source")
    from ai.registry import ActionRegistry
    actions_dir = exe.parent / "_internal" / "ai" / "actions"
    expected = {"translate", "summarize", "code_explain", "code_optimize",
                "code_comment", "to_markdown"}
    reg = ActionRegistry()
    count = reg.scan_dir(actions_dir)
    if count != 6 or reg.errors or {item["id"] for item in reg.all()} != expected:
        raise AssertionError("Packaged actions invalid: dir=%s count=%s errors=%s ids=%s" %
                             (actions_dir, count, reg.errors,
                              [item["id"] for item in reg.all()]))
    return "matches current source; six packaged actions loaded"


def encrypted_json(records):
    blob = cm._dpapi_protect(json.dumps(records).encode("utf-8"))
    if blob is None:
        raise RuntimeError("DPAPI unavailable")
    return json.dumps({"dpapi": True, "data": base64.b64encode(blob).decode("ascii")})


def tray_host(pid):
    hosts = []
    def visit(hwnd, unused):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == pid \
                and win32gui.GetWindowText(hwnd) == "WinClipboardTrayHost":
            hosts.append(hwnd)
    win32gui.EnumWindows(visit, None)
    return hosts[0] if hosts else None


def session(exe, appdata):
    env = dict(os.environ, APPDATA=str(appdata), QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    log = appdata / "WinClipboard" / "app.log"
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("EXE exited before startup: %s" % proc.returncode)
            host = tray_host(proc.pid)
            boot = appdata / "WinClipboard" / "boot_state.json"
            if host and boot.exists() and json.loads(boot.read_text()).get("streak") == 0:
                break
            time.sleep(0.2)
        else:
            windows = []
            def inspect_window(hwnd, unused):
                if win32process.GetWindowThreadProcessId(hwnd)[1] == proc.pid:
                    texts = [win32gui.GetWindowText(hwnd)]
                    win32gui.EnumChildWindows(hwnd, lambda child, _: texts.append(
                        win32gui.GetWindowText(child)), None)
                    windows.append(texts)
            win32gui.EnumWindows(inspect_window, None)
            diagnostics = log.read_text(encoding="utf-8")[-4000:] if log.exists() else "no log"
            raise RuntimeError("Packaged startup timeout: windows=%r; log=%s" %
                               (windows, diagnostics))
        # Render the real history page; then use the app's normal exit path.
        win32gui.PostMessage(host, cm.WM_COMMAND, cm.M_SHOW, 0)
        time.sleep(2)
        win32gui.PostMessage(host, cm.WM_COMMAND, cm.M_QUIT, 0)
        result = proc.wait(timeout=20)
        if result != 0:
            raise RuntimeError("Packaged exit code: %s" % result)
        text = log.read_text(encoding="utf-8")
        if any(marker in text for marker in ("Traceback", "初始化失败", "未捕获异常")):
            raise RuntimeError("Packaged log contains an error")
        boot_state = json.loads(boot.read_text())
        if boot_state.get("running") is not False:
            raise RuntimeError("Packaged clean exit not recorded")
        return {"startup": "ok", "render": "ok", "exit_code": result,
                "clean_exit": True, "log_errors": 0}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def build_provenance(root):
    """构建来源元数据:HEAD/脏标记/关键源文件摘要/依赖版本/时间。
    未提交的工作区不能只用 HEAD 代表构建来源——源文件 SHA-256 一并留档。"""
    def git(*args):
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                              text=True, encoding="utf-8",
                              errors="replace").stdout.strip()
    prov = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_head": git("rev-parse", "HEAD") or "unknown",
        "git_dirty": bool(git("status", "--porcelain")),
        "source_sha256": {},
        "dependencies": {},
    }
    for name in ("winclipboard_qt.py", "clipboard_manager.py", "store.py",
                 "pipeline.py", "workflow_steps.py", "workflow_runlog.py",
                 "quickpaste.py", "diagnostics.py", "app_meta.py",
                 "winclipboard.spec"):
        p = root / name
        if p.exists():
            prov["source_sha256"][name] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    try:
        import importlib.metadata as md
        for pkg in ("PySide6", "PySide6-Fluent-Widgets", "pywin32", "Pillow",
                    "cryptography", "PyInstaller"):
            try:
                prov["dependencies"][pkg] = md.version(pkg)
            except Exception:
                prov["dependencies"][pkg] = "not-installed"
    except Exception:
        pass
    prov["python"] = sys.version.split()[0]
    return prov


def main():
    exe = Path(sys.argv[1] if len(sys.argv) > 1 else
               "dist/WinClipboard/WinClipboard.exe").resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    # Suppress event-based captures from current applications. The app may
    # still take an initial snapshot; all such data remains in this temporary
    # directory and is removed when the session finishes.
    processes = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process | Select-Object -ExpandProperty ProcessName"],
        capture_output=True, check=True).stdout.decode("utf-8", "replace").splitlines()
    excluded = [name.lower() + ".exe" for name in processes]
    excluded += ["winclipboard.exe", cm.foreground_app_exe()]
    report = {"version": APP_VERSION, "exe": exe.name,
              "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "exe_built_at": time.strftime(
                  "%Y-%m-%dT%H:%M:%S",
                  time.localtime(exe.stat().st_mtime)),
              "build": build_provenance(Path(__file__).resolve().parent),
              "archive": verify_archive(exe), "modes": {}}
    for encrypted in (False, True):
        with tempfile.TemporaryDirectory(prefix="wclb_packaged_smoke_") as tmp:
            appdata = Path(tmp)
            data = appdata / "WinClipboard"
            data.mkdir()
            cfg = dict(cm.DEFAULT_CFG, max_items=1000, sensitive_guard=False,
                       encrypt_history=encrypted, exclude_apps=excluded,
                       hotkey_mod=6, hotkey_key=0x87,
                       quick_mod=6, quick_key=0x86, mica=False,
                       welcome_seen=True, last_update_check=time.time())
            (data / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            records = [{"type": "text", "content": "packaged-row-%04d" % i,
                        "ts": time.time(), "pinned": False} for i in range(405)]
            store = ClipboardStore(str(data / "clipboard.db"))
            store.migrate_from_list(records if not encrypted else [{
                "type": "text", "content": "stale-database-marker", "ts": time.time()}])
            store.close()
            if encrypted:
                (data / "history.json").write_text(encrypted_json(list(reversed(records))),
                                                   encoding="utf-8")
                for suffix in (".bak1", ".bak2", ".bak3", ".migrated"):
                    (data / ("history.json" + suffix)).write_text(
                        json.dumps(records), encoding="utf-8")
            sessions = [session(exe, appdata), session(exe, appdata)]
            if encrypted:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    if (data / ("clipboard.db" + suffix)).exists():
                        raise AssertionError("Plaintext SQLite residue: " + suffix)
                for suffix in ("", ".bak1", ".bak2", ".bak3", ".migrated"):
                    payload = json.loads((data / ("history.json" + suffix)).read_text())
                    if payload.get("dpapi") is not True:
                        raise AssertionError("Unencrypted JSON residue")
                loaded = cm._history_from_container(json.loads(
                    (data / "history.json").read_text()))
            else:
                store = ClipboardStore(str(data / "clipboard.db"))
                loaded = store.load_all()
                store.close()
            expected = {r["content"] for r in records}
            seeded = [r for r in loaded if r["content"] in expected]
            if len(seeded) != 405 or {r["content"] for r in seeded} != expected \
                    or any(r["content"] == "stale-database-marker" for r in loaded):
                raise AssertionError("Packaged restart did not preserve all seeded history")
            name = "encrypted" if encrypted else "sqlite"
            report["modes"][name] = {"sessions": sessions, "seeded_history_count": len(seeded)}
            print("PASS", name, "two starts + renders + clean exits; 405 records retained")
    target = exe.parent / "build-verification.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS packaged smoke test; report:", target)


if __name__ == "__main__":
    main()
