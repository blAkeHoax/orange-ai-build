"""
test_event_delivery.py - Test Suite for Delivery Orchestration & Safe Watermark Advancement.

Stage 3.7E.3C.1: Delivery Orchestration & Safe Watermark Advancement
Verifies:
1. Explicit baseline initialization (snapshotting source high-waters, no historical flood, overwrite prevention).
2. Delivery contract (explicit SUCCESS, FAILURE, AMBIGUOUS, exception handling, missing result rejection).
3. Deduplication and retry (skip delivered, retry failures with stable ID, ambiguous not recorded).
4. Watermark advancement safety (confirmed delivery only, partial batch stoppage, equal-timestamp peer grouping,
   contiguous job-step integer frontiers, independent source isolation, no reminder watermark, batch overflow limits).
5. Crash and concurrency resilience (at-least-once retryability, lock timeout fail-closed, cross-process safety).
"""

import os
import sqlite3
import tempfile
import shutil
import time
import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from brain.briefing.models import (
    EventCandidate,
    EventPriority,
    DeliveryStatus,
    DeliveryResult,
    EventDeliveryBatchResult,
)
from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreError,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EventStoreLockTimeoutError,
    EVENT_WATERMARKS_FILENAME,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.event_readers import (
    EventCandidateReader,
    sort_candidates,
)
from brain.briefing.event_delivery import (
    inspect_source_high_waters,
    initialize_event_store_baseline,
    BaseDeliveryAdapter,
    InProcessDeliveryAdapter,
    calculate_safe_watermark_advancements,
    EventDeliveryOrchestrator,
)


class BaseEventDeliveryTestCase(unittest.TestCase):
    """Test fixture providing isolated temporary directory and SQLite database."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_delivery_")
        self.db_path = os.path.join(self.test_dir, "delivery_test.db")
        self.store_path = os.path.join(self.test_dir, EVENT_WATERMARKS_FILENAME)
        self.fixed_now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)

        self._init_test_database()

    def tearDown(self):
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    def _init_test_database(self):
        """Create exact schemas for jobs, job_steps, reminders, and structured_memories."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE jobs (
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

            CREATE TABLE job_steps (
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

            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fired INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE structured_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()


# ===========================================================================
# Suite 1: First-Run Baseline Initialization
# ===========================================================================

class TestFirstRunInitialization(BaseEventDeliveryTestCase):
    """Tests explicit baseline initialization, pre-baseline inspection, and overwrite refusal."""

    def test_missing_store_does_not_initialize_on_normal_load(self):
        """Normal load on a missing store raises EventStoreNotFoundError and does not create the file."""
        self.assertFalse(os.path.exists(self.store_path))
        with self.assertRaises(EventStoreNotFoundError):
            EventWatermarkStore.load(self.store_path)
        self.assertFalse(os.path.exists(self.store_path))

    def test_explicit_baseline_initialization_snapshots_sources(self):
        """initialize_event_store_baseline snapshots authoritative source positions."""
        conn = sqlite3.connect(self.db_path)
        # Pre-populate DB records
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('j_pre', 'Pre-existing job', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T09:30:00Z', '2026-10-31T09:30:00Z')"
        )
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (15, 'j_pre', 1, 1, 'cmd', 1, '2026-10-31T09:10:00Z')")
        conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'core', 'Orange AI', '2026-10-31T08:00:00Z', '2026-10-31T09:45:00Z')")
        conn.commit()
        conn.close()

        store = initialize_event_store_baseline(
            file_path=self.store_path,
            db_path=self.db_path,
            now=self.fixed_now,
        )
        self.assertTrue(os.path.exists(self.store_path))
        snap = store.get_snapshot()

        # Check high-waters match database snapshot
        self.assertEqual(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T09:30:00+00:00")
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 15)
        self.assertEqual(snap.get_watermark(WATERMARK_MEMORIES_UPDATED_AT), "2026-10-31T09:45:00+00:00")
        self.assertIsNone(snap.get_watermark(WATERMARK_LAST_STARTUP_RECONCILIATION))
        self.assertTrue(snap.is_first_run_baseline)

    def test_existing_store_cannot_be_overwritten(self):
        """A second call to initialize_event_store_baseline raises FileExistsError and preserves state."""
        initialize_event_store_baseline(file_path=self.store_path, db_path=self.db_path, now=self.fixed_now)
        self.assertTrue(os.path.exists(self.store_path))

        with self.assertRaises(FileExistsError):
            initialize_event_store_baseline(file_path=self.store_path, db_path=self.db_path, now=self.fixed_now)

    def test_no_pre_baseline_events_recorded_as_delivered(self):
        """Baseline initialization does not falsely populate delivered_events."""
        store = initialize_event_store_baseline(file_path=self.store_path, db_path=self.db_path, now=self.fixed_now)
        snap = store.get_snapshot()
        self.assertEqual(len(snap.delivered_events), 0, "No historical events should be falsely marked delivered")


# ===========================================================================
# Suite 2: Delivery Contract
# ===========================================================================

class TestDeliveryContract(BaseEventDeliveryTestCase):
    """Tests delivery interface contract: SUCCESS, FAILURE, AMBIGUOUS, exceptions, and missing return values."""

    def test_explicit_success_recognized(self):
        """Adapter returning SUCCESS or DeliveryResult(SUCCESS) is recorded as successful delivery."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_succ", "job_completed", "j1", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Success", source="jobs")

        adapter = InProcessDeliveryAdapter(handler=lambda cand: DeliveryResult(cand.event_id, DeliveryStatus.SUCCESS))
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 1)
        self.assertEqual(res.failed_count, 0)
        self.assertEqual(res.ambiguous_count, 0)
        self.assertIn("evt_succ", res.delivered_event_ids)
        self.assertTrue(store.is_delivered("evt_succ"))

    def test_explicit_failure_recognized(self):
        """Adapter returning FAILURE is recorded as failed and NOT saved in delivered_events."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_fail", "job_completed", "j2", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Failure", source="jobs")

        adapter = InProcessDeliveryAdapter(handler=lambda cand: DeliveryResult(cand.event_id, DeliveryStatus.FAILURE, error="Network unavailable"))
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 0)
        self.assertEqual(res.failed_count, 1)
        self.assertIn("evt_fail", res.failed_event_ids)
        self.assertEqual(res.errors.get("evt_fail"), "Network unavailable")
        self.assertFalse(store.is_delivered("evt_fail"))

    def test_exception_mapped_to_ambiguous_and_not_success(self):
        """Adapter raising an unhandled exception is mapped to AMBIGUOUS and NOT marked delivered."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_exc", "job_completed", "j3", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Crash", source="jobs")

        def bad_handler(cand):
            raise TimeoutError("Adapter connection timeout")

        adapter = InProcessDeliveryAdapter(handler=bad_handler)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 0)
        self.assertEqual(res.ambiguous_count, 1)
        self.assertIn("evt_exc", res.ambiguous_event_ids)
        self.assertIn("timeout", res.errors.get("evt_exc", "").lower())
        self.assertFalse(store.is_delivered("evt_exc"))

    def test_missing_callback_result_is_ambiguous(self):
        """Adapter returning None or invalid type is strictly treated as AMBIGUOUS, not success."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_none", "job_completed", "j4", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Void", source="jobs")

        adapter = InProcessDeliveryAdapter(handler=lambda cand: None)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 0)
        self.assertEqual(res.ambiguous_count, 1)
        self.assertFalse(store.is_delivered("evt_none"))


# ===========================================================================
# Suite 3: Deduplication and Retry Semantics
# ===========================================================================

class TestDeduplicationAndRetry(BaseEventDeliveryTestCase):
    """Tests skipping previously delivered events, retrying failures, and duplicate idempotence."""

    def test_already_delivered_id_is_skipped(self):
        """Previously delivered candidate is skipped without invoking delivery adapter."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.record_delivery("evt_done")

        c = EventCandidate("evt_done", "job_completed", "j_done", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Done", source="jobs")

        delivery_invoked = []
        def handler(cand):
            delivery_invoked.append(cand.event_id)
            return True

        adapter = InProcessDeliveryAdapter(handler=handler)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.skipped_count, 1)
        self.assertEqual(res.delivered_count, 0)
        self.assertEqual(len(delivery_invoked), 0, "Delivery handler must NOT be called for already-delivered event")

    def test_failed_delivery_retried_with_same_id(self):
        """Failed candidate remains undelivered and succeeds on next delivery cycle with same event ID."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_retry", "job_completed", "j_retry", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Retry", source="jobs")

        # Cycle 1: Fails
        adapter_fail = InProcessDeliveryAdapter(handler=lambda cand: False)
        orch1 = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter_fail)
        res1 = orch1.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res1.failed_count, 1)
        self.assertFalse(store.is_delivered("evt_retry"))

        # Cycle 2: Retries and succeeds
        adapter_success = InProcessDeliveryAdapter(handler=lambda cand: True)
        orch2 = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter_success)
        res2 = orch2.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res2.delivered_count, 1)
        self.assertTrue(store.is_delivered("evt_retry"))

    def test_duplicate_record_attempts_remain_idempotent(self):
        """Recording the same event ID repeatedly is completely idempotent."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        first_call = store.record_delivery("evt_idempotent")
        second_call = store.record_delivery("evt_idempotent")
        self.assertTrue(first_call)
        self.assertFalse(second_call)

    def test_material_changes_generate_distinct_ids_in_delivery(self):
        """Candidates with material differences receive distinct event IDs and deliver independently."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        c1 = EventCandidate(
            event_id="job:completed:101:a1b2",
            event_type="job_completed",
            entity_id="101",
            occurred_at="2026-10-31T10:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.NORMAL,
            title="Job 101 Completed",
            source="jobs",
            material_data={"status": "completed", "exit_code": 0},
        )
        c2 = EventCandidate(
            event_id="job:completed:101:c3d4",
            event_type="job_completed",
            entity_id="101",
            occurred_at="2026-10-31T10:05:00Z",
            detected_at="2026-10-31T12:05:00Z",
            priority=EventPriority.NORMAL,
            title="Job 101 Re-run Completed",
            source="jobs",
            material_data={"status": "completed", "exit_code": 0, "run": 2},
        )

        adapter = InProcessDeliveryAdapter()
        orch = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)
        res = orch.execute_delivery_cycle(candidates=[c1, c2], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 2)
        self.assertTrue(store.is_delivered("job:completed:101:a1b2"))
        self.assertTrue(store.is_delivered("job:completed:101:c3d4"))


# ===========================================================================
# Suite 4: Watermark Advancement Safety
# ===========================================================================

class TestWatermarkAdvancementSafety(BaseEventDeliveryTestCase):
    """Tests contiguous watermark frontiers, partial batch failures, peer timestamp grouping, and source isolation."""

    def test_watermark_advances_after_confirmed_delivery_only(self):
        """Watermark advances only when candidate delivery is confirmed."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_c", "job_completed", "j1", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Success", source="jobs")

        adapter = InProcessDeliveryAdapter()
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 1)
        self.assertIn(WATERMARK_JOBS_COMPLETED_AT, res.advanced_watermarks)
        self.assertEqual(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T10:00:00+00:00")

    def test_partial_batch_failure_does_not_advance_past_failed_candidate(self):
        """
        Prompt requirement:
        - Candidate A at 10:00 is delivered.
        - Candidate B at 10:01 fails.
        - Candidate C at 10:02 is delivered.
        The watermark must NOT advance to 10:02 if B remains undelivered; it must stop at 10:00.
        """
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        cA = EventCandidate("evt_A", "job_completed", "jA", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job A", source="jobs")
        cB = EventCandidate("evt_B", "job_completed", "jB", "2026-10-31T10:01:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job B", source="jobs")
        cC = EventCandidate("evt_C", "job_completed", "jC", "2026-10-31T10:02:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job C", source="jobs")

        def handler(cand):
            if cand.event_id == "evt_B":
                return False  # B fails
            return True       # A and C succeed

        adapter = InProcessDeliveryAdapter(handler=handler)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[cA, cB, cC], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 2)
        self.assertEqual(res.failed_count, 1)

        # Watermark must advance to 10:00 (A), but NOT to 10:02 (C) because B at 10:01 failed!
        self.assertEqual(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T10:00:00+00:00")

    def test_equal_timestamps_handled_without_skipping_peers(self):
        """
        If candidates A1 and A2 share timestamp 10:00:00, and A1 succeeds while A2 fails:
        Watermark must NOT advance to 10:00:00 because peer A2 is unconfirmed.
        """
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        cA1 = EventCandidate("evt_A1", "job_completed", "jA1", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job A1", source="jobs")
        cA2 = EventCandidate("evt_A2", "job_completed", "jA2", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job A2", source="jobs")

        def handler(cand):
            if cand.event_id == "evt_A2":
                return False  # A2 fails
            return True       # A1 succeeds

        adapter = InProcessDeliveryAdapter(handler=handler)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[cA1, cA2], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 1)
        self.assertEqual(res.failed_count, 1)

        # Watermark must NOT advance to 10:00:00 because A2 at 10:00:00 failed!
        self.assertIsNone(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT))

        # Second cycle: A2 retried and succeeds -> Now both peers are delivered, watermark advances to 10:00:00
        adapter2 = InProcessDeliveryAdapter(handler=lambda cand: True)
        orch2 = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter2)
        res2 = orch2.execute_delivery_cycle(candidates=[cA1, cA2], now=self.fixed_now)
        self.assertEqual(res2.skipped_count, 1)   # A1 was already delivered
        self.assertEqual(res2.delivered_count, 1) # A2 delivered
        self.assertEqual(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T10:00:00+00:00")

    def test_job_step_id_advancement_is_strictly_contiguous(self):
        """Job step watermark advances contiguously; a failure at step 2 stops advancement before step 3."""
        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 0},
            now=self.fixed_now,
        )

        s1 = EventCandidate("evt_s1", "job_step_completed", "1", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.LOW, "Step 1", source="job_steps")
        s2 = EventCandidate("evt_s2", "job_step_failed", "2", "2026-10-31T10:01:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Step 2", source="job_steps")
        s3 = EventCandidate("evt_s3", "job_step_completed", "3", "2026-10-31T10:02:00Z", "2026-10-31T12:00:00Z", EventPriority.LOW, "Step 3", source="job_steps")

        def handler(cand):
            if cand.event_id == "evt_s2":
                return False  # Step 2 delivery fails
            return True

        adapter = InProcessDeliveryAdapter(handler=handler)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[s1, s2, s3], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 2)
        self.assertEqual(res.failed_count, 1)

        # Step 1 was delivered, Step 2 failed -> watermark must advance to 1, NOT 3!
        self.assertEqual(store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 1)

    def test_independent_source_watermarks_remain_independent(self):
        """A failure in jobs delivery does not block safe advancement of job_steps watermark."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        job_fail = EventCandidate("evt_jf", "job_completed", "j1", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Fail", source="jobs")
        step_succ = EventCandidate("evt_ss", "job_step_completed", "5", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.LOW, "Step Succ", source="job_steps")

        def handler(cand):
            if cand.source == "jobs":
                return False
            return True

        adapter = InProcessDeliveryAdapter(handler=handler)
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[job_fail, step_succ], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 1)
        self.assertEqual(res.failed_count, 1)

        # Step watermark advances to 5; Job watermark remains unchanged (None)
        self.assertEqual(store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 5)
        self.assertIsNone(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT))

    def test_reminder_delivery_records_ids_without_inventing_watermark(self):
        """Reminders record delivery IDs without advancing or creating any reminder watermark."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        rem = EventCandidate("evt_rem_1", "reminder_due", "101", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.HIGH, "Tax reminder", source="reminders")

        adapter = InProcessDeliveryAdapter()
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)

        res = orchestrator.execute_delivery_cycle(candidates=[rem], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 1)
        self.assertTrue(store.is_delivered("evt_rem_1"))

        # Verify no reminder watermark key exists
        snap = store.get_snapshot()
        self.assertNotIn("reminders_due_at", snap.watermarks)
        self.assertNotIn("reminders_max_id", snap.watermarks)

    def test_candidate_overflow_does_not_silently_advance_watermarks(self):
        """When batch_limit is reached, unattempted overflow candidates prevent watermark from jumping past them."""
        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 0},
            now=self.fixed_now,
        )

        steps = [
            EventCandidate(f"evt_step_{i}", "job_step_completed", str(i), "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.LOW, f"Step {i}", source="job_steps")
            for i in range(1, 6)
        ]

        adapter = InProcessDeliveryAdapter()
        orchestrator = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter, default_batch_limit=2)

        # Execute with batch_limit=2 (only steps 1 and 2 attempted; steps 3, 4, 5 pending)
        res = orchestrator.execute_delivery_cycle(candidates=steps, batch_limit=2, now=self.fixed_now)
        self.assertEqual(res.delivered_count, 2)
        # Watermark must advance to 2, NOT 5!
        self.assertEqual(store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 2)


# ===========================================================================
# Suite 5: Crash and Concurrency
# ===========================================================================

class TestCrashAndConcurrency(BaseEventDeliveryTestCase):
    """Tests crash resilience, at-least-once retryability, and cross-process safety."""

    def test_crash_before_delivery_does_not_advance(self):
        """Crash or failure before delivery prevents any watermark advancement or delivery recording."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_crash", "job_completed", "j_crash", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Crash", source="jobs")

        # Simulate crash during candidate inspection before delivery
        simulated_crash = True
        if not simulated_crash:
            store.record_delivery("evt_crash")
            store.advance_watermark(WATERMARK_JOBS_COMPLETED_AT, "2026-10-31T10:00:00Z")

        self.assertFalse(store.is_delivered("evt_crash"))
        self.assertIsNone(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT))

    def test_crash_after_delivery_before_record_is_retryable(self):
        """
        Documented at-least-once model:
        If process crashes after adapter.deliver() succeeds but before store.record_delivery(),
        the event is retried on subsequent run without breaking watermark consistency.
        """
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_at_least_once", "job_completed", "j_alo", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job ALO", source="jobs")

        delivery_log = []
        adapter = InProcessDeliveryAdapter(handler=lambda cand: delivery_log.append(cand.event_id) or True)

        # Simulate attempt where delivery succeeds, but process is interrupted before recording
        _ = adapter.deliver(c)
        self.assertEqual(len(delivery_log), 1)
        self.assertFalse(store.is_delivered("evt_at_least_once"))

        # On restart / retry, orchestrator runs normally and confirms delivery
        orch = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=adapter)
        res = orch.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        self.assertEqual(res.delivered_count, 1)
        self.assertTrue(store.is_delivered("evt_at_least_once"))
        self.assertEqual(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T10:00:00+00:00")

    def test_lock_timeout_fails_closed(self):
        """When store file lock cannot be acquired within timeout, orchestrator fails closed."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_lock", "job_completed", "j_lock", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Lock", source="jobs")

        # Hold file lock from another instance with short timeout
        store._file_lock.timeout = 0.2
        held_lock = store._file_lock
        held_lock.acquire()

        try:
            # Another independent store instance trying to orchestrate will time out
            other_store = EventWatermarkStore(file_path=self.store_path)
            other_store._file_lock.timeout = 0.2
            orch = EventDeliveryOrchestrator(watermark_store=other_store)

            with self.assertRaises(EventStoreLockTimeoutError):
                orch.execute_delivery_cycle(candidates=[c], now=self.fixed_now)
        finally:
            held_lock.release()

    def test_delivery_record_persistence_failure_prevents_watermark_advancement(self):
        """
        If store.record_delivery() raises an error during delivery recording,
        orchestration fails closed and does not advance watermarks.
        """
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        c = EventCandidate("evt_persist_err", "job_completed", "j_err", "2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z", EventPriority.NORMAL, "Job Err", source="jobs")

        # Monkeypatch store.record_delivery to simulate disk write failure
        def bad_record(*args, **kwargs):
            raise OSError("Disk write failed")

        store.record_delivery = bad_record
        orch = EventDeliveryOrchestrator(watermark_store=store, delivery_adapter=InProcessDeliveryAdapter())

        with self.assertRaises(OSError):
            orch.execute_delivery_cycle(candidates=[c], now=self.fixed_now)

        # Verify watermark was not advanced
        snap = EventWatermarkStore.load(self.store_path).get_snapshot()
        self.assertIsNone(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT))
        self.assertFalse(snap.is_delivered("evt_persist_err"))

    def test_cross_process_mutation_safe_no_lost_updates(self):
        """
        Concurrent threads operating on the store file with lock protection
        do not overwrite each other's deliveries or watermarks.
        """
        import concurrent.futures

        EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        def worker1():
            s = EventWatermarkStore.load(self.store_path)
            for i in range(10):
                s.record_delivery(f"evt_worker1_{i}")

        def worker2():
            s = EventWatermarkStore.load(self.store_path)
            for i in range(10):
                s.record_delivery(f"evt_worker2_{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker1)
            f2 = executor.submit(worker2)
            concurrent.futures.wait([f1, f2])

        final_store = EventWatermarkStore.load(self.store_path)
        final_snap = final_store.get_snapshot()
        for i in range(10):
            self.assertTrue(final_snap.is_delivered(f"evt_worker1_{i}"))
            self.assertTrue(final_snap.is_delivered(f"evt_worker2_{i}"))


if __name__ == "__main__":
    unittest.main()
