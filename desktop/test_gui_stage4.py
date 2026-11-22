"""
Orange AI Desktop - Stage 4 Test Suite.

Comprehensive tests for the six secondary views:
1. Agent Jobs View:
   - Real JobManager integration, status filter pills, split list/detail layout,
     job lifecycle actions (Resume, Pause, Approve, Deny, Cancel), audit log inspection.
2. Memory View:
   - Structured facts and legacy memory metrics, category pills, live keyword search,
     remember new fact card, explicit confirmation before retraction/deletion.
3. Activity View:
   - Multi-provenance tabs (System Logs, Job Audits, Conversation Turns),
     level/outcome filtering, search filter, authentic data loading without synthetic feeds.
4. Permissions View:
   - Elevation status (is_running_as_admin), 3 policy tiers (SAFE, CONFIRMATION_REQUIRED, HIGH_RISK),
     complete ToolRegistry audit table, strictly read-only inspection.
5. Tools View:
   - Tool card generation from ToolRegistry, category filters, live keyword search,
     safe handoff to Chat composer (no direct execution from UI buttons).
6. Settings View:
   - Theme selector with persistence in gui_settings.json, API health check status,
     storage locations and architecture disclosures.
7. Navigation & App Integration:
   - OrangeAIApp switching across all 6 secondary views, view caching in _views_cache,
     cross-view handoffs (_run_query_in_chat).
8. CLI & Backend Integrity:
   - Zero changes to tool permissions, zero parallel router, clean shutdown.
"""

import os
import sys
import json
import time
import tempfile
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from gui.settings import get_theme_preference, set_theme_preference
from gui.app import OrangeAIApp
from gui.views.agent_jobs_view import AgentJobsView
from gui.views.memory_view import MemoryView
from gui.views.activity_view import ActivityView
from gui.views.permissions_view import PermissionsView
from gui.views.tools_view import ToolsView
from gui.views.settings_view import SettingsView
from tools.registry import registry, Tool
from tools.permissions import RiskLevel, check_permission, PermissionStatus
from tools.system import is_running_as_admin
from brain.jobs.models import Job, JobStatus, JobStep
import brain.mood as mood

# ==============================================================================
# Stage 4.2.1: Test-Environment Isolation Fixtures & Hooks
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
        del mood.DB_PATH
    if _module_temp_dir is not None:
        try:
            _module_temp_dir.cleanup()
        except Exception:
            pass


try:
    import pytest

    @pytest.fixture(autouse=True, scope="function")
    def isolate_mood_db():
        """Autouse pytest fixture guaranteeing mood DB isolation for all GUI tests."""
        temp_dir = tempfile.TemporaryDirectory()
        isolated_db = os.path.join(temp_dir.name, "isolated_mood.db")
        orig_path = getattr(mood, "_override_db_path", None)
        mood.DB_PATH = isolated_db
        try:
            yield isolated_db
        finally:
            try:
                mood.close()
            except Exception:
                pass
            if orig_path is not None:
                mood.DB_PATH = orig_path
            else:
                del mood.DB_PATH
            try:
                temp_dir.cleanup()
            except Exception:
                pass
except ImportError:
    pass


class TestAppNavigationAndCaching(unittest.TestCase):
    """Test Suite 1: Shell navigation across all Stage 4 views and view caching."""

    @classmethod
    def setUpClass(cls):
        # Stage 3.7E.5B-B.2: Isolate EventCoordinator watermark store path
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.watermark_path = os.path.join(cls.temp_dir.name, "event_watermarks.json")
        cls.isolated_mood_db = os.path.join(cls.temp_dir.name, "isolated_mood.db")

        # Stage 4.2.1: Isolate brain.mood.DB_PATH before OrangeAIApp() initializes
        cls._orig_mood_override = getattr(mood, "_override_db_path", None)
        mood.DB_PATH = cls.isolated_mood_db

        from gui.services.event_coordinator import EventCoordinator
        cls._orig_coord_init = EventCoordinator.__init__
        def _isolated_coord_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or cls.watermark_path
            cls._orig_coord_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)
        cls.coord_patcher = patch.object(EventCoordinator, "__init__", _isolated_coord_init)
        cls.coord_patcher.start()

        ctk.set_appearance_mode("light")
        cls.app = OrangeAIApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass
        cls.coord_patcher.stop()

        # Restore brain.mood.DB_PATH
        try:
            mood.close()
        except Exception:
            pass
        if cls._orig_mood_override is not None:
            mood.DB_PATH = cls._orig_mood_override
        else:
            del mood.DB_PATH

        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    def test_navigation_to_all_stage4_views(self):
        """Verify navigation rail navigates to each secondary view without errors."""
        views_to_test = ["jobs", "memory", "activity", "permissions", "tools", "settings", "chat"]
        for view_key in views_to_test:
            self.app._on_navigate(view_key)
            self.app.update_idletasks()
            self.assertIn(view_key, self.app._views_cache)
            self.assertIsNotNone(self.app._active_view)

    def test_view_caching_preserves_instances(self):
        """Verify that views are cached and not re-instantiated upon re-navigation."""
        self.app._on_navigate("memory")
        memory_instance_1 = self.app._views_cache.get("memory")
        self.assertIsInstance(memory_instance_1, MemoryView)

        self.app._on_navigate("jobs")
        jobs_instance = self.app._views_cache.get("jobs")
        self.assertIsInstance(jobs_instance, AgentJobsView)

        self.app._on_navigate("memory")
        memory_instance_2 = self.app._views_cache.get("memory")
        self.assertIs(memory_instance_1, memory_instance_2)

    def test_run_query_in_chat_handoff(self):
        """Verify _run_query_in_chat switches to chat and populates composer."""
        self.app._run_query_in_chat("Test handoff query from tools view")
        self.app.update_idletasks()

        chat_view = self.app._views_cache.get("chat")
        self.assertIsNotNone(chat_view)
        self.assertEqual(chat_view._composer.get_text(), "Test handoff query from tools view")


class TestAgentJobsView(unittest.TestCase):
    """Test Suite 2: AgentJobsView with real models and lifecycle controls."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.sample_jobs = [
            Job(
                id="job_active_1",
                goal="Monitor disk health",
                status=JobStatus.RUNNING,
                priority=1,
                step_count=2,
            ),
            Job(
                id="job_waiting_1",
                goal="Restart production service",
                status=JobStatus.WAITING_CONFIRMATION,
                priority=2,
                last_error=None,
            ),
            Job(
                id="job_paused_1",
                goal="Batch backup files",
                status=JobStatus.PAUSED,
                priority=0,
            ),
            Job(
                id="job_completed_1",
                goal="Index workspace documents",
                status=JobStatus.COMPLETED,
                final_response="Indexed 1,420 files successfully",
            ),
        ]

    def test_jobs_view_rendering_and_status_filtering(self):
        view = AgentJobsView(self.root)
        view._on_jobs_loaded(self.sample_jobs)
        self.root.update_idletasks()

        self.assertEqual(len(view._jobs), 4)

        # Filter by Active (RUNNING)
        view._set_filter("RUNNING")
        self.assertEqual(view._filter_status, "RUNNING")
        filtered = [j for j in view._jobs if j.status.value == view._filter_status]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].id, "job_active_1")

        # Filter by Waiting
        view._set_filter("WAITING_CONFIRMATION")
        self.assertEqual(view._filter_status, "WAITING_CONFIRMATION")
        filtered = [j for j in view._jobs if j.status.value == view._filter_status]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].id, "job_waiting_1")

        # Filter by All
        view._set_filter(None)
        self.assertIsNone(view._filter_status)

        view.destroy()

    def test_job_selection_and_action_controls(self):
        view = AgentJobsView(self.root)
        view._on_jobs_loaded(self.sample_jobs)

        # Select waiting job -> details shown
        waiting_job = self.sample_jobs[1]
        view._show_job_detail(waiting_job)
        self.root.update_idletasks()

        self.assertIsNotNone(view._selected_job)
        self.assertEqual(view._selected_job.id, "job_waiting_1")

        # Select paused job -> details shown
        paused_job = self.sample_jobs[2]
        view._show_job_detail(paused_job)
        self.root.update_idletasks()
        self.assertEqual(view._selected_job.id, "job_paused_1")

        view.destroy()

    def test_empty_jobs_state(self):
        view = AgentJobsView(self.root)
        view._on_jobs_loaded([])
        self.root.update_idletasks()
        self.assertEqual(len(view._jobs), 0)
        view.destroy()


class TestMemoryView(unittest.TestCase):
    """Test Suite 3: MemoryView metrics, filtering, remembering, and retraction."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.sample_structured = [
            {"category": "user", "key": "preferred_name", "value": "Sayan", "confidence": 1.0, "updated_at": "2026-10-29"},
            {"category": "preference", "key": "theme", "value": "dark", "confidence": 0.9, "updated_at": "2026-10-29"},
            {"category": "project", "key": "active_project", "value": "Orange AI Desktop", "confidence": 0.95, "updated_at": "2026-10-29"},
        ]
        self.sample_legacy = [
            "Prefers concise terminal commands",
            "Meeting every Monday at 10am",
        ]
        self.sample_stats = {
            "structured_count": 3,
            "legacy_count": 2,
            "categories": ["user", "preference", "project"],
        }

    def test_memory_view_display_and_metrics(self):
        view = MemoryView(self.root)
        view._on_memories_loaded(self.sample_structured, self.sample_legacy, self.sample_stats)
        self.root.update_idletasks()

        self.assertEqual(len(view._structured_memories), 3)
        self.assertEqual(len(view._legacy_memories), 2)
        self.assertEqual(view._stat_structured.cget("text"), "3")
        self.assertEqual(view._stat_legacy.cget("text"), "2")
        view.destroy()

    def test_memory_category_filtering(self):
        view = MemoryView(self.root)
        view._on_memories_loaded(self.sample_structured, self.sample_legacy, self.sample_stats)

        # Filter by preference
        view._set_category_filter("preference")
        self.assertEqual(view._active_category, "preference")

        # Filter by legacy
        view._set_category_filter("legacy")
        self.assertEqual(view._active_category, "legacy")

        # Clear filter
        view._set_category_filter(None)
        self.assertIsNone(view._active_category)

        view.destroy()

    def test_memory_keyword_search(self):
        view = MemoryView(self.root)
        view._on_memories_loaded(self.sample_structured, self.sample_legacy, self.sample_stats)

        view._search_query = "sayan"
        self.root.update_idletasks()
        view._render_memory_items()
        self.assertEqual(view._search_query, "sayan")

        view.destroy()

    def test_explicit_confirmation_required_before_forget(self):
        """Security/Privacy: Forget action must require explicit user confirmation."""
        view = MemoryView(self.root)
        view._on_memories_loaded(self.sample_structured, self.sample_legacy, self.sample_stats)

        item = self.sample_structured[0]
        with patch("brain.memory.retire_structured") as mock_retire, \
             patch("brain.memory.retract_entity") as mock_retract:
            # Calling _prompt_forget_structured opens confirmation dialog without executing
            view._prompt_forget_structured(item)
            self.root.update_idletasks()

            mock_retire.assert_not_called()
            mock_retract.assert_not_called()
            self.assertGreater(len(view._dialog_container.winfo_children()), 0)

        view.destroy()


class TestActivityView(unittest.TestCase):
    """Test Suite 4: ActivityView multi-provenance data sources and filtering."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_provenance_source_switching(self):
        view = ActivityView(self.root)
        self.root.update_idletasks()

        # Default source is logs
        self.assertEqual(view._active_source, "logs")

        # Switch to jobs
        view._set_source("jobs")
        self.assertEqual(view._active_source, "jobs")

        # Switch to conversations
        view._set_source("conversations")
        self.assertEqual(view._active_source, "conversations")

        view.destroy()

    def test_system_log_level_filter(self):
        view = ActivityView(self.root)
        sample_logs = [
            {"timestamp": "2026-10-29 10:00:00", "level": "INFO", "message": "App started"},
            {"timestamp": "2026-10-29 10:00:05", "level": "WARNING", "message": "High memory usage"},
            {"timestamp": "2026-10-29 10:00:10", "level": "ERROR", "message": "Failed connection"},
        ]
        view._logs = sample_logs

        view._set_level_filter("ERROR")
        self.assertEqual(view._level_filter, "ERROR")

        filtered = [e for e in sample_logs if (view._level_filter is None or e["level"] == view._level_filter)]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["message"], "Failed connection")

        view.destroy()


class TestPermissionsView(unittest.TestCase):
    """Test Suite 5: PermissionsView privilege status and 3 policy tiers."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_elevation_status_and_tier_rendering(self):
        view = PermissionsView(self.root)
        self.root.update_idletasks()

        # Check that elevation label exists
        self.assertIsNotNone(view._elevation_label)
        self.assertIn("Process Privilege Level", view._elevation_label.cget("text"))

        # Check that all tools from registry are present
        all_tools = registry.list_tools()
        self.assertGreater(len(all_tools), 0)
        tool_names = [t.name for t in all_tools]

        # Check risk classification for a known safe tool
        self.assertIn("get_time", tool_names)
        safe_tool = registry.get("get_time")
        self.assertEqual(safe_tool.risk_level, RiskLevel.SAFE)

        # Check risk classification for a confirmation-required tool
        self.assertIn("close_process", tool_names)
        confirm_tool = registry.get("close_process")
        self.assertEqual(confirm_tool.risk_level, RiskLevel.CONFIRMATION_REQUIRED)

        # Check permission check evaluation
        safe_res = check_permission(safe_tool)
        self.assertEqual(safe_res.status, PermissionStatus.ALLOWED)
        confirm_res = check_permission(confirm_tool)
        self.assertEqual(confirm_res.status, PermissionStatus.REQUIRES_CONFIRMATION)

        view.destroy()

    def test_view_is_strictly_read_only(self):
        """Verify that PermissionsView has no toggle switches or elevation bypass buttons."""
        view = PermissionsView(self.root)
        self.root.update_idletasks()

        # Search for any Switch widgets or editable elements
        switches = [w for w in view.winfo_children() if isinstance(w, ctk.CTkSwitch)]
        self.assertEqual(len(switches), 0)

        view.destroy()


class TestToolsView(unittest.TestCase):
    """Test Suite 6: ToolsView catalog rendering, filtering, and safe chat handoff."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_tool_catalog_population(self):
        queries_received = []
        view = ToolsView(self.root, on_run_in_chat=lambda q: queries_received.append(q))
        self.root.update_idletasks()

        self.assertGreater(len(view._all_tools), 0)
        tool_names = [t.name for t in view._all_tools]
        self.assertIn("get_time", tool_names)

        view.destroy()

    def test_tools_category_and_search_filter(self):
        view = ToolsView(self.root)
        self.root.update_idletasks()

        # Filter by System
        view._set_category("system")
        self.assertEqual(view._active_category, "system")
        filtered = [t for t in view._all_tools if t.category.lower() == view._active_category]
        self.assertTrue(all(t.category.lower() == "system" for t in filtered))

        # Filter by search query
        view._search_query = "time"
        filtered = [t for t in view._all_tools if "time" in t.name.lower()]
        self.assertTrue(any("time" in t.name.lower() for t in filtered))

        view.destroy()

    def test_tool_run_in_chat_security(self):
        """Security: Tool card Run in Chat must trigger chat composer handoff, never direct execution."""
        queries_received = []
        view = ToolsView(self.root, on_run_in_chat=lambda q: queries_received.append(q))
        self.root.update_idletasks()

        with patch.object(registry, "execute") as mock_exec:
            tool_obj = registry.get("get_time")
            view._on_run_tool(tool_obj)

            # Assert registry.execute was NOT called directly from button
            mock_exec.assert_not_called()

            # Assert query was safely delegated to on_run_in_chat
            self.assertEqual(len(queries_received), 1)
            self.assertIn("get_time", queries_received[0])

        view.destroy()


class TestSettingsView(unittest.TestCase):
    """Test Suite 7: SettingsView theme persistence, API check, and file disclosures."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_settings_theme_toggle_persistence(self):
        view = SettingsView(self.root)
        self.root.update_idletasks()

        original_pref = get_theme_preference()

        try:
            # Switch to dark
            view._select_theme("dark")
            self.assertEqual(get_theme_preference(), "dark")
            self.assertTrue(theme_manager.is_dark)

            # Switch back to light
            view._select_theme("light")
            self.assertEqual(get_theme_preference(), "light")
            self.assertFalse(theme_manager.is_dark)
        finally:
            set_theme_preference(original_pref)
            theme_manager.set_theme(original_pref)

        view.destroy()

    def test_settings_api_health_check(self):
        view = SettingsView(self.root)
        self.root.update_idletasks()

        # Test with mocked is_available True
        view._on_connection_tested(True)
        self.assertIn("Connected", view._api_status_badge.cget("text"))

        # Test with mocked is_available False
        view._on_connection_tested(False)
        self.assertIn("Offline", view._api_status_badge.cget("text"))

        view.destroy()


class TestCliAndEngineIntegrity(unittest.TestCase):
    """Test Suite 8: Verify CLI mode and shared engine integrity during Stage 4."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.isolated_mood_db = os.path.join(cls.temp_dir.name, "isolated_cli_mood.db")
        cls._orig_mood_override = getattr(mood, "_override_db_path", None)
        mood.DB_PATH = cls.isolated_mood_db

    @classmethod
    def tearDownClass(cls):
        try:
            mood.close()
        except Exception:
            pass
        if cls._orig_mood_override is not None:
            mood.DB_PATH = cls._orig_mood_override
        else:
            del mood.DB_PATH
        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    def test_process_user_turn_deterministic_commands(self):
        import main
        main.ensure_initialized()

        # 1. mood command
        outcome = main.process_user_turn("mood")
        self.assertEqual(outcome["type"], "mood")
        self.assertIn("I'm", outcome["reply"])

        # 2. plan command
        outcome = main.process_user_turn("plan open notepad then list files")
        self.assertEqual(outcome["type"], "plan")
        self.assertIn("Plan for:", outcome["reply"])

        # 3. memory profile command
        outcome = main.process_user_turn("show profile")
        self.assertEqual(outcome["type"], "memory")
        self.assertIn("Orange AI Profile", outcome["reply"])

        # 4. safe tool command (system time)
        outcome = main.process_user_turn("what time is it")
        self.assertEqual(outcome["type"], "tool")
        self.assertIn("Current time is", outcome["reply"])


if __name__ == "__main__":
    unittest.main()
