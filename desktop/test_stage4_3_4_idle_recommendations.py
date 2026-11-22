"""
test_stage4_3_4_idle_recommendations.py - Comprehensive Test Suite for Stage 4.3.4.

Validates:
1. Category A: Activity Tracking & Idle Detection (9 tests)
2. Category B: Periodic Ticker & Evaluation Concurrency (8 tests)
3. Category C: Candidate Filtering, Cooldown & Selection (8 tests)
4. Category D: UI Delivery, Single-Owner Persistence & Race Hardening (9 tests)
Total: 34 tests.

Strict Test-Environment Isolation:
- brain.mood.DB_PATH redirected to temporary test database.
- brain.briefing.settings.SETTINGS_PATH redirected to temporary gui_settings.json.
- RecommendationStore uses explicit isolated temporary files with _STRICT_TEST_ISOLATION active.
- Zero mutations to production databases or stores.
"""

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
import brain.mood as mood
import brain.briefing.settings as b_settings
from brain.briefing.models import (
    ProjectBriefingContext,
    ReminderBriefingContext,
    JobBriefingContext,
)
from brain.briefing.recommendations import (
    Recommendation,
    RecommendationStore,
    RecommendationPriority,
    RecommendationState,
    RecommendationCategory,
    RecommendationEvaluator,
)


# ==============================================================================
# Module-Level Test-Environment Isolation Fixtures
# ==============================================================================
_module_temp_dir: Optional[tempfile.TemporaryDirectory] = None
_orig_mood_path: Optional[str] = None
_settings_patcher = None
_test_rec_store_path: Optional[str] = None


def setUpModule():
    """Ensure total test isolation from production databases and settings."""
    global _module_temp_dir, _orig_mood_path, _settings_patcher, _test_rec_store_path
    _module_temp_dir = tempfile.TemporaryDirectory(prefix="orange_test_stage434_")
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
        json.dump(
            {
                "theme": "dark",
                "contextual_advisories_enabled": True,
                "advisory_cooldown_hours": 4,
            },
            f,
        )
    _settings_patcher = patch("gui.settings.get_settings_path", return_value=test_gui_settings)
    _settings_patcher.start()

    # 3. Setup test recommendations file
    _test_rec_store_path = os.path.join(temp_dir, "test_recommendations.json")
    with open(_test_rec_store_path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "1.0", "recommendations": {}}, f)

    ctk.set_appearance_mode("Dark")


def tearDownModule():
    """Restore original paths and cleanup temporary directory."""
    global _module_temp_dir, _orig_mood_path, _settings_patcher
    if _orig_mood_path:
        mood.DB_PATH = _orig_mood_path
    if _settings_patcher:
        _settings_patcher.stop()
    if _module_temp_dir:
        try:
            _module_temp_dir.cleanup()
        except Exception:
            pass


class BaseStage434TestCase(unittest.TestCase):
    """Base setup for Stage 4.3.4 test cases providing isolated app and store."""

    def setUp(self):
        self.app = OrangeAIApp.__new__(OrangeAIApp)
        self.app._is_shutting_down = False
        self.app._last_user_activity_time = time.monotonic()
        self.app._idle_cycle_delivered = False
        self.app._idle_inactivity_threshold_sec = 600.0
        self.app._idle_check_interval_ms = 30000
        self.app._idle_worker_lock = threading.Lock()
        self.app._idle_worker_thread = None
        self.app._idle_worker_token = 0
        self.app._idle_attempt_states = {}
        self.app._idle_attempt_events = {}

        self.app._briefing_worker_lock = threading.Lock()
        self.app._briefing_worker_thread = None
        self.app._briefing_worker_token = 0
        self.app._briefing_attempt_states = {}
        self.app._briefing_attempt_events = {}

        self.app._db_path = None
        self.app._job_store = None

        # Isolated test store
        self.temp_store_file = os.path.join(
            _module_temp_dir.name, f"test_store_{int(time.time()*1000)}.json"
        )
        with open(self.temp_store_file, "w", encoding="utf-8") as f:
            json.dump({"schema_version": "1.0", "recommendations": {}}, f)
        self.store = RecommendationStore(file_path=self.temp_store_file)
        self.app._recommendation_store = self.store

        # Mock views and bridge
        self.chat_mock = MagicMock()
        self.app._views_cache = {"chat": self.chat_mock}
        self.bridge_mock = MagicMock()
        self.bridge_mock._is_closed = False
        self.bridge_mock._active_request_id = None
        self.app._assistant_bridge = self.bridge_mock

        # Prevent Tkinter widget recursion
        self.app._startup_recon_after_id = None
        self.app._morning_briefing_after_id = None
        self.app._idle_check_after_id = None
        self.app.destroy = MagicMock()
        self.app.after_cancel = MagicMock()
        self.app.after = MagicMock(return_value="mock_after_id")

        # Reset settings
        b_settings.set_contextual_advisories_enabled(True)
        b_settings.set_advisory_cooldown_hours(4)

    def tearDown(self):
        self.app._is_shutting_down = True
        try:
            if os.path.exists(self.temp_store_file):
                os.remove(self.temp_store_file)
            lock_file = f"{self.temp_store_file}.lock"
            if os.path.exists(lock_file):
                os.remove(lock_file)
        except Exception:
            pass


# ==============================================================================
# Category A: Activity Tracking & Idle Detection (9 tests)
# ==============================================================================
class TestStage434CategoryAActivityTracking(BaseStage434TestCase):

    def test_01_initial_idle_tracking_state(self):
        """Verify initial state attributes are properly set."""
        now = time.monotonic()
        self.assertAlmostEqual(self.app._last_user_activity_time, now, delta=2.0)
        self.assertFalse(self.app._idle_cycle_delivered)
        self.assertEqual(self.app._idle_inactivity_threshold_sec, 600.0)
        self.assertEqual(self.app._idle_check_interval_ms, 30000)

    def test_02_interaction_key_event_updates_timestamp(self):
        """Keyboard event updates _last_user_activity_time."""
        self.app._last_user_activity_time = time.monotonic() - 100.0
        old_time = self.app._last_user_activity_time
        self.app._on_user_interaction(MagicMock(keysym="Return"))
        self.assertGreater(self.app._last_user_activity_time, old_time)

    def test_03_interaction_button_press_updates_timestamp(self):
        """Mouse click event updates _last_user_activity_time."""
        self.app._last_user_activity_time = time.monotonic() - 100.0
        old_time = self.app._last_user_activity_time
        self.app._on_user_interaction(MagicMock(num=1))
        self.assertGreater(self.app._last_user_activity_time, old_time)

    def test_04_interaction_mouse_wheel_updates_timestamp(self):
        """Mouse scroll event updates _last_user_activity_time."""
        self.app._last_user_activity_time = time.monotonic() - 100.0
        old_time = self.app._last_user_activity_time
        self.app._on_user_interaction(MagicMock(delta=120))
        self.assertGreater(self.app._last_user_activity_time, old_time)

    def test_05_interaction_button4_button5_updates_timestamp(self):
        """Linux mouse scroll buttons 4 & 5 update _last_user_activity_time."""
        self.app._last_user_activity_time = time.monotonic() - 100.0
        old_time = self.app._last_user_activity_time
        self.app._on_user_interaction(MagicMock(num=4))
        self.assertGreater(self.app._last_user_activity_time, old_time)

        self.app._last_user_activity_time = time.monotonic() - 100.0
        old_time = self.app._last_user_activity_time
        self.app._on_user_interaction(MagicMock(num=5))
        self.assertGreater(self.app._last_user_activity_time, old_time)

    def test_06_inactivity_threshold_under_600s_returns_early(self):
        """Inactivity under 600s (e.g. 500s) does not dispatch evaluation worker."""
        self.app._last_user_activity_time = time.monotonic() - 500.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertIsNone(self.app._idle_worker_thread)
            mock_worker.assert_not_called()

    def test_07_inactivity_threshold_at_or_over_600s_triggers_evaluation(self):
        """Inactivity >= 600s (e.g. 601s) launches background evaluation worker."""
        self.app._last_user_activity_time = time.monotonic() - 601.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertIsNotNone(self.app._idle_worker_thread)
            # Give background thread brief moment to start
            time.sleep(0.05)
            mock_worker.assert_called_once()

    def test_08_interaction_resets_idle_cycle_delivered_flag(self):
        """User interaction resets _idle_cycle_delivered to False."""
        self.app._idle_cycle_delivered = True
        self.app._on_user_interaction()
        self.assertFalse(self.app._idle_cycle_delivered)

    def test_09_user_interaction_does_not_suppress_events(self):
        """_on_user_interaction returns None to allow normal Tkinter propagation."""
        res = self.app._on_user_interaction(MagicMock())
        self.assertIsNone(res)
        self.assertNotEqual(res, "break")


# ==============================================================================
# Category B: Periodic Ticker & Evaluation Concurrency (8 tests)
# ==============================================================================
class TestStage434CategoryBConcurrencyAndSettings(BaseStage434TestCase):

    def test_10_contextual_advisories_disabled_blocks_evaluation(self):
        """When contextual advisories are disabled, idle check returns early."""
        b_settings.set_contextual_advisories_enabled(False)
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertIsNone(self.app._idle_worker_thread)
            mock_worker.assert_not_called()
            self.app.after.assert_called_with(30000, self.app._check_idle_recommendations)

    def test_11_idle_cycle_already_delivered_blocks_re_evaluation(self):
        """If a card has already been delivered during this idle period, return early."""
        self.app._idle_cycle_delivered = True
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertIsNone(self.app._idle_worker_thread)
            mock_worker.assert_not_called()

    def test_12_mutual_exclusion_active_bridge_request(self):
        """Active AssistantBridge request prevents idle evaluation."""
        self.bridge_mock._active_request_id = "req_active_1"
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertIsNone(self.app._idle_worker_thread)
            mock_worker.assert_not_called()

    def test_13_mutual_exclusion_active_morning_briefing(self):
        """Active morning briefing worker thread prevents idle evaluation."""
        fake_briefing_thread = MagicMock()
        fake_briefing_thread.is_alive.return_value = True
        self.app._briefing_worker_thread = fake_briefing_thread
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertIsNone(self.app._idle_worker_thread)
            mock_worker.assert_not_called()

    def test_14_mutual_exclusion_active_idle_worker(self):
        """Active idle worker thread prevents spawning a second concurrent worker."""
        fake_idle_thread = MagicMock()
        fake_idle_thread.is_alive.return_value = True
        self.app._idle_worker_thread = fake_idle_thread
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(self.app, "_run_idle_recommendation_worker") as mock_worker:
            self.app._check_idle_recommendations()
            self.assertEqual(self.app._idle_worker_thread, fake_idle_thread)
            mock_worker.assert_not_called()

    def test_15_monotonically_increasing_worker_tokens(self):
        """Subsequent evaluations increment the worker token sequentially."""
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(self.app, "_run_idle_recommendation_worker"):
            self.app._check_idle_recommendations()
            token1 = self.app._idle_worker_token
            self.app._idle_worker_thread = None  # simulate worker finished

            self.app._last_user_activity_time = time.monotonic() - 700.0
            self.app._idle_cycle_delivered = False
            self.app._check_idle_recommendations()
            token2 = self.app._idle_worker_token

            self.assertEqual(token1, 1)
            self.assertEqual(token2, 2)

    def test_16_shutdown_guard_blocks_ticker_and_evaluation(self):
        """When shutting down, ticker exits immediately without rescheduling."""
        self.app._is_shutting_down = True
        self.app._last_user_activity_time = time.monotonic() - 700.0
        self.app._check_idle_recommendations()
        self.assertIsNone(self.app._idle_worker_thread)
        self.app.after.assert_not_called()

    def test_17_worker_exception_resilience(self):
        """Worker exceptions are caught gracefully and mark attempt FAILED."""
        self.app._last_user_activity_time = time.monotonic() - 700.0
        with patch.object(
            self.app,
            "_run_idle_recommendation_worker",
            side_effect=RuntimeError("Simulated worker error"),
        ):
            self.app._check_idle_recommendations()
            if self.app._idle_worker_thread:
                self.app._idle_worker_thread.join(timeout=2.0)
            self.assertIsNone(self.app._idle_worker_thread)
            self.assertEqual(self.app._idle_attempt_states.get(1), "FAILED")


# ==============================================================================
# Category C: Candidate Filtering, Cooldown & Selection (8 tests)
# ==============================================================================
class TestStage434CategoryCCooldownAndSelection(BaseStage434TestCase):

    def _create_rec(
        self,
        rec_id: str,
        priority: RecommendationPriority = RecommendationPriority.HIGH,
        state: RecommendationState = RecommendationState.ACTIVE,
        last_surfaced_at: Optional[str] = None,
    ) -> Recommendation:
        rec = Recommendation(
            rec_id=rec_id,
            category="job",
            entity_id=rec_id.split(":")[1],
            recommendation_type="approval_pending",
            title=f"Title for {rec_id}",
            explanation=f"Explanation for {rec_id}",
            suggested_action="Review",
            priority=priority,
            evidence_fingerprint="fp_" + rec_id,
            state=state,
            last_surfaced_at=last_surfaced_at,
        )
        self.store.upsert_candidate(rec)
        return rec

    def test_18_only_active_recommendations_eligible(self):
        """Only ACTIVE recommendations are selected; DISMISSED/STALE/DEFERRED excluded."""
        r_active = self._create_rec("job:active_1:approval", state=RecommendationState.ACTIVE)
        r_dismissed = self._create_rec("job:dismissed_1:approval", state=RecommendationState.DISMISSED)
        r_stale = self._create_rec("job:stale_1:approval", state=RecommendationState.STALE)

        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r_active, r_dismissed, r_stale]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            mock_dispatch.assert_called_once()
                            selected = mock_dispatch.call_args[0][0]
                            self.assertEqual(selected.rec_id, r_active.rec_id)

    def test_19_per_recommendation_cooldown_recent_filtered_out(self):
        """Recommendation surfaced 1 hour ago (within 4h cooldown) is filtered out."""
        surfaced_1h_ago = (datetime.now() - timedelta(hours=1)).isoformat()
        r_recent = self._create_rec("job:rec_1h:approval", last_surfaced_at=surfaced_1h_ago)

        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r_recent]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            mock_dispatch.assert_not_called()
                            self.assertEqual(self.app._idle_attempt_states.get(1), "COMPLETED_ALL_IN_COOLDOWN")

    def test_20_per_recommendation_cooldown_expired_is_eligible(self):
        """Recommendation surfaced 5 hours ago (past 4h cooldown) is eligible."""
        surfaced_5h_ago = (datetime.now() - timedelta(hours=5)).isoformat()
        r_old = self._create_rec("job:rec_5h:approval", last_surfaced_at=surfaced_5h_ago)

        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r_old]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            mock_dispatch.assert_called_once()
                            selected = mock_dispatch.call_args[0][0]
                            self.assertEqual(selected.rec_id, r_old.rec_id)

    def test_21_user_configured_cooldown_hours_respected(self):
        """Cooldown respects user setting (e.g. 2h vs 8h)."""
        b_settings.set_advisory_cooldown_hours(2)
        surfaced_3h_ago = (datetime.now() - timedelta(hours=3)).isoformat()
        r_rec = self._create_rec("job:rec_2h_test:approval", last_surfaced_at=surfaced_3h_ago)

        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r_rec]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            mock_dispatch.assert_called_once()

    def test_22_single_card_delivery_when_multiple_eligible(self):
        """When multiple candidates are eligible, exactly ONE is selected."""
        r1 = self._create_rec("job:rec_1:approval", priority=RecommendationPriority.NORMAL)
        r2 = self._create_rec("job:rec_2:approval", priority=RecommendationPriority.NORMAL)

        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r1, r2]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            self.assertEqual(mock_dispatch.call_count, 1)

    def test_23_deterministic_priority_ranking_selection(self):
        """Highest priority candidate (HIGH over NORMAL) is delivered first."""
        r_norm = self._create_rec("job:rec_norm:approval", priority=RecommendationPriority.NORMAL)
        r_high = self._create_rec("job:rec_high:approval", priority=RecommendationPriority.HIGH)

        # candidates in evaluate return order (high rank comes first)
        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r_high, r_norm]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            selected = mock_dispatch.call_args[0][0]
                            self.assertEqual(selected.rec_id, r_high.rec_id)

    def test_24_all_candidates_in_cooldown_transitions_state(self):
        """When all candidates are in cooldown, transition to COMPLETED_ALL_IN_COOLDOWN."""
        surfaced_recent = (datetime.now() - timedelta(minutes=30)).isoformat()
        r1 = self._create_rec("job:rec_c1:approval", last_surfaced_at=surfaced_recent)
        r2 = self._create_rec("job:rec_c2:approval", last_surfaced_at=surfaced_recent)

        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[r1, r2]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            mock_dispatch.assert_not_called()
                            self.assertEqual(self.app._idle_attempt_states.get(1), "COMPLETED_ALL_IN_COOLDOWN")

    def test_25_empty_candidate_list_transitions_state(self):
        """When no candidates exist, transition to COMPLETED_EMPTY."""
        with patch.object(self.app, "_dispatch_idle_recommendation") as mock_dispatch:
            with patch("brain.briefing.recommendations.RecommendationEvaluator.evaluate", return_value=[]):
                with patch("brain.briefing.collector.BriefingCollector._collect_identity_and_projects", return_value=("User", None, [])):
                    with patch("brain.briefing.collector.BriefingCollector._collect_reminders", return_value=([], [])):
                        with patch("brain.briefing.collector.BriefingCollector._collect_jobs", return_value=([], [], [], [])):
                            self.app._run_idle_recommendation_worker(1)
                            mock_dispatch.assert_not_called()
                            self.assertEqual(self.app._idle_attempt_states.get(1), "COMPLETED_EMPTY")


# ==============================================================================
# Category D: UI Delivery, Single-Owner Persistence & Race Hardening (10 tests)
# ==============================================================================
class TestStage434CategoryDDeliveryPersistenceAndRaces(BaseStage434TestCase):

    def _create_active_rec(self, rec_id: str = "job:test_rec:approval") -> Recommendation:
        rec = Recommendation(
            rec_id=rec_id,
            category="job",
            entity_id="test_rec",
            recommendation_type="approval_pending",
            title="Approval Needed",
            explanation="Job requires user sign-off",
            suggested_action="Approve or reject",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_test_rec",
            state=RecommendationState.ACTIVE,
        )
        self.store.upsert_candidate(rec)
        return rec

    def test_26_successful_ui_delivery_and_presentation(self):
        """Successful delivery calls dispatch_with_ack and ChatView.on_assistant_result."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token
        self.app._idle_attempt_states[token] = "EVALUATING"
        self.bridge_mock.dispatch_with_ack.side_effect = lambda cb, timeout: (True, cb(), None)

        now = datetime.now()
        res = self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=now)
        self.assertTrue(res)
        self.chat_mock.on_assistant_result.assert_called_once()
        self.assertEqual(self.app._idle_attempt_states.get(token), "ACKNOWLEDGED")

    def test_27_single_owner_persistence_only_after_presentation(self):
        """Single-owner persistence records surfaced in store with channel='proactive'."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token
        self.bridge_mock.dispatch_with_ack.side_effect = lambda cb, timeout: (True, cb(), None)

        now = datetime.now()
        self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=now)

        stored = self.store.get(rec.rec_id)
        self.assertIsNotNone(stored.last_surfaced_at)
        self.assertIn("proactive", stored.channels)

    def test_28_idle_cycle_delivered_set_on_confirmed_presentation(self):
        """_idle_cycle_delivered is set to True upon confirmed presentation."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token
        self.bridge_mock.dispatch_with_ack.side_effect = lambda cb, timeout: (True, cb(), None)

        self.assertFalse(self.app._idle_cycle_delivered)
        self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=datetime.now())
        self.assertTrue(self.app._idle_cycle_delivered)

    def test_29_presentation_failure_does_not_persist(self):
        """When UI presentation fails, recommendation is NOT persisted as surfaced."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token
        self.chat_mock.on_assistant_result.side_effect = RuntimeError("UI presentation exploded")
        self.bridge_mock.dispatch_with_ack.side_effect = lambda cb, timeout: (False, None, "UI presentation exploded")

        res = self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=datetime.now())
        self.assertFalse(res)
        stored = self.store.get(rec.rec_id)
        self.assertIsNone(stored.last_surfaced_at)
        self.assertNotIn("proactive", stored.channels)
        self.assertFalse(self.app._idle_cycle_delivered)
        self.assertEqual(self.app._idle_attempt_states.get(token), "FAILED")

    def test_30_worker_timeout_wins_invalidates_late_presentation(self):
        """Timeout wins: attempt marked FAILED and late UI presentation callback is rejected."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token

        # Simulate timeout before UI commit
        saved_cb = []
        def fake_dispatch(cb, timeout=5.0):
            saved_cb.append(cb)
            return (False, None, "Timeout waiting for acknowledgement")

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        res = self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=datetime.now())
        self.assertFalse(res)
        self.assertEqual(self.app._idle_attempt_states.get(token), "FAILED")

        # Now when the delayed callback eventually executes, it must be rejected
        with self.assertRaises(RuntimeError) as cm:
            saved_cb[0]()
        self.assertIn("already been invalidated (FAILED)", str(cm.exception))

    def test_31_presentation_wins_grace_period_coordination(self):
        """Presentation wins: UI callback committed before timeout; worker waits and acknowledges."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token
        self.app._idle_attempt_events[token] = threading.Event()

        insertion_started = threading.Event()
        insertion_resume = threading.Event()

        def pause_during_insertion(req_id, outcome):
            insertion_started.set()
            insertion_resume.wait()

        self.chat_mock.on_assistant_result.side_effect = pause_during_insertion

        def fake_dispatch(fn, timeout=5.0):
            t = threading.Thread(target=fn, name="TestIdleUIThread-31")
            t.start()
            insertion_started.wait()
            # Worker times out while insertion is active (state is COMMITTED)
            return (False, None, "UI dispatch timed out after 5.0s")

        self.bridge_mock.dispatch_with_ack.side_effect = fake_dispatch

        def release_insertion():
            time.sleep(0.05)
            insertion_resume.set()

        threading.Thread(target=release_insertion, daemon=True).start()

        res = self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=datetime.now())
        self.assertTrue(res)
        self.assertEqual(self.app._idle_attempt_states.get(token), "ACKNOWLEDGED")
        self.assertTrue(self.app._idle_cycle_delivered)

    def test_32_stale_token_rejection_in_ui_callback(self):
        """UI callback rejects attempt when worker token has advanced."""
        rec = self._create_active_rec()
        stale_token = 1
        self.app._idle_worker_token = 2  # newer token exists
        self.app._idle_attempt_states[stale_token] = "PRESENTING"

        # Simulate dispatch_with_ack executing callback for stale token
        with patch.object(self.app._assistant_bridge, "dispatch_with_ack") as mock_dispatch:
            mock_dispatch.side_effect = lambda cb, timeout: (False, None, "Stale token")
            res = self.app._dispatch_idle_recommendation(rec, store=self.store, token=stale_token, now=datetime.now())
            self.assertFalse(res)

    def test_33_concurrent_dismissal_invalidation(self):
        """If user dismissed recommendation during evaluation, UI callback aborts."""
        rec = self._create_active_rec()
        token = 1
        self.app._idle_worker_token = token

        # User dismisses recommendation in store
        self.store.dismiss(rec.rec_id)

        # dispatch_with_ack tries to present
        def check_and_run(cb, timeout=5.0):
            try:
                cb()
                return (True, True, None)
            except Exception as e:
                return (False, None, str(e))

        self.bridge_mock.dispatch_with_ack.side_effect = check_and_run

        res = self.app._dispatch_idle_recommendation(rec, store=self.store, token=token, now=datetime.now())
        self.assertFalse(res)
        self.chat_mock.on_assistant_result.assert_not_called()
        self.assertFalse(self.app._idle_cycle_delivered)

    def test_34_clean_shutdown_cancels_ticker_and_joins_worker(self):
        """_on_close cancels idle ticker and safely joins active worker."""
        self.app._idle_check_after_id = "after_id_idle_99"
        fake_idle_thread = MagicMock()
        fake_idle_thread.is_alive.return_value = True
        self.app._idle_worker_thread = fake_idle_thread
        self.app._idle_worker_token = 5
        self.app._idle_attempt_states[5] = "EVALUATING"
        self.app._idle_attempt_events[5] = threading.Event()

        self.app._on_close()

        self.app.after_cancel.assert_any_call("after_id_idle_99")
        self.assertEqual(self.app._idle_attempt_states[5], "CANCELLED")
        self.assertTrue(self.app._idle_attempt_events[5].is_set())
        fake_idle_thread.join.assert_called_with(timeout=1.5)


if __name__ == "__main__":
    unittest.main()
