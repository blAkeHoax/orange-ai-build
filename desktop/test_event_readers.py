"""
test_event_readers.py - Test Suite for Read-Only Event Detection & Candidate Generation.

Stage 3.7E.3B: Event Readers & Candidate Generation
Verifies:
1. Watermark behavior (read-only integration, fail-closed on missing/corrupt store, zero mutations).
2. Job events (completed, failed, waiting_confirmation, paused, retrying, source preservation, stable IDs, watermark filtering).
3. Job-step events (successful steps, failed steps, null completed_at handling, attempt distinctions, watermark max_id filtering).
4. Reminder events (due unfired, future ignored, fired ignored, rescheduling changes identity, zero mutations).
5. Memory events (project create/update, non-project memories, session retirement, sensitive filtering, secret sanitization, watermark filtering).
6. Candidate integrity (ordering, stability, material distinctions, no executable code, empty vs failure distinction).
"""

import os
import sqlite3
import tempfile
import shutil
import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from brain.briefing.models import EventCandidate, EventPriority
from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EVENT_WATERMARKS_FILENAME,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
)
from brain.briefing.event_readers import (
    EventCandidateReader,
    sort_candidates,
    parse_timestamp_naive,
)
from brain.conflict_resolver import register_session_retraction, clear_session_retractions


class BaseEventReaderTestCase(unittest.TestCase):
    """Test fixture providing isolated temporary directory and SQLite database."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_readers_")
        self.db_path = os.path.join(self.test_dir, "test_memory.db")
        self.store_path = os.path.join(self.test_dir, EVENT_WATERMARKS_FILENAME)
        self.fixed_now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
        self.fixed_local_now = datetime(2026, 10, 31, 12, 0, 0)

        # Clear any in-memory retractions
        clear_session_retractions()

        # Initialize SQLite test database with actual application schemas
        self._init_test_database()

    def tearDown(self):
        clear_session_retractions()
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    def _init_test_database(self):
        """Create exact schemas for jobs, job_steps, reminders, and structured_memories."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE jobs (
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
            );

            CREATE TABLE job_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
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
                model_name TEXT
            );

            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fired INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE structured_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()


# ===========================================================================
# Suite 1: Watermark Behavior & Fail-Closed
# ===========================================================================

class TestWatermarkBehaviorAndFailClosed(BaseEventReaderTestCase):
    """Tests watermark read-only integration, zero-mutation guarantee, and fail-closed handling."""

    def test_valid_watermark_store_loads_and_reads_snapshot(self):
        """Reader reads a valid watermark store without error."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)

        candidates = reader.collect_all_candidates(now=self.fixed_local_now)
        self.assertIsInstance(candidates, list)
        self.assertEqual(len(candidates), 0, "Empty database produces empty candidate list")

    def test_missing_watermark_store_fails_closed(self):
        """If watermark store file is missing, collect_all_candidates must raise EventStoreNotFoundError."""
        self.assertFalse(os.path.exists(self.store_path))
        reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)

        with self.assertRaises(EventStoreNotFoundError):
            reader.collect_all_candidates(now=self.fixed_local_now)

        # Confirm store was not created silently
        self.assertFalse(os.path.exists(self.store_path), "Missing store must NOT be created automatically")

    def test_corrupt_watermark_store_fails_closed(self):
        """If watermark store is corrupt, candidate generation must raise EventStoreCorruptError."""
        with open(self.store_path, "w", encoding="utf-8") as f:
            f.write("{BROKEN CORRUPT JSON")

        reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)
        with self.assertRaises(EventStoreCorruptError):
            reader.collect_all_candidates(now=self.fixed_local_now)

    def test_reader_never_mutates_or_advances_watermarks(self):
        """Collecting candidates must NEVER advance watermarks or mutate the watermark file."""
        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 5},
            now=self.fixed_now,
        )
        before_mtime = os.path.getmtime(self.store_path)

        # Insert candidate jobs and steps
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_1', 'Backup database', 'COMPLETED', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')"
        )
        conn.execute(
            "INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) "
            "VALUES (10, 'job_1', 1, 1, 'shell', 1, '2026-10-31T10:01:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)
        candidates = reader.collect_all_candidates(now=self.fixed_local_now)
        self.assertGreaterEqual(len(candidates), 1)

        # Check watermark store: position must still be 5
        reloaded = EventWatermarkStore.load(self.store_path)
        self.assertEqual(reloaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 5)
        self.assertEqual(os.path.getmtime(self.store_path), before_mtime, "Watermark file must not be touched")

    def test_reader_never_records_delivery_or_prunes_records(self):
        """Candidate scanning must not record delivery IDs into the store."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_deliv', 'Sync files', 'COMPLETED', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)
        candidates = reader.collect_all_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)

        # Verify delivered_events in store remains empty
        reloaded = EventWatermarkStore.load(self.store_path)
        self.assertEqual(len(reloaded.get_snapshot().delivered_events), 0)
        self.assertFalse(reloaded.is_delivered(candidates[0].event_id))


# ===========================================================================
# Suite 2: Job Event Detection
# ===========================================================================

class TestJobEventDetection(BaseEventReaderTestCase):
    """Tests job state transitions: COMPLETED, FAILED, WAITING_CONFIRMATION, PAUSED, RETRYING."""

    def test_completed_job_candidate(self):
        """Job in COMPLETED status generates job_completed event with NORMAL or HIGH priority."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, priority, attempt_count, created_at, updated_at, completed_at, source) "
            "VALUES ('job_c1', 'Build release package', 'COMPLETED', 1, 1, '2026-10-31T10:00:00Z', '2026-10-31T10:10:00Z', '2026-10-31T10:10:00Z', 'user')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)

        c = candidates[0]
        self.assertEqual(c.event_type, "job_completed")
        self.assertEqual(c.entity_id, "job_c1")
        self.assertEqual(c.priority, EventPriority.NORMAL)
        self.assertIn("Build release package", c.title)
        self.assertEqual(c.material_data.get("status"), "completed")

    def test_failed_job_candidate(self):
        """Job in FAILED status generates job_failed event with HIGH priority and sanitized error."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, priority, attempt_count, last_error, created_at, updated_at, completed_at, source) "
            "VALUES ('job_f1', 'Deploy web service', 'FAILED', 0, 3, 'Bearer secret_token_12345 failed', '2026-10-31T10:00:00Z', '2026-10-31T10:15:00Z', '2026-10-31T10:15:00Z', 'scheduler')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)

        c = candidates[0]
        self.assertEqual(c.event_type, "job_failed")
        self.assertEqual(c.priority, EventPriority.HIGH)
        # Secret sanitization check
        self.assertNotIn("secret_token_12345", c.title)
        self.assertNotIn("secret_token_12345", str(c.evidence))
        self.assertNotIn("secret_token_12345", c.material_data.get("error", ""))

    def test_waiting_confirmation_candidate(self):
        """Job in WAITING_CONFIRMATION generates CRITICAL priority candidate."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, pending_confirmation_json, created_at, updated_at) "
            "VALUES ('job_w1', 'Delete obsolete logs', 'WAITING_CONFIRMATION', '{\"action\": \"delete\"}', '2026-10-31T10:00:00Z', '2026-10-31T10:20:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)

        c = candidates[0]
        self.assertEqual(c.event_type, "job_waiting_confirmation")
        self.assertEqual(c.priority, EventPriority.CRITICAL)
        self.assertIn("Approval required", c.title)

    def test_paused_and_retrying_candidates(self):
        """PAUSED and RETRYING jobs produce candidates."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at) "
            "VALUES ('job_p1', 'Migrate schema', 'PAUSED', '2026-10-31T10:00:00Z', '2026-10-31T10:25:00Z')"
        )
        conn.execute(
            "INSERT INTO jobs (id, goal, status, attempt_count, max_attempts, created_at, updated_at) "
            "VALUES ('job_r1', 'Fetch remote assets', 'RETRYING', 2, 3, '2026-10-31T10:00:00Z', '2026-10-31T10:26:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 2)
        types = {c.event_type for c in candidates}
        self.assertEqual(types, {"job_paused", "job_retrying"})

    def test_running_job_omitted_from_progress_events(self):
        """RUNNING status alone must NOT emit progress events."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, started_at) "
            "VALUES ('job_run', 'Ongoing index build', 'RUNNING', '2026-10-31T10:00:00Z', '2026-10-31T10:30:00Z', '2026-10-31T10:30:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 0, "RUNNING jobs should not emit events")

    def test_interactive_vs_scheduler_job_source_preserved(self):
        """Job source ('user' vs 'scheduler') is preserved in candidate metadata."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, source, created_at, updated_at, completed_at) "
            "VALUES ('job_sched', 'Daily backup', 'COMPLETED', 'scheduler', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(candidates[0].material_data.get("source"), "scheduler")

    def test_watermark_filters_older_completed_jobs(self):
        """Jobs completed at or before jobs_completed_at watermark are filtered out."""
        conn = sqlite3.connect(self.db_path)
        # Job 1: completed at 09:00 (older than watermark 09:30)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_old', 'Old backup', 'COMPLETED', '2026-10-31T08:50:00Z', '2026-10-31T09:00:00Z', '2026-10-31T09:00:00Z')"
        )
        # Job 2: completed at 10:00 (newer than watermark 09:30)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_new', 'New backup', 'COMPLETED', '2026-10-31T09:50:00Z', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOBS_COMPLETED_AT: "2026-10-31T09:30:00Z"},
            now=self.fixed_now,
        )
        snapshot = store.get_snapshot()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(snapshot=snapshot, now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].entity_id, "job_new")

    def test_repeated_job_scans_produce_stable_ids(self):
        """Repeated scans of the same job produce identical event IDs across invocations."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_repeat', 'Stable goal', 'COMPLETED', '2026-10-31T09:00:00Z', '2026-10-31T09:10:00Z', '2026-10-31T09:10:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        c1 = reader.collect_job_candidates(now=self.fixed_local_now)[0]
        c2 = reader.collect_job_candidates(now=self.fixed_local_now + timedelta(hours=3))[0]
        self.assertEqual(c1.event_id, c2.event_id)

    def test_malformed_job_timestamps_handled_safely(self):
        """Malformed or corrupt timestamps in jobs table do not raise exceptions."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_bad_ts', 'Bad timestamp job', 'COMPLETED', 'NOT_AN_ISO_DATE', 'INVALID_DATE', 'CORRUPT_TS')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].entity_id, "job_bad_ts")



# ===========================================================================
# Suite 3: Job-Step Event Detection
# ===========================================================================

class TestJobStepEventDetection(BaseEventReaderTestCase):
    """Tests job step execution records: success=1, success=0, attempt distinctions, and null completed_at."""

    def test_successful_step_candidate(self):
        """Step with success=1 generates job_step_completed candidate."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, tool_name, success, started_at, completed_at) "
            "VALUES (1, 'job_s1', 1, 1, 'tool_call', 'read_file', 1, '2026-10-31T10:00:00Z', '2026-10-31T10:00:05Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_step_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "job_step_completed")
        self.assertEqual(candidates[0].priority, EventPriority.LOW)
        self.assertEqual(candidates[0].entity_id, "1")

    def test_failed_step_candidate(self):
        """Step with success=0 generates job_step_failed candidate with NORMAL priority."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, tool_name, success, error, started_at) "
            "VALUES (2, 'job_s2', 1, 2, 'tool_call', 'execute_cmd', 0, 'Command timed out', '2026-10-31T10:01:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_step_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "job_step_failed")
        self.assertEqual(candidates[0].priority, EventPriority.NORMAL)
        self.assertIn("Command timed out", candidates[0].title)

    def test_missing_completed_at_does_not_suppress_verified_result(self):
        """completed_at being NULL must not prevent step with success=1 from being detected."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, tool_name, success, started_at, completed_at) "
            "VALUES (3, 'job_s3', 1, 1, 'tool_call', 'write_file', 1, '2026-10-31T10:02:00Z', NULL)"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_step_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "job_step_completed")

    def test_job_step_max_id_watermark_filters_lower_ids(self):
        """Steps with ID <= job_step_max_id are excluded."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (1, 'j', 1, 1, 'a', 1, '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (2, 'j', 1, 2, 'a', 1, '2026-10-31T10:01:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (3, 'j', 1, 3, 'a', 1, '2026-10-31T10:02:00Z')")
        conn.commit()
        conn.close()

        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_JOB_STEP_MAX_ID: 2},
            now=self.fixed_now,
        )
        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_step_candidates(snapshot=store.get_snapshot(), now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].entity_id, "3")

    def test_step_attempts_remain_distinguishable(self):
        """Different attempts of the same step index generate distinct event IDs."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (11, 'j_att', 1, 1, 'cmd', 0, '2026-10-31T10:00:00Z')")
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (12, 'j_att', 2, 1, 'cmd', 1, '2026-10-31T10:05:00Z')")
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_step_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0].event_id, candidates[1].event_id)

    def test_repeated_step_scans_produce_stable_ids(self):
        """Repeated scans of the same step produce identical event IDs."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO job_steps (id, job_id, attempt, step_index, action_type, success, started_at) VALUES (20, 'j_stable', 1, 1, 'cmd', 1, '2026-10-31T10:00:00Z')")
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        c1 = reader.collect_job_step_candidates(now=self.fixed_local_now)[0]
        c2 = reader.collect_job_step_candidates(now=self.fixed_local_now + timedelta(hours=2))[0]
        self.assertEqual(c1.event_id, c2.event_id)



# ===========================================================================
# Suite 4: Reminder Event Detection
# ===========================================================================

class TestReminderEventDetection(BaseEventReaderTestCase):
    """Tests reminder candidate detection: due unfired, future skipped, fired skipped, rescheduled identity."""

    def test_due_unfired_reminder_produces_candidate(self):
        """Reminder with fired=0 and due_at <= now produces candidate."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) "
            "VALUES (1, 'Submit quarterly taxes', '2026-10-31T10:00:00', '2026-10-30T10:00:00', 0)"
        )
        conn.commit()
        conn.close()

        # now is 12:00 (2 hours after due_at)
        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_reminder_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)

        c = candidates[0]
        self.assertEqual(c.event_type, "reminder_due")
        self.assertEqual(c.entity_id, "1")
        self.assertIn("Submit quarterly taxes", c.title)

    def test_future_and_fired_reminders_ignored(self):
        """Future reminders (due_at > now) and already fired reminders (fired=1) are ignored."""
        conn = sqlite3.connect(self.db_path)
        # Future reminder (due at 14:00, now is 12:00)
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) "
            "VALUES (2, 'Future doctor appointment', '2026-10-31T14:00:00', '2026-10-30T10:00:00', 0)"
        )
        # Already fired reminder
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) "
            "VALUES (3, 'Already fired team sync', '2026-10-31T09:00:00', '2026-10-30T10:00:00', 1)"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_reminder_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 0)

    def test_rescheduled_reminder_changes_identity(self):
        """Rescheduling a reminder to a new due_at timestamp produces a different event ID."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) "
            "VALUES (4, 'Project status checkin', '2026-10-31T10:00:00', '2026-10-30T10:00:00', 0)"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        c1 = reader.collect_reminder_candidates(now=self.fixed_local_now)[0]

        # Reschedule in database
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE reminders SET due_at = '2026-10-31T11:00:00' WHERE id = 4")
        conn.commit()
        conn.close()

        c2 = reader.collect_reminder_candidates(now=self.fixed_local_now)[0]
        self.assertNotEqual(c1.event_id, c2.event_id, "Rescheduled reminder must produce distinct event identity")

    def test_reminder_table_remains_strictly_unmutated(self):
        """Collecting reminder candidates must not alter the fired flag or any columns."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders (id, message, due_at, created_at, fired) "
            "VALUES (5, 'Critical commitment', '2026-10-31T11:00:00', '2026-10-30T10:00:00', 0)"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        _ = reader.collect_reminder_candidates(now=self.fixed_local_now)

        # Check database: fired must still be 0
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute("SELECT fired FROM reminders WHERE id = 5")
        fired_val = cur.fetchone()[0]
        conn.close()
        self.assertEqual(fired_val, 0, "Reminder table must remain strictly unmutated")


# ===========================================================================
# Suite 5: Structured Memory Event Detection
# ===========================================================================

class TestStructuredMemoryEventDetection(BaseEventReaderTestCase):
    """Tests project and memory creation/updates, session retirement, and sensitive filtering."""

    def test_project_creation_and_update_candidates(self):
        """Project records emit project_created or project_updated based on timestamps."""
        conn = sqlite3.connect(self.db_path)
        # Project 1: created_at == updated_at -> project_created
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('project', 'new_proj', 'Orange AI Desktop 2.0', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        # Project 2: updated_at > created_at -> project_updated
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('project', 'current', 'Core Engine Refactor', '2026-10-30T10:00:00Z', '2026-10-31T11:00:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 2)

        types = {c.event_type for c in candidates}
        self.assertEqual(types, {"project_created", "project_updated"})

    def test_unrelated_memory_produces_memory_event_not_project_event(self):
        """Non-project structured memories emit memory_created / memory_updated with LOW priority."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('preference', 'editor', 'Visual Studio Code', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "memory_created")
        self.assertEqual(candidates[0].priority, EventPriority.LOW)

    def test_retired_and_forgotten_entities_excluded(self):
        """Entities retired in the active session must be excluded from event candidates."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('project', 'archived', 'Legacy Web App', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Retire 'Legacy Web App'
        register_session_retraction("project", "Legacy Web App")

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 0, "Retired project memory must be suppressed")

    def test_sensitive_personal_content_filtered(self):
        """Memories containing personal/romantic goals must be excluded."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('goal', 'personal', 'Plan wedding anniversary with wife', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 0, "Sensitive personal memories must be filtered out")

    def test_memory_secrets_sanitized(self):
        """API keys and bearer tokens in structured memories are redacted in candidate title/evidence."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('credential', 'prod_key', 'api_key=sk-secret1234567890abcdef', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertNotIn("sk-secret1234567890abcdef", c.title)
        self.assertNotIn("sk-secret1234567890abcdef", str(c.evidence))

    def test_memory_watermark_filters_older_records(self):
        """Memories updated at or before memories_updated_at watermark are filtered out."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('project', 'old_p', 'Old Project', '2026-10-31T08:00:00Z', '2026-10-31T08:00:00Z')"
        )
        conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) "
            "VALUES ('project', 'new_p', 'New Project', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={WATERMARK_MEMORIES_UPDATED_AT: "2026-10-31T09:00:00Z"},
            now=self.fixed_now,
        )
        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(snapshot=store.get_snapshot(), now=self.fixed_local_now)
        self.assertEqual(len(candidates), 1)
        self.assertIn("New Project", candidates[0].title)

    def test_hard_deletions_do_not_produce_events(self):
        """Hard deletions from SQLite do not fabricate deletion events."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO structured_memories (id, category, memory_key, value, created_at, updated_at) "
            "VALUES (99, 'project', 'temp', 'Temp Project', '2026-10-31T10:00:00Z', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.execute("DELETE FROM structured_memories WHERE id = 99")
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_memory_candidates(now=self.fixed_local_now)
        self.assertEqual(len(candidates), 0, "No deletion events fabricated for hard deletes")


    def test_raw_conversation_history_never_scanned(self):
        """Reader never scans conversation_history table."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO conversation_history (role, content, timestamp) "
            "VALUES ('user', 'Hey please do this task', '2026-10-31T10:00:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        # Verify collect_all_candidates produces 0 events if only conversation_history has data
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        candidates = reader.collect_all_candidates(watermark_store=store, now=self.fixed_local_now)
        self.assertEqual(len(candidates), 0)


# ===========================================================================
# Suite 6: Candidate Integrity, Ordering, and Deduplication
# ===========================================================================

class TestCandidateIntegrityAndOrdering(BaseEventReaderTestCase):
    """Tests deterministic ordering, stable IDs across restarts, material change distinctions."""

    def test_stable_event_ids_across_simulated_restarts(self):
        """Repeated scans of the same database records produce identical event IDs."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_restart', 'Build index', 'COMPLETED', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')"
        )
        conn.commit()
        conn.close()

        reader1 = EventCandidateReader(db_path=self.db_path)
        candidates1 = reader1.collect_job_candidates(now=self.fixed_local_now)

        # Simulate restart with fresh reader and timestamp
        reader2 = EventCandidateReader(db_path=self.db_path)
        candidates2 = reader2.collect_job_candidates(now=self.fixed_local_now + timedelta(minutes=45))

        self.assertEqual(candidates1[0].event_id, candidates2[0].event_id, "Event ID must be identical across restarts")

    def test_deterministic_multi_tier_ordering(self):
        """Candidates are sorted deterministically: Priority -> Occurred descending -> Type -> Entity."""
        c1 = EventCandidate("evt_1", "job_completed", "j1", "2026-10-31T10:00:00", "2026-10-31T12:00:00", EventPriority.NORMAL, "T1")
        c2 = EventCandidate("evt_2", "job_waiting_confirmation", "j2", "2026-10-31T09:00:00", "2026-10-31T12:00:00", EventPriority.CRITICAL, "T2")
        c3 = EventCandidate("evt_3", "job_failed", "j3", "2026-10-31T11:00:00", "2026-10-31T12:00:00", EventPriority.HIGH, "T3")
        c4 = EventCandidate("evt_4", "job_step_completed", "s1", "2026-10-31T11:30:00", "2026-10-31T12:00:00", EventPriority.LOW, "T4")

        sorted_list = sort_candidates([c1, c4, c2, c3])
        # Expected order: CRITICAL (c2) -> HIGH (c3) -> NORMAL (c1) -> LOW (c4)
        self.assertEqual([c.event_id for c in sorted_list], ["evt_2", "evt_3", "evt_1", "evt_4"])

    def test_no_executable_payloads(self):
        """Event candidates must only contain informational data; zero executable commands."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) "
            "VALUES ('job_safe', 'Clean cache', 'COMPLETED', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')"
        )
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        candidates = reader.collect_job_candidates(now=self.fixed_local_now)
        for c in candidates:
            # Check candidate dict for action triggers or executable keys
            cd = c.to_dict()
            self.assertNotIn("command", cd)
            self.assertNotIn("action_dispatch", cd)
            self.assertNotIn("tool_call", cd)

    def test_material_changes_produce_distinct_candidate_ids(self):
        """Material changes (e.g. status change from COMPLETED to FAILED) produce different event IDs."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) VALUES ('j_mat', 'Index', 'COMPLETED', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')")
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        c_comp = reader.collect_job_candidates(now=self.fixed_local_now)[0]

        # Update to FAILED
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE jobs SET status = 'FAILED', last_error = 'OOM killed' WHERE id = 'j_mat'")
        conn.commit()
        conn.close()

        c_fail = reader.collect_job_candidates(now=self.fixed_local_now)[0]
        self.assertNotEqual(c_comp.event_id, c_fail.event_id)

    def test_incidental_timestamps_do_not_change_candidate_ids(self):
        """Changes in scan time (now) do not alter the generated candidate event_id."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO jobs (id, goal, status, created_at, updated_at, completed_at) VALUES ('j_inc', 'Task', 'COMPLETED', '2026-10-31T10:00:00Z', '2026-10-31T10:05:00Z', '2026-10-31T10:05:00Z')")
        conn.commit()
        conn.close()

        reader = EventCandidateReader(db_path=self.db_path)
        id_10am = reader.collect_job_candidates(now=datetime(2026, 10, 31, 10, 10, 0))[0].event_id
        id_11am = reader.collect_job_candidates(now=datetime(2026, 10, 31, 11, 0, 0))[0].event_id
        id_tomorrow = reader.collect_job_candidates(now=datetime(2026, 11, 1, 9, 0, 0))[0].event_id

        self.assertEqual(id_10am, id_11am)
        self.assertEqual(id_11am, id_tomorrow)

    def test_empty_scan_distinguishable_from_failure(self):
        """An empty valid scan returns an empty list, whereas a broken/missing store raises an error."""
        # Valid initialized store with empty DB -> returns []
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        reader = EventCandidateReader(db_path=self.db_path, watermark_store_path=self.store_path)
        empty_res = reader.collect_all_candidates(now=self.fixed_local_now)
        self.assertEqual(empty_res, [])

        # Missing DB path -> raises FileNotFoundError
        broken_reader = EventCandidateReader(db_path=os.path.join(self.test_dir, "missing.db"), watermark_store_path=self.store_path)
        with self.assertRaises(FileNotFoundError):
            broken_reader.collect_all_candidates(now=self.fixed_local_now)



if __name__ == "__main__":
    unittest.main()
