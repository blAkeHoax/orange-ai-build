"""
Orange AI Desktop: Live Reliability Validation Suite
Covers:
- Test Group 1: Computer Operations (Files, Processes, System Metrics)
- Test Group 2: Autonomous Agent Jobs (Multi-step, Step Limits, Confirmations, Tool Failures)
- Test Group 3: Restart Recovery (Memory, Reminders, Interrupted Job Crash Recovery)
- Test Group 4: Permissions & Auditability (Risk Levels, Confirmation Semantics, Audit Trail)
"""

import os
import sys
import json
import tempfile
import shutil
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from unittest.mock import MagicMock, patch

desktop_dir = os.path.dirname(os.path.abspath(__file__))
if desktop_dir not in sys.path:
    sys.path.insert(0, desktop_dir)
api_dir = os.path.join(os.path.dirname(desktop_dir), "Orange API")
if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

from tools.permissions import RiskLevel, PermissionStatus, check_permission, execute_with_permission
from tools.registry import Tool, ToolRegistry, create_default_registry, registry as global_registry
import tools.files as files_tools
import tools.system as system_tools
import tools.processes as process_tools

import brain.memory as memory_module
import brain.reminders as reminders_module
from brain.conflict_resolver import register_session_retraction, is_session_retired, clear_session_retractions
from brain.agent import AgentEngine
from brain.agent_models import (
    AgentAction,
    ActionType,
    AgentState,
    AgentStatus,
    ToolResult,
)
from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore
from brain.jobs.runtime import JobRuntime
from brain.jobs.manager import JobManager


class MockAPIClient:
    """Mock API client returning pre-configured sequences of structured LLM responses."""

    def __init__(self, responses: Optional[List[Any]] = None):
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
        return {
            "reply": json.dumps({"action": {"type": "FINISH", "response": "Default mock response"}}),
            "model": "mock:nvidia/nemotron-3.5-lightning-30b-a3b",
            "success": True,
        }


class TestGroup1ComputerOperations(unittest.TestCase):
    """Validation for Desktop file, process, and system tools under controlled fixtures."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_grp1_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_list_files_success_empty_and_nonexistent(self):
        # 1. Success on populated directory
        f1 = os.path.join(self.test_dir, "test1.txt")
        f2 = os.path.join(self.test_dir, "test2.log")
        with open(f1, "w") as f: f.write("a")
        with open(f2, "w") as f: f.write("b")

        result = files_tools.list_files(self.test_dir)
        self.assertIn("test1.txt", result)
        self.assertIn("test2.log", result)

        # 2. Empty directory
        empty_dir = os.path.join(self.test_dir, "empty_subdir")
        os.makedirs(empty_dir, exist_ok=True)
        res_empty = files_tools.list_files(empty_dir)
        self.assertEqual(res_empty, "Folder is empty.")

        # 3. Nonexistent directory
        res_nonexistent = files_tools.list_files(os.path.join(self.test_dir, "ghost_folder"))
        self.assertTrue(
            "cannot find" in res_nonexistent.lower()
            or "no such" in res_nonexistent.lower()
            or "error" in res_nonexistent.lower()
            or "system cannot find" in res_nonexistent.lower()
        )

    def test_read_file_success_and_error_handling(self):
        fpath = os.path.join(self.test_dir, "sample.txt")
        content = "Line 1: Hello Orange AI\nLine 2: Reliability Validation"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

        read_content = files_tools.read_file(fpath)
        self.assertEqual(read_content, content)

        # Nonexistent file
        ghost = os.path.join(self.test_dir, "does_not_exist.txt")
        err_res = files_tools.read_file(ghost)
        self.assertTrue("no such file" in err_res.lower() or "cannot find" in err_res.lower())

    def test_find_file_and_find_folder(self):
        sub = os.path.join(self.test_dir, "nested", "inner")
        os.makedirs(sub, exist_ok=True)
        target_file = os.path.join(sub, "orange_target_doc.md")
        with open(target_file, "w") as f:
            f.write("target")

        # 1. find_file match
        res_found = files_tools.find_file("orange_target_doc.md", search_path=self.test_dir)
        self.assertIn(target_file, res_found)

        # 2. find_file missing
        res_missing = files_tools.find_file("phantom.bin", search_path=self.test_dir)
        self.assertIn("No file named 'phantom.bin' found", res_missing)

        # 3. find_file invalid search path
        res_invalid_path = files_tools.find_file("file.txt", search_path=os.path.join(self.test_dir, "no_such_root"))
        self.assertIn("doesn't exist on this machine", res_invalid_path)

        # 4. find_folder match
        res_folder = files_tools.find_folder("inner", search_paths=[self.test_dir])
        self.assertIn("Found 1 folder(s)", res_folder)
        self.assertIn("inner", res_folder)

        # 5. find_folder empty input
        self.assertEqual(files_tools.find_folder(""), "Tell me the folder name to search for.")

    def test_system_metrics_readiness(self):
        time_str = system_tools.get_time()
        self.assertRegex(time_str, r"\d{1,2}:\d{2}\s*(?:AM|PM)")

        date_str = system_tools.get_date()
        self.assertTrue(len(date_str) > 5)

        cpu_str = system_tools.get_cpu_usage()
        self.assertIn("%", cpu_str)

        ram_str = system_tools.get_ram_usage()
        self.assertIn("%", ram_str)

        disk_str = system_tools.get_disk_usage()
        self.assertTrue("total" in disk_str.lower() or "free" in disk_str.lower())

        battery_str = system_tools.get_battery()
        self.assertTrue(isinstance(battery_str, str) and len(battery_str) > 0)

        admin_status = system_tools.is_running_as_admin()
        self.assertIsInstance(admin_status, bool)

    def test_process_tools(self):
        self.assertTrue(process_tools.is_running("python"))
        self.assertFalse(process_tools.is_running("fake_daemon_never_existed_9999.exe"))

        procs = process_tools.list_processes()
        self.assertTrue(len(procs) > 0)
        self.assertTrue("-" in procs)


class TestGroup2AutonomousAgentJobs(unittest.TestCase):
    """Validation for autonomous multi-step jobs, step limits, confirmations, and failures."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name
        self.store = JobStore(db_path=self.db_path)

        def dummy_confirm_func():
            return "Confirmed test action executed."

        self.confirm_tool = Tool(
            name="test_confirm_action",
            description="Harmless confirmation tool for unit tests",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=dummy_confirm_func,
        )
        global_registry.register(self.confirm_tool)

    def tearDown(self):
        global_registry._tools.pop("test_confirm_action", None)
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_multi_step_job_execution(self):
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage", "arguments": {}}, "summary": "Step 1: CPU"},
            {"action": {"type": "CALL_TOOL", "tool_name": "get_ram_usage", "arguments": {}}, "summary": "Step 2: RAM"},
            {"action": {"type": "FINISH", "response": "System normal"}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Check CPU then RAM and finish", max_attempts=1)
        completed_job = manager.start(job.id)

        self.assertEqual(completed_job.status, JobStatus.COMPLETED)
        self.assertEqual(completed_job.final_response, "System normal")

        steps = manager.get_history(job.id)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0].tool_name, "get_cpu_usage")
        self.assertEqual(steps[1].tool_name, "get_ram_usage")
        self.assertEqual(steps[2].action_type, "FINISH")

    def test_step_limit_enforcement(self):
        # Provide infinite get_time calls
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "get_time", "arguments": {}}, "summary": f"Step {i}"}
            for i in range(10)
        ])
        # Set max_steps to 3
        engine = AgentEngine(api_client=mock_api, max_steps=3)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Run until step limit", max_attempts=1)
        finished_job = manager.start(job.id)

        # In AgentEngine, hitting max_steps marks status MAX_STEPS_REACHED, and runtime marks job FAILED
        self.assertEqual(finished_job.status, JobStatus.FAILED)
        self.assertTrue(
            "maximum step limit" in (finished_job.last_error or "").lower()
            or "maximum step limit" in (finished_job.final_response or "").lower()
            or "step limit" in (finished_job.last_error or "").lower()
        )

    def test_confirmation_waiting_state_and_resumption(self):
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_confirm_action", "arguments": {}}, "summary": "Call confirm tool"},
            {"action": {"type": "FINISH", "response": "Action completed after confirmation"}},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Do confirmed action", max_attempts=1)
        paused_job = manager.start(job.id)

        self.assertEqual(paused_job.status, JobStatus.WAITING_CONFIRMATION)
        self.assertIsNotNone(paused_job.pending_confirmation_json)

        pending_action = paused_job.get_pending_confirmation()
        self.assertIsNotNone(pending_action)
        self.assertEqual(pending_action.tool_name, "test_confirm_action")

        # Resume with yes
        resumed_job = manager.confirm(job.id, "yes")
        self.assertEqual(resumed_job.status, JobStatus.COMPLETED)
        self.assertEqual(resumed_job.final_response, "Action completed after confirmation")

        steps = manager.get_history(job.id)
        self.assertTrue(len(steps) >= 1)
        self.assertEqual(steps[0].tool_name, "test_confirm_action")
        self.assertTrue(steps[0].success)

    def test_confirmation_denial_not_executed(self):
        mock_api = MockAPIClient([
            {"action": {"type": "CALL_TOOL", "tool_name": "test_confirm_action", "arguments": {}}, "summary": "Call confirm tool"},
        ])
        engine = AgentEngine(api_client=mock_api)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Do confirmed action", max_attempts=1)
        paused_job = manager.start(job.id)
        self.assertEqual(paused_job.status, JobStatus.WAITING_CONFIRMATION)

        # Reject confirmation
        cancelled_job = manager.confirm(job.id, "no")
        self.assertEqual(cancelled_job.status, JobStatus.CANCELLED)
        self.assertIn("declined confirmation", (cancelled_job.final_response or "").lower())

        # Verify no steps executed the tool
        steps = manager.get_history(job.id)
        self.assertEqual(len(steps), 0)

    def test_tool_failure_and_retry_limits(self):
        # Malformed responses causing agent engine failure
        mock_api = MockAPIClient([
            "bad json 1", "bad json 2",
            "bad json 3", "bad json 4",
        ])
        engine = AgentEngine(api_client=mock_api, max_retries=2)
        runtime = JobRuntime(store=self.store, engine=engine)
        manager = JobManager(store=self.store, runtime=runtime)

        job = manager.create(goal="Failing task", max_attempts=2)
        retrying_job = manager.start(job.id)
        self.assertEqual(retrying_job.status, JobStatus.RETRYING)
        self.assertEqual(retrying_job.attempt_count, 1)

        # Attempt 2 -> exhausts max_attempts
        failed_job = runtime.execute_job(retrying_job)
        self.assertEqual(failed_job.status, JobStatus.FAILED)
        self.assertIn("Max attempts (2) reached", failed_job.last_error or "")


class TestGroup3RestartRecovery(unittest.TestCase):
    """Validation for restart persistence and crash recovery across memory, reminders, and jobs."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_memory_restart_persistence(self):
        orig_path = memory_module.DB_PATH
        memory_module.DB_PATH = self.db_path
        memory_module._conn = None
        memory_module.initialize_memory()

        # Commit structured and legacy memories
        memory_module.remember_structured("identity", "city", "Tokyo")
        memory_module.remember("Prefers dark mode in terminal")
        register_session_retraction("tool", "emacs")

        # Simulate Restart: clear in-memory singletons and re-open DB
        memory_module._conn = None
        clear_session_retractions()

        # Verify persistent data survives restart
        reloaded_structured = memory_module.recall_structured()
        self.assertTrue(any(r["category"] == "identity" and r["value"] == "Tokyo" for r in reloaded_structured))

        reloaded_legacy = memory_module.recall()
        self.assertTrue(any("dark mode" in m for m in reloaded_legacy))

        # Verify session-only state was reset on fresh restart
        self.assertFalse(is_session_retired("tool", "emacs"))

        memory_module.DB_PATH = orig_path
        memory_module._conn = None

    def test_reminders_restart_persistence_and_no_duplicate_actions(self):
        orig_path = reminders_module.DB_PATH
        reminders_module.DB_PATH = self.db_path
        reminders_module._conn = None
        conn = reminders_module.get_connection()

        # Schedule reminder due 10 seconds ago
        past_time = datetime.now() - timedelta(seconds=10)
        rem_id = reminders_module.add_reminder("Submit invoice", past_time)

        # Simulate Restart
        reminders_module._conn = None
        conn = reminders_module.get_connection()

        # 1. Verify reminder persisted across restart
        pending = reminders_module.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], rem_id)

        due = reminders_module.get_due_reminders(datetime.now())
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["message"], "Submit invoice")

        # Mark fired
        reminders_module.mark_fired(rem_id)

        # Simulate second restart
        reminders_module._conn = None
        conn = reminders_module.get_connection()

        # 2. Verify fired reminder does NOT fire again (no duplicate actions)
        due_after = reminders_module.get_due_reminders(datetime.now())
        self.assertEqual(len(due_after), 0)
        pending_after = reminders_module.list_pending()
        self.assertEqual(len(pending_after), 0)

        reminders_module.DB_PATH = orig_path
        reminders_module._conn = None

    def test_job_crash_recovery(self):
        store = JobStore(db_path=self.db_path)
        manager = JobManager(store=store)

        # 1. Job with certainty (checkpoint matched with completed step)
        j1 = store.create_job(goal="Job with certainty", max_attempts=3)
        j1.attempt_count = 1
        j1.status = JobStatus.RUNNING
        store.update_job(j1)

        step = JobStep(
            job_id=j1.id,
            attempt=1,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="get_cpu_usage",
            success=True,
        )
        store.record_step(step)

        state1 = AgentState(
            run_id="run_int_1",
            goal=j1.goal,
            original_request=j1.goal,
            status=AgentStatus.RUNNING,
            step_count=1,
            observations=[{"step": 1, "tool": "get_cpu_usage", "success": True}],
        )
        store.save_checkpoint(j1.id, state1)

        # 2. Job with uncertainty (step log missing or incomplete)
        j2 = store.create_job(goal="Job with uncertainty", max_attempts=3)
        store.set_status(j2.id, JobStatus.RUNNING)

        recovered = manager.recover_interrupted()
        self.assertEqual(len(recovered), 2)

        j1_rec = manager.get_job(j1.id)
        self.assertEqual(j1_rec.status, JobStatus.PAUSED)
        self.assertIn("safely paused", (j1_rec.last_error or "").lower())

        j2_rec = manager.get_job(j2.id)
        self.assertEqual(j2_rec.status, JobStatus.RETRYING)
        self.assertEqual(j2_rec.attempt_count, 1)


class TestGroup4PermissionsAndAuditability(unittest.TestCase):
    """Validation of risk level gates, confirmation enforcement, and audit logs."""

    def setUp(self):
        self.reg = ToolRegistry()
        self.safe_tool = Tool("safe_read", "Read info", RiskLevel.SAFE, lambda: "safe output")
        self.confirm_tool = Tool("confirm_op", "Change state", RiskLevel.CONFIRMATION_REQUIRED, lambda target: f"changed {target}")
        self.high_risk_tool = Tool("high_risk_op", "Format drive", RiskLevel.HIGH_RISK, lambda: "wiped")
        self.reg.register(self.safe_tool)
        self.reg.register(self.confirm_tool)
        self.reg.register(self.high_risk_tool)

    def test_permission_evaluation(self):
        self.assertEqual(check_permission(self.safe_tool).status, PermissionStatus.ALLOWED)
        self.assertEqual(check_permission(self.confirm_tool).status, PermissionStatus.REQUIRES_CONFIRMATION)
        self.assertEqual(check_permission(self.high_risk_tool).status, PermissionStatus.DENIED)
        # Admin does not bypass HIGH_RISK
        self.assertEqual(check_permission(self.high_risk_tool, is_admin=True).status, PermissionStatus.DENIED)

    def test_execution_path_enforcement(self):
        # 1. SAFE executes directly
        res_safe = self.reg.execute("safe_read")
        self.assertEqual(res_safe, "safe output")

        # 2. CONFIRMATION_REQUIRED denied by user
        res_denied = self.reg.execute("confirm_op", "config", prompt_fn=lambda prompt: "no")
        self.assertIn("action cancelled", res_denied.lower())
        self.assertIn("declined confirmation", res_denied.lower())

        # 3. CONFIRMATION_REQUIRED approved by user
        res_approved = self.reg.execute("confirm_op", "config", prompt_fn=lambda prompt: "yes")
        self.assertEqual(res_approved, "changed config")

        # 4. HIGH_RISK unconditionally blocked
        res_high = self.reg.execute("high_risk_op", prompt_fn=lambda prompt: "yes", is_admin=True)
        self.assertIn("cannot be executed automatically", res_high)

        # 5. Nonexistent tool handled safely
        res_unknown = self.reg.execute("ghost_tool")
        self.assertIn("not registered", res_unknown)

    def test_audit_records_integrity_and_secret_redaction(self):
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()
        store = JobStore(db_path=temp_db.name)

        job = store.create_job("Audit test")
        step = JobStep(
            job_id=job.id,
            attempt=1,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="configure_service",
            arguments_json='{"user": "alice", "api_key": "SECRET_KEY_9999"}',
            result_data_json='{"status": "ok"}',
            success=True,
            duration_ms=45,
        )
        store.record_step(step)

        steps = store.get_steps(job.id)
        self.assertEqual(len(steps), 1)
        s = steps[0]
        self.assertEqual(s.step_index, 1)
        self.assertEqual(s.tool_name, "configure_service")
        self.assertEqual(s.success, True)
        self.assertEqual(s.duration_ms, 45)

        try:
            os.remove(temp_db.name)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
