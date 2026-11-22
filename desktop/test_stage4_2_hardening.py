"""
test_stage4_2_hardening.py - Comprehensive Unit & Regression Tests for Stage 4.2 Hardening.

Covers:
- DEF-01: ActivityView._read_job_steps timestamp robustness with real JobStep objects.
- DEF-03: Per-user writable data directory resolution, asset path resolution, SQLite integrity, and idempotent migration.
- DEF-04: View activation lifecycle, throttling (1.0s), and user state/filter preservation.
- API Settings: URL resolution, persistence, and OrangeAPIClient integration.
"""

import os
import sys
import time
import shutil
import tempfile
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from brain.paths import (
    get_user_data_dir,
    get_project_root,
    get_assets_dir,
    get_asset_path,
    verify_sqlite_integrity,
    migrate_legacy_data_if_needed,
)
from brain.jobs.models import Job, JobStep, JobStatus
from gui.settings import (
    get_settings_path,
    load_settings,
    save_settings,
    get_theme_preference,
    set_theme_preference,
    get_api_url,
    set_api_url,
)
from brain.api_client import OrangeAPIClient, DEFAULT_ORANGE_API_URL


class TestDef01ActivityViewJobSteps(unittest.TestCase):
    """DEF-01: Verify ActivityView safely reads JobStep timestamps without AttributeError."""

    def test_job_steps_timestamp_handling_and_fallback(self):
        from gui.views.activity_view import ActivityView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = ActivityView(root)

            # Step 1: started_at and completed_at present
            s1 = JobStep(
                job_id="job_abc12345",
                step_index=1,
                action_type="CALL_TOOL",
                tool_name="system_info",
                success=True,
                started_at="2026-10-30T06:00:00+00:00",
                completed_at="2026-10-30T06:00:02+00:00",
                duration_ms=2000,
            )
            # Step 2: started_at is None, completed_at is present
            s2 = JobStep(
                job_id="job_abc12345",
                step_index=2,
                action_type="CALL_TOOL",
                tool_name="list_processes",
                success=True,
                started_at=None,
                completed_at="2026-10-30T06:05:00+00:00",
                duration_ms=1000,
            )
            # Step 3: both started_at and completed_at are None
            s3 = JobStep(
                job_id="job_abc12345",
                step_index=3,
                action_type="CALL_TOOL",
                tool_name="network_ping",
                success=False,
                started_at=None,
                completed_at=None,
                duration_ms=None,
            )

            mock_job = Job(
                id="job_abc1234567890",
                goal="System Diagnostics",
                status=JobStatus.COMPLETED,
            )

            mock_manager = MagicMock()
            mock_manager.list_jobs.return_value = [mock_job]
            mock_manager.get_history.return_value = [s1, s2, s3]

            with patch("main.get_job_manager", return_value=mock_manager):
                steps = view._read_job_steps()

            # Verify no AttributeError was thrown and results are sorted descending by time
            self.assertEqual(len(steps), 3)
            self.assertEqual(steps[0]["tool_name"], "list_processes")
            self.assertEqual(steps[0]["time"], "2026-10-30 06:05:00")
            self.assertEqual(steps[1]["tool_name"], "system_info")
            self.assertEqual(steps[1]["time"], "2026-10-30 06:00:00")
            self.assertEqual(steps[2]["tool_name"], "network_ping")
            self.assertEqual(steps[2]["time"], "")
        finally:
            root.destroy()

    def test_job_step_handles_manager_exceptions_gracefully(self):
        from gui.views.activity_view import ActivityView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = ActivityView(root)
            mock_manager = MagicMock()
            mock_manager.list_jobs.side_effect = sqlite3.OperationalError("database is locked")

            with patch("main.get_job_manager", return_value=mock_manager):
                steps = view._read_job_steps()
            self.assertEqual(steps, [])
        finally:
            root.destroy()


class TestDef03PathResolutionAndMigration(unittest.TestCase):
    """DEF-03: Verify per-user directory paths, integrity check, and migration safety."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_paths_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_dev_mode_returns_project_root(self):
        with patch.object(sys, "frozen", False, create=True):
            with patch.dict(os.environ, {}, clear=True):
                path = get_user_data_dir()
                self.assertEqual(os.path.normpath(path), os.path.normpath(PROJECT_ROOT))

    def test_env_var_override(self):
        custom_dir = os.path.join(self.test_dir, "custom_user_data")
        with patch.dict(os.environ, {"ORANGE_AI_USER_DATA_DIR": custom_dir}):
            path = get_user_data_dir()
            self.assertEqual(os.path.normpath(path), os.path.normpath(custom_dir))
            self.assertTrue(os.path.isdir(custom_dir))

    def test_frozen_mode_uses_localappdata(self):
        fake_localappdata = os.path.join(self.test_dir, "AppData", "Local")
        os.makedirs(fake_localappdata, exist_ok=True)
        with patch.object(sys, "frozen", True, create=True):
            with patch.dict(os.environ, {"LOCALAPPDATA": fake_localappdata}, clear=True):
                path = get_user_data_dir()
                expected = os.path.join(fake_localappdata, "Orange AI")
                self.assertEqual(os.path.normpath(path), os.path.normpath(expected))
                self.assertTrue(os.path.isdir(expected))

    def test_get_assets_dir_and_path(self):
        assets_dir = get_assets_dir()
        self.assertTrue(os.path.isdir(assets_dir))
        icon_path = get_asset_path("app_icon.ico")
        self.assertTrue(os.path.isfile(icon_path))

    def test_sqlite_integrity_check(self):
        valid_db = os.path.join(self.test_dir, "valid.db")
        conn = sqlite3.connect(valid_db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO t VALUES (1, 'hello');")
        conn.commit()
        conn.close()
        self.assertTrue(verify_sqlite_integrity(valid_db))

        # Corrupted database file
        corrupt_db = os.path.join(self.test_dir, "corrupt.db")
        with open(corrupt_db, "wb") as f:
            f.write(b"NOT A VALID SQLITE DATABASE FILE HEADER")
        self.assertFalse(verify_sqlite_integrity(corrupt_db))

        # Non-existent file
        self.assertFalse(verify_sqlite_integrity(os.path.join(self.test_dir, "missing.db")))

    def test_migrate_legacy_data_first_launch(self):
        source_dir = os.path.join(self.test_dir, "source")
        target_dir = os.path.join(self.test_dir, "target")
        os.makedirs(source_dir, exist_ok=True)
        os.makedirs(target_dir, exist_ok=True)

        # Create valid source database and settings
        src_db = os.path.join(source_dir, "memory.db")
        conn = sqlite3.connect(src_db)
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT);")
        conn.execute("INSERT INTO memories VALUES (1, 'Remember user preferences');")
        conn.commit()
        conn.close()

        src_settings = os.path.join(source_dir, "gui_settings.json")
        with open(src_settings, "w", encoding="utf-8") as f:
            f.write('{"theme": "dark", "api_url": "http://127.0.0.1:8000"}')

        # Run migration into target
        report = migrate_legacy_data_if_needed(legacy_source_dir=source_dir, target_dir=target_dir, force=True)
        self.assertEqual(report["status"], "success")
        self.assertIn("memory.db", report["migrated"])
        self.assertIn("gui_settings.json", report["migrated"])

        target_db = os.path.join(target_dir, "memory.db")
        self.assertTrue(os.path.isfile(target_db))
        self.assertTrue(verify_sqlite_integrity(target_db))

        # Verify second call is idempotent and skips existing files
        report2 = migrate_legacy_data_if_needed(legacy_source_dir=source_dir, target_dir=target_dir, force=True)
        self.assertEqual(report2["status"], "success")
        self.assertIn("memory.db", report2["skipped"])
        self.assertIn("gui_settings.json", report2["skipped"])


class TestDef04ViewActivationLifecycle(unittest.TestCase):
    """DEF-04: Verify on_activated() throttling and active user state preservation."""

    def test_agent_jobs_view_activation_throttling(self):
        from gui.views.agent_jobs_view import AgentJobsView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = AgentJobsView(root)
            # Set a non-default filter to ensure it is preserved
            view._current_filter = "running"
            view._search_query = "database"

            with patch.object(view, "refresh_jobs") as mock_refresh:
                # First activation: set _last_activation back in time so it fires
                view._last_activation = 0.0
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 1)

                # Rapid immediate second activation within 1s should be throttled
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 1)

                # Advance timestamp past throttle threshold
                view._last_activation = time.time() - 2.0
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 2)

                # Verify user state was preserved
                self.assertEqual(view._current_filter, "running")
                self.assertEqual(view._search_query, "database")
        finally:
            root.destroy()

    def test_memory_view_activation_throttling(self):
        from gui.views.memory_view import MemoryView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = MemoryView(root)
            view._active_tab = "projects"
            view._search_query = "client_alpha"

            with patch.object(view, "refresh_memories") as mock_refresh:
                view._last_activation = 0.0
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 1)

                # Rapid call throttled
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 1)

                # Advance timestamp past throttle threshold
                view._last_activation = time.time() - 2.0
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 2)

                # Preserve user state
                self.assertEqual(view._active_tab, "projects")
                self.assertEqual(view._search_query, "client_alpha")
        finally:
            root.destroy()

    def test_activity_view_activation_throttling(self):
        from gui.views.activity_view import ActivityView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = ActivityView(root)
            view._active_filter = "conversations"

            with patch.object(view, "refresh_activity") as mock_refresh:
                view._last_activation = 0.0
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 1)

                # Throttled call
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 1)

                # Advance timestamp
                view._last_activation = time.time() - 2.0
                view.on_activated()
                self.assertEqual(mock_refresh.call_count, 2)

                self.assertEqual(view._active_filter, "conversations")
        finally:
            root.destroy()

    def test_tools_view_activation_throttling(self):
        from gui.views.tools_view import ToolsView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = ToolsView(root)
            with patch.object(view, "_load_tools") as mock_load:
                view._last_activation = 0.0
                view.on_activated()
                self.assertEqual(mock_load.call_count, 1)

                view.on_activated()
                self.assertEqual(mock_load.call_count, 1)

                view._last_activation = time.time() - 2.0
                view.on_activated()
                self.assertEqual(mock_load.call_count, 2)
        finally:
            root.destroy()

    def test_settings_view_activation_throttling(self):
        from gui.views.settings_view import SettingsView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = SettingsView(root)
            with patch.object(view, "_test_api_connection") as mock_test:
                view._last_activation = 0.0
                view.on_activated()
                self.assertEqual(mock_test.call_count, 1)

                view.on_activated()
                self.assertEqual(mock_test.call_count, 1)

                view._last_activation = time.time() - 2.0
                view.on_activated()
                self.assertEqual(mock_test.call_count, 2)
        finally:
            root.destroy()

    def test_chat_view_preserves_composer_text_on_activation(self):
        from gui.views.chat_view import ChatView
        import customtkinter as ctk

        root = ctk.CTk()
        root.withdraw()
        try:
            view = ChatView(root)
            view._composer.set_text("Unsent work-in-progress draft message")
            view.on_activated()
            self.assertEqual(view._composer.get_text(), "Unsent work-in-progress draft message")
        finally:
            root.destroy()


class TestSettingsPersistence(unittest.TestCase):
    """Verify settings loading, saving, defaults, and API client integration."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_settings_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_settings_read_write_cycle(self):
        with patch.dict(os.environ, {"ORANGE_AI_USER_DATA_DIR": self.test_dir}):
            # Initially file does not exist, defaults are returned
            settings = load_settings()
            self.assertEqual(settings["theme"], "light")
            self.assertIsNone(get_api_url())

            # Mutate and save
            set_theme_preference("dark")
            set_api_url("https://orange-api-cuuo.onrender.com")

            # Reload and verify
            reloaded = load_settings()
            self.assertEqual(reloaded["theme"], "dark")
            self.assertEqual(reloaded["api_url"], "https://orange-api-cuuo.onrender.com")
            self.assertEqual(get_theme_preference(), "dark")
            self.assertEqual(get_api_url(), "https://orange-api-cuuo.onrender.com")

    def test_orange_api_client_resolution_precedence(self):
        with patch.dict(os.environ, {"ORANGE_AI_USER_DATA_DIR": self.test_dir}, clear=True):
            # 1. Default fallback when no env or settings
            client = OrangeAPIClient()
            self.assertEqual(client.api_url, DEFAULT_ORANGE_API_URL)

            # 2. Configured in gui_settings.json
            set_api_url("http://127.0.0.1:8000")
            client2 = OrangeAPIClient()
            self.assertEqual(client2.api_url, "http://127.0.0.1:8000")

            # 3. Environment variable overrides gui_settings.json
            with patch.dict(os.environ, {"ORANGE_API_URL": "http://127.0.0.1:9999"}):
                client3 = OrangeAPIClient()
                self.assertEqual(client3.api_url, "http://127.0.0.1:9999")

            # 4. Explicit parameter overrides everything
            client4 = OrangeAPIClient(api_url="https://custom-proxy.internal")
            self.assertEqual(client4.api_url, "https://custom-proxy.internal")


if __name__ == "__main__":
    unittest.main()
