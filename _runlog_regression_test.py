# -*- coding: utf-8 -*-
"""运行记录落盘回归(阶段 5.2):原子替换、损坏不隐式覆写、失败可反馈、脱敏。

Run: .venv/Scripts/python.exe -X utf8 _runlog_regression_test.py
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clipboard_manager as cm
import workflow_runlog as wrl


class RunlogRegression(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_runlog_")
        self.paths = patch.multiple(cm, APP_DIR=self.tmp.name,
                                    LOG_FILE=os.path.join(self.tmp.name,
                                                          "app.log"))
        self.paths.start()
        self.fp = os.path.join(self.tmp.name, "workflow_runs.json")

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    @staticmethod
    def run_rec():
        rec = wrl.RunRecorder("测试工作流", "输入正文SECRET-INPUT", 2)
        rec.step_start(0, "ai", "GLM", "输入正文SECRET-INPUT")
        rec.step_ok(0, "输出正文SECRET-OUTPUT")
        rec.step_start(1, "human", "人工", "输出正文SECRET-OUTPUT")
        rec.step_ok(1, "编辑后")
        return rec.finish()

    def test_roundtrip_ids_and_keep(self):
        r1 = self.run_rec()
        self.assertEqual(wrl.save_run(r1, keep=3), 1)
        self.assertEqual(wrl.save_run(self.run_rec(), keep=3), 2)
        for _ in range(4):
            wrl.save_run(self.run_rec(), keep=3)
        runs = wrl.load_runs()
        self.assertEqual(len(runs), 3)                 # keep 截断
        self.assertEqual([r["id"] for r in runs], [6, 5, 4])

    def test_no_body_text_saved(self):
        wrl.save_run(self.run_rec())
        raw = Path(self.fp).read_text(encoding="utf-8")
        self.assertNotIn("SECRET-INPUT", raw)
        self.assertNotIn("SECRET-OUTPUT", raw)
        runs = wrl.load_runs()
        self.assertEqual(runs[0]["input_chars"], len("输入正文SECRET-INPUT"))
        self.assertEqual(runs[0]["steps"][0]["output_chars"],
                         len("输出正文SECRET-OUTPUT"))

    def test_corrupt_file_quarantined_not_overwritten_silently(self):
        Path(self.fp).write_text("{corrupt", encoding="utf-8")
        new_id = wrl.save_run(self.run_rec())
        self.assertEqual(new_id, 1)                    # 空白重建,新序号
        corrupt = self.fp + ".corrupt"
        self.assertTrue(Path(corrupt).exists())
        self.assertEqual(Path(corrupt).read_text(encoding="utf-8"),
                         "{corrupt")                   # 原件留档可查
        self.assertEqual(len(wrl.load_runs()), 1)      # 新文件有效

    def test_save_failure_preserves_original(self):
        wrl.save_run(self.run_rec())
        before = Path(self.fp).read_bytes()
        with patch.object(os, "replace", side_effect=OSError("注入失败")):
            self.assertIsNone(wrl.save_run(self.run_rec()))
        self.assertEqual(Path(self.fp).read_bytes(), before)
        self.assertFalse(os.path.exists(self.fp + ".tmp"))

    def test_clear_failure_preserves_original(self):
        wrl.save_run(self.run_rec())
        before = Path(self.fp).read_bytes()
        with patch.object(os, "replace", side_effect=OSError("注入失败")):
            self.assertFalse(wrl.clear_runs())
        self.assertEqual(Path(self.fp).read_bytes(), before)
        self.assertEqual(len(wrl.load_runs()), 1)      # 未被隐式清空

    def test_clear_success_atomic(self):
        wrl.save_run(self.run_rec())
        self.assertTrue(wrl.clear_runs())
        self.assertEqual(wrl.load_runs(), [])
        self.assertFalse(os.path.exists(self.fp + ".tmp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
