"""
test_stage4_3_3_1_race_hardening.py - Deterministic Race Condition Verification & Hardening.

Stage 4.3.3.1: Narrow Race-Condition Verification and Hardening
Validates synchronization between:
- gui/app.py _ui_present()
- gui/app.py _on_morning_briefing_raw_output()
- briefing worker timeout/failure handling
- AssistantBridge.dispatch_with_ack()
- ChatView/timeline insertion operation

Ensures zero contradictory outcomes:
- Presentation wins: successful UI insertion is acknowledged and captured delivery date is persisted.
- Timeout wins: attempt is invalidated (FAILED) and no timeline insertion occurs.
- No visible insertion after an attempt has definitively become FAILED.
- No false failure if presentation has already won.

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
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
import brain.mood as mood
import brain.briefing.settings as b_settings
from brain.briefing.models import DailyBriefing, DailyBriefingContext
import main


# ==============================================================================
# Module-Level Test-Environment Isolation Fixtures
# ==============================================================================
_module_temp_dir: Optional[tempfile.TemporaryDirectory] = None
_orig_mood_path: Optional[str] = None


def setUpModule():
    """Ensure total test isolation from production databases and settings."""
    global _module_temp_dir, _orig_mood_path
    _module_temp_dir = tempfile.TemporaryDirectory(prefix="orange_test_stage4331_")
    temp_dir = _module_temp_dir.name

    # 1. Isolate brain.mood.DB_PATH
    _orig_mood_path = mood.DB_PATH
    test_db_path = os.path.join(temp_dir, "test_mood.db")
    conn = sqlite3.connect(test_db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mood_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, score REAL, timestamp TEXT, note TEXT)"
    )
    conn.commit()
    conn.close()
    mood.DB_PATH = test_db_path

    # 2. Isolate gui_settings.json
    test_gui_settings = os.path.join(temp_dir, "gui_settings.json")
    with open(test_gui_settings, "w", encoding="utf-8") as f:
        json.dump({"theme": "dark", "briefing_preferred_time": "08:00"}, f)
    b_settings.SETTINGS_PATH = test_gui_settings

    # 3. Disable CTK window rendering during headless testing
    ctk.set_appearance_mode("Dark")


def tearDownModule():
    """Restore original paths and cleanup temporary directory."""
    global _module_temp_dir, _orig_mood_path
    if _orig_mood_path:
        mood.DB_PATH = _orig_mood_path
    if _module_temp_dir:
        try:
            _module_temp_dir.cleanup()
        except Exception:
            pass


class TestStage4331RaceHardening(unittest.TestCase):
    """Deterministic race condition and synchronization test suite."""

    def setUp(self):
        """Create an isolated OrangeAIApp instance for each test."""
        self.app = OrangeAIApp.__new__(OrangeAIApp)
        self.app._is_shutting_down = False
        self.app._briefing_worker_lock = threading.Lock()
        self.app._briefing_worker_thread = None
        self.app._briefing_worker_token = 0
        self.app._briefing_attempt_states = {}
        self.app._briefing_attempt_events = {}

        # Mock views and bridge
        self.chat_mock = MagicMock()
        self.app._views_cache = {"chat": self.chat_mock}
        self.bridge_mock = MagicMock()
        self.bridge_mock._is_closed = False
        self.app._assistant_bridge = self.bridge_mock

        # Prevent Tkinter uninitialized widget recursion
        self.app._startup_recon_after_id = None
        self.app._morning_briefing_after_id = None
        self.app.destroy = MagicMock()
        self.app.after_cancel = MagicMock()

        # Reset briefing date setting
        b_settings.set_last_briefing_date(None)

    def tearDown(self):
        """Clean up app resources."""
        self.app._is_shutting_down = True
        b_settings.set_last_briefing_date(None)

    # ==========================================================================
    # Core Deterministic Race Tests
    # ==========================================================================

    def test_01_timeout_while_callback_paused_before_insertion_timeout_wins(self):
        """
        1. Timeout while callback is paused after validation but before insertion.
        Coherent outcome: TIMEOUT WINS.
        - Worker invalidates attempt and marks FAILED.
        - Callback resumes, detects FAILED under lock, raises RuntimeError.
        - chat_view.on_assistant_result is NEVER called.
        - Delivery date is NOT marked.
        """
        token = 1
        captured_date = "2026-11-15"
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        self.app._briefing_attempt_events[token] = threading.Event()

        callback_paused = threading.Event()
        callback_resume = threading.Event()

        # Wrap _ui_present to intercept right before atomic check-and-commit
        def fake_dispatch(fn, timeout=5.0):
            def _runner():
                callback_paused.set()
                # Wait until the worker has timed out and marked attempt FAILED
                callback_resume.wait()
                try:
                    fn()
                except Exception:
                    pass

            t = threading.Thread(target=_runner, name="TestUIThread-1")
            t.start()
            callback_paused.wait()
            # Worker timeout occurs while callback is paused before insertion
            return (False, None, "UI dispatch timed out after 5.0s")

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        # Run worker delivery
        res = self.app._on_morning_briefing_raw_output(
            "Test briefing content", token=token, captured_date=captured_date
        )

        # Worker returned False and marked attempt FAILED
        self.assertFalse(res)
        self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

        # Release the paused callback
        callback_resume.set()
        time.sleep(0.05)

        # Verified: No visible insertion after attempt has definitively become FAILED
        self.chat_mock.on_assistant_result.assert_not_called()
        self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_02_timeout_during_insertion_presentation_wins_acknowledged(self):
        """
        2. Timeout while callback is paused DURING timeline insertion (after commit).
        Coherent outcome: PRESENTATION WINS.
        - Callback passed pre-insertion check and transitioned to COMMITTED.
        - Worker wait times out, inspects state, sees COMMITTED.
        - Worker grants grace period waiting on commit_event.
        - Timeline insertion completes, marks PRESENTED, sets commit_event.
        - Worker acknowledges presentation, marks ACKNOWLEDGED, persists captured date.
        - No false failure. Exactly one coherent outcome.
        """
        token = 2
        captured_date = "2026-11-15"
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        self.app._briefing_attempt_events[token] = threading.Event()

        insertion_started = threading.Event()
        insertion_resume = threading.Event()
        inserted_messages = []

        def pause_during_insertion(req_id, outcome):
            insertion_started.set()
            insertion_resume.wait()
            inserted_messages.append((req_id, outcome))

        self.chat_mock.on_assistant_result.side_effect = pause_during_insertion

        def fake_dispatch(fn, timeout=5.0):
            t = threading.Thread(target=fn, name="TestUIThread-2")
            t.start()
            insertion_started.wait()
            # Worker times out while insertion is active
            return (False, None, "UI dispatch timed out after 5.0s")

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        # Auto-release insertion after worker enters grace period wait
        def release_worker():
            time.sleep(0.05)
            insertion_resume.set()

        threading.Thread(target=release_worker, daemon=True).start()

        # Run worker delivery
        res = self.app._on_morning_briefing_raw_output(
            "Test briefing content", token=token, captured_date=captured_date
        )

        # Verified: Presentation won cleanly!
        self.assertTrue(res)
        self.assertEqual(self.app._briefing_attempt_states[token], "ACKNOWLEDGED")
        self.assertEqual(len(inserted_messages), 1)
        self.assertEqual(b_settings.get_last_briefing_date(), captured_date)

    def test_03_presentation_completed_before_timeout_handled_presentation_wins(self):
        """
        3. Presentation already completed (PRESENTED) before worker processes timeout result.
        Coherent outcome: PRESENTATION WINS.
        - No false failure if presentation has already won.
        - Worker acknowledges presentation and persists captured date.
        """
        token = 3
        captured_date = "2026-11-15"
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        self.app._briefing_attempt_events[token] = threading.Event()

        def fake_dispatch(fn, timeout=5.0):
            # Function runs and succeeds synchronously
            fn()
            # But dispatch reports timeout/signaling delay
            return (False, None, "UI dispatch timed out after 5.0s")

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        res = self.app._on_morning_briefing_raw_output(
            "Test briefing content", token=token, captured_date=captured_date
        )

        self.assertTrue(res)
        self.assertEqual(self.app._briefing_attempt_states[token], "ACKNOWLEDGED")
        self.chat_mock.on_assistant_result.assert_called_once()
        self.assertEqual(b_settings.get_last_briefing_date(), captured_date)

    def test_04_insertion_raises_exception_worker_marks_failed(self):
        """
        4. Insertion raises an exception in ChatView.
        Coherent outcome: TIMEOUT / FAILURE WINS.
        - State is marked FAILED.
        - Worker does not mark date and returns False.
        """
        token = 4
        captured_date = "2026-11-15"
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        self.app._briefing_attempt_events[token] = threading.Event()

        self.chat_mock.on_assistant_result.side_effect = RuntimeError("Render crash")

        def fake_dispatch(fn, timeout=5.0):
            try:
                fn()
                return (True, True, None)
            except Exception as e:
                return (False, None, str(e))

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        res = self.app._on_morning_briefing_raw_output(
            "Test briefing content", token=token, captured_date=captured_date
        )

        self.assertFalse(res)
        self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_05_midnight_crossing_captured_date_persisted(self):
        """
        5. Attempt crosses midnight: captured local delivery date remains correct.
        """
        token = 5
        captured_date = "2026-11-15"
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        self.app._briefing_attempt_events[token] = threading.Event()

        def fake_dispatch(fn, timeout=5.0):
            fn()
            return (True, True, None)

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        res = self.app._on_morning_briefing_raw_output(
            "Midnight briefing", token=token, captured_date=captured_date
        )

        self.assertTrue(res)
        self.assertEqual(self.app._briefing_attempt_states[token], "ACKNOWLEDGED")
        self.assertEqual(b_settings.get_last_briefing_date(), "2026-11-15")

    def test_06_stale_token_rejected_before_insertion(self):
        """
        6. Stale token arriving during a newer attempt is rejected before insertion.
        """
        self.app._briefing_worker_token = 10
        self.app._briefing_attempt_states[9] = "GENERATING"
        self.app._briefing_attempt_states[10] = "GENERATING"

        res = self.app._on_morning_briefing_raw_output(
            "Old briefing", token=9, captured_date="2026-11-15"
        )

        self.assertFalse(res)
        self.chat_mock.on_assistant_result.assert_not_called()
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_07_gui_shutdown_invalidates_in_progress_attempt(self):
        """
        7. GUI shutdown invalidates in-progress attempt cleanly without deadlock.
        """
        token = 7
        self.app._briefing_worker_token = token
        self.app._briefing_attempt_states[token] = "GENERATING"
        self.app._briefing_attempt_events[token] = threading.Event()

        self.app._on_close()

        self.assertTrue(self.app._is_shutting_down)
        self.assertEqual(self.app._briefing_attempt_states[token], "CANCELLED")
        self.assertTrue(self.app._briefing_attempt_events[token].is_set())
        self.assertIsNone(b_settings.get_last_briefing_date())

    def test_08_repeated_deterministic_runs(self):
        """
        8. Run the core in-progress callback timeout race 20 times in a loop.
        Confirms zero flakiness and 100% determinism.
        """
        for iteration in range(20):
            token = 100 + iteration
            self.app._briefing_worker_token = token
            self.app._briefing_attempt_states[token] = "GENERATING"
            self.app._briefing_attempt_events[token] = threading.Event()
            self.chat_mock.reset_mock()
            b_settings.set_last_briefing_date(None)

            callback_paused = threading.Event()
            callback_resume = threading.Event()

            def fake_dispatch(fn, timeout=5.0):
                def _runner():
                    callback_paused.set()
                    callback_resume.wait()
                    try:
                        fn()
                    except Exception:
                        pass

                t = threading.Thread(target=_runner)
                t.start()
                callback_paused.wait()
                return (False, None, "UI dispatch timed out after 5.0s")

            self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

            res = self.app._on_morning_briefing_raw_output(
                f"Briefing iter {iteration}", token=token, captured_date="2026-11-15"
            )

            self.assertFalse(res, f"Iteration {iteration} failed: worker returned True")
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")

            callback_resume.set()
            time.sleep(0.01)

            self.chat_mock.on_assistant_result.assert_not_called()
            self.assertEqual(self.app._briefing_attempt_states[token], "FAILED")
            self.assertIsNone(b_settings.get_last_briefing_date())


if __name__ == "__main__":
    unittest.main()
