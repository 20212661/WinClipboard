# -*- mode: python ; coding: utf-8 -*-
# WinClipboard PyInstaller 打包配置(onedir:启动快,便于增量更新)
# 构建: 打包构建.bat 或  python -m PyInstaller winclipboard.spec --noconfirm
# 产物: dist/WinClipboard/WinClipboard.exe(整个目录一起分发)
import os

block_cipher = None

datas = []

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
        "ai_quick",
        "pinboard",
        # 阶段 8 拆分出的模块(静态导入为主,此处兜底)
        "ui_base",
        "viewer",
        "image_editor",
        "workflow_ui",
        "win32_host",
        "privacy_store",
        "history_store",
        # 上下文包(全部延迟导入,静态分析看不见)
        "ctxpack",
        "ctxpack.model",
        "ctxpack.budget",
        "ctxpack.assemble",
        "ctxpack.store",
        "ctxpack.service",
        "ctxpack.ui_tray",
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
        # Pillow 大件:AVIF 编码器(剪贴板图片不产生 AVIF)与 Tk 桥(无 tkinter)
        "PIL._avif",
        "PIL._imagingtk",
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

# PyInstaller 6.22 起 collect_data_files 返回 (源文件, 目标目录) 二元组,
# 而 COLLECT 的 TOC 只收 (目标名, 源路径, 类型) 三元组,必须手工转换。
_qfw_datas = []
for _src, _dir in collect_data_files(
        # .mimosa/hook-state 是安全钩子写进包目录的运行状态,不是程序资源
        "qfluentwidgets", excludes=[".mimosa/**/*", "**/.mimosa/**/*"]):
    _dest = _dir.replace("\\", "/") + "/" + os.path.basename(_src)
    _qfw_datas.append((_dest, _src, "DATA"))
a.datas += _qfw_datas

# Windows Qt wheels use the OS ICU API (unversioned ucnv_* exports).
# A third-party icuuc.dll discovered through PATH, e.g. Poppler's versioned
# ICU build, shadows the OS DLL and makes QtCore fail with WinError 127.
# Keep an ICU DLL shipped with Qt itself; never bundle a PATH-discovered copy.
import PySide6
qt_package_dir = os.path.normcase(os.path.dirname(PySide6.__file__))
a.binaries = [entry for entry in a.binaries
              if os.path.basename(entry[0]).lower() != "icuuc.dll"
              or os.path.normcase(os.path.dirname(entry[1])) == qt_package_dir]

# 减体积:excludes 只挡得住 Python 绑定模块,挡不住底层 Qt DLL/插件,
# 必须按文件名过滤。全是 QWidget+QSS 应用用不到的大件:软件 OpenGL 兜底
# (QSS 走光栅渲染)、QML/Quick 全家、PDF 库与其图片插件、触屏虚拟键盘、
# 备用平台后端(qwindows.dll 才是实际使用的)、Pillow 的 AVIF 编码器。
_DROP_FILES = {
    "opengl32sw.dll",
    "qt6quick.dll", "qt6qml.dll", "qt6qmlmeta.dll",
    "qt6qmlmodels.dll", "qt6qmlworkerscript.dll",
    "qt6pdf.dll", "qpdf.dll",
    "qt6virtualkeyboard.dll", "qtvirtualkeyboardplugin.dll",
    "qdirect2d.dll", "qminimal.dll",
    "qtuiotouchplugin.dll",
    # 注意:qoffscreen.dll 必须保留——打包冒烟测试用 QT_QPA_PLATFORM=offscreen
    # 验证 EXE,砍了它验证管线自己就起不来。
}

a.binaries = [entry for entry in a.binaries
              if os.path.basename(entry[0]).lower() not in _DROP_FILES
              and not os.path.basename(entry[0]).lower().startswith(("_avif.", "_imagingtk."))]

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
