# -*- mode: python ; coding: utf-8 -*-
# WinClipboard PyInstaller 打包配置(onedir:启动快,便于增量更新)
# 构建: 打包构建.bat 或  python -m PyInstaller winclipboard.spec --noconfirm
# 产物: dist/WinClipboard/WinClipboard.exe(整个目录一起分发)
import os

block_cipher = None

datas = [
    # AI 动作定义(声明式 JSON;ai/registry 运行时扫描,静态分析发现不了)
    ("ai/actions", "ai/actions"),
]

a = Analysis(
    ["winclipboard_qt.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # pywin32 的延迟导入,PyInstaller 静态分析看不见
        "win32timezone",
        "win32crypt",
        "win32clipboard",
        "win32gui",
        "win32con",
        "win32api",
        "win32event",
        # 本项目模块(被字符串/延迟路径引用时兜底)
        "perf_probe",
        "pinboard",
        "ai",
        "ai.registry",
        "ai.executor",
        "command",
        "command.executor",
        "palette",
        # 可迁移加密备份(AES-256-GCM),延迟导入兜底
        "cryptography",
        "cryptography.hazmat.primitives.ciphers.aead",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 用不到的大件,显著减体积
        "tkinter",
        "unittest",
        "pydoc_data",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtNetworkAuth",
        "PySide6.QtCharts",
        "PySide6.QtPdf",
        "PySide6.QtSql",
        "PySide6.QtTest",
        # 注意:不能排除 QtXml/QtSvg/QtNetwork —— qfluentwidgets、
        # frameless 运行时按需导入,排了就 "No module named"(实测 QtXml 踩坑)
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# qfluentwidgets 的 QSS/图片/SVG 资源必须随包(纯数据,静态分析发现不了);
# PySide6 自身的二进制/插件由 PyInstaller 的官方 hook 收集,不需手动 collect
from PyInstaller.utils.hooks import collect_data_files  # noqa: E402

a.datas += collect_data_files("qfluentwidgets")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WinClipboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX 常触发杀软误报,不开
    console=False,                # 托盘常驻,无控制台
    disable_windowed_traceback=False,
    icon=os.path.join("build", "icon.ico") if os.path.exists(
        os.path.join("build", "icon.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="WinClipboard",
)
