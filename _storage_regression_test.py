# -*- coding: utf-8 -*-
"""Storage transition, blob lifetime and full-history UI regressions.

Run: .venv/Scripts/python.exe -X utf8 _storage_regression_test.py
All paths are temporary; safe mode avoids clipboard listeners and hotkeys.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import clipboard_manager as cm
from store import ClipboardStore
from PIL import Image


class StorageRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wclb_storage_regression_")
        root = self.tmp.name
        paths = {"APP_DIR": root}
        for key, name in (("IMAGE_DIR", "images"), ("BLOB_DIR", "data/blobs"),
                          ("HISTORY_FILE", "history.json"), ("DB_FILE", "clipboard.db"),
                          ("PRIVATE_FILE", "private.json"), ("CONFIG_FILE", "config.json"),
                          ("LOG_FILE", "app.log"), ("ICON_FILE", "icon.ico"),
                          ("BOOT_STATE_FILE", "boot_state.json")):
            paths[key] = os.path.join(root, name)
        self.paths = patch.multiple(cm, **paths)
        self.paths.start()
        cm.ensure_dirs()
        cm.save_config(dict(cm.DEFAULT_CFG, max_items=3000, sensitive_guard=False))
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            if core.store is not None:
                core.store.close()
            core._capture_exec.shutdown(wait=True)
        self.paths.stop()
        self.tmp.cleanup()

    def core(self):
        core = cm.ClipboardCore(safe_mode=True)
        # Do not leave delayed GC timers attached to temporary test databases.
        core._schedule_gc = lambda: None
        self.cores.append(core)
        return core

    def text(self, core, value):
        core.add_item(("text", value))

    def contents(self, core):
        return [r["content"] for r in core.history]

    def stale_db(self):
        store = ClipboardStore(cm.DB_FILE)
        store.insert({"type": "text", "content": "stale-sqlite", "ts": 1})
        store.close()

    def test_encrypted_restart_preserves_additions_deletions_and_empty_history(self):
        core = self.core()
        self.text(core, "before-switch")
        core.set_history_encryption(True)
        self.text(core, "after-switch")
        core.delete_at(1)
        core.flush_history()
        self.stale_db()  # Simulate an interrupted old-version conversion.
        restarted = self.core()
        self.assertEqual(self.contents(restarted), ["after-switch"])
        self.assertFalse(Path(cm.DB_FILE).exists())
        restarted.clear_unpinned()
        restarted.flush_history()
        self.stale_db()
        self.assertEqual(self.contents(self.core()), [])

    def test_encryption_removes_sqlite_and_encrypts_all_legacy_json_copies(self):
        core = self.core()
        self.text(core, "private-test-marker")
        for suffix in ("", ".bak1", ".bak2", ".bak3", ".migrated", ".tmp"):
            Path(cm.HISTORY_FILE + suffix).write_text(
                json.dumps(core.history), encoding="utf-8")
        core.set_history_encryption(True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.assertFalse(Path(cm.DB_FILE + suffix).exists())
        for suffix in ("", ".bak1", ".bak2", ".bak3", ".migrated"):
            data = json.loads(Path(cm.HISTORY_FILE + suffix).read_text(encoding="utf-8"))
            self.assertTrue(data["dpapi"])
            self.assertEqual(cm._history_from_container(data)[0]["content"],
                             "private-test-marker")

    def test_failed_encryption_keeps_original_database_and_mode(self):
        core = self.core()
        self.text(core, "keep-me")
        with patch.object(cm, "_dpapi_protect", return_value=None):
            with self.assertRaises(OSError):
                core.set_history_encryption(True)
        self.assertFalse(core.cfg["encrypt_history"])
        self.assertIsNotNone(core.store)
        self.assertTrue(Path(cm.DB_FILE).exists())
        self.assertFalse(Path(cm.HISTORY_FILE).exists())

    def test_failed_history_write_preserves_database(self):
        core = self.core()
        self.text(core, "keep-me")
        with patch.object(cm, "save_history", return_value=False):
            with self.assertRaises(OSError):
                core.set_history_encryption(True)
        self.assertFalse(core.cfg["encrypt_history"])
        self.assertEqual(core.store.count(), 1)

    def test_failed_mode_config_preserves_database(self):
        core = self.core()
        self.text(core, "keep-me")
        with patch.object(cm, "save_config", return_value=False):
            with self.assertRaises(OSError):
                core.set_history_encryption(True)
        self.assertFalse(core.cfg["encrypt_history"])
        self.assertEqual(core.store.count(), 1)
        self.assertTrue(Path(cm.DB_FILE).exists())

    def test_failed_database_cleanup_keeps_json_authoritative_on_restart(self):
        core = self.core()
        self.text(core, "before-switch")
        with patch.object(cm, "_remove_history_database", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                core.set_history_encryption(True)
        self.assertTrue(core.cfg["encrypt_history"])
        self.assertIsNone(core.store)
        self.text(core, "after-cleanup-failure")
        core.flush_history()
        restarted = self.core()
        self.assertEqual(self.contents(restarted), ["after-cleanup-failure", "before-switch"])
        self.assertFalse(Path(cm.DB_FILE).exists())

    def test_encrypted_startup_recovers_backup_instead_of_stale_database(self):
        core = self.core()
        self.text(core, "current-backup")
        core.set_history_encryption(True)
        core._dirty = True
        core.flush_history()
        Path(cm.HISTORY_FILE).write_text("broken", encoding="utf-8")
        self.stale_db()
        self.assertEqual(self.contents(self.core()), ["current-backup"])

    def test_unreadable_encrypted_history_never_overwrites_with_stale_database(self):
        cm.save_config(dict(cm.DEFAULT_CFG, encrypt_history=True))
        path = Path(cm.HISTORY_FILE)
        path.write_text("broken", encoding="utf-8")
        self.stale_db()
        with self.assertRaises(OSError):
            self.core()
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")
        self.assertTrue(Path(cm.DB_FILE).exists())

    def test_legacy_database_migrates_when_no_json_exists(self):
        self.stale_db()
        cm.save_config(dict(cm.DEFAULT_CFG, encrypt_history=True))
        self.assertEqual(self.contents(self.core()), ["stale-sqlite"])
        self.assertFalse(Path(cm.DB_FILE).exists())

    def test_encrypted_startup_never_rotates_plaintext_api_key_into_config_backup(self):
        marker = "startup-secret-test-marker"
        Path(cm.CONFIG_FILE).write_text(json.dumps({
            "encrypt_history": True,
            "ai_services": [{"name": "test", "api_key": marker}]}), encoding="utf-8")
        self.core()
        for path in Path(cm.APP_DIR).glob("config.json*"):
            self.assertNotIn(marker, path.read_text(encoding="utf-8"))

    def test_encrypted_startup_preserves_unreadable_api_key_config(self):
        raw = json.dumps({"encrypt_history": True, "ai_services": [
            {"name": "test", "api_key": {"dpapi": True, "data": "invalid"}}]})
        Path(cm.CONFIG_FILE).write_text(raw, encoding="utf-8")
        self.core()
        self.assertEqual(Path(cm.CONFIG_FILE).read_text(encoding="utf-8"), raw)

    def test_switch_back_and_restart_preserve_current_records_and_identity(self):
        core = self.core()
        self.text(core, "old")
        core.set_history_encryption(True)
        self.text(core, "new")
        original = list(core.history)
        core.set_history_encryption(False)
        self.assertTrue(all(a is b for a, b in zip(original, core.history)))
        self.assertFalse(Path(cm.HISTORY_FILE).exists())
        self.assertEqual(self.contents(self.core()), ["new", "old"])

    def test_failed_switch_back_keeps_encrypted_source_and_removes_partial_database(self):
        core = self.core()
        self.text(core, "encrypted-record")
        core.set_history_encryption(True)
        with patch.object(cm, "save_config", return_value=False):
            with self.assertRaises(OSError):
                core.set_history_encryption(False)
        self.assertTrue(core.cfg["encrypt_history"])
        self.assertIsNone(core.store)
        self.assertFalse(Path(cm.DB_FILE).exists())
        self.assertEqual(self.contents(self.core()), ["encrypted-record"])

    def test_failed_encrypted_flush_remains_dirty_and_can_retry(self):
        core = self.core()
        core.set_history_encryption(True)
        self.text(core, "retry-write")
        with patch.object(cm, "_dpapi_protect", return_value=None):
            core.flush_history()
        self.assertTrue(core._dirty)
        self.assertEqual(cm.load_history(), [])
        core.flush_history()
        self.assertFalse(core._dirty)
        self.assertEqual(cm.load_history()[0]["content"], "retry-write")

    def test_eviction_preserves_recaptured_image_for_undo_in_both_modes(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                core = self.core()
                if encrypted:
                    core.set_history_encryption(True)
                core.max_items = 1
                path = Path(cm.APP_DIR) / ("undo-%s.png" % encrypted)
                image = Image.new("RGB", (4, 4), "blue")
                image.save(path)
                capture = ("image", {"path": str(path), "w": 4, "h": 4,
                                     "hash": cm.pixel_sha256(image)})
                core.add_item(capture)
                core.soft_delete_at(0)
                core.add_item(capture)
                self.text(core, "evict-image")
                self.assertTrue(path.exists())
                self.assertEqual(len(core.undo_delete()), 1)
                self.assertTrue(path.exists())
                # Once the restored record is evicted without a transaction,
                # the last reference must release the file.
                self.text(core, "release-image")
                self.assertFalse(path.exists())
                if core.store is not None:
                    core.store.close()
                    core.store = None

    def test_full_query_and_post_filters_reach_old_records_beyond_400(self):
        core = self.core()
        self.text(core, "https://example.com/old-link")
        self.text(core, '{"old_json":true}')
        for i in range(1205):
            self.text(core, "ordinary-row-%04d" % i)
        self.assertEqual(len(core.query_view("all")), 1207)
        self.assertEqual(len(core.query_view("all", limit=400)), 400)
        for category, query in (("link", ""), ("all", "type:json"),
                                ("all", "type:link"), ("all", "ordinary-row")):
            self.assertEqual(core.query_view(category, query), core.filter_view(category, query))
        self.assertEqual(len(core.query_view("link", limit=1)), 1)
        self.assertEqual(len(core.query_view("all", "type:json", limit=1)), 1)

    def test_real_offscreen_page_loads_beyond_400_and_reports_conversion_failure(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        import winclipboard_qt as wq
        qapp = QApplication.instance() or QApplication([])
        qapp.setQuitOnLastWindowClosed(False)
        core = self.core()
        for i in range(405):
            self.text(core, "ui-row-%04d" % i)
        wq.init_palette(wq.isDarkTheme())
        with patch.object(wq, "UiHealthMonitor"):
            app = wq.WinClipboardApp(qapp, safe_mode=True)
        self.cores.append(app.core)
        try:
            app.refresh_view()
            self.assertEqual(len(app.panel._full_seq), 405)
            while app.panel._win_limit < 405:
                app.panel._extend_window()
                qapp.processEvents()
            # 扩展走后台分批渲染(性能优化:GUI 不整段阻塞),排空后应全量物化
            deadline = time.time() + 30
            while app.panel._render_pending and time.time() < deadline:
                qapp.processEvents()
            self.assertEqual(len(app.panel._cards), 405)
            self.assertEqual(app.panel._cards[-1].rec["content"], "ui-row-0000")
            switch = app.win.settings_page.c_enc
            with patch.object(cm, "save_history", return_value=False), \
                    patch.object(wq.InfoBar, "error") as error:
                switch.setChecked(True)
                self.assertFalse(switch.isChecked())
                self.assertFalse(app.cfg["encrypt_history"])
                error.assert_called_once()
        finally:
            app.core.on_event = None
            app.core.on_dirty = None
            for timer in app.findChildren(QTimer):
                timer.stop()
            app.win.hide()
            app.quick.hide()
            app.win.deleteLater()
            app.quick.deleteLater()
            app.deleteLater()
            qapp.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
