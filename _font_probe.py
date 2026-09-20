# -*- coding: utf-8 -*-
"""离屏/真实平台中文字体对照探针(开发诊断用,可删)。

同一行 StrongBodyLabel("图钉画布") 分别在两种 QT_QPA_PLATFORM 下渲染并抓图,
对比输出到命令行给定的两个 PNG 路径之一:

    QT_QPA_PLATFORM=offscreen python _font_probe.py out.png
    python _font_probe.py out.png
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PySide6.QtCore import qVersion  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout  # noqa: E402
from qfluentwidgets import StrongBodyLabel, setTheme, Theme  # noqa: E402

app = QApplication([])
setTheme(Theme.LIGHT)
w = QWidget()
w.setStyleSheet("QWidget{background:#ffffff;}")
v = QVBoxLayout(w)
lbl1 = StrongBodyLabel("图钉画布")
lbl2 = StrongBodyLabel("图钉画布 2")
v.addWidget(lbl1)
v.addWidget(lbl2)
w.resize(220, 90)
w.show()
app.processEvents()
w.grab().save(sys.argv[1])

from PySide6.QtGui import QFontInfo  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
f = lbl1.font()
print("qt=%s platform=%s label-font=%s resolved=%s app-font=%s" % (
    qVersion(), QGuiApplication.platformName(), f.family(),
    QFontInfo(f).family(), QGuiApplication.font().family()))
