"""
test_briefing_cli.py - Comprehensive Test Suite for Daily Briefing CLI & Delivery.

Stage 3.7C: Validates CLI command recognition, intent routing, process_user_turn dispatch,
user-controlled morning briefing preference, deduplication, and safe non-blocking delivery.
Strictly isolated: runs on temporary SQLite databases and isolated settings files.
"""

import os
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from brain.intents import Intent
from brain.router import route
from brain.briefing.settings import (
    is_morning_briefing_enabled,
    set_morning_briefing_enabled,
    get_last_briefing_date,
    set_last_briefing_date,
    should_deliver_morning_briefing,
)
import main


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


class TestBriefingCLIRouting(unittest.TestCase):
    """Test router intent recognition for briefing commands."""

    def test_briefing_command_recognition(self):
        """1. 'briefing' routes to Intent.DAILY_BRIEFING."""
        self.assertEqual(route("briefing"), Intent.DAILY_BRIEFING)
        self.assertEqual(route("Briefing"), Intent.DAILY_BRIEFING)
        self.assertEqual(route("  briefing  "), Intent.DAILY_BRIEFING)

    def test_daily_briefing_command_recognition(self):
        """2. 'daily briefing' and variants route to Intent.DAILY_BRIEFING."""
        self.assertEqual(route("daily briefing"), Intent.DAILY_BRIEFING)
        self.assertEqual(route("Daily Briefing"), Intent.DAILY_BRIEFING)
        self.assertEqual(route("show briefing"), Intent.DAILY_BRIEFING)
        self.assertEqual(route("get briefing"), Intent.DAILY_BRIEFING)
        self.assertEqual(route("today's briefing"), Intent.DAILY_BRIEFING)

    def test_unrelated_phrases_not_intercepted(self):
        """3. 'good morning' and conversational text remain Intent.CHAT."""
        self.assertEqual(route("good morning"), Intent.CHAT)
        self.assertEqual(route("good morning Orange"), Intent.CHAT)
        self.assertEqual(route("can you prepare a briefing about space?"), Intent.CHAT)
        self.assertEqual(route("remember briefing tomorrow"), Intent.REMEMBER)


class TestBriefingDeliveryAndSettings(unittest.TestCase):
    """Test user-controlled morning briefing preferences and deduplication."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.patcher = patch("gui.settings.get_settings_path", return_value=self.settings_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_morning_preference_default_and_toggle(self):
        """4. Morning briefing preference defaults to False and toggles cleanly."""
        self.assertFalse(is_morning_briefing_enabled())

        set_morning_briefing_enabled(True)
        self.assertTrue(is_morning_briefing_enabled())

        set_morning_briefing_enabled(False)
        self.assertFalse(is_morning_briefing_enabled())

    def test_deduplication_and_delivery_check(self):
        """5. should_deliver_morning_briefing returns True once per day, then False."""
        now = datetime(2026, 10, 30, 9, 0, 0)
        today_str = "2026-10-30"

        # Disabled by default -> False
        self.assertFalse(should_deliver_morning_briefing(now))

        # Enable
        set_morning_briefing_enabled(True)

        # First launch of the day -> True
        self.assertTrue(should_deliver_morning_briefing(now))

        # Deliver and mark date
        set_last_briefing_date(today_str)
        self.assertEqual(get_last_briefing_date(), today_str)

        # Subsequent launch on the same day -> False (deduplicated)
        self.assertFalse(should_deliver_morning_briefing(now))

        # Next day -> True
        tomorrow = now + timedelta(days=1)
        self.assertTrue(should_deliver_morning_briefing(tomorrow))


class TestBriefingDispatchInMain(unittest.TestCase):
    """Test process_user_turn dispatch of briefing intent and configuration commands."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_memory.db")
        _init_test_db(self.db_path)
        self.settings_file = os.path.join(self.temp_dir.name, "gui_settings.json")

        self.db_patcher = patch("brain.memory.DB_PATH", self.db_path)
        self.db_paths_patcher = patch("brain.paths.get_user_data_dir", return_value=self.temp_dir.name)
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.settings_file)
        self.recs_patcher = patch("brain.briefing.recommendations.get_recommendations_store_path", return_value=os.path.join(self.temp_dir.name, "recommendations.json"))

        self.db_patcher.start()
        self.db_paths_patcher.start()
        self.settings_patcher.start()
        self.recs_patcher.start()

        main.ensure_initialized()
        from brain.briefing.settings import set_preferred_briefing_time
        set_preferred_briefing_time("00:00")

    def tearDown(self):
        self.recs_patcher.stop()
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

    def test_briefing_enable_and_disable_commands(self):
        """6. 'briefing enable' and 'briefing disable' CLI commands work without API call."""
        res_enable = main.process_user_turn("briefing enable")
        self.assertEqual(res_enable["type"], "briefing")
        self.assertIn("Morning briefing enabled", res_enable["reply"])
        self.assertTrue(is_morning_briefing_enabled())

        res_status = main.process_user_turn("briefing status")
        self.assertIn("Morning Briefing: Enabled", res_status["reply"])

        res_disable = main.process_user_turn("briefing disable")
        self.assertEqual(res_disable["type"], "briefing")
        self.assertIn("Morning briefing disabled", res_disable["reply"])
        self.assertFalse(is_morning_briefing_enabled())

    def test_briefing_command_dispatch_and_fallback(self):
        """7. 'briefing' command dispatches BriefingCollector and BriefingSynthesizer cleanly."""
        res = main.process_user_turn("briefing")
        self.assertEqual(res["type"], "briefing")
        self.assertIn("Daily Briefing", res["reply"])
        # Should record today's date for deduplication
        today_str = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(get_last_briefing_date(), today_str)

    def test_daily_briefing_command_empty_context_safe(self):
        """8. 'daily briefing' on empty database generates graceful deterministic summary."""
        res = main.process_user_turn("daily briefing")
        self.assertEqual(res["type"], "briefing")
        self.assertIn("Primary Focus", res["reply"])
        self.assertIn("Suggested Next Steps", res["reply"])

    def test_zero_automatic_tool_execution(self):
        """9. Generating briefing never triggers tool execution or background jobs."""
        with patch("tools.registry.registry.execute") as mock_tool_exec:
            res = main.process_user_turn("briefing")
            self.assertEqual(res["type"], "briefing")
            mock_tool_exec.assert_not_called()

    def test_morning_delivery_disabled_does_nothing(self):
        """10. When disabled, deliver_morning_briefing_if_needed does not mark date or deliver."""
        set_morning_briefing_enabled(False)
        delivered = main.deliver_morning_briefing_if_needed()
        self.assertFalse(delivered)
        self.assertIsNone(get_last_briefing_date())

    def test_morning_delivery_successful_remote_synthesis_marks_date(self):
        """11. Successful remote synthesis marks today's date and delivers briefing."""
        set_morning_briefing_enabled(True)
        today_str = datetime.now().strftime("%Y-%m-%d")

        mock_api = MagicMock()
        mock_api.is_available.return_value = True
        mock_api.assist.return_value = {
            "reply": "Good morning! Here is today's overview.",
            "model": "gemini-3.5-flash",
            "success": True,
        }

        output_lines = []
        delivered = main.deliver_morning_briefing_if_needed(
            client=mock_api,
            output_fn=output_lines.append,
        )

        self.assertTrue(delivered)
        self.assertEqual(get_last_briefing_date(), today_str)
        self.assertTrue(any("Good morning!" in line for line in output_lines))

    def test_morning_delivery_successful_fallback_synthesis_marks_date(self):
        """12. Successful deterministic fallback marks today's date when API is offline."""
        set_morning_briefing_enabled(True)
        today_str = datetime.now().strftime("%Y-%m-%d")

        mock_api = MagicMock()
        mock_api.is_available.return_value = False

        output_lines = []
        delivered = main.deliver_morning_briefing_if_needed(
            client=mock_api,
            output_fn=output_lines.append,
        )

        self.assertTrue(delivered)
        self.assertEqual(get_last_briefing_date(), today_str)
        self.assertTrue(any("Daily Briefing" in line for line in output_lines))

    def test_morning_delivery_unexpected_synthesis_failure_leaves_date_unmarked_and_retries(self):
        """13. Unexpected synthesis crash leaves date unmarked, allowing retry on next startup."""
        set_morning_briefing_enabled(True)
        today_str = datetime.now().strftime("%Y-%m-%d")

        # First run: synthesis raises an unhandled error
        with patch("brain.briefing.BriefingSynthesizer.synthesize", side_effect=RuntimeError("Unexpected memory crash")):
            delivered = main.deliver_morning_briefing_if_needed()
            self.assertFalse(delivered)
            # Date must NOT be marked
            self.assertIsNone(get_last_briefing_date())
            # Still eligible for delivery today
            self.assertTrue(should_deliver_morning_briefing())

        # Second run (subsequent launch / retry): synthesis succeeds
        output_lines = []
        retry_delivered = main.deliver_morning_briefing_if_needed(output_fn=output_lines.append)
        self.assertTrue(retry_delivered)
        self.assertEqual(get_last_briefing_date(), today_str)
        # Now marked, subsequent check is deduplicated
        self.assertFalse(should_deliver_morning_briefing())

    def test_morning_delivery_output_error_retains_date_marker(self):
        """14. Date marker represents successful synthesis and is retained even if output print raises."""
        set_morning_briefing_enabled(True)
        today_str = datetime.now().strftime("%Y-%m-%d")

        def bad_output(text):
            raise IOError("Broken console pipe")

        delivered = main.deliver_morning_briefing_if_needed(output_fn=bad_output)
        self.assertTrue(delivered)
        # Date remains recorded because synthesis succeeded
        self.assertEqual(get_last_briefing_date(), today_str)


if __name__ == "__main__":
    unittest.main()
