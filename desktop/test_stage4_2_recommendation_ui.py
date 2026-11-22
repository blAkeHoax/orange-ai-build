"""
test_stage4_2_recommendation_ui.py - Comprehensive Test Suite for Stage 4.2 Recommendation UI & Action Cards.

Covers:
1. Recommendation cards render the correct recommendation content.
2. Priority and lifecycle states display correctly.
3. Dismiss invokes the existing seven-day dismissal behavior.
4. Snooze invokes the existing 24-hour deferral behavior.
5. Mark Handled persists the HANDLED transition.
6. View Evidence displays actual evidence.
7. Missing evidence produces a safe empty state.
8. UI refreshes after a successful transition.
9. Failed transitions produce appropriate feedback.
10. Stale recommendations cannot be incorrectly transitioned.
11. Repeated actions do not corrupt lifecycle state.
12. Existing recommendation persistence and identity behavior remain unchanged.
13. UI interactions cannot invoke tools or bypass permissions.

Uses isolated temporary JSON stores and mock environments. Never modifies production stores or databases.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import customtkinter as ctk

from brain.briefing.recommendations import (
    Recommendation,
    RecommendationPriority,
    RecommendationState,
    RecommendationCategory,
    RecommendationStore,
)
from gui.components.recommendation_card import RecommendationCard
from gui.components.message_timeline import MessageTimeline
from gui.views.chat_view import ChatView


class TestStage42RecommendationUI(unittest.TestCase):
    """Test suite validating Stage 4.2 interactive recommendation cards and lifecycle UI."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("dark")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = os.path.join(self.temp_dir.name, "recommendations.json")
        self.store = RecommendationStore(file_path=self.store_path)

        # Create a sample test recommendation in isolated store
        self.sample_rec = Recommendation(
            rec_id="project_health:project_alpha:blocked",
            category=RecommendationCategory.PROJECT_BLOCKED.value,
            entity_id="project_alpha",
            recommendation_type="blocked",
            title="Project Alpha Blocked on Staging",
            explanation="Project Alpha has 2 failed deployment runs in the last 24 hours.",
            suggested_action="Check the staging runner logs and retry step 2.",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="abc123def4567890",
            evidence=[
                "Step 2 failed with exit code 1 at 2026-11-14T10:00:00Z",
                "Runner timeout observed after 300 seconds",
            ],
            state=RecommendationState.ACTIVE,
        )
        self.store.upsert_candidate(self.sample_rec)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_card_renders_correct_content(self):
        """1. Recommendation cards render the correct recommendation content."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        self.assertEqual(card._title_lbl.cget("text"), "Project Alpha Blocked on Staging")
        self.assertIn("Project Alpha has 2 failed deployment runs", card._exp_lbl.cget("text"))
        self.assertIn("Check the staging runner logs", card._sugg_lbl.cget("text"))
        self.assertEqual(card._id_lbl.cget("text"), f"#{self.sample_rec.short_id}")
        self.assertIn("Project Blocked", card._category_lbl.cget("text"))
        card.destroy()

    def test_02_priority_and_lifecycle_states_display(self):
        """2. Priority and lifecycle states display correctly."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        self.assertEqual(card._priority_lbl.cget("text"), "HIGH")
        self.assertEqual(card._state_lbl.cget("text"), "ACTIVE")

        # Test Critical priority color mapping
        crit_rec = Recommendation(
            rec_id="job_confirmation:00182864a197:approval_pending",
            category="job_confirmation",
            entity_id="00182864a197",
            recommendation_type="approval_pending",
            title="Approval Pending",
            explanation="Confirmation required",
            suggested_action="Review",
            priority=RecommendationPriority.CRITICAL,
            evidence_fingerprint="crit_fp",
        )
        card_crit = RecommendationCard(self.root, crit_rec, store=self.store)
        self.assertEqual(card_crit._get_priority_color(RecommendationPriority.CRITICAL), "#E74C3C")
        self.assertEqual(card_crit._priority_lbl.cget("text"), "CRITICAL")

        card.destroy()
        card_crit.destroy()

    def test_03_dismiss_invokes_seven_day_dismissal(self):
        """3. Dismiss invokes the existing seven-day dismissal behavior."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
        with patch("gui.components.recommendation_card.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            card._on_dismiss_clicked()

        persisted = self.store.get(self.sample_rec.rec_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.state, RecommendationState.DISMISSED)
        self.assertIsNotNone(persisted.suppressed_until)

        # Verify suppression is approximately 7 days from the mock/action reference date
        supp_dt = datetime.fromisoformat(persisted.suppressed_until)
        self.assertGreaterEqual((supp_dt.date() - now.date()).days, 6)

        self.assertEqual(card._state_lbl.cget("text"), "DISMISSED")
        self.assertIn("Dismissed for 7 days", card._feedback_lbl.cget("text"))
        card.destroy()

    def test_04_snooze_invokes_twenty_four_hour_deferral(self):
        """4. Snooze invokes the existing 24-hour deferral behavior."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        card._on_snooze_clicked()

        persisted = self.store.get(self.sample_rec.rec_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.state, RecommendationState.DEFERRED)
        self.assertIsNotNone(persisted.suppressed_until)

        supp_dt = datetime.fromisoformat(persisted.suppressed_until)
        # Should be deferred ~24 hours into the future
        self.assertGreater(supp_dt, datetime.now())

        self.assertEqual(card._state_lbl.cget("text"), "DEFERRED")
        self.assertIn("Snoozed for 24 hours", card._feedback_lbl.cget("text"))
        card.destroy()

    def test_05_mark_handled_persists_handled_transition(self):
        """5. Mark Handled persists the HANDLED transition."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        card._on_handle_clicked()

        persisted = self.store.get(self.sample_rec.rec_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.state, RecommendationState.HANDLED)
        self.assertEqual(persisted.handled_fingerprint, self.sample_rec.evidence_fingerprint)
        self.assertIsNone(persisted.suppressed_until)

        self.assertEqual(card._state_lbl.cget("text"), "HANDLED")
        self.assertIn("Marked Handled", card._feedback_lbl.cget("text"))
        card.destroy()

    def test_06_view_evidence_displays_actual_evidence(self):
        """6. View Evidence displays actual evidence."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        # Initially collapsed
        self.assertFalse(card._is_evidence_visible)

        # Toggle open
        card._toggle_evidence_view()
        self.assertTrue(card._is_evidence_visible)
        self.assertEqual(card._evidence_btn.cget("text"), "Hide Evidence ▴")

        # Inspect children of evidence container
        labels = [w.cget("text") for w in card._evidence_container.winfo_children() if isinstance(w, ctk.CTkLabel)]
        all_text = " ".join(labels)
        self.assertIn("Step 2 failed with exit code 1", all_text)
        self.assertIn("Runner timeout observed", all_text)
        self.assertIn(self.sample_rec.entity_id, all_text)

        # Toggle closed
        card._toggle_evidence_view()
        self.assertFalse(card._is_evidence_visible)
        self.assertEqual(card._evidence_btn.cget("text"), "View Evidence ▾")

        card.destroy()

    def test_07_missing_evidence_produces_safe_empty_state(self):
        """7. Missing evidence produces a safe empty state."""
        rec_no_ev = Recommendation(
            rec_id="reminders:rem_1:overdue",
            category="reminder",
            entity_id="rem_1",
            recommendation_type="overdue",
            title="Overdue Tax Reminder",
            explanation="Reminder is past due.",
            suggested_action="Review reminder.",
            priority=RecommendationPriority.NORMAL,
            evidence_fingerprint="fp_rem",
            evidence=[],  # Empty evidence
        )
        card = RecommendationCard(self.root, rec_no_ev, store=self.store)

        card._toggle_evidence_view()
        labels = [w.cget("text") for w in card._evidence_container.winfo_children() if isinstance(w, ctk.CTkLabel)]
        all_text = " ".join(labels)

        self.assertIn("No underlying evidence signals recorded", all_text)
        card.destroy()

    def test_08_ui_refreshes_after_successful_transition(self):
        """8. UI refreshes after a successful transition."""
        card = RecommendationCard(self.root, self.sample_rec, store=self.store)

        # Pre-action state: buttons enabled
        self.assertEqual(card._dismiss_btn.cget("state"), "normal")
        self.assertEqual(card._snooze_btn.cget("state"), "normal")
        self.assertEqual(card._handle_btn.cget("state"), "normal")

        # Perform action
        card._on_dismiss_clicked()

        # Post-action state: buttons disabled, feedback shown, state updated
        self.assertEqual(card._dismiss_btn.cget("state"), "disabled")
        self.assertEqual(card._snooze_btn.cget("state"), "disabled")
        self.assertEqual(card._handle_btn.cget("state"), "disabled")
        self.assertEqual(card._state_lbl.cget("text"), "DISMISSED")
        self.assertTrue(card._is_transitioned)

        card.destroy()

    def test_09_failed_transitions_produce_appropriate_feedback(self):
        """9. Failed transitions produce appropriate feedback."""
        mock_store = MagicMock()
        mock_store.dismiss.return_value = False
        mock_store.defer.return_value = False
        mock_store.handle.return_value = False

        card = RecommendationCard(self.root, self.sample_rec, store=mock_store)

        card._on_dismiss_clicked()
        self.assertIn("Could not dismiss recommendation", card._feedback_lbl.cget("text"))

        card._on_snooze_clicked()
        self.assertIn("Could not snooze recommendation", card._feedback_lbl.cget("text"))

        card._on_handle_clicked()
        self.assertIn("Could not mark handled", card._feedback_lbl.cget("text"))

        # Buttons should still be normal when an error occurs
        self.assertEqual(card._dismiss_btn.cget("state"), "normal")

        card.destroy()

    def test_10_stale_recommendations_cannot_be_incorrectly_transitioned(self):
        """10. Stale recommendations cannot be incorrectly transitioned."""
        stale_rec = Recommendation(
            rec_id="project_health:project_beta:stale",
            category="project_health",
            entity_id="project_beta",
            recommendation_type="resolved",
            title="Project Beta Inactive",
            explanation="Old stale record",
            suggested_action="None",
            priority=RecommendationPriority.LOW,
            evidence_fingerprint="fp_beta",
            state=RecommendationState.STALE,  # Already stale
        )
        card = RecommendationCard(self.root, stale_rec, store=self.store)

        # Since it's already transitioned/stale, buttons are disabled
        self.assertTrue(card._is_transitioned)
        self.assertEqual(card._dismiss_btn.cget("state"), "disabled")
        self.assertEqual(card._snooze_btn.cget("state"), "disabled")
        self.assertEqual(card._handle_btn.cget("state"), "disabled")

        # Clicks are ignored
        card._on_dismiss_clicked()
        persisted = self.store.get(stale_rec.rec_id)
        self.assertIsNone(persisted)  # Was not updated

        card.destroy()

    def test_11_repeated_actions_do_not_corrupt_lifecycle_state(self):
        """11. Repeated actions do not corrupt lifecycle state."""
        callback_mock = MagicMock()
        card = RecommendationCard(self.root, self.sample_rec, store=self.store, on_action_callback=callback_mock)

        # First click succeeds
        card._on_handle_clicked()
        self.assertEqual(callback_mock.call_count, 1)

        # Second click immediately ignored
        card._on_handle_clicked()
        card._on_dismiss_clicked()
        card._on_snooze_clicked()
        self.assertEqual(callback_mock.call_count, 1)

        persisted = self.store.get(self.sample_rec.rec_id)
        self.assertEqual(persisted.state, RecommendationState.HANDLED)

        card.destroy()

    def test_12_existing_recommendation_persistence_and_identity_unchanged(self):
        """12. Existing recommendation persistence and identity behavior remain unchanged."""
        # Check dict conversion and round-trip
        data = self.sample_rec.to_dict()
        reconstituted = Recommendation.from_dict(data)

        self.assertEqual(reconstituted.rec_id, self.sample_rec.rec_id)
        self.assertEqual(reconstituted.short_id, self.sample_rec.short_id)
        self.assertEqual(reconstituted.evidence_fingerprint, self.sample_rec.evidence_fingerprint)
        self.assertEqual(reconstituted.evidence, self.sample_rec.evidence)

        # Check short ID parsing
        self.assertEqual(self.sample_rec.short_id, "project_")  # first 8 chars of entity_id

    def test_13_ui_interactions_cannot_invoke_tools_or_bypass_permissions(self):
        """13. UI interactions cannot invoke tools or bypass permissions."""
        with patch("tools.registry.registry.execute") as mock_tool_exec, \
             patch("brain.jobs.runtime.JobRuntime.execute_job") as mock_job_exec:

            card = RecommendationCard(self.root, self.sample_rec, store=self.store)

            card._on_dismiss_clicked()
            card._on_snooze_clicked()
            card._on_handle_clicked()
            card._toggle_evidence_view()

            # Zero tool executions and zero job executions
            mock_tool_exec.assert_not_called()
            mock_job_exec.assert_not_called()

            card.destroy()

    def test_14_message_timeline_and_chat_view_integration(self):
        """14. Verify timeline embedding and ChatView result routing."""
        scroll_frame = ctk.CTkScrollableFrame(self.root)
        timeline = MessageTimeline(scroll_frame)

        card = timeline.add_recommendation_card(self.sample_rec, store=self.store)
        self.assertIsInstance(card, RecommendationCard)
        self.assertEqual(card.rec.rec_id, self.sample_rec.rec_id)

        # Test ChatView on_assistant_result routing
        chat_view = ChatView(self.root)
        outcome = {
            "reply": "Here is the result.\n\n💡 **Orange AI Advisory** [HIGH]: **Project Alpha Blocked on Staging**\n   Check the runner logs.",
            "model": "test-model",
            "type": "assistant",
            "recommendation": self.sample_rec.to_dict(),
        }

        initial_count = timeline.get_message_count()
        chat_view.on_assistant_result("req_0001", outcome)

        # Timeline has received the message and recommendation card
        self.assertGreater(chat_view._timeline.get_message_count(), 0)

        scroll_frame.destroy()
        chat_view.destroy()


if __name__ == "__main__":
    unittest.main()
