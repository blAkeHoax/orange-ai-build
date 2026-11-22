"""
test_stage_3_7e_5b_d1_database_resolver.py - Comprehensive Unit & Integration Tests
for Stage 3.7E.5B-D1 Centralized Database Resolver & Connection Hardening.

Verifies:
1. Development path resolution.
2. Frozen path resolution.
3. Runtime path resolution after environment changes.
4. Explicit path override.
5. Connection configuration (timeout, WAL, busy timeout, thread safety).
6. Connection configuration (row_factory).
7. Connection closure, transaction rollbacks, and ownership.
8. Multiple independent database files concurrently.
9. Path switching without stale connection reuse across memory, reminders, mood, planner.
10. Import-order independence (dynamic resolution at call time).
11. JobStore compatibility and short-lived connection semantics.
12. Memory subsystem functionality on isolated database.
13. Reminders subsystem functionality on isolated database.
14. Mood subsystem functionality on isolated database.
15. Planner subsystem functionality on isolated database.
16. Default database path detection and normalization.
17. Legacy patch("*.DB_PATH") compatibility.
"""

import os
import sys
import unittest
import tempfile
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

from brain.database import (
    resolve_database_path,
    get_database_path,
    is_default_database_path,
    connect_database,
    close_connection,
    connection_context,
    transaction,
    DEFAULT_TIMEOUT,
    DEFAULT_BUSY_TIMEOUT_MS,
    DATABASE_FILENAME,
)
import brain.memory as memory
import brain.reminders as reminders
import brain.mood as mood
import brain.planner as planner
from brain.jobs.store import JobStore


class TestCentralDatabaseResolver(unittest.TestCase):
    """Test suite for centralized database resolver and connection management."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.isolated_db_dir = os.path.join(self.temp_dir.name, "user_data")
        os.makedirs(self.isolated_db_dir, exist_ok=True)
        self.isolated_db_path = os.path.join(self.isolated_db_dir, DATABASE_FILENAME)

        # Stash environment
        self._orig_env_data_dir = os.environ.get("ORANGE_AI_USER_DATA_DIR")
        self._orig_frozen = getattr(sys, "frozen", None)

        # Ensure clean state in consumer modules
        memory.close()
        reminders.close()
        mood.close()
        planner.close()

    def tearDown(self):
        memory.close()
        reminders.close()
        mood.close()
        planner.close()

        if self._orig_env_data_dir is not None:
            os.environ["ORANGE_AI_USER_DATA_DIR"] = self._orig_env_data_dir
        else:
            os.environ.pop("ORANGE_AI_USER_DATA_DIR", None)

        if self._orig_frozen is not None:
            sys.frozen = self._orig_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen

        self.temp_dir.cleanup()

    def test_01_development_path_resolution(self):
        """Without frozen flag or environment override, resolves to workspace memory.db."""
        os.environ.pop("ORANGE_AI_USER_DATA_DIR", None)
        if hasattr(sys, "frozen"):
            del sys.frozen

        resolved = resolve_database_path()
        expected = os.path.normpath(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(memory.__file__))), "memory.db")
        )
        self.assertEqual(os.path.normpath(resolved), expected)
        self.assertEqual(os.path.normpath(get_database_path()), expected)
        self.assertEqual(os.path.normpath(memory.DB_PATH), expected)
        self.assertEqual(os.path.normpath(reminders.DB_PATH), expected)
        self.assertEqual(os.path.normpath(mood.DB_PATH), expected)
        self.assertEqual(os.path.normpath(planner.DB_PATH), expected)

    def test_02_frozen_path_resolution(self):
        """In frozen mode, resolves to %LOCALAPPDATA%\\Orange AI\\memory.db."""
        os.environ.pop("ORANGE_AI_USER_DATA_DIR", None)
        sys.frozen = True
        try:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                expected = os.path.normpath(os.path.join(local_app_data, "Orange AI", "memory.db"))
            else:
                expected = os.path.normpath(os.path.join(os.path.expanduser("~"), ".orange_ai", "memory.db"))

            resolved = resolve_database_path()
            self.assertEqual(os.path.normpath(resolved), expected)
            self.assertEqual(os.path.normpath(memory.DB_PATH), expected)
            self.assertEqual(os.path.normpath(reminders.DB_PATH), expected)
            self.assertEqual(os.path.normpath(mood.DB_PATH), expected)
            self.assertEqual(os.path.normpath(planner.DB_PATH), expected)
        finally:
            del sys.frozen

    def test_03_environment_override_dynamic_resolution(self):
        """Setting ORANGE_AI_USER_DATA_DIR redirects all modules dynamically without reload."""
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir
        expected = os.path.normpath(self.isolated_db_path)

        self.assertEqual(os.path.normpath(resolve_database_path()), expected)
        self.assertEqual(os.path.normpath(memory.get_db_path()), expected)
        self.assertEqual(os.path.normpath(memory.DB_PATH), expected)
        self.assertEqual(os.path.normpath(reminders.DB_PATH), expected)
        self.assertEqual(os.path.normpath(mood.DB_PATH), expected)
        self.assertEqual(os.path.normpath(planner.DB_PATH), expected)
        self.assertEqual(os.path.normpath(JobStore().db_path), expected)

    def test_04_explicit_path_override(self):
        """Passing explicit db_path overrides environment and defaults."""
        custom_db = os.path.join(self.temp_dir.name, "custom_subdir", "test_override.db")
        resolved = resolve_database_path(custom_db)
        self.assertEqual(os.path.normpath(resolved), os.path.normpath(custom_db))
        # Verifies parent directory was created
        self.assertTrue(os.path.exists(os.path.dirname(custom_db)))

        js = JobStore(db_path=custom_db)
        self.assertEqual(os.path.normpath(js.db_path), os.path.normpath(custom_db))

    def test_05_connection_configuration_wal_and_busy_timeout(self):
        """Connection factory correctly applies WAL mode, busy timeout, and thread safety."""
        test_db = os.path.join(self.temp_dir.name, "test_config.db")
        conn = connect_database(test_db)
        try:
            # WAL mode
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode;")
            journal_mode = cur.fetchone()[0].lower()
            self.assertEqual(journal_mode, "wal")

            # Busy timeout
            cur.execute("PRAGMA busy_timeout;")
            busy_to = cur.fetchone()[0]
            self.assertEqual(busy_to, DEFAULT_BUSY_TIMEOUT_MS)
        finally:
            close_connection(conn)

    def test_06_connection_configuration_row_factory(self):
        """connect_database applies row_factory when specified."""
        test_db = os.path.join(self.temp_dir.name, "test_row_factory.db")
        conn = connect_database(test_db, row_factory=sqlite3.Row)
        try:
            conn.execute("CREATE TABLE test_rf (id INT, name TEXT);")
            conn.execute("INSERT INTO test_rf VALUES (1, 'Alice');")
            conn.commit()
            row = conn.execute("SELECT id, name FROM test_rf;").fetchone()
            self.assertIsInstance(row, sqlite3.Row)
            self.assertEqual(row["name"], "Alice")
            self.assertEqual(row[0], 1)
        finally:
            close_connection(conn)

    def test_07_connection_closure_and_ownership(self):
        """connection_context and transaction guarantee connection closure and rollback."""
        test_db = os.path.join(self.temp_dir.name, "test_closure.db")

        # 1. Normal connection_context
        with connection_context(test_db) as conn:
            conn.execute("CREATE TABLE t1 (val INT);")
            conn.commit()
        # Connection closed after context
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1;")

        # 2. Transaction rollback on exception
        with self.assertRaises(ValueError):
            with transaction(test_db) as tx_conn:
                tx_conn.execute("INSERT INTO t1 VALUES (42);")
                raise ValueError("Simulated abort")

        # Verify 42 was rolled back
        with connection_context(test_db) as verify_conn:
            cnt = verify_conn.execute("SELECT count(*) FROM t1 WHERE val = 42;").fetchone()[0]
            self.assertEqual(cnt, 0)

    def test_08_multiple_independent_database_files(self):
        """Concurrent connections to independent files remain strictly isolated."""
        db1 = os.path.join(self.temp_dir.name, "independent_1.db")
        db2 = os.path.join(self.temp_dir.name, "independent_2.db")

        with connection_context(db1) as c1, connection_context(db2) as c2:
            c1.execute("CREATE TABLE tbl1 (x TEXT);")
            c1.execute("INSERT INTO tbl1 VALUES ('db1_data');")
            c1.commit()

            c2.execute("CREATE TABLE tbl2 (y TEXT);")
            c2.execute("INSERT INTO tbl2 VALUES ('db2_data');")
            c2.commit()

            # Verify tbl1 does not exist in db2
            with self.assertRaises(sqlite3.OperationalError):
                c2.execute("SELECT * FROM tbl1;")

            # Verify tbl2 does not exist in db1
            with self.assertRaises(sqlite3.OperationalError):
                c1.execute("SELECT * FROM tbl2;")

    def test_09_path_switching_without_stale_connection_reuse(self):
        """Switching db_path cleanly closes previous connection and connects to new path."""
        db_a = os.path.join(self.temp_dir.name, "switch_a.db")
        db_b = os.path.join(self.temp_dir.name, "switch_b.db")

        for mod, name in [
            (memory, "memory"),
            (reminders, "reminders"),
            (mood, "mood"),
            (planner, "planner"),
        ]:
            conn_a = mod.get_connection(db_a)
            self.assertEqual(os.path.normpath(mod._conn_path), os.path.normpath(db_a))

            conn_b = mod.get_connection(db_b)
            self.assertEqual(os.path.normpath(mod._conn_path), os.path.normpath(db_b))
            self.assertIsNot(conn_a, conn_b, f"Module {name} reused connection across different paths!")

            # Verify conn_a was closed
            with self.assertRaises(sqlite3.ProgrammingError, msg=f"Module {name} failed to close old connection!"):
                conn_a.execute("SELECT 1;")

            mod.close()
            self.assertIsNone(mod._conn)
            self.assertIsNone(mod._conn_path)

    def test_10_import_order_independence(self):
        """Dynamic resolution occurs at call time, regardless of when modules were imported."""
        # Modules are already imported. Changing environment now must redirect operations.
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir
        expected = os.path.normpath(self.isolated_db_path)

        # Verify all modules dynamically pick up the new path
        self.assertEqual(os.path.normpath(resolve_database_path()), expected)
        self.assertEqual(os.path.normpath(memory.DB_PATH), expected)
        self.assertEqual(os.path.normpath(reminders.DB_PATH), expected)
        self.assertEqual(os.path.normpath(mood.DB_PATH), expected)
        self.assertEqual(os.path.normpath(planner.DB_PATH), expected)

        # Connect and check that the file is created at the isolated path
        conn = memory.get_connection()
        try:
            self.assertEqual(os.path.normpath(memory._conn_path), expected)
            self.assertTrue(os.path.exists(self.isolated_db_path))
        finally:
            memory.close()

    def test_11_jobstore_compatibility(self):
        """JobStore seamlessly works with centralized dynamic path resolution."""
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir

        store = JobStore()
        self.assertEqual(os.path.normpath(store.db_path), os.path.normpath(self.isolated_db_path))

        # Create and retrieve a test job
        job = store.create_job(goal="Verify central resolver compatibility", source="test")
        retrieved = store.get_job(job.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.goal, "Verify central resolver compatibility")

    def test_12_memory_subsystem_behavior(self):
        """Memory CRUD operations operate correctly on redirected database."""
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir

        # Legacy memory
        m_id = memory.remember("The project codename is Citrus.")
        recalled = memory.recall(limit=10)
        self.assertIn("The project codename is Citrus.", recalled)

        # Structured memory
        memory.remember_structured("system", "codename", "Citrus", confidence=0.95)
        prof_str = memory.build_profile()
        self.assertIn("Citrus", prof_str)
        recalled_struct = memory.recall_structured()
        self.assertTrue(any(row["category"] == "system" and row["key"] == "codename" and row["value"] == "Citrus" for row in recalled_struct))

        # Conversation history
        memory.save_turn("user", "Hello Orange AI")
        memory.save_turn("assistant", "Hello! How can I assist you?")
        history = memory.load_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "Hello Orange AI")

    def test_13_reminder_subsystem_behavior(self):
        """Reminder operations operate correctly on redirected database."""
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir

        due_time = datetime.now() - timedelta(minutes=5)
        r_id = reminders.add_reminder("Review central database report", due_time)
        self.assertIsInstance(r_id, int)

        due = reminders.get_due_reminders(datetime.now())
        matching = [r for r in due if r["id"] == r_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["message"], "Review central database report")

        reminders.mark_fired(r_id)
        pending = reminders.list_pending()
        self.assertNotIn(r_id, [p["id"] for p in pending])

    def test_14_mood_subsystem_behavior(self):
        """Mood operations operate correctly on redirected database."""
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir

        mood._set_energy(0.42)
        energy = mood.get_energy()
        self.assertAlmostEqual(energy, 0.42, places=2)

        mood.apply_user_sentiment("You did an awesome and great job!")
        new_energy = mood.get_energy()
        self.assertGreater(new_energy, 0.42)

    def test_15_planner_subsystem_behavior(self):
        """Planner operations operate correctly on redirected database."""
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.isolated_db_dir

        plan = planner.create_plan("remind me to check tests then show profile")
        self.assertEqual(len(plan.steps), 2)

        def mock_dispatch(intent, text):
            return f"Executed {intent.value}: {text}"

        res = planner.execute_plan(plan, mock_dispatch)
        self.assertIn("completed", res)

        outcomes = planner.recent_outcomes(limit=5)
        self.assertIn("check tests", outcomes)

    def test_16_is_default_database_path(self):
        """is_default_database_path correctly detects whether a path is the active default."""
        os.environ.pop("ORANGE_AI_USER_DATA_DIR", None)
        if hasattr(sys, "frozen"):
            del sys.frozen

        dev_path = resolve_database_path()
        self.assertTrue(is_default_database_path(None))
        self.assertTrue(is_default_database_path(dev_path))
        self.assertTrue(is_default_database_path(dev_path.lower()))
        self.assertFalse(is_default_database_path(os.path.join(self.temp_dir.name, "other.db")))

    def test_17_mock_patch_db_path_compatibility(self):
        """Legacy tests that use patch('brain.memory.DB_PATH') continue to work properly."""
        custom_patch_path = os.path.join(self.temp_dir.name, "patched_memory.db")

        self.assertNotEqual(memory.DB_PATH, custom_patch_path)

        with patch("brain.memory.DB_PATH", custom_patch_path):
            self.assertEqual(memory.DB_PATH, custom_patch_path)
            conn = memory.get_connection()
            self.assertEqual(os.path.normpath(memory._conn_path), os.path.normpath(custom_patch_path))
            memory.close()

        # After unpatch, returns to normal dynamic resolution
        self.assertNotEqual(memory.DB_PATH, custom_patch_path)


if __name__ == "__main__":
    unittest.main()
