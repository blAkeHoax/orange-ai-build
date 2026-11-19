"""
test_stage4_5b_phase2_db_safety.py - Comprehensive Phase 2 Database Safety & Checkpoint Test Suite.

Verifies all Stage 4.5B Phase 2 safety invariants:
1. Turn-locking & pre-update rejection (_is_updating rejects new turns with code -32001).
2. Clean scheduler and Stage 2C background worker termination.
3. Successful WAL TRUNCATE checkpoint with busy == 0 and size truncated from >0 to 0 bytes.
4. 100% data row preservation and integrity check on disposable database.
5. Lock contention failure path (fails closed and aborts).
6. Exclusive access verification post-checkpoint.
7. Worker shutdown timeout and still-alive worker detection (mandatory abort).
8. Scheduler shutdown failure detection (mandatory abort).
9. Turn-registration concurrency race protection (atomic admission under _turn_threads_lock).
10. Database connection remaining open detection (deterministic verification, mandatory abort).
11. Unexpected WAL and locked SHM sidecar detection (mandatory abort).
12. Database path consistency & custom_db_path production lockdown (PermissionError in production).
13. Zero baseline drift across all 7 protected operational resources.
14. Test isolation guard fails closed upon unauthorized operational AppData DB access attempt.
15. Python runtime shutdown failure or corruption detection (mandatory abort).
"""

import os
import sys
import unittest
import unittest.mock
import tempfile
import sqlite3
import time
import json
import hashlib
import threading
from pathlib import Path

# Add project roots to sys.path
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

# Import runtime adapter and desktop engine components
from runtime import runtime_adapter
import brain.memory as mem
import brain.mood as mood
import brain.planner as planner
import brain.reminders as reminders
from brain import scheduler


class OperationalDatabaseAccessViolation(RuntimeError):
    """Raised when any test or framework operation attempts to access the operational AppData database."""
    pass


# Authoritative Operational AppData database paths that MUST NEVER be accessed by tests
_OPERATIONAL_APPDATA_DB = os.path.normcase(
    os.path.abspath(
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Orange AI", "memory.db")
    )
)
_OPERATIONAL_APPDATA_DIR = os.path.normcase(
    os.path.abspath(
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Orange AI")
    )
)


class TestStage45BPhase2DatabaseSafety(unittest.TestCase):
    """Phase 2 Database Safety, Checkpoint Hardening, and Quiescence Verification Test Suite."""

    @classmethod
    def setUpClass(cls):
        # 1. Install SQLite connection guard
        cls._orig_sqlite3_connect = sqlite3.connect

        def _guarded_sqlite3_connect(database, *args, **kwargs):
            if isinstance(database, (str, bytes, os.PathLike)):
                norm = os.path.normcase(os.path.abspath(str(database)))
                if (
                    norm == _OPERATIONAL_APPDATA_DB
                    or norm.startswith(_OPERATIONAL_APPDATA_DB)
                    or (norm.startswith(_OPERATIONAL_APPDATA_DIR) and norm.endswith((".db", ".db-wal", ".db-shm")))
                ):
                    raise OperationalDatabaseAccessViolation(
                        f"TEST ISOLATION GUARD: Illegal access attempt to operational AppData database '{database}'! "
                        f"Tests MUST operate strictly on isolated disposable databases."
                    )
            return cls._orig_sqlite3_connect(database, *args, **kwargs)

        sqlite3.connect = _guarded_sqlite3_connect

        # 2. Install resolve_database_path guard
        cls._orig_resolve_database_path = runtime_adapter.resolve_database_path

        def _guarded_resolve_database_path(db_path=None):
            if db_path is None or not str(db_path).strip():
                raise OperationalDatabaseAccessViolation(
                    "TEST ISOLATION GUARD: resolve_database_path() was invoked without an explicit disposable db_path! "
                    "Tests must provide an explicit disposable database."
                )
            return cls._orig_resolve_database_path(db_path)

        runtime_adapter.resolve_database_path = _guarded_resolve_database_path

    @classmethod
    def tearDownClass(cls):
        # Restore original functions
        sqlite3.connect = cls._orig_sqlite3_connect
        runtime_adapter.resolve_database_path = cls._orig_resolve_database_path

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.disposable_db = os.path.join(self.temp_dir.name, "disposable_test.db")
        self._init_disposable_db(self.disposable_db)

        # Baseline hashes for all 7 protected resources
        self.protected_baselines = self._compute_protected_resource_hashes()

        # Isolate environment data directory to temporary test directory
        self._orig_user_data_dir = os.environ.get("ORANGE_AI_USER_DATA_DIR")
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.temp_dir.name

        # Layer 1 test isolation: automatically mock main.ensure_initialized
        self._ensure_init_patcher = unittest.mock.patch("main.ensure_initialized")
        self._ensure_init_patcher.start()

    def tearDown(self):
        try:
            self._ensure_init_patcher.stop()
        except Exception:
            pass

        if self._orig_user_data_dir is not None:
            os.environ["ORANGE_AI_USER_DATA_DIR"] = self._orig_user_data_dir
        else:
            os.environ.pop("ORANGE_AI_USER_DATA_DIR", None)

        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

        # Reset runtime adapter flags and clean locks
        runtime_adapter._is_updating = False
        runtime_adapter._is_shutting_down = False
        with runtime_adapter._turn_threads_lock:
            runtime_adapter._active_turn_threads.clear()
        runtime_adapter._stage2c_stop_event.clear()
        runtime_adapter._stage2c_worker_thread = None
        runtime_adapter._stage2c_reconciliation_thread = None
        # Clean cached module connections
        runtime_adapter.close_all_database_connections()

    def _compute_protected_resource_hashes(self):
        paths = [
            r"D:\Orange AI\Orange AI Desktop\memory.db",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Orange AI", "memory.db"),
            r"D:\Orange AI\installer\memory.db",
            r"D:\Orange AI\Orange AI Desktop\recommendations.json",
            r"D:\Orange AI\Orange AI Desktop\event_watermarks.json",
            r"D:\Orange AI\installer\Orange AI.exe",
            r"D:\Orange AI\installer\Orange AI.exe.bak_20261106_205045",
        ]
        hashes = {}
        for p in paths:
            if os.path.exists(p):
                with open(p, "rb") as f:
                    hashes[p] = hashlib.sha256(f.read()).hexdigest().lower()
        return hashes

    def _assert_zero_drift_on_protected_resources(self):
        current_hashes = self._compute_protected_resource_hashes()
        for p, orig_h in self.protected_baselines.items():
            curr_h = current_hashes.get(p)
            self.assertEqual(
                orig_h,
                curr_h,
                f"PROTECTED RESOURCE DRIFT DETECTED on {p}! Expected {orig_h}, got {curr_h}",
            )

    def _init_disposable_db(self, db_path: str):
        conn = self._orig_sqlite3_connect(db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                message TEXT NOT NULL,
                due_at TEXT NOT NULL,
                fired INTEGER NOT NULL DEFAULT 0
            );
        """)
        for i in range(10):
            conn.execute(
                "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                ("user" if i % 2 == 0 else "assistant", f"Message content {i}", "2026-11-17T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO memories (category, key, value, updated_at) VALUES (?, ?, ?, ?)",
                ("preference", f"key_{i}", f"val_{i}", "2026-11-17T00:00:00Z"),
            )
        conn.commit()
        conn.close()

    def test_01_pre_update_turn_rejection(self):
        """Verify that _is_updating flag causes chat.send to reject new user turns with code -32001."""
        runtime_adapter._is_updating = True

        responses = []
        def mock_send_error(req_id, code, message, data=None):
            responses.append({"type": "error", "req_id": req_id, "code": code, "message": message})

        def mock_send_response(req_id, result):
            responses.append({"type": "response", "req_id": req_id, "result": result})

        orig_send_error = runtime_adapter.send_error
        orig_send_response = runtime_adapter.send_response
        try:
            runtime_adapter.send_error = mock_send_error
            runtime_adapter.send_response = mock_send_response

            runtime_adapter.handle_chat_send("req-test-1", {"text": "Hello during update"})

            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0]["type"], "error")
            self.assertEqual(responses[0]["code"], -32001)
            self.assertIn("preparing for update", responses[0]["message"])
        finally:
            runtime_adapter.send_error = orig_send_error
            runtime_adapter.send_response = orig_send_response
            runtime_adapter._is_updating = False

    def test_02_scheduler_and_worker_clean_shutdown(self):
        """Verify that scheduler and Stage 2C background workers can be stopped cleanly."""
        runtime_adapter._stage2c_stop_event.clear()
        runtime_adapter._stop_stage2c_background_workers()
        self.assertTrue(runtime_adapter._stage2c_stop_event.is_set())

        scheduler.stop(wait_timeout=2.0)
        self.assertTrue(scheduler._stop_event.is_set())

    def test_03_wal_checkpoint_truncate_success(self):
        """Verify that handle_system_shutdown_for_update checkpoints and truncates WAL with busy == 0."""
        conn_holder = sqlite3.connect(self.disposable_db, timeout=5.0)
        conn_holder.execute("PRAGMA wal_autocheckpoint=0;")
        for i in range(50):
            conn_holder.execute(
                "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                ("user", f"Uncheckpointed frame write {i}", "2026-11-17T00:00:00Z"),
            )
        conn_holder.commit()

        wal_path = f"{self.disposable_db}-wal"
        self.assertTrue(os.path.exists(wal_path), "WAL file should exist before checkpoint")
        wal_size_before = os.path.getsize(wal_path)
        self.assertGreater(wal_size_before, 0, f"WAL size should be > 0 bytes before checkpoint, was {wal_size_before}")

        try:
            result = runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

            self.assertEqual(result["status"], "ready_for_update")
            self.assertEqual(result["checkpoint"]["busy"], 0)
            self.assertEqual(result["wal_bytes"], 0)
            self.assertTrue(result["connections_closed"])
            self.assertTrue(result["workers_stopped"])
            self.assertEqual(result["integrity_check"], "ok")

            self.assertTrue(os.path.exists(wal_path), "WAL sidecar should remain present while connection holder is open")
            self.assertEqual(os.path.getsize(wal_path), 0, "WAL sidecar must be truncated to exactly 0 bytes")
        finally:
            conn_holder.close()

    def test_04_data_preservation_and_record_integrity(self):
        """Verify that 100% of rows exist and match after checkpoint."""
        conn_pre = sqlite3.connect(self.disposable_db)
        pre_chat_count = conn_pre.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]
        pre_mem_count = conn_pre.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn_pre.close()

        runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

        conn_post = sqlite3.connect(self.disposable_db)
        post_chat_count = conn_post.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]
        post_mem_count = conn_post.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        integrity = conn_post.execute("PRAGMA integrity_check").fetchall()
        conn_post.close()

        self.assertEqual(pre_chat_count, post_chat_count)
        self.assertEqual(pre_mem_count, post_mem_count)
        self.assertEqual(integrity, [("ok",)])

    def test_05_lock_contention_failure_path_aborts_update(self):
        """Verify that an active lock held by another connection causes pre-update shutdown to abort safely."""
        lock_conn = sqlite3.connect(self.disposable_db, timeout=0.1)
        lock_conn.execute("BEGIN EXCLUSIVE;")

        try:
            with self.assertRaises(RuntimeError) as ctx:
                runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
            err_msg = str(ctx.exception)
            self.assertTrue(
                "busy" in err_msg.lower() or "lock" in err_msg.lower(),
                f"Expected lock or busy error, got: {err_msg}",
            )
        finally:
            lock_conn.rollback()
            lock_conn.close()

    def test_06_exclusive_access_verification_post_checkpoint(self):
        """Verify that after clean shutdown, exclusive lock test succeeds immediately."""
        runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})

        test_conn = sqlite3.connect(self.disposable_db, timeout=1.0)
        try:
            test_conn.execute("BEGIN IMMEDIATE;")
            test_conn.execute("ROLLBACK;")
            success = True
        except Exception:
            success = False
        finally:
            test_conn.close()

        self.assertTrue(success, "Should be able to acquire immediate transaction without lock conflict")

    def test_07_worker_shutdown_timeout_and_still_alive_aborts(self):
        """Verify that if a worker thread deliberately ignores stop and remains alive, update preparation aborts."""
        stop_blocker = threading.Event()
        def uncooperative_worker():
            stop_blocker.wait(5.0)

        t_bad = threading.Thread(target=uncooperative_worker, name="UncooperativeWorker", daemon=True)
        t_bad.start()

        with runtime_adapter._turn_threads_lock:
            runtime_adapter._active_turn_threads.add(t_bad)

        try:
            with self.assertRaises(RuntimeError) as ctx:
                runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
            self.assertIn("still alive", str(ctx.exception).lower())
            self.assertIn("UncooperativeWorker", str(ctx.exception))
        finally:
            stop_blocker.set()
            t_bad.join(timeout=1.0)
            with runtime_adapter._turn_threads_lock:
                runtime_adapter._active_turn_threads.discard(t_bad)

    def test_08_scheduler_shutdown_failure_aborts(self):
        """Verify that if scheduler worker thread remains alive after stop, update preparation aborts."""
        stop_blocker = threading.Event()
        def uncooperative_scheduler_worker():
            stop_blocker.wait(5.0)

        t_sched = threading.Thread(target=uncooperative_scheduler_worker, name="UncooperativeSchedulerWorker", daemon=True)
        t_sched.start()

        orig_worker = getattr(scheduler, "_active_job_thread", None)
        scheduler._active_job_thread = t_sched

        try:
            with self.assertRaises(RuntimeError) as ctx:
                runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
            self.assertIn("scheduler", str(ctx.exception).lower())
            self.assertIn("still alive", str(ctx.exception).lower())
        finally:
            stop_blocker.set()
            t_sched.join(timeout=1.0)
            scheduler._active_job_thread = orig_worker

    def test_09_turn_registration_concurrency_race(self):
        """
        Concurrency stress test verifying that turn registration and shutdown mode transition are atomic.
        Asserts that no turn can be admitted after shutdown begins, and all admitted turns are joined before checkpoint.
        """
        turn_outcomes = {}
        outcomes_lock = threading.Lock()
        race_threads = []
        num_callers = 15

        def simulated_user_turn(text, prompt_fn=None):
            time.sleep(0.01)
            return {"reply": f"Reply to: {text}", "model": "mock"}

        def mock_err(req_id, code, msg, data=None):
            with outcomes_lock:
                turn_outcomes[req_id] = ("error", code, msg)

        def mock_resp(req_id, res):
            with outcomes_lock:
                turn_outcomes[req_id] = ("response", res)

        orig_err = runtime_adapter.send_error
        orig_resp = runtime_adapter.send_response
        runtime_adapter.send_error = mock_err
        runtime_adapter.send_response = mock_resp

        try:
            def caller(i):
                runtime_adapter.handle_chat_send(f"race-{i}", {"text": f"race turn {i}"})

            with unittest.mock.patch("runtime.runtime_adapter._ensure_api_client_configured"), \
                 unittest.mock.patch("main.process_user_turn", side_effect=simulated_user_turn):
                for i in range(num_callers):
                    th = threading.Thread(target=caller, args=(i,), daemon=True)
                    race_threads.append(th)

                for th in race_threads:
                    th.start()

                # Simultaneously transition into update mode and checkpoint
                result = runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
                self.assertEqual(result["status"], "ready_for_update")

                for th in race_threads:
                    th.join(timeout=3.0)

            # Every turn must be accounted for: either completed ("response") or rejected ("error" -32001)
            self.assertEqual(len(turn_outcomes), num_callers, "All concurrent requests must be deterministically accounted for")
            for req_id, outcome in turn_outcomes.items():
                if outcome[0] == "error":
                    self.assertEqual(outcome[1], -32001)
                    self.assertIn("preparing for update", outcome[2])
                else:
                    self.assertEqual(outcome[0], "response")

            with runtime_adapter._turn_threads_lock:
                self.assertEqual(len(runtime_adapter._active_turn_threads), 0)
        finally:
            runtime_adapter.send_error = orig_err
            runtime_adapter.send_response = orig_resp

    def test_10_database_connection_remaining_open_aborts(self):
        """Verify that if a database connection remains unclosed in any module, close_all_database_connections raises."""
        orig_close = mem.close
        mem._conn = "mock_open_connection_handle"
        mem.close = lambda: None  # Simulate failure to close connection
        try:
            with self.assertRaises(RuntimeError) as ctx:
                runtime_adapter.close_all_database_connections()
            self.assertIn("connections remained open", str(ctx.exception))
            self.assertIn("brain.memory", str(ctx.exception))
        finally:
            mem._conn = None
            mem.close = orig_close

    def test_11_unexpected_wal_or_shm_sidecar_state_aborts(self):
        """Verify that an unexpected non-zero WAL sidecar triggers a mandatory update abort."""
        wal_path = f"{self.disposable_db}-wal"
        orig_getsize = os.path.getsize
        def mock_getsize(path):
            if str(path) == wal_path:
                return 4096
            return orig_getsize(path)

        with unittest.mock.patch("os.path.getsize", side_effect=mock_getsize):
            with unittest.mock.patch("os.path.exists", return_value=True):
                with self.assertRaises(RuntimeError) as ctx:
                    runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
                self.assertIn("WAL sidecar not truncated", str(ctx.exception))

    def test_11b_locked_shm_sidecar_aborts(self):
        """Verify that a locked or inaccessible SHM sidecar triggers a mandatory update abort."""
        shm_path = f"{self.disposable_db}-shm"

        real_exists = os.path.exists
        def mock_exists(p):
            if str(p) == shm_path:
                return True
            return real_exists(p)

        orig_open = open
        def mock_open(path, mode="r", *args, **kwargs):
            if str(path) == shm_path and "r+b" in mode:
                raise PermissionError("The process cannot access the file because it is being used by another process")
            return orig_open(path, mode, *args, **kwargs)

        with unittest.mock.patch("os.path.exists", side_effect=mock_exists):
            with unittest.mock.patch("builtins.open", side_effect=mock_open):
                with self.assertRaises(RuntimeError) as ctx:
                    runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
                self.assertIn("SHM sidecar is locked", str(ctx.exception))

    def test_12_database_path_production_lockdown(self):
        """Verify that custom_db_path is rejected with PermissionError when ORANGE_AI_TEST_MODE is disabled."""
        orig_mode = os.environ.get("ORANGE_AI_TEST_MODE")
        try:
            os.environ.pop("ORANGE_AI_TEST_MODE", None)
            with self.assertRaises(PermissionError) as ctx:
                runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
            self.assertIn("forbidden in production", str(ctx.exception))
        finally:
            if orig_mode is not None:
                os.environ["ORANGE_AI_TEST_MODE"] = orig_mode

    def test_13_protected_resources_zero_drift(self):
        """Confirm that neither tests nor adapter modifications modified any of the 7 protected baselines."""
        self._assert_zero_drift_on_protected_resources()

    def test_14_test_guard_rejects_operational_appdata_access(self):
        """
        Verify that the test isolation guard fails closed and immediately raises
        OperationalDatabaseAccessViolation if any operation attempts to touch the operational AppData DB.
        """
        # 1. Direct sqlite3.connect to operational DB
        with self.assertRaises(OperationalDatabaseAccessViolation) as ctx1:
            sqlite3.connect(_OPERATIONAL_APPDATA_DB)
        self.assertIn("TEST ISOLATION GUARD", str(ctx1.exception))

        # 2. Direct sqlite3.connect to operational DB sidecars
        with self.assertRaises(OperationalDatabaseAccessViolation) as ctx2:
            sqlite3.connect(f"{_OPERATIONAL_APPDATA_DB}-wal")
        self.assertIn("TEST ISOLATION GUARD", str(ctx2.exception))

        # 3. Accidental resolve_database_path without explicit disposable db_path
        with self.assertRaises(OperationalDatabaseAccessViolation) as ctx3:
            runtime_adapter.resolve_database_path()
        self.assertIn("TEST ISOLATION GUARD", str(ctx3.exception))

    def test_15_python_runtime_shutdown_failure_aborts(self):
        """
        Verify that if the Python runtime encounters an internal error during shutdown
        or fails to complete checkpoint, update preparation fails closed.
        """
        # Case A: Checkpoint throws an unhandled sqlite error
        with unittest.mock.patch("sqlite3.connect") as mock_conn_fn:
            mock_c = unittest.mock.MagicMock()
            mock_cur = unittest.mock.MagicMock()
            mock_cur.execute.side_effect = sqlite3.OperationalError("simulated disk I/O error during checkpoint")
            mock_c.cursor.return_value = mock_cur
            mock_conn_fn.return_value = mock_c
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
            self.assertIn("disk I/O error", str(ctx.exception))

        # Case B: Integrity check reports corruption
        with unittest.mock.patch("sqlite3.connect") as mock_conn_fn:
            mock_c = unittest.mock.MagicMock()
            mock_cur = unittest.mock.MagicMock()
            # Step 7 checkpoint succeeds:
            mock_cur.execute.return_value.fetchone.return_value = (0, 0, 0)
            # Step 12 integrity check fails:
            mock_cur.execute.return_value.fetchall.return_value = [("row 15 missing from index",)]
            mock_c.cursor.return_value = mock_cur
            mock_conn_fn.return_value = mock_c

            with self.assertRaises(RuntimeError) as ctx2:
                runtime_adapter.handle_system_shutdown_for_update({"db_path": self.disposable_db})
            self.assertIn("integrity check failed", str(ctx2.exception).lower())


if __name__ == "__main__":
    unittest.main()
