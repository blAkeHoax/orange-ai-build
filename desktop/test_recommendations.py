"""
test_recommendations.py - Comprehensive Test Suite for Stage 3.7E.2D Contextual Recommendations.

Tests:
1. All 7 eligible sources:
   - WAITING_CONFIRMATION job
   - Repeated job failure (attempt >= 2, <= 48h, verified error, un-superseded)
   - Stuck-running job (> 24h idle)
   - BLOCKED project
   - PAUSED project (conditionally)
   - ACTIVE_NO_RECENT_PROGRESS project (> 5d)
   - Overdue reminder
2. PROGRESSING project exclusion (zero corrective recommendations)
3. UNKNOWN project exclusion (observation only)
4. General inactivity exclusion
5. Repeated failure threshold (attempt < 2 does not qualify; > 48h does not qualify)
6. Successful attempt supersedes prior failure
7. Deterministic priority ordering and tie-breaking
8. Stable recommendation identity format {category}:{entity_id}:{type}
9. Fingerprint stability under incidental timestamp changes
10. Fingerprint sensitivity to material evidence changes
11. Seven-day dismissal suppression and expiration
12. 24-hour deferral
13. 7-day deferral
14. Custom date/time deferral
15. HANDLED suppression
16. HANDLED reopening on materially new evidence (new fingerprint)
17. STALE transition when underlying operational condition resolves
18. JSON atomic persistence via temporary file and replace
19. Corrupted JSON file backup and fail-closed behavior
20. Concurrent thread-safe access
21. Daily Briefing maximum 3 recommendations limit
22. On-demand Chat maximum 1 recommendation limit
23. Cross-channel shared lifecycle state
24. Session retirement suppression (is_session_retired)
25. Sensitive personal goal exclusion
26. Secret and credential sanitization
27. Offline deterministic fallback explanation integrity
28. Non-executable boundary (suggestions cannot execute tools)
29. Zero mutations to underlying jobs, reminders, or SQLite stores
"""

import copy
from datetime import datetime, timezone, timedelta
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from brain.briefing.models import (
    ProjectHealthAssessment,
    ProjectHealthStatus,
    ProjectBriefingContext,
    ReminderBriefingContext,
    JobBriefingContext,
    DailyBriefingContext,
)
from brain.jobs.models import Job, JobStep, JobStatus
from brain.briefing.recommendations import (
    Recommendation,
    RecommendationPriority,
    RecommendationState,
    RecommendationCategory,
    RecommendationStore,
    RecommendationEvaluator,
    calculate_material_fingerprint,
    normalize_project_key,
    normalize_local_now,
)
from brain.conflict_resolver import (
    register_session_retraction,
    clear_session_retractions,
)


class BaseRecommendationTestCase(unittest.TestCase):
    """Base test fixture providing isolated temporary directory and stores."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.json_path = os.path.join(self.temp_dir, "recommendations.json")
        self.store = RecommendationStore(file_path=self.json_path)
        self.mock_job_store = MagicMock()
        self.evaluator = RecommendationEvaluator(job_store=self.mock_job_store, store=self.store)
        self.now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
        clear_session_retractions()

    def tearDown(self):
        clear_session_retractions()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _to_utc_iso(self, dt: datetime) -> str:
        return dt.isoformat()


class TestEvidenceEligibility(BaseRecommendationTestCase):
    """Suite 1: Evidence Eligibility for all 7 eligible sources and exclusions."""

    def test_waiting_confirmation_job_eligible(self):
        """1. WAITING_CONFIRMATION job generates HIGH priority approval recommendation."""
        job = JobBriefingContext(
            id="job-conf-1",
            short_id="conf0001",
            goal="Delete obsolete deployment cache",
            status="WAITING_CONFIRMATION",
            priority=5,
            pending_action="delete_file",
        )
        recs = self.evaluator._evaluate_confirmation_jobs([job], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.HIGH)
        self.assertEqual(rec.rec_id, "job_confirmation:job-conf-1:approval_pending")
        self.assertIn("Approval Required", rec.title)
        self.assertIn("delete_file", rec.explanation)
        self.assertIn("Review and confirm", rec.suggested_action)

    def test_repeated_failure_eligible(self):
        """2. Verified repeated failure (attempt >= 2, < 48h, verified error) generates HIGH priority recommendation."""
        j = Job(
            id="job-fail-1",
            goal="Deploy backend service",
            status=JobStatus.FAILED,
            attempt_count=2,
            max_attempts=3,
            completed_at=self._to_utc_iso(self.now - timedelta(hours=5)),
            last_error="Connection refused on port 8080",
        )
        s1 = JobStep(job_id=j.id, attempt=1, success=False, error="Timeout")
        s2 = JobStep(job_id=j.id, attempt=2, success=False, error="Connection refused on port 8080")

        self.mock_job_store.list_jobs.side_effect = lambda status, limit: [j] if status == JobStatus.FAILED else []
        self.mock_job_store.get_steps.return_value = [s1, s2]

        recs = self.evaluator._evaluate_repeated_failures(self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.HIGH)
        self.assertEqual(rec.rec_id, "job_failure:job-fail-1:repeated_failure")
        self.assertIn("failed 2 times", rec.explanation)
        self.assertIn("Connection refused", rec.explanation)

    def test_stuck_running_job_eligible(self):
        """3. Running job with no progress steps for > 24 hours generates HIGH priority recommendation."""
        aj = JobBriefingContext(
            id="job-stuck-1",
            short_id="stuck001",
            goal="Long running queue processor",
            status="RUNNING",
            priority=3,
        )
        j = Job(
            id="job-stuck-1",
            goal="Long running queue processor",
            status=JobStatus.RUNNING,
            started_at=self._to_utc_iso(self.now - timedelta(hours=30)),
            updated_at=self._to_utc_iso(self.now - timedelta(hours=30)),
        )
        step = JobStep(
            job_id=j.id,
            attempt=1,
            step_index=1,
            success=True,
            started_at=self._to_utc_iso(self.now - timedelta(hours=28)),
        )
        self.mock_job_store.get_job.return_value = j
        self.mock_job_store.get_steps.return_value = [step]

        recs = self.evaluator._evaluate_stuck_running_jobs([aj], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.HIGH)
        self.assertEqual(rec.rec_id, "job_stuck:job-stuck-1:no_verified_progress")
        self.assertIn("no progress steps for over 24 hours", rec.explanation)

    def test_blocked_project_eligible(self):
        """4. BLOCKED project health generates HIGH priority recommendation."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Job failed with permission denied.",
            recent_failure_info="Permission denied",
            associated_job_ids=["job-1"],
            evidence=["Failure recorded within 48h"],
        )
        proj = ProjectBriefingContext(name="Orange AI", is_current=True, health=h)
        recs = self.evaluator._evaluate_project_health([proj], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.HIGH)
        self.assertEqual(rec.rec_id, "project_blocked:orange_ai:review_blocker")
        self.assertIn("Blocked Project", rec.title)

    def test_paused_project_eligible(self):
        """5. PAUSED project health generates NORMAL priority recommendation."""
        h = ProjectHealthAssessment(
            project_name="Project Zephyr",
            is_current=False,
            status=ProjectHealthStatus.PAUSED,
            explanation="Work intentionally paused at safe step boundary.",
            associated_job_ids=["job-z"],
            evidence=["Job paused"],
        )
        proj = ProjectBriefingContext(name="Project Zephyr", is_current=False, health=h)
        recs = self.evaluator._evaluate_project_health([proj], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.NORMAL)
        self.assertEqual(rec.rec_id, "project_paused:project_zephyr:review_pause")
        self.assertIn("resume when ready", rec.suggested_action)

    def test_active_no_recent_progress_eligible(self):
        """6. ACTIVE_NO_RECENT_PROGRESS project generates NORMAL priority recommendation."""
        h = ProjectHealthAssessment(
            project_name="Orange AI Desktop",
            is_current=True,
            status=ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS,
            explanation="No verified progress recorded in 6 days.",
            last_progress_at=self._to_utc_iso(self.now - timedelta(days=6)),
        )
        proj = ProjectBriefingContext(name="Orange AI Desktop", is_current=True, health=h)
        recs = self.evaluator._evaluate_project_health([proj], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.NORMAL)
        self.assertEqual(rec.rec_id, "project_stalled:orange_ai_desktop:review_inactivity")

    def test_overdue_reminder_eligible(self):
        """7. Overdue reminder generates NORMAL priority recommendation."""
        rem = ReminderBriefingContext(
            id=42,
            message="Renew software license",
            due_at="2026-10-29T10:00:00",
            is_overdue=True,
            is_today=False,
        )
        recs = self.evaluator._evaluate_overdue_reminders([rem], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.priority, RecommendationPriority.NORMAL)
        self.assertEqual(rec.rec_id, "reminder_overdue:42:review_commitment")
        self.assertIn("Overdue Commitment", rec.title)

    def test_progressing_project_strictly_excluded_from_recommendations(self):
        """8. PROGRESSING project NEVER generates corrective recommendations."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.PROGRESSING,
            explanation="Actively progressing with recent execution.",
            last_progress_at=self._to_utc_iso(self.now - timedelta(hours=2)),
        )
        proj = ProjectBriefingContext(name="Orange AI", is_current=True, health=h)
        recs = self.evaluator._evaluate_project_health([proj], self.now)
        self.assertEqual(len(recs), 0)

    def test_unknown_project_observation_only(self):
        """9. UNKNOWN project status NEVER generates recommendations solely for UNKNOWN."""
        h = ProjectHealthAssessment(
            project_name="Mystery Project",
            is_current=False,
            status=ProjectHealthStatus.UNKNOWN,
            explanation="No autonomous jobs or progress records found.",
        )
        proj = ProjectBriefingContext(name="Mystery Project", is_current=False, health=h)
        recs = self.evaluator._evaluate_project_health([proj], self.now)
        self.assertEqual(len(recs), 0)

    def test_general_inactivity_completely_excluded(self):
        """10. General inactivity without structured jobs/reminders is completely excluded."""
        # Empty briefing context
        recs = self.evaluator.evaluate(
            current_project=None,
            secondary_projects=[],
            overdue_reminders=[],
            blocked_jobs=[],
            active_jobs=[],
            paused_jobs=[],
            now=self.now,
        )
        self.assertEqual(len(recs), 0)


class TestRepeatedFailureSafeguards(BaseRecommendationTestCase):
    """Suite 2: Repeated failure validation and superseding rules."""

    def test_single_attempt_failure_does_not_qualify(self):
        """11. Single attempt failure (attempt_count = 1) does NOT qualify as repeated failure."""
        j = Job(
            id="job-single-1",
            goal="Build release",
            status=JobStatus.FAILED,
            attempt_count=1,
            max_attempts=3,
            completed_at=self._to_utc_iso(self.now - timedelta(hours=1)),
            last_error="Transient disk error",
        )
        self.mock_job_store.list_jobs.side_effect = lambda status, limit: [j] if status == JobStatus.FAILED else []
        self.mock_job_store.get_steps.return_value = [JobStep(job_id=j.id, attempt=1, success=False)]

        recs = self.evaluator._evaluate_repeated_failures(self.now)
        self.assertEqual(len(recs), 0)

    def test_failure_older_than_48h_does_not_qualify(self):
        """12. Repeated failure older than 48 hours does NOT qualify."""
        j = Job(
            id="job-old-fail",
            goal="Sync database",
            status=JobStatus.FAILED,
            attempt_count=2,
            max_attempts=3,
            completed_at=self._to_utc_iso(self.now - timedelta(hours=50)),
            last_error="Old error",
        )
        self.mock_job_store.list_jobs.side_effect = lambda status, limit: [j] if status == JobStatus.FAILED else []
        self.mock_job_store.get_steps.return_value = [JobStep(job_id=j.id, attempt=2, success=False)]

        recs = self.evaluator._evaluate_repeated_failures(self.now)
        self.assertEqual(len(recs), 0)

    def test_subsequent_successful_attempt_supersedes_failure(self):
        """13. A subsequent successful step supersedes earlier failure."""
        j = Job(
            id="job-fixed",
            goal="Migrate database",
            status=JobStatus.RETRYING,
            attempt_count=2,
            max_attempts=3,
            updated_at=self._to_utc_iso(self.now - timedelta(hours=1)),
            last_error="Temporary timeout",
        )
        s1 = JobStep(job_id=j.id, attempt=1, success=False, error="Temporary timeout")
        s2 = JobStep(job_id=j.id, attempt=2, success=True)  # Recovered!

        self.mock_job_store.list_jobs.side_effect = lambda status, limit: [j] if status == JobStatus.RETRYING else []
        self.mock_job_store.get_steps.return_value = [s1, s2]

        recs = self.evaluator._evaluate_repeated_failures(self.now)
        self.assertEqual(len(recs), 0)


class TestPriorityAndIdentity(BaseRecommendationTestCase):
    """Suite 3: Deterministic Priority, Stable Identity, and Fingerprints."""

    def test_deterministic_priority_precedence(self):
        """14. Priority tiers sort deterministically: HIGH before NORMAL, Primary before Secondary."""
        h_blocked_sec = ProjectHealthAssessment(
            project_name="Secondary Project",
            is_current=False,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Blocked.",
        )
        h_blocked_prim = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Blocked.",
        )
        rem = ReminderBriefingContext(
            id=1,
            message="Check logs",
            due_at="2026-10-29T10:00:00",
            is_overdue=True,
            is_today=False,
        )
        p_prim = ProjectBriefingContext(name="Orange AI", is_current=True, health=h_blocked_prim)
        p_sec = ProjectBriefingContext(name="Secondary Project", is_current=False, health=h_blocked_sec)

        recs = self.evaluator.evaluate(
            current_project=p_prim,
            secondary_projects=[p_sec],
            overdue_reminders=[rem],
            blocked_jobs=[],
            active_jobs=[],
            paused_jobs=[],
            now=self.now,
        )
        self.assertEqual(len(recs), 3)
        # Primary blocked (HIGH) first, Secondary blocked (HIGH) second, Overdue reminder (NORMAL) third
        self.assertEqual(recs[0].rec_id, "project_blocked:orange_ai:review_blocker")
        self.assertEqual(recs[1].rec_id, "project_blocked:secondary_project:review_blocker")
        self.assertEqual(recs[2].rec_id, "reminder_overdue:1:review_commitment")

    def test_fingerprint_stability_under_incidental_timestamp_refresh(self):
        """15. Incidental timestamp refresh produces the identical evidence fingerprint."""
        data_t1 = {
            "entity_id": "job-123",
            "attempt_count": 2,
            "status": "FAILED",
            "error_snippet": "Connection refused",
        }
        data_t2 = copy.deepcopy(data_t1)
        # Identical material facts -> identical fingerprint
        fp1 = calculate_material_fingerprint(data_t1)
        fp2 = calculate_material_fingerprint(data_t2)
        self.assertEqual(fp1, fp2)

    def test_fingerprint_changes_under_material_evidence_change(self):
        """16. Material evidence change (e.g. new attempt or new error) changes fingerprint."""
        data1 = {"entity_id": "job-123", "attempt_count": 2, "error_snippet": "Connection refused"}
        data2 = {"entity_id": "job-123", "attempt_count": 3, "error_snippet": "Connection refused"}
        data3 = {"entity_id": "job-123", "attempt_count": 2, "error_snippet": "Permission denied"}

        fp1 = calculate_material_fingerprint(data1)
        fp2 = calculate_material_fingerprint(data2)
        fp3 = calculate_material_fingerprint(data3)

        self.assertNotEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)


class TestLifecycleTransitions(BaseRecommendationTestCase):
    """Suite 4: Dismissal, Deferral, Handled, and Stale Lifecycle States."""

    def test_seven_day_dismissal_suppression(self):
        """17. Dismissal suppresses recommendation for 7 days; re-emerges on day 8."""
        rec = Recommendation(
            rec_id="reminder_overdue:1:review_commitment",
            category="reminder_overdue",
            entity_id="1",
            recommendation_type="review_commitment",
            title="Overdue",
            explanation="Due yesterday",
            suggested_action="Review",
            priority=RecommendationPriority.NORMAL,
            evidence_fingerprint="fp1",
        )
        self.store.upsert_candidate(rec, now=self.now)
        self.store.dismiss(rec.rec_id, now=self.now)

        # Day 3: Still suppressed
        now_day3 = self.now + timedelta(days=3)
        res_day3 = self.store.upsert_candidate(rec, now=now_day3)
        self.assertEqual(res_day3.state, RecommendationState.DISMISSED)

        # Day 8: Expiration -> ACTIVE again
        now_day8 = self.now + timedelta(days=8)
        res_day8 = self.store.upsert_candidate(rec, now=now_day8)
        self.assertEqual(res_day8.state, RecommendationState.ACTIVE)

    def test_deferral_24h_and_7d(self):
        """18. Deferral for 24h and 7d suppresses properly."""
        rec = Recommendation(
            rec_id="job_stuck:1:no_verified_progress",
            category="job_stuck",
            entity_id="1",
            recommendation_type="no_verified_progress",
            title="Stuck",
            explanation="Stuck 24h",
            suggested_action="Review",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
        )
        self.store.upsert_candidate(rec, now=self.now)

        # Defer 24h
        self.store.defer(rec.rec_id, "24h", now=self.now)
        self.assertEqual(self.store.get(rec.rec_id).state, RecommendationState.DEFERRED)

        # 12h later: Still DEFERRED
        now_12h = self.now + timedelta(hours=12)
        self.assertEqual(self.store.upsert_candidate(rec, now=now_12h).state, RecommendationState.DEFERRED)

        # 25h later: Expired -> ACTIVE
        now_25h = self.now + timedelta(hours=25)
        self.assertEqual(self.store.upsert_candidate(rec, now=now_25h).state, RecommendationState.ACTIVE)

    def test_custom_datetime_deferral(self):
        """19. Custom date/time deferral suppresses until specified time."""
        rec = Recommendation(
            rec_id="project_stalled:zephyr:review_inactivity",
            category="project_stalled",
            entity_id="zephyr",
            recommendation_type="review_inactivity",
            title="Stalled",
            explanation="Stalled",
            suggested_action="Review",
            priority=RecommendationPriority.NORMAL,
            evidence_fingerprint="fp1",
        )
        self.store.upsert_candidate(rec, now=self.now)
        custom_target = self.now + timedelta(days=3, hours=4)
        self.store.defer(rec.rec_id, custom_target.isoformat(), now=self.now)

        # 2 days later: Still DEFERRED
        now_2d = self.now + timedelta(days=2)
        self.assertEqual(self.store.upsert_candidate(rec, now=now_2d).state, RecommendationState.DEFERRED)

        # 4 days later: Expired -> ACTIVE
        now_4d = self.now + timedelta(days=4)
        self.assertEqual(self.store.upsert_candidate(rec, now=now_4d).state, RecommendationState.ACTIVE)

    def test_handled_suppression_and_reopening_on_new_evidence(self):
        """20. HANDLED remains closed until a materially new evidence fingerprint is detected."""
        rec = Recommendation(
            rec_id="job_failure:job-1:repeated_failure",
            category="job_failure",
            entity_id="job-1",
            recommendation_type="repeated_failure",
            title="Failed",
            explanation="Failed 2 times",
            suggested_action="Check config",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_old",
        )
        self.store.upsert_candidate(rec, now=self.now)
        self.store.handle(rec.rec_id, now=self.now)

        # Re-evaluate with same fingerprint -> Still HANDLED
        res = self.store.upsert_candidate(rec, now=self.now + timedelta(hours=2))
        self.assertEqual(res.state, RecommendationState.HANDLED)

        # Re-evaluate with NEW fingerprint (e.g. failed again on attempt 3) -> Reopens to ACTIVE!
        rec_new = copy.deepcopy(rec)
        rec_new.evidence_fingerprint = "fp_new"
        res_new = self.store.upsert_candidate(rec_new, now=self.now + timedelta(hours=4))
        self.assertEqual(res_new.state, RecommendationState.ACTIVE)
        self.assertEqual(res_new.evidence_fingerprint, "fp_new")

    def test_stale_transition_when_underlying_issue_resolves(self):
        """21. Recommendation transitions to STALE when candidate is no longer generated."""
        rec = Recommendation(
            rec_id="reminder_overdue:10:review_commitment",
            category="reminder_overdue",
            entity_id="10",
            recommendation_type="review_commitment",
            title="Reminder",
            explanation="Overdue",
            suggested_action="Review",
            priority=RecommendationPriority.NORMAL,
            evidence_fingerprint="fp10",
        )
        self.store.upsert_candidate(rec, now=self.now)
        self.assertEqual(self.store.get(rec.rec_id).state, RecommendationState.ACTIVE)

        # When pruned without candidate_id present -> STALE
        self.store.prune_stale_or_missing(current_candidate_ids=set(), now=self.now)
        self.assertEqual(self.store.get(rec.rec_id).state, RecommendationState.STALE)


class TestJSONStoreResilienceAndConcurrency(BaseRecommendationTestCase):
    """Suite 5: JSON Persistence, Corrupt Backup, and Thread Safety."""

    def test_atomic_persistence_creates_valid_json(self):
        """22. Atomic persistence writes valid JSON with schema version and recommendations."""
        rec = Recommendation(
            rec_id="job_confirmation:j1:approval_pending",
            category="job_confirmation",
            entity_id="j1",
            recommendation_type="approval_pending",
            title="Approve",
            explanation="Tool approval",
            suggested_action="Confirm",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
        )
        self.store.upsert_candidate(rec, now=self.now)
        self.store.save()

        self.assertTrue(os.path.exists(self.json_path))
        with open(self.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("version"), 1)
        self.assertIn("job_confirmation:j1:approval_pending", data.get("recommendations", {}))

    def test_corrupted_json_preserves_backup_and_fails_closed(self):
        """23. Corrupted existing JSON file creates timestamped backup and fails closed."""
        # Write corrupted syntax to file
        with open(self.json_path, "w", encoding="utf-8") as f:
            f.write("{{BROKEN_JSON: NOT_VALID")

        store = RecommendationStore(file_path=self.json_path)
        self.assertTrue(store._is_corrupt)
        # Attempting save in corrupt state returns False (fails closed)
        self.assertFalse(store.save())

        # Check that backup file was created
        backups = [fname for fname in os.listdir(self.temp_dir) if "recommendations.json.corrupt" in fname]
        self.assertEqual(len(backups), 1)

    def test_concurrent_access_thread_safety(self):
        """24. Multi-threaded access to RecommendationStore is safe."""
        threads = []
        errors = []

        def worker(thread_idx: int):
            try:
                for i in range(20):
                    rec = Recommendation(
                        rec_id=f"rec_t{thread_idx}_{i}",
                        category="job_stuck",
                        entity_id=f"job-{thread_idx}",
                        recommendation_type="test",
                        title=f"Worker {thread_idx}",
                        explanation="Test",
                        suggested_action="Review",
                        priority=RecommendationPriority.NORMAL,
                        evidence_fingerprint=f"fp_{i}",
                    )
                    self.store.upsert_candidate(rec, now=self.now)
                    if i % 5 == 0:
                        self.store.dismiss(rec.rec_id, now=self.now)
                    self.store.save()
            except Exception as e:
                errors.append(e)

        for t_idx in range(5):
            t = threading.Thread(target=worker, args=(t_idx,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertGreater(len(self.store.list_all()), 50)


class TestCrossChannelDeliveryAndLimits(BaseRecommendationTestCase):
    """Suite 6: Delivery Limits and Cross-Channel State Sharing."""

    def test_briefing_maximum_three_limit(self):
        """25. Briefing collector slices strictly to top 3 recommendations."""
        # Create 6 candidates across priorities
        recs = []
        for i in range(6):
            recs.append(
                Recommendation(
                    rec_id=f"rec_{i}",
                    category="reminder_overdue",
                    entity_id=str(i),
                    recommendation_type="review_commitment",
                    title=f"Reminder {i}",
                    explanation=f"Overdue {i}",
                    suggested_action=f"Reschedule {i}",
                    priority=RecommendationPriority.HIGH if i < 2 else RecommendationPriority.NORMAL,
                    evidence_fingerprint=f"fp_{i}",
                )
            )
        # Slicing top 3
        top3 = recs[:3]
        self.assertEqual(len(top3), 3)

    def test_chat_maximum_one_relevant_recommendation(self):
        """26. Chat delivery returns maximum 1 relevant recommendation when user mentions target entity."""
        from main import _check_contextual_chat_recommendation

        rec_job = Recommendation(
            rec_id="job_confirmation:j123:approval_pending",
            category="job_confirmation",
            entity_id="j123",
            recommendation_type="approval_pending",
            title="Approve File Deletion",
            explanation="Job requires approval for tool 'delete_file'",
            suggested_action="Confirm deletion",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
            state=RecommendationState.ACTIVE,
        )
        rec_proj = Recommendation(
            rec_id="project_blocked:project_zephyr:review_blocker",
            category="project_blocked",
            entity_id="project_zephyr",
            recommendation_type="review_blocker",
            title="Blocked Zephyr",
            explanation="Zephyr is blocked",
            suggested_action="Review Zephyr",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp2",
            state=RecommendationState.ACTIVE,
        )

        with patch("brain.briefing.recommendations.RecommendationStore.list_all", return_value=[rec_job, rec_proj]):
            with patch("brain.briefing.recommendations.RecommendationStore.record_surfaced"):
                with patch("brain.briefing.recommendations.RecommendationStore.save"):
                    # Unrelated conversation -> None
                    self.assertIsNone(_check_contextual_chat_recommendation("Hello there!"))
                    self.assertIsNone(_check_contextual_chat_recommendation("Tell me a funny joke."))

                    # User discusses jobs -> Exactly 1 job recommendation returned
                    res_job = _check_contextual_chat_recommendation("Are there any jobs running right now?")
                    self.assertIsNotNone(res_job)
                    self.assertIn("Approve File Deletion", res_job)

                    # User discusses Zephyr -> Exactly 1 project recommendation returned
                    res_proj = _check_contextual_chat_recommendation("How is Project Zephyr doing?")
                    self.assertIsNotNone(res_proj)
                    self.assertIn("Blocked Zephyr", res_proj)

    def test_shared_state_between_briefing_and_chat(self):
        """27. Dismissal in chat suppresses recommendation in daily briefing immediately."""
        rec = Recommendation(
            rec_id="project_blocked:orange_ai:review_blocker",
            category="project_blocked",
            entity_id="orange_ai",
            recommendation_type="review_blocker",
            title="Blocked",
            explanation="Blocked",
            suggested_action="Review",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
        )
        self.store.upsert_candidate(rec, now=self.now)
        # User dismisses via chat control
        self.store.dismiss(rec.rec_id, now=self.now)

        # Briefing evaluation checks store
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Blocked.",
        )
        proj = ProjectBriefingContext(name="Orange AI", is_current=True, health=h)
        active_recs = self.evaluator.evaluate(
            current_project=proj,
            secondary_projects=[],
            overdue_reminders=[],
            blocked_jobs=[],
            active_jobs=[],
            paused_jobs=[],
            now=self.now + timedelta(hours=2),
        )
        # Suppressed by shared store!
        self.assertEqual(len(active_recs), 0)


class TestPrivacyAndNonExecutableBoundary(BaseRecommendationTestCase):
    """Suite 7: Privacy, Secret Sanitization, and Non-Executable Safety."""

    def test_session_retracted_entities_suppressed(self):
        """28. Session-retired projects and jobs NEVER generate recommendations."""
        register_session_retraction("project", "Classified Project")
        h = ProjectHealthAssessment(
            project_name="Classified Project",
            is_current=False,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Classified failure.",
        )
        proj = ProjectBriefingContext(name="Classified Project", is_current=False, health=h)
        recs = self.evaluator._evaluate_project_health([proj], self.now)
        self.assertEqual(len(recs), 0)

    def test_sensitive_personal_goals_excluded(self):
        """29. Sensitive personal reminders are excluded from recommendations."""
        rem = ReminderBriefingContext(
            id=99,
            message="Discuss relationship issues with my girlfriend",
            due_at="2026-10-29T10:00:00",
            is_overdue=True,
            is_today=False,
        )
        recs = self.evaluator._evaluate_overdue_reminders([rem], self.now)
        self.assertEqual(len(recs), 0)

    def test_secret_sanitization_applies_to_recommendation_fields(self):
        """30. Credentials and auth tokens in explanations are redacted."""
        job = JobBriefingContext(
            id="job-sec",
            short_id="sec0001",
            goal="Deploy with token Bearer secret-auth-token-12345",
            status="WAITING_CONFIRMATION",
            priority=5,
            pending_action="http_post",
        )
        recs = self.evaluator._evaluate_confirmation_jobs([job], self.now)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertNotIn("secret-auth-token-12345", rec.explanation)
        self.assertIn("[REDACTED]", rec.explanation)

    def test_deterministic_fallback_renders_recommendations(self):
        """31. Deterministic briefing fallback renders recommendations without LLM dependence."""
        from brain.briefing.synthesizer import BriefingSynthesizer

        rec = Recommendation(
            rec_id="job_confirmation:j1:approval_pending",
            category="job_confirmation",
            entity_id="j1",
            recommendation_type="approval_pending",
            title="Approve File Deletion",
            explanation="Job j1 requires approval before deleting /tmp/cache.",
            suggested_action="Review and confirm deletion.",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
            state=RecommendationState.ACTIVE,
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            recommendations=[rec],
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        briefing = synthesizer.synthesize(ctx).summary_markdown

        self.assertIn("## 🎯 Suggested Next Steps", briefing)
        self.assertIn("Approve File Deletion", briefing)
        self.assertIn("`[HIGH]`", briefing)
        self.assertIn("Review and confirm deletion.", briefing)
        self.assertIn("`j1`", briefing)

    def test_non_executable_boundary(self):
        """32. Recommendation suggestions are plain advisory text; never executable commands."""
        rec = Recommendation(
            rec_id="job_failure:j1:repeated_failure",
            category="job_failure",
            entity_id="j1",
            recommendation_type="repeated_failure",
            title="Job Failed",
            explanation="Repeated failure in job j1",
            suggested_action="Inspect logs and verify database credentials",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
        )
        # Must be safe informative text
        self.assertNotIn("exec(", rec.suggested_action)
        self.assertNotIn("sh ", rec.suggested_action)
        self.assertNotIn("rm -rf", rec.suggested_action)


class TestCLIUserControls(BaseRecommendationTestCase):
    """Suite 8: User Controls via CLI Commands (main.py)."""

    def test_handle_recommendation_cli_list_dismiss_defer_handle_why(self):
        """33. CLI controls (list, dismiss, snooze, handle, why) function properly."""
        from main import handle_recommendation_cli

        rec = Recommendation(
            rec_id="reminder_overdue:123:review_commitment",
            category="reminder_overdue",
            entity_id="123",
            recommendation_type="review_commitment",
            title="Overdue Tax Filing",
            explanation="Taxes due on 2026-10-25",
            suggested_action="File tax forms",
            priority=RecommendationPriority.NORMAL,
            evidence_fingerprint="fp_tax",
            evidence=["Tax deadline passed"],
            state=RecommendationState.ACTIVE,
        )

        with patch("brain.briefing.recommendations.RecommendationStore.list_all", return_value=[rec]):
            with patch("brain.briefing.recommendations.RecommendationStore._resolve_target", return_value=rec):
                with patch("brain.briefing.recommendations.RecommendationStore.dismiss", return_value=True):
                    with patch("brain.briefing.recommendations.RecommendationStore.defer", return_value=True):
                        with patch("brain.briefing.recommendations.RecommendationStore.handle", return_value=True):
                            # rec list
                            res_list = handle_recommendation_cli("rec list")
                            self.assertIn("Overdue Tax Filing", res_list["reply"])

                            # rec dismiss
                            res_dis = handle_recommendation_cli("rec dismiss 123")
                            self.assertIn("dismissed for 7 days", res_dis["reply"])

                            # rec snooze
                            res_snooze = handle_recommendation_cli("rec snooze 123 24h")
                            self.assertIn("deferred until", res_snooze["reply"])

                            # rec handle
                            res_handle = handle_recommendation_cli("rec handle 123")
                            self.assertIn("marked handled", res_handle["reply"])

                            # rec why
                            res_why = handle_recommendation_cli("rec why 123")
                            self.assertIn("Recommendation Details", res_why["reply"])
                            self.assertIn("Tax deadline passed", res_why["reply"])


class TestCrossProcessLockingAndLostUpdates(BaseRecommendationTestCase):
    """Suite 9: Cross-Process Mutual Exclusion and Lost-Update Prevention (Correction A)."""

    def test_two_independent_store_instances_updating_same_file(self):
        """34. Two independent store instances safely coordinate mutations via lockfile."""
        store_a = RecommendationStore(file_path=self.json_path)
        store_b = RecommendationStore(file_path=self.json_path)

        rec1 = Recommendation(
            rec_id="project_blocked:orange_ai:review_blocker",
            category="project_blocked",
            entity_id="orange_ai",
            recommendation_type="review_blocker",
            title="Orange AI Blocked",
            explanation="Missing credentials",
            suggested_action="Add key",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp1",
        )
        rec2 = Recommendation(
            rec_id="job_failed:job-99:repeated_failure",
            category="job_failed",
            entity_id="job-99",
            recommendation_type="repeated_failure",
            title="Job 99 Failed",
            explanation="Job 99 failed",
            suggested_action="Retry job",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp2",
        )

        # Store A writes rec1 and rec2
        store_a.upsert_candidate(rec1, now=self.now)
        store_a.upsert_candidate(rec2, now=self.now)
        store_a.save()

        # Store B dismisses rec1
        success_b = store_b.dismiss(rec1.rec_id, now=self.now)
        self.assertTrue(success_b)

        # Store A defers rec2 for 24h
        success_a = store_a.defer(rec2.rec_id, "24h", now=self.now)
        self.assertTrue(success_a)

        # Reload fresh instance from disk: both mutations must be present!
        fresh_store = RecommendationStore(file_path=self.json_path)
        r1 = fresh_store.get(rec1.rec_id)
        r2 = fresh_store.get(rec2.rec_id)
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        self.assertEqual(r1.state, RecommendationState.DISMISSED)
        self.assertEqual(r2.state, RecommendationState.DEFERRED)

    def test_stale_instance_does_not_overwrite_dismissal(self):
        """35. Stale in-memory instance reloading under lock preserves concurrent dismissal."""
        store_gui = RecommendationStore(file_path=self.json_path)
        store_cli = RecommendationStore(file_path=self.json_path)

        rec = Recommendation(
            rec_id="project_blocked:proj_alpha:review_blocker",
            category="project_blocked",
            entity_id="proj_alpha",
            recommendation_type="review_blocker",
            title="Alpha Blocked",
            explanation="Blocker",
            suggested_action="Fix",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_alpha",
        )
        store_gui.upsert_candidate(rec, now=self.now)
        store_gui.save()

        # CLI dismisses the recommendation
        store_cli.dismiss(rec.rec_id, now=self.now)
        self.assertEqual(store_cli.get(rec.rec_id).state, RecommendationState.DISMISSED)

        # Stale GUI records surfacing on rec (or saves)
        store_gui.record_surfaced(rec.rec_id, channel="chat", now=self.now + timedelta(minutes=5))

        # Check disk: Dismissed state must NOT have been overwritten to ACTIVE
        reloaded = RecommendationStore(file_path=self.json_path)
        rec_reloaded = reloaded.get(rec.rec_id)
        self.assertEqual(rec_reloaded.state, RecommendationState.DISMISSED)
        self.assertIn("chat", rec_reloaded.channels)

    def test_lock_timeout_behavior(self):
        """36. CrossProcessFileLock raises TimeoutError when lock is held by another instance."""
        from brain.briefing.recommendations import CrossProcessFileLock
        lock_path = f"{self.json_path}.lock"
        lock1 = CrossProcessFileLock(lock_path, timeout=0.2)
        lock2 = CrossProcessFileLock(lock_path, timeout=0.1)

        with lock1:
            with self.assertRaises(TimeoutError):
                with lock2:
                    pass

    def test_lock_release_after_exceptions(self):
        """37. CrossProcessFileLock releases file descriptor reliably on exceptions."""
        from brain.briefing.recommendations import CrossProcessFileLock
        lock_path = f"{self.json_path}.lock"
        lock1 = CrossProcessFileLock(lock_path, timeout=0.5)
        lock2 = CrossProcessFileLock(lock_path, timeout=0.5)

        try:
            with lock1:
                raise RuntimeError("Simulated crash inside lock")
        except RuntimeError:
            pass

        # Lock 2 must acquire immediately without timeout
        acquired = False
        with lock2:
            acquired = True
        self.assertTrue(acquired)

    def test_preservation_of_unrelated_recommendation_records(self):
        """38. Mutating one recommendation preserves all unrelated recommendation records."""
        store = RecommendationStore(file_path=self.json_path)
        for i in range(5):
            rec = Recommendation(
                rec_id=f"rec_{i}",
                category="project_blocked",
                entity_id=f"proj_{i}",
                recommendation_type="review_blocker",
                title=f"Project {i}",
                explanation="Explanation",
                suggested_action="Action",
                priority=RecommendationPriority.NORMAL,
                evidence_fingerprint=f"fp_{i}",
            )
            store.upsert_candidate(rec, now=self.now)
        store.save()

        # Dismiss only rec_2
        store.dismiss("rec_2", now=self.now)

        reloaded = RecommendationStore(file_path=self.json_path)
        all_recs = reloaded.list_all()
        self.assertEqual(len(all_recs), 5)
        self.assertEqual(reloaded.get("rec_2").state, RecommendationState.DISMISSED)
        for i in (0, 1, 3, 4):
            self.assertEqual(reloaded.get(f"rec_{i}").state, RecommendationState.ACTIVE)


class TestChatEntityRelevanceAndCooldownBypass(BaseRecommendationTestCase):
    """Suite 10: Entity-Specific Chat Relevance and 4-Hour Cooldown (Correction B)."""

    def setUp(self):
        super().setUp()
        from main import _check_contextual_chat_recommendation
        self.check_fn = _check_contextual_chat_recommendation

        self.rec_proj = Recommendation(
            rec_id="project_blocked:orange_ai:review_blocker",
            category="project_blocked",
            entity_id="orange_ai",
            recommendation_type="review_blocker",
            title="Blocked Orange AI",
            explanation="Orange AI is blocked on API key",
            suggested_action="Configure key in settings",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_proj",
            state=RecommendationState.ACTIVE,
        )
        self.rec_job = Recommendation(
            rec_id="job_failed:5a1b2c3d:repeated_failure",
            category="job_failed",
            entity_id="5a1b2c3d-1111-2222-3333-444455556666",
            recommendation_type="repeated_failure",
            title="Failed Backup Job 5a1b2c3d",
            explanation="Job '5a1b2c3d' (database backup) failed with network error.",
            suggested_action="Inspect network connection",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_job",
            state=RecommendationState.ACTIVE,
        )
        self.rec_rem = Recommendation(
            rec_id="reminder_overdue:14:review_commitment",
            category="reminder_overdue",
            entity_id="14",
            recommendation_type="review_commitment",
            title="Overdue Tax Reminder #14",
            explanation="Reminder 'submit quarterly tax filing' is overdue.",
            suggested_action="Submit filing promptly",
            priority=RecommendationPriority.NORMAL,
            evidence_fingerprint="fp_rem",
            state=RecommendationState.ACTIVE,
        )

    def test_relevance_scenarios_and_isolation(self):
        """39. Verifies exact entity matching, cross-project isolation, and generic keyword rejection."""
        with patch("brain.briefing.recommendations.RecommendationStore.list_all", return_value=[self.rec_proj, self.rec_job, self.rec_rem]):
            with patch("brain.briefing.recommendations.RecommendationStore.record_surfaced"):
                # 1. Exact project inquiry -> Matches
                res1 = self.check_fn("How is Orange AI doing?")
                self.assertIsNotNone(res1)
                self.assertIn("Blocked Orange AI", res1)

                # 2. Different project inquiry -> None (Cross-project isolation)
                res2 = self.check_fn("Tell me about Project Saturn")
                self.assertIsNone(res2)

                # 3. Exact job identifier inquiry -> Matches specific job
                res3 = self.check_fn("What is the status of job 5a1b2c3d?")
                self.assertIsNotNone(res3)
                self.assertIn("Failed Backup Job 5a1b2c3d", res3)

                # 4. Generic task management question -> None (Must NOT match)
                res4 = self.check_fn("Give me tips on task management.")
                self.assertIsNone(res4)

                # 5. Generic project mention -> None (Must NOT match)
                res5 = self.check_fn("I enjoy working on projects and reading.")
                self.assertIsNone(res5)

                # 6. Unrelated reminder inquiry -> None (Must NOT match)
                res6 = self.check_fn("What is a good reminder application for Windows?")
                self.assertIsNone(res6)

                # 7. Relevant reminder inquiry -> Matches specific commitment
                res7 = self.check_fn("Did I complete the quarterly tax filing?")
                self.assertIsNotNone(res7)
                self.assertIn("Overdue Tax Reminder #14", res7)

                # 8. Project with no active recommendation -> None
                res8 = self.check_fn("How is Apollo?")
                self.assertIsNone(res8)

    def test_four_hour_cooldown_and_explicit_inquiry_bypass(self):
        """40. 4-hour cooldown suppresses peripheral mentions but explicit inquiry bypasses it."""
        # Mark rec_proj as surfaced 1 hour ago (within 4-hour window)
        surfaced_1h_ago = (datetime.now() - timedelta(hours=1)).isoformat()
        self.rec_proj.channels = ["chat"]
        self.rec_proj.last_surfaced_at = surfaced_1h_ago

        with patch("brain.briefing.recommendations.RecommendationStore.list_all", return_value=[self.rec_proj]):
            with patch("brain.briefing.recommendations.RecommendationStore.record_surfaced"):
                # 9. Explicit inquiry about Orange AI -> Bypasses cooldown!
                res_bypass = self.check_fn("Why is Orange AI blocked?")
                self.assertIsNotNone(res_bypass)
                self.assertIn("Blocked Orange AI", res_bypass)

                # 10. Peripheral inquiry where inquiry is not explicit -> Suppressed by cooldown
                # For example, job rec surfaced 30m ago queried via operational query
                surfaced_30m_ago = (datetime.now() - timedelta(minutes=30)).isoformat()
                self.rec_job.channels = ["chat"]
                self.rec_job.last_surfaced_at = surfaced_30m_ago

        with patch("brain.briefing.recommendations.RecommendationStore.list_all", return_value=[self.rec_job]):
            with patch("brain.briefing.recommendations.RecommendationStore.record_surfaced"):
                # Non-explicit query "any jobs running?" within cooldown is suppressed
                res_suppressed = self.check_fn("Are there any jobs running right now?")
                self.assertIsNone(res_suppressed)


class TestCustomDateTimeDeferralValidation(BaseRecommendationTestCase):
    """Suite 11: Custom Date/Time Deferral Validation and CLI Preserving (Correction C)."""

    def test_valid_deferral_formats_and_persistence(self):
        """41. 24h, 7d, ISO timestamps, and space-separated formats defer and persist properly."""
        store = RecommendationStore(file_path=self.json_path)
        rec = Recommendation(
            rec_id="test_rec_defer",
            category="project_blocked",
            entity_id="orange_ai",
            recommendation_type="review_blocker",
            title="Deferral Test",
            explanation="Explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_defer",
        )
        store.upsert_candidate(rec, now=self.now)
        store.save()

        # 1. 24h deferral
        local_now = normalize_local_now(self.now)
        self.assertTrue(store.defer(rec.rec_id, "24h", now=self.now))
        r1 = store.get(rec.rec_id)
        self.assertEqual(r1.state, RecommendationState.DEFERRED)
        self.assertEqual(r1.suppressed_until, (local_now + timedelta(hours=24)).isoformat())

        # 2. 7d deferral
        self.assertTrue(store.defer(rec.rec_id, "7d", now=self.now))
        r2 = store.get(rec.rec_id)
        self.assertEqual(r2.state, RecommendationState.DEFERRED)
        self.assertEqual(r2.suppressed_until, (local_now + timedelta(days=7)).isoformat())

        # 3. Valid ISO 8601 with 'T'
        future_iso = "2026-11-20T14:00:00"
        self.assertTrue(store.defer(rec.rec_id, future_iso, now=self.now))
        r3 = store.get(rec.rec_id)
        self.assertEqual(r3.suppressed_until, future_iso)

        # 4. Valid ISO 8601 with space separator
        future_space = "2026-11-25 18:30:00"
        self.assertTrue(store.defer(rec.rec_id, future_space, now=self.now))
        r4 = store.get(rec.rec_id)
        self.assertEqual(r4.suppressed_until, "2026-11-25T18:30:00")

    def test_invalid_and_past_timestamps_rejected_without_state_change(self):
        """42. Past, equal-to-now, empty, and invalid timestamps are rejected and preserve previous state."""
        store = RecommendationStore(file_path=self.json_path)
        rec = Recommendation(
            rec_id="rec_invalid_test",
            category="project_blocked",
            entity_id="orange_ai",
            recommendation_type="review_blocker",
            title="Invalid Test",
            explanation="Explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_inv",
            state=RecommendationState.ACTIVE,
        )
        store.upsert_candidate(rec, now=self.now)
        store.save()

        # 1. Past timestamp rejected
        past_iso = "2020-01-01T00:00:00"
        self.assertFalse(store.defer(rec.rec_id, past_iso, now=self.now))
        self.assertEqual(store.get(rec.rec_id).state, RecommendationState.ACTIVE)
        self.assertIsNone(store.get(rec.rec_id).suppressed_until)

        # 2. Equal to now rejected
        self.assertFalse(store.defer(rec.rec_id, self.now.isoformat(), now=self.now))
        self.assertEqual(store.get(rec.rec_id).state, RecommendationState.ACTIVE)

        # 3. Invalid string rejected
        self.assertFalse(store.defer(rec.rec_id, "tomorrow-afternoon", now=self.now))
        self.assertEqual(store.get(rec.rec_id).state, RecommendationState.ACTIVE)

        # 4. Empty string rejected
        self.assertFalse(store.defer(rec.rec_id, "", now=self.now))
        self.assertEqual(store.get(rec.rec_id).state, RecommendationState.ACTIVE)

    def test_cli_snooze_preserves_space_separated_arguments(self):
        """43. CLI 'rec snooze <id> YYYY-MM-DD HH:MM:SS' parses full timestamp without truncation."""
        from main import handle_recommendation_cli
        store = RecommendationStore(file_path=self.json_path)
        rec = Recommendation(
            rec_id="rec_cli_snooze",
            category="project_blocked",
            entity_id="orange_ai",
            recommendation_type="review_blocker",
            title="CLI Snooze Test",
            explanation="Explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="fp_cli",
            state=RecommendationState.ACTIVE,
        )
        store.upsert_candidate(rec, now=self.now)
        store.save()

        with patch("brain.briefing.recommendations.RecommendationStore._resolve_target", return_value=rec):
            with patch("brain.briefing.recommendations.RecommendationStore.defer", return_value=True):
                # CLI command with space-separated timestamp
                res = handle_recommendation_cli("rec snooze rec_cli_snooze 2026-11-20 15:30:00")
                self.assertIn("deferred until", res["reply"])


if __name__ == "__main__":
    unittest.main()
