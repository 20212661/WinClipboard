@echo off
chcp 65001 >nul
REM WinClipboard per-user installer wrapper.
REM Copies dist\WinClipboard to %LOCALAPPDATA%\Programs, creates shortcuts,
REM registers an uninstall entry. No admin required. Pure ASCII content.
setlocal
cd /d "%~dp0"
if not exist "dist\WinClipboard\WinClipboard.exe" (
    echo [ERROR] dist\WinClipboard\WinClipboard.exe not found.
    echo Run 打包构建.bat first to build the app.
    pause
    exit /b 1
)
set "VER=1.0.0"
if exist ".venv\Scripts\python.exe" (
    for /f "usebackq delims=" %%v in (`.venv\Scripts\python.exe -c "import clipboard_manager as cm; print(cm.APP_VERSION)" 2^>nul`) do set "VER=%%v"
)
powershell -NoProfile -ExecutionPolicy Bypass -File installer.ps1 -Version "%VER%"
pause
