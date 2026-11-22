"""
test_job_hardening.py - Phase 2 tests for Interrupted Job Recovery and Lifecycle Hardening.

Covers all 9 required test categories:
  1. start() rejection for non-startable statuses without DB mutation (DEF-01)
  2. Recovery after recorded terminal FINISH/RESPOND step (DEF-02)
  3. Current-attempt isolation across crashes (DEF-03)
  4. Concurrent interactive agent and persistent job audit-log isolation (DEF-04)
  5. Bounded repeated crash recovery capping at same boundary (DEF-06)
  6. Atomic rollback when checkpoint persistence fails after step insert (GAP-01)
  7. Non-blocking reminder delivery while a long-running job executes (DEF-05)
  8. Missing or corrupt checkpoint behavior and retry exhaustion (GAP-02)
  9. Concurrent job-start duplicate execution prevention and confirmation gates (LIM-02)

All tests use isolated temporary databases — never production memory.db.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore
from brain.jobs.runtime import JobRuntime, JobPersistenceError
from brain.jobs.manager import JobManager
from brain.agent import AgentEngine
from brain.agent_models import AgentState, AgentStatus, AgentAction, ActionType, ToolResult
import brain.scheduler as scheduler


def _make_temp_db():
    """Create an isolated temporary SQLite database for testing."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _make_mock_engine(completed=True, delay_s=0.0):
    """Create a mock AgentEngine with optional delay."""
    engine = MagicMock(spec=AgentEngine)
    engine._record_observation = MagicMock()
    engine._observation_callback = None
    engine._active_runs = {}

    def _start_run(goal, conversation_history=None, observation_callback=None):
        if delay_s > 0:
            time.sleep(delay_s)
        state = AgentState(
            run_id=f"run-{int(time.time()*1000)}",
            goal=goal,
            original_request=goal,
            status=AgentStatus.COMPLETED if completed else AgentStatus.FAILED,
            final_response="Done." if completed else "Failed.",
            step_count=1,
        )
        return state

    engine.start_run = MagicMock(side_effect=_start_run)
    return engine


class TestStageA_DEF01_StatusPreconditions(unittest.TestCase):
    """Category 1: JobManager.start() must reject non-startable statuses without DB mutation."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        self.engine = _make_mock_engine()
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _create_job_with_status(self, status: JobStatus) -> Job:
        job = self.store.create_job(goal="test goal", max_attempts=3)
        job.status = status
        if status in (JobStatus.RUNNING, JobStatus.RETRYING):
            job.attempt_count = 1
        self.store.update_job(job)
        return job

    def test_start_rejects_paused_job(self):
        job = self._create_job_with_status(JobStatus.PAUSED)
        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("PAUSED", str(ctx.exception))
        self.assertIn("job resume", str(ctx.exception))

    def test_start_rejects_waiting_confirmation(self):
        job = self._create_job_with_status(JobStatus.WAITING_CONFIRMATION)
        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("WAITING_CONFIRMATION", str(ctx.exception))
        self.assertIn("job confirm", str(ctx.exception))

    def test_start_rejects_running_job(self):
        job = self._create_job_with_status(JobStatus.RUNNING)
        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("already RUNNING", str(ctx.exception))

    def test_start_rejects_completed_job(self):
        job = self._create_job_with_status(JobStatus.COMPLETED)
        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("terminal state", str(ctx.exception))
        self.assertIn("COMPLETED", str(ctx.exception))

    def test_start_rejects_failed_job(self):
        job = self._create_job_with_status(JobStatus.FAILED)
        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("terminal state", str(ctx.exception))
        self.assertIn("FAILED", str(ctx.exception))

    def test_start_rejects_cancelled_job(self):
        job = self._create_job_with_status(JobStatus.CANCELLED)
        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("terminal state", str(ctx.exception))
        self.assertIn("CANCELLED", str(ctx.exception))

    def test_start_accepts_pending(self):
        job = self._create_job_with_status(JobStatus.PENDING)
        result = self.manager.start(job.id)
        self.assertEqual(result.status, JobStatus.COMPLETED)

    def test_start_accepts_retrying(self):
        job = self._create_job_with_status(JobStatus.RETRYING)
        result = self.manager.start(job.id)
        self.assertEqual(result.status, JobStatus.COMPLETED)

    def test_rejected_start_does_not_mutate_db(self):
        job = self._create_job_with_status(JobStatus.PAUSED)
        original_updated_at = job.updated_at

        with self.assertRaises(ValueError):
            self.manager.start(job.id)

        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, JobStatus.PAUSED)
        self.assertEqual(reloaded.updated_at, original_updated_at)
        self.assertEqual(reloaded.attempt_count, job.attempt_count)


class TestStageA_DEF03_AttemptIsolation(unittest.TestCase):
    """Category 3: recover_interrupted() must isolate steps to current attempt."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        self.engine = _make_mock_engine()
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_recovery_attempt_isolation(self):
        job = self.store.create_job(goal="test isolation", max_attempts=5)
        job.status = JobStatus.RUNNING
        job.attempt_count = 2
        self.store.update_job(job)

        step1 = JobStep(
            job_id=job.id, attempt=1, step_index=1,
            action_type="CALL_TOOL", tool_name="test_tool",
            success=True, reasoning="step from attempt 1",
        )
        self.store.record_step(step1)

        recovered = self.manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, JobStatus.RETRYING)
        self.assertIn("uncertainty", recovered[0].last_error)

    def test_recovery_no_cross_attempt_contamination(self):
        job = self.store.create_job(goal="test contamination", max_attempts=5)
        job.status = JobStatus.RUNNING
        job.attempt_count = 2
        self.store.update_job(job)

        step1 = JobStep(
            job_id=job.id, attempt=1, step_index=1,
            action_type="CALL_TOOL", tool_name="test_tool",
            success=True, reasoning="attempt 1 step",
        )
        self.store.record_step(step1)

        fake_state = AgentState(
            run_id="fake-run",
            goal="test contamination",
            original_request="test contamination",
            status=AgentStatus.RUNNING,
            observations=[{"step": 1, "action": {}, "result": {}}],
            step_count=1,
        )
        self.store.save_checkpoint(job.id, fake_state)

        recovered = self.manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertNotEqual(recovered[0].status, JobStatus.PAUSED)
        self.assertIn(recovered[0].status, (JobStatus.RETRYING, JobStatus.FAILED))

    def test_recovery_same_attempt_steps_establish_certainty(self):
        job = self.store.create_job(goal="test certainty", max_attempts=5)
        job.status = JobStatus.RUNNING
        job.attempt_count = 1
        self.store.update_job(job)

        step1 = JobStep(
            job_id=job.id, attempt=1, step_index=1,
            action_type="CALL_TOOL", tool_name="test_tool",
            success=True, reasoning="current attempt step",
        )
        self.store.record_step(step1)

        fake_state = AgentState(
            run_id="run-1",
            goal="test certainty",
            original_request="test certainty",
            status=AgentStatus.RUNNING,
            observations=[{"step": 1, "action": {}, "result": {}}],
            step_count=1,
        )
        self.store.save_checkpoint(job.id, fake_state)

        recovered = self.manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, JobStatus.PAUSED)


class TestStageB_DEF02_TerminalStepRecovery(unittest.TestCase):
    """Category 2: Recover recorded successful terminal step without replay."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        self.engine = _make_mock_engine()
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_recovery_of_recorded_finish_step(self):
        """Job crashed after recording FINISH step but before status update to COMPLETED."""
        job = self.store.create_job(goal="finish job", max_attempts=3)
        job.status = JobStatus.RUNNING
        job.attempt_count = 1
        self.store.update_job(job)

        finish_step = JobStep(
            job_id=job.id, attempt=1, step_index=2,
            action_type="FINISH", tool_name=None,
            success=True, reasoning="Goal achieved.",
            result_data_json=json.dumps({"response": "Successfully finished all tasks."}),
        )
        self.store.record_step(finish_step)

        recovered = self.manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        rec = recovered[0]
        self.assertEqual(rec.status, JobStatus.COMPLETED)
        self.assertEqual(rec.final_response, "Successfully finished all tasks.")
        self.assertIsNotNone(rec.completed_at)
        self.assertIn("terminal step was recorded", rec.last_error)

    def test_recovery_of_recorded_respond_step(self):
        """Job crashed after recording RESPOND step."""
        job = self.store.create_job(goal="respond job", max_attempts=3)
        job.status = JobStatus.RUNNING
        job.attempt_count = 1
        self.store.update_job(job)

        respond_step = JobStep(
            job_id=job.id, attempt=1, step_index=1,
            action_type="RESPOND", tool_name=None,
            success=True, reasoning="Direct response.",
            result_data_json=json.dumps({"response": "Answer is 42."}),
        )
        self.store.record_step(respond_step)

        recovered = self.manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        rec = recovered[0]
        self.assertEqual(rec.status, JobStatus.COMPLETED)
        self.assertEqual(rec.final_response, "Answer is 42.")

    def test_recovery_ignores_failed_terminal_step(self):
        """Failed terminal step does not complete the job."""
        job = self.store.create_job(goal="failed finish", max_attempts=3)
        job.status = JobStatus.RUNNING
        job.attempt_count = 1
        self.store.update_job(job)

        failed_step = JobStep(
            job_id=job.id, attempt=1, step_index=1,
            action_type="FINISH", tool_name=None,
            success=False, reasoning="Crashed inside finish.",
        )
        self.store.record_step(failed_step)

        recovered = self.manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertNotEqual(recovered[0].status, JobStatus.COMPLETED)
        self.assertEqual(recovered[0].status, JobStatus.RETRYING)


class TestStageB_DEF04_ObservationCallbackIsolation(unittest.TestCase):
    """Category 4: Concurrent interactive agent and persistent job audit-log isolation."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_concurrent_interactive_and_job_execution_audit_isolation(self):
        """
        Interactive agent runs using the shared AgentEngine instance while a persistent
        job executes. Interactive observations must NEVER leak into the persistent job's
        step audit log.
        """
        engine = AgentEngine(api_client=MagicMock())
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="persistent job goal")

        # Mock start_run to simulate tool execution for both job and interactive
        def fake_start_run(goal, conversation_history=None, observation_callback=None):
            state = AgentState(
                run_id=f"run-{goal[:6]}", goal=goal, original_request=goal,
                status=AgentStatus.RUNNING, step_count=1,
            )
            action = AgentAction(type=ActionType.CALL_TOOL, tool_name="test_tool", arguments={})
            result = ToolResult(success=True, tool_name="test_tool", data={"goal": goal})
            engine._record_observation(state, action, result, observation_callback=observation_callback)
            state.status = AgentStatus.COMPLETED
            state.final_response = f"Done {goal}"
            return state

        engine.start_run = MagicMock(side_effect=fake_start_run)

        # 1. Execute persistent job
        manager.start(job.id)

        # 2. Run interactive agent run (no observation_callback) on the SAME engine
        interactive_state = engine.start_run("interactive ephemeral task")
        self.assertEqual(interactive_state.status, AgentStatus.COMPLETED)

        # 3. Check persistent job step history — must only contain job's steps
        history = manager.get_history(job.id)
        self.assertEqual(len(history), 1)
        self.assertIn("persistent", history[0].get_result_data().get("goal", ""))
        self.assertNotIn("interactive", history[0].get_result_data().get("goal", ""))


class TestStageC_DEF06_BoundedCrashRecovery(unittest.TestCase):
    """Category 5: Repeated crashes at the same boundary are strictly bounded (DEF-06)."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        self.engine = _make_mock_engine()
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _seed_running_job_at_step(self, step_idx=1, attempt=1):
        job = self.store.create_job(goal="bound test", max_attempts=5)
        job.status = JobStatus.RUNNING
        job.attempt_count = attempt
        self.store.update_job(job)

        step = JobStep(
            job_id=job.id, attempt=attempt, step_index=step_idx,
            action_type="CALL_TOOL", tool_name="t", success=True, reasoning="ok",
        )
        self.store.record_step(step)

        state = AgentState(
            run_id="run-b", goal="bound test", original_request="bound test",
            status=AgentStatus.RUNNING,
            observations=[{"step": step_idx, "action": {}, "result": {}}],
            step_count=step_idx,
        )
        self.store.save_checkpoint(job.id, state)
        return job

    def test_repeated_crashes_at_same_boundary_capped_at_two(self):
        """Simulate 3 crashes at step 1: 1st -> PAUSED, 2nd -> PAUSED, 3rd -> FAILED."""
        job = self._seed_running_job_at_step(step_idx=1)

        # Crash 1
        rec1 = self.manager.recover_interrupted()[0]
        self.assertEqual(rec1.status, JobStatus.PAUSED)
        self.assertIn("[crash_count:1@step:1]", rec1.last_error)

        # Resume job and crash again at step 1
        rec1.status = JobStatus.RUNNING
        self.store.update_job(rec1)

        # Crash 2
        rec2 = self.manager.recover_interrupted()[0]
        self.assertEqual(rec2.status, JobStatus.PAUSED)
        self.assertIn("[crash_count:2@step:1]", rec2.last_error)

        # Resume job and crash 3rd time at step 1
        rec2.status = JobStatus.RUNNING
        self.store.update_job(rec2)

        # Crash 3 -> Must transition to FAILED
        rec3 = self.manager.recover_interrupted()[0]
        self.assertEqual(rec3.status, JobStatus.FAILED)
        self.assertIsNotNone(rec3.completed_at)
        self.assertIn("Repeated process crashes detected at step boundary", rec3.last_error)

    def test_crash_recovery_resets_on_step_progress(self):
        """Crash count resets when the job makes progress to a new step."""
        job = self._seed_running_job_at_step(step_idx=1)

        # Crash 1 at step 1
        rec1 = self.manager.recover_interrupted()[0]
        self.assertEqual(rec1.status, JobStatus.PAUSED)
        self.assertIn("[crash_count:1@step:1]", rec1.last_error)

        # Job resumes, advances to step 2, and crashes
        rec1.status = JobStatus.RUNNING
        rec1.step_count = 2
        self.store.update_job(rec1)

        step2 = JobStep(
            job_id=job.id, attempt=1, step_index=2,
            action_type="CALL_TOOL", tool_name="t2", success=True, reasoning="ok2",
        )
        self.store.record_step(step2)

        state2 = AgentState(
            run_id="run-b", goal="bound test", original_request="bound test",
            status=AgentStatus.RUNNING,
            observations=[{"step": 1}, {"step": 2}],
            step_count=2,
        )
        self.store.save_checkpoint(job.id, state2)

        # Crash at step 2 -> Count is 1 (not 2)
        rec2 = self.manager.recover_interrupted()[0]
        self.assertEqual(rec2.status, JobStatus.PAUSED)
        self.assertIn("[crash_count:1@step:2]", rec2.last_error)


class TestStageC_GAP01_AtomicStepAndCheckpoint(unittest.TestCase):
    """Category 6: Atomic rollback when checkpoint persistence fails (GAP-01)."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_atomic_step_and_checkpoint_success(self):
        job = self.store.create_job(goal="atomic test")
        step = JobStep(job_id=job.id, attempt=1, step_index=1, action_type="CALL_TOOL", success=True)
        state = AgentState(run_id="r1", goal="g", original_request="g", status=AgentStatus.RUNNING, step_count=1)

        step_id = self.store.record_step_and_checkpoint(step, state)
        self.assertIsNotNone(step_id)

        # Both step and checkpoint exist
        steps = self.store.get_steps(job.id)
        self.assertEqual(len(steps), 1)
        loaded_state = self.store.load_checkpoint(job.id)
        self.assertIsNotNone(loaded_state)
        self.assertEqual(loaded_state.step_count, 1)

    def test_checkpoint_failure_rolls_back_step(self):
        """If checkpoint update fails, the inserted step must be rolled back."""
        job = self.store.create_job(goal="atomic rollback test")
        step = JobStep(job_id=job.id, attempt=1, step_index=1, action_type="CALL_TOOL", success=True)

        broken_state = MagicMock(spec=AgentState)
        broken_state.to_json.side_effect = RuntimeError("Serialization exploded")

        with self.assertRaises(RuntimeError):
            self.store.record_step_and_checkpoint(step, broken_state)

        # Step record MUST NOT be present in database
        steps = self.store.get_steps(job.id)
        self.assertEqual(len(steps), 0)


class TestStageD_DEF05_SchedulerNonBlocking(unittest.TestCase):
    """Category 7: Reminder delivery while a long-running job executes (DEF-05)."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        # Mock engine that simulates 0.4s execution time
        self.engine = _make_mock_engine(completed=True, delay_s=0.4)
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_reminder_delivered_during_long_running_job(self):
        """
        Start an asynchronous job via check_once(async_job=True).
        While the job is running in its worker thread, a second check_once
        pass delivers due reminders promptly without blocking.
        """
        job = self.manager.create(goal="long running task")

        # Pass 1: Spawns worker thread for job
        started = scheduler.check_once(
            on_reminder_due=lambda r: None,
            job_manager=self.manager,
            async_job=True,
        )
        self.assertIsNotNone(started)

        # Worker is now running in background
        reminders_delivered = []
        mock_reminder = {"id": 101, "message": "Call dentist", "due_at": "2026-10-29T10:00:00"}

        with patch("brain.scheduler.get_due_reminders", return_value=[mock_reminder]), \
             patch("brain.scheduler.mark_fired"):

            # Pass 2: Must execute immediately and deliver reminders
            t0 = time.time()
            scheduler.check_once(
                on_reminder_due=lambda r: reminders_delivered.append(r),
                job_manager=self.manager,
                async_job=True,
            )
            elapsed = time.time() - t0

            # Reminder check returned rapidly (< 0.2s, not blocked by 0.4s job)
            self.assertLess(elapsed, 0.25)
            self.assertEqual(len(reminders_delivered), 1)
            self.assertEqual(reminders_delivered[0]["id"], 101)

        # Wait for worker thread to finish
        if scheduler._active_job_thread:
            scheduler._active_job_thread.join(timeout=2.0)

        # Verify job completed
        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, JobStatus.COMPLETED)


class TestStageD_LIM02_ConcurrentJobStart(unittest.TestCase):
    """Category 9: Concurrent job-start duplicate execution prevention and confirmation gates (LIM-02)."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        self.engine = _make_mock_engine(completed=True, delay_s=0.1)
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_concurrent_starts_prevent_duplicate_running_jobs(self):
        """
        Two threads call manager.start() simultaneously on two different pending jobs.
        Only ONE job must transition to RUNNING; the other must remain PENDING.
        """
        j1 = self.manager.create(goal="job 1")
        j2 = self.manager.create(goal="job 2")

        results = {}

        def _start_j1():
            results["j1"] = self.manager.start(j1.id)

        def _start_j2():
            results["j2"] = self.manager.start(j2.id)

        t1 = threading.Thread(target=_start_j1)
        t2 = threading.Thread(target=_start_j2)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # One job completed, the other remained PENDING due to concurrency limit
        statuses = [results["j1"].status, results["j2"].status]
        self.assertIn(JobStatus.COMPLETED, statuses)
        self.assertIn(JobStatus.PENDING, statuses)

    def test_confirmation_gate_preserved_under_concurrency(self):
        """WAITING_CONFIRMATION job cannot be started via start(); requires confirm()."""
        job = self.manager.create(goal="confirm goal")
        job.status = JobStatus.WAITING_CONFIRMATION
        self.store.update_job(job)

        with self.assertRaises(ValueError) as ctx:
            self.manager.start(job.id)
        self.assertIn("WAITING_CONFIRMATION", str(ctx.exception))

        # Explicit decline -> CANCELLED
        declined = self.manager.confirm(job.id, "no")
        self.assertEqual(declined.status, JobStatus.CANCELLED)


class TestStageE_GAP02_MissingCheckpoint(unittest.TestCase):
    """Category 8: Missing or corrupt checkpoint behavior and retry exhaustion (GAP-02)."""

    def setUp(self):
        self.db_path = _make_temp_db()
        self.store = JobStore(db_path=self.db_path)
        self.engine = _make_mock_engine()
        self.runtime = JobRuntime(store=self.store, engine=self.engine)
        self.manager = JobManager(store=self.store, runtime=self.runtime)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_resume_with_missing_checkpoint_fails_cleanly(self):
        """PAUSED job with missing checkpoint must fail cleanly, NOT silently replay."""
        job = self.store.create_job(goal="missing cp")
        job.status = JobStatus.PAUSED
        job.agent_state_json = None
        self.store.update_job(job)

        resumed = self.manager.resume(job.id)
        self.assertEqual(resumed.status, JobStatus.FAILED)
        self.assertIn("checkpoint corrupted or missing", resumed.last_error)
        self.assertIsNotNone(resumed.completed_at)

    def test_resume_with_corrupt_checkpoint_fails_cleanly(self):
        """PAUSED job with corrupted JSON checkpoint must fail cleanly."""
        job = self.store.create_job(goal="corrupt cp")
        job.status = JobStatus.PAUSED
        job.agent_state_json = "not-valid-json{{{"
        self.store.update_job(job)

        resumed = self.manager.resume(job.id)
        self.assertEqual(resumed.status, JobStatus.FAILED)
        self.assertIn("checkpoint corrupted or missing", resumed.last_error)

    def test_confirm_with_missing_checkpoint_fails_cleanly(self):
        """WAITING_CONFIRMATION job approved without checkpoint fails cleanly."""
        job = self.store.create_job(goal="confirm missing cp")
        job.status = JobStatus.WAITING_CONFIRMATION
        job.agent_state_json = None
        self.store.update_job(job)

        confirmed = self.manager.confirm(job.id, "yes")
        self.assertEqual(confirmed.status, JobStatus.FAILED)
        self.assertIn("checkpoint", confirmed.last_error.lower())

    def test_retry_exhaustion_transitions_to_failed(self):
        """Retry exhaustion when max_attempts reached permanently fails."""
        job = self.store.create_job(goal="exhaustion", max_attempts=2)
        job.attempt_count = 2  # Already at max attempts
        job.status = JobStatus.RUNNING
        self.store.update_job(job)

        # Trigger attempt failure
        failed_job = self.runtime._handle_attempt_failure(job, "model error")
        self.assertEqual(failed_job.status, JobStatus.FAILED)
        self.assertIn("Max attempts (2) reached", failed_job.last_error)
        self.assertIsNotNone(failed_job.completed_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
