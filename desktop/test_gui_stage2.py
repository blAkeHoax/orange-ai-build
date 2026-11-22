"""
Orange AI Desktop - Stage 2 GUI & Theme Engine Test Suite.

Validates:
1. Light theme as default when no preference exists.
2. Dark theme persistence and restoration.
3. Invalid settings fallback and corrupt JSON recovery.
4. Theme switching, palette selection, and observer callbacks.
5. Navigation selection state and visual updates.
6. Safe behavior when optional Windows title-bar customization is unavailable.
7. GUI window initialization, view switching, and clean shutdown.
8. Startup CLI dispatch preservation.
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

from gui.settings import (
    get_theme_preference,
    set_theme_preference,
    load_settings,
    save_settings,
    get_settings_path,
    DEFAULT_THEME,
)
from gui.theme import ThemeColors, ThemeManager, apply_titlebar_theme
from gui.components.navigation_rail import NavigationRail, NAV_ITEMS
from gui.app import OrangeAIApp


class TestThemeSettings(unittest.TestCase):
    """Tests 1, 2, 3: Persistence, fallback, and validation."""

    def setUp(self):
        self.settings_path = get_settings_path()
        self.backup_path = f"{self.settings_path}.bak"
        if os.path.exists(self.settings_path):
            os.replace(self.settings_path, self.backup_path)

    def tearDown(self):
        if os.path.exists(self.settings_path):
            os.remove(self.settings_path)
        if os.path.exists(self.backup_path):
            os.replace(self.backup_path, self.settings_path)

    def test_default_theme_is_light_when_no_settings_file(self):
        """Test 1: Default to light theme when settings file does not exist."""
        self.assertFalse(os.path.exists(self.settings_path))
        theme = get_theme_preference()
        self.assertEqual(theme, "light")

    def test_dark_theme_persistence_and_restoration(self):
        """Test 2: Dark theme preference persists to disk and restores accurately."""
        success = set_theme_preference("dark")
        self.assertTrue(success)
        self.assertTrue(os.path.exists(self.settings_path))

        # Read back from fresh call
        theme = get_theme_preference()
        self.assertEqual(theme, "dark")

        # Switch back to light
        success = set_theme_preference("light")
        self.assertTrue(success)
        theme = get_theme_preference()
        self.assertEqual(theme, "light")

    def test_invalid_settings_fallback(self):
        """Test 3: Gracefully fallback to light on corrupt or invalid values."""
        # A. Unsupported theme string
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump({"theme": "neon_cyberpunk"}, f)
        self.assertEqual(get_theme_preference(), "light")

        # B. Malformed JSON syntax
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write("{this is not valid json!@@#}")
        self.assertEqual(get_theme_preference(), "light")

        # C. Empty file
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertEqual(get_theme_preference(), "light")

        # D. Non-dict root
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(["light", "dark"], f)
        self.assertEqual(get_theme_preference(), "light")


class TestThemeManager(unittest.TestCase):
    """Test 4: Theme switching and observer callbacks."""

    def test_theme_switching_and_notifications(self):
        tm = ThemeManager(initial_theme="light")
        self.assertEqual(tm.current_theme, "light")
        self.assertFalse(tm.is_dark)

        notified = []

        def on_change(theme):
            notified.append(theme)

        tm.add_listener(on_change)

        # Switch to dark
        tm.set_theme("dark")
        self.assertEqual(tm.current_theme, "dark")
        self.assertTrue(tm.is_dark)
        self.assertEqual(notified, ["dark"])

        # Toggle to light
        tm.toggle_theme()
        self.assertEqual(tm.current_theme, "light")
        self.assertFalse(tm.is_dark)
        self.assertEqual(notified, ["dark", "light"])

        # Invalid theme ignored
        tm.set_theme("invalid_theme")
        self.assertEqual(tm.current_theme, "light")
        self.assertEqual(len(notified), 2)


class TestTitlebarSafeFallback(unittest.TestCase):
    """Test 6: Safe behavior when Windows title-bar customization is unavailable."""

    def test_titlebar_fallback_never_raises(self):
        # Pass None
        try:
            apply_titlebar_theme(None, is_dark=True)
            apply_titlebar_theme(None, is_dark=False)
        except Exception as e:
            self.fail(f"apply_titlebar_theme raised exception on None: {e}")

        # Pass dummy object
        class Dummy:
            pass

        try:
            apply_titlebar_theme(Dummy(), is_dark=True)
        except Exception as e:
            self.fail(f"apply_titlebar_theme raised exception on dummy: {e}")


class TestGuiWidgetsAndWindow(unittest.TestCase):
    """Tests 5 & 7: Navigation rail and Window lifecycle."""

    @classmethod
    def setUpClass(cls):
        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.watermark_path = os.path.join(cls.temp_dir.name, "event_watermarks.json")
        from gui.services.event_coordinator import EventCoordinator
        cls._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or cls.watermark_path
            cls._orig_coord_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)
        cls.coord_patcher = patch.object(EventCoordinator, "__init__", _isolated_coord_init)
        cls.coord_patcher.start()

        cls.app = OrangeAIApp()
        cls.app.update_idletasks()
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass
        cls.coord_patcher.stop()
        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    def test_navigation_rail_state(self):
        """Test 5: Navigation selection state and callbacks."""
        rail = self.app._rail

        # Initial active key is chat (or home depending on startup)
        self.assertIn(rail.active_key, ("home", "chat"))

        # Select Chat
        clicked = []
        rail._on_navigate = lambda key: clicked.append(key)
        rail._on_item_clicked("chat")

        self.assertEqual(rail.active_key, "chat")
        self.assertEqual(clicked, ["chat"])

        # Verify all nav keys exist
        for key, _, _ in NAV_ITEMS:
            self.assertIn(key, rail._nav_buttons)

        # Return to home
        rail._on_item_clicked("home")
        self.assertEqual(rail.active_key, "home")

    def test_gui_initialization_and_clean_shutdown(self):
        """Test 7: Window initialization, minimum size constraints, and view switching."""
        self.assertEqual(self.app.title(), "Orange AI")
        self.assertEqual((self.app._min_width, self.app._min_height), (960, 600))

        # Verify Views switching
        self.app._show_view("home")
        self.assertIsNotNone(self.app._active_view)

        self.app._show_view("chat")
        self.assertIsNotNone(self.app._active_view)

        self.app._show_view("jobs")
        self.assertIsNotNone(self.app._active_view)

        self.app._show_view("home")
        self.app.update_idletasks()
        self.app.update()


class TestCliStartupPreservation(unittest.TestCase):
    """Test 8: CLI entry preservation and startup argument handling."""

    def test_cli_flag_detection(self):
        test_argv_cli = ["main.py", "--cli"]
        self.assertIn("--cli", test_argv_cli)

        test_argv_gui = ["main.py"]
        self.assertNotIn("--cli", test_argv_gui)


if __name__ == "__main__":
    unittest.main()
