@echo off
chcp 65001 >nul
REM WinClipboard launcher (console version for debugging; normally use the .vbs or desktop shortcut).
REM Single entry point: winclipboard_qt.py (PySide6 + PySide6-Fluent-Widgets required).
REM
REM Interpreter resolution order:
REM   1. python.txt next to this script: one line, full path of pythonw.exe (explicit override)
REM   2. .venv next to this script - the project's OWN environment (preferred)
REM   3. hermes venv interpreter (LAST RESORT ONLY: running from it locks the
REM      hermes agent venv and blocks Hermes self-updates until this app closes)
REM   4. pythonw.exe found on PATH
REM
REM NOTE: keep this file pure ASCII to avoid codepage parsing issues on Chinese Windows.
setlocal EnableDelayedExpansion
set "PYW="

REM 1) explicit override file
if exist "%~dp0python.txt" (
    for /f "usebackq delims=" %%i in ("%~dp0python.txt") do (
        if not defined CFGPY set "CFGPY=%%i"
    )
)
if defined CFGPY if exist "!CFGPY!" set "PYW=!CFGPY!"

REM 2) project-local venv
if "!PYW!"=="" (
    if exist "%~dp0.venv\Scripts\pythonw.exe" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
    if not defined PYW if exist "%~dp0.venv\Scripts\python.exe" set "PYW=%~dp0.venv\Scripts\python.exe"
)

REM 3) hermes venv (last resort - see header note)
if "!PYW!"=="" (
    set "HPY=C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
    if exist "!HPY!" (
        set "PYW=!HPY:\python.exe=\pythonw.exe!"
        if not exist "!PYW!" set "PYW=!HPY!"
    )
)

REM 4) pythonw.exe on PATH
if "!PYW!"=="" (
    where pythonw.exe >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where pythonw.exe') do (
            if "!PYW!"=="" set "PYW=%%i"
        )
    )
)

if "!PYW!"=="" (
    echo [ERROR] No Python interpreter found.
    echo Create python.txt next to this script containing the full path of
    echo pythonw.exe, or put pythonw.exe on PATH. It needs PySide6,
    echo PySide6-Fluent-Widgets, pywin32 and Pillow.
    pause
    exit /b 1
)

REM console twin for probing (same directory python.exe; fall back to PYW itself)
set "PY=!PYW:\pythonw.exe=\python.exe!"
if not exist "!PY!" set "PY=!PYW!"

REM Qt + Fluent is the only UI; verify dependencies and show guidance if missing
"!PY!" -c "import PySide6, qfluentwidgets" >nul 2>&1
if not !errorlevel!==0 (
    echo [ERROR] PySide6 / PySide6-Fluent-Widgets not installed in the interpreter above.
    echo Install with: pip install PySide6 PySide6-Fluent-Widgets pywin32 Pillow
    pause
    exit /b 1
)
set "SCRIPT=%~dp0winclipboard_qt.py"
set "MODE=Qt + Fluent"

echo Using interpreter: !PYW!
echo Entry point: [!MODE!]

if /i "!PYW:pythonw.exe=!"=="!PYW!" (
    REM interpreter is a console python - run in foreground so errors are visible
    "!PYW!" "!SCRIPT!"
    exit /b !errorlevel!
)

start "" "!PYW!" "!SCRIPT!"
echo WinClipboard started. Tray icon running; press Ctrl+Shift+V to open the panel.
ping 127.0.0.1 -n 3 >nul
exit /b 0
