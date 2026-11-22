"""
test_jobs.py - Focused unit and integration tests for Phase 5A Persistent Job Data Layer.

Validates:
1. JobStatus enum values
2. Job serialization round-trip
3. JobStep serialization round-trip
4. AgentAction serialization round-trip
5. ToolResult serialization round-trip
6. AgentState checkpoint serialization round-trip
7. Job creation via JobStore
8. Job retrieval via JobStore
9. Job update via JobStore
10. Job status transitions
11. Job listing and filtering
12. Job step insertion and ordering
13. Checkpoint save/load round-trip
14. Due retry query
15. Pending job query
16. 8-character ID resolution
17. Ambiguous ID rejection (AmbiguousJobIdError)
18. Pending confirmation helper round-trip

All tests run on isolated temporary SQLite databases and never touch memory.db.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from brain.agent_models import (
    AgentState,
    AgentStatus,
    AgentAction,
    ActionType,
    ToolResult,
    AgentContext,
)
from brain.jobs.models import (
    JobStatus,
    Job,
    JobStep,
    get_short_id,
)
from brain.jobs.store import (
    JobStore,
    AmbiguousJobIdError,
)


class TestJobDataModels(unittest.TestCase):
    """Test data contracts and serialization logic for Job and JobStep."""

    def test_job_status_values(self):
        """Verify all 8 locked JobStatus values exist."""
        expected = {
            "PENDING",
            "RUNNING",
            "WAITING_CONFIRMATION",
            "PAUSED",
            "RETRYING",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }
        actual = {s.value for s in JobStatus}
        self.assertEqual(actual, expected)

    def test_job_serialization_round_trip(self):
        """Verify Job dataclass serialization to/from dict and JSON."""
        job = Job(
            id="a1b2c3d4e5f6",
            goal="Analyze disk usage and generate report",
            status=JobStatus.RUNNING,
            priority=2,
            attempt_count=1,
            max_attempts=3,
            step_count=4,
            retry_delay_seconds=120,
            next_retry_at="2026-09-20T12:00:00Z",
            last_error="API timeout",
            final_response="Disk has 45GB free",
            source="scheduler",
            tags="system,disk",
        )
        # 1. to_dict / from_dict
        d = job.to_dict()
        self.assertEqual(d["id"], "a1b2c3d4e5f6")
        self.assertEqual(d["status"], "RUNNING")
        self.assertEqual(d["short_id"], "a1b2c3d4")

        restored_from_dict = Job.from_dict(d)
        self.assertEqual(restored_from_dict.id, job.id)
        self.assertEqual(restored_from_dict.status, JobStatus.RUNNING)
        self.assertEqual(restored_from_dict.priority, 2)
        self.assertEqual(restored_from_dict.short_id, "a1b2c3d4")

        # 2. to_json / from_json
        j_str = job.to_json()
        restored_from_json = Job.from_json(j_str)
        self.assertEqual(restored_from_json.id, job.id)
        self.assertEqual(restored_from_json.status, JobStatus.RUNNING)
        self.assertEqual(restored_from_json.last_error, "API timeout")

    def test_job_step_serialization_round_trip(self):
        """Verify JobStep dataclass serialization to/from dict and JSON."""
        step = JobStep(
            id=42,
            job_id="job_123456",
            attempt=1,
            step_index=2,
            action_type="CALL_TOOL",
            tool_name="get_disk_usage",
            reasoning="Need to inspect available storage",
            success=True,
            error=None,
            duration_ms=145,
            model_name="gemini:gemini-3.5-flash",
        )
        step.set_arguments({"drive": "C:"})
        step.set_result_data({"free_gb": 45.2, "total_gb": 512.0})

        # Check argument/result unpacking
        self.assertEqual(step.get_arguments(), {"drive": "C:"})
        self.assertEqual(step.get_result_data()["free_gb"], 45.2)

        # to_dict / from_dict
        d = step.to_dict()
        self.assertEqual(d["tool_name"], "get_disk_usage")
        self.assertTrue(d["success"])

        restored = JobStep.from_dict(d)
        self.assertEqual(restored.job_id, "job_123456")
        self.assertEqual(restored.step_index, 2)
        self.assertEqual(restored.get_arguments(), {"drive": "C:"})
        self.assertEqual(restored.get_result_data()["total_gb"], 512.0)

        # to_json / from_json
        j_str = step.to_json()
        from_json_step = JobStep.from_json(j_str)
        self.assertEqual(from_json_step.duration_ms, 145)
        self.assertEqual(from_json_step.model_name, "gemini:gemini-3.5-flash")

    def test_agent_models_serialization(self):
        """Verify AgentAction, ToolResult, and AgentState serialization contracts."""
        # 1. AgentAction
        action = AgentAction(
            type=ActionType.CALL_TOOL,
            tool_name="read_file",
            arguments={"path": "C:\\notes.txt"},
            reasoning_summary="Need notes content",
        )
        a_json = action.to_json()
        a_restored = AgentAction.from_json(a_json)
        self.assertEqual(a_restored.type, ActionType.CALL_TOOL)
        self.assertEqual(a_restored.tool_name, "read_file")
        self.assertEqual(a_restored.arguments["path"], "C:\\notes.txt")

        # 2. ToolResult
        result = ToolResult(
            success=True,
            tool_name="read_file",
            data={"lines": 12},
            error=None,
            metadata={"size_bytes": 350},
        )
        r_json = result.to_json()
        r_restored = ToolResult.from_json(r_json)
        self.assertTrue(r_restored.success)
        self.assertEqual(r_restored.data["lines"], 12)

        # 3. AgentState Checkpoint
        state = AgentState(
            run_id="run_abc123",
            goal="Read notes and summarize",
            original_request="Read notes and summarize",
            status=AgentStatus.WAITING_CONFIRMATION,
            step_count=3,
            retry_count=0,
            completed_steps=["list_files: 5 files found"],
            observations=[{"step": 1, "tool": "list_files", "success": True}],
            last_action=action,
            last_result=result,
            pending_confirmation=action,
            waiting_for_user=False,
            final_response=None,
        )
        s_json = state.to_json()
        s_restored = AgentState.from_json(s_json)
        self.assertEqual(s_restored.run_id, "run_abc123")
        self.assertEqual(s_restored.status, AgentStatus.WAITING_CONFIRMATION)
        self.assertEqual(s_restored.step_count, 3)
        self.assertIsNotNone(s_restored.last_action)
        self.assertEqual(s_restored.last_action.tool_name, "read_file")
        self.assertIsNotNone(s_restored.pending_confirmation)
        self.assertEqual(s_restored.pending_confirmation.type, ActionType.CALL_TOOL)
        self.assertEqual(len(s_restored.observations), 1)

    def test_job_pending_confirmation_helpers(self):
        """Verify Job helper methods for setting and getting pending confirmation."""
        job = Job(id="j1", goal="Test confirmation")
        self.assertIsNone(job.get_pending_confirmation())

        action = AgentAction(
            type=ActionType.CALL_TOOL,
            tool_name="close_process",
            arguments={"name": "notepad.exe"},
            reasoning_summary="Close text editor",
        )
        job.set_pending_confirmation(action)
        self.assertIsNotNone(job.pending_confirmation_json)

        unpacked = job.get_pending_confirmation()
        self.assertIsNotNone(unpacked)
        self.assertEqual(unpacked.tool_name, "close_process")
        self.assertEqual(unpacked.arguments["name"], "notepad.exe")

        job.set_pending_confirmation(None)
        self.assertIsNone(job.pending_confirmation_json)
        self.assertIsNone(job.get_pending_confirmation())


class TestJobStore(unittest.TestCase):
    """Integration tests for JobStore with an isolated SQLite database."""

    def setUp(self):
        """Create a unique temporary database file for each test."""
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_file.close()
        self.store = JobStore(db_path=self.temp_file.name)

    def tearDown(self):
        """Remove the temporary database file after test completion."""
        try:
            if os.path.exists(self.temp_file.name):
                os.remove(self.temp_file.name)
        except Exception:
            pass

    def test_create_and_get_job(self):
        """Verify job creation and retrieval."""
        job = self.store.create_job(
            goal="Check system temperatures",
            priority=1,
            max_attempts=3,
            source="user",
            tags="system,monitoring",
        )
        self.assertIsNotNone(job.id)
        self.assertEqual(job.goal, "Check system temperatures")
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.priority, 1)

        fetched = self.store.get_job(job.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, job.id)
        self.assertEqual(fetched.goal, "Check system temperatures")
        self.assertEqual(fetched.status, JobStatus.PENDING)
        self.assertEqual(fetched.tags, "system,monitoring")

    def test_get_nonexistent_job(self):
        """Verify get_job returns None for missing IDs."""
        self.assertIsNone(self.store.get_job("nonexistent_id_999"))

    def test_update_job(self):
        """Verify updating an existing job record."""
        job = self.store.create_job(goal="Initial goal")
        job.goal = "Updated goal description"
        job.status = JobStatus.RUNNING
        job.step_count = 5
        job.final_response = "Finished"
        self.store.update_job(job)

        fetched = self.store.get_job(job.id)
        self.assertEqual(fetched.goal, "Updated goal description")
        self.assertEqual(fetched.status, JobStatus.RUNNING)
        self.assertEqual(fetched.step_count, 5)
        self.assertEqual(fetched.final_response, "Finished")

    def test_set_status_transition(self):
        """Verify atomic set_status transitions with updated timestamps."""
        job = self.store.create_job(goal="Status transition test")
        self.assertEqual(job.status, JobStatus.PENDING)

        # Transition to RUNNING
        self.store.set_status(job.id, JobStatus.RUNNING, started_at="2026-09-20T00:00:00Z")
        j1 = self.store.get_job(job.id)
        self.assertEqual(j1.status, JobStatus.RUNNING)
        self.assertEqual(j1.started_at, "2026-09-20T00:00:00Z")

        # Transition to WAITING_CONFIRMATION
        self.store.set_status(job.id, JobStatus.WAITING_CONFIRMATION)
        j2 = self.store.get_job(job.id)
        self.assertEqual(j2.status, JobStatus.WAITING_CONFIRMATION)

        # Transition to COMPLETED
        self.store.set_status(job.id, JobStatus.COMPLETED, completed_at="2026-09-20T00:05:00Z")
        j3 = self.store.get_job(job.id)
        self.assertEqual(j3.status, JobStatus.COMPLETED)
        self.assertEqual(j3.completed_at, "2026-09-20T00:05:00Z")

    def test_list_jobs_and_filtering(self):
        """Verify listing jobs with and without status filters, respecting priority ordering."""
        j1 = self.store.create_job(goal="Job 1 (Low)", priority=-1)
        j2 = self.store.create_job(goal="Job 2 (High)", priority=10)
        j3 = self.store.create_job(goal="Job 3 (Normal)", priority=0)

        self.store.set_status(j2.id, JobStatus.RUNNING)
        self.store.set_status(j3.id, JobStatus.COMPLETED)

        # List all
        all_jobs = self.store.list_jobs()
        self.assertEqual(len(all_jobs), 3)
        # Should be ordered by priority descending: j2 (10), j3 (0), j1 (-1)
        self.assertEqual(all_jobs[0].id, j2.id)
        self.assertEqual(all_jobs[1].id, j3.id)
        self.assertEqual(all_jobs[2].id, j1.id)

        # Filter by status
        running_jobs = self.store.list_jobs(status=JobStatus.RUNNING)
        self.assertEqual(len(running_jobs), 1)
        self.assertEqual(running_jobs[0].id, j2.id)

        completed_jobs = self.store.list_jobs(status=JobStatus.COMPLETED)
        self.assertEqual(len(completed_jobs), 1)
        self.assertEqual(completed_jobs[0].id, j3.id)

    def test_record_and_get_steps(self):
        """Verify audit step recording and deterministic ordering."""
        job = self.store.create_job(goal="Multi-step test")

        step1 = JobStep(
            job_id=job.id,
            attempt=1,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="get_cpu_usage",
            success=True,
            reasoning="Checking CPU",
            duration_ms=50,
        )
        step2 = JobStep(
            job_id=job.id,
            attempt=1,
            step_index=2,
            action_type="CALL_TOOL",
            tool_name="get_ram_usage",
            success=True,
            reasoning="Checking RAM",
            duration_ms=65,
        )
        step3 = JobStep(
            job_id=job.id,
            attempt=2,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="get_cpu_usage",
            success=True,
            reasoning="Retry attempt 2 step 1",
            duration_ms=45,
        )

        id1 = self.store.record_step(step1)
        id2 = self.store.record_step(step2)
        id3 = self.store.record_step(step3)
        self.assertTrue(id1 > 0 and id2 > id1 and id3 > id2)

        # Retrieve attempt 1 steps
        attempt1_steps = self.store.get_steps(job.id, attempt=1)
        self.assertEqual(len(attempt1_steps), 2)
        self.assertEqual(attempt1_steps[0].tool_name, "get_cpu_usage")
        self.assertEqual(attempt1_steps[1].tool_name, "get_ram_usage")

        # Retrieve all steps across all attempts
        all_steps = self.store.get_steps(job.id)
        self.assertEqual(len(all_steps), 3)
        self.assertEqual(all_steps[2].attempt, 2)

    def test_checkpoint_save_and_load_round_trip(self):
        """Verify saving and restoring an AgentState checkpoint into a Job record."""
        job = self.store.create_job(goal="Checkpoint test")

        state = AgentState(
            run_id="run_checkpoint_test",
            goal="Checkpoint test",
            original_request="Checkpoint test",
            status=AgentStatus.RUNNING,
            step_count=2,
            completed_steps=["step 1 done", "step 2 done"],
            observations=[{"step": 1, "output": "ok"}],
            final_response=None,
        )

        self.store.save_checkpoint(job.id, state)

        # Load back
        restored = self.store.load_checkpoint(job.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.run_id, "run_checkpoint_test")
        self.assertEqual(restored.step_count, 2)
        self.assertEqual(len(restored.completed_steps), 2)
        self.assertEqual(restored.completed_steps[0], "step 1 done")

    def test_get_due_retries(self):
        """Verify querying due retrying jobs based on next_retry_at timestamps."""
        now = datetime.now(timezone.utc)
        past_iso = (now - timedelta(minutes=5)).isoformat()
        future_iso = (now + timedelta(minutes=10)).isoformat()

        # Job 1: Due (past retry time)
        j1 = self.store.create_job(goal="Due retry")
        j1.status = JobStatus.RETRYING
        j1.next_retry_at = past_iso
        self.store.update_job(j1)

        # Job 2: Not due (future retry time)
        j2 = self.store.create_job(goal="Future retry")
        j2.status = JobStatus.RETRYING
        j2.next_retry_at = future_iso
        self.store.update_job(j2)

        # Job 3: RUNNING (not RETRYING)
        j3 = self.store.create_job(goal="Running job")
        j3.status = JobStatus.RUNNING
        self.store.update_job(j3)

        due = self.store.get_due_retries(now=now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].id, j1.id)

    def test_get_pending_jobs(self):
        """Verify querying PENDING jobs ordered by priority."""
        j1 = self.store.create_job(goal="Pending low", priority=1)
        j2 = self.store.create_job(goal="Pending high", priority=10)
        j3 = self.store.create_job(goal="Running job")
        self.store.set_status(j3.id, JobStatus.RUNNING)

        pending = self.store.get_pending_jobs()
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0].id, j2.id)  # priority 10 first
        self.assertEqual(pending[1].id, j1.id)

    def test_8_char_id_resolution(self):
        """Verify resolving exact matches and short 8-char prefixes."""
        job = self.store.create_job(goal="Resolve test", job_id="a1b2c3d4e5f6")
        short_id = get_short_id(job.id)
        self.assertEqual(short_id, "a1b2c3d4")

        # Exact match
        self.assertEqual(self.store.resolve_job_id("a1b2c3d4e5f6"), "a1b2c3d4e5f6")
        # 8-char prefix match
        self.assertEqual(self.store.resolve_job_id("a1b2c3d4"), "a1b2c3d4e5f6")
        # Shorter prefix match
        self.assertEqual(self.store.resolve_job_id("a1b2c"), "a1b2c3d4e5f6")
        # Non-existent prefix returns None
        self.assertIsNone(self.store.resolve_job_id("nonexistent"))
        # Empty prefix returns None
        self.assertIsNone(self.store.resolve_job_id(""))

        # resolve_job convenience helper
        resolved_job = self.store.resolve_job("a1b2c3d4")
        self.assertIsNotNone(resolved_job)
        self.assertEqual(resolved_job.goal, "Resolve test")

    def test_ambiguous_id_rejection(self):
        """Verify that ambiguous prefix matches raise AmbiguousJobIdError."""
        # Create two jobs sharing a prefix
        self.store.create_job(goal="Job Alpha", job_id="conflict_1111")
        self.store.create_job(goal="Job Beta", job_id="conflict_2222")

        # Resolving 'conflict' must raise AmbiguousJobIdError
        with self.assertRaises(AmbiguousJobIdError) as ctx:
            self.store.resolve_job_id("conflict")

        err_msg = str(ctx.exception)
        self.assertIn("matches 2 jobs", err_msg)
        self.assertIn("conflict_1111", err_msg)
        self.assertIn("conflict_2222", err_msg)

        # But exact match for conflict_1111 still succeeds
        self.assertEqual(self.store.resolve_job_id("conflict_1111"), "conflict_1111")


if __name__ == "__main__":
    unittest.main()
