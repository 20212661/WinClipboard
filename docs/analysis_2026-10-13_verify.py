"""Read-only project audit reproductions, with all data in a temporary sandbox."""
import os
import sys
import json
import tempfile
import queue
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"


def main():
    with tempfile.TemporaryDirectory(prefix="wclb_audit_") as sandbox:
        os.environ["APPDATA"] = sandbox
        import clipboard_manager as cm
        from store import ClipboardStore
        from ctxpack.service import snapshot_args_from_record, ContextPackService
        from ctxpack.model import ContextItem
        from ctxpack.store import load_packs_status
        from workflow_steps import WorkflowContext

        cm.ensure_dirs()
        rows = []
        def record(name, result):
            rows.append({"case": name, "result": result})

        try:
            snapshot_args_from_record({"type": "image", "content": "C:/test.png",
                                       "w": 100, "h": 200, "hash": "abc"})
            record("image_snapshot", "no exception")
        except Exception as exc:
            record("image_snapshot", type(exc).__name__ + ": " + str(exc))

        item = ContextItem.from_dict({"iid": "a", "kind": "text", "content": "x", "priority": 0})
        record("priority_zero_roundtrip", item.priority)
        malformed = Path(sandbox) / "malformed.json"
        malformed.write_text(json.dumps({"packs": [{"items": [{"priority": "bad"}]}]}))
        try:
            record("malformed_pack_status", load_packs_status(str(malformed)).status)
        except Exception as exc:
            record("malformed_pack_status", type(exc).__name__ + ": " + str(exc))

        service = ContextPackService(str(Path(sandbox) / "plain_packs.json"))
        service.create("first")
        service.create("second")
        record("plaintext_pack_backup_exists", Path(service.path + ".bak1").exists())

        core = cm.ClipboardCore.__new__(cm.ClipboardCore)
        core.cfg = dict(cm.DEFAULT_CFG, sensitive_guard=False)
        core.max_items = 100
        core.history, core.private = [], []
        core.signal_q = queue.Queue()
        core.worker = SimpleNamespace(cmd_q=queue.Queue())
        core.monitor_paused, core._exclude_set = False, set()
        core.store = ClipboardStore(str(Path(sandbox) / "audit.db"))
        try:
            core.import_payload({"items": [{"type": "text", "content": "audit imported",
                                            "ts": 1000, "pinned": True}]})
            before = dict(core.history[0])
            persisted = core.store.load_all()[0]
            record("import_memory_vs_database", {
                "memory": {k: before[k] for k in ("ts", "pinned")},
                "database": {k: persisted[k] for k in ("ts", "pinned")},
                "sql_pinned_matches": core.query_view("all", "is:pinned"),
                "memory_pinned_matches": core.filter_view("all", "is:pinned")})
        finally:
            core.store.close()

        search_store = ClipboardStore(str(Path(sandbox) / "search.db"))
        try:
            for text in ("ordinary text", "100% complete", "under_score"):
                search_store.insert({"type": "text", "content": text, "ts": 1, "pinned": False})
            core.store = search_store
            core.history = search_store.load_all()
            core._reset_hist_caches()
            record("search_percent_sql_vs_memory", {
                "sql": core.query_view("all", "%"),
                "memory": core.filter_view("all", "%")})
        finally:
            search_store.close()

        cm.save_config(dict(cm.DEFAULT_CFG, cleanup_days=1))
        old_store = ClipboardStore(cm.DB_FILE)
        old_store.insert({"type": "text", "content": "old entry", "ts": 1000,
                          "pinned": False})
        old_store.close()
        safe_core = cm.ClipboardCore(safe_mode=True)
        record("safe_mode_startup_old_history_count", len(safe_core.history))
        if safe_core.store is not None:
            safe_core.store.close()
        safe_core._capture_exec.shutdown(wait=True)

        from PySide6.QtWidgets import QApplication
        from pinboard.manager import PinBoardManager
        from pinboard.storage import PinBoardStorage
        qt = QApplication.instance() or QApplication([])
        storage = PinBoardStorage(cm.APP_DIR)
        storage.save_state({"version": 2, "items": [{"asset": "test.png"}], "open": False})
        app = SimpleNamespace(cfg=dict(cm.DEFAULT_CFG), safe_mode=True)
        manager = PinBoardManager(app)
        manager.save_now()
        record("safe_mode_board_after_save", storage.load_state().get("items"))
        manager.window.hide()

        ctx = WorkflowContext(input_text="original", context="reference literally {input}")
        record("template_literal_placeholder", ctx.render("CTX:{context}"))
        print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
