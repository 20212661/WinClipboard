# -*- coding: utf-8 -*-
"""容量语义回归(阶段 5.1):max_items 限制未置顶条数,置顶不占预算。

Run: .venv/Scripts/python.exe -X utf8 _capacity_regression_test.py
SQLite 与加密 JSON 两种事实源都覆盖(临时目录 + 安全模式核心):
  - 恰达上限不淘汰 / 超限淘汰最旧普通记录 / 0/1/N 条置顶不淘汰
  - 去重不虚增计数 / 取消置顶后超限立即淘汰
  - 共享 blob:置顶引用在时淘汰普通记录文件保留;最后引用释放才删文件
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clipboard_manager as cm
from PIL import Image


class CapacityRegression(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_capacity_")
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            if core.store is not None:
                core.store.close()
            core._capture_exec.shutdown(wait=True)
        self.tmp.cleanup()

    @staticmethod
    def _paths_for(root):
        paths = {"APP_DIR": str(root)}
        for key, name in (("IMAGE_DIR", "images"), ("BLOB_DIR", "data/blobs"),
                          ("HISTORY_FILE", "history.json"),
                          ("DB_FILE", "clipboard.db"),
                          ("PRIVATE_FILE", "private.json"),
                          ("CONFIG_FILE", "config.json"),
                          ("LOG_FILE", "app.log"), ("ICON_FILE", "icon.ico"),
                          ("BOOT_STATE_FILE", "boot_state.json")):
            paths[key] = os.path.join(str(root), name)
        return paths

    def run_for_both_modes(self, fn):
        """每种事实源各自独立子目录(两腿不能共享同一个 DB 文件——
        SQLite 腿的核心还握着连接时,加密腿无法 unlink)。"""
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                root = Path(self.tmp.name) / ("json" if encrypted else "sqlite")
                root.mkdir()
                with patch.multiple(cm, **self._paths_for(root)):
                    cm.ensure_dirs()
                    cm.save_config(dict(cm.DEFAULT_CFG,
                                        sensitive_guard=False))
                    core = cm.ClipboardCore(safe_mode=True)
                    core._schedule_gc = lambda: None
                    if encrypted:
                        core.set_history_encryption(True)
                    core.max_items = int(fn.max_items)
                    self.cores.append(core)
                    fn(core)

    @staticmethod
    def add(core, *texts):
        for t in texts:
            core.add_item(("text", t))

    @staticmethod
    def contents(core):
        return [r["content"] for r in core.history]

    # ---- 场景函数(以属性携带 max_items) ----
    def test_exact_limit_no_evict(self):
        def scenario(core):
            self.add(core, "a", "b", "c")
            self.assertEqual(self.contents(core), ["c", "b", "a"])
        scenario.max_items = 3
        self.run_for_both_modes(scenario)

    def test_over_limit_evicts_oldest_unpinned(self):
        def scenario(core):
            self.add(core, "a", "b", "c", "d")    # 每次 add 后即时淘汰
            self.assertEqual(self.contents(core), ["d", "c"])
        scenario.max_items = 2
        self.run_for_both_modes(scenario)

    def test_pinned_excluded_from_budget(self):
        def scenario(core):
            self.add(core, "a", "b")               # history = [b, a](新者在前)
            core.toggle_pin_at(0)                  # 固定 b
            self.add(core, "c")                    # 未置顶 a,c = 2 恰好
            self.add(core, "d")                    # 未置顶 3 > 2 → 淘汰 a
            self.assertEqual(self.contents(core), ["d", "c", "b"])
            self.assertTrue(core.history[2].get("pinned"))   # b 挤占总长但不在预算
        scenario.max_items = 2
        self.run_for_both_modes(scenario)

    def test_zero_and_all_pinned_never_evicted(self):
        def scenario(core):
            self.add(core, "a", "b")
            core.toggle_pin_at(0)                  # 固定 b
            core.toggle_pin_at(1)                  # 固定 a(全部置顶)
            self.add(core, "c", "d")               # 未置顶 c,d = 2 恰好
            self.add(core, "e")                    # 未置顶 3 > 2 → 淘汰 c
            self.assertEqual(self.contents(core), ["e", "d", "b", "a"])  # 置顶两条保留
            self.assertEqual(sum(1 for h in core.history
                                 if not h.get("pinned")), 2)
        scenario.max_items = 2
        self.run_for_both_modes(scenario)

    def test_dedup_does_not_inflate(self):
        def scenario(core):
            self.add(core, "same")
            self.add(core, "same")                # 去重提升,不新增条目
            self.add(core, "x")
            self.assertEqual(self.contents(core), ["x", "same"])
        scenario.max_items = 2
        self.run_for_both_modes(scenario)

    def test_unpin_over_limit_evicts_immediately(self):
        def scenario(core):
            self.add(core, "a", "b")               # [b, a]
            core.toggle_pin_at(1)                  # 固定 a → 未置顶 b = 1 ≤ 2
            self.add(core, "c")                    # [c, b, a*] 未置顶 2 恰好
            self.assertEqual(len(core.history), 3)
            core.toggle_pin_at(2)                  # 取消 a 的固定 → 未置顶 3 > 2
            # a 最早添加 = 最旧未置顶 → 被淘汰;b 保留
            self.assertEqual(self.contents(core), ["c", "b"])
        scenario.max_items = 2
        self.run_for_both_modes(scenario)

    def test_pinned_image_asset_survives_eviction_pressure(self):
        """置顶图片在容量压力下永不淘汰,资产文件保留。"""
        def scenario(core):
            img_path = Path(self.tmp.name) / "cap-img.png"
            Image.new("RGB", (4, 4), "red").save(img_path)
            core.add_item(("image", {"path": str(img_path), "w": 4, "h": 4,
                                     "hash": cm.pixel_sha256(
                                         Image.new("RGB", (4, 4), "red"))}))
            core.toggle_pin_at(0)                 # 置顶:不占未置顶预算
            self.add(core, "t1", "t2")            # t1 被淘汰(未置顶 2 > 1)
            self.assertEqual([h["type"] for h in core.history],
                             ["text", "image"])
            self.assertTrue(Path(core.history[1]["content"]).exists()
                            or Path(img_path).exists())
        scenario.max_items = 1
        self.run_for_both_modes(scenario)

    def test_last_reference_released_by_eviction(self):
        """普通图片记录被容量淘汰且撤销栈无引用 → 资产文件释放
        (引用计数路径未因容量语义改动回退)。"""
        def scenario(core):
            img_path = Path(cm.IMAGE_DIR) / "cap-evict.png"
            Image.new("RGB", (4, 4), "blue").save(img_path)
            core.add_item(("image", {"path": str(img_path), "w": 4, "h": 4,
                                     "hash": cm.pixel_sha256(
                                         Image.new("RGB", (4, 4), "blue"))}))
            self.assertTrue(Path(img_path).exists())
            self.add(core, "t1")                  # 图片被淘汰(未置顶 2 > 1)
            self.assertEqual([h["type"] for h in core.history], ["text"])
            self.assertFalse(Path(img_path).exists())
        scenario.max_items = 1
        self.run_for_both_modes(scenario)


if __name__ == "__main__":
    unittest.main(verbosity=2)
