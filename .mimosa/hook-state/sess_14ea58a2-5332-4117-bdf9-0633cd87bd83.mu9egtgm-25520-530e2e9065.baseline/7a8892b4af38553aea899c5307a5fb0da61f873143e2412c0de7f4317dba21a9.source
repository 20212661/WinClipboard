# -*- coding: utf-8 -*-
"""后台图片解码:画布恢复/钉图时不冻 UI。

大图解码单张可达几十毫秒,50 张全量同步恢复会冻窗秒级;这里照 ThumbLoader
的模式在工作线程 QImage 解码 + 降采样,完成后经信号投回 GUI 线程。
"""
import queue
import threading

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QImage

from perf_probe import PerfSpan

# preview 最长边(DIU):画布默认显示用降采样图,1:1 时再换全分辨率
MAX_PREVIEW_EDGE = 1600


class PinImageLoader(QObject):
    """解码队列线程;QImage 加载/缩放线程安全,pixmap 化留在 GUI 线程做。"""

    loaded = Signal(str, QImage)        # (asset 名, preview 图;失败为 null)

    def __init__(self):
        super().__init__()
        self._q = queue.Queue()
        self._pending = set()
        self.loaded.connect(self._on_loaded)
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, name, path):
        if name and name not in self._pending:
            self._pending.add(name)
            self._q.put((name, path))

    def _run(self):
        while True:
            name, path = self._q.get()
            with PerfSpan("pin_preview_decode"):   # 解码 + 降采样(工作线程)
                img = QImage()
                try:
                    src = QImage(path)          # 线程安全;失败得到 null 图
                    if not src.isNull():
                        big = max(src.width(), src.height())
                        if big > MAX_PREVIEW_EDGE:
                            k = MAX_PREVIEW_EDGE / float(big)
                            img = src.scaled(max(1, int(src.width() * k)),
                                             max(1, int(src.height() * k)),
                                             Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation)
                        else:
                            img = src
                except Exception:
                    img = QImage()
            self.loaded.emit(name, img)

    def _on_loaded(self, name, img):
        self._pending.discard(name)
