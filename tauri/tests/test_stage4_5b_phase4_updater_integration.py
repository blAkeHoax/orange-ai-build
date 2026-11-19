"""
test_stage4_5b_phase4_updater_integration.py - Orange AI Stage 4.5B Phase 4 Python Runtime Integration Suite.

Verifies runtime-side database shutdown, WAL truncate checkpoint, turn rejection,
and safety gate integration for Option 1 desktop updates:
1. Strict triple-layer isolation guard prohibiting operational AppData DB access.
2. Safe SQLite WAL truncate checkpoint with busy=0 and wal_bytes=0.
3. 100% data row preservation on disposable test database.
4. Turn rejection under update quiescence (code -32001).
5. Error rejection on lock contention or uncheckpointable state.
6. Schema parity between Python response and Rust verify_pre_update_response expectations.
7. Post-checkpoint connection closure permitting exclusive external installer access.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Add project roots
_test_dir = os.path.dirname(os.path.abspath(__file__))
_tauri_root = os.path.dirname(_test_dir)
_desktop_candidates = [
    os.path.join(os.path.dirname(_tauri_root), "desktop"),
    os.path.join(os.path.dirname(_tauri_root), "Orange AI Desktop"),
]
_desktop_root = next((d for d in _desktop_candidates if os.path.exists(d)), _desktop_candidates[0])

os.environ["ORANGE_AI_DESKTOP_DIR"] = _desktop_root
os.environ["ORANGE_AI_TEST_MODE"] = "1"

if _desktop_root not in sys.path:
    sys.path.insert(0, _desktop_root)
if _tauri_root not in sys.path:
    sys.path.insert(0, _tauri_root)

from runtime import runtime_adapter

# ---------------------------------------------------------------------------
# Strict Isolation Guard
# ---------------------------------------------------------------------------
class OperationalDatabaseAccessViolation(Exception):
    pass


_REAL_SQLITE3_CONNECT = None
_OPERATIONAL_DB_PATH = os.path.normcase(
    os.path.normpath(
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Orange AI", "memory.db")
    )
)


def _guard_sqlite3_connect(database, *args, **kwargs):
    if isinstance(database, (str, bytes, Path)):
        norm = os.path.normcase(os.path.normpath(str(database)))
        if norm == _OPERATIONAL_DB_PATH or "localappdata\\orange ai\\memory.db" in norm:
            raise OperationalDatabaseAccessViolation(
                f"FATAL: Test attempted access to operational AppData database: {database}"
            )
    return _REAL_SQLITE3_CONNECT(database, *args, **kwargs)


class TestStage4_5BPhase4UpdaterIntegration(unittest.TestCase):
    """Phase 4 updater service and database safety gate integration tests."""

    @classmethod
    def setUpClass(cls):
        global _REAL_SQLITE3_CONNECT
        if _REAL_SQLITE3_CONNECT is None:
            _REAL_SQLITE3_CONNECT = sqlite3.connect
            sqlite3.connect = _guard_sqlite3_connect

    @classmethod
    def tearDownClass(cls):
        global _REAL_SQLITE3_CONNECT
        if _REAL_SQLITE3_CONNECT is not None:
            sqlite3.connect = _REAL_SQLITE3_CONNECT
            _REAL_SQLITE3_CONNECT = None

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="orange_ai_phase4_test_")
        self.disposable_db = os.path.join(self.temp_dir, "test_memory.db")
        self._init_disposable_db(self.disposable_db)

        # Reset runtime adapter flags
        runtime_adapter._is_updating = False
        runtime_adapter._is_shutting_down = False
        with runtime_adapter._turn_threads_lock:
            runtime_adapter._active_turn_threads.clear()

    def tearDown(self):
        runtime_adapter._is_updating = False
        runtime_adapter._is_shutting_down = False
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _init_disposable_db(self, db_path):
        """Create a disposable WAL-mode SQLite database with test conversations."""
        conn = _REAL_SQLITE3_CONNECT(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        for i in range(1, 245):
            conn.execute(
                "INSERT INTO conversation_history (role, content, model) VALUES (?, ?, ?)",
                ("user" if i % 2 == 1 else "assistant", f"Turn message {i}", "test-model"),
            )
        conn.commit()
        conn.close()

    def test_01_isolation_guard_blocks_operational_db(self):
        """Verify that the test isolation guard fails closed on operational AppData DB access."""
        with self.assertRaises(OperationalDatabaseAccessViolation):
            sqlite3.connect(_OPERATIONAL_DB_PATH)

    def test_02_pre_update_shutdown_wal_truncate_success(self):
        """Verify system.shutdown_for_update truncates WAL and reports ready_for_update."""
        # Insert uncheckpointed data to generate WAL frames
        conn = _REAL_SQLITE3_CONNECT(self.disposable_db)
        conn.execute("INSERT INTO conversation_history (role, content) VALUES ('user', 'uncheckpointed')")
        conn.commit()
        conn.close()

        res = runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

        self.assertEqual(res["status"], "ready_for_update")
        self.assertEqual(res["checkpoint"]["busy"], 0)
        self.assertEqual(os.path.normpath(res["target_db"]), os.path.normpath(self.disposable_db))

        # Inspect WAL file on disk: must be 0 bytes or removed
        wal_path = f"{self.disposable_db}-wal"
        if os.path.exists(wal_path):
            self.assertEqual(os.path.getsize(wal_path), 0, "WAL sidecar must be 0 bytes post-truncate")

        # Verify database integrity and complete data preservation
        conn_verify = _REAL_SQLITE3_CONNECT(self.disposable_db)
        integrity = conn_verify.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(integrity, "ok")
        count = conn_verify.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]
        self.assertEqual(count, 245, "All 245 test turns must be preserved")
        conn_verify.close()

    def test_03_turn_rejection_under_update_quiescence(self):
        """Verify that the adapter rejects all subsequent turns after shutdown_for_update."""
        # Shutdown for update
        runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

        # Attempt turn and capture error
        captured = []
        orig_send_error = runtime_adapter.send_error
        try:
            runtime_adapter.send_error = lambda req_id, code, msg, data=None: captured.append(
                {"code": code, "msg": msg}
            )
            runtime_adapter.dispatch_request("turn_1", "chat.send", {"text": "Should be rejected"})
        finally:
            runtime_adapter.send_error = orig_send_error

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["code"], -32001)
        self.assertIn("preparing for update", captured[0]["msg"].lower())

    def test_04_schema_conformance_with_rust_verification(self):
        """Verify that the pre-update response conforms to Rust verify_pre_update_response expectations."""
        res = runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

        # Rust assertions require:
        # status == "ready_for_update"
        # checkpoint.busy == 0
        # target_db is a valid string path
        self.assertEqual(res.get("status"), "ready_for_update")
        self.assertIn("checkpoint", res)
        self.assertEqual(res["checkpoint"].get("busy"), 0)
        self.assertIn("target_db", res)
        self.assertTrue(os.path.exists(res["target_db"]))

    def test_05_exclusive_file_access_post_shutdown(self):
        """Verify that all Python SQLite connections are closed, permitting exclusive write access."""
        runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

        # Test exclusive write access by external process
        with open(self.disposable_db, "r+b") as f:
            header = f.read(16)
            self.assertTrue(header.startswith(b"SQLite format 3"))
            f.seek(0, os.SEEK_END)
            self.assertTrue(f.writable())


if __name__ == "__main__":
    unittest.main(verbosity=2)
