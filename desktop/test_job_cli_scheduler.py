"""
test_job_cli_scheduler.py - Dedicated Phase 5C Test Suite for CLI & Scheduler Integration.

Tests at minimum:
1. job <goal> creates and starts a Job
2. job create <goal> creates PENDING Job
3. job start <id> starts PENDING Job
4. job list displays table
5. job status <id> displays structured details
6. job pause <id> pauses job
7. job resume <id> resumes paused job
8. job cancel <id> cancels job
9. job confirm <id> yes approves confirmation
10. job confirm <id> no declines confirmation -> CANCELLED
11. job history <id> displays chronological audit trail
12. valid short ID resolution works across CLI commands
13. ambiguous short ID rejection outputs helpful error
14. nonexistent Job handling outputs clean message
15. invalid Job command handling produces no tracebacks
16. scheduler starts eligible PENDING Job
17. scheduler starts due RETRYING Job
18. scheduler does not start a second Job while one is RUNNING
19. queued PENDING Job starts after execution slot becomes available
20. startup recovery occurs before scheduler processing
21. WAITING_CONFIRMATION survives simulated restart
22. existing reminder scheduler behavior remains intact
23. agent <goal> remains ephemeral (handle_job_cli returns False)
24. task <goal> remains ephemeral (handle_job_cli returns False)
"""

import os
import io
import json
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

from brain.agent import AgentEngine
from brain.agent_models import AgentState, AgentStatus, AgentAction, ActionType
from brain.jobs import (
    JobStore,
    JobManager,
    JobRuntime,
    JobStatus,
    Job,
    JobStep,
    AmbiguousJobIdError,
)
from brain import scheduler
from brain import reminders
from main import handle_job_cli, _render_job_outcome
from tools.registry import Tool, registry
from tools.permissions import RiskLevel


class MockAPIClient:
    """Mock API client returning pre-configured sequences of structured LLM responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.call_count = 0

    def is_available(self, timeout: float = 1.0) -> bool:
        return True

    def assist(
        self,
        message: str,
        history=None,
        system_instruction=None,
        profile=None,
        provider=None,
        timeout: float = 60.0,
        **kwargs,
    ):
        self.call_count += 1
        if self.responses:
            next_resp = self.responses.pop(0)
            if isinstance(next_resp, Exception):
                raise next_resp
            reply_text = json.dumps(next_resp) if isinstance(next_resp, dict) else str(next_resp)
            return {"reply": reply_text, "model": "mock:gemini-3.5-flash", "success": True}
        return {
            "reply": json.dumps({"action": {"type": "FINISH", "response": "Mock CLI task completed"}}),
            "model": "mock:gemini-3.5-flash",
            "success": True,
        }


class TestJobCLIScheduler(unittest.TestCase):
    """Test suite for Phase 5C CLI and Scheduler Integration."""

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_file.close()
        self.store = JobStore(db_path=self.temp_file.name)

        # Harmless test confirmation tool
        def safe_confirm():
            return "Confirmed harmless action."

        self.confirm_tool = Tool(
            name="test_cli_confirm",
            description="Harmless confirmation tool for CLI tests",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=safe_confirm,
        )
        registry.register(self.confirm_tool)

    def tearDown(self):
        registry._tools.pop("test_cli_confirm", None)
        try:
            if os.path.exists(self.temp_file.name):
                os.remove(self.temp_file.name)
        except Exception:
            pass

    def _make_manager(self, responses=None):
        api = MockAPIClient(responses)
        engine = AgentEngine(api_client=api)
        runtime = JobRuntime(store=self.store, engine=engine)
        return JobManager(store=self.store, runtime=runtime)

    # -------------------------------------------------------------
    # CLI COMMAND TESTS
    # -------------------------------------------------------------

    def test_job_goal_creates_and_starts(self):
        """1. 'job <goal>' creates and immediately starts a persistent Job."""
        manager = self._make_manager([
            {"action": {"type": "FINISH", "response": "System checked successfully."}}
        ])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli("job check system performance", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("Starting persistent job", output)
        self.assertIn("System checked successfully", output)

        jobs = manager.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.COMPLETED)
        self.assertEqual(jobs[0].goal, "check system performance")

    def test_job_create_goal_creates_pending(self):
        """2. 'job create <goal>' creates Job in PENDING status without executing."""
        manager = self._make_manager()
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli("job create investigate memory leak", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("in PENDING status", output)
        self.assertIn("Use 'job start", output)

        jobs = manager.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.PENDING)
        self.assertEqual(jobs[0].goal, "investigate memory leak")

    def test_job_start_id(self):
        """3. 'job start <id>' starts a PENDING Job."""
        manager = self._make_manager([
            {"action": {"type": "FINISH", "response": "Investigation completed."}}
        ])
        job = manager.create("investigate memory leak")
        self.assertEqual(job.status, JobStatus.PENDING)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job start {job.short_id}", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn(f"Starting job [{job.short_id}]", output)
        self.assertIn("Investigation completed", output)

        updated = manager.get_job(job.id)
        self.assertEqual(updated.status, JobStatus.COMPLETED)

    def test_job_list(self):
        """4. 'job list' displays readable table with ID, status, attempts, steps, goal."""
        manager = self._make_manager()
        j1 = manager.create("Goal 1")
        j2 = manager.create("Goal 2")
        self.store.set_status(j1.id, JobStatus.COMPLETED)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli("job list", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("ID", output)
        self.assertIn("STATUS", output)
        self.assertIn(j1.short_id, output)
        self.assertIn(j2.short_id, output)
        self.assertIn("COMPLETED", output)
        self.assertIn("PENDING", output)

    def test_job_status_id(self):
        """5. 'job status <id>' displays structured details."""
        manager = self._make_manager()
        job = manager.create("Inspect thermal sensors")
        job.final_response = "All temperatures normal."
        job.status = JobStatus.COMPLETED
        self.store.update_job(job)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job status {job.short_id}", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn(f"Job Status: [{job.short_id}]", output)
        self.assertIn(f"Full ID:       {job.id}", output)
        self.assertIn("Inspect thermal sensors", output)
        self.assertIn("COMPLETED", output)
        self.assertIn("All temperatures normal", output)

    def test_job_pause_id(self):
        """6. 'job pause <id>' cooperatively pauses job."""
        manager = self._make_manager()
        job = manager.create("Pausable task")
        # Pending job transitions immediately to PAUSED
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job pause {job.short_id}", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("paused", output.lower())
        updated = manager.get_job(job.id)
        self.assertEqual(updated.status, JobStatus.PAUSED)

    def test_job_resume_id(self):
        """7. 'job resume <id>' resumes a paused job."""
        manager = self._make_manager([
            {"action": {"type": "FINISH", "response": "Resumed and finished."}}
        ])
        job = manager.create("Paused task")
        job.status = JobStatus.PAUSED
        # Save minimal checkpoint for resume
        state = AgentState(
            run_id="run_paused_1",
            goal=job.goal,
            original_request=job.goal,
            status=AgentStatus.RUNNING,
            step_count=1,
            observations=[{"step": 1, "tool": "get_cpu_usage", "success": True}],
        )
        self.store.save_checkpoint(job.id, state)
        self.store.update_job(job)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job resume {job.short_id}", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("Resuming job", output)
        self.assertIn("Resumed and finished", output)

    def test_job_cancel_id(self):
        """8. 'job cancel <id>' permanently cancels job."""
        manager = self._make_manager()
        job = manager.create("Doomed task")

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job cancel {job.short_id}", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("cancelled", output.lower())
        updated = manager.get_job(job.id)
        self.assertEqual(updated.status, JobStatus.CANCELLED)

    def test_job_confirm_yes(self):
        """9. 'job confirm <id> yes' approves confirmation and resumes."""
        manager = self._make_manager([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_cli_confirm", "arguments": {}}, "summary": "Confirm step"},
            {"action": {"type": "FINISH", "response": "Confirmed task finished."}},
        ])
        job = manager.create("Confirm task")
        manager.start(job.id)

        # Job is now WAITING_CONFIRMATION
        paused = manager.get_job(job.id)
        self.assertEqual(paused.status, JobStatus.WAITING_CONFIRMATION)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job confirm {job.short_id} yes", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("Confirmed task finished", output)
        completed = manager.get_job(job.id)
        self.assertEqual(completed.status, JobStatus.COMPLETED)

    def test_job_confirm_no(self):
        """10. 'job confirm <id> no' declines confirmation -> CANCELLED."""
        manager = self._make_manager([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_cli_confirm", "arguments": {}}, "summary": "Confirm step"},
        ])
        job = manager.create("Confirm task")
        manager.start(job.id)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job confirm {job.short_id} no", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("CANCELLED", output)
        cancelled = manager.get_job(job.id)
        self.assertEqual(cancelled.status, JobStatus.CANCELLED)

    def test_job_history_id(self):
        """11. 'job history <id>' displays chronological step audit trail."""
        manager = self._make_manager()
        job = manager.create("History task")
        step1 = JobStep(job_id=job.id, attempt=1, step_index=1, action_type="CALL_TOOL", tool_name="get_cpu_usage", success=True, duration_ms=45)
        step2 = JobStep(job_id=job.id, attempt=1, step_index=2, action_type="FINISH", success=True)
        self.store.record_step(step1)
        self.store.record_step(step2)

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli(f"job history {job.short_id}", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("Audit Trail for Job", output)
        self.assertIn("CALL_TOOL (Tool: get_cpu_usage)", output)
        self.assertIn("FINISH", output)

    def test_valid_short_id_resolution(self):
        """12. Valid short ID prefix resolves properly across CLI commands."""
        manager = self._make_manager()
        job = self.store.create_job("Unique prefix task", job_id="abcdef123456")

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli("job status abcdef12", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("Unique prefix task", output)

    def test_ambiguous_short_id_rejection(self):
        """13. Ambiguous short ID rejection reports clear error without crashing."""
        manager = self._make_manager()
        self.store.create_job("Alpha", job_id="dup123_aaaa")
        self.store.create_job("Beta", job_id="dup123_bbbb")

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli("job status dup123", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("Ambiguous job ID", output)

    def test_nonexistent_job_handling(self):
        """14. Nonexistent Job handling prints clean message without raising exceptions."""
        manager = self._make_manager()
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handled = handle_job_cli("job status nonexist999", manager)
            output = out.getvalue()

        self.assertTrue(handled)
        self.assertIn("No job found", output)

    def test_invalid_job_commands_produce_no_tracebacks(self):
        """15. Invalid Job commands produce clean guidance without tracebacks."""
        manager = self._make_manager()
        test_inputs = [
            "job",
            "job create",
            "job start",
            "job pause",
            "job resume",
            "job cancel",
            "job confirm",
            "job confirm 123",
            "job confirm 123 invalidchoice",
            "job history",
        ]
        for cmd in test_inputs:
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                handled = handle_job_cli(cmd, manager)
                output = out.getvalue()
            self.assertTrue(handled, f"Command '{cmd}' was not handled.")
            self.assertNotIn("Traceback", output)
            self.assertIn("Orange AI:", output)

    # -------------------------------------------------------------
    # SCHEDULER & CONCURRENCY TESTS
    # -------------------------------------------------------------

    def test_scheduler_starts_eligible_pending_job(self):
        """16. Scheduler starts eligible PENDING job."""
        manager = self._make_manager([
            {"action": {"type": "FINISH", "response": "Scheduler executed pending job."}}
        ])
        job = manager.create("Pending auto job")
        self.assertEqual(job.status, JobStatus.PENDING)

        event_called = []
        def on_event(j):
            event_called.append(j)

        # Run one check pass of the scheduler
        res = scheduler.check_once(on_reminder_due=lambda r: None, on_job_event=on_event, job_manager=manager)

        self.assertIsNotNone(res)
        self.assertEqual(res.status, JobStatus.COMPLETED)
        self.assertEqual(res.id, job.id)
        self.assertEqual(len(event_called), 1)

    def test_scheduler_starts_due_retrying_job(self):
        """17. Scheduler starts due RETRYING job whose backoff timer elapsed."""
        manager = self._make_manager([
            {"action": {"type": "FINISH", "response": "Retry attempt succeeded."}}
        ])
        job = manager.create("Retry task")
        job.status = JobStatus.RETRYING
        # Next retry 5 minutes in the past
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        job.next_retry_at = past_iso
        self.store.update_job(job)

        res = scheduler.check_once(on_reminder_due=lambda r: None, job_manager=manager)
        self.assertIsNotNone(res)
        self.assertEqual(res.status, JobStatus.COMPLETED)
        self.assertEqual(res.attempt_count, 1)

    def test_scheduler_does_not_start_second_job_while_one_running(self):
        """18. Scheduler does not start a second Job while one is RUNNING."""
        manager = self._make_manager()
        # Job 1 is RUNNING
        j1 = manager.create("Job 1")
        self.store.set_status(j1.id, JobStatus.RUNNING)

        # Job 2 is PENDING
        j2 = manager.create("Job 2")
        self.assertEqual(j2.status, JobStatus.PENDING)

        res = scheduler.check_once(on_reminder_due=lambda r: None, job_manager=manager)
        # Should be None (no job triggered due to concurrency lock)
        self.assertIsNone(res)

        j2_current = manager.get_job(j2.id)
        self.assertEqual(j2_current.status, JobStatus.PENDING)

    def test_queued_pending_job_starts_after_slot_frees(self):
        """19. Queued PENDING Job starts after execution slot becomes available."""
        manager = self._make_manager([
            {"action": {"type": "FINISH", "response": "Slot freed job executed."}}
        ])
        j1 = manager.create("Job 1")
        self.store.set_status(j1.id, JobStatus.RUNNING)

        j2 = manager.create("Job 2")

        # Pass 1: Blocked
        res1 = scheduler.check_once(on_reminder_due=lambda r: None, job_manager=manager)
        self.assertIsNone(res1)

        # Free slot
        self.store.set_status(j1.id, JobStatus.COMPLETED)

        # Pass 2: Allowed
        res2 = scheduler.check_once(on_reminder_due=lambda r: None, job_manager=manager)
        self.assertIsNotNone(res2)
        self.assertEqual(res2.id, j2.id)
        self.assertEqual(res2.status, JobStatus.COMPLETED)

    def test_startup_recovery_before_scheduler(self):
        """20. Startup recovery identifies and safely pauses crashed RUNNING job before scheduler."""
        manager = self._make_manager()
        crashed = manager.create("Crashed job")
        crashed.attempt_count = 1
        crashed.status = JobStatus.RUNNING
        self.store.update_job(crashed)

        step = JobStep(job_id=crashed.id, attempt=1, step_index=1, action_type="CALL_TOOL", tool_name="get_cpu_usage", success=True)
        self.store.record_step(step)
        state = AgentState(run_id="c1", goal=crashed.goal, original_request=crashed.goal, status=AgentStatus.RUNNING, step_count=1, observations=[{"step": 1, "tool": "get_cpu_usage", "success": True}])
        self.store.save_checkpoint(crashed.id, state)

        # Step 4 of startup: recover_interrupted
        recovered = manager.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, JobStatus.PAUSED)

        # Now start scheduler: it will not find any RUNNING or PENDING jobs
        res = scheduler.check_once(on_reminder_due=lambda r: None, job_manager=manager)
        self.assertIsNone(res)

    def test_waiting_confirmation_survives_restart(self):
        """21. WAITING_CONFIRMATION survives simulated restart and displays in job status."""
        manager_1 = self._make_manager([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_cli_confirm", "arguments": {}}, "summary": "Confirm"},
        ])
        job = manager_1.create("Restart confirm task")
        manager_1.start(job.id)

        self.assertEqual(manager_1.get_job(job.id).status, JobStatus.WAITING_CONFIRMATION)

        # --- SIMULATE PROCESS RESTART ---
        manager_2 = self._make_manager([
            {"action": {"type": "FINISH", "response": "Post-restart finish."}}
        ])

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handle_job_cli(f"job status {job.short_id}", manager_2)
            status_output = out.getvalue()

        self.assertIn("WAITING_CONFIRMATION", status_output)
        self.assertIn("Pending Action: test_cli_confirm", status_output)

        # User confirms post-restart
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            handle_job_cli(f"job confirm {job.short_id} yes", manager_2)
            confirm_output = out.getvalue()

        self.assertIn("Post-restart finish", confirm_output)
        self.assertEqual(manager_2.get_job(job.id).status, JobStatus.COMPLETED)

    def test_existing_reminder_scheduler_behavior_intact(self):
        """22. Existing reminder scheduler behavior remains intact with full DB isolation."""
        orig_path = reminders.DB_PATH
        orig_conn = reminders._conn
        try:
            # Phase 2.2: Redirect reminders to this test's temporary database
            reminders.DB_PATH = self.temp_file.name
            reminders._conn = None

            due_dt = datetime.now() - timedelta(minutes=1)
            rem_id = reminders.add_reminder("Take medication", due_dt)

            fired_reminders = []
            def on_rem(r):
                fired_reminders.append(r)

            # Call scheduler check
            scheduler.check_once(on_reminder_due=on_rem, job_manager=None)

            self.assertTrue(len(fired_reminders) >= 1)
            self.assertTrue(any(r["id"] == rem_id for r in fired_reminders))
        finally:
            if reminders._conn:
                try:
                    reminders._conn.close()
                except Exception:
                    pass
            reminders.DB_PATH = orig_path
            reminders._conn = orig_conn

    def test_reminder_scheduler_isolation_and_restoration(self):
        """25. Verify reminder scheduler test does not mutate development memory.db and restores on failure."""
        orig_path = reminders.DB_PATH
        orig_conn = reminders._conn

        # 1. Snapshot development database reminders count if memory.db exists
        dev_count_before = None
        if os.path.exists(orig_path):
            import sqlite3
            with sqlite3.connect(f"file:///{orig_path.replace(os.sep, '/')}?mode=ro", uri=True) as ro_conn:
                dev_count_before = ro_conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]

        # 2. Run reminder operation in temporary database
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()
        try:
            try:
                reminders.DB_PATH = temp_db.name
                reminders._conn = None

                due_dt = datetime.now() - timedelta(minutes=1)
                rem_id = reminders.add_reminder("Isolated medication test", due_dt)

                fired = []
                scheduler.check_once(on_reminder_due=lambda r: fired.append(r), job_manager=None)

                self.assertEqual(len(fired), 1)
                self.assertEqual(fired[0]["id"], rem_id)

                # Simulated failure to verify finally restoration
                raise ValueError("Simulated assertion or test failure")
            except ValueError:
                pass
            finally:
                if reminders._conn:
                    try:
                        reminders._conn.close()
                    except Exception:
                        pass
                reminders.DB_PATH = orig_path
                reminders._conn = orig_conn

            # 3. Assert orig_path and orig_conn were restored
            self.assertEqual(reminders.DB_PATH, orig_path)
            self.assertEqual(reminders._conn, orig_conn)

            # 4. Assert development database count did NOT change
            if dev_count_before is not None:
                with sqlite3.connect(f"file:///{orig_path.replace(os.sep, '/')}?mode=ro", uri=True) as ro_conn:
                    dev_count_after = ro_conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
                self.assertEqual(dev_count_before, dev_count_after, "Development memory.db was mutated by test!")
        finally:
            if os.path.exists(temp_db.name):
                try:
                    os.remove(temp_db.name)
                except Exception:
                    pass

    def test_scheduler_stop_stops_loop_and_is_idempotent(self):
        """26. Scheduler loop stops promptly on stop() and repeated stop calls are safe."""
        thread = scheduler.start(
            on_reminder_due=lambda r: None,
            job_manager=None,
        )
        self.assertTrue(thread.is_alive())

        # First stop
        scheduler.stop(wait_timeout=1.0)
        self.assertFalse(thread.is_alive())

        # Repeated stop calls must be safe and not error
        scheduler.stop(wait_timeout=0.1)
        scheduler.stop(wait_timeout=0.0)

    def test_scheduler_stop_joins_completed_worker(self):
        """27. Completed worker thread is joined cleanly on scheduler.stop()."""
        completed_flag = threading.Event()

        def _quick_worker():
            time.sleep(0.05)
            completed_flag.set()

        worker = threading.Thread(target=_quick_worker, daemon=True, name="JobWorker-quick")
        with scheduler._thread_lock:
            scheduler._active_job_thread = worker
        worker.start()

        scheduler.stop(wait_timeout=1.0)
        self.assertTrue(completed_flag.is_set())
        self.assertFalse(worker.is_alive())

    def test_scheduler_stop_bounds_slow_worker(self):
        """28. Slow worker thread does not block shutdown beyond wait_timeout."""
        def _slow_worker():
            time.sleep(2.0)

        worker = threading.Thread(target=_slow_worker, daemon=True, name="JobWorker-slow")
        with scheduler._thread_lock:
            scheduler._active_job_thread = worker
        worker.start()

        t0 = time.perf_counter()
        # Bound timeout to 0.25s
        scheduler.stop(wait_timeout=0.25)
        elapsed = time.perf_counter() - t0

        # Shutdown completed within bounded time (< 0.8s), far less than the 2.0s worker sleep
        self.assertLess(elapsed, 0.8)
        self.assertTrue(worker.is_alive())

        # Clean up active worker reference so other tests are unaffected
        with scheduler._thread_lock:
            if scheduler._active_job_thread is worker:
                scheduler._active_job_thread = None

    def test_agent_and_task_remain_ephemeral(self):
        """23 & 24. 'agent <goal>' and 'task <goal>' bypass handle_job_cli."""
        manager = self._make_manager()

        # Both must return False so main.py falls through to lines 166-193 (AgentEngine)
        self.assertFalse(handle_job_cli("agent check my cpu and ram", manager))
        self.assertFalse(handle_job_cli("task run diagnostic", manager))
        self.assertFalse(handle_job_cli("plan check system", manager))
        self.assertFalse(handle_job_cli("remind me in 5 minutes", manager))
        self.assertFalse(handle_job_cli("hello orange ai", manager))


if __name__ == "__main__":
    unittest.main()
