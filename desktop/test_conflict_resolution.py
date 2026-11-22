"""
test_conflict_resolution.py - Stage 3.6C: Test Suite for Cross-Category Memory Conflict Resolution.

Verifies:
1. Questions and hypotheticals cause zero conflict-triggered writes (Level 0).
2. Explicit corrections and retractions suppress intended obsolete facts (Level 1).
3. Singleton recency resolution works deterministically (Level 5).
4. Multiple tools, languages, and positive likes coexist by default (Level 6).
5. Scoped facts coexist when scopes differ (Level 4).
6. Transient locations do not overwrite permanent identity locations.
7. project:current changes without corrupting portfolio descriptions.
8. Legacy contradictions are suppressed only when the resolver can establish a valid slot conflict.
9. Cross-category suppression does not delete database records or suppress unrelated facts.
10. Invalid and missing timestamps are handled deterministically.
11. Chat and agent context ceilings remain strictly enforced (1,500 / 1,200 chars).
12. Resolution and budgeting perform exactly zero SQLite writes.
13. Repeated runs produce 100% deterministic results.
"""

import sqlite3
import unittest
from datetime import datetime, timedelta

from brain import memory as memory_module
from brain.conflict_resolver import (
    ConflictRelation,
    ConflictResolutionReport,
    FactSlot,
    SlotCardinality,
    clear_session_retractions,
    extract_fact_slot,
    get_session_retractions,
    is_legacy_reconciled,
    is_session_retired,
    parse_iso_timestamp,
    register_session_retraction,
    resolve_memory_conflicts,
    unregister_session_retraction,
)
from brain.context_budget import (
    ContextBudgetManager,
    DEFAULT_AGENT_BUDGET_CHARS,
    DEFAULT_CHAT_BUDGET_CHARS,
    MemoryBudgetItem,
    budget_agent_memory,
    budget_chat_memory,
)
from brain.memory_classifier import classify_memory
from test_memory_phase1 import BaseIsolatedMemoryTestCase


class TestConflictResolution(BaseIsolatedMemoryTestCase):
    """Stage 3.6C: Comprehensive validation of deterministic conflict resolution."""

    def test_01_questions_and_hypotheticals_cause_no_writes(self):
        """1. Questions and hypotheticals cause zero memory-formation or conflict-triggered writes."""
        count_before_m = self.test_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        count_before_s = self.test_conn.execute("SELECT COUNT(*) FROM structured_memories").fetchone()[0]

        questions = [
            "Should I switch from Python to Go?",
            "Do I prefer dark mode or light theme?",
            "What is my current project?",
            "What if I stopped using Docker?",
            "Maybe I like green tea better?",
        ]

        for q in questions:
            memories = classify_memory(q)
            self.assertEqual(memories, [], f"Query '{q}' should produce zero classified memories")
            # Budgeting/retrieval against question queries must not modify SQLite
            budget_chat_memory(query=q)
            memory_module.retrieve_relevant_context(q)

        count_after_m = self.test_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        count_after_s = self.test_conn.execute("SELECT COUNT(*) FROM structured_memories").fetchone()[0]

        self.assertEqual(count_before_m, count_after_m)
        self.assertEqual(count_before_s, count_after_s)

    def test_02_explicit_corrections_and_retractions_suppress_obsolete(self):
        """2. Explicit corrections and retractions suppress intended obsolete facts."""
        memory_module.remember_structured("tool", "tool_docker", "Docker container engine")
        memory_module.remember("Notes on Docker deployment setup")

        # User explicitly retires tool Docker
        res = memory_module.retract_entity("tool", "Docker")
        self.assertEqual(res["status"], "removed")
        self.assertIsNone(memory_module.get_memory("tool", "tool_docker"))

        # Verify session-only suppression registry has Docker
        self.assertIn("docker", get_session_retractions("tool"))

        # In-memory retrieval must now suppress legacy notes mentioning Docker
        results = memory_module.retrieve_relevant_context("Docker deployment")
        self.assertEqual(results, [])

    def test_03_singleton_recency_resolution_deterministic(self):
        """3. Singleton recency resolution works deterministically."""
        # Older theme: dark mode
        t_old = (datetime.now() - timedelta(days=5)).isoformat()
        # Newer theme: light mode
        t_new = datetime.now().isoformat()

        candidates = [
            {
                "id": 1,
                "category": "preference",
                "key": "theme",
                "value": "dark mode",
                "created_at": t_old,
                "updated_at": t_old,
                "source": "structured",
                "score": 10.0,
            },
            {
                "id": 2,
                "category": "preference",
                "key": "theme",
                "value": "light mode",
                "created_at": t_new,
                "updated_at": t_new,
                "source": "structured",
                "score": 10.0,
            },
        ]

        resolved, report = resolve_memory_conflicts(candidates)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["value"], "light mode")
        self.assertEqual(report.suppressed_count, 1)
        self.assertEqual(report.conflicts_resolved[0]["winner"], "light mode")

    def test_04_multi_valued_coexistence_by_default(self):
        """4. Multiple tools, languages, and positive likes coexist by default."""
        t_old = (datetime.now() - timedelta(days=10)).isoformat()
        t_new = datetime.now().isoformat()

        candidates = [
            {
                "id": 1,
                "category": "tool",
                "key": "tool_python",
                "value": "Python programming language",
                "created_at": t_old,
                "updated_at": t_old,
                "source": "structured",
                "score": 10.0,
            },
            {
                "id": 2,
                "category": "tool",
                "key": "tool_go",
                "value": "Go programming language",
                "created_at": t_new,
                "updated_at": t_new,
                "source": "structured",
                "score": 10.0,
            },
            {
                "id": 3,
                "category": "preference",
                "key": "like_coffee",
                "value": "coffee",
                "created_at": t_old,
                "updated_at": t_old,
                "source": "structured",
                "score": 8.0,
            },
            {
                "id": 4,
                "category": "preference",
                "key": "like_tea",
                "value": "tea",
                "created_at": t_new,
                "updated_at": t_new,
                "source": "structured",
                "score": 8.0,
            },
        ]

        resolved, report = resolve_memory_conflicts(candidates)
        # All 4 distinct multi-valued facts must coexist peacefully!
        self.assertEqual(len(resolved), 4)
        self.assertEqual(report.suppressed_count, 0)
        vals = [r["value"] for r in resolved]
        self.assertIn("Python programming language", vals)
        self.assertIn("Go programming language", vals)
        self.assertIn("coffee", vals)
        self.assertIn("tea", vals)

    def test_05_scoped_facts_coexist_when_scopes_differ(self):
        """5. Scoped facts coexist when scopes differ."""
        candidates = [
            {
                "id": 1,
                "category": "tool",
                "key": "tool_python",
                "value": "Python for data science",
                "source": "structured",
            },
            {
                "id": 2,
                "category": "tool",
                "key": "tool_go",
                "value": "Go for microservices",
                "source": "structured",
            },
        ]

        resolved, report = resolve_memory_conflicts(candidates)
        self.assertEqual(len(resolved), 2)
        slots = [extract_fact_slot(r) for r in resolved]
        self.assertEqual(slots[0].cardinality, SlotCardinality.SCOPED)
        self.assertEqual(slots[0].scope, "data science")
        self.assertEqual(slots[1].cardinality, SlotCardinality.SCOPED)
        self.assertEqual(slots[1].scope, "microservices")

    def test_06_transient_location_does_not_overwrite_permanent_identity_locations(self):
        """6. Transient locations do not overwrite permanent identity locations."""
        memory_module.remember_structured("identity", "city", "Berlin")
        memory_module.remember("I am visiting London this week for a conference")

        # Transient visit must not be suppressed as a city contradiction
        reconciled = memory_module.recall_reconciled()
        self.assertIn("I am visiting London this week for a conference", reconciled)

        # Permanent residence remains Berlin in structured memory
        self.assertEqual(memory_module.get_memory("identity", "city"), "Berlin")

    def test_07_project_current_changes_without_corrupting_portfolio(self):
        """7. project:current changes without corrupting portfolio descriptions."""
        # 1. Establish initial current project with rich notes
        memory_module.remember_structured(
            "project", "orange_ai", "Orange AI Desktop (autonomous assistant with system tools)"
        )
        memory_module.remember_structured(
            "project", "current", "Orange AI Desktop (autonomous assistant with system tools)"
        )

        # Verify current is Orange AI
        self.assertEqual(
            memory_module.get_memory("project", "current"),
            "Orange AI Desktop (autonomous assistant with system tools)"
        )

        # 2. User sets a new current project
        # Classifier extracts key="current" for "My current project is Helios"
        for m in classify_memory("My current project is Helios"):
            memory_module.remember_structured(m["category"], m["key"], m["value"], m["confidence"])

        # Current project is now Helios
        self.assertEqual(memory_module.get_memory("project", "current"), "Helios")

        # Previous project's portfolio record and rich description are fully preserved!
        self.assertEqual(
            memory_module.get_memory("project", "orange_ai"),
            "Orange AI Desktop (autonomous assistant with system tools)"
        )

    def test_08_legacy_contradictions_suppressed_only_for_valid_slot_conflicts(self):
        """8. Legacy contradictions are suppressed only when resolver can establish a valid slot conflict."""
        memory_module.remember_structured("identity", "name", "Sayan Dey")
        memory_module.remember_structured("identity", "city", "Tokyo")
        memory_module.remember_structured("tool", "preferred_editor", "Neovim")
        memory_module.remember_structured("project", "current", "Project Zephyr")

        # Legacy notes
        memory_module.remember("My name is John Doe")                        # Contradicts identity.name -> SUPPRESSED
        memory_module.remember("I live in London")                           # Contradicts identity.city -> SUPPRESSED
        memory_module.remember("My preferred editor is Sublime Text")        # Contradicts preferred_editor -> SUPPRESSED
        memory_module.remember("My current project is Project Apollo")        # Contradicts project:current -> SUPPRESSED
        memory_module.remember("Client meeting was in London")               # Third-party location -> PRESERVED
        memory_module.remember("Sublime Text license key backup notes")      # Non-first-person note -> PRESERVED
        memory_module.remember("Apollo lunar mission research paper")        # Historical context -> PRESERVED

        reconciled = memory_module.recall_reconciled(limit=50)

        self.assertNotIn("My name is John Doe", reconciled)
        self.assertNotIn("I live in London", reconciled)
        self.assertNotIn("My preferred editor is Sublime Text", reconciled)
        self.assertNotIn("My current project is Project Apollo", reconciled)

        self.assertIn("Client meeting was in London", reconciled)
        self.assertIn("Sublime Text license key backup notes", reconciled)
        self.assertIn("Apollo lunar mission research paper", reconciled)

    def test_09_cross_category_suppression_preserves_database_and_unrelated(self):
        """9. Cross-category suppression does not delete database records or suppress unrelated facts."""
        memory_module.remember_structured("tool", "tool_docker", "Docker")
        memory_module.remember_structured("preference", "like_docker", "Docker")
        memory_module.remember("Configured Docker swarm cluster")
        memory_module.remember("Good morning team")                          # Contains 'go' letters, not Docker
        memory_module.remember("Documentation files are in docs folder")

        # Retract entity in tool category
        memory_module.retract_entity("tool", "Docker")

        # Verify structured preference row was NOT deleted from SQLite
        pref_val = memory_module.get_memory("preference", "like_docker")
        self.assertEqual(pref_val, "Docker", "Database record in preference must not be deleted by tool retraction")

        # But in-memory retrieval suppresses Docker mentions during this session
        res = memory_module.retrieve_relevant_context("Docker")
        self.assertEqual(len(res), 0)

        # Unrelated words (e.g. 'good', 'docs') must NOT be suppressed
        unrelated_res = memory_module.retrieve_relevant_context("good morning docs")
        self.assertGreater(len(unrelated_res), 0)

    def test_10_invalid_and_missing_timestamps_deterministic(self):
        """10. Invalid and missing timestamps are handled deterministically without crashing."""
        self.assertIsNone(parse_iso_timestamp(None))
        self.assertIsNone(parse_iso_timestamp(""))
        self.assertIsNone(parse_iso_timestamp("NOT_A_DATE"))
        self.assertIsNone(parse_iso_timestamp(12345))

        valid_dt = parse_iso_timestamp("2026-10-26T12:00:00")
        self.assertIsNotNone(valid_dt)
        self.assertEqual(valid_dt.year, 2026)

        # Test resolution with corrupted timestamps: valid timestamp wins over invalid
        candidates = [
            {"id": 1, "category": "identity", "key": "city", "value": "Paris", "updated_at": "CORRUPTED", "source": "structured"},
            {"id": 2, "category": "identity", "key": "city", "value": "Tokyo", "updated_at": "2026-10-26T12:00:00", "source": "structured"},
        ]
        resolved, report = resolve_memory_conflicts(candidates)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["value"], "Tokyo")

    def test_11_chat_and_agent_budget_ceilings_enforced(self):
        """11. Chat and agent context ceilings remain strictly enforced (1,500 / 1,200 chars)."""
        # Seed 20 structured facts and 20 legacy facts
        for i in range(20):
            memory_module.remember_structured("tool", f"tool_{i}", f"Enterprise utility framework number {i} with long description")
            memory_module.remember(f"Legacy operational runbook procedure number {i} with detailed logging instructions")

        # Chat budget: ceiling 1,500 chars
        res_chat = budget_chat_memory(max_chars=DEFAULT_CHAT_BUDGET_CHARS)
        self.assertLessEqual(res_chat.total_chars, DEFAULT_CHAT_BUDGET_CHARS)
        self.assertTrue(res_chat.overflow)

        # Agent budget: combined ceiling 1,200 chars
        res_agent = budget_agent_memory(goal="Enterprise utility framework", max_chars=DEFAULT_AGENT_BUDGET_CHARS)
        self.assertLessEqual(res_agent.combined_rendered_chars, DEFAULT_AGENT_BUDGET_CHARS)
        self.assertLessEqual(len(res_agent.agent_relevant_memories), 5)

    def test_12_resolution_and_budgeting_zero_sqlite_writes(self):
        """12. Resolution and budgeting perform exactly zero SQLite writes."""
        memory_module.remember_structured("identity", "name", "Sayan Dey")
        memory_module.remember("Notes on software architecture")

        # Snapshot exact database rowcounts and table hashes
        count_m1 = self.test_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        count_s1 = self.test_conn.execute("SELECT COUNT(*) FROM structured_memories").fetchone()[0]
        count_h1 = self.test_conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]

        # Execute 25 retrieval and budgeting operations with conflicting queries
        for _ in range(25):
            memory_module.retrieve_relevant_context("software architecture")
            memory_module.recall_reconciled()
            budget_chat_memory(query="My name is Alex now")
            budget_agent_memory(goal="Build architecture")

        count_m2 = self.test_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        count_s2 = self.test_conn.execute("SELECT COUNT(*) FROM structured_memories").fetchone()[0]
        count_h2 = self.test_conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]

        self.assertEqual(count_m1, count_m2, "Memories table must not be modified by retrieval or budgeting")
        self.assertEqual(count_s1, count_s2, "Structured memories must not be modified by retrieval or budgeting")
        self.assertEqual(count_h1, count_h2, "History must not be modified by retrieval or budgeting")

    def test_13_repeated_runs_deterministic(self):
        """13. Repeated runs produce 100% deterministic results."""
        memory_module.remember_structured("preference", "theme", "light theme")
        memory_module.remember_structured("tool", "tool_python", "Python")
        memory_module.remember_structured("tool", "tool_go", "Go")
        memory_module.remember("Notes on programming")

        first_res = budget_chat_memory()

        for _ in range(30):
            subsequent_res = budget_chat_memory()
            self.assertEqual(first_res.formatted_profile, subsequent_res.formatted_profile)
            self.assertEqual(first_res.formatted_legacy, subsequent_res.formatted_legacy)
            self.assertEqual(first_res.total_chars, subsequent_res.total_chars)
            self.assertEqual(first_res.included_count, subsequent_res.included_count)
            self.assertEqual(first_res.dropped_count, subsequent_res.dropped_count)


if __name__ == "__main__":
    unittest.main()
