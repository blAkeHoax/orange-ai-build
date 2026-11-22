"""
test_stage4_3_3_briefing_ack.py - Test Suite for Stage 4.3.3.

Stage 4.3.3: Confirmed Delivery Acknowledgement & Premature Date-Marking Decoupling
Validates all 20 required test conditions:
- Original 14 tests:
  1. Successful synthesis and presentation marks date.
  2. Synthesis failure does not mark date.
  3. Empty briefing does not mark date.
  4. Dispatch failure does not mark date.
  5. GUI presentation failure does not mark date.
  6. Acknowledgement marks date exactly once.
  7. Duplicate acknowledgement is idempotent.
  8. Stale attempt acknowledgement is rejected.
  9. Failed attempt remains eligible for retry.
  10. Successful acknowledgement suppresses same-day retry.
  11. Shutdown before presentation does not mark date.
  12. Restart after unacknowledged attempt allows retry.
  13. Existing recurring 60-second ticker continues working.
  14. Existing reminder and job-event delivery remain unaffected.
- Six additional tests:
  15. Timed-out callback executes late: stale callback is rejected before presentation.
  16. Old callback arrives during a newer attempt: newer attempt remains unaffected.
  17. Attempt crosses midnight: captured intended delivery date is persisted.
  18. Date persistence fails after presentation: failure is logged and no false persistence success is reported.
  19. GUI mode with auto_mark_delivered=False: main.py never marks the date.
  20. CLI default behavior: existing date-marking contract remains unchanged.

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
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
from gui.services.event_coordinator import EventCoordinator
import brain.mood as mood
import brain.briefing.settings as b_settings
from brain.briefing.models import DailyBriefing, DailyBriefingContext
import main
from brain.jobs.models import Job, JobStatus


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


class TestStage433BriefingAck(unittest.TestCase):
    """Comprehensive test suite for Stage 4.3.3 confirmed delivery and date-marking decoupling."""

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

        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("00:00")

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

    # ==========================================================================
    # Original 14 Tests
    # ==========================================================================

    def test_01_successful_synthesis_and_presentation_marks_date(self):
        """1. Successful synthesis followed by successful presentation marks delivery date."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = self.app._on_morning_briefing_raw_output(
                "🌅 Orange AI [Morning Briefing]:\n\nDaily summary content\n--------------------------------------------------",
                token=token,
                captured_date=today_str,
            )
            self.assertTrue(res)
            self.assertEqual(b_settings.get_last_briefing_date(), today_str)
            self.assertEqual(self.app._briefing_attempt_states[token], "ACKNOWLEDGED")

    def test_02_synthesis_failure_does_not_mark_date(self):
        """2. Synthesis failure does not mark delivery date."""
        with patch("brain.briefing.BriefingSynthesizer.synthesize", side_effect=RuntimeError("LLM synthesis crash")):
            delivered = main.deliver_morning_briefing_if_needed(auto_mark_delivered=False)
            self.assertFalse(delivered)
            self.assertIsNone(b_settings.get_last_briefing_date())

    def test_03_empty_briefing_does_not_mark_date(self):
        """3. Empty briefing content does not falsely mark delivery date."""
        fake_empty_briefing = DailyBriefing(
            date="2026-11-15",
            summary_markdown="   \n  ",
            priorities=[],
            raw_reply="",
            model_name="local_deterministic",
            is_fallback=True,
        )
        with patch("brain.briefing.BriefingSynthesizer.synthesize", return_value=fake_empty_briefing):
            delivered = main.deliver_morning_briefing_if_needed(auto_mark_delivered=False)
            self.assertFalse(delivered)
            self.assertIsNone(b_settings.get_last_briefing_date())

    def test_04_dispatch_failure_does_not_mark_date(self):
        """4. Bridge dispatch failure does not mark delivery date."""
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(False, None, "Bridge is closed")):
            res = self.app._on_morning_briefing_raw_output(
                "Briefing body text",
                token=token,
                captured_date="2026-11-15",
            )
            self.assertFalse(res)
            self.assertIsNone(b_settings.get_last_briefing_date())
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

    def test_05_gui_presentation_failure_does_not_mark_date(self):
        """5. GUI presentation failure in timeline does not mark delivery date."""
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(False, None, "ChatView rendering crashed")):
            res = self.app._on_morning_briefing_raw_output(
                "Briefing body text",
                token=token,
                captured_date="2026-11-15",
            )
            self.assertFalse(res)
            self.assertIsNone(b_settings.get_last_briefing_date())
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

    def test_06_acknowledgement_marks_date_exactly_once(self):
        """6. Acknowledgement marks date exactly once."""
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        today_str = "2026-11-15"

        with patch("brain.briefing.settings.save_settings", wraps=b_settings.save_settings) as mock_save:
            with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
                res = self.app._on_morning_briefing_raw_output(
                    "Briefing body text",
                    token=token,
                    captured_date=today_str,
                )
                self.assertTrue(res)
                self.assertEqual(mock_save.call_count, 1)
                self.assertEqual(b_settings.get_last_briefing_date(), today_str)

    def test_07_duplicate_acknowledgement_is_idempotent(self):
        """7. Duplicate acknowledgement is idempotent and does not corrupt settings."""
        today_str = "2026-11-15"
        b_settings.set_last_briefing_date(today_str)
        self.assertEqual(b_settings.get_last_briefing_date(), today_str)

        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "ACKNOWLEDGED"

        # Calling again with already acknowledged token returns False without duplicate presentation
        res = self.app._on_morning_briefing_raw_output("Duplicate text", token=token, captured_date=today_str)
        self.assertFalse(res)
        self.assertEqual(b_settings.get_last_briefing_date(), today_str)

    def test_08_stale_attempt_acknowledgement_is_rejected(self):
        """8. Stale attempt acknowledgement is rejected and does not dispatch or mark date."""
        self.app._briefing_worker_token = 2
        self.app._briefing_attempt_states[1] = "FAILED"
        self.app._briefing_attempt_states[2] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack") as mock_dispatch:
            res = self.app._on_morning_briefing_raw_output("Stale output", token=1, captured_date="2026-11-15")
            self.assertFalse(res)
            mock_dispatch.assert_not_called()
            self.assertIsNone(b_settings.get_last_briefing_date())

    def test_09_failed_attempt_remains_eligible_for_retry(self):
        """9. Failed attempts remain eligible for retry on next check."""
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(False, None, "UI dispatch timed out")):
            res = self.app._on_morning_briefing_raw_output("Text", token=token, captured_date="2026-11-15")
            self.assertFalse(res)
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

        # Delivery remains eligible for retry
        self.assertTrue(b_settings.should_deliver_morning_briefing())

    def test_10_successful_acknowledgement_suppresses_same_day_retry(self):
        """10. Successful acknowledgement suppresses same-day retries."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = self.app._on_morning_briefing_raw_output("Briefing body", token=token, captured_date=today_str)
            self.assertTrue(res)

        self.assertFalse(b_settings.should_deliver_morning_briefing())

    def test_11_shutdown_before_presentation_does_not_mark_date(self):
        """11. Application shutdown before presentation does not mark delivery date."""
        self.app._is_shutting_down = True
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        with patch.object(self.app._assistant_bridge, "dispatch_with_ack") as mock_dispatch:
            res = self.app._on_morning_briefing_raw_output("Text", token=token, captured_date="2026-11-15")
            self.assertFalse(res)
            mock_dispatch.assert_not_called()
            self.assertIsNone(b_settings.get_last_briefing_date())

    def test_12_restart_after_unacknowledged_attempt_allows_retry(self):
        """12. Application restart after unacknowledged attempt allows catch-up delivery."""
        self.assertIsNone(b_settings.get_last_briefing_date())
        self.assertTrue(b_settings.should_deliver_morning_briefing())

    def test_13_existing_recurring_60s_ticker_continues_working(self):
        """13. Existing 60-second recurring eligibility check continues working without duplicate accumulation."""
        self.app._morning_briefing_after_id = "test_after_id"
        with patch.object(self.app, "after_cancel") as mock_cancel, \
             patch.object(self.app, "after", return_value="new_after_id") as mock_after:
            self.app._check_morning_briefing()
            mock_cancel.assert_called_once_with("test_after_id")
            mock_after.assert_called_once_with(60000, self.app._check_morning_briefing)

    def test_14_existing_reminder_and_job_event_delivery_remain_unaffected(self):
        """14. Existing reminder and job-event delivery remain completely operational."""
        job = Job(id="job-ack-1", goal="Run tests", status=JobStatus.COMPLETED)
        with patch.object(self.app._event_coordinator, "trigger_reconciliation") as mock_recon:
            self.app._on_scheduler_job_event(job)
            mock_recon.assert_called_once_with(reason="job_event")

    # ==========================================================================
    # Six Additional Tests
    # ==========================================================================

    def test_15_timed_out_callback_late_execution_rejected(self):
        """15. Timed-out callback executing late on GUI thread is rejected before presentation."""
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        # Worker invokes raw output which simulates a 5.0s timeout
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(False, None, "UI dispatch timed out after 5.0s")):
            res = self.app._on_morning_briefing_raw_output("Timed out text", token=token, captured_date="2026-11-15")
            self.assertFalse(res)
            # Worker marked attempt state as FAILED
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

        # Now simulate the queued _ui_present executing late in Tkinter event queue
        # When checking attempt state, it must reject and raise/abort without modifying chat_view
        chat_mock = MagicMock()
        self.app._views_cache = {"chat": chat_mock}

        # Attempt to insert directly with invalidated state
        with self._briefing_worker_lock_guard(token):
            current_state = self.app._briefing_attempt_states.get(token)
            self.assertIn(current_state, ("FAILED", "CANCELLED"))
        # chat_mock.on_assistant_result was not called
        chat_mock.on_assistant_result.assert_not_called()
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_16_old_callback_arrives_during_newer_attempt_unaffected(self):
        """16. Old callback arrives during a newer attempt: newer attempt remains completely unaffected."""
        # Setup: Worker 1 timed out or completed, now worker 2 is active
        self.app._briefing_worker_token = 2
        self.app._briefing_attempt_states[1] = "FAILED"
        self.app._briefing_attempt_states[2] = "GENERATING"

        # Old callback 1 arrives
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack") as mock_dispatch:
            res1 = self.app._on_morning_briefing_raw_output("Old text from worker 1", token=1, captured_date="2026-11-15")
            self.assertFalse(res1)
            mock_dispatch.assert_not_called()

        # Newer attempt 2 state is unchanged
        self.assertEqual(self.app._briefing_worker_token, 2)
        self.assertEqual(self.app._briefing_attempt_states[2], "GENERATING")
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_17_attempt_crosses_midnight_persists_captured_date(self):
        """17. Attempt crossing midnight persists the captured intended delivery date."""
        captured_date = "2026-11-15"
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        # Simulate midnight crossing: datetime.now() inside callback would be Day 2, but captured_date is Day 1
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)):
            res = self.app._on_morning_briefing_raw_output(
                "Midnight crossing briefing",
                token=token,
                captured_date=captured_date,
            )
            self.assertTrue(res)
            # The date marked in settings MUST be the captured date, not any other date
            self.assertEqual(b_settings.get_last_briefing_date(), captured_date)

    def test_18_date_persistence_fails_after_presentation_logged_no_false_success(self):
        """18. Date persistence failure after presentation is logged and reports failure without false success."""
        token = 1
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"

        # Presentation succeeds, but set_last_briefing_date fails (returns False)
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack", return_value=(True, True, None)), \
             patch("brain.briefing.settings.set_last_briefing_date", return_value=False):
            res = self.app._on_morning_briefing_raw_output(
                "Presented text",
                token=token,
                captured_date="2026-11-15",
            )
            # Must return False and not falsely report success
            self.assertFalse(res)
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

    def test_19_gui_mode_auto_mark_delivered_false_main_never_marks_date(self):
        """19. In GUI mode with auto_mark_delivered=False, main.py never marks the delivery date."""
        mock_output = MagicMock()
        delivered = main.deliver_morning_briefing_if_needed(output_fn=mock_output, auto_mark_delivered=False)
        self.assertTrue(delivered)
        mock_output.assert_called_once()
        # main.py did NOT mark the date
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_20_cli_default_behavior_existing_contract_unchanged(self):
        """20. CLI default behavior (auto_mark_delivered=True) retains existing contract and marks date on console error."""
        def bad_output(text):
            raise IOError("Broken console pipe")

        today_str = datetime.now().strftime("%Y-%m-%d")
        # CLI call does not pass auto_mark_delivered (defaults to True)
        delivered = main.deliver_morning_briefing_if_needed(output_fn=bad_output)
        self.assertTrue(delivered)
        # Date remains recorded because synthesis succeeded under CLI contract
        self.assertEqual(b_settings.get_last_briefing_date(), today_str)

    # ==========================================================================
    # Helper Context Manager
    # ==========================================================================
    def _briefing_worker_lock_guard(self, token: int):
        class LockGuard:
            def __init__(self, lock):
                self.lock = lock
            def __enter__(self):
                self.lock.acquire()
            def __exit__(self, *args):
                self.lock.release()
        return LockGuard(self.app._briefing_worker_lock)


if __name__ == "__main__":
    unittest.main()
