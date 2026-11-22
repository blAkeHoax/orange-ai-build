"""
test_gui_event_integration.py - Integration Test Suite for GUI & Scheduler Event Integration.

Stage 3.7E.3C.3: GUI & Scheduler Event Integration
Tests:
1. Startup reconciliation integration (non-blocking, first-run baseline, corrupt store fail-closed).
2. Scheduler callback integration (reminder & job triggers, coalescing, no direct duplicates).
3. ChatView delivery adapter (UI confirmation, timeout handling, quiet internal step progression).
4. Authoritative deduplication across reconciliation cycles and restarts.
5. Concurrency and single-flight execution (no simultaneous cycles, morning briefing coexistence).
6. Safe GUI shutdown (bounded termination, late callback discard, clean resource closure).
"""

import os
import sys
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
from gui.services.assistant_bridge import AssistantBridge
from gui.services.event_coordinator import (
    EventCoordinator,
    ChatViewDeliveryAdapter,
    format_event_candidate,
)
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
    generate_event_id,
    normalize_utc_timestamp,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.event_readers import EventCandidateReader
from brain.briefing.reconciliation import StartupReconciler
from brain.jobs.models import Job, JobStatus
from brain.agent_models import AgentAction, ActionType
import main


def _seed_test_schema(db_path: str):
    """Seed comprehensive schema required by all event sources in a temporary DB."""
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
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test Suite 1: Startup Reconciliation Integration
# ---------------------------------------------------------------------------

class TestStartupReconciliationIntegration(unittest.TestCase):
    """Test Suite 1: GUI Startup Integration, Non-Blocking Recovery, and Baseline Invariants."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _seed_test_schema(self.db_path)
        self.store_path = os.path.join(self.temp_dir.name, "event_watermarks.json")
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")

        self.db_patch = patch("brain.memory.DB_PATH", self.db_path)
        self.db_path_patch = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patch = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        self.db_patch.start()
        self.db_path_patch.start()
        self.settings_patch.start()

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
        self._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or self.store_path
            target_db = db_path or self.db_path
            self._orig_coord_init(coord_self, app, bridge=bridge, db_path=target_db, watermark_store_path=store_path, **kwargs)
        self.coord_patch = patch.object(EventCoordinator, "__init__", _isolated_coord_init)
        self.coord_patch.start()

        main.ensure_initialized()
        ctk.set_appearance_mode("light")

    def tearDown(self):
        self.coord_patch.stop()
        self.db_patch.stop()
        self.db_path_patch.stop()
        self.settings_patch.stop()
        main.job_manager = None
        main.job_store = None
        main.job_runtime = None
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_gui_startup_schedules_reconciliation_without_blocking(self):
        """1. GUI startup schedules non-blocking reconciliation worker via after()."""
        with patch("brain.scheduler.start"), \
             patch("gui.services.event_coordinator.EventCoordinator.trigger_reconciliation") as mock_trigger:
            app = OrangeAIApp()
            app.withdraw()
            try:
                self.assertIsNotNone(app._startup_recon_after_id)
                self.assertIsNotNone(app._event_coordinator)

                # Fire the scheduled after callback
                app._run_startup_reconciliation()
                mock_trigger.assert_called_once_with(reason="startup")
            finally:
                app._on_close()

    def test_first_run_baseline_established_on_clean_start(self):
        """2. Clean environment establishes baseline via reconciler without historical notifications."""
        # Pre-seed an old completed job in DB
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at, source) "
            "VALUES ('job-old', 'Old Task', 'COMPLETED', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', '2026-10-01T00:05:00Z', 'interactive')"
        )
        conn.commit()
        conn.close()

        with patch("brain.scheduler.start"):
            app = OrangeAIApp()
            app.withdraw()
            try:
                # Configure coordinator with isolated store path
                coord = EventCoordinator(app, db_path=self.db_path, watermark_store_path=self.store_path)
                res = coord.reconciler.reconcile()

                self.assertEqual(res.status, ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED)
                self.assertEqual(res.delivered_count, 0)
                self.assertTrue(os.path.exists(self.store_path))

                # Verify watermark snapshotted the pre-existing job
                store = EventWatermarkStore.load(self.store_path)
                self.assertIn(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), ("2026-10-01T00:05:00Z", "2026-10-01T00:05:00+00:00"))
            finally:
                app._on_close()

    def test_existing_store_not_overwritten(self):
        """3. An existing watermark store is preserved and not overwritten on startup."""
        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 42},
        )

        with patch("brain.scheduler.start"):
            app = OrangeAIApp()
            app.withdraw()
            try:
                coord = EventCoordinator(app, db_path=self.db_path, watermark_store_path=self.store_path)
                res = coord.reconciler.reconcile()

                self.assertIn(res.status, (ReconciliationStatus.SUCCESS, ReconciliationStatus.NO_ELIGIBLE_CANDIDATES))
                reloaded = EventWatermarkStore.load(self.store_path)
                self.assertEqual(reloaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 42)
            finally:
                app._on_close()

    def test_corrupt_store_fails_closed_safely(self):
        """4. Corrupt watermark store causes coordinator to fail closed without crashing GUI."""
        with open(self.store_path, "w", encoding="utf-8") as f:
            f.write("NOT_VALID_JSON{{{")

        with patch("brain.scheduler.start"):
            app = OrangeAIApp()
            app.withdraw()
            try:
                coord = EventCoordinator(app, db_path=self.db_path, watermark_store_path=self.store_path)
                res = coord._execute_cycle()

                self.assertIsNotNone(res)
                self.assertEqual(res.status, ReconciliationStatus.STORE_CORRUPT)
                self.assertEqual(res.delivered_count, 0)
            finally:
                app._on_close()

    def test_no_duplicate_scheduler_starts(self):
        """5. Re-invoking _start_scheduler is strictly idempotent."""
        with patch("brain.scheduler.start") as mock_sched_start:
            app = OrangeAIApp()
            app.withdraw()
            try:
                self.assertEqual(mock_sched_start.call_count, 1)
                app._start_scheduler()
                self.assertEqual(mock_sched_start.call_count, 1)
            finally:
                app._on_close()


# ---------------------------------------------------------------------------
# Test Suite 2: Scheduler Callback Integration
# ---------------------------------------------------------------------------

class TestSchedulerCallbackIntegration(unittest.TestCase):
    """Test Suite 2: Scheduler Callbacks, Coalescing, and Deduplication."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _seed_test_schema(self.db_path)
        self.store_path = os.path.join(self.temp_dir.name, "event_watermarks.json")
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")

        self.db_patch = patch("brain.memory.DB_PATH", self.db_path)
        self.db_path_patch = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patch = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        self.db_patch.start()
        self.db_path_patch.start()
        self.settings_patch.start()

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
        self._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or self.store_path
            target_db = db_path or self.db_path
            self._orig_coord_init(coord_self, app, bridge=bridge, db_path=target_db, watermark_store_path=store_path, **kwargs)
        self.coord_patch = patch.object(EventCoordinator, "__init__", _isolated_coord_init)
        self.coord_patch.start()

        main.ensure_initialized()
        ctk.set_appearance_mode("light")

        with patch("brain.scheduler.start"):
            self.app = OrangeAIApp()
            self.app.withdraw()
            self.app._event_coordinator = EventCoordinator(
                self.app,
                db_path=self.db_path,
                watermark_store_path=self.store_path,
            )

    def tearDown(self):
        try:
            self.app._on_close()
        except Exception:
            pass
        self.coord_patch.stop()
        self.db_patch.stop()
        self.db_path_patch.stop()
        self.settings_patch.stop()
        main.job_manager = None
        main.job_store = None
        main.job_runtime = None
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_reminder_callback_triggers_reconciliation_and_dispatches_ui(self):
        """6. Reminder callback delivers notification to UI and signals coordinator."""
        reminder = {"id": 105, "message": "Prepare demo", "due_at": "2026-10-31T12:00:00"}

        # Correction B: When coordinator is present, acts as signal only (no direct UI bypass)
        with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch, \
             patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger:
            rem_res = self.app._on_scheduler_reminder_due(reminder)
            self.assertFalse(rem_res)
            mock_dispatch.assert_not_called()
            mock_trigger.assert_called_once_with(reason="reminder_due")

        # Fallback: When coordinator is absent, delivers directly to UI
        self.app._event_coordinator = None
        with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            rem_res = self.app._on_scheduler_reminder_due(reminder)
            self.assertTrue(rem_res)
            mock_dispatch.assert_called_once()
            self.assertIn("#105", mock_dispatch.call_args[0][1])

    def test_job_event_callback_triggers_reconciliation_and_dispatches_ui(self):
        """7. Job event callback delivers notification to UI and signals coordinator."""
        job = Job(id="job-abc-123", goal="Index files", status=JobStatus.COMPLETED, final_response="Indexed 50 files")

        # Correction B: When coordinator is present, acts as signal only
        with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch, \
             patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger:
            self.app._on_scheduler_job_event(job)
            mock_dispatch.assert_not_called()
            mock_trigger.assert_called_once_with(reason="job_event")

        # Fallback: When coordinator is absent, delivers directly to UI
        self.app._event_coordinator = None
        with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            self.app._on_scheduler_job_event(job)
            mock_dispatch.assert_called_once()
            self.assertIn("Completed: Indexed 50 files", mock_dispatch.call_args[0][1])

    def test_multiple_callbacks_coalesced_safely(self):
        """8. Rapid multiple triggers coalesce into a single rerun without spawning multiple workers."""
        coord = self.app._event_coordinator

        # Acquire running state
        with coord._lock:
            coord._is_running = True
            coord._rerun_requested = False

        # Fire 5 rapid triggers
        for i in range(5):
            res = coord.trigger_reconciliation(reason=f"rapid_{i}")
            self.assertTrue(res)

        # Worker should have coalesced all triggers into a single rerun flag
        with coord._lock:
            self.assertTrue(coord._rerun_requested)
            coord._is_running = False
            coord._rerun_requested = False

    def test_no_duplicate_direct_and_pipeline_notifications(self):
        """9. Event delivered records in store so pipeline does not duplicate it."""
        # Initialize baseline store before new event occurs
        EventWatermarkStore.initialize_first_run(file_path=self.store_path)

        job = Job(
            id="job-dedup-1",
            goal="Format disk",
            status=JobStatus.COMPLETED,
            completed_at="2026-10-31T10:00:00Z",
            final_response="Success",
            source="interactive",
        )

        # Seed in DB as completed
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at, final_response, source) "
            "VALUES ('job-dedup-1', 'Format disk', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z', 'Success', 'interactive')"
        )
        conn.commit()
        conn.close()

        # Step 1: Callback signals coordinator without duplicate direct UI bypass
        with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch, \
             patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger:
            self.app._on_scheduler_job_event(job)
            self.assertEqual(mock_dispatch.call_count, 0)
            mock_trigger.assert_called_once_with(reason="job_event")

        # Step 2: Now run reconciliation pipeline with confirmed UI acknowledgement
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = self.app._event_coordinator.reconciler.reconcile()
            # Candidate should have been delivered by the pipeline
            self.assertEqual(res.delivered_count, 1)

            # Step 3: Run pipeline again - already delivered candidate is deduplicated
            res2 = self.app._event_coordinator.reconciler.reconcile()
            self.assertEqual(res2.delivered_count, 0)


# ---------------------------------------------------------------------------
# Test Suite 3: ChatView Delivery Adapter & UI Acknowledgement
# ---------------------------------------------------------------------------

class TestUIDeliveryAdapter(unittest.TestCase):
    """Test Suite 3: Thread-Safety, Confirmation Contract, and Step Progression."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = MagicMock()
        self.app._is_shutting_down = False
        self.app._views_cache = {"chat": MagicMock()}

        self.bridge = MagicMock(spec=AssistantBridge)
        self.bridge._is_closed = False
        self.bridge.dispatch_with_ack = MagicMock()

        self.adapter = ChatViewDeliveryAdapter(self.app, bridge=self.bridge, timeout=2.0)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_ui_presentation_success_confirmed_before_recording(self):
        """10. Adapter confirms UI presentation and returns SUCCESS with timestamp."""
        candidate = EventCandidate(
            event_id="evt_test_10",
            event_type="reminder_due",
            entity_id="10",
            occurred_at="2026-10-31T12:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.HIGH,
            title="Meeting with Bob",
            material_data={"reminder_id": 10, "message": "Meeting with Bob"},
            source="reminders",
        )

        self.bridge.dispatch_with_ack.return_value = (True, True, None)

        result = self.adapter.deliver(candidate)

        self.assertEqual(result.status, DeliveryStatus.SUCCESS)
        self.assertIsNotNone(result.delivered_at)
        self.bridge.dispatch_with_ack.assert_called_once()

    def test_ui_presentation_failure_returns_failure(self):
        """11. Error during UI execution returns FAILURE status."""
        candidate = EventCandidate(
            event_id="evt_test_11",
            event_type="job_failed",
            entity_id="job-11",
            occurred_at="2026-10-31T12:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.HIGH,
            title="Job failed",
            material_data={"job_id": "job-11", "error": "Disk full"},
            source="jobs",
        )

        self.bridge.dispatch_with_ack.return_value = (False, None, "Widget destroyed")

        result = self.adapter.deliver(candidate)

        self.assertEqual(result.status, DeliveryStatus.FAILURE)
        self.assertIn("Widget destroyed", result.error or "")

    def test_dispatch_timeout_returns_ambiguous(self):
        """12. Unacknowledged UI dispatch past timeout returns AMBIGUOUS."""
        candidate = EventCandidate(
            event_id="evt_test_12",
            event_type="job_completed",
            entity_id="job-12",
            occurred_at="2026-10-31T12:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.NORMAL,
            title="Job completed",
            material_data={"job_id": "job-12"},
            source="jobs",
        )

        self.bridge.dispatch_with_ack.return_value = (False, None, "UI dispatch timed out after 2.0s")

        result = self.adapter.deliver(candidate)

        self.assertEqual(result.status, DeliveryStatus.AMBIGUOUS)
        self.assertIn("timed out", (result.error or "").lower())

    def test_quiet_progression_for_internal_steps(self):
        """13. Internal successful step returns SUCCESS immediately without ChatView dispatch."""
        candidate = EventCandidate(
            event_id="evt_step_13",
            event_type="job_step_completed",
            entity_id="13",
            occurred_at="2026-10-31T12:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.LOW,
            title="Step 1 completed",
            material_data={"step_id": 13, "job_id": "job-13", "step_index": 1},
            source="job_steps",
        )

        result = self.adapter.deliver(candidate)

        self.assertEqual(result.status, DeliveryStatus.SUCCESS)
        self.bridge.dispatch_with_ack.assert_not_called()


# ---------------------------------------------------------------------------
# Test Suite 4: Deduplication Across Cycles & Restarts
# ---------------------------------------------------------------------------

class TestDeduplicationAcrossCycles(unittest.TestCase):
    """Test Suite 4: Cross-Cycle and Cross-Restart Deduplication."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _seed_test_schema(self.db_path)
        self.store_path = os.path.join(self.temp_dir.name, "event_watermarks.json")

        EventWatermarkStore.initialize_first_run(self.store_path)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_same_event_not_displayed_twice_across_cycles(self):
        """14. A candidate delivered in cycle 1 is not re-delivered in cycle 2."""
        # Insert a completed job
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at, final_response, source) "
            "VALUES ('job-c1', 'Compile C', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T09:30:00Z', '2026-10-31T09:30:00Z', 'Done', 'interactive')"
        )
        conn.commit()
        conn.close()

        delivered_records = []
        mock_adapter = MagicMock()
        mock_adapter.deliver.side_effect = lambda c: DeliveryResult(
            event_id=c.event_id,
            status=DeliveryStatus.SUCCESS,
            delivered_at="2026-10-31T10:00:00Z",
        )

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=mock_adapter,
        )

        # Cycle 1: Delivers the job
        res1 = reconciler.reconcile()
        self.assertEqual(res1.delivered_count, 1)
        self.assertEqual(mock_adapter.deliver.call_count, 1)

        # Cycle 2: Same job is skipped
        res2 = reconciler.reconcile()
        self.assertEqual(res2.delivered_count, 0)
        self.assertEqual(mock_adapter.deliver.call_count, 1)

    def test_failed_event_retried_in_next_cycle(self):
        """15. Candidate that failed delivery in cycle 1 is retried in cycle 2."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at, final_response, source) "
            "VALUES ('job-retry', 'Build APK', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T09:30:00Z', '2026-10-31T09:30:00Z', 'Done', 'interactive')"
        )
        conn.commit()
        conn.close()

        mock_adapter = MagicMock()
        # Fail cycle 1, succeed cycle 2
        mock_adapter.deliver.side_effect = [
            DeliveryResult(event_id="dummy", status=DeliveryStatus.FAILURE, error="Network drop"),
            DeliveryResult(event_id="dummy", status=DeliveryStatus.SUCCESS, delivered_at="2026-10-31T10:00:00Z"),
        ]

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=mock_adapter,
        )

        # Cycle 1: Fails
        res1 = reconciler.reconcile()
        self.assertEqual(res1.failed_count, 1)
        self.assertEqual(res1.delivered_count, 0)

        # Cycle 2: Retried and succeeds
        res2 = reconciler.reconcile()
        self.assertEqual(res2.delivered_count, 1)
        self.assertEqual(mock_adapter.deliver.call_count, 2)


# ---------------------------------------------------------------------------
# Test Suite 5: Concurrency & Single-Flight Execution
# ---------------------------------------------------------------------------

class TestConcurrencyAndSingleFlight(unittest.TestCase):
    """Test Suite 5: Single-Flight Coordination, Non-Blocking Morning Briefing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _seed_test_schema(self.db_path)
        self.store_path = os.path.join(self.temp_dir.name, "event_watermarks.json")
        EventWatermarkStore.initialize_first_run(self.store_path)

        self.app = MagicMock()
        self.app._is_shutting_down = False

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_single_flight_enforces_one_cycle_at_a_time(self):
        """16. Multiple calls to trigger_reconciliation enforce at most one running worker."""
        coord = EventCoordinator(
            self.app,
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )

        execution_counts = [0]
        block_event = threading.Event()

        def slow_reconcile(*args, **kwargs):
            execution_counts[0] += 1
            block_event.wait(timeout=1.0)
            return ReconciliationResult(status=ReconciliationStatus.SUCCESS)

        coord.reconciler.reconcile = slow_reconcile

        # Launch trigger 1
        coord.trigger_reconciliation(reason="t1")
        time.sleep(0.05)

        # While trigger 1 is running, launch trigger 2 and 3
        coord.trigger_reconciliation(reason="t2")
        coord.trigger_reconciliation(reason="t3")

        # Unblock trigger 1
        block_event.set()
        time.sleep(0.15)

        coord.shutdown(timeout=2.0)

        # Exactly 2 execution cycles should have run (initial + 1 coalesced rerun)
        self.assertEqual(execution_counts[0], 2)

    def test_morning_briefing_and_reconciliation_do_not_block_each_other(self):
        """17. Morning briefing worker and reconciliation worker run independently."""
        coord = EventCoordinator(
            self.app,
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )

        briefing_ran = threading.Event()
        recon_ran = threading.Event()

        def fake_briefing():
            time.sleep(0.05)
            briefing_ran.set()

        def fake_recon(*args, **kwargs):
            time.sleep(0.05)
            recon_ran.set()
            return ReconciliationResult(status=ReconciliationStatus.SUCCESS)

        coord.reconciler.reconcile = fake_recon

        t_brief = threading.Thread(target=fake_briefing)
        t_brief.start()

        coord.trigger_reconciliation(reason="test")

        self.assertTrue(briefing_ran.wait(timeout=1.0))
        self.assertTrue(recon_ran.wait(timeout=1.0))
        coord.shutdown(timeout=1.0)


# ---------------------------------------------------------------------------
# Test Suite 6: GUI Shutdown & Late-Callback Safety
# ---------------------------------------------------------------------------

class TestShutdownAndLateCallbackSafety(unittest.TestCase):
    """Test Suite 6: Bounded Shutdown, Late-Result Discard, Resource Ordering."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _seed_test_schema(self.db_path)
        self.store_path = os.path.join(self.temp_dir.name, "event_watermarks.json")
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")

        self.db_patch = patch("brain.memory.DB_PATH", self.db_path)
        self.db_path_patch = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patch = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        self.db_patch.start()
        self.db_path_patch.start()
        self.settings_patch.start()

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
        self._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or self.store_path
            target_db = db_path or self.db_path
            self._orig_coord_init(coord_self, app, bridge=bridge, db_path=target_db, watermark_store_path=store_path, **kwargs)
        self.coord_patch = patch.object(EventCoordinator, "__init__", _isolated_coord_init)
        self.coord_patch.start()

        main.ensure_initialized()
        ctk.set_appearance_mode("light")

        with patch("brain.scheduler.start"):
            self.app = OrangeAIApp()
            self.app.withdraw()

    def tearDown(self):
        try:
            self.app._on_close()
        except Exception:
            pass
        self.coord_patch.stop()
        self.db_patch.stop()
        self.db_path_patch.stop()
        self.settings_patch.stop()
        main.job_manager = None
        main.job_store = None
        main.job_runtime = None
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_shutdown_during_reconciliation_is_bounded(self):
        """18. Closing GUI during active reconciliation terminates within bounded timeout."""
        block_event = threading.Event()

        def hanging_reconcile(*args, **kwargs):
            block_event.wait(timeout=5.0)
            return ReconciliationResult(status=ReconciliationStatus.SUCCESS)

        self.app._event_coordinator.reconciler.reconcile = hanging_reconcile
        self.app._event_coordinator.trigger_reconciliation(reason="hanging")
        time.sleep(0.05)

        start_t = time.time()
        self.app._on_close()
        elapsed = time.time() - start_t

        # Shutdown should have finished within bounded window (~2.0-3.0s), NOT 5s
        self.assertLess(elapsed, 4.0)
        block_event.set()

    def test_late_worker_results_discarded_after_shutdown(self):
        """19. Late delivery attempts after shutdown are marked AMBIGUOUS and not recorded."""
        candidate = EventCandidate(
            event_id="evt_late_19",
            event_type="reminder_due",
            entity_id="19",
            occurred_at="2026-10-31T12:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.HIGH,
            title="Late reminder",
            material_data={"reminder_id": 19, "message": "Late reminder"},
            source="reminders",
        )

        adapter = self.app._event_coordinator.delivery_adapter

        # Close the app
        self.app._on_close()

        # Attempt late delivery
        res = adapter.deliver(candidate)
        self.assertEqual(res.status, DeliveryStatus.AMBIGUOUS)

    def test_assistant_bridge_dispatch_with_ack_timeout(self):
        """20. dispatch_with_ack on closed bridge returns failure cleanly."""
        bridge = self.app._assistant_bridge
        bridge.shutdown()

        success, res, err = bridge.dispatch_with_ack(lambda: True, timeout=0.5)
        self.assertFalse(success)
        self.assertIn("closed", (err or "").lower())

    def test_scheduling_after_without_acknowledgement_does_not_count_as_success(self):
        """21. Scheduling after() without confirmed execution does not count as SUCCESS."""
        app = MagicMock()
        app._is_shutting_down = False
        app._assistant_bridge = None
        app.after = MagicMock()  # schedules after but does not invoke callback

        adapter = ChatViewDeliveryAdapter(app, bridge=None, timeout=0.2)
        candidate = EventCandidate(
            event_id="evt_no_ack",
            event_type="reminder_due",
            entity_id="99",
            occurred_at="2026-10-31T12:00:00Z",
            detected_at="2026-10-31T12:00:00Z",
            priority=EventPriority.HIGH,
            title="Unacknowledged",
            material_data={"reminder_id": 99, "message": "Unacknowledged"},
            source="reminders",
        )

        res = adapter.deliver(candidate)
        self.assertEqual(res.status, DeliveryStatus.AMBIGUOUS)

    def test_ambiguous_delivery_remains_retryable(self):
        """22. Candidate with AMBIGUOUS status is not recorded in store and retried in next cycle."""
        EventWatermarkStore.initialize_first_run(
            self.store_path,
            initial_watermarks={WATERMARK_JOBS_COMPLETED_AT: "2026-10-31T08:00:00Z"},
        )

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at, final_response, source) "
            "VALUES ('job-ambig', 'Sync drive', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T09:30:00Z', '2026-10-31T09:30:00Z', 'Done', 'interactive')"
        )
        conn.commit()
        conn.close()

        mock_adapter = MagicMock()
        # First attempt ambiguous, second attempt success
        mock_adapter.deliver.side_effect = [
            DeliveryResult(event_id="dummy", status=DeliveryStatus.AMBIGUOUS, error="UI dispatch timeout"),
            DeliveryResult(event_id="dummy", status=DeliveryStatus.SUCCESS, delivered_at="2026-10-31T10:00:00Z"),
        ]

        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
            delivery_adapter=mock_adapter,
        )

        # Cycle 1: Ambiguous -> 0 delivered, 1 ambiguous
        res1 = reconciler.reconcile()
        self.assertEqual(res1.ambiguous_count, 1)
        self.assertEqual(res1.delivered_count, 0)

        # Cycle 2: Candidate is retried and delivered
        res2 = reconciler.reconcile()
        self.assertEqual(res2.delivered_count, 1)
        self.assertEqual(mock_adapter.deliver.call_count, 2)

    def test_scheduler_check_once_behavior_unchanged(self):
        """23. Scheduler check_once fires reminders and jobs without regression."""
        from brain import scheduler

        fired_reminders = []
        fired_jobs = []

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) "
            "VALUES (501, 'Test Reminder', '2020-01-01T00:00:00', '2020-01-01T00:00:00', 0)"
        )
        conn.commit()
        conn.close()

        with patch("brain.reminders.DB_PATH", self.db_path):
            scheduler.check_once(
                on_reminder_due=lambda r: fired_reminders.append(r),
                on_job_event=lambda j: fired_jobs.append(j),
                job_manager=None,
                async_job=False,
            )

        self.assertEqual(len(fired_reminders), 1)
        self.assertEqual(fired_reminders[0]["id"], 501)

        # Database record should be marked fired
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT fired FROM reminders WHERE id = 501")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_memory_and_scheduler_close_ordering(self):
        """24. _on_close stops scheduler and coordinator before closing database resources."""
        order = []

        with patch("brain.scheduler.stop", side_effect=lambda **kw: order.append("scheduler_stop")), \
             patch.object(self.app._event_coordinator, "shutdown", side_effect=lambda **kw: order.append("coordinator_shutdown")), \
             patch("brain.memory.close", side_effect=lambda: order.append("memory_close")):
            self.app._on_close()

        self.assertIn("coordinator_shutdown", order)
        self.assertIn("scheduler_stop", order)
        self.assertIn("memory_close", order)
        self.assertLess(order.index("coordinator_shutdown"), order.index("memory_close"))
        self.assertLess(order.index("scheduler_stop"), order.index("memory_close"))


class TestStage37E3C4ReliabilityCorrections(unittest.TestCase):
    """
    Test Suite 7: Stage 3.7E.3C.4 Reliability Corrections.
    A5: Confirmed reminder delivery, fired-state contract, shutdown/timeout handling, duplicate prevention.
    B4: Elimination of direct delivery race, event reservation locks, single authoritative worker, retryability.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _seed_test_schema(self.db_path)
        self.store_path = os.path.join(self.temp_dir.name, "event_watermarks.json")
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")

        self.db_patch = patch("brain.memory.DB_PATH", self.db_path)
        self.rem_patch = patch("brain.reminders.DB_PATH", self.db_path)
        self.db_path_patch = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patch = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        self.db_patch.start()
        self.rem_patch.start()
        self.db_path_patch.start()
        self.settings_patch.start()

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
        self._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or self.store_path
            target_db = db_path or self.db_path
            self._orig_coord_init(coord_self, app, bridge=bridge, db_path=target_db, watermark_store_path=store_path, **kwargs)
        self.coord_patch = patch.object(EventCoordinator, "__init__", _isolated_coord_init)
        self.coord_patch.start()

        import brain.reminders
        brain.reminders.close()

        main.ensure_initialized()
        ctk.set_appearance_mode("light")

        with patch("brain.scheduler.start"):
            self.app = OrangeAIApp()
            self.app.withdraw()

    def tearDown(self):
        try:
            self.app._on_close()
        except Exception:
            pass
        self.coord_patch.stop()
        import brain.reminders
        brain.reminders.close()
        self.db_patch.stop()
        self.rem_patch.stop()
        self.db_path_patch.stop()
        self.settings_patch.stop()
        main.job_manager = None
        main.job_store = None
        main.job_runtime = None
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _insert_reminder(self, rem_id: int, message: str, due_at: str, fired: int = 0):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) VALUES (?, ?, ?, ?, ?)",
            (rem_id, message, due_at, due_at, fired),
        )
        conn.commit()
        conn.close()

    def _get_reminder_fired(self, rem_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT fired FROM reminders WHERE id = ?", (rem_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else -1

    def test_failed_ui_presentation_does_not_mark_reminder_fired(self):
        """25. Failed UI presentation does not mark reminder fired in SQLite and leaves watermark unrecorded."""
        self._insert_reminder(601, "Review security patches", "2026-10-31T09:00:00", fired=0)

        adapter = ChatViewDeliveryAdapter(self.app, bridge=self.app._assistant_bridge)
        candidate = EventCandidate(
            event_id="reminder_due:601:hash",
            event_type="reminder_due",
            entity_id="601",
            occurred_at="2026-10-31T09:00:00",
            detected_at="2026-10-31T09:05:00",
            priority=EventPriority.HIGH,
            title="Reminder due: Review security patches",
            evidence=["Test reminder"],
            material_data={"reminder_id": 601, "message": "Review security patches"},
            source="reminders",
        )

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(False, None, "UI presentation crashed")):
            res = adapter.deliver(candidate)

        self.assertEqual(res.status, DeliveryStatus.FAILURE)
        # Authoritative SQLite state must remain unfired
        self.assertEqual(self._get_reminder_fired(601), 0)

    def test_ambiguous_acknowledgement_timeout_does_not_mark_reminder_fired(self):
        """26. UI acknowledgement timeout (>5s) does not mark reminder fired in SQLite and leaves event retryable."""
        self._insert_reminder(602, "Call contractor", "2026-10-31T09:00:00", fired=0)

        adapter = ChatViewDeliveryAdapter(self.app, bridge=self.app._assistant_bridge)
        candidate = EventCandidate(
            event_id="reminder_due:602:hash",
            event_type="reminder_due",
            entity_id="602",
            occurred_at="2026-10-31T09:00:00",
            detected_at="2026-10-31T09:05:00",
            priority=EventPriority.HIGH,
            title="Reminder due: Call contractor",
            evidence=["Test reminder"],
            material_data={"reminder_id": 602, "message": "Call contractor"},
            source="reminders",
        )

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(False, None, "dispatch timed out after 5.0s")):
            res = adapter.deliver(candidate)

        self.assertEqual(res.status, DeliveryStatus.AMBIGUOUS)
        self.assertEqual(self._get_reminder_fired(602), 0)

    def test_gui_shutdown_during_delivery_does_not_mark_reminder_fired(self):
        """27. GUI shutdown during delivery returns AMBIGUOUS and does not mark reminder fired."""
        self._insert_reminder(603, "Update client roster", "2026-10-31T09:00:00", fired=0)

        adapter = ChatViewDeliveryAdapter(self.app, bridge=self.app._assistant_bridge)
        candidate = EventCandidate(
            event_id="reminder_due:603:hash",
            event_type="reminder_due",
            entity_id="603",
            occurred_at="2026-10-31T09:00:00",
            detected_at="2026-10-31T09:05:00",
            priority=EventPriority.HIGH,
            title="Reminder due: Update client roster",
            evidence=["Test reminder"],
            material_data={"reminder_id": 603, "message": "Update client roster"},
            source="reminders",
        )

        self.app._is_shutting_down = True
        res = adapter.deliver(candidate)

        self.assertEqual(res.status, DeliveryStatus.AMBIGUOUS)
        self.assertEqual(self._get_reminder_fired(603), 0)

    def test_confirmed_ui_presentation_marks_reminder_fired_and_records_watermark(self):
        """28. Confirmed UI presentation updates SQLite fired=1 and records delivery in watermark store."""
        self._insert_reminder(604, "Deploy production release", "2026-10-31T09:00:00", fired=0)

        adapter = ChatViewDeliveryAdapter(self.app, bridge=self.app._assistant_bridge)
        candidate = EventCandidate(
            event_id="reminder_due:604:hash",
            event_type="reminder_due",
            entity_id="604",
            occurred_at="2026-10-31T09:00:00",
            detected_at="2026-10-31T09:05:00",
            priority=EventPriority.HIGH,
            title="Reminder due: Deploy production release",
            evidence=["Test reminder"],
            material_data={"reminder_id": 604, "message": "Deploy production release"},
            source="reminders",
        )

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = adapter.deliver(candidate)

        self.assertEqual(res.status, DeliveryStatus.SUCCESS)
        # SQLite state must be marked fired after confirmed presentation
        self.assertEqual(self._get_reminder_fired(604), 1)

    def test_startup_recovery_delivers_unfired_reminder_and_prevents_scheduler_duplicate(self):
        """29. Startup recovery delivers unfired reminder, marks fired, and prevents later scheduler double-firing."""
        past_iso = (datetime.now() - timedelta(minutes=10)).isoformat()
        self._insert_reminder(605, "Review sprint metrics", past_iso, fired=0)

        # Baseline watermark store
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path)
        reconciler = StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.store_path,
        )

        delivered_messages = []
        class MockSuccessAdapter(ChatViewDeliveryAdapter):
            def deliver(self, candidate):
                res = super().deliver(candidate)
                delivered_messages.append(candidate)
                return res

        test_adapter = MockSuccessAdapter(self.app, bridge=self.app._assistant_bridge)

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = reconciler.reconcile(adapter=test_adapter)

        self.assertEqual(res.status, ReconciliationStatus.SUCCESS)
        self.assertEqual(len(delivered_messages), 1)
        self.assertEqual(delivered_messages[0].entity_id, "605")
        self.assertEqual(self._get_reminder_fired(605), 1)

        # Subsequent scheduler check must find 0 due reminders (duplicate eliminated)
        fired_in_scheduler = []
        from brain import scheduler
        scheduler.check_once(
            on_reminder_due=lambda r: fired_in_scheduler.append(r),
            job_manager=None,
            auto_mark_fired=False,
        )
        self.assertEqual(len(fired_in_scheduler), 0, "Scheduler must not double-fire recovered reminder!")

    def test_scheduler_callbacks_act_as_signals_only_without_direct_bypass(self):
        """30. Scheduler callbacks act as signals only to EventCoordinator without competing UI bypasses."""
        reminder = {"id": 606, "message": "Sign payroll", "due_at": "2026-10-31T10:00:00"}
        job = Job(id="job-sig-1", goal="Run sync", status=JobStatus.COMPLETED, final_response="Done")

        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger, \
             patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            rem_res = self.app._on_scheduler_reminder_due(reminder)
            self.assertFalse(rem_res)
            mock_trigger.assert_called_with(reason="reminder_due")
            mock_dispatch.assert_not_called()

            self.app._on_scheduler_job_event(job)
            mock_trigger.assert_called_with(reason="job_event")
            mock_dispatch.assert_not_called()

    def test_event_coordinator_in_flight_reservation_prevents_concurrent_duplicate(self):
        """31. EventCoordinator reservation lock claims event, suppresses concurrent peers, and releases in finally."""
        coord = self.app._event_coordinator
        evt_id = "test:unique_event:123"

        # Claim 1: Acquired
        self.assertTrue(coord.reserve_event(evt_id))
        # Claim 2: Suppressed (in flight)
        self.assertFalse(coord.reserve_event(evt_id))

        # Release
        coord.release_event(evt_id)

        # Once delivered in session/store, reservation also returns False (suppressed)
        coord._delivered_in_session.add(evt_id)
        self.assertFalse(coord.reserve_event(evt_id))

    def test_scheduler_auto_mark_fired_parameter_backward_compatibility(self):
        """32. Scheduler auto_mark_fired flag and callback return contract maintain backward compatibility."""
        from brain import scheduler
        past_iso = (datetime.now() - timedelta(minutes=5)).isoformat()

        # 1. Callback returns False -> do not mark fired even if auto_mark_fired=True
        self._insert_reminder(701, "Legacy test 1", past_iso, fired=0)
        scheduler.check_once(
            on_reminder_due=lambda r: False,
            job_manager=None,
            auto_mark_fired=True,
        )
        self.assertEqual(self._get_reminder_fired(701), 0)

        # 2. Callback returns None (legacy handler) and auto_mark_fired=True -> legacy auto mark fired
        self._insert_reminder(702, "Legacy test 2", past_iso, fired=0)
        scheduler.check_once(
            on_reminder_due=lambda r: None,
            job_manager=None,
            auto_mark_fired=True,
        )
        self.assertEqual(self._get_reminder_fired(702), 1)

        # 3. auto_mark_fired=False -> does not mark fired even if callback returns None
        self._insert_reminder(703, "Legacy test 3", past_iso, fired=0)
        scheduler.check_once(
            on_reminder_due=lambda r: None,
            job_manager=None,
            auto_mark_fired=False,
        )
        self.assertEqual(self._get_reminder_fired(703), 0)


if __name__ == "__main__":
    unittest.main()
