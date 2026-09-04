@echo off
chcp 65001 >nul
REM WinClipboard launcher (console version for debugging; normally use the .vbs or desktop shortcut).
REM Auto-selects the entry point: prefers the Qt + Fluent version; falls back to the tkinter
REM version when PySide6 is not installed.
REM NOTE: keep this file pure ASCII to avoid codepage parsing issues on Chinese Windows.
setlocal
set "PYW=C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe"
set "PY=C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    echo Please make sure the hermes venv exists with pywin32 and Pillow installed.
    pause
    exit /b 1
)

REM Probe Qt dependencies; fall back to tkinter (needs only pywin32 + Pillow).
"%PY%" -c "import PySide6, qfluentwidgets" >nul 2>&1
if %errorlevel%==0 (
    set "SCRIPT=%~dp0winclipboard_qt.py"
    set "MODE=Qt + Fluent"
) else (
    set "SCRIPT=%~dp0clipboard_manager.py"
    set "MODE=tkinter fallback - PySide6 not found"
)

if exist "%PYW%" (
    start "" "%PYW%" "%SCRIPT%"
    echo WinClipboard started [%MODE%]. Tray icon running; press Ctrl+Shift+V to open the panel.
    ping 127.0.0.1 -n 3 >nul
    exit /b 0
)
"%PY%" "%SCRIPT%"
exit /b %errorlevel%
