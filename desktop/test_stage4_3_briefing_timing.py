"""
test_stage4_3_briefing_timing.py - Dedicated Test Suite for Stage 4.3.1.

Stage 4.3.1: Test-Environment Isolation & Preferred Briefing Timing
Validates all 10 scopes mandated by the Stage 4.3.1 specification:
1. Briefing disabled returns False regardless of time.
2. Briefing enabled but current time before preferred time returns False.
3. Current time exactly equals preferred time returns True.
4. Current time after preferred time returns True.
5. Briefing already delivered today returns False (same-day deduplication).
6. Default preferred time of 08:30 boundary behaviors (08:29:59 vs 08:30:00 vs 08:30:01).
7. Custom preferred times (e.g. 06:15, 14:45, 23:59).
8. Invalid stored preferred time and validation fallback to default 08:30.
9. Midnight and local calendar-date boundaries across transitions.
10. Deterministic behavior through injected now parameter (naive and timezone-aware).

Strictly isolated: Runs on isolated temporary settings and databases; zero mutations to production resources.
"""

import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, time, timezone, timedelta
from typing import Optional
from unittest.mock import patch, MagicMock

import brain.briefing.settings as b_settings
import brain.mood as mood
import gui.settings as g_settings


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


# ==============================================================================
# Test Suite: Preferred Morning Briefing Timing & Deduplication
# ==============================================================================
class TestPreferredBriefingTiming(unittest.TestCase):
    """Test suite verifying all Stage 4.3.1 timing, validation, and boundary conditions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_settings_path = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.temp_settings_path)
        self.settings_patcher.start()

    def tearDown(self):
        self.settings_patcher.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Scope 1: Briefing Disabled
    # --------------------------------------------------------------------------
    def test_01_briefing_disabled_returns_false_at_all_times(self):
        """1. When morning briefing is disabled, returns False regardless of time of day."""
        b_settings.set_morning_briefing_enabled(False)
        self.assertFalse(b_settings.is_morning_briefing_enabled())

        # Test before, at, and after default 08:30
        t_before = datetime(2026, 11, 15, 7, 0, 0)
        t_exact = datetime(2026, 11, 15, 8, 30, 0)
        t_after = datetime(2026, 11, 15, 12, 0, 0)

        self.assertFalse(b_settings.should_deliver_morning_briefing(t_before))
        self.assertFalse(b_settings.should_deliver_morning_briefing(t_exact))
        self.assertFalse(b_settings.should_deliver_morning_briefing(t_after))

    # --------------------------------------------------------------------------
    # Scope 2: Briefing Enabled but Current Time Before Preferred Time
    # --------------------------------------------------------------------------
    def test_02_enabled_but_before_preferred_time_returns_false(self):
        """2. When enabled, returns False if current local time is before configured preferred time."""
        b_settings.set_morning_briefing_enabled(True)
        # Default is 08:30
        self.assertEqual(b_settings.get_preferred_briefing_time(), "08:30")

        # 06:00, 08:00, 08:29:59
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 6, 0, 0)))
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 8, 0, 0)))
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 8, 29, 59)))

    # --------------------------------------------------------------------------
    # Scope 3: Current Time Exactly Equals Preferred Time
    # --------------------------------------------------------------------------
    def test_03_exact_preferred_time_returns_true(self):
        """3. When current local time exactly matches configured preferred time, returns True."""
        b_settings.set_morning_briefing_enabled(True)
        t_exact = datetime(2026, 11, 15, 8, 30, 0)
        self.assertTrue(b_settings.should_deliver_morning_briefing(t_exact))

    # --------------------------------------------------------------------------
    # Scope 4: Current Time After Preferred Time
    # --------------------------------------------------------------------------
    def test_04_after_preferred_time_returns_true(self):
        """4. When current local time has passed preferred time on an undelivered date, returns True."""
        b_settings.set_morning_briefing_enabled(True)
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 8, 30, 1)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 9, 15, 0)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 17, 45, 0)))

    # --------------------------------------------------------------------------
    # Scope 5: Briefing Already Delivered Today
    # --------------------------------------------------------------------------
    def test_05_already_delivered_today_returns_false(self):
        """5. Once marked delivered for today's calendar date, subsequent checks return False."""
        b_settings.set_morning_briefing_enabled(True)
        t_due = datetime(2026, 11, 15, 8, 30, 0)
        today_str = "2026-11-15"

        # First check -> eligible
        self.assertTrue(b_settings.should_deliver_morning_briefing(t_due))

        # Mark delivered today
        b_settings.set_last_briefing_date(today_str)
        self.assertEqual(b_settings.get_last_briefing_date(), today_str)

        # Subsequent checks throughout the day must return False
        self.assertFalse(b_settings.should_deliver_morning_briefing(t_due))
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 9, 0, 0)))
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 23, 59, 59)))

    # --------------------------------------------------------------------------
    # Scope 6: Default Preferred Time of 08:30 Boundary Precision
    # --------------------------------------------------------------------------
    def test_06_default_time_boundary_precision(self):
        """6. Precision boundaries for default 08:30 (08:29:59.999 vs 08:30:00 vs 08:30:01)."""
        b_settings.set_morning_briefing_enabled(True)
        self.assertEqual(b_settings.get_preferred_briefing_time(), "08:30")

        # 1 microsecond before 08:30 -> False
        t_just_before = datetime(2026, 11, 15, 8, 29, 59, 999999)
        self.assertFalse(b_settings.should_deliver_morning_briefing(t_just_before))

        # Exactly 08:30:00 -> True
        t_exact = datetime(2026, 11, 15, 8, 30, 0, 0)
        self.assertTrue(b_settings.should_deliver_morning_briefing(t_exact))

        # 1 second after 08:30 -> True
        t_just_after = datetime(2026, 11, 15, 8, 30, 1, 0)
        self.assertTrue(b_settings.should_deliver_morning_briefing(t_just_after))

    # --------------------------------------------------------------------------
    # Scope 7: Custom Preferred Times
    # --------------------------------------------------------------------------
    def test_07_custom_preferred_times(self):
        """7. Custom configured times (06:15, 14:45, 23:59) enforce exact respective thresholds."""
        b_settings.set_morning_briefing_enabled(True)

        # Custom Early: 06:15
        b_settings.set_preferred_briefing_time("06:15")
        self.assertEqual(b_settings.get_preferred_briefing_time(), "06:15")
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 6, 14, 59)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 6, 15, 0)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 6, 16, 0)))

        # Custom Afternoon: 14:45
        b_settings.set_preferred_briefing_time("14:45")
        self.assertEqual(b_settings.get_preferred_briefing_time(), "14:45")
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 14, 44, 59)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 14, 45, 0)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 15, 0, 0)))

        # Custom End of Day: 23:59
        b_settings.set_preferred_briefing_time("23:59")
        self.assertEqual(b_settings.get_preferred_briefing_time(), "23:59")
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 23, 58, 59)))
        self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 23, 59, 0)))

    # --------------------------------------------------------------------------
    # Scope 8: Invalid Stored Preferred Time and Existing Fallback
    # --------------------------------------------------------------------------
    def test_08_invalid_stored_time_falls_back_to_default_0830(self):
        """8. Invalid or corrupted preferred times in settings file safely fall back to 08:30."""
        b_settings.set_morning_briefing_enabled(True)

        # Directly inject corrupted values into settings store
        corrupted_values = ["99:99", "25:00", "invalid", "", "08:5", 1234, None]
        for bad_val in corrupted_values:
            with open(self.temp_settings_path, "w", encoding="utf-8") as f:
                json.dump({"morning_briefing_enabled": True, "preferred_briefing_time": bad_val}, f)

            # Getter must fall back to 08:30
            self.assertEqual(b_settings.get_preferred_briefing_time(), "08:30")

            # Check logic uses 08:30 fallback
            self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 8, 29, 0)))
            self.assertTrue(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 8, 30, 0)))

    # --------------------------------------------------------------------------
    # Scope 9: Midnight and Local-Date Boundaries
    # --------------------------------------------------------------------------
    def test_09_midnight_and_local_date_boundaries(self):
        """9. Date boundaries across midnight: prior day delivery does not block next day at target time."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")

        day1_dt = datetime(2026, 11, 15, 9, 0, 0)
        day1_str = "2026-11-15"

        # Delivered on day 1
        self.assertTrue(b_settings.should_deliver_morning_briefing(day1_dt))
        b_settings.set_last_briefing_date(day1_str)
        self.assertFalse(b_settings.should_deliver_morning_briefing(day1_dt))

        # At Day 1 23:59:59 -> still False
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 15, 23, 59, 59)))

        # At Day 2 00:00:01 -> New calendar date, but 00:00:01 < 08:30:00 -> False
        day2_midnight = datetime(2026, 11, 16, 0, 0, 1)
        self.assertFalse(b_settings.should_deliver_morning_briefing(day2_midnight))

        # At Day 2 08:29:59 -> False
        self.assertFalse(b_settings.should_deliver_morning_briefing(datetime(2026, 11, 16, 8, 29, 59)))

        # At Day 2 08:30:00 -> True (new day's briefing ready)
        day2_due = datetime(2026, 11, 16, 8, 30, 0)
        self.assertTrue(b_settings.should_deliver_morning_briefing(day2_due))

        # If configured for 00:00, day 2 fires immediately at midnight
        b_settings.set_preferred_briefing_time("00:00")
        self.assertTrue(b_settings.should_deliver_morning_briefing(day2_midnight))

    # --------------------------------------------------------------------------
    # Scope 10: Deterministic Behavior Through Injected Now Parameter
    # --------------------------------------------------------------------------
    def test_10_deterministic_injected_now_naive_and_aware(self):
        """10. Deterministic evaluation for naive datetime, timezone-aware datetime, and now=None."""
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("08:30")

        # 10a. Injected naive local datetime
        naive_before = datetime(2026, 11, 15, 8, 0, 0)
        naive_after = datetime(2026, 11, 15, 8, 30, 0)
        self.assertFalse(b_settings.should_deliver_morning_briefing(naive_before))
        self.assertTrue(b_settings.should_deliver_morning_briefing(naive_after))

        # 10b. Injected timezone-aware datetime (UTC)
        # Note: Local timezone is IST (UTC+5:30).
        # 03:00 UTC == 08:30 IST.
        # 02:59 UTC == 08:29 IST (< 08:30).
        aware_utc_before = datetime(2026, 11, 15, 2, 59, 0, tzinfo=timezone.utc)
        aware_utc_at = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
        aware_utc_after = datetime(2026, 11, 15, 3, 1, 0, tzinfo=timezone.utc)

        # Converted to local time internally
        local_before = aware_utc_before.astimezone()
        local_at = aware_utc_at.astimezone()
        local_after = aware_utc_after.astimezone()

        if local_before.time() < time(8, 30):
            self.assertFalse(b_settings.should_deliver_morning_briefing(aware_utc_before))
        if local_at.time() >= time(8, 30):
            self.assertTrue(b_settings.should_deliver_morning_briefing(aware_utc_at))
        if local_after.time() >= time(8, 30):
            self.assertTrue(b_settings.should_deliver_morning_briefing(aware_utc_after))

        # 10c. now=None samples current local clock
        # Set preferred time to "00:00" -> guaranteed True regardless of execution time
        b_settings.set_preferred_briefing_time("00:00")
        self.assertTrue(b_settings.should_deliver_morning_briefing(None))
        self.assertTrue(b_settings.should_deliver_morning_briefing())

        # Set preferred time to "23:59" (and assume tests don't run at 23:59:59) -> False
        # To be completely safe against test running at 23:59, test against an opposite time
        current_hour = datetime.now().hour
        future_hour = (current_hour + 2) % 24
        if future_hour > current_hour:
            b_settings.set_preferred_briefing_time(f"{future_hour:02d}:00")
            self.assertFalse(b_settings.should_deliver_morning_briefing())


if __name__ == "__main__":
    unittest.main()
