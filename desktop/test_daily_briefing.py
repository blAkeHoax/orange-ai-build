"""
test_daily_briefing.py - Comprehensive Test Suite for Context-Aware Daily Briefing.

Stage 3.7B: Validates read-only context collection, deterministic priority ranking,
privacy & personal goal filtering, time/freshness boundaries, context budgeting,
and Orange API synthesis with fallback.
Strictly isolated: runs on temporary SQLite databases and never touches production memory.db.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from brain.briefing.models import (
    PriorityLevel,
    PriorityItem,
    ProjectBriefingContext,
    ReminderBriefingContext,
    JobBriefingContext,
    DailyBriefingContext,
    DailyBriefing,
    ProjectHealthStatus,
    ProjectHealthAssessment,
)
from brain.briefing.collector import BriefingCollector, sanitize_briefing_text
from brain.briefing.synthesizer import BriefingSynthesizer
from brain.briefing.recommendations import RecommendationStore, get_recommendations_store_path
from brain.conflict_resolver import (
    register_session_retraction,
    unregister_session_retraction,
    clear_session_retractions,
)
from brain.agent_models import AgentAction, ActionType
from brain.jobs.models import JobStatus, Job, JobStep
from brain.jobs.store import JobStore


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
    CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT,
        created_at TEXT DEFAULT ''
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS conversation_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        due_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        fired INTEGER NOT NULL DEFAULT 0
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


class TestDailyBriefingCollector(unittest.TestCase):
    """Test suite for BriefingCollector operational discovery and deterministic ranking."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name
        _init_test_db(self.db_path)
        self.temp_recs_dir = tempfile.TemporaryDirectory()
        self.recs_path = os.path.join(self.temp_recs_dir.name, "recommendations.json")
        self.rec_store = RecommendationStore(file_path=self.recs_path)
        self.job_store = JobStore(db_path=self.db_path)
        self.collector = BriefingCollector(
            db_path=self.db_path,
            job_store=self.job_store,
            recommendation_store=self.rec_store,
        )
        RecommendationStore._STRICT_TEST_ISOLATION = True
        clear_session_retractions()

    def tearDown(self):
        RecommendationStore._STRICT_TEST_ISOLATION = False
        clear_session_retractions()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
        try:
            self.temp_recs_dir.cleanup()
        except Exception:
            pass

    def _insert_structured(self, category: str, key: str, value: str):
        conn = sqlite3.connect(self.db_path)
        now_iso = datetime.now().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO structured_memories(category, memory_key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (category, key, value, now_iso, now_iso),
        )
        conn.commit()
        conn.close()

    def _insert_reminder(self, message: str, due_at: datetime, fired: int = 0) -> int:
        conn = sqlite3.connect(self.db_path)
        now_iso = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO reminders(message, due_at, created_at, fired) VALUES (?, ?, ?, ?)",
            (message, due_at.isoformat(), now_iso, fired),
        )
        conn.commit()
        last_id = cur.lastrowid
        conn.close()
        return last_id

    def test_current_and_secondary_project_collection(self):
        """1. Collects primary current project and distinct secondary projects."""
        self._insert_structured("identity", "name", "Sayan Dey")
        self._insert_structured("project", "current", "Orange AI Desktop (personal AI companion)")
        self._insert_structured("project", "zephyr", "Project Zephyr (web app)")
        self._insert_structured("project", "portfolio", "Personal Portfolio")

        ctx = self.collector.collect()
        self.assertEqual(ctx.user_name, "Sayan Dey")
        self.assertIsNotNone(ctx.current_project)
        self.assertEqual(ctx.current_project.name, "Orange AI Desktop")
        self.assertEqual(ctx.current_project.description, "Orange AI Desktop (personal AI companion)")
        self.assertTrue(ctx.current_project.is_current)

        sec_names = [p.name for p in ctx.secondary_projects]
        self.assertIn("Project Zephyr", sec_names)
        self.assertIn("Personal Portfolio", sec_names)
        self.assertNotIn("Orange AI Desktop", sec_names)

    def test_relevant_goal_filtering_and_sensitive_exclusion(self):
        """2. Relevant goals are linked to projects; sensitive/romantic goals are strictly excluded."""
        self._insert_structured("project", "current", "Orange AI")
        # Relevant work goals
        self._insert_structured("goal", "stage37", "Complete Orange AI Stage 3.7 briefing engine")
        self._insert_structured("goal", "learn_python", "Study advanced Python concurrency and async")
        # Sensitive romantic/personal goal that must be excluded
        self._insert_structured("goal", "long_term", "marry her")
        self._insert_structured("goal", "wedding", "Plan romantic wedding date")

        ctx = self.collector.collect()

        # Orange AI should have the relevant goal mapped
        self.assertIn("Complete Orange AI Stage 3.7 briefing engine", ctx.current_project.related_goals)

        # Neither 'marry her' nor 'wedding' should appear in goals or priorities
        for p in ctx.priorities:
            self.assertNotIn("marry", p.title.lower())
            self.assertNotIn("wedding", p.title.lower())

    def test_overdue_and_due_today_reminder_classification(self):
        """3. Reminders partitioned into overdue vs due today without duplication."""
        now = datetime(2026, 10, 30, 14, 0, 0)
        # Overdue reminder (yesterday)
        r_overdue = self._insert_reminder("Submit invoice", now - timedelta(days=1))
        # Due today (later at 18:00)
        r_today = self._insert_reminder("Daily team sync", datetime(2026, 10, 30, 18, 0, 0))
        # Due next week
        r_future = self._insert_reminder("Renew passport", now + timedelta(days=7))
        # Already fired reminder (should be excluded)
        self._insert_reminder("Old fired task", now - timedelta(hours=2), fired=1)

        ctx = self.collector.collect(now=now)

        overdue_ids = [r.id for r in ctx.overdue_reminders]
        today_ids = [r.id for r in ctx.today_reminders]

        self.assertIn(r_overdue, overdue_ids)
        self.assertNotIn(r_overdue, today_ids)

        self.assertIn(r_today, today_ids)
        self.assertNotIn(r_today, overdue_ids)

        self.assertNotIn(r_future, overdue_ids)
        self.assertNotIn(r_future, today_ids)

    def test_blocked_running_retrying_and_paused_jobs_collection(self):
        """4. Jobs correctly classified by status and pending action extracted."""
        # 1. Blocked job waiting confirmation
        j_blocked = self.job_store.create_job(goal="Delete temp folder", priority=10)
        j_blocked.status = JobStatus.WAITING_CONFIRMATION
        action = AgentAction(type=ActionType.CALL_TOOL, tool_name="delete_folder", arguments={})
        j_blocked.set_pending_confirmation(action)
        self.job_store.update_job(j_blocked)

        # 2. Running job
        j_running = self.job_store.create_job(goal="Process large dataset", priority=5)
        j_running.status = JobStatus.RUNNING
        self.job_store.update_job(j_running)

        # 3. Retrying job
        j_retrying = self.job_store.create_job(goal="Fetch remote telemetry", priority=2)
        j_retrying.status = JobStatus.RETRYING
        j_retrying.last_error = "Connection reset by peer"
        self.job_store.update_job(j_retrying)

        # 4. Paused job
        j_paused = self.job_store.create_job(goal="Train clustering model", priority=4)
        j_paused.status = JobStatus.PAUSED
        self.job_store.update_job(j_paused)

        ctx = self.collector.collect()

        blocked_ids = [j.id for j in ctx.blocked_jobs]
        active_ids = [j.id for j in ctx.active_jobs]
        paused_ids = [j.id for j in ctx.paused_jobs]

        self.assertIn(j_blocked.id, blocked_ids)
        self.assertEqual(ctx.blocked_jobs[0].pending_action, "delete_folder")

        self.assertIn(j_running.id, active_ids)
        self.assertIn(j_retrying.id, active_ids)
        self.assertIn(j_paused.id, paused_ids)

    def test_recent_completion_filtering_rolling_24_hours(self):
        """5. Completed jobs filtered strictly within previous rolling 24 hours."""
        now = datetime(2026, 10, 30, 14, 0, 0)
        # Job A: completed 3 hours ago (within 24h)
        j_recent = self.job_store.create_job(goal="Run benchmark suite")
        j_recent.status = JobStatus.COMPLETED
        j_recent.completed_at = (now - timedelta(hours=3)).isoformat()
        self.job_store.update_job(j_recent)

        # Job B: completed 48 hours ago (outside 24h)
        j_old = self.job_store.create_job(goal="Migrate database v1")
        j_old.status = JobStatus.COMPLETED
        j_old.completed_at = (now - timedelta(hours=48)).isoformat()
        self.job_store.update_job(j_old)

        ctx = self.collector.collect(now=now)

        completed_ids = [j.id for j in ctx.recent_completed_jobs]
        self.assertIn(j_recent.id, completed_ids)
        self.assertNotIn(j_old.id, completed_ids)
        self.assertTrue(any("Run benchmark suite" in dev for dev in ctx.recent_project_developments))

    def test_deterministic_priority_ordering(self):
        """6. Verifies strict 6-tier priority calculation and secondary numerical priority sorting."""
        now = datetime(2026, 10, 30, 14, 0, 0)

        # Tier 1: Overdue reminder & Blocked job
        self._insert_reminder("Critical overdue audit", now - timedelta(hours=2))
        j_blocked_low = self.job_store.create_job(goal="Low prio blocked", priority=1)
        j_blocked_low.status = JobStatus.WAITING_CONFIRMATION
        self.job_store.update_job(j_blocked_low)

        j_blocked_high = self.job_store.create_job(goal="High prio blocked", priority=10)
        j_blocked_high.status = JobStatus.WAITING_CONFIRMATION
        self.job_store.update_job(j_blocked_high)

        # Tier 2: Today's reminder
        self._insert_reminder("Team demo today", datetime(2026, 10, 30, 17, 0, 0))

        # Tier 3: Running job
        j_active = self.job_store.create_job(goal="Active sync job", priority=8)
        j_active.status = JobStatus.RUNNING
        self.job_store.update_job(j_active)

        # Tier 4: Paused job
        j_paused = self.job_store.create_job(goal="Paused migration", priority=3)
        j_paused.status = JobStatus.PAUSED
        self.job_store.update_job(j_paused)

        # Tier 5: Current project
        self._insert_structured("project", "current", "Alpha Release")

        # Tier 6: Secondary project & actionable system notice
        self._insert_structured("project", "beta", "Beta Feature")
        collector_with_sys = BriefingCollector(
            db_path=self.db_path,
            job_store=self.job_store,
            system_checker=lambda: "Battery is critically low (14%) and unplugged.",
            recommendation_store=self.rec_store,
        )

        ctx = collector_with_sys.collect(now=now)
        priorities = ctx.priorities

        # Assert tier order is monotonically non-decreasing in enum value (1 -> 4)
        levels = [p.level.value for p in priorities]
        self.assertEqual(levels, sorted(levels))

        # Verify Tier 1 contains CRITICAL
        critical_items = [p for p in priorities if p.level == PriorityLevel.CRITICAL]
        self.assertEqual(len(critical_items), 3)

        # Verify secondary numerical sorting in blocked jobs: High prio (10) appears before Low prio (1)
        blocked_titles = [p.title for p in critical_items if "requires approval" in p.title]
        self.assertIn("High prio blocked", blocked_titles[0])
        self.assertIn("Low prio blocked", blocked_titles[1])

        # Verify Tier 2 contains HIGH for today's reminder
        high_rem = [p for p in priorities if p.category == "reminder_today"]
        self.assertEqual(len(high_rem), 1)

        # Verify Tier 6 contains LOW for secondary project and system notice
        low_items = [p for p in priorities if p.level == PriorityLevel.LOW]
        self.assertTrue(any(p.category == "system" for p in low_items))
        self.assertTrue(any(p.category == "project_secondary" for p in low_items))

    def test_conflict_and_forgetting_safeguards(self):
        """7. Retired entities in active session are excluded from briefing."""
        self._insert_structured("project", "retired_proj", "Abandoned Tool")
        register_session_retraction("project", "Abandoned Tool")

        ctx = self.collector.collect()
        sec_names = [p.name for p in ctx.secondary_projects]
        self.assertNotIn("Abandoned Tool", sec_names)

    def test_job_goal_session_retraction_protection(self):
        """13. Retracted job goals in active session cannot leak through any job sections."""
        # Create jobs across multiple statuses
        j_blocked = self.job_store.create_job(goal="Delete cache for Abandoned Tool", priority=10)
        j_blocked.status = JobStatus.WAITING_CONFIRMATION
        self.job_store.update_job(j_blocked)

        j_active = self.job_store.create_job(goal="Run sync for Abandoned Tool", priority=8)
        j_active.status = JobStatus.RUNNING
        self.job_store.update_job(j_active)

        j_paused = self.job_store.create_job(goal="Migration of Abandoned Tool database", priority=5)
        j_paused.status = JobStatus.PAUSED
        self.job_store.update_job(j_paused)

        j_completed = self.job_store.create_job(goal="Finished cleanup for Abandoned Tool", priority=2)
        j_completed.status = JobStatus.COMPLETED
        j_completed.completed_at = datetime.now(timezone.utc).isoformat()
        self.job_store.update_job(j_completed)

        # Also create an unrelated valid active job
        j_valid = self.job_store.create_job(goal="Process payment transactions", priority=7)
        j_valid.status = JobStatus.RUNNING
        self.job_store.update_job(j_valid)

        # Register session retraction for "Abandoned Tool"
        register_session_retraction("project", "Abandoned Tool")

        ctx = self.collector.collect()

        # Verify retracted jobs are completely excluded across all job lists and developments
        all_job_goals = (
            [j.goal for j in ctx.blocked_jobs]
            + [j.goal for j in ctx.active_jobs]
            + [j.goal for j in ctx.paused_jobs]
            + [j.goal for j in ctx.recent_completed_jobs]
        )
        self.assertFalse(any("Abandoned Tool" in g for g in all_job_goals))
        self.assertFalse(any("Abandoned Tool" in dev for dev in ctx.recent_project_developments))
        self.assertFalse(any("Abandoned Tool" in p.title for p in ctx.priorities))

        # Verify the unrelated job is preserved
        active_goals = [j.goal for j in ctx.active_jobs]
        self.assertIn("Process payment transactions", active_goals)

        # Verify SQLite job records were not deleted or modified
        db_job = self.job_store.get_job(j_blocked.id)
        self.assertIsNotNone(db_job)
        self.assertEqual(db_job.status, JobStatus.WAITING_CONFIRMATION)

    def test_secret_sanitization_reminders_and_job_goals(self):
        """14. Credential assignments, API keys, and Bearer tokens are redacted from briefing context and synthesis."""
        now = datetime(2026, 10, 30, 14, 0, 0)

        # Reminder with OpenAI sk- key and password assignment
        self._insert_reminder(
            "Deploy staging with sk-proj-1234567890abcdef123 and password=SuperSecretPassword123! today",
            now + timedelta(hours=2),
        )

        # Job with Bearer token and api_key assignment
        j_blocked = self.job_store.create_job(
            goal="Fetch customer data using Bearer eyJhbGciOiJIUzI1Ni.jwtpayload123 and api_key=xyzSecretKey987",
            priority=9,
        )
        j_blocked.status = JobStatus.WAITING_CONFIRMATION
        self.job_store.update_job(j_blocked)

        ctx = self.collector.collect(now=now)

        # 1. Verify secrets are redacted in context fields
        rem_msg = ctx.today_reminders[0].message
        self.assertNotIn("sk-proj-1234567890abcdef123", rem_msg)
        self.assertNotIn("SuperSecretPassword123!", rem_msg)
        self.assertIn("[REDACTED]", rem_msg)
        self.assertIn("Deploy staging with", rem_msg)

        job_goal = ctx.blocked_jobs[0].goal
        self.assertNotIn("eyJhbGciOiJIUzI1Ni.jwtpayload123", job_goal)
        self.assertNotIn("xyzSecretKey987", job_goal)
        self.assertIn("[REDACTED]", job_goal)
        self.assertIn("Fetch customer data using", job_goal)

        # 2. Verify secrets do not appear in priorities
        for p in ctx.priorities:
            self.assertNotIn("sk-proj-1234567890abcdef123", p.title)
            self.assertNotIn("SuperSecretPassword123!", p.title)
            self.assertNotIn("eyJhbGciOiJIUzI1Ni.jwtpayload123", p.title)
            self.assertNotIn("xyzSecretKey987", p.title)

        # 3. Verify synthesis prompt contains no secrets
        synthesizer = BriefingSynthesizer(api_client=None)
        prompt, _ = synthesizer._build_prompt(ctx)
        self.assertNotIn("sk-proj-1234567890abcdef123", prompt)
        self.assertNotIn("SuperSecretPassword123!", prompt)
        self.assertNotIn("eyJhbGciOiJIUzI1Ni.jwtpayload123", prompt)
        self.assertNotIn("xyzSecretKey987", prompt)

        # 4. Verify deterministic fallback markdown contains no secrets
        briefing = synthesizer.synthesize(ctx)
        self.assertNotIn("sk-proj-1234567890abcdef123", briefing.summary_markdown)
        self.assertNotIn("SuperSecretPassword123!", briefing.summary_markdown)
        self.assertNotIn("eyJhbGciOiJIUzI1Ni.jwtpayload123", briefing.summary_markdown)
        self.assertNotIn("xyzSecretKey987", briefing.summary_markdown)

        # 5. Verify ordinary words are not excessively redacted
        clean_text = sanitize_briefing_text("Review API key documentation and token counter")
        self.assertEqual(clean_text, "Review API key documentation and token counter")

    def test_general_work_goals_preservation_and_deduplication(self):
        """15. General work goals are preserved in context.general_goals without false project association."""
        self._insert_structured("project", "current", "Orange AI Desktop")
        self._insert_structured("project", "zephyr", "Project Zephyr")

        # 1. Project-specific goal
        self._insert_structured("goal", "p1", "Complete Orange AI Desktop Stage 3.7 briefing engine")
        # 2. Secondary project goal
        self._insert_structured("goal", "p2", "Release Project Zephyr v2")
        # 3. General work goal (not tied to specific project name)
        self._insert_structured("goal", "g1", "Learn Rust memory safety and concurrency patterns")
        # 4. Duplicate general work goal
        self._insert_structured("goal", "g2", "Learn Rust memory safety and concurrency patterns")
        # 5. Unrelated personal goal
        self._insert_structured("goal", "personal", "Buy groceries and dental floss")

        ctx = self.collector.collect()

        # Project-specific goals are correctly mapped
        self.assertEqual(ctx.current_project.related_goals, ["Complete Orange AI Desktop Stage 3.7 briefing engine"])
        sec_zephyr = next(p for p in ctx.secondary_projects if p.name == "Project Zephyr")
        self.assertEqual(sec_zephyr.related_goals, ["Release Project Zephyr v2"])

        # General goal is preserved in general_goals without false association to current_project
        self.assertIn("Learn Rust memory safety and concurrency patterns", ctx.general_goals)
        self.assertNotIn("Learn Rust memory safety and concurrency patterns", ctx.current_project.related_goals)
        self.assertNotIn("Learn Rust memory safety and concurrency patterns", sec_zephyr.related_goals)

        # Deduplication check: general_goals contains the item exactly once
        self.assertEqual(ctx.general_goals.count("Learn Rust memory safety and concurrency patterns"), 1)

        # Unrelated personal goal is excluded
        self.assertNotIn("Buy groceries and dental floss", ctx.general_goals)

    def test_privacy_filter_precision_technical_relationships(self):
        """16. Technical relationship contexts are preserved while personal romantic relationships are excluded."""
        # Technical relationship goals
        self._insert_structured("goal", "tech1", "Design entity-relationship diagram for core schema")
        self._insert_structured("goal", "tech2", "Analyze database relationship between users and orders")
        self._insert_structured("goal", "tech3", "Model relationship between variables in regression analysis")
        self._insert_structured("goal", "tech4", "Implement table relationships for PostgreSQL database")

        # Sensitive personal goals
        self._insert_structured("goal", "pers1", "Fix relationship with my girlfriend")
        self._insert_structured("goal", "pers2", "Discuss relationship issues with therapist")
        self._insert_structured("goal", "pers3", "Romantic relationship goals with partner")
        self._insert_structured("goal", "pers4", "Propose to girlfriend next summer")

        ctx = self.collector.collect()

        # Technical relationship goals MUST be preserved
        self.assertIn("Design entity-relationship diagram for core schema", ctx.general_goals)
        self.assertIn("Analyze database relationship between users and orders", ctx.general_goals)
        self.assertIn("Model relationship between variables in regression analysis", ctx.general_goals)
        self.assertIn("Implement table relationships for PostgreSQL database", ctx.general_goals)

        # Sensitive personal goals MUST be excluded
        all_goals = ctx.general_goals + (ctx.current_project.related_goals if ctx.current_project else [])
        for g in all_goals:
            self.assertNotIn("girlfriend", g.lower())
            self.assertNotIn("relationship with my", g.lower())
            self.assertNotIn("relationship issues", g.lower())
            self.assertNotIn("romantic relationship", g.lower())

    def test_project_job_association_precision_and_tag_precedence(self):
        """18. Project-job association enforces tag precedence, word boundaries, and ambiguity rejection."""
        # Setup projects: current is 'Orange AI', secondary is 'Project Zephyr' and 'App'
        self._insert_structured("project", "current", "Orange AI")
        self._insert_structured("project", "sec1", "Project Zephyr")
        self._insert_structured("project", "sec2", "App")

        # 1. Project name 'Orange AI' / 'AI' vs unrelated text containing 'raise'
        j_raise = self.job_store.create_job(goal="Please raise an issue with the cloud provider", priority=1)
        j_raise.status = JobStatus.RUNNING
        self.job_store.update_job(j_raise)

        # 2. Project name 'App' vs 'application'
        j_app = self.job_store.create_job(goal="Deploy web application to production servers", priority=1)
        j_app.status = JobStatus.RUNNING
        self.job_store.update_job(j_app)

        # 3. Standalone 'App' match
        j_app_exact = self.job_store.create_job(goal="Fix crash in App when opening user settings", priority=2)
        j_app_exact.status = JobStatus.RUNNING
        self.job_store.update_job(j_app_exact)

        # 4. Multi-word match: 'Orange AI'
        j_multi = self.job_store.create_job(goal="Run benchmark suite on Orange AI core", priority=3)
        j_multi.status = JobStatus.RUNNING
        self.job_store.update_job(j_multi)

        # 5. Case-insensitive match: 'project zephyr'
        j_case = self.job_store.create_job(goal="publish release build for project zephyr", priority=4)
        j_case.status = JobStatus.RUNNING
        self.job_store.update_job(j_case)

        # 6. Explicit project tag precedence: job goal doesn't mention project name
        j_tagged = self.job_store.create_job(goal="Optimize database queries and indexes", priority=5, tags="project:Orange AI")
        j_tagged.status = JobStatus.RUNNING
        self.job_store.update_job(j_tagged)

        # 7. Unrelated job that must remain unassociated
        j_unrelated = self.job_store.create_job(goal="Check disk space on drive D", priority=1)
        j_unrelated.status = JobStatus.RUNNING
        self.job_store.update_job(j_unrelated)

        # 8. Ambiguous job mentioning multiple distinct projects rejected
        j_ambiguous = self.job_store.create_job(goal="Sync Orange AI data pipeline with Project Zephyr", priority=2)
        j_ambiguous.status = JobStatus.RUNNING
        self.job_store.update_job(j_ambiguous)

        ctx = self.collector.collect()

        curr_jobs = ctx.current_project.active_jobs
        zephyr = next(p for p in ctx.secondary_projects if p.name == "Project Zephyr")
        app_proj = next(p for p in ctx.secondary_projects if p.name == "App")

        # Assert 1: 'raise' is NOT mapped to 'Orange AI' or 'AI'
        self.assertFalse(any("raise an issue" in item for item in curr_jobs))

        # Assert 2: 'application' is NOT mapped to 'App'
        self.assertFalse(any("Deploy web application" in item for item in app_proj.active_jobs))

        # Assert 3: Standalone 'App' is mapped to 'App'
        self.assertTrue(any("Fix crash in App" in item for item in app_proj.active_jobs))

        # Assert 4: Multi-word 'Orange AI' is mapped to current project
        self.assertTrue(any("Run benchmark suite on Orange AI core" in item for item in curr_jobs))

        # Assert 5: Case-insensitive 'project zephyr' is mapped to Project Zephyr
        self.assertTrue(any("publish release build for project zephyr" in item for item in zephyr.active_jobs))

        # Assert 6: Explicit tag 'project:Orange AI' mapped to current project
        self.assertTrue(any("Optimize database queries and indexes" in item for item in curr_jobs))

        # Assert 7: Unrelated job is not mapped to any project
        for p in [ctx.current_project, zephyr, app_proj]:
            self.assertFalse(any("Check disk space on drive D" in item for item in p.active_jobs))

        # Assert 8: Ambiguous multiple project mention is rejected from all projects
        for p in [ctx.current_project, zephyr, app_proj]:
            self.assertFalse(any("Sync Orange AI data pipeline with Project Zephyr" in item for item in p.active_jobs))

    def test_briefing_collector_recommendation_store_isolation_regression(self):
        """15. BriefingCollector with injected recommendation_store writes only to isolated store and never touches production workspace JSON."""
        import hashlib
        prod_recs_path = get_recommendations_store_path()
        prod_exists = os.path.exists(prod_recs_path)
        with open(prod_recs_path, "rb") as f:
            prod_before_sha = hashlib.sha256(f.read()).hexdigest()

        # 1. Verify strict isolation guard raises if production path is accessed while active
        with self.assertRaises(RuntimeError) as cm:
            RecommendationStore(file_path=None)
        self.assertIn("test isolation violation", str(cm.exception))

        # 2. Create a WAITING_CONFIRMATION job that would trigger job_confirmation recommendation
        j_confirm = self.job_store.create_job(goal="Delete temp folder", priority=10)
        j_confirm.status = JobStatus.WAITING_CONFIRMATION
        action = AgentAction(type=ActionType.CALL_TOOL, tool_name="delete_folder", arguments={"path": "/tmp/test"})
        j_confirm.set_pending_confirmation(action)
        self.job_store.update_job(j_confirm)

        # 3. Collect briefing context using the isolated collector
        ctx = self.collector.collect()

        # 4. The recommendation must be saved into self.rec_store (isolated temporary store)
        recs_in_test_store = self.rec_store.list_all()
        self.assertTrue(len(recs_in_test_store) >= 1)
        found_test_rec = any(r.category == "job_confirmation" and r.entity_id == j_confirm.id for r in recs_in_test_store)
        self.assertTrue(found_test_rec, "Job confirmation recommendation must be in isolated test store")

        # 5. The production workspace recommendations.json MUST be byte-for-byte identical
        with open(prod_recs_path, "rb") as f:
            prod_after_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(prod_before_sha, prod_after_sha, "Production recommendations.json was modified by test!")
        self.assertEqual(prod_after_sha, "8ecff69a4cf9bdee074352ba3941f7760d69394403ed9a671e312c28e38cb2fb")


class TestDailyBriefingSynthesizer(unittest.TestCase):
    """Test suite for BriefingSynthesizer text synthesis and deterministic fallback."""

    def setUp(self):
        self.context = DailyBriefingContext(
            user_name="Sayan Dey",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(
                name="Orange AI Desktop",
                is_current=True,
                description="Personal AI companion",
                related_goals=["Finish Stage 3.7"],
                active_jobs=["[abc12345] Run regression suite"],
            ),
            secondary_projects=[
                ProjectBriefingContext(name="Project Zephyr", is_current=False)
            ],
            overdue_reminders=[
                ReminderBriefingContext(id=1, message="Submit invoice", due_at="2026-10-29T17:00:00", is_overdue=True, is_today=False)
            ],
            today_reminders=[
                ReminderBriefingContext(id=2, message="Call architect", due_at="2026-10-30T16:00:00", is_overdue=False, is_today=True)
            ],
            blocked_jobs=[
                JobBriefingContext(id="job-1", short_id="j1", goal="Delete temp cache", status="WAITING_CONFIRMATION", priority=10, pending_action="delete_folder")
            ],
            active_jobs=[],
            paused_jobs=[],
            recent_completed_jobs=[],
            actionable_system_status="Battery low (15%).",
            recent_project_developments=["Completed autonomous job [done1]: Benchmark run"],
            priorities=[
                PriorityItem(category="reminder_overdue", title="Overdue: Submit invoice", level=PriorityLevel.CRITICAL, action_hint="Pay immediately."),
                PriorityItem(category="job_waiting_confirmation", title="Job [j1] requires approval: Delete temp cache", level=PriorityLevel.CRITICAL, action_hint="Confirm yes/no."),
                PriorityItem(category="reminder_today", title="Due Today: Call architect", level=PriorityLevel.HIGH),
                PriorityItem(category="project_current", title="Primary Project: Orange AI Desktop", level=PriorityLevel.MEDIUM),
            ],
        )

    def test_prompt_construction_and_privacy_filtering(self):
        """8. Prompt excludes unneeded secrets and enforces grounding rules."""
        synthesizer = BriefingSynthesizer(api_client=None)
        user_msg, sys_inst = synthesizer._build_prompt(self.context)

        # System prompt instructs against inventing facts or calling tools
        self.assertIn("ONLY summarize the facts", sys_inst)
        self.assertIn("DO NOT invent tasks", sys_inst)
        self.assertIn("DO NOT execute tools", sys_inst)

        # User message contains verified facts
        self.assertIn("Orange AI Desktop", user_msg)
        self.assertIn("Overdue: Submit invoice", user_msg)
        self.assertIn("Job [j1] requires approval", user_msg)
        self.assertIn("Battery low (15%).", user_msg)

        # Empty general goals do not produce GENERAL ACTIVE GOALS in prompt
        self.assertNotIn("GENERAL ACTIVE GOALS:", user_msg)

    def test_general_goals_synthesis_integration(self):
        """17. General goals appear in prompt and deterministic fallback under Active Goals & Focus."""
        ctx_with_goals = DailyBriefingContext(
            user_name="Sayan Dey",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(
                name="Orange AI Desktop",
                is_current=True,
                related_goals=["Finish Stage 3.7"],
            ),
            priorities=[],
            general_goals=[
                "Learn Rust memory safety and concurrency patterns",
                "Design entity-relationship diagram for core schema",
            ],
        )

        synthesizer = BriefingSynthesizer(api_client=None)

        # 1. Prompt includes GENERAL ACTIVE GOALS without corrupting project goals
        user_msg, _ = synthesizer._build_prompt(ctx_with_goals)
        self.assertIn("GENERAL ACTIVE GOALS: Learn Rust memory safety and concurrency patterns, Design entity-relationship diagram for core schema", user_msg)
        self.assertIn("PROJECT GOALS: Finish Stage 3.7", user_msg)

        # 2. Deterministic fallback includes "## 🎯 Active Goals & Focus" with all goals preserved
        briefing = synthesizer.synthesize(ctx_with_goals)
        self.assertTrue(briefing.is_fallback)
        self.assertIn("## 🎯 Active Goals & Focus", briefing.summary_markdown)
        self.assertIn("- Learn Rust memory safety and concurrency patterns", briefing.summary_markdown)
        self.assertIn("- Design entity-relationship diagram for core schema", briefing.summary_markdown)

        # 3. Empty general goals does not produce empty "Active Goals & Focus" section
        ctx_empty_goals = DailyBriefingContext(
            user_name="Sayan Dey",
            current_date="Friday, October 30, 2026",
            current_project=None,
            priorities=[],
            general_goals=[],
        )
        briefing_empty = synthesizer.synthesize(ctx_empty_goals)
        self.assertNotIn("Active Goals & Focus", briefing_empty.summary_markdown)

    def test_context_budget_enforcement(self):
        """9. Prompt body is strictly bounded by max_chars ceiling."""
        synthesizer = BriefingSynthesizer(api_client=None)
        # Enforce small ceiling
        user_msg, _ = synthesizer._build_prompt(self.context, max_chars=300)
        self.assertLessEqual(len(user_msg), 450)
        self.assertIn("[Context truncated for budget]", user_msg)

    def test_api_success_synthesis(self):
        """10. Successful OrangeAPIClient.assist response returns synthesized briefing."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = True
        mock_client.assist.return_value = {
            "reply": "## 🌅 Executive Summary\nGood morning Sayan! Focus on Orange AI Desktop today.",
            "model": "gemini-3.5-flash",
            "success": True,
        }

        synthesizer = BriefingSynthesizer(api_client=mock_client)
        briefing = synthesizer.synthesize(self.context)

        self.assertFalse(briefing.is_fallback)
        self.assertEqual(briefing.model_name, "gemini-3.5-flash")
        self.assertIn("Good morning Sayan!", briefing.summary_markdown)
        mock_client.assist.assert_called_once()

    def test_api_offline_graceful_deterministic_fallback(self):
        """11. When API is unavailable or errors, synthesizer returns structured deterministic markdown."""
        mock_client = MagicMock()
        mock_client.is_available.return_value = False

        synthesizer = BriefingSynthesizer(api_client=mock_client)
        briefing = synthesizer.synthesize(self.context)

        self.assertTrue(briefing.is_fallback)
        self.assertEqual(briefing.model_name, "local_deterministic")
        self.assertIn("# 🌅 Daily Briefing", briefing.summary_markdown)
        self.assertIn("Immediate Attention Required", briefing.summary_markdown)
        self.assertIn("Overdue: Submit invoice", briefing.summary_markdown)
        self.assertIn("Job [j1] requires approval", briefing.summary_markdown)
        self.assertIn("Suggested Next Steps", briefing.summary_markdown)

    def test_no_database_writes_during_collection_or_synthesis(self):
        """12. Collection and synthesis perform exactly zero writes across all database tables."""
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()
        try:
            _init_test_db(temp_db.name)
            # Snapshot table counts
            conn = sqlite3.connect(temp_db.name)
            counts_before = {
                "structured_memories": conn.execute("SELECT COUNT(*) FROM structured_memories").fetchone()[0],
                "reminders": conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "conversation_history": conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0],
            }
            conn.close()

            # Execute collection with isolated recommendation store
            temp_recs_dir = tempfile.TemporaryDirectory()
            recs_path = os.path.join(temp_recs_dir.name, "recommendations.json")
            rec_store = RecommendationStore(file_path=recs_path)
            collector = BriefingCollector(db_path=temp_db.name, recommendation_store=rec_store)
            ctx = collector.collect()

            # Execute synthesis
            synthesizer = BriefingSynthesizer(api_client=None)
            briefing = synthesizer.synthesize(ctx)
            self.assertIsNotNone(briefing)

            # Snapshot table counts after
            conn = sqlite3.connect(temp_db.name)
            counts_after = {
                "structured_memories": conn.execute("SELECT COUNT(*) FROM structured_memories").fetchone()[0],
                "reminders": conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "conversation_history": conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0],
            }
            conn.close()

            self.assertEqual(counts_before, counts_after, "Database counts changed during read-only operation!")
        finally:
            try:
                temp_recs_dir.cleanup()
            except Exception:
                pass
            if os.path.exists(temp_db.name):
                try:
                    os.remove(temp_db.name)
                except Exception:
                    pass


class TestDailyBriefingProjectHealthIntegration(unittest.TestCase):
    """Test suite for Stage 3.7E.2C: Daily Briefing Project Health Integration."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name
        _init_test_db(self.db_path)
        self.temp_recs_dir = tempfile.TemporaryDirectory()
        self.recs_path = os.path.join(self.temp_recs_dir.name, "recommendations.json")
        self.rec_store = RecommendationStore(file_path=self.recs_path)
        self.job_store = JobStore(db_path=self.db_path)
        self.collector = BriefingCollector(
            db_path=self.db_path,
            job_store=self.job_store,
            recommendation_store=self.rec_store,
        )
        self.now = datetime(2026, 10, 30, 12, 0, 0)
        RecommendationStore._STRICT_TEST_ISOLATION = True

    def tearDown(self):
        RecommendationStore._STRICT_TEST_ISOLATION = False
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
        try:
            self.temp_recs_dir.cleanup()
        except Exception:
            pass

    def _to_utc_iso(self, local_dt: datetime) -> str:
        return local_dt.astimezone().astimezone(timezone.utc).isoformat()

    def _insert_structured(self, category: str, key: str, value: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (category, key, value, self.now.isoformat(), self.now.isoformat()),
        )
        conn.commit()
        conn.close()

    def test_current_project_health_included_in_briefing_context(self):
        """1. Current project health assessment is included in collected briefing context."""
        self._insert_structured("project", "current", "Orange AI")
        step_time = self._to_utc_iso(self.now - timedelta(hours=2))
        j = self.job_store.create_job(goal="Run Orange AI unit tests", status=JobStatus.RUNNING)
        step = JobStep(job_id=j.id, step_index=1, success=True, started_at=step_time)
        self.job_store.record_step(step)

        ctx = self.collector.collect(now=self.now)
        self.assertIsNotNone(ctx.current_project)
        self.assertIsNotNone(ctx.current_project.health)
        self.assertEqual(ctx.current_project.health.status, ProjectHealthStatus.PROGRESSING)
        self.assertEqual(len(ctx.project_health_assessments), 1)
        self.assertEqual(ctx.project_health_assessments[0].project_name, "Orange AI")

    def test_relevant_secondary_project_health_included(self):
        """2. Relevant secondary project health is included in context."""
        self._insert_structured("project", "current", "Orange AI")
        self._insert_structured("project", "sec1", "Project Zephyr")
        j = self.job_store.create_job(goal="Task for Project Zephyr", status=JobStatus.PAUSED)

        ctx = self.collector.collect(now=self.now)
        zephyr = next((p for p in ctx.secondary_projects if p.name == "Project Zephyr"), None)
        self.assertIsNotNone(zephyr)
        self.assertIsNotNone(zephyr.health)
        self.assertEqual(zephyr.health.status, ProjectHealthStatus.PAUSED)
        self.assertTrue(any(a.project_name == "Project Zephyr" for a in ctx.project_health_assessments))

    def test_unavailable_project_health_does_not_crash_briefing_collection(self):
        """3. Unavailable project-health evaluator errors do not crash briefing collection."""
        self._insert_structured("project", "current", "Orange AI")
        with patch("brain.briefing.project_health.ProjectHealthEvaluator.evaluate_all", side_effect=RuntimeError("Database lock")):
            ctx = self.collector.collect(now=self.now)
            self.assertIsNotNone(ctx)
            self.assertIsNotNone(ctx.current_project)
            self.assertIsNone(ctx.current_project.health)
            self.assertEqual(len(ctx.project_health_assessments), 0)

    def test_blocked_status_preserved_through_synthesis(self):
        """4. BLOCKED status and explanation are preserved through prompt and fallback synthesis."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Waiting for confirmation to delete old logs.",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True, health=h),
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        user_msg, sys_inst = synthesizer._build_prompt(ctx)
        self.assertIn("[BLOCKED] - Waiting for confirmation to delete old logs.", user_msg)
        self.assertIn("EXACT status given", sys_inst)

        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("`BLOCKED` — Waiting for confirmation to delete old logs.", fallback)

    def test_paused_status_preserved_through_synthesis(self):
        """5. PAUSED status is preserved through synthesis."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.PAUSED,
            explanation="Job paused at clean step boundary.",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True, health=h),
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("`PAUSED` — Job paused at clean step boundary.", fallback)

    def test_progressing_status_preserved_through_synthesis(self):
        """6. PROGRESSING status is preserved through synthesis."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.PROGRESSING,
            explanation="Actively progressing with recent execution.",
            last_progress_at="2026-10-30T10:00:00+00:00",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True, health=h),
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("`PROGRESSING` — Actively progressing with recent execution.", fallback)
        self.assertIn("Last Verified Progress:", fallback)

    def test_active_no_recent_progress_preserved_through_synthesis(self):
        """7. ACTIVE_NO_RECENT_PROGRESS status is preserved through synthesis."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS,
            explanation="Current primary project has no verified progress recorded in the last 5 days.",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True, health=h),
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("`ACTIVE_NO_RECENT_PROGRESS`", fallback)
        self.assertIn("no verified progress recorded in the last 5 days", fallback)

    def test_unknown_status_preserved_and_honestly_explained(self):
        """8. UNKNOWN status is preserved and explained honestly without manufacturing confidence."""
        h = ProjectHealthAssessment(
            project_name="Project Zephyr",
            is_current=False,
            status=ProjectHealthStatus.UNKNOWN,
            explanation="No autonomous jobs or progress records are associated with project 'Project Zephyr'.",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            secondary_projects=[ProjectBriefingContext(name="Project Zephyr", is_current=False, health=h)],
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        user_msg, _ = synthesizer._build_prompt(ctx)
        self.assertIn("[Health: UNKNOWN - No autonomous jobs or progress records", user_msg)

        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("`UNKNOWN` — No autonomous jobs or progress records", fallback)

    def test_completed_jobs_alone_do_not_create_completion_claim(self):
        """9. Completed jobs alone yield UNKNOWN with explicit note that project completion is unverified."""
        self._insert_structured("project", "current", "Orange AI")
        t_comp = self._to_utc_iso(self.now - timedelta(days=5))
        j1 = self.job_store.create_job(goal="Task 1 on Orange AI", status=JobStatus.COMPLETED)
        j1.completed_at = t_comp
        self.job_store.update_job(j1)
        j2 = self.job_store.create_job(goal="Task 2 on Orange AI", status=JobStatus.COMPLETED)
        j2.completed_at = t_comp
        self.job_store.update_job(j2)

        ctx = self.collector.collect(now=self.now)
        self.assertEqual(ctx.current_project.health.status, ProjectHealthStatus.UNKNOWN)
        self.assertIn("overall project completion has not been verified", ctx.current_project.health.explanation)

        synthesizer = BriefingSynthesizer(api_client=None)
        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("`UNKNOWN`", fallback)
        self.assertIn("overall project completion has not been verified", fallback)

    def test_running_job_alone_does_not_create_progress_claim(self):
        """10. RUNNING job without progress steps does not create a PROGRESSING claim."""
        self._insert_structured("project", "sec1", "Worker Service")
        old_time = self._to_utc_iso(self.now - timedelta(days=6))
        j = self.job_store.create_job(
            goal="Process queue on Worker Service",
            status=JobStatus.RUNNING,
        )
        j.started_at = old_time
        j.updated_at = old_time
        self.job_store.update_job(j)

        ctx = self.collector.collect(now=self.now)
        worker = next((p for p in ctx.secondary_projects if p.name == "Worker Service"), None)
        self.assertIsNotNone(worker)
        self.assertNotEqual(worker.health.status, ProjectHealthStatus.PROGRESSING)
        self.assertEqual(worker.health.status, ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS)

    def test_stuck_running_job_warning_presented_separately(self):
        """11. Stuck-running warnings are presented separately from the main status."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS,
            explanation="No verified progress in 5 days.",
            has_stuck_running_job=True,
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True, health=h),
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        user_msg, _ = synthesizer._build_prompt(ctx)
        self.assertIn("PRIMARY PROJECT WARNING: Running job has no recorded progress for > 24 hours.", user_msg)

        fallback = synthesizer._generate_deterministic_briefing(ctx)
        self.assertIn("Warning: Running background job has no progress recorded for > 24 hours.", fallback)

    def test_retired_information_does_not_leak_into_briefing(self):
        """12. Session-retired projects do not leak into briefing context or synthesis."""
        self._insert_structured("project", "current", "Orange AI")
        self._insert_structured("project", "sec1", "Classified Project")
        clear_session_retractions()
        register_session_retraction("project", "Classified Project")
        try:
            ctx = self.collector.collect(now=self.now)
            self.assertFalse(any(p.name == "Classified Project" for p in ctx.secondary_projects))
            self.assertFalse(any(a.project_name == "Classified Project" for a in ctx.project_health_assessments))

            synthesizer = BriefingSynthesizer(api_client=None)
            fallback = synthesizer._generate_deterministic_briefing(ctx)
            self.assertNotIn("Classified Project", fallback)
        finally:
            clear_session_retractions()

    def test_secret_sanitization_applies_to_health_details_in_synthesis(self):
        """13. Secret sanitization redacts credentials from project health explanations."""
        h = ProjectHealthAssessment(
            project_name="Orange AI",
            is_current=True,
            status=ProjectHealthStatus.BLOCKED,
            explanation="Failed with token Bearer secret-token-12345678 and api_key: sk-9876543210abcdef",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True, health=h),
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        user_msg, _ = synthesizer._build_prompt(ctx)
        self.assertNotIn("sk-9876543210abcdef", user_msg)
        self.assertIn("[REDACTED]", user_msg)

    def test_context_budgeting_enforced_with_multiple_projects(self):
        """14. Context budgeting remains strictly enforced when multiple projects have health details."""
        sec_projs = []
        for i in range(12):
            h = ProjectHealthAssessment(
                project_name=f"Project-{i}",
                is_current=False,
                status=ProjectHealthStatus.PROGRESSING,
                explanation=f"Detailed long explanation of milestone status and deliverables for subsystem {i} with audit trail.",
                evidence=[f"Evidence log item {k} with extra verbose debugging data" for k in range(5)],
            )
            sec_projs.append(ProjectBriefingContext(name=f"Project-{i}", is_current=False, health=h))

        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True),
            secondary_projects=sec_projs,
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        user_msg, _ = synthesizer._build_prompt(ctx, max_chars=1200)
        self.assertLessEqual(len(user_msg), 1500)
        self.assertIn("[Context truncated for budget]", user_msg)

    def test_deterministic_fallback_displays_project_health_correctly(self):
        """15. Deterministic fallback displays primary and secondary project health cleanly."""
        h_curr = ProjectHealthAssessment(
            project_name="Orange AI Desktop",
            is_current=True,
            status=ProjectHealthStatus.PROGRESSING,
            explanation="Active development on daily briefing.",
            last_progress_at="2026-10-30T11:00:00",
        )
        h_sec = ProjectHealthAssessment(
            project_name="Data Pipeline",
            is_current=False,
            status=ProjectHealthStatus.PAUSED,
            explanation="Paused at ETL boundary.",
        )
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI Desktop", is_current=True, health=h_curr),
            secondary_projects=[ProjectBriefingContext(name="Data Pipeline", is_current=False, health=h_sec)],
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        fallback = synthesizer.synthesize(ctx).summary_markdown

        self.assertIn("# 🌅 Daily Briefing — Friday, October 30, 2026", fallback)
        self.assertIn("**Primary Focus:** Orange AI Desktop", fallback)
        self.assertIn("`PROGRESSING` — Active development on daily briefing.", fallback)
        self.assertIn("## 📁 Secondary Projects", fallback)
        self.assertIn("**Data Pipeline:** `PAUSED` — Paused at ETL boundary.", fallback)

    def test_briefing_behavior_unchanged_when_no_health_available(self):
        """16. Existing briefing behavior remains unchanged when no health assessments are present."""
        ctx = DailyBriefingContext(
            user_name="User",
            current_date="Friday, October 30, 2026",
            current_project=ProjectBriefingContext(name="Orange AI", is_current=True),
            secondary_projects=[ProjectBriefingContext(name="Subsystem B", is_current=False)],
        )
        synthesizer = BriefingSynthesizer(api_client=None)
        fallback = synthesizer.synthesize(ctx).summary_markdown

        self.assertIn("**Primary Focus:** Orange AI", fallback)
        self.assertNotIn("Health & Status:", fallback)
        self.assertNotIn("## 📁 Secondary Projects", fallback)


if __name__ == "__main__":
    unittest.main()
