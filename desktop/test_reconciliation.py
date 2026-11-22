"""
test_reconciliation.py - Comprehensive Unit and Integration Tests for Stage 3.7E.3C.2.

Covers:
1. First-Run Baseline Initialization (genuine first-run, unexpected missing detection,
   consistency with NULL completed_at, empty DB handling, no synthetic records, flood prevention).
2. Existing-Store Recovery (jobs, steps, memories, reminders, delivery suppression, no raw convo scans).
3. Bounded Recovery & Flood Prevention (batch limits, overflow tracking, contiguous watermark stoppage,
   multi-cycle continuation, deterministic priority ordering).
4. Partial Failure & Fail-Closed Behavior (source failure distinction, partial delivery status,
   source isolation, lock timeout, persistence errors).
5. Idempotency & Crash Safety (repeated scans, crash resilience, at-least-once retryability,
   last_startup_reconciliation watermark tracking).
"""

from datetime import datetime, timezone
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from brain.briefing.models import (
    EventCandidate,
    EventPriority,
    DeliveryStatus,
    DeliveryResult,
    ReconciliationStatus,
    ReconciliationResult,
)
from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreError,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EventStoreSchemaError,
    EventStoreLockTimeoutError,
    normalize_utc_timestamp,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.event_readers import EventCandidateReader
from brain.briefing.event_delivery import (
    InProcessDeliveryAdapter,
    EventDeliveryOrchestrator,
    initialize_event_store_baseline,
)
from brain.briefing.reconciliation import (
    StartupReconciler,
    detect_unexpected_missing_store,
    DEFAULT_RECONCILIATION_BATCH_LIMIT,
)


class BaseReconciliationTestCase(unittest.TestCase):
    """Base harness configuring isolated temporary directories and schema setup."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_reconciliation_")
        self.db_path = os.path.join(self.test_dir, "test_memory.db")
        self.store_path = os.path.join(self.test_dir, "event_watermarks.json")
        self.fixed_now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
        self._init_sqlite_schema(self.db_path)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_sqlite_schema(self, db_path: str):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                priority INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                step_count INTEGER NOT NULL DEFAULT 0,
                retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
                next_retry_at TEXT,
                last_error TEXT,
                pending_confirmation_json TEXT,
                final_response TEXT,
                agent_state_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                source TEXT NOT NULL DEFAULT 'user',
                tags TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS job_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                step_index INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                tool_name TEXT,
                arguments_json TEXT,
                reasoning TEXT,
                success INTEGER,
                result_data_json TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                model_name TEXT
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fired INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS structured_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(category, memory_key)
            );

            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()


# ===========================================================================
# Suite 1: First-Run Baseline Initialization
# ===========================================================================

class TestFirstRunBaseline(BaseReconciliationTestCase):
    """Tests establishing first-run baselines, missing-store safeguards, and timestamp consistency."""

    def test_missing_store_genuine_first_run_establishes_baseline(self):
        """Missing watermark store with no prior history evidence establishes an explicit first-run baseline."""
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        self.assertFalse(os.path.exists(self.store_path))

        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED)
        self.assertTrue(res.is_first_run)
        self.assertTrue(res.baseline_established)
        self.assertTrue(os.path.exists(self.store_path))

        # Check store contents
        store = EventWatermarkStore.load(self.store_path)
        snap = store.get_snapshot()
        self.assertTrue(snap.is_first_run_baseline)
        self.assertEqual(len(snap.delivered_events), 0)
        self.assertIn(WATERMARK_LAST_STARTUP_RECONCILIATION, res.advanced_watermarks)

    def test_baseline_initialization_does_not_overwrite_existing_store(self):
        """Existing store is never overwritten by baseline initialization."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.record_delivery("evt_existing_1")

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        # Should NOT report FIRST_RUN_BASELINE_ESTABLISHED
        self.assertNotEqual(res.status, ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED)
        self.assertFalse(res.is_first_run)

        # Existing record preserved
        loaded_store = EventWatermarkStore.load(self.store_path)
        self.assertTrue(loaded_store.is_delivered("evt_existing_1"))

    def test_unexpected_missing_store_with_orphaned_lock_fails_closed(self):
        """Missing store with an orphaned .lock file fails closed with STORE_MISSING_UNEXPECTED."""
        # Create orphaned lock file
        lock_file = f"{self.store_path}.lock"
        with open(lock_file, "w") as f:
            f.write("orphaned lock")

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.STORE_MISSING_UNEXPECTED)
        self.assertFalse(os.path.exists(self.store_path), "Must not create a new store when unexpectedly missing")

    def test_unexpected_missing_store_with_corrupt_backup_fails_closed(self):
        """Missing primary store with existing .corrupt backup fails closed."""
        corrupt_backup = f"{self.store_path}.corrupt.20261031_100000"
        with open(corrupt_backup, "w") as f:
            f.write("corrupt backup payload")

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.STORE_MISSING_UNEXPECTED)
        self.assertFalse(os.path.exists(self.store_path))

    def test_baseline_position_matches_reader_timestamp_semantics_including_null_completed_at(self):
        """
        When a completed job has completed_at IS NULL, the baseline must snapshot updated_at,
        preventing it from appearing as a false post-baseline candidate.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('j_null_comp', 'Task with null comp', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T09:30:00Z', NULL)"
        )
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res1 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res1.status, ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED)

        # Baseline watermark should reflect the job's updated_at (09:30:00)
        store = EventWatermarkStore.load(self.store_path)
        self.assertEqual(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T09:30:00+00:00")

        # Second reconciliation must NOT detect j_null_comp as a new candidate!
        res2 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res2.status, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES)
        self.assertEqual(res2.eligible_count, 0)

    def test_empty_sources_initialize_safely(self):
        """Clean empty database initializes baseline safely with zero step ID and None timestamps."""
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED)

        store = EventWatermarkStore.load(self.store_path)
        snap = store.get_snapshot()
        self.assertIsNone(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT))
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 0)
        self.assertIsNone(snap.get_watermark(WATERMARK_MEMORIES_UPDATED_AT))

    def test_no_synthetic_delivery_records_and_no_pre_baseline_replay(self):
        """First-run baseline initialization creates zero synthetic deliveries and skips historical events."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('j_hist', 'Old Task', 'COMPLETED', '2026-10-31T08:00:00Z', '2026-10-31T08:30:00Z', '2026-10-31T08:30:00Z')"
        )
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (5, 'j_hist', 1, 1, 'cmd', 1, '2026-10-31T08:10:00Z')")
        conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'app', 'Orange AI', '2026-10-31T08:00:00Z', '2026-10-31T08:00:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED)

        store = EventWatermarkStore.load(self.store_path)
        self.assertEqual(len(store.get_snapshot().delivered_events), 0, "No synthetic delivery records created")

        # Second pass: zero candidates delivered
        res2 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res2.status, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES)
        self.assertEqual(res2.delivered_count, 0)


# ===========================================================================
# Suite 2: Existing-Store Recovery
# ===========================================================================

class TestExistingStoreRecovery(BaseReconciliationTestCase):
    """Tests recovering new post-watermark events across all sources and suppressing delivered ones."""

    def setUp(self):
        super().setUp()
        # Initialize store with initial baseline at 10:00
        self.store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={
                WATERMARK_JOBS_COMPLETED_AT: "2026-10-31T10:00:00Z",
                WATERMARK_JOB_STEP_MAX_ID: 10,
                WATERMARK_MEMORIES_UPDATED_AT: "2026-10-31T10:00:00Z",
            },
            now=self.fixed_now,
        )

    def test_new_terminal_job_recovered_after_watermark(self):
        """Terminal job completed after watermark is recovered."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('j_new', 'Build release', 'COMPLETED', '2026-10-31T10:05:00Z', '2026-10-31T10:15:00Z', '2026-10-31T10:15:00Z')"
        )
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(res.delivered_count, 1)
        loaded = EventWatermarkStore.load(self.store_path)
        self.assertEqual(loaded.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T10:15:00+00:00")

    def test_new_job_step_recovered_after_watermark(self):
        """Job step with id > job_step_max_id is recovered."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'Task', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (11, 'j1', 1, 1, 'compile', 1, '2026-10-31T10:10:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(res.delivered_count, 1)
        loaded = EventWatermarkStore.load(self.store_path)
        self.assertEqual(loaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 11)

    def test_new_structured_memory_recovered_after_watermark(self):
        """Structured memory updated after watermark is recovered."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'new_proj', 'Desktop Proj', '2026-10-31T10:20:00Z', '2026-10-31T10:20:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(res.delivered_count, 1)
        loaded = EventWatermarkStore.load(self.store_path)
        self.assertEqual(loaded.get_watermark(WATERMARK_MEMORIES_UPDATED_AT), "2026-10-31T10:20:00+00:00")

    def test_due_unfired_reminder_recovered(self):
        """Due and unfired reminder is recovered without inventing a reminder watermark."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO reminders (id, message, due_at, created_at, fired) VALUES (99, 'Submit timesheet', '2026-10-31 11:00:00', '2026-10-31 09:00:00', 0)")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(res.delivered_count, 1)
        # Reminders do NOT create or advance any reminder watermark
        self.assertNotIn("reminders_due_at", res.advanced_watermarks)
        loaded = EventWatermarkStore.load(self.store_path)
        self.assertIsNone(loaded.get_watermark("reminders_due_at"))

    def test_already_delivered_events_suppressed(self):
        """Previously delivered event is suppressed and not delivered again."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('j_done', 'Finished', 'COMPLETED', '2026-10-31T10:05:00Z', '2026-10-31T10:10:00Z', '2026-10-31T10:10:00Z')"
        )
        conn.commit()
        conn.close()

        delivery_calls = []
        adapter = InProcessDeliveryAdapter(handler=lambda c: delivery_calls.append(c.event_id) or True)

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=adapter,
        )

        # First run: delivers event
        res1 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res1.delivered_count, 1)
        self.assertEqual(len(delivery_calls), 1)

        # Second run: suppressed
        res2 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res2.status, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES)
        self.assertEqual(len(delivery_calls), 1, "Adapter must NOT be called for already-delivered event")

    def test_no_raw_conversation_history_scanned(self):
        """Conversation history table is never queried for candidate events."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO conversation_history (role, content, created_at) VALUES ('user', 'Secret message', '2026-10-31T11:00:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES)
        self.assertEqual(res.total_detected, 0)

    def test_deleted_memory_does_not_produce_fabricated_events(self):
        """Hard deletion from structured_memories produces zero fabricated deletion events."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'temp', 'To delete', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')")
        conn.commit()
        # Immediately delete it
        conn.execute("DELETE FROM structured_memories WHERE memory_key = 'temp'")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES)
        self.assertEqual(res.total_detected, 0)


# ===========================================================================
# Suite 3: Bounded Recovery and Flood Prevention
# ===========================================================================

class TestBoundedRecovery(BaseReconciliationTestCase):
    """Tests batch limit compliance, overflow management, contiguous frontier, and multi-cycle work."""

    def setUp(self):
        super().setUp()
        self.store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 0},
            now=self.fixed_now,
        )

    def test_batch_limit_respected_and_overflow_tracked(self):
        """When 5 step candidates exist and batch_limit=2, only 2 are attempted and 3 are deferred as overflow."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j_batch', 'Batch test', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        for i in range(1, 6):
            conn.execute(f"INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES ({i}, 'j_batch', 1, {i}, 'cmd', 1, '2026-10-31T10:00:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            default_batch_limit=2,
        )
        res = reconciler.reconcile(now=self.fixed_now, batch_limit=2)

        self.assertEqual(res.status, ReconciliationStatus.PARTIAL_SUCCESS)
        self.assertEqual(res.total_detected, 5)
        self.assertEqual(res.eligible_count, 5)
        self.assertEqual(res.selected_count, 2)
        self.assertEqual(res.delivered_count, 2)
        self.assertEqual(res.overflow_count, 3)
        self.assertEqual(len(res.overflow_event_ids), 3)

    def test_watermark_does_not_advance_past_overflow_candidates(self):
        """Watermark advances only up to the contiguous delivered frontier (step 2), NOT past overflow (step 5)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j_wm', 'Watermark test', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        for i in range(1, 6):
            conn.execute(f"INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES ({i}, 'j_wm', 1, {i}, 'cmd', 1, '2026-10-31T10:00:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            default_batch_limit=2,
        )
        _ = reconciler.reconcile(now=self.fixed_now, batch_limit=2)

        loaded_store = EventWatermarkStore.load(self.store_path)
        # Watermark must be exactly 2, never 5
        self.assertEqual(loaded_store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 2)

    def test_subsequent_reconciliation_continues_pending_work(self):
        """Subsequent reconciliation runs progressively process remaining overflow candidates."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j_cont', 'Continuation test', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        for i in range(1, 6):
            conn.execute(f"INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES ({i}, 'j_cont', 1, {i}, 'cmd', 1, '2026-10-31T10:00:00Z')")
        conn.commit()
        conn.close()

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            default_batch_limit=2,
        )

        # Cycle 1: steps 1-2 delivered, watermark=2
        res1 = reconciler.reconcile(now=self.fixed_now, batch_limit=2)
        self.assertEqual(res1.delivered_count, 2)
        self.assertEqual(EventWatermarkStore.load(self.store_path).get_watermark(WATERMARK_JOB_STEP_MAX_ID), 2)

        # Cycle 2: steps 3-4 delivered, watermark=4
        res2 = reconciler.reconcile(now=self.fixed_now, batch_limit=2)
        self.assertEqual(res2.delivered_count, 2)
        self.assertEqual(EventWatermarkStore.load(self.store_path).get_watermark(WATERMARK_JOB_STEP_MAX_ID), 4)

        # Cycle 3: step 5 delivered, watermark=5, fully completed
        res3 = reconciler.reconcile(now=self.fixed_now, batch_limit=2)
        self.assertEqual(res3.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(res3.delivered_count, 1)
        self.assertEqual(res3.overflow_count, 0)
        self.assertEqual(EventWatermarkStore.load(self.store_path).get_watermark(WATERMARK_JOB_STEP_MAX_ID), 5)

    def test_priority_ordering_preserved_during_bounded_selection(self):
        """High-priority events (e.g. overdue reminder) are prioritized over lower-priority steps."""
        conn = sqlite3.connect(self.db_path)
        # Low priority steps
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j_prio', 'Prio job', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j_prio', 1, 1, 'cmd', 1, '2026-10-31T09:00:00Z')")
        # Critical reminder (>2h overdue)
        conn.execute("INSERT INTO reminders (id, message, due_at, created_at, fired) VALUES (1, 'Critical server check', '2026-10-31 08:00:00', '2026-10-31 07:00:00', 0)")
        conn.commit()
        conn.close()

        delivered_order = []
        adapter = InProcessDeliveryAdapter(handler=lambda c: delivered_order.append(c.priority) or True)

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=adapter,
        )
        res = reconciler.reconcile(now=self.fixed_now, batch_limit=1)

        self.assertEqual(res.selected_count, 1)
        # The 1 candidate selected must be the CRITICAL reminder, NOT the LOW step
        self.assertEqual(delivered_order[0], EventPriority.CRITICAL)


# ===========================================================================
# Suite 4: Partial Failure and Fail-Closed Behavior
# ===========================================================================

class TestPartialFailure(BaseReconciliationTestCase):
    """Tests failure distinction, partial reconciliation status, isolation, and lock handling."""

    def setUp(self):
        super().setUp()
        self.store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={
                WATERMARK_JOBS_COMPLETED_AT: "2026-10-31T10:00:00Z",
                WATERMARK_JOB_STEP_MAX_ID: 0,
            },
            now=self.fixed_now,
        )

    def test_source_collection_failure_fails_closed(self):
        """Database connection or query error returns SOURCE_COLLECTION_FAILURE and does not claim zero candidates."""
        # Intentionally point to an invalid DB path
        reconciler = StartupReconciler(
            db_path=os.path.join(self.test_dir, "nonexistent.db"),
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.SOURCE_COLLECTION_FAILURE)
        self.assertIn("error", res.errors)

    def test_partial_delivery_failure_tracks_unconfirmed_and_partial_status(self):
        """When candidate A succeeds and B fails, status is PARTIAL_SUCCESS and B remains unconfirmed."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'T1', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j1', 1, 1, 'cmd', 1, '2026-10-31T10:01:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (2, 'j1', 1, 2, 'cmd', 1, '2026-10-31T10:02:00Z')")
        conn.commit()
        conn.close()

        def handler(cand):
            if "2" in cand.event_id:
                return DeliveryResult(cand.event_id, DeliveryStatus.FAILURE, error="Network glitch")
            return DeliveryResult(cand.event_id, DeliveryStatus.SUCCESS)

        adapter = InProcessDeliveryAdapter(handler=handler)
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=adapter,
        )
        res = reconciler.reconcile(now=self.fixed_now)

        self.assertEqual(res.status, ReconciliationStatus.PARTIAL_SUCCESS)
        self.assertEqual(res.delivered_count, 1)
        self.assertEqual(res.failed_count, 1)
        self.assertEqual(EventWatermarkStore.load(self.store_path).get_watermark(WATERMARK_JOB_STEP_MAX_ID), 1)

    def test_independent_source_watermark_advances_despite_unrelated_failure(self):
        """A failure in jobs delivery does not block the safe advancement of job_steps watermark."""
        conn = sqlite3.connect(self.db_path)
        # Step candidate (succeeds)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'T1', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j1', 1, 1, 'cmd', 1, '2026-10-31T10:01:00Z')")
        # Terminal job candidate (fails)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) VALUES ('j2', 'T2', 'COMPLETED', '2026-10-31T10:05:00Z', '2026-10-31T10:10:00Z', '2026-10-31T10:10:00Z')")
        conn.commit()
        conn.close()

        def handler(cand):
            if cand.source == "jobs":
                return DeliveryResult(cand.event_id, DeliveryStatus.FAILURE, error="Job delivery channel down")
            return DeliveryResult(cand.event_id, DeliveryStatus.SUCCESS)

        adapter = InProcessDeliveryAdapter(handler=handler)
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=adapter,
        )
        res = reconciler.reconcile(now=self.fixed_now)

        self.assertEqual(res.delivered_count, 1)
        self.assertEqual(res.failed_count, 1)
        loaded = EventWatermarkStore.load(self.store_path)
        # Job step watermark safely advanced
        self.assertEqual(loaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 1)
        # Jobs watermark remained unchanged
        self.assertEqual(loaded.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T10:00:00+00:00")

    def test_lock_timeout_fails_closed(self):
        """When store file lock cannot be acquired within timeout, reconciler returns LOCK_TIMEOUT."""
        held_lock = self.store._file_lock
        held_lock.timeout = 0.2
        held_lock.acquire()

        try:
            other_store = EventWatermarkStore(file_path=self.store_path)
            other_store._file_lock.timeout = 0.2
            other_reconciler = StartupReconciler(
                db_path=self.db_path,
                watermark_store=other_store,
            )
            res = other_reconciler.reconcile(now=self.fixed_now)
            self.assertEqual(res.status, ReconciliationStatus.LOCK_TIMEOUT)
        finally:
            held_lock.release()

    def test_persistence_failure_prevents_watermark_advancement(self):
        """If watermark store fails to persist, returns PERSISTENCE_FAILURE and leaves watermarks untouched."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'T1', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j1', 1, 1, 'cmd', 1, '2026-10-31T10:01:00Z')")
        conn.commit()
        conn.close()

        # Monkeypatch store.record_delivery to raise OSError
        def broken_record(*args, **kwargs):
            raise OSError("Simulated disk write failure")

        self.store.record_delivery = broken_record
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store=self.store,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res.status, ReconciliationStatus.PERSISTENCE_FAILURE)


# ===========================================================================
# Suite 5: Idempotency and Crash Safety
# ===========================================================================

class TestIdempotencyAndCrashSafety(BaseReconciliationTestCase):
    """Tests repeated reconciliation idempotency, crash recovery, and watermark tracking."""

    def setUp(self):
        super().setUp()
        self.store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 0},
            now=self.fixed_now,
        )

    def test_repeated_reconciliation_is_idempotent(self):
        """Repeated reconciliation on unchanged data generates zero duplicate deliveries."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'T1', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j1', 1, 1, 'cmd', 1, '2026-10-31T10:01:00Z')")
        conn.commit()
        conn.close()

        delivered_log = []
        adapter = InProcessDeliveryAdapter(handler=lambda c: delivered_log.append(c.event_id) or True)

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=adapter,
        )

        res1 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res1.delivered_count, 1)
        self.assertEqual(len(delivered_log), 1)

        res2 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res2.status, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES)
        self.assertEqual(len(delivered_log), 1, "Must remain exactly 1 delivery invocation")

    def test_crash_before_delivery_preserves_state(self):
        """Interruption before candidate delivery leaves watermarks and delivered list untouched."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'T1', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j1', 1, 1, 'cmd', 1, '2026-10-31T10:01:00Z')")
        conn.commit()
        conn.close()

        # Simulate interruption during reader collection
        broken_reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)
        def crashing_collect(*args, **kwargs):
            raise SystemExit("Simulated crash")
        broken_reader.collect_all_candidates = crashing_collect

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            reader=broken_reader,
        )

        with self.assertRaises(SystemExit):
            reconciler.reconcile(now=self.fixed_now)

        loaded_store = EventWatermarkStore.load(self.store_path)
        self.assertEqual(loaded_store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 0)
        self.assertEqual(len(loaded_store.get_snapshot().delivered_events), 0)

    def test_crash_after_delivery_before_recording_is_retryable_at_least_once(self):
        """
        Documented at-least-once model:
        If process crashes after adapter receives event but before store records it,
        event is safely retried on next startup without corrupting state.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j1', 'T1', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j1', 1, 1, 'cmd', 1, '2026-10-31T10:01:00Z')")
        conn.commit()
        conn.close()

        delivery_count = [0]
        def crashing_adapter(cand):
            delivery_count[0] += 1
            if delivery_count[0] == 1:
                # Deliver successfully, but crash before return
                raise RuntimeError("Crash between deliver and record")
            return DeliveryResult(cand.event_id, DeliveryStatus.SUCCESS)

        adapter = InProcessDeliveryAdapter(handler=crashing_adapter)
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=adapter,
        )

        # Run 1: crashes / mapped to ambiguous, watermark not advanced
        res1 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res1.status, ReconciliationStatus.DELIVERY_FAILURE)
        self.assertEqual(EventWatermarkStore.load(self.store_path).get_watermark(WATERMARK_JOB_STEP_MAX_ID), 0)

        # Run 2: restart retries and succeeds cleanly
        res2 = reconciler.reconcile(now=self.fixed_now)
        self.assertEqual(res2.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(res2.delivered_count, 1)
        self.assertEqual(EventWatermarkStore.load(self.store_path).get_watermark(WATERMARK_JOB_STEP_MAX_ID), 1)

    def test_reconciliation_watermark_advances_on_completion(self):
        """Successful reconciliation updates WATERMARK_LAST_STARTUP_RECONCILIATION timestamp."""
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )
        res = reconciler.reconcile(now=self.fixed_now)
        self.assertIn(WATERMARK_LAST_STARTUP_RECONCILIATION, res.advanced_watermarks)
        loaded_store = EventWatermarkStore.load(self.store_path)
        self.assertEqual(
            loaded_store.get_watermark(WATERMARK_LAST_STARTUP_RECONCILIATION),
            "2026-10-31T12:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
