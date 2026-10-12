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
powershell -NoProfile -ExecutionPolicy Bypass -File installer.ps1
pause
