# -*- coding: utf-8 -*-
"""Inspector 视图离屏渲染(开发自检):URL/JWT/Base64 三视图截图到临时目录。"""
import base64
import io
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 离屏平台字体库为空(不指目录所有字符渲染成占位框)
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_manager as cm

TMP = os.path.join(tempfile.gettempdir(), "wclb_insp_preview")
os.makedirs(TMP, exist_ok=True)
cm.APP_DIR = TMP
cm.IMAGE_DIR = os.path.join(TMP, "images")
cm.HISTORY_FILE = os.path.join(TMP, "history.json")
cm.PRIVATE_FILE = os.path.join(TMP, "private.json")
cm.LOG_FILE = os.path.join(TMP, "app.log")
os.makedirs(cm.IMAGE_DIR, exist_ok=True)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
import winclipboard_qt as wq
wq.apply_fonts(app)           # 中文回退字族


class FakeApp(QObject):
    from PySide6.QtGui import QIcon
    icon = QIcon()
    core = cm.ClipboardCore.__new__(cm.ClipboardCore)
    cfg = {}
    _viewers = []


fake = FakeApp()


def b64u(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def save(v, name):
    v.show()
    app.processEvents()
    v.grab().save(os.path.join(TMP, name))
    v.close()
    print("saved", name)


uv = wq.ViewerWindow(fake, {"type": "text", "content":
                            "https://example.com/api/users?page=2&size=20&token=abc#frag"})
save(uv, "insp_url.png")

jwt = (b64u('{"alg":"HS256","typ":"JWT"}') + "." +
       b64u('{"sub":"1234567890","name":"张三","iat":%d,"exp":%d}'
            % (time.time() - 3600, time.time() + 7200)) + "." +
       "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
save(wq.ViewerWindow(fake, {"type": "text", "content": jwt}), "insp_jwt.png")

txt = base64.b64encode(
    "部署清单:\n1. 数据库迁移\n2. 灰度 10%\n3. 观察一小时\n这段文本长度要超过嗅探下限。".encode()).decode()
save(wq.ViewerWindow(fake, {"type": "text", "content": txt}), "insp_b64.png")

from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (320, 200), (68, 128, 220)).save(buf, "PNG")
save(wq.ViewerWindow(fake, {"type": "text",
                            "content": base64.b64encode(buf.getvalue()).decode()}),
     "insp_b64img.png")
