"""
test_project_health.py - Synthetic Test Suite for Project Health Evaluator.

Stage 3.7E.2B: Comprehensive classification, safeguard, association, privacy,
and timestamp testing for the Deterministic Project Health Evaluator.
Strictly isolated: runs on synthetic in-memory fixtures and temporary SQLite databases.
Never mutates the development or installer databases.
"""

import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from brain.briefing.models import ProjectHealthStatus, ProjectHealthAssessment
from brain.briefing.project_health import (
    parse_timestamp,
    match_job_to_projects,
    evaluate_project_health,
    ProjectHealthEvaluator,
    PROGRESS_RECENCY_HOURS,
    FAILURE_RECENCY_HOURS,
    STUCK_RUNNING_JOB_HOURS,
    INACTIVITY_THRESHOLD_DAYS,
)
from brain.conflict_resolver import (
    register_session_retraction,
    clear_session_retractions,
)
from brain.agent_models import AgentAction, ActionType
from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore


def _init_test_db(db_path: str) -> None:
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
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        goal TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        priority INTEGER NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        step_count INTEGER NOT NULL DEFAULT 0,
        retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
        next_retry_at TEXT,
        last_error TEXT,
        pending_confirmation_json TEXT,
        final_response TEXT,
        agent_state_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        source TEXT NOT NULL DEFAULT 'user',
        tags TEXT DEFAULT ''
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS job_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        attempt INTEGER NOT NULL,
        step_index INTEGER NOT NULL,
        action_type TEXT NOT NULL,
        tool_name TEXT,
        arguments_json TEXT,
        reasoning TEXT,
        success INTEGER,
        result_data_json TEXT,
        error TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        duration_ms INTEGER,
        model_name TEXT,
        UNIQUE(job_id, attempt, step_index)
    )
    """)
    conn.commit()
    conn.close()


class BaseHealthTestCase(unittest.TestCase):
    """Base test case providing timestamp conversion helpers."""

    def setUp(self):
        self.now = datetime(2026, 10, 30, 12, 0, 0)

    def _to_utc_iso(self, local_dt: datetime) -> str:
        """Convert local datetime to UTC ISO-8601 string (+00:00)."""
        local_aware = local_dt.astimezone()
        utc_dt = local_aware.astimezone(timezone.utc)
        return utc_dt.isoformat()


class TestProjectHealthClassification(BaseHealthTestCase):
    """Suite 1: Core Health Status Classifications (Pure In-Memory)."""

    def test_waiting_confirmation_produces_blocked(self):
        """1. WAITING_CONFIRMATION job produces BLOCKED status."""
        job = Job(id="job-1", goal="Deploy update to server", status=JobStatus.WAITING_CONFIRMATION)
        action = AgentAction(type=ActionType.CALL_TOOL, tool_name="deploy_server", arguments={"target": "prod"})
        job.set_pending_confirmation(action)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[job],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.BLOCKED)
        self.assertIn("requires confirmation", assessment.explanation)
        self.assertIn("deploy_server", assessment.explanation)
        self.assertEqual(assessment.confidence, "HIGH")

    def test_explicit_paused_produces_paused(self):
        """2. Explicit PAUSED job with no blocker produces PAUSED status."""
        job = Job(id="job-2", goal="Train neural network", status=JobStatus.PAUSED)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[job],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.PAUSED)
        self.assertIn("paused", assessment.explanation.lower())
        self.assertEqual(assessment.confidence, "HIGH")

    def test_recent_successful_step_produces_progressing(self):
        """3. Recent successful execution step (< 48h) produces PROGRESSING."""
        job = Job(id="job-3", goal="Build frontend UI", status=JobStatus.RUNNING)
        step_time = self._to_utc_iso(self.now - timedelta(hours=6))
        step = JobStep(job_id="job-3", step_index=1, success=True, started_at=step_time)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[job],
            steps_by_job={"job-3": [step]},
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.PROGRESSING)
        self.assertIn("actively progressing", assessment.explanation)
        self.assertEqual(assessment.last_progress_at, step_time)
        self.assertEqual(assessment.confidence, "HIGH")

    def test_running_job_without_recent_progress_produces_active_no_recent_progress(self):
        """4. RUNNING job with no progress for 6 days produces ACTIVE_NO_RECENT_PROGRESS."""
        old_time = self._to_utc_iso(self.now - timedelta(days=6))
        job = Job(
            id="job-4",
            goal="Compile dataset",
            status=JobStatus.RUNNING,
            started_at=old_time,
            updated_at=old_time,
        )
        old_step = JobStep(job_id="job-4", step_index=1, success=True, started_at=old_time)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[job],
            steps_by_job={"job-4": [old_step]},
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS)
        self.assertIn("no verified progress", assessment.explanation)
        self.assertEqual(assessment.confidence, "MEDIUM")

    def test_explicit_authoritative_completion_produces_completed(self):
        """5. Explicit authoritative completion produces COMPLETED when explicitly verified."""
        job = Job(id="job-5", goal="Release version 1.0", status=JobStatus.COMPLETED)
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[job],
            now=self.now,
            explicit_completion=True,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.COMPLETED)
        self.assertIn("verified as completed", assessment.explanation)
        self.assertEqual(assessment.confidence, "HIGH")

    def test_no_jobs_and_insufficient_evidence_produces_unknown(self):
        """6. Project with no jobs and insufficient activity evidence produces UNKNOWN."""
        assessment = evaluate_project_health(
            project_name="Project Zephyr",
            is_current=False,
            associated_jobs=[],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.UNKNOWN)
        self.assertIn("No autonomous jobs", assessment.explanation)
        self.assertEqual(assessment.confidence, "INSUFFICIENT")


class TestCompletionSafeguards(BaseHealthTestCase):
    """Suite 2: Strict Project Completion Safeguards."""

    def test_all_associated_jobs_completed_produces_unknown_not_completed(self):
        """7. All associated jobs completed MUST produce UNKNOWN, not COMPLETED."""
        t1 = self._to_utc_iso(self.now - timedelta(days=10))
        t2 = self._to_utc_iso(self.now - timedelta(days=9))
        j1 = Job(id="j1", goal="Task 1", status=JobStatus.COMPLETED, completed_at=t1)
        j2 = Job(id="j2", goal="Task 2", status=JobStatus.COMPLETED, completed_at=t2)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[j1, j2],
            now=self.now,
            explicit_completion=False,
        )
        # CRITICAL SAFEGUARD: Must NOT be COMPLETED
        self.assertNotEqual(assessment.status, ProjectHealthStatus.COMPLETED)
        self.assertEqual(assessment.status, ProjectHealthStatus.UNKNOWN)
        self.assertIn("overall project completion has not been verified", assessment.explanation)
        self.assertIn("All 2 associated autonomous jobs are completed", assessment.explanation)

    def test_empty_job_list_never_produces_completed(self):
        """8. Empty job list must not produce COMPLETED."""
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[],
            now=self.now,
        )
        self.assertNotEqual(assessment.status, ProjectHealthStatus.COMPLETED)

    def test_no_active_jobs_never_produces_completed(self):
        """9. No active jobs must not produce COMPLETED."""
        j_cancel = Job(id="j-c", goal="Cancelled task", status=JobStatus.CANCELLED)
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=False,
            associated_jobs=[j_cancel],
            now=self.now,
        )
        self.assertNotEqual(assessment.status, ProjectHealthStatus.COMPLETED)
        self.assertEqual(assessment.status, ProjectHealthStatus.UNKNOWN)

    def test_retired_or_forgotten_project_excluded(self):
        """10. Retired or forgotten projects are completely excluded from health assessment."""
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "test_mem.db")
        _init_test_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'sec1', 'Deprecated App', '2026-01-01', '2026-01-01')")
        conn.commit()
        conn.close()

        evaluator = ProjectHealthEvaluator(db_path=db_path)

        # Before retirement: 1 project returned
        self.assertEqual(len(evaluator.evaluate_all()), 1)

        # Register session retraction
        register_session_retraction("project", "Deprecated App")
        try:
            # After retirement: excluded entirely!
            self.assertEqual(len(evaluator.evaluate_all()), 0)
            self.assertIsNone(evaluator.evaluate_project("Deprecated App"))
        finally:
            clear_session_retractions()
            temp_dir.cleanup()


class TestConflictingJobStates(BaseHealthTestCase):
    """Suite 3: Conflicting and Overlapping Job States."""

    def test_waiting_confirmation_and_running_job_produces_partial_blockage(self):
        """11. One WAITING_CONFIRMATION job and one RUNNING job produce BLOCKED with partial-blockage explanation."""
        j_wait = Job(id="j-wait", goal="Delete old logs", status=JobStatus.WAITING_CONFIRMATION)
        action = AgentAction(type=ActionType.CALL_TOOL, tool_name="delete_files", arguments={"dir": "logs"})
        j_wait.set_pending_confirmation(action)

        j_run = Job(id="j-run", goal="Index new documents", status=JobStatus.RUNNING)
        step_time = self._to_utc_iso(self.now - timedelta(hours=2))
        step = JobStep(job_id="j-run", step_index=1, success=True, started_at=step_time)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[j_wait, j_run],
            steps_by_job={"j-run": [step]},
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.BLOCKED)
        self.assertIn("waiting for your confirmation", assessment.explanation)
        self.assertIn("another job is progressing", assessment.explanation)

    def test_failed_job_and_newer_progressing_work_does_not_block(self):
        """12. A FAILED job superseded by newer progressing work does not block the project."""
        # Failed 10 hours ago
        fail_time = self._to_utc_iso(self.now - timedelta(hours=10))
        j_fail = Job(id="j-fail", goal="Fetch API data", status=JobStatus.FAILED, last_error="HTTP 500", completed_at=fail_time)

        # Succeeded 2 hours ago
        step_time = self._to_utc_iso(self.now - timedelta(hours=2))
        j_progress = Job(id="j-prog", goal="Process local cache", status=JobStatus.RUNNING)
        step = JobStep(job_id="j-prog", step_index=1, success=True, started_at=step_time)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[j_fail, j_progress],
            steps_by_job={"j-prog": [step]},
            now=self.now,
        )
        # Should be PROGRESSING because failure was superseded by newer progress!
        self.assertEqual(assessment.status, ProjectHealthStatus.PROGRESSING)
        self.assertTrue(any("superseded by subsequent progress" in ev for ev in assessment.evidence))

    def test_historical_failure_older_than_48h_does_not_block(self):
        """13. A failure older than 48 hours does not block the project."""
        old_fail_time = self._to_utc_iso(self.now - timedelta(hours=50))
        j_old_fail = Job(id="j-old", goal="Old task", status=JobStatus.FAILED, last_error="Network error", completed_at=old_fail_time)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[j_old_fail],
            now=self.now,
        )
        # Not BLOCKED because failure is > 48 hours old
        self.assertNotEqual(assessment.status, ProjectHealthStatus.BLOCKED)

    def test_paused_job_with_no_blocker_produces_paused(self):
        """14. A PAUSED job with no higher-priority blocker produces PAUSED."""
        j_paused = Job(id="j-p", goal="Export video", status=JobStatus.PAUSED)
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[j_paused],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.PAUSED)

    def test_running_job_alone_does_not_establish_progressing(self):
        """15. A RUNNING job without verified step progress does NOT produce PROGRESSING."""
        old_time = self._to_utc_iso(self.now - timedelta(hours=3))
        j_run = Job(
            id="j-r",
            goal="Scan network",
            status=JobStatus.RUNNING,
            started_at=old_time,
            updated_at=old_time,
        )
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=False,
            associated_jobs=[j_run],
            steps_by_job={"j-r": []},
            now=self.now,
        )
        # Must not be PROGRESSING
        self.assertNotEqual(assessment.status, ProjectHealthStatus.PROGRESSING)

    def test_running_job_without_progress_over_24h_receives_warning(self):
        """16. A RUNNING job without progress for > 24 hours receives a stuck warning."""
        old_time = self._to_utc_iso(self.now - timedelta(hours=30))
        j_stuck = Job(
            id="j-stuck",
            goal="Heavy render",
            status=JobStatus.RUNNING,
            started_at=old_time,
            updated_at=old_time,
        )
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[j_stuck],
            steps_by_job={"j-stuck": []},
            now=self.now,
        )
        self.assertTrue(assessment.has_stuck_running_job)
        self.assertTrue(any("more than 24 hours" in ev for ev in assessment.evidence))


class TestProjectJobAssociation(unittest.TestCase):
    """Suite 4: Project-to-Job Association Hierarchy and Precision."""

    def setUp(self):
        self.projects = [("Orange AI", True), ("Project Zephyr", False), ("App", False)]

    def test_explicit_project_tag_matches_correctly(self):
        """17. Explicit project tags match with highest precedence."""
        j = Job(id="j1", goal="Fix database schema", tags="project:Orange AI")
        matched = match_job_to_projects(j, self.projects)
        self.assertEqual(matched, "Orange AI")

    def test_case_insensitive_word_boundary_matching(self):
        """18. Case-insensitive word boundary matches project in goal."""
        j = Job(id="j2", goal="run test suite for project zephyr now")
        matched = match_job_to_projects(j, self.projects)
        self.assertEqual(matched, "Project Zephyr")

    def test_similar_project_names_avoid_substring_false_positives(self):
        """19. 'App' does not falsely match 'application' or 'Apple'."""
        j1 = Job(id="j3", goal="Deploy web application to server")
        self.assertIsNone(match_job_to_projects(j1, self.projects))

        j2 = Job(id="j4", goal="Order new Apple hardware")
        self.assertIsNone(match_job_to_projects(j2, self.projects))

        j3 = Job(id="j5", goal="Fix crash in App when opening settings")
        self.assertEqual(match_job_to_projects(j3, self.projects), "App")

    def test_ambiguous_multi_project_goals_rejected(self):
        """20. Job goals mentioning multiple distinct projects are rejected."""
        j = Job(id="j6", goal="Sync Orange AI data with Project Zephyr")
        self.assertIsNone(match_job_to_projects(j, self.projects))

    def test_generic_jobs_remain_unassociated(self):
        """21. Generic jobs without tags or project names remain unassociated."""
        j = Job(id="j7", goal="Clean up disk space on C drive")
        self.assertIsNone(match_job_to_projects(j, self.projects))

    def test_unassociated_jobs_do_not_contaminate_project_health(self):
        """22. Unassociated active jobs do not appear in any project's associated_job_ids."""
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "test_mem.db")
        _init_test_db(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'current', 'Orange AI', '2026-01-01', '2026-01-01')")
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('gen-1', 'Clean temp files', 'RUNNING', '2026-10-30T10:00:00+00:00', '2026-10-30T10:00:00+00:00')")
        conn.commit()
        conn.close()

        evaluator = ProjectHealthEvaluator(db_path=db_path)
        assessments = evaluator.evaluate_all()
        unassociated = evaluator.get_unassociated_active_jobs()

        self.assertEqual(len(assessments), 1)
        self.assertEqual(len(assessments[0].associated_job_ids), 0)
        self.assertEqual(len(unassociated), 1)
        self.assertEqual(unassociated[0].id, "gen-1")

        temp_dir.cleanup()


class TestPrivacyAndSanitization(unittest.TestCase):
    """Suite 5: Privacy, Forgetting, and Secret Sanitization."""

    def test_session_retired_projects_excluded(self):
        """23. Session-retired projects are completely excluded."""
        clear_session_retractions()
        register_session_retraction("project", "SecretProject")
        try:
            temp_dir = tempfile.TemporaryDirectory()
            db_path = os.path.join(temp_dir.name, "test_mem.db")
            _init_test_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'current', 'SecretProject', '2026-01-01', '2026-01-01')")
            conn.commit()
            conn.close()

            evaluator = ProjectHealthEvaluator(db_path=db_path)
            self.assertEqual(len(evaluator.evaluate_all()), 0)
            temp_dir.cleanup()
        finally:
            clear_session_retractions()

    def test_session_retired_jobs_excluded(self):
        """24. Session-retired job goals are excluded from association."""
        clear_session_retractions()
        register_session_retraction("job", "confidential task")
        try:
            temp_dir = tempfile.TemporaryDirectory()
            db_path = os.path.join(temp_dir.name, "test_mem.db")
            _init_test_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'current', 'Orange AI', '2026-01-01', '2026-01-01')")
            conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES ('j-conf', 'run confidential task on Orange AI', 'RUNNING', '2026-10-30T10:00:00+00:00', '2026-10-30T10:00:00+00:00')")
            conn.commit()
            conn.close()

            evaluator = ProjectHealthEvaluator(db_path=db_path)
            assessments = evaluator.evaluate_all()
            self.assertEqual(len(assessments), 1)
            self.assertEqual(len(assessments[0].associated_job_ids), 0)
            temp_dir.cleanup()
        finally:
            clear_session_retractions()

    def test_sensitive_personal_goals_excluded(self):
        """25. Sensitive romantic and personal family goals are excluded from project assessment."""
        from brain.briefing.collector import _is_sensitive_personal_goal
        # Personal/romantic goal must be identified as sensitive
        self.assertTrue(_is_sensitive_personal_goal("marry her and plan the wedding"))
        self.assertTrue(_is_sensitive_personal_goal("buy anniversary gift for wife"))
        # Technical relationship context must be safely preserved
        self.assertFalse(_is_sensitive_personal_goal("design entity-relationship schema for database"))
        self.assertFalse(_is_sensitive_personal_goal("build data relationship pipeline for Orange AI"))

    def test_secret_sanitization_in_assessment_details(self):
        """26. Secret API keys, Bearer tokens, and passwords in errors are redacted."""
        now = datetime(2026, 10, 30, 12, 0, 0)
        local_aware = (now - timedelta(hours=1)).astimezone()
        comp_time = local_aware.astimezone(timezone.utc).isoformat()

        job = Job(
            id="job-sec",
            goal="Connect to cloud API",
            status=JobStatus.FAILED,
            last_error="Authorization failed for Bearer eyJhbGciOiJIUzI1NiJ9.secret and api_key: sk-1234567890abcdef",
            completed_at=comp_time,
        )
        assessment = evaluate_project_health("Orange AI", True, [job], now=now)

        self.assertNotIn("sk-1234567890abcdef", assessment.explanation)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", assessment.explanation)
        self.assertIn("[REDACTED]", assessment.explanation)
        if assessment.recent_failure_info:
            self.assertNotIn("sk-1234567890abcdef", assessment.recent_failure_info)
            self.assertIn("[REDACTED]", assessment.recent_failure_info)


class TestTimestampReliability(BaseHealthTestCase):
    """Suite 6: Timestamp Parsing, Normalization, and Timezone Safety."""

    def test_utc_aware_timestamp_normalization(self):
        """27. UTC-aware timestamps normalize to naive local datetimes."""
        ts_utc = "2026-10-30T10:00:00+00:00"
        dt = parse_timestamp(ts_utc)
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)

    def test_local_naive_timestamp_preservation(self):
        """28. Local naive timestamps parse cleanly without modification."""
        ts_local = "2026-10-30T12:00:00.123456"
        dt = parse_timestamp(ts_local)
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)
        self.assertEqual(dt.hour, 12)

    def test_naive_aware_subtraction_never_raises_type_error(self):
        """29. Comparing parsed timestamps against datetime.now() never raises TypeError."""
        ts_utc = "2026-10-30T10:00:00+00:00"
        parsed_dt = parse_timestamp(ts_utc)
        try:
            delta = self.now - parsed_dt
            self.assertIsInstance(delta, timedelta)
        except TypeError as e:
            self.fail(f"TypeError raised during datetime subtraction: {e}")

    def test_malformed_timestamp_handled_safely(self):
        """30. Malformed or invalid timestamps return None without throwing."""
        self.assertIsNone(parse_timestamp("not-a-timestamp"))
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp(None))

    def test_null_completed_at_uses_started_at_for_progress(self):
        """31. When completed_at is NULL, valid started_at establishes progress."""
        job = Job(id="j1", goal="Run diagnostics", status=JobStatus.RUNNING)
        step_time = self._to_utc_iso(self.now - timedelta(hours=3))
        step = JobStep(job_id="j1", step_index=1, success=True, started_at=step_time, completed_at=None)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[job],
            steps_by_job={"j1": [step]},
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.PROGRESSING)
        self.assertEqual(assessment.last_progress_at, step_time)

    def test_future_timestamp_rejected_as_recent_progress(self):
        """32. Future timestamps (beyond clock skew tolerance) are rejected from recent progress."""
        job = Job(id="j2", goal="Future work", status=JobStatus.RUNNING)
        future_time = self._to_utc_iso(self.now + timedelta(days=2))
        step = JobStep(job_id="j2", step_index=1, success=True, started_at=future_time)

        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=False,
            associated_jobs=[job],
            steps_by_job={"j2": [step]},
            now=self.now,
        )
        # Future step rejected -> not PROGRESSING
        self.assertNotEqual(assessment.status, ProjectHealthStatus.PROGRESSING)


class TestProjectTypeBehavior(BaseHealthTestCase):
    """Suite 7: Primary (Current) vs. Secondary Project Rules."""

    def test_current_project_5_days_no_progress_is_active_no_recent_progress(self):
        """33. Current project with 5 days of no progress qualifies for ACTIVE_NO_RECENT_PROGRESS."""
        assessment = evaluate_project_health(
            project_name="Orange AI",
            is_current=True,
            associated_jobs=[],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS)

    def test_secondary_project_with_active_job_5_days_no_progress_qualifies(self):
        """34. Secondary project with active work and no progress for 5 days qualifies."""
        old_time = self._to_utc_iso(self.now - timedelta(days=6))
        job = Job(
            id="j-sec",
            goal="Secondary project task",
            status=JobStatus.PENDING,
            created_at=old_time,
        )
        assessment = evaluate_project_health(
            project_name="Project Zephyr",
            is_current=False,
            associated_jobs=[job],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS)

    def test_inactive_secondary_project_without_jobs_remains_unknown(self):
        """35. Inactive secondary project with no evidence of active work remains UNKNOWN."""
        assessment = evaluate_project_health(
            project_name="Project Zephyr",
            is_current=False,
            associated_jobs=[],
            now=self.now,
        )
        self.assertEqual(assessment.status, ProjectHealthStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
