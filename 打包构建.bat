@echo off
chcp 65001 >nul
REM WinClipboard one-click build -> dist\WinClipboard\WinClipboard.exe
REM Uses the project's own .venv (creates missing deps via requirements.txt).
REM Keep this file pure ASCII.
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] .venv not found. Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt pyinstaller
    pause
    exit /b 1
)

"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PY%" -m pip install pyinstaller || (pause & exit /b 1)
)

REM Build icon (same generator the app uses at runtime, so tray/exe match)
if not exist build mkdir build
"%PY%" -c "import clipboard_manager as cm; cm.ensure_dirs(); cm.make_icon(r'build\icon.ico')" || echo [WARN] icon generation failed, exe falls back to default icon

echo Building WinClipboard (onedir)...
"%PY%" -m PyInstaller winclipboard.spec --noconfirm --clean
if errorlevel 1 (
    echo [ERROR] build failed.
    pause
    exit /b 1
)

echo.
echo Done: dist\WinClipboard\WinClipboard.exe
echo Ship the whole dist\WinClipboard folder (onedir build).
pause
