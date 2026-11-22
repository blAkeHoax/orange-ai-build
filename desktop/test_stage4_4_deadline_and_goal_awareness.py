"""
test_stage4_4_deadline_and_goal_awareness.py - Stage 4.4 End-to-End Validation Suite
Deadline & Goal Awareness Hardening, Context Budgeting, and Tauri Runtime Integration.

Covers:
1. Active project deadlines are consistently retrieved and surfaced in ordinary chat context.
2. Unfinished checklist tasks are consistently retrieved and surfaced in ordinary chat context.
3. Agent Engine context receives the appropriate deadline and checklist information.
4. Memory context budgeting preserves deadline and task information within approved ceilings (1,500 / 1,200 chars).
5. Completed, dismissed, or retired items are reliably excluded.
6. Memory conflict resolution and project scoping remain intact.
7. Missing or ambiguous information is surfaced honestly without inventing dates or task statuses.
8. Full chat turn pipeline delivers project deadline to the assistant API.
9. Tauri runtime adapter delegates cleanly to core engine under isolated environment.

All tests operate in isolated temporary directories and mock environments.
Zero modification to production databases or frozen subsystems.
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import brain.memory as memory_module
from brain.context_budget import (
    budget_chat_memory,
    budget_agent_memory,
    DEFAULT_CHAT_BUDGET_CHARS,
    DEFAULT_AGENT_BUDGET_CHARS,
)
from brain.agent_models import AgentState, AgentStatus, TaskItem, TaskStatus
from brain.agent_context import (
    build_agent_context,
    format_decision_prompt,
    AGENT_SYSTEM_CONSTRAINTS,
)
from brain.conflict_resolver import resolve_memory_conflicts
from brain.briefing.recommendations import (
    Recommendation,
    RecommendationPriority,
    RecommendationState,
    RecommendationStore,
)
import main
from test_memory_phase1 import BaseIsolatedMemoryTestCase


class TestStage44DeadlineAndGoalAwareness(BaseIsolatedMemoryTestCase):
    """
    Stage 4.4 Verification Suite:
    Validates deadline awareness, checklist task surfacing, context budgeting ceilings,
    anti-hallucination guarantees, and Tauri/runtime integration.
    """

    def setUp(self):
        super().setUp()
        self.now_iso = datetime.now(timezone.utc).isoformat()

    def test_01_active_project_deadlines_surfaced_in_chat_profile(self):
        """1. Active project deadlines are consistently retrieved and surfaced in ordinary chat context."""
        # Insert active project deadline into structured_memories
        self.test_conn.execute(
            """
            INSERT INTO structured_memories (category, memory_key, value, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "project",
                "current",
                "Orange AI Desktop 2.0 - Deadline: 2026-12-01 (Beta freeze Nov 25)",
                1.0,
                self.now_iso,
                self.now_iso,
            ),
        )
        self.test_conn.commit()

        # Execute chat context budgeting
        budget_res = budget_chat_memory(
            query="What is our project deadline and timeline?",
            max_chars=DEFAULT_CHAT_BUDGET_CHARS,
            conn=self.test_conn,
        )

        # Verify structured profile contains [PROJECT] category and active deadline
        self.assertIn("[PROJECT]", budget_res.formatted_profile)
        self.assertIn(
            "current: Orange AI Desktop 2.0 - Deadline: 2026-12-01 (Beta freeze Nov 25)",
            budget_res.formatted_profile,
        )
        self.assertLessEqual(budget_res.total_chars, DEFAULT_CHAT_BUDGET_CHARS)
        self.assertFalse(budget_res.overflow)

    def test_02_unfinished_checklist_tasks_surfaced_in_decision_prompt(self):
        """2. Unfinished checklist tasks are consistently retrieved and surfaced in ordinary chat context."""
        state = AgentState(
            run_id="run_test_02",
            goal="Implement database migration and release",
            original_request="Deploy database migration and release",
            status=AgentStatus.RUNNING,
        )
        state.checklist = [
            TaskItem(
                id="task_1",
                content="Run database migration script",
                status=TaskStatus.COMPLETED,
            ),
            TaskItem(
                id="task_2",
                content="Verify foreign key indexes and WAL checkpoint",
                status=TaskStatus.IN_PROGRESS,
                active_form="Verifying index bounds and checkpointing",
            ),
            TaskItem(
                id="task_3",
                content="Deploy staging release candidate",
                status=TaskStatus.PENDING,
            ),
        ]

        context = build_agent_context(
            goal="Implement database migration and release",
            original_request="Deploy database migration and release",
            state=state,
            max_chars=DEFAULT_AGENT_BUDGET_CHARS,
        )

        prompt = format_decision_prompt(context)

        # Verify prompt renders all tasks with accurate visual states
        self.assertIn("CURRENT TASK CHECKLIST:", prompt)
        self.assertIn("[x] 1. Run database migration script", prompt)
        self.assertIn(
            "[>] 2. Verify foreign key indexes and WAL checkpoint (IN PROGRESS: Verifying index bounds and checkpointing)",
            prompt,
        )
        self.assertIn("[ ] 3. Deploy staging release candidate", prompt)
        self.assertIn("Progress: 1/3 completed (33%)", prompt)

        # Verify unfinished items are distinct and unambiguous
        self.assertIn("Verifying index bounds", prompt)
        self.assertIn("Deploy staging release candidate", prompt)

    def test_03_agent_engine_context_receives_deadline_and_checklist(self):
        """3. Agent Engine context receives the appropriate deadline and checklist information."""
        # Insert active project deadline
        self.test_conn.execute(
            """
            INSERT INTO structured_memories (category, memory_key, value, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "project",
                "current",
                "Stage 4.4 Verification - Hard Deadline: 2026-11-20",
                1.0,
                self.now_iso,
                self.now_iso,
            ),
        )
        self.test_conn.commit()

        state = AgentState(
            run_id="run_test_03",
            goal="Complete Stage 4.4 verification",
            original_request="Verify Stage 4.4 deadline awareness and report",
            status=AgentStatus.RUNNING,
        )
        state.checklist = [
            TaskItem(
                id="task_audit",
                content="Audit frozen subsystems",
                status=TaskStatus.IN_PROGRESS,
            ),
            TaskItem(
                id="task_report",
                content="Generate Stage 4.4 verification report",
                status=TaskStatus.PENDING,
            ),
        ]

        # Case A: General goal (deadline delivered via Tier 2 user profile)
        context_general = build_agent_context(
            goal="Execute assigned work",
            original_request="Execute assigned work",
            state=state,
            max_chars=DEFAULT_AGENT_BUDGET_CHARS,
        )
        self.assertIn("[PROJECT]", context_general.user_profile)
        self.assertIn("current: Stage 4.4 Verification - Hard Deadline: 2026-11-20", context_general.user_profile)
        self.assertEqual(len(context_general.checklist), 2)
        self.assertEqual(context_general.checklist[0]["id"], "task_audit")
        self.assertEqual(context_general.checklist[0]["status"], "in_progress")
        self.assertEqual(context_general.checklist[1]["status"], "pending")

        decision_prompt = format_decision_prompt(context_general)
        self.assertIn("Stage 4.4 Verification - Hard Deadline: 2026-11-20", decision_prompt)
        self.assertIn("[>] 1. Audit frozen subsystems (IN PROGRESS)", decision_prompt)
        self.assertIn("[ ] 2. Generate Stage 4.4 verification report", decision_prompt)

        # Case B: Goal specifically mentioning project name (deadline delivered via query-relevant memories)
        context_specific = build_agent_context(
            goal="Complete Stage 4.4 verification",
            original_request="Complete Stage 4.4 verification",
            state=state,
            max_chars=DEFAULT_AGENT_BUDGET_CHARS,
        )
        self.assertTrue(
            any("Stage 4.4 Verification - Hard Deadline: 2026-11-20" in (m.get("value") or m.get("content") or "")
                for m in context_specific.relevant_memories)
        )
        decision_prompt_specific = format_decision_prompt(context_specific)
        self.assertIn("Stage 4.4 Verification - Hard Deadline: 2026-11-20", decision_prompt_specific)

    def test_04_budgeting_preserves_deadline_and_task_info_under_ceilings(self):
        """4. Memory context budgeting preserves deadline and task information within approved ceilings."""
        # Add high-priority Tier 1 Identity facts
        self.test_conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('identity', 'user_name', 'Lead Engineer', ?, ?)",
            (self.now_iso, self.now_iso),
        )
        # Add Tier 2 Active Project with Deadline
        self.test_conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'current', 'Critical Project Launch - Deadline: 2026-12-15 18:00 UTC', ?, ?)",
            (self.now_iso, self.now_iso),
        )

        # Fill database with large number of lower-tier (Tier 4 / Tier 5) items to force overflow
        for i in range(40):
            self.test_conn.execute(
                "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('preference', ?, ?, ?, ?)",
                (f"pref_item_{i:02d}", f"Highly specific preference detail number {i:02d} with padding text for testing context budgeting truncation boundaries.", self.now_iso, self.now_iso),
            )
            self.test_conn.execute(
                "INSERT INTO memories (content, created_at) VALUES (?, ?)",
                (f"Legacy memory item {i:02d}: user mentioned arbitrary historic detail during turn {i:02d} with extra padding.", self.now_iso),
            )
        self.test_conn.commit()

        # Chat Budgeting Check (1500 chars)
        chat_budget = budget_chat_memory(
            query="Tell me about the project launch",
            max_chars=DEFAULT_CHAT_BUDGET_CHARS,
            conn=self.test_conn,
        )

        self.assertTrue(chat_budget.overflow, "Expected overflow due to massive volume of lower-tier items")
        self.assertLessEqual(chat_budget.total_chars, DEFAULT_CHAT_BUDGET_CHARS)
        # Verify Tier 2 Project deadline was preserved despite dropping lower tier items
        self.assertIn("[PROJECT]", chat_budget.formatted_profile)
        self.assertIn("Critical Project Launch - Deadline: 2026-12-15 18:00 UTC", chat_budget.formatted_profile)
        self.assertIn("Lead Engineer", chat_budget.formatted_profile)

        # Agent Budgeting Check 1: Goal not explicitly mentioning the project name
        agent_budget_general = budget_agent_memory(
            goal="Review system logs and memory",
            max_chars=DEFAULT_AGENT_BUDGET_CHARS,
            conn=self.test_conn,
        )
        self.assertLessEqual(agent_budget_general.combined_rendered_chars, DEFAULT_AGENT_BUDGET_CHARS)
        self.assertIn("[PROJECT]", agent_budget_general.formatted_profile)
        self.assertIn("Critical Project Launch - Deadline: 2026-12-15 18:00 UTC", agent_budget_general.formatted_profile)

        # Agent Budgeting Check 2: Goal specifically relevant to the project
        agent_budget_relevant = budget_agent_memory(
            goal="Plan execution for Critical Project Launch",
            max_chars=DEFAULT_AGENT_BUDGET_CHARS,
            conn=self.test_conn,
        )
        self.assertLessEqual(agent_budget_relevant.combined_rendered_chars, DEFAULT_AGENT_BUDGET_CHARS)
        # In this case, query-relevant structured fact is routed to agent_relevant_memories with zero duplication
        rel_vals = [m.get("value", "") for m in agent_budget_relevant.agent_relevant_memories]
        self.assertTrue(any("Critical Project Launch - Deadline: 2026-12-15 18:00 UTC" in v for v in rel_vals))

    def test_05_completed_dismissed_or_retired_items_reliably_excluded(self):
        """5. Completed, dismissed, or retired items are reliably excluded."""
        # 5a. Retired / forgotten memory exclusion
        self.test_conn.execute(
            "INSERT INTO memories (content, created_at) VALUES ('Obsolete deadline: Project Beta due 2025-01-01', ?)",
            (self.now_iso,),
        )
        self.test_conn.commit()

        # Forget the obsolete memory
        memory_module.forget("Obsolete deadline: Project Beta due 2025-01-01")

        budget_res = budget_chat_memory(
            query="Project Beta deadline",
            max_chars=DEFAULT_CHAT_BUDGET_CHARS,
            conn=self.test_conn,
        )
        self.assertNotIn("Obsolete deadline: Project Beta due 2025-01-01", budget_res.formatted_legacy)

        # 5b. Recommendation suppression / exclusion
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = os.path.join(temp_dir, "recommendations.json")
            store = RecommendationStore(file_path=store_path)

            rec = Recommendation(
                rec_id="project_health:alpha:deadline_slip",
                category="project_blocked",
                entity_id="alpha",
                recommendation_type="deadline_slip",
                title="Alpha Deadline Slipping",
                explanation="2 tasks blocked on external API",
                suggested_action="Review API dependency",
                priority=RecommendationPriority.HIGH,
                evidence_fingerprint="fp123456",
                state=RecommendationState.ACTIVE,
            )
            store.upsert_candidate(rec)

            # Dismiss recommendation (7-day suppression)
            dismissed = store.dismiss(rec.rec_id)
            self.assertTrue(dismissed)

            persisted = store.get(rec.rec_id)
            self.assertEqual(persisted.state, RecommendationState.DISMISSED)
            self.assertIsNotNone(persisted.suppressed_until)

            # Active candidate filtering excludes dismissed items
            active_items = [
                r for r in store.list_all()
                if r.state == RecommendationState.ACTIVE
            ]
            self.assertEqual(len(active_items), 0)

    def test_06_memory_conflict_resolution_and_project_scoping_intact(self):
        """6. Memory conflict resolution and project scoping remain intact."""
        # 6a. Singleton recency: project:current supersedes previous value
        t0 = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        t1 = datetime.now(timezone.utc).isoformat()

        # In structured_memories, UNIQUE(category, memory_key) replaces or updates
        self.test_conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'current', 'Project Titan - Deadline Nov 1', ?, ?)",
            (t0, t0),
        )
        self.test_conn.commit()

        # Update to new project deadline
        self.test_conn.execute(
            "UPDATE structured_memories SET value = 'Project Titan - Deadline Nov 20', updated_at = ? WHERE category = 'project' AND memory_key = 'current'",
            (t1,),
        )
        self.test_conn.commit()

        # 6b. Project scoping: multiple projects coexist under distinct keys
        self.test_conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'mobile_app', 'Mobile Client - Deadline Dec 15', ?, ?)",
            (t1, t1),
        )
        self.test_conn.commit()

        budget = budget_chat_memory(query="project deadlines", max_chars=1500, conn=self.test_conn)

        # Verify new value is present, old value is absent, and scoped mobile_app coexists
        self.assertIn("current: Project Titan - Deadline Nov 20", budget.formatted_profile)
        self.assertNotIn("Project Titan - Deadline Nov 1", budget.formatted_profile)
        self.assertIn("mobile_app: Mobile Client - Deadline Dec 15", budget.formatted_profile)

    def test_07_sparse_context_honesty_zero_hallucination(self):
        """7. Missing or ambiguous information is surfaced honestly without inventing dates or task statuses."""
        # Database has no project memories or deadlines
        cursor = self.test_conn.execute("SELECT COUNT(*) FROM structured_memories WHERE category = 'project'")
        self.assertEqual(cursor.fetchone()[0], 0)

        budget = budget_chat_memory(
            query="What is the deadline for project nebulous?",
            max_chars=1500,
            conn=self.test_conn,
        )

        # Profile does not invent a [PROJECT] header or dates
        self.assertNotIn("[PROJECT]", budget.formatted_profile)
        self.assertNotIn("Deadline", budget.formatted_profile)

        # Empty AgentState checklist
        state = AgentState(
            run_id="run_test_07",
            goal="Inspect undefined task",
            original_request="Inspect undefined task",
            status=AgentStatus.RUNNING,
        )
        context = build_agent_context(
            goal="Inspect undefined task",
            original_request="Inspect undefined task",
            state=state,
        )
        prompt = format_decision_prompt(context)

        # Prompt explicitly displays fallback empty notice
        self.assertIn(
            "CURRENT TASK CHECKLIST:\nNone (For multi-step goals, use tool 'todo_write' to declare a checklist).",
            prompt,
        )

        # Verify core constraints enforce honesty
        self.assertIn("Do NOT invent new tools or assume tools that are not listed.", AGENT_SYSTEM_CONSTRAINTS)
        self.assertIn("Do not mark tasks completed merely because a tool call was attempted.", AGENT_SYSTEM_CONSTRAINTS)

    def test_08_full_chat_pipeline_ordinary_turn_delivers_project_deadline(self):
        """8. Full process_user_turn delivers active project deadline to api_client.assist."""
        self.test_conn.execute(
            "INSERT INTO structured_memories (category, memory_key, value, created_at, updated_at) VALUES ('project', 'current', 'Project Nexus - Final Delivery: 2026-12-25', ?, ?)",
            (self.now_iso, self.now_iso),
        )
        self.test_conn.commit()

        captured_assist_args = {}

        def mock_assist(message, history, system_instruction=None, profile="", **kwargs):
            if kwargs.get("task_type") != "structured_output":
                captured_assist_args["message"] = message
                captured_assist_args["profile"] = profile
                captured_assist_args["system_instruction"] = system_instruction
            return {"reply": "Acknowledged delivery date Dec 25, 2026.", "model": "test-mock", "success": True}

        if main.api_client is None:
            main.api_client = main.OrangeAPIClient()

        with patch.object(main.api_client, "assist", side_effect=mock_assist):
            res = main.process_user_turn("What is our current project deadline?")
            self.assertEqual(res.get("type"), "assistant")
            self.assertIn("Acknowledged delivery date", res.get("reply", ""))

        # Verify api_client.assist received the profile containing the active project deadline
        received_profile = captured_assist_args.get("profile", "")
        self.assertIn("[PROJECT]", received_profile)
        self.assertIn("current: Project Nexus - Final Delivery: 2026-12-25", received_profile)

    def test_09_tauri_runtime_adapter_integration_in_isolated_environment(self):
        """9. Tauri runtime adapter delegates cleanly to core engine under isolated environment."""
        tauri_runtime_path = r"D:\Orange AI\Orange AI Tauri\runtime"
        if tauri_runtime_path not in sys.path:
            sys.path.insert(0, tauri_runtime_path)

        import runtime_adapter

        # Verify runtime_adapter exposes registered JSON-RPC handlers
        self.assertTrue(callable(runtime_adapter.handle_chat_send))
        self.assertTrue(callable(runtime_adapter.handle_chat_history))
        self.assertTrue(callable(runtime_adapter.handle_activity_get))
        self.assertTrue(callable(runtime_adapter.handle_settings_get))

        # Test history retrieval against isolated DB
        self.test_conn.execute(
            "INSERT INTO conversation_history (role, content, created_at) VALUES ('user', 'Hello Tauri', ?)",
            (self.now_iso,),
        )
        self.test_conn.commit()

        history_items = runtime_adapter.handle_chat_history({"limit": 5})
        self.assertGreaterEqual(len(history_items), 1)
        self.assertEqual(history_items[-1]["role"], "user")
        self.assertEqual(history_items[-1]["content"], "Hello Tauri")


if __name__ == "__main__":
    unittest.main()
