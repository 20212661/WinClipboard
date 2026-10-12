# -*- coding: utf-8 -*-
"""分页计数缓存回归(阶段 6.1):无过滤总数走缓存,写操作正确失效。

Run: .venv/Scripts/python.exe -X utf8 _pagecount_regression_test.py
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clipboard_manager as cm
from store import ClipboardStore


class PageCountRegression(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_pgcount_")
        self.paths = patch.multiple(cm, APP_DIR=self.tmp.name)
        self.paths.start()
        self.st = ClipboardStore(os.path.join(self.tmp.name, "c.db"))

    def tearDown(self):
        self.st.close()
        self.paths.stop()
        self.tmp.cleanup()

    def seed(self, n, base=1_000_000.0):
        self.st.migrate_from_list([
            {"type": "text", "pinned": i % 7 == 0, "ts": base + i,
             "content": "条目 %03d 内容" % i} for i in range(n)])

    def total(self, **kw):
        return self.st.query_page(**kw)[1]

    def test_cache_and_invalidation(self):
        self.seed(12)
        self.assertEqual(self.total(), 12)             # 首次计数并缓存
        self.assertEqual(self.total(), 12)             # 缓存复用
        self.st.insert({"type": "text", "pinned": False, "ts": 2.0,
                        "content": "新插入"})
        self.assertEqual(self.total(), 13)             # 插入失效
        first = self.st.query_page(limit=1)[0][0]
        self.st.mark_deleted([first["_db"]])
        self.assertEqual(self.total(), 12)             # 软删失效
        self.st.restore([first["_db"]])
        self.assertEqual(self.total(), 13)             # 恢复失效
        self.st.delete_many([first["_db"]])
        self.assertEqual(self.total(), 12)             # 物理删失效
        self.st.reset()
        self.assertEqual(self.total(), 0)              # 整库重置失效
        self.seed(5, base=2_000_000.0)
        self.assertEqual(self.total(), 5)              # 迁移导入失效

    def test_filtered_totals_still_exact(self):
        self.seed(14)
        self.assertEqual(self.total(), 14)
        # 与暴力 SQL 逐一对照(过滤路径不走缓存)
        self.assertEqual(self.total(q="条目 00"), 10)
        self.assertEqual(self.total(content_type="text"), 14)
        self.assertEqual(self.total(pinned=True), 2)
        # 缓存建立后过滤总数不受缓存影响
        self.assertEqual(self.total(pinned=False), 12)
        self.st.insert({"type": "text", "pinned": True, "ts": 3.0,
                        "content": "置顶新条目"})
        self.assertEqual(self.total(pinned=True), 3)
        self.assertEqual(self.total(), 15)

    def test_count_method_agrees(self):
        self.seed(9)
        self.assertEqual(self.st.count(), 9)
        self.st.mark_deleted([self.st.query_page(limit=1)[0][0]["_db"]])
        self.assertEqual(self.st.count(), 8)
        self.assertEqual(self.total(), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
