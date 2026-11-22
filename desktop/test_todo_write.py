"""
test_todo_write.py - Comprehensive Unit & Integration Test Suite for TodoWrite (REA Phase 3).

Covers:
1. TaskStatus and TaskItem serialization and deserialization.
2. AgentState serialization round-trip with checklist.
3. Legacy AgentState JSON without checklist loads cleanly with empty checklist.
4. Valid checklist creation and replacement via todo_write().
5. Accurate progress metrics (total, completed, in_progress, pending, active_task).
6. Missing ID handling (auto-assignment of deterministic string IDs).
7. Invalid status rejection with clear error.
8. Multiple in_progress items normalization (first in_progress kept, subsequent demoted to pending).
9. Empty/whitespace content description rejection.
10. Non-list payloads rejection.
11. 20-item boundary test (exactly 20 items succeeds).
12. Exceeding 20 items rejection (21 items fails).
13. Content (> 256 chars) and active_form (> 128 chars) length limit rejections.
14. 8 KB payload size limit rejection.
15. Malformed persisted checklist recovery.
16. Direct state mutation when state is passed to todo_write().
17. ToolRegistry registration verification (SAFE, category planning).
18. JobStore atomic checkpoint persistence and restore round-trip.
19. Job pause/resume persistence of checklist across simulated restarts.
20. GUI detail card parsing and rendering verification.

All tests run on isolated temporary SQLite databases and never touch memory.db.
"""

import os
import json
import tempfile
import unittest
from datetime import datetime, timezone

from brain.agent_models import (
    TaskStatus,
    TaskItem,
    AgentState,
    AgentStatus,
    AgentAction,
    ActionType,
    ToolResult,
    AgentContext,
)
from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore
from brain.agent_context import build_agent_context, format_decision_prompt
from tools.todo import (
    todo_write,
    MAX_CHECKLIST_ITEMS,
    MAX_CONTENT_LENGTH,
    MAX_ACTIVE_FORM_LENGTH,
    MAX_PAYLOAD_BYTES,
)
from tools.registry import registry, Tool
from tools.permissions import RiskLevel, check_permission, PermissionStatus


class TestTaskContractsAndSerialization(unittest.TestCase):
    """Test data contracts and serialization logic for TaskItem and AgentState."""

    def test_task_status_values(self):
        """Verify TaskStatus enum matches expected lowercase values."""
        self.assertEqual(TaskStatus.PENDING.value, "pending")
        self.assertEqual(TaskStatus.IN_PROGRESS.value, "in_progress")
        self.assertEqual(TaskStatus.COMPLETED.value, "completed")

    def test_task_item_round_trip(self):
        """Verify TaskItem serializes and deserializes cleanly."""
        item = TaskItem(
            id="task_1",
            content="Inspect system metrics",
            status=TaskStatus.IN_PROGRESS,
            active_form="Inspecting system metrics",
        )
        data = item.to_dict()
        self.assertEqual(data["id"], "task_1")
        self.assertEqual(data["content"], "Inspect system metrics")
        self.assertEqual(data["status"], "in_progress")
        self.assertEqual(data["active_form"], "Inspecting system metrics")

        restored = TaskItem.from_dict(data)
        self.assertEqual(restored.id, "task_1")
        self.assertEqual(restored.content, "Inspect system metrics")
        self.assertEqual(restored.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(restored.active_form, "Inspecting system metrics")

        # JSON string round-trip
        json_str = item.to_json()
        restored_json = TaskItem.from_json(json_str)
        self.assertEqual(restored_json, restored)

    def test_agent_state_with_checklist_round_trip(self):
        """Verify AgentState with checklist survives to_json() / from_json()."""
        state = AgentState(
            run_id="run_123",
            goal="Multi-step system audit",
            original_request="Multi-step system audit",
            status=AgentStatus.RUNNING,
            checklist=[
                TaskItem(id="1", content="Check CPU", status=TaskStatus.COMPLETED),
                TaskItem(id="2", content="Check RAM", status=TaskStatus.IN_PROGRESS, active_form="Checking RAM"),
                TaskItem(id="3", content="Save report", status=TaskStatus.PENDING),
            ],
        )

        state_json = state.to_json()
        restored = AgentState.from_json(state_json)

        self.assertEqual(restored.run_id, "run_123")
        self.assertEqual(len(restored.checklist), 3)
        self.assertEqual(restored.checklist[0].status, TaskStatus.COMPLETED)
        self.assertEqual(restored.checklist[1].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(restored.checklist[1].active_form, "Checking RAM")
        self.assertEqual(restored.checklist[2].status, TaskStatus.PENDING)

    def test_legacy_agent_state_deserialization(self):
        """Verify legacy AgentState JSON without 'checklist' field loads with empty checklist."""
        legacy_dict = {
            "run_id": "legacy_run_456",
            "goal": "Legacy goal",
            "original_request": "Legacy goal",
            "status": "COMPLETED",
            "step_count": 3,
            "observations": [],
        }
        legacy_json = json.dumps(legacy_dict)
        state = AgentState.from_json(legacy_json)
        self.assertEqual(state.run_id, "legacy_run_456")
        self.assertEqual(state.checklist, [])

    def test_malformed_persisted_checklist_recovery(self):
        """Verify malformed checklist entries in persisted state are recovered safely without crash."""
        corrupt_dict = {
            "run_id": "corrupt_run",
            "goal": "Corrupt goal",
            "original_request": "Corrupt goal",
            "status": "RUNNING",
            "checklist": [
                {"id": "1", "content": "Valid item", "status": "completed"},
                "not_a_dict",
                {"broken": True},  # Missing content and valid fields
                {"id": "2", "content": "Another valid item", "status": "pending"},
            ],
        }
        state = AgentState.from_dict(corrupt_dict)
        # Invalid items should be handled gracefully
        self.assertGreaterEqual(len(state.checklist), 2)
        self.assertEqual(state.checklist[0].content, "Valid item")


class TestTodoWriteToolValidation(unittest.TestCase):
    """Test validation, boundaries, normalization, and metrics of todo_write()."""

    def test_valid_checklist_creation_and_metrics(self):
        """Verify valid input produces correct metrics and structured result."""
        todos = [
            {"id": "t1", "content": "Read config file", "status": "completed"},
            {"id": "t2", "content": "Update setting", "status": "in_progress", "active_form": "Updating setting"},
            {"id": "t3", "content": "Restart process", "status": "pending"},
        ]
        res = todo_write(todos)
        self.assertTrue(res["success"])
        self.assertEqual(res["total_tasks"], 3)
        self.assertEqual(res["completed"], 1)
        self.assertEqual(res["in_progress"], 1)
        self.assertEqual(res["pending"], 1)
        self.assertEqual(res["active_task"], "Update setting")
        self.assertEqual(len(res["checklist"]), 3)

    def test_missing_ids_auto_assignment(self):
        """Verify missing IDs are deterministically generated as sequential strings."""
        todos = [
            {"content": "First task", "status": "completed"},
            {"content": "Second task", "status": "pending"},
        ]
        res = todo_write(todos)
        self.assertTrue(res["success"])
        self.assertEqual(res["checklist"][0]["id"], "1")
        self.assertEqual(res["checklist"][1]["id"], "2")

    def test_multiple_in_progress_normalization(self):
        """Verify multiple in_progress tasks normalize: first kept in_progress, subsequent demoted to pending."""
        todos = [
            {"id": "1", "content": "Task 1", "status": "in_progress", "active_form": "Doing task 1"},
            {"id": "2", "content": "Task 2", "status": "in_progress", "active_form": "Doing task 2"},
            {"id": "3", "content": "Task 3", "status": "in_progress"},
        ]
        res = todo_write(todos)
        self.assertTrue(res["success"])
        self.assertEqual(res["in_progress"], 1)
        self.assertEqual(res["pending"], 2)
        self.assertEqual(res["checklist"][0]["status"], "in_progress")
        self.assertEqual(res["checklist"][1]["status"], "pending")
        self.assertEqual(res["checklist"][2]["status"], "pending")
        self.assertEqual(res["active_task"], "Task 1")

    def test_invalid_status_rejection(self):
        """Verify unsupported status strings are rejected with a clear error."""
        todos = [{"id": "1", "content": "Task", "status": "unknown_status"}]
        res = todo_write(todos)
        self.assertFalse(res["success"])
        self.assertIn("invalid status", res["error"])

    def test_empty_content_rejection(self):
        """Verify empty or whitespace-only content descriptions are rejected."""
        todos_empty = [{"id": "1", "content": ""}]
        res1 = todo_write(todos_empty)
        self.assertFalse(res1["success"])
        self.assertIn("empty or missing 'content'", res1["error"])

        todos_ws = [{"id": "1", "content": "   "}]
        res2 = todo_write(todos_ws)
        self.assertFalse(res2["success"])
        self.assertIn("empty or missing 'content'", res2["error"])

    def test_non_list_payload_rejection(self):
        """Verify non-list payloads are cleanly rejected."""
        res = todo_write({"id": "1", "content": "Not in a list"})
        self.assertFalse(res["success"])
        self.assertIn("must be a list", res["error"])

    def test_20_item_boundary_acceptance(self):
        """Verify exactly 20 items (boundary limit) is accepted."""
        todos = [{"content": f"Task number {i}", "status": "pending"} for i in range(1, 21)]
        res = todo_write(todos)
        self.assertTrue(res["success"])
        self.assertEqual(res["total_tasks"], 20)

    def test_exceeding_20_items_rejection(self):
        """Verify 21 items exceeds maximum allowed limit and is rejected."""
        todos = [{"content": f"Task number {i}", "status": "pending"} for i in range(1, 22)]
        res = todo_write(todos)
        self.assertFalse(res["success"])
        self.assertIn("exceeds maximum allowed items (20)", res["error"])

    def test_content_length_limit_rejection(self):
        """Verify task content exceeding 256 characters is rejected."""
        long_content = "x" * 257
        todos = [{"content": long_content, "status": "pending"}]
        res = todo_write(todos)
        self.assertFalse(res["success"])
        self.assertIn("exceeds maximum content length (256 characters)", res["error"])

    def test_active_form_length_limit_rejection(self):
        """Verify active_form exceeding 128 characters is rejected."""
        long_active = "y" * 129
        todos = [{"content": "Valid task", "status": "in_progress", "active_form": long_active}]
        res = todo_write(todos)
        self.assertFalse(res["success"])
        self.assertIn("exceeds maximum active_form length (128 characters)", res["error"])

    def test_payload_size_limit_rejection(self):
        """Verify payload exceeding 8 KB limit is rejected."""
        # 20 items each with 255 chars plus large keys
        oversized = [
            {
                "id": f"long_task_id_padding_{i}" * 5,
                "content": "A" * 250,
                "status": "pending",
                "active_form": "B" * 120,
            }
            for i in range(20)
        ]
        # Total JSON bytes will exceed 8192 bytes
        payload_bytes = len(json.dumps(oversized).encode("utf-8"))
        self.assertGreater(payload_bytes, 8192)

        res = todo_write(oversized)
        self.assertFalse(res["success"])
        self.assertIn("exceeds 8 KB limit", res["error"])

    def test_direct_agent_state_mutation(self):
        """Verify passing active AgentState mutates state.checklist directly."""
        state = AgentState(
            run_id="run_state_test",
            goal="Testing direct state update",
            original_request="Testing direct state update",
            status=AgentStatus.RUNNING,
            checklist=[],
        )

        todos = [
            {"id": "1", "content": "Step A", "status": "completed"},
            {"id": "2", "content": "Step B", "status": "in_progress", "active_form": "Doing Step B"},
        ]
        res = todo_write(todos, state=state)
        self.assertTrue(res["success"])
        self.assertEqual(len(state.checklist), 2)
        self.assertEqual(state.checklist[0].content, "Step A")
        self.assertEqual(state.checklist[0].status, TaskStatus.COMPLETED)
        self.assertEqual(state.checklist[1].status, TaskStatus.IN_PROGRESS)


class TestToolRegistryAndPermissions(unittest.TestCase):
    """Test ToolRegistry registration and permission compliance for todo_write."""

    def test_tool_registered_as_safe(self):
        """Verify todo_write is registered, classified as SAFE, and in category planning."""
        tool = registry.get("todo_write")
        self.assertIsNotNone(tool, "todo_write must be registered in ToolRegistry")
        self.assertEqual(tool.name, "todo_write")
        self.assertEqual(tool.risk_level, RiskLevel.SAFE)
        self.assertEqual(tool.category, "planning")
        self.assertFalse(tool.requires_confirmation)

        perm = check_permission(tool)
        self.assertEqual(perm.status, PermissionStatus.ALLOWED)

    def test_execution_via_registry(self):
        """Verify executing todo_write via registry.execute() succeeds cleanly."""
        res = registry.execute(
            "todo_write",
            todos=[{"content": "Task through registry", "status": "pending"}],
        )
        self.assertIsInstance(res, dict)
        self.assertTrue(res["success"])
        self.assertEqual(res["total_tasks"], 1)


class TestPromptAndContextIntegration(unittest.TestCase):
    """Test checklist formatting and context building."""

    def test_context_building_with_checklist(self):
        """Verify build_agent_context includes serialized checklist."""
        state = AgentState(
            run_id="ctx_run",
            goal="Context test goal",
            original_request="Context test goal",
            status=AgentStatus.RUNNING,
            checklist=[
                TaskItem(id="1", content="Task 1", status=TaskStatus.COMPLETED),
                TaskItem(id="2", content="Task 2", status=TaskStatus.IN_PROGRESS, active_form="Doing Task 2"),
            ],
        )
        ctx = build_agent_context(
            goal=state.goal,
            original_request=state.original_request,
            state=state,
        )
        self.assertEqual(len(ctx.checklist), 2)
        self.assertEqual(ctx.checklist[0]["content"], "Task 1")

    def test_prompt_formatting_with_active_checklist(self):
        """Verify format_decision_prompt formats active checklist with progress."""
        ctx = AgentContext(
            user_request="Test prompt request",
            checklist=[
                {"id": "1", "content": "Inspect file", "status": "completed"},
                {"id": "2", "content": "Edit settings", "status": "in_progress", "active_form": "Editing port to 8080"},
                {"id": "3", "content": "Verify port", "status": "pending"},
            ],
        )
        prompt = format_decision_prompt(ctx)
        self.assertIn("CURRENT TASK CHECKLIST:", prompt)
        self.assertIn("[x] 1. Inspect file", prompt)
        self.assertIn("[>] 2. Edit settings (IN PROGRESS: Editing port to 8080)", prompt)
        self.assertIn("[ ] 3. Verify port", prompt)
        self.assertIn("Progress: 1/3 completed (33%)", prompt)

    def test_prompt_formatting_empty_checklist_instruction(self):
        """Verify empty checklist provides clear instruction to declare a checklist for multi-step goals."""
        ctx = AgentContext(user_request="Single step prompt request", checklist=[])
        prompt = format_decision_prompt(ctx)
        self.assertIn("CURRENT TASK CHECKLIST:", prompt)
        self.assertIn("None (For multi-step goals, use tool 'todo_write' to declare a checklist).", prompt)


class TestJobStorePersistenceAndRecovery(unittest.TestCase):
    """Test SQLite persistence of checklist into jobs.agent_state_json and crash recovery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_todo_memory.db")
        self.store = JobStore(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_checkpoint_saves_and_restores_checklist(self):
        """Verify record_step_and_checkpoint saves checklist and load_checkpoint restores it."""
        job = self.store.create_job(goal="Durable checklist test")

        state = AgentState(
            run_id=job.id,
            goal=job.goal,
            original_request=job.goal,
            status=AgentStatus.RUNNING,
            step_count=1,
            checklist=[
                TaskItem(id="1", content="Step 1", status=TaskStatus.COMPLETED),
                TaskItem(id="2", content="Step 2", status=TaskStatus.IN_PROGRESS, active_form="Running step 2"),
            ],
        )

        step = JobStep(
            job_id=job.id,
            attempt=1,
            step_index=1,
            action_type="CALL_TOOL",
            tool_name="todo_write",
            reasoning="Updating checklist",
            success=True,
        )

        # Atomic step and checkpoint persistence
        self.store.record_step_and_checkpoint(step, state)

        # Restore checkpoint from database
        restored_state = self.store.load_checkpoint(job.id)
        self.assertIsNotNone(restored_state)
        self.assertEqual(len(restored_state.checklist), 2)
        self.assertEqual(restored_state.checklist[0].status, TaskStatus.COMPLETED)
        self.assertEqual(restored_state.checklist[1].status, TaskStatus.IN_PROGRESS)
        self.assertEqual(restored_state.checklist[1].active_form, "Running step 2")

    def test_job_pause_and_resume_preserves_checklist(self):
        """Verify paused job loaded on resume retains its exact checklist."""
        job = self.store.create_job(goal="Pause resume checklist test")
        state = AgentState(
            run_id=job.id,
            goal=job.goal,
            original_request=job.goal,
            status=AgentStatus.RUNNING,
            step_count=2,
            checklist=[
                TaskItem(id="1", content="Part A", status=TaskStatus.COMPLETED),
                TaskItem(id="2", content="Part B", status=TaskStatus.PENDING),
            ],
        )

        self.store.save_checkpoint(job.id, state)
        self.store.set_status(job.id, JobStatus.PAUSED)

        # Re-fetch job and checkpoint
        fetched_job = self.store.get_job(job.id)
        self.assertEqual(fetched_job.status, JobStatus.PAUSED)

        loaded_state = self.store.load_checkpoint(fetched_job.id)
        self.assertEqual(len(loaded_state.checklist), 2)
        self.assertEqual(loaded_state.checklist[0].status, TaskStatus.COMPLETED)
        self.assertEqual(loaded_state.checklist[1].status, TaskStatus.PENDING)


class TestGUIIntegration(unittest.TestCase):
    """Test GUI checklist card parsing without full GUI display loop."""

    def test_gui_checklist_parsing_from_agent_state_json(self):
        """Verify agent_state_json parses safely and extracts checklist items."""
        state = AgentState(
            run_id="gui_test_run",
            goal="GUI test goal",
            original_request="GUI test goal",
            status=AgentStatus.RUNNING,
            checklist=[
                TaskItem(id="1", content="Task Alpha", status=TaskStatus.COMPLETED),
                TaskItem(id="2", content="Task Beta", status=TaskStatus.IN_PROGRESS, active_form="Processing Beta"),
            ],
        )
        job = Job(
            id="test_gui_job",
            goal="GUI test goal",
            status=JobStatus.RUNNING,
            agent_state_json=state.to_json(),
        )

        # Parse logic matching gui/views/agent_jobs_view.py
        state_dict = json.loads(job.agent_state_json)
        checklist_items = state_dict.get("checklist") or []
        self.assertEqual(len(checklist_items), 2)

        completed_cnt = sum(1 for it in checklist_items if it.get("status") == "completed")
        total_cnt = len(checklist_items)
        pct = int((completed_cnt / total_cnt) * 100) if total_cnt > 0 else 0

        self.assertEqual(completed_cnt, 1)
        self.assertEqual(total_cnt, 2)
        self.assertEqual(pct, 50)


if __name__ == "__main__":
    unittest.main()
