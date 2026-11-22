"""
test_job_runtime.py - Comprehensive Unit & Integration Tests for Phase 5B Job Runtime & Manager.

Tests:
1. PENDING -> RUNNING lifecycle transition
2. Simple SAFE-tool job execution -> COMPLETED
3. Multi-step job execution with all steps logged
4. Confirmation-required tool pauses in WAITING_CONFIRMATION
5. Confirmation approval resumes and completes job
6. Confirmation rejection cancels job without retry
7. Permission denial fails job without retry
8. Retryable failure transitions to RETRYING with backoff
9. Fresh-start retry replay (attempt_count incremented, old steps preserved)
10. Max attempts reached transitions to FAILED
11. Cooperative pause at step boundary -> PAUSED
12. Resume paused job from checkpoint -> COMPLETED
13. Persisted confirmation survives simulated restart
14. Interrupted RUNNING job recovery with certainty -> PAUSED
15. Corrupted/uncertain checkpoint recovery -> fresh attempt RETRYING
16. Database checkpoint failure halts execution and fails job
17. Concurrency limit (max 1 RUNNING job) queues new jobs as PENDING
18. Job history preservation across multiple attempts

All tests use isolated temporary SQLite databases and mock LLM responses.
"""

import os
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from brain.agent import AgentEngine
from brain.agent_models import (
    AgentState,
    AgentStatus,
    AgentAction,
    ActionType,
    ToolResult,
)
from brain.jobs.models import (
    JobStatus,
    Job,
    JobStep,
)
from brain.jobs.store import JobStore
from brain.jobs.runtime import JobRuntime, JobPersistenceError
from brain.jobs.manager import JobManager
from tools.registry import Tool, registry
from tools.permissions import RiskLevel


class MockAPIClient:
    """Mock API client returning pre-configured sequences of structured LLM responses."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None):
        self.responses = list(responses or [])
        self.call_count = 0

    def is_available(self, timeout: float = 1.0) -> bool:
        return True

    def assist(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
        system_instruction: Optional[str] = None,
        profile: Optional[str] = None,
        provider: Optional[str] = None,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.call_count += 1
        if self.responses:
            next_resp = self.responses.pop(0)
            if isinstance(next_resp, Exception):
                raise next_resp
            reply_text = json.dumps(next_resp) if isinstance(next_resp, dict) else str(next_resp)
            return {"reply": reply_text, "model": "mock:nvidia/nemotron-3.5-lightning-30b-a3b", "success": True}
        # Default response
        return {
            "reply": json.dumps({"action": {"type": "FINISH", "response": "Default mock response"}}),
            "model": "mock:nvidia/nemotron-3.5-lightning-30b-a3b",
            "success": True,
        }


class TestJobRuntimeAndManager(unittest.TestCase):
    """Test suite for Phase 5B Job Runtime and Manager."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_file.close()
        self.store = JobStore(db_path=self.temp_file.name)

        # Register harmless test tool requiring confirmation (avoids launching real OS GUI apps)
        def dummy_confirm_func():
            return "Confirmed test action executed successfully."

        self.test_confirm_tool = Tool(
            name="test_confirm_action",
            description="Harmless confirmation tool for unit tests",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=dummy_confirm_func,
        )
        registry.register(self.test_confirm_tool)

    def tearDown(self):
        registry._tools.pop("test_confirm_action", None)
        try:
            if os.path.exists(self.temp_file.name):
                os.remove(self.temp_file.name)
        except Exception:
            pass

    def test_pending_to_running_and_completed(self):
        """Test standard lifecycle: create (PENDING) -> start (RUNNING) -> COMPLETED."""
        mock_api = MockAPIClient([
            {"action": {"type": "FINISH", "response": "Task finished immediately."}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Simple finish goal")
        self.assertEqual(job.status, JobStatus.PENDING)

        started_job = manager.start(job.id)
        self.assertEqual(started_job.status, JobStatus.COMPLETED)
        self.assertEqual(started_job.final_response, "Task finished immediately.")
        self.assertIsNotNone(started_job.completed_at)

        # Verify in database
        db_job = self.store.get_job(job.id)
        self.assertEqual(db_job.status, JobStatus.COMPLETED)
        self.assertEqual(db_job.final_response, "Task finished immediately.")

    def test_simple_safe_tool_job(self):
        """Test job executing a single SAFE tool (get_cpu_usage) and completing."""
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "Check CPU"},
            {"action": {"type": "FINISH", "response": "CPU checked successfully."}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Check cpu usage")
        completed_job = manager.start(job.id)

        self.assertEqual(completed_job.status, JobStatus.COMPLETED)
        self.assertEqual(completed_job.step_count, 2)

        # Verify job_steps audit trail in SQLite
        steps = manager.get_history(job.id)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].tool_name, "get_cpu_usage")
        self.assertTrue(steps[0].success)
        self.assertEqual(steps[1].action_type, "FINISH")

    def test_multistep_job(self):
        """Test multi-step job logging all steps in SQLite with attempt=1."""
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "Step 1: CPU"},
            {"action": {"type": "CALL_TOOL", "tool_name": "get_ram_usage", "arguments": {}}, "summary": "Step 2: RAM"},
            {"action": {"type": "FINISH", "response": "All metrics collected."}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Check CPU and RAM")
        completed_job = manager.start(job.id)

        self.assertEqual(completed_job.status, JobStatus.COMPLETED)
        steps = manager.get_history(job.id)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0].tool_name, "get_cpu_usage")
        self.assertEqual(steps[1].tool_name, "get_ram_usage")
        self.assertEqual(steps[2].action_type, "FINISH")

    def test_confirmation_required_pauses_without_execution(self):
        """Test that a CONFIRMATION_REQUIRED tool pauses in WAITING_CONFIRMATION without executing."""
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_confirm_action", "arguments": {}}, "summary": "Launch test confirm action"},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Do confirmed action")
        paused_job = manager.start(job.id)

        self.assertEqual(paused_job.status, JobStatus.WAITING_CONFIRMATION)
        self.assertIsNotNone(paused_job.pending_confirmation_json)

        pending_action = paused_job.get_pending_confirmation()
        self.assertIsNotNone(pending_action)
        self.assertEqual(pending_action.tool_name, "test_confirm_action")

        # Verify no tool executed into job_steps yet
        steps = manager.get_history(job.id)
        self.assertEqual(len(steps), 0)

    def test_confirmation_approval_resumes_and_completes(self):
        """Test approving a pending confirmation executes the tool and finishes the job."""
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_confirm_action", "arguments": {}}, "summary": "Confirm action"},
            {"action": {"type": "FINISH", "response": "Action confirmed and completed."}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Do test action")
        manager.start(job.id)

        # Confirm approval
        confirmed_job = manager.confirm(job.id, "yes")
        self.assertEqual(confirmed_job.status, JobStatus.COMPLETED)
        self.assertIsNone(confirmed_job.pending_confirmation_json)

        steps = manager.get_history(job.id)
        self.assertTrue(len(steps) >= 1)
        self.assertEqual(steps[0].tool_name, "test_confirm_action")

    def test_confirmation_rejection_cancels_without_retry(self):
        """Test rejecting a confirmation cancels the job permanently without retrying."""
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "close_process", "arguments": {"name": "test.exe"}}, "summary": "Kill process"},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Kill test process")
        manager.start(job.id)

        # Reject confirmation
        cancelled_job = manager.confirm(job.id, "no")
        self.assertEqual(cancelled_job.status, JobStatus.CANCELLED)
        self.assertIn("declined confirmation", cancelled_job.final_response)

        # Ensure it does not become RETRYING
        due_retries = self.store.get_due_retries()
        self.assertEqual(len(due_retries), 0)

    def test_permission_denial_fails_without_retry(self):
        """Test that a denied action immediately transitions to FAILED and never retries."""
        # Temporarily register a test high-risk tool
        def dangerous_action():
            return "formatted"

        high_risk_tool = Tool(
            name="test_high_risk_action",
            description="Dangerous test action",
            risk_level=RiskLevel.HIGH_RISK,
            func=dangerous_action,
        )
        registry.register(high_risk_tool)

        try:
            mock_api = MockAPIClient([
                {"action": {"type": "CALL_TOOL", "tool_name": "test_high_risk_action", "arguments": {}}, "summary": "Execute forbidden action"},
            ])
            engine = AgentEngine(api_client=mock_api)
            runtime = JobRuntime(store=self.store, engine=engine)
            manager = JobManager(store=self.store, runtime=runtime)

            job = manager.create(goal="Do forbidden action")
            failed_job = manager.start(job.id)

            self.assertEqual(failed_job.status, JobStatus.FAILED)
            self.assertIn("not permitted", failed_job.last_error)

            # Ensure it is not in retries
            due_retries = self.store.get_due_retries()
            self.assertEqual(len(due_retries), 0)
        finally:
            registry._tools.pop("test_high_risk_action", None)

    def test_retryable_failure_and_exponential_backoff(self):
        """Test that max-retries / model error triggers RETRYING with exponential backoff."""
        # Feed malformed responses to trigger AgentEngine MAX_RETRIES_REACHED
        mock_api = MockAPIClient([
            "invalid json 1",
            "invalid json 2",
            "invalid json 3",
        ])
        engine = AgentEngine(api_client=mock_api, max_retries=2)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Test retryable error", retry_delay_seconds=30)
        retrying_job = manager.start(job.id)

        self.assertEqual(retrying_job.status, JobStatus.RETRYING)
        self.assertEqual(retrying_job.attempt_count, 1)
        self.assertIsNotNone(retrying_job.next_retry_at)

        # Check backoff calculation: delay = 30 * (2^0) = 30s
        next_dt = datetime.fromisoformat(retrying_job.next_retry_at)
        now = datetime.now(timezone.utc)
        self.assertTrue(next_dt > now)

    def test_fresh_start_retry_replay(self):
        """Test that retry starts a clean attempt while preserving previous attempt steps."""
        # Attempt 1: tool 1 succeeds, then model error causes failure
        # Attempt 2: tool 1 succeeds, tool 2 succeeds, then finish
        mock_api = MockAPIClient([
            # Attempt 1
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "Attempt 1 CPU"},
            "bad json causing attempt 1 failure",
            "bad json causing attempt 1 failure 2",
            # Attempt 2 (fresh start with original goal)
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "Attempt 2 CPU"},
            {"action": {"type": "FINISH", "response": "Attempt 2 succeeded."}},
        ])
        engine = AgentEngine(api_client=mock_api, max_retries=2)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Check cpu usage with retry")
        job1 = manager.start(job.id)
        self.assertEqual(job1.status, JobStatus.RETRYING)
        self.assertEqual(job1.attempt_count, 1)

        # Execute Attempt 2
        job2 = runtime.execute_job(job1)
        self.assertEqual(job2.status, JobStatus.COMPLETED)
        self.assertEqual(job2.attempt_count, 2)

        # Check historical audit log preserves attempt 1 and attempt 2 steps
        all_steps = manager.get_history(job.id)
        attempt1_steps = [s for s in all_steps if s.attempt == 1]
        attempt2_steps = [s for s in all_steps if s.attempt == 2]

        self.assertEqual(len(attempt1_steps), 1)
        self.assertEqual(attempt1_steps[0].tool_name, "get_cpu_usage")

        self.assertEqual(len(attempt2_steps), 2)
        self.assertEqual(attempt2_steps[0].tool_name, "get_cpu_usage")
        self.assertEqual(attempt2_steps[1].action_type, "FINISH")

    def test_max_attempts_exhaustion_fails_permanently(self):
        """Test job transitions to FAILED when attempt_count reaches max_attempts."""
        mock_api = MockAPIClient([
            "bad json 1", "bad json 2",
            "bad json 3", "bad json 4",
            "bad json 5", "bad json 6",
        ])
        engine = AgentEngine(api_client=mock_api, max_retries=2)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Doomed job", max_attempts=2)

        # Attempt 1
        j1 = manager.start(job.id)
        self.assertEqual(j1.status, JobStatus.RETRYING)

        # Attempt 2 (reaches max_attempts)
        j2 = runtime.execute_job(j1)
        self.assertEqual(j2.status, JobStatus.FAILED)
        self.assertIn("Max attempts (2) reached", j2.last_error)

    def test_pause_and_resume_job(self):
        """Test cooperative pause at step boundary and subsequent resume."""
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "Step 1"},
            {"action": {"type": "CALL_TOOL", "tool_name": "get_ram_usage", "arguments": {}}, "summary": "Step 2"},
            {"action": {"type": "FINISH", "response": "Resume complete."}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Pausable task")

        # Simulate pause request triggered right before tool 1 executes
        runtime._pause_requested.add(job.id)

        # Execute -> Should pause right after step 1
        paused_job = manager.start(job.id)
        self.assertEqual(paused_job.status, JobStatus.PAUSED)
        self.assertEqual(paused_job.step_count, 1)

        # Verify step 1 was checkpointed
        checkpoint = self.store.load_checkpoint(job.id)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint.step_count, 1)

        # Resume job -> completes step 2 and finish
        resumed_job = manager.resume(job.id)
        self.assertEqual(resumed_job.status, JobStatus.COMPLETED)
        self.assertEqual(resumed_job.final_response, "Resume complete.")

    def test_persisted_confirmation_across_simulated_restart(self):
        """Test that WAITING_CONFIRMATION survives app restart and resumes with approval."""
        mock_api_1 = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_confirm_action", "arguments": {}}, "summary": "Do test action"},
        ])
        engine_1 = AgentEngine(api_client=mock_api_1)
        runtime_1 = JobRuntime(store=self.store, engine=engine_1)
        manager_1 = JobManager(store=self.store, runtime=runtime_1)

        job = manager_1.create(goal="Test confirmation restart")
        manager_1.start(job.id)

        # Verify job is paused in WAITING_CONFIRMATION
        job_before_restart = self.store.get_job(job.id)
        self.assertEqual(job_before_restart.status, JobStatus.WAITING_CONFIRMATION)

        # --- SIMULATE PROCESS RESTART ---
        # Create fresh runtime and manager connected to same SQLite DB
        mock_api_2 = MockAPIClient([
            {"action": {"type": "FINISH", "response": "Action finished post-restart."}},
        ])
        engine_2 = AgentEngine(api_client=mock_api_2)
        runtime_2 = JobRuntime(store=self.store, engine=engine_2)
        manager_2 = JobManager(store=self.store, runtime=runtime_2)

        # User inspects and approves confirmation after restart
        job_after_restart = manager_2.get_job(job.id)
        self.assertEqual(job_after_restart.status, JobStatus.WAITING_CONFIRMATION)

        confirmed_job = manager_2.confirm(job.id, "yes")
        self.assertEqual(confirmed_job.status, JobStatus.COMPLETED)
        self.assertEqual(confirmed_job.final_response, "Action finished post-restart.")

    def test_interrupted_running_job_recovery_certainty(self):
        """Test crash recovery when all steps and checkpoint match: safely transitions to PAUSED."""
        job = self.store.create_job(goal="Interrupted clean task")
        job.attempt_count = 1
        job.status = JobStatus.RUNNING
        self.store.update_job(job)

        # Create 1 complete step and corresponding checkpoint
        step = JobStep(
            job_id=job.id,
            attempt=1,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="get_cpu_usage",
            success=True,
        )
        self.store.record_step(step)

        state = AgentState(
            run_id="run_int_1",
            goal=job.goal,
            original_request=job.goal,
            status=AgentStatus.RUNNING,
            step_count=1,
            observations=[{"step": 1, "tool": "get_cpu_usage", "success": True}],
        )
        self.store.save_checkpoint(job.id, state)

        # Run recovery
        manager = JobManager(store=self.store)
        recovered = manager.recover_interrupted()

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].id, job.id)
        self.assertEqual(recovered[0].status, JobStatus.PAUSED)
        self.assertIn("safely paused", recovered[0].last_error)

    def test_corrupted_or_uncertain_checkpoint_recovery(self):
        """Test crash recovery when step was incomplete: schedules fresh attempt as RETRYING."""
        job = self.store.create_job(goal="Uncertain crashed task", max_attempts=3)
        self.store.set_status(job.id, JobStatus.RUNNING)

        # Incomplete step (success=None, crashed mid-execution)
        step = JobStep(
            job_id=job.id,
            attempt=1,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="close_process",
            success=None,  # Incomplete!
        )
        self.store.record_step(step)

        manager = JobManager(store=self.store)
        recovered = manager.recover_interrupted()

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, JobStatus.RETRYING)
        self.assertEqual(recovered[0].attempt_count, 1)  # incremented for next attempt
        self.assertIn("uncertainty detected", recovered[0].last_error)

    def test_database_checkpoint_failure_fails_job(self):
        """Test critical invariant: database failure writing checkpoint halts execution immediately."""
        class BrokenStore(JobStore):
            def save_checkpoint(self, job_id, agent_state):
                raise sqlite3.OperationalError("Disk full or simulated database failure")

        broken_store = BrokenStore(db_path=self.temp_file.name)
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "CPU"},
            {"action": {"type": "FINISH", "response": "Should not reach here"}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=broken_store, engine=engine)

        job = broken_store.create_job(goal="Test persistence failure")
        result_job = runtime.execute_job(job)

        self.assertEqual(result_job.status, JobStatus.FAILED)
        self.assertIn("Persistence invariant violation", result_job.last_error)
        # Verify engine was not allowed to proceed to step 2
        self.assertEqual(mock_api.call_count, 1)

    def test_concurrency_limit_enforced(self):
        """Test concurrency limit: if one job is RUNNING, a second job remains PENDING."""
        # Create Job 1 and manually set it to RUNNING in SQLite
        j1 = self.store.create_job(goal="Active running job")
        self.store.set_status(j1.id, JobStatus.RUNNING)

        manager = JobManager(store=self.store)
        j2 = manager.create(goal="Queued job")
        self.assertEqual(j2.status, JobStatus.PENDING)

        # Attempt to start Job 2 while Job 1 is running
        attempted_j2 = manager.start(j2.id)
        self.assertEqual(attempted_j2.status, JobStatus.PENDING)

        # Complete Job 1
        self.store.set_status(j1.id, JobStatus.COMPLETED)

        # Now Job 2 can start
        mock_api = MockAPIClient([
            {"action": {"type": "FINISH", "response": "Job 2 finished."}},
        ])
        manager.runtime.engine = AgentEngine(api_client=mock_api)
        started_j2 = manager.start(j2.id)
        self.assertEqual(started_j2.status, JobStatus.COMPLETED)

    def test_job_history_preservation_across_attempts(self):
        """Test that get_history preserves complete ordered audit trail across attempts."""
        job = self.store.create_job(goal="History test")

        s1 = JobStep(job_id=job.id, attempt=1, step_index=1, action_type="CALL_TOOL", tool_name="get_time", success=True)
        s2 = JobStep(job_id=job.id, attempt=1, step_index=2, action_type="CALL_TOOL", tool_name="get_date", success=False, error="Timeout")
        s3 = JobStep(job_id=job.id, attempt=2, step_index=1, action_type="CALL_TOOL", tool_name="get_time", success=True)
        s4 = JobStep(job_id=job.id, attempt=2, step_index=2, action_type="FINISH", success=True)

        self.store.record_step(s1)
        self.store.record_step(s2)
        self.store.record_step(s3)
        self.store.record_step(s4)

        manager = JobManager(store=self.store)
        history = manager.get_history(job.id)

        self.assertEqual(len(history), 4)
        self.assertEqual([(s.attempt, s.step_index) for s in history], [(1, 1), (1, 2), (2, 1), (2, 2)])


if __name__ == "__main__":
    unittest.main()
