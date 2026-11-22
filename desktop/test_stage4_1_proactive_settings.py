"""
test_stage4_1_proactive_settings.py - Isolated Test Suite for Stage 4.1 Proactive Settings & User Controls.

Validates:
1. Morning briefing enable/disable persistence.
2. Preferred time validation and persistence (strict HH:MM 24h format).
3. Contextual advisory enable/disable behavior at delivery boundary.
4. Advisory cooldown choices (2, 4, 8) and persistence.
5. Defaults when settings file or keys are missing.
6. Recovery from malformed or unsupported values.
7. Persistence across settings reload / reinitialization.
8. Preservation of unrelated GUI settings (e.g. theme, api_url).
9. GUI control state matching effective backend settings.
10. Existing advisory behavior remaining intact when enabled.

Uses isolated temporary files and mocks. Never modifies production databases or JSON stores.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import brain.briefing.settings as b_settings
import gui.settings as g_settings
from main import _check_contextual_chat_recommendation


class TestStage41ProactiveSettings(unittest.TestCase):
    """Test suite verifying Stage 4.1 proactive settings and controls."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_settings_path = os.path.join(self.temp_dir.name, "gui_settings.json")
        self.settings_patcher = patch("gui.settings.get_settings_path", return_value=self.temp_settings_path)
        self.settings_patcher.start()

    def tearDown(self):
        self.settings_patcher.stop()
        self.temp_dir.cleanup()

    # 1. Morning briefing enable/disable persistence
    def test_01_morning_briefing_toggle_persistence(self):
        self.assertFalse(b_settings.is_morning_briefing_enabled())
        self.assertFalse(g_settings.get_morning_briefing_enabled())

        b_settings.set_morning_briefing_enabled(True)
        self.assertTrue(b_settings.is_morning_briefing_enabled())
        self.assertTrue(g_settings.get_morning_briefing_enabled())

        g_settings.set_morning_briefing_enabled(False)
        self.assertFalse(b_settings.is_morning_briefing_enabled())
        self.assertFalse(g_settings.get_morning_briefing_enabled())

    # 2. Preferred time validation and persistence
    def test_02_preferred_time_validation_and_persistence(self):
        # Default
        self.assertEqual(b_settings.get_preferred_briefing_time(), "08:30")
        self.assertEqual(g_settings.get_preferred_briefing_time(), "08:30")

        # Valid times
        valid_times = ["00:00", "07:15", "08:30", "12:00", "23:59"]
        for vt in valid_times:
            self.assertTrue(b_settings.set_preferred_briefing_time(vt), f"Expected {vt} to be accepted")
            self.assertEqual(b_settings.get_preferred_briefing_time(), vt)

        # Invalid times must be rejected
        invalid_times = ["24:00", "12:60", "8:30", "08:5", "abcd", "-01:00", "25:00", "", None, 1234]
        for inv in invalid_times:
            self.assertFalse(b_settings.set_preferred_briefing_time(inv), f"Expected {inv} to be rejected")
            # Remains at last valid
            self.assertEqual(b_settings.get_preferred_briefing_time(), "23:59")

    # 3. Contextual advisory enable/disable behavior
    def test_03_contextual_advisory_toggle_persistence(self):
        # Default is True
        self.assertTrue(b_settings.is_contextual_advisories_enabled())
        self.assertTrue(g_settings.get_contextual_advisories_enabled())

        b_settings.set_contextual_advisories_enabled(False)
        self.assertFalse(b_settings.is_contextual_advisories_enabled())
        self.assertFalse(g_settings.get_contextual_advisories_enabled())

        g_settings.set_contextual_advisories_enabled(True)
        self.assertTrue(b_settings.is_contextual_advisories_enabled())

    # 4. Advisory cooldown choices and persistence
    def test_04_advisory_cooldown_choices_and_persistence(self):
        # Default is 4
        self.assertEqual(b_settings.get_advisory_cooldown_hours(), 4)
        self.assertEqual(g_settings.get_advisory_cooldown_hours(), 4)

        # Supported choices: 2, 4, 8
        for hours in (2, 4, 8):
            self.assertTrue(b_settings.set_advisory_cooldown_hours(hours))
            self.assertEqual(b_settings.get_advisory_cooldown_hours(), hours)

        # Unsupported choices must be rejected
        for bad in (0, 1, 3, 5, 6, 12, 24, "four", -1):
            self.assertFalse(b_settings.set_advisory_cooldown_hours(bad))
            # Preserves last valid (8)
            self.assertEqual(b_settings.get_advisory_cooldown_hours(), 8)

    # 5. Defaults when settings are missing
    def test_05_defaults_when_settings_file_missing(self):
        if os.path.exists(self.temp_settings_path):
            os.remove(self.temp_settings_path)

        self.assertFalse(b_settings.is_morning_briefing_enabled())
        self.assertEqual(b_settings.get_preferred_briefing_time(), "08:30")
        self.assertTrue(b_settings.is_contextual_advisories_enabled())
        self.assertEqual(b_settings.get_advisory_cooldown_hours(), 4)

    # 6. Recovery from malformed or unsupported values
    def test_06_recovery_from_malformed_values(self):
        malformed_data = {
            "morning_briefing_enabled": "corrupt_string",
            "preferred_briefing_time": "99:99",
            "contextual_advisories_enabled": "invalid_bool",
            "advisory_cooldown_hours": 999,
        }
        with open(self.temp_settings_path, "w", encoding="utf-8") as f:
            json.dump(malformed_data, f)

        # String truthy conversion or safe fallback
        self.assertEqual(b_settings.get_preferred_briefing_time(), "08:30")
        self.assertEqual(b_settings.get_advisory_cooldown_hours(), 4)
        self.assertTrue(b_settings.is_contextual_advisories_enabled())

    # 7. Persistence across settings reload/reinitialization
    def test_07_persistence_across_settings_reload(self):
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("09:15")
        b_settings.set_contextual_advisories_enabled(False)
        b_settings.set_advisory_cooldown_hours(8)

        # Re-read raw JSON from disk
        with open(self.temp_settings_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        self.assertTrue(raw["morning_briefing_enabled"])
        self.assertEqual(raw["preferred_briefing_time"], "09:15")
        self.assertFalse(raw["contextual_advisories_enabled"])
        self.assertEqual(raw["advisory_cooldown_hours"], 8)

    # 8. Preservation of unrelated GUI settings
    def test_08_preservation_of_unrelated_settings(self):
        initial = {
            "theme": "dark",
            "api_url": "http://127.0.0.1:8000/api",
            "custom_ui_param": 42
        }
        with open(self.temp_settings_path, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("06:45")

        with open(self.temp_settings_path, "r", encoding="utf-8") as f:
            updated = json.load(f)

        self.assertEqual(updated["theme"], "dark")
        self.assertEqual(updated["api_url"], "http://127.0.0.1:8000/api")
        self.assertEqual(updated["custom_ui_param"], 42)
        self.assertTrue(updated["morning_briefing_enabled"])
        self.assertEqual(updated["preferred_briefing_time"], "06:45")

    # 9. GUI control state matching effective backend settings
    def test_09_gui_control_state_matching_backend(self):
        from gui.views.settings_view import SettingsView
        import customtkinter as ctk

        # Configure custom settings in backend
        b_settings.set_morning_briefing_enabled(True)
        b_settings.set_preferred_briefing_time("07:45")
        b_settings.set_contextual_advisories_enabled(False)
        b_settings.set_advisory_cooldown_hours(2)

        # Mock root window
        mock_root = MagicMock()
        with patch.object(ctk.CTkScrollableFrame, "__init__", return_value=None), \
             patch.object(ctk.CTkFrame, "__init__", return_value=None), \
             patch.object(SettingsView, "_test_api_connection"):
            view = SettingsView.__new__(SettingsView)
            view._last_activation = 0.0

            # Mock controls
            view._briefing_switch = MagicMock()
            view._time_entry = MagicMock()
            view._time_validation_lbl = MagicMock()
            view._advisories_switch = MagicMock()
            view._cooldown_dropdown = MagicMock()

            view._refresh_proactive_controls()

            view._briefing_switch.select.assert_called_once()
            view._time_entry.insert.assert_called_with(0, "07:45")
            view._advisories_switch.deselect.assert_called_once()
            view._cooldown_dropdown.set.assert_called_with("2 hours")

    # 10. Existing advisory behavior remaining intact when enabled vs disabled
    def test_10_advisory_surfacing_boundary_enforcement(self):
        from brain.briefing.recommendations import RecommendationState

        mock_rec = MagicMock()
        mock_rec.rec_id = "rec_001"
        mock_rec.short_id = "r001"
        mock_rec.title = "Test Project Review"
        mock_rec.explanation = "Project needs attention."
        mock_rec.suggested_action = "Review now."
        mock_rec.channels = ["chat"]
        mock_rec.last_surfaced_at = None
        mock_rec.priority.value = "HIGH"
        mock_rec.priority.rank = 1
        mock_rec.state = RecommendationState.ACTIVE

        mock_store = MagicMock()
        mock_store.list_all.return_value = [mock_rec]

        with patch("brain.briefing.recommendations.RecommendationStore", return_value=mock_store), \
             patch("brain.briefing.recommendations.match_recommendation_relevance", return_value=(True, False)):

            # Case A: Enabled -> Advisory is returned
            b_settings.set_contextual_advisories_enabled(True)
            res_enabled = _check_contextual_chat_recommendation("How is the test project?")
            self.assertIsNotNone(res_enabled)
            self.assertIn("Test Project Review", res_enabled)
            self.assertIn("💡 **Orange AI Advisory**", res_enabled)

            # Case B: Disabled -> Advisory is None
            b_settings.set_contextual_advisories_enabled(False)
            res_disabled = _check_contextual_chat_recommendation("How is the test project?")
            self.assertIsNone(res_disabled)

            # Case C: Cooldown enforcement (e.g. 8 hours)
            b_settings.set_contextual_advisories_enabled(True)
            b_settings.set_advisory_cooldown_hours(8)

            # Surfaced 5 hours ago (less than 8 hours cooldown) -> None
            five_hours_ago = (datetime.now() - timedelta(hours=5)).isoformat()
            mock_rec.last_surfaced_at = five_hours_ago
            res_cooldown = _check_contextual_chat_recommendation("How is the test project?")
            self.assertIsNone(res_cooldown, "Expected advisory to be blocked by 8h cooldown")

            # With 2 hours cooldown, 5 hours elapsed is greater than cooldown -> returned
            b_settings.set_advisory_cooldown_hours(2)
            res_after_cooldown = _check_contextual_chat_recommendation("How is the test project?")
            self.assertIsNotNone(res_after_cooldown, "Expected advisory to pass with 2h cooldown")


if __name__ == "__main__":
    unittest.main()
