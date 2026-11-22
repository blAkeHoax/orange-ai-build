"""
test_gui_scheduler_briefing.py - Test Suite for GUI Scheduler Lifecycle & Morning Briefing Parity.

Stage 3.7E.1: Validates GUI scheduler initialization, lifecycle shutdown, thread-safe callbacks,
and proactive morning briefing delivery through the GUI conversation architecture.
Strictly isolated: runs on temporary SQLite databases and isolated settings files.
"""

import os
import sys
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
from gui.services.event_coordinator import EventCoordinator
from brain.jobs.models import Job, JobStatus
from brain.agent_models import AgentAction, ActionType
from brain.briefing.settings import (
    is_morning_briefing_enabled,
    set_morning_briefing_enabled,
    get_last_briefing_date,
    set_last_briefing_date,
    should_deliver_morning_briefing,
)
import main
import brain.mood as mood
from brain.briefing.settings import set_preferred_briefing_time

_module_temp_dir: Optional[tempfile.TemporaryDirectory] = None
_orig_mood_path: Optional[str] = None


def setUpModule():
    """Module-level setup guaranteeing brain.mood.DB_PATH is isolated before any GUI tests run."""
    global _module_temp_dir, _orig_mood_path
    _module_temp_dir = tempfile.TemporaryDirectory()
    isolated_db = os.path.join(_module_temp_dir.name, "isolated_mood.db")
    _orig_mood_path = getattr(mood, "_override_db_path", None)
    mood.DB_PATH = isolated_db


def tearDownModule():
    """Module-level teardown reliably restoring brain.mood.DB_PATH after all GUI tests finish."""
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


class TestGUISchedulerLifecycle(unittest.TestCase):
    """Test Suite 1: GUI Scheduler Startup, Idempotency, and Bounded Shutdown."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _init_test_db(self.db_path)
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.watermark_path = os.path.join(self.temp_dir.name, "event_watermarks.json")

        self.db_patcher = patch("brain.memory.DB_PATH", self.db_path)
        self.db_paths_patcher = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
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
        set_preferred_briefing_time("00:00")
        ctk.set_appearance_mode("light")

    def tearDown(self):
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

    def test_gui_initialization_starts_scheduler_once_with_callbacks(self):
        """1. GUI startup starts scheduler exactly once with proper callbacks and job_manager."""
        with patch("brain.scheduler.start") as mock_sched_start, \
             patch("brain.scheduler.stop") as mock_sched_stop:

            app = OrangeAIApp()
            app.withdraw()

            try:
                # Scheduler should have been started once
                self.assertEqual(mock_sched_start.call_count, 1)
                kwargs = mock_sched_start.call_args[1]
                self.assertIsNotNone(kwargs.get("on_reminder_due"))
                self.assertIsNotNone(kwargs.get("on_job_event"))
                self.assertIsNotNone(kwargs.get("job_manager"))
                self.assertTrue(app._scheduler_started)

                # Repeated call to _start_scheduler is a no-op
                app._start_scheduler()
                self.assertEqual(mock_sched_start.call_count, 1)
            finally:
                app._on_close()
                self.assertEqual(mock_sched_stop.call_count, 1)
                self.assertFalse(app._scheduler_started)

    def test_gui_shutdown_is_idempotent(self):
        """2. Repeated calls to _on_close do not raise exceptions."""
        with patch("brain.scheduler.start"), patch("brain.scheduler.stop") as mock_sched_stop:
            app = OrangeAIApp()
            app.withdraw()

            # First close
            app._on_close()
            self.assertEqual(mock_sched_stop.call_count, 1)

            # Second close
            app._on_close()
            # Stop should not be called again since _scheduler_started was reset
            self.assertEqual(mock_sched_stop.call_count, 1)

    def test_scheduler_startup_failure_does_not_corrupt_gui_init(self):
        """3. Scheduler startup exception is caught and does not prevent GUI initialization."""
        with patch("brain.scheduler.start", side_effect=RuntimeError("Scheduler thread conflict")):
            app = OrangeAIApp()
            app.withdraw()
            try:
                self.assertFalse(app._scheduler_started)
                # App components still initialized cleanly
                self.assertIsNotNone(app._workspace_container)
                self.assertIsNotNone(app._assistant_bridge)
            finally:
                app._on_close()


class TestGUICallbackThreadSafety(unittest.TestCase):
    """Test Suite 2: Background Thread-Safety and Notification Marshalling."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _init_test_db(self.db_path)
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.watermark_path = os.path.join(self.temp_dir.name, "event_watermarks.json")

        self.db_patcher = patch("brain.memory.DB_PATH", self.db_path)
        self.db_paths_patcher = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
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
        set_preferred_briefing_time("00:00")
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

    def test_reminder_callback_marshals_to_ui_safely(self):
        """4. Scheduler reminder callback acts as signal to EventCoordinator (or falls back safely if absent)."""
        reminder = {"id": 101, "message": "Prepare team report", "due_at": "2026-10-30T17:00:00"}

        # 1. Primary path: EventCoordinator is present -> acts as signal only
        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger, \
             patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            res = self.app._on_scheduler_reminder_due(reminder)
            self.assertFalse(res)
            mock_trigger.assert_called_once_with(reason="reminder_due")
            mock_dispatch.assert_not_called()

        # 2. Fallback path: EventCoordinator is absent -> marshals safely to UI
        orig_coord = self.app._event_coordinator
        try:
            self.app._event_coordinator = None
            with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
                res = self.app._on_scheduler_reminder_due(reminder)
                self.assertTrue(res)
                mock_dispatch.assert_called_once()
                target_func = mock_dispatch.call_args[0][0]
                notification_text = mock_dispatch.call_args[0][1]
                msg_type = mock_dispatch.call_args[0][2]

                self.assertEqual(target_func, self.app._deliver_notification_to_chat)
                self.assertIn("#101", notification_text)
                self.assertIn("Prepare team report", notification_text)
                self.assertEqual(msg_type, "reminder")
        finally:
            self.app._event_coordinator = orig_coord

    def test_job_confirmation_callback_marshals_to_ui_safely(self):
        """5. Job WAITING_CONFIRMATION callback acts as signal to EventCoordinator (or falls back safely)."""
        job = Job(id="job-uuid-1", goal="Prune temporary build cache", status=JobStatus.WAITING_CONFIRMATION)
        action = AgentAction(type=ActionType.CALL_TOOL, tool_name="delete_folder", arguments={"path": "/tmp"})
        job.set_pending_confirmation(action)

        # 1. Primary path: EventCoordinator is present -> acts as signal only
        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger, \
             patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            self.app._on_scheduler_job_event(job)
            mock_trigger.assert_called_once_with(reason="job_event")
            mock_dispatch.assert_not_called()

        # 2. Fallback path: EventCoordinator is absent -> marshals safely to UI
        orig_coord = self.app._event_coordinator
        try:
            self.app._event_coordinator = None
            with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
                self.app._on_scheduler_job_event(job)
                mock_dispatch.assert_called_once()
                notification_text = mock_dispatch.call_args[0][1]
                self.assertIn("Action 'delete_folder'", notification_text)
                self.assertIn("job confirm", notification_text)
                self.assertEqual(mock_dispatch.call_args[0][2], "job")
        finally:
            self.app._event_coordinator = orig_coord

    def test_job_completed_and_failed_callbacks(self):
        """6. Completed and Failed job events trigger reconciliation (or format fallback alerts)."""
        job_done = Job(id="job-2", goal="Train model", status=JobStatus.COMPLETED, final_response="Model v1 ready")
        job_fail = Job(id="job-3", goal="Sync drive", status=JobStatus.FAILED, last_error="Network timeout")
        job_running = Job(id="job-4", goal="Run tests", status=JobStatus.RUNNING)

        # 1. Primary path: EventCoordinator is present -> triggers reconciliation for completed/failed
        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_trigger, \
             patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            self.app._on_scheduler_job_event(job_done)
            self.assertEqual(mock_trigger.call_count, 1)
            mock_dispatch.assert_not_called()

            self.app._on_scheduler_job_event(job_fail)
            self.assertEqual(mock_trigger.call_count, 2)
            mock_dispatch.assert_not_called()

        # 2. Fallback path: EventCoordinator is absent
        orig_coord = self.app._event_coordinator
        try:
            self.app._event_coordinator = None
            with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
                self.app._on_scheduler_job_event(job_done)
                self.assertEqual(mock_dispatch.call_count, 1)
                self.assertIn("Completed: Model v1 ready", mock_dispatch.call_args[0][1])

                self.app._on_scheduler_job_event(job_fail)
                self.assertEqual(mock_dispatch.call_count, 2)
                self.assertIn("Failed: Network timeout", mock_dispatch.call_args[0][1])

                self.app._on_scheduler_job_event(job_running)
                self.assertEqual(mock_dispatch.call_count, 2)
        finally:
            self.app._event_coordinator = orig_coord

    def test_callback_after_gui_shutdown_is_safely_ignored(self):
        """7. Callbacks arriving after bridge shutdown are dropped without errors."""
        self.app._assistant_bridge.shutdown()

        with patch.object(self.app._assistant_bridge, "_dispatch_to_ui") as mock_dispatch:
            # Invoking reminder callback after close
            self.app._on_scheduler_reminder_due({"id": 1, "message": "Late reminder"})
            mock_dispatch.assert_not_called()

            # Invoking job callback after close
            job = Job(id="job-9", goal="Late job", status=JobStatus.COMPLETED)
            self.app._on_scheduler_job_event(job)
            mock_dispatch.assert_not_called()


class TestGUIMorningBriefingDelivery(unittest.TestCase):
    """Test Suite 3: Morning Briefing Startup Delivery in GUI."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _init_test_db(self.db_path)
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.watermark_path = os.path.join(self.temp_dir.name, "event_watermarks.json")

        self.db_patcher = patch("brain.memory.DB_PATH", self.db_path)
        self.db_paths_patcher = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.settings_file)

        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
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
        set_preferred_briefing_time("00:00")
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

    def test_morning_briefing_delivered_when_enabled(self):
        """8. Enabled morning briefing delivers formatted markdown through GUI conversation bridge."""
        set_morning_briefing_enabled(True)
        today_str = datetime.now().strftime("%Y-%m-%d")

        with patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            # Simulate deliver_morning_briefing_if_needed calling output_fn with briefing text
            def fake_deliver(client=None, manager=None, output_fn=None, **kwargs):
                if output_fn:
                    output_fn("\n🌅 Orange AI [Morning Briefing]:\n\n# 🌅 Daily Briefing\nAll systems nominal.\n--------------------------------------------------")
                return True

            mock_deliver.side_effect = fake_deliver

            with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)) as mock_ack:
                self.app._check_morning_briefing()

                # Wait briefly for worker thread to execute
                time.sleep(0.15)

                mock_deliver.assert_called_once()
                mock_ack.assert_called_once()

    def test_morning_briefing_not_delivered_when_disabled(self):
        """9. When morning briefing is disabled, no worker thread or deliver call occurs."""
        set_morning_briefing_enabled(False)

        with patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            self.app._check_morning_briefing()
            time.sleep(0.05)
            mock_deliver.assert_not_called()

    def test_morning_briefing_not_repeated_when_already_delivered_today(self):
        """10. When briefing was already delivered today, delivery is deduplicated and not repeated."""
        set_morning_briefing_enabled(True)
        today_str = datetime.now().strftime("%Y-%m-%d")
        set_last_briefing_date(today_str)

        with patch("main.deliver_morning_briefing_if_needed") as mock_deliver:
            self.app._check_morning_briefing()
            time.sleep(0.05)
            mock_deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
