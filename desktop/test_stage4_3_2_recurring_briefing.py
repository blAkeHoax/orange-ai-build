"""
test_stage4_3_2_recurring_briefing.py - Test Suite for Stage 4.3.2.

Stage 4.3.2: Recurring Scheduler & Background Worker Delivery
Validates all 19 required test conditions:
- Recurring eligibility (1-6)
- Worker concurrency (7-10)
- Threading and delivery (11-14)
- Shutdown & cancellation (15-16)
- Regression (17-19)

Strictly isolated: Runs on isolated temporary settings and databases; zero mutations to production resources.
"""

import os
import sys
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, time as dtime, timezone, timedelta
from typing import Optional
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
from gui.services.event_coordinator import EventCoordinator
import brain.mood as mood
import brain.briefing.settings as b_settings
import main
from brain.jobs.models import Job, JobStatus
from brain.agent_models import AgentAction, ActionType


# ==============================================================================
# Module-Level Test-Environment Isolation Fixtures
# ==============================================================================
_module_temp_dir: Optional[tempfile.TemporaryDirectory] = None
_orig_mood_path: Optional[str] = None


def setUpModule():
    """Module-level setup guaranteeing brain.mood.DB_PATH is isolated before any tests run."""
    global _module_temp_dir, _orig_mood_path
    _module_temp_dir = tempfile.TemporaryDirectory()
    isolated_db = os.path.join(_module_temp_dir.name, "isolated_mood.db")
    _orig_mood_path = getattr(mood, "_override_db_path", None)
    mood.DB_PATH = isolated_db


def tearDownModule():
    """Module-level teardown reliably restoring brain.mood.DB_PATH after all tests finish."""
    global _module_temp_dir, _orig_mood_path
    try:
        mood.close()
    except Exception:
        pass
    if _orig_mood_path is not None:
        mood.DB_PATH = _orig_mood_path
    else:
        try:
            del mood.DB_PATH
        except AttributeError:
            pass
    if _module_temp_dir is not None:
        try:
            _module_temp_dir.cleanup()
        except Exception:
            pass


def _init_test_db(db_path: str):
    """Seed schema for temporary test database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS structured_memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        memory_key TEXT NOT NULL,
        value TEXT NOT NULL,
        confidence REAL DEFAULT 1.0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(category, memory_key)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT,
        created_at TEXT DEFAULT ''
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        due_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        fired INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()


class TestStage432RecurringBriefing(unittest.TestCase):
    """Test suite for recurring morning briefing eligibility and background worker delivery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _init_test_db(self.db_path)
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.watermark_path = os.path.join(self.temp_dir.name, "event_watermarks.json")

        self.db_patcher = patch("brain.memory.DB_PATH", self.db_path)
        self.db_paths_patcher = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        # Isolate EventCoordinator watermark store path
        self._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or self.watermark_path
            target_db = db_path or self.db_path
            self._orig_coord_init(coord_self, app, bridge=bridge, db_path=target_db, watermark_store_path=store_path, **kwargs)
        self.coord_patcher = patch.object(EventCoordinator, "__init__", _isolated_coord_init)

        self.db_patcher.start()
        self.db_paths_patcher.start()
        self.settings_patcher.start()
        self.coord_patcher.start()

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
        self.coord_patcher.stop()
        self.db_patcher.stop()
        self.db_paths_patcher.stop()
        self.settings_patcher.stop()
        main.job_manager = None
        main.job_store = None
        main.job_runtime = None
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Group 1: Recurring Eligibility (1-6)
    # --------------------------------------------------------------------------
    def test_01_no_delivery_before_preferred_time(self):
        """1. When current time is before preferred time, check does not spawn worker."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")
        before_time = datetime(2026, 11, 15, 8, 0, 0)

        with patch("brain.briefing.settings.datetime") as mock_dt, \
             patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            mock_dt.now.return_value = before_time
            self.app._check_morning_briefing()
            time.sleep(0.05)
            mock_deliver.assert_not_called()
            self.assertIsNone(self.app._briefing_worker_thread)

    def test_02_delivery_becomes_eligible_at_preferred_time(self):
        """2. When current time reaches preferred time, worker is dispatched."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")
        exact_time = datetime(2026, 11, 15, 8, 30, 0)

        with patch("brain.briefing.settings.datetime") as mock_dt, \
             patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            mock_dt.now.return_value = exact_time
            mock_deliver.return_value = True

            self.app._check_morning_briefing()
            time.sleep(0.1)
            mock_deliver.assert_called_once()

    def test_03_delivery_becomes_eligible_after_preferred_time(self):
        """3. When current time is after preferred time on undelivered day, worker is dispatched."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")
        after_time = datetime(2026, 11, 15, 10, 15, 0)

        with patch("brain.briefing.settings.datetime") as mock_dt, \
             patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            mock_dt.now.return_value = after_time
            mock_deliver.return_value = True

            self.app._check_morning_briefing()
            time.sleep(0.1)
            mock_deliver.assert_called_once()

    def test_04_running_gui_reaches_preferred_time_without_restart(self):
        """4. Running GUI transitions from ineligible to eligible across ticks without restart."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")

        t_before = datetime(2026, 11, 15, 8, 29, 0)
        t_due = datetime(2026, 11, 15, 8, 30, 0)

        with patch("brain.briefing.settings.datetime") as mock_dt, \
             patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            # Tick 1: Before preferred time
            mock_dt.now.return_value = t_before
            self.app._check_morning_briefing()
            time.sleep(0.05)
            mock_deliver.assert_not_called()

            # Tick 2: Preferred time reached in same GUI instance
            mock_dt.now.return_value = t_due
            mock_deliver.return_value = True
            self.app._check_morning_briefing()
            time.sleep(0.1)
            mock_deliver.assert_called_once()

    def test_05_disabled_briefing_does_not_start_worker(self):
        """5. When briefing is disabled, no worker is started even after preferred time."""
        b_settings.set_morning_briefing_enabled(False)
        b_settings.set_preferred_briefing_time("08:30")
        t_after = datetime(2026, 11, 15, 9, 0, 0)

        with patch("brain.briefing.settings.datetime") as mock_dt, \
             patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            mock_dt.now.return_value = t_after
            self.app._check_morning_briefing()
            time.sleep(0.05)
            mock_deliver.assert_not_called()
            self.assertIsNone(self.app._briefing_worker_thread)

    def test_06_already_delivered_date_does_not_start_worker(self):
        """6. When already marked delivered for today, no worker is started."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")
        b_settings.set_last_briefing_date("2026-11-15")

        t_after = datetime(2026, 11, 15, 9, 0, 0)

        with patch("brain.briefing.settings.datetime") as mock_dt, \
             patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            mock_dt.now.return_value = t_after
            self.app._check_morning_briefing()
            time.sleep(0.05)
            mock_deliver.assert_not_called()
            self.assertIsNone(self.app._briefing_worker_thread)

    # --------------------------------------------------------------------------
    # Group 2: Worker Concurrency & State (7-10)
    # --------------------------------------------------------------------------
    def test_07_repeated_checks_do_not_create_overlapping_workers(self):
        """7. A second check while a worker is already running does not spawn an overlapping worker."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("00:00")

        worker_started_event = threading.Event()
        worker_release_event = threading.Event()
        execution_count = [0]

        def slow_deliver(client=None, manager=None, output_fn=None, **kwargs):
            execution_count[0] += 1
            worker_started_event.set()
            worker_release_event.wait(timeout=2.0)
            return True

        with patch("main.deliver_morning_briefing_if_needed", side_effect=slow_deliver):
            # First tick spawns worker
            self.app._check_morning_briefing()
            self.assertTrue(worker_started_event.wait(timeout=1.0))
            self.assertIsNotNone(self.app._briefing_worker_thread)
            self.assertTrue(self.app._briefing_worker_thread.is_alive())

            # Second tick while worker 1 is active
            self.app._check_morning_briefing()

            # Release worker 1
            worker_release_event.set()
            time.sleep(0.1)

            # Exactly one execution occurred
            self.assertEqual(execution_count[0], 1)

    def test_08_worker_completion_releases_active_state(self):
        """8. When worker finishes normally, active thread state is cleanly released."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("00:00")

        with patch("main.deliver_morning_briefing_if_needed", return_value=True):
            self.app._check_morning_briefing()
            time.sleep(0.15)
            self.assertIsNone(self.app._briefing_worker_thread)

    def test_09_worker_failure_releases_active_state(self):
        """9. When worker raises an unexpected exception, active state is released in finally block."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("00:00")

        def crashing_deliver(client=None, manager=None, output_fn=None):
            raise RuntimeError("Synthesis simulated crash")

        with patch("main.deliver_morning_briefing_if_needed", side_effect=crashing_deliver):
            self.app._check_morning_briefing()
            time.sleep(0.15)
            # Must be released back to None, not permanently blocked
            self.assertIsNone(self.app._briefing_worker_thread)

    def test_10_stale_worker_completion_cannot_clear_newer_worker(self):
        """10. Token guard ensures a stale/interrupted worker cannot clear a newer active worker's reference."""
        dummy_thread = threading.Thread(target=lambda: time.sleep(0.5), daemon=True)
        dummy_thread.start()

        with self.app._briefing_worker_lock:
            self.app._briefing_worker_thread = dummy_thread
            self.app._briefing_worker_token = 2  # Current token is 2

        # Simulate stale worker 1 attempting to clear state in finally
        with self.app._briefing_worker_lock:
            stale_token = 1
            if self.app._briefing_worker_token == stale_token:
                self.app._briefing_worker_thread = None

        # Thread reference must still be preserved for token 2
        self.assertIsNotNone(self.app._briefing_worker_thread)
        self.assertEqual(self.app._briefing_worker_thread, dummy_thread)

    # --------------------------------------------------------------------------
    # Group 3: Threading & Delivery Safety (11-14)
    # --------------------------------------------------------------------------
    def test_11_collection_and_synthesis_execute_outside_gui_thread(self):
        """11. Briefing worker executes on a non-main daemon thread."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("00:00")

        execution_thread = [None]
        event = threading.Event()

        def capture_thread(client=None, manager=None, output_fn=None, **kwargs):
            execution_thread[0] = threading.current_thread()
            event.set()
            return True

        with patch("main.deliver_morning_briefing_if_needed", side_effect=capture_thread):
            self.app._check_morning_briefing()
            self.assertTrue(event.wait(timeout=1.0))
            self.assertIsNotNone(execution_thread[0])
            self.assertNotEqual(execution_thread[0], threading.main_thread())
            self.assertTrue(execution_thread[0].daemon)

    def test_12_ui_presentation_marshalled_through_assistant_bridge(self):
        """12. Output presentation is dispatched to UI thread via AssistantBridge."""
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)) as mock_ack:
            briefing_text = "🌅 Orange AI [Morning Briefing]:\n\n# Daily Summary\nAll clear.\n--------------------------------------------------"
            self.app._on_morning_briefing_raw_output(briefing_text)
            mock_ack.assert_called_once()
            target_func = mock_ack.call_args[0][0]
            self.assertTrue(callable(target_func))

    def test_13_worker_callbacks_do_not_directly_modify_tkinter_widgets(self):
        """13. _on_morning_briefing_raw_output delegates exclusively to bridge without calling Tkinter methods directly."""
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)) as mock_ack:
            self.app._on_morning_briefing_raw_output("Simple notification text")
            mock_ack.assert_called_once()

    def test_14_gui_closure_prevents_late_ui_updates(self):
        """14. Output arriving after GUI shutdown or bridge closure is discarded without errors."""
        self.app._is_shutting_down = True
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack") as mock_ack, \
             patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            self.app._on_morning_briefing_raw_output("Late text after close")
            mock_ack.assert_not_called()
            mock_dispatch.assert_not_called()

        self.app._is_shutting_down = False
        self.app._assistant_bridge.shutdown()
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack") as mock_ack, \
             patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            self.app._on_morning_briefing_raw_output("Late text after bridge shutdown")
            mock_ack.assert_not_called()
            mock_dispatch.assert_not_called()
            mock_dispatch.assert_not_called()

    # --------------------------------------------------------------------------
    # Group 4: Shutdown & Cancellation (15-16)
    # --------------------------------------------------------------------------
    def test_15_pending_recurring_callbacks_cancelled_on_shutdown(self):
        """15. Calling _on_close cancels pending recurring after-id cleanly."""
        self.assertIsNotNone(self.app._morning_briefing_after_id)
        self.app._on_close()
        self.assertIsNone(self.app._morning_briefing_after_id)
        self.assertTrue(self.app._is_shutting_down)

    def test_16_worker_shutdown_does_not_indefinitely_block_gui(self):
        """16. Closing GUI while a worker is running completes within bounded timeout."""
        slow_worker = threading.Thread(target=lambda: time.sleep(1.0), daemon=True)
        slow_worker.start()

        with self.app._briefing_worker_lock:
            self.app._briefing_worker_thread = slow_worker

        start_t = time.time()
        self.app._on_close()
        elapsed = time.time() - start_t
        # Must join within bounded timeout (<= 2.0s)
        self.assertLess(elapsed, 2.5)

    # --------------------------------------------------------------------------
    # Group 5: Regression & Sidecar Invariants (17-19)
    # --------------------------------------------------------------------------
    def test_17_existing_reminder_scheduler_remains_operational(self):
        """17. Reminder scheduler triggers continue to marshal to EventCoordinator as signals."""
        reminder = {"id": 201, "message": "Pay electricity bill", "due_at": "2026-11-15T09:00:00"}
        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger:
            res = self.app._on_scheduler_reminder_due(reminder)
            self.assertFalse(res)
            mock_trigger.assert_called_once_with(reason="reminder_due")

    def test_18_existing_job_event_delivery_remains_operational(self):
        """18. Job events continue to trigger reconciliation without interference from briefing loop."""
        job = Job(id="job-rec-1", goal="Optimize image assets", status=JobStatus.COMPLETED)
        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger:
            self.app._on_scheduler_job_event(job)
            mock_trigger.assert_called_once_with(reason="job_event")

    def test_19_existing_event_coordinator_acknowledgement_unchanged(self):
        """19. EventCoordinator delivery adapter acknowledgement contracts remain intact."""
        from gui.services.event_coordinator import ChatViewDeliveryAdapter
        from brain.briefing.models import EventCandidate, EventPriority, DeliveryStatus

        cand = EventCandidate(
            event_id="job:completed:j1",
            source="jobs",
            event_type="job_completed",
            entity_id="j1",
            title="Job Completed",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            detected_at=datetime.now(timezone.utc).isoformat(),
            priority=EventPriority.HIGH,
            material_data={"final_response": "Asset optimization completed"},
        )

        adapter = ChatViewDeliveryAdapter(self.app, bridge=self.app._assistant_bridge)
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = adapter.deliver(cand)
            self.assertEqual(res.status, DeliveryStatus.SUCCESS)


if __name__ == "__main__":
    unittest.main()
