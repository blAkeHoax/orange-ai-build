"""
test_stage5b_hybrid_memory_and_empathy.py - Comprehensive Unit Tests for Stage 5B.

Tests:
1. Pre-turn Turn-1 Calibration (< 2ms local detector):
   - Frustrated message with error markers -> direct_fix style directive.
   - Late-night hour (0 <= hour < 5) -> fatigued style directive.
   - Milestone celebration message -> excited momentum style directive.
   - Calm/focused technical request -> focused peer collaborator directive.
   - Toggle behavior when adaptive_empathy_enabled is True vs False.
2. Hybrid Memory & Conversational State Extraction:
   - Valid JSON parsing and normalization of memories & state.
   - Markdown fence stripping (```json ... ```).
   - Malformed JSON fallback returning empty memories without raising.
   - Confidence threshold filtering (filtering below threshold, keeping above).
   - Fast-skip on 1-2 word acknowledgments and slash commands.
   - Deduplication against deterministic regex rules (regex precedence).
   - No-op write prevention when memory is already stored identically.
3. Turn Integration & Session State:
   - main.process_user_turn populates outcome["learned"], outcome["retired"], and outcome["conversational_state"].
   - main.reset_session resets history and _session_conversational_state.
"""

import os
import sys
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

DESKTOP_DIR = os.path.abspath(os.path.dirname(__file__))
if DESKTOP_DIR not in sys.path:
    sys.path.insert(0, DESKTOP_DIR)

import brain.memory as memory_module
import main
from brain.memory_classifier import (
    classify_memory,
    detect_conversational_state,
    build_adaptive_style_directive,
    extract_memories_and_state_hybrid,
)


class BaseIsolatedStage5BTestCase(unittest.TestCase):
    """Base test case providing an isolated temporary SQLite database for brain.memory."""

    def setUp(self):
        self._orig_conn = memory_module._conn
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name

        self.test_conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=10.0,
        )
        self.test_conn.execute("PRAGMA journal_mode=WAL;")
        self.test_conn.execute("PRAGMA busy_timeout = 5000;")

        # Create schema tables identically to memory.py
        self.test_conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.test_conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.test_conn.execute("""
            CREATE TABLE IF NOT EXISTS structured_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(category, memory_key)
            )
        """)
        self.test_conn.commit()
        memory_module._conn = self.test_conn

        # Reset in-memory session state
        main.reset_session()
        main.history = []

    def tearDown(self):
        try:
            self.test_conn.close()
        except Exception:
            pass
        memory_module._conn = self._orig_conn
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass


class TestPreTurnCalibration(BaseIsolatedStage5BTestCase):
    """Test instant pre-turn conversational signal detection and adaptive style directives."""

    def test_01_detector_latency_under_2ms(self):
        """Pre-turn detector executes locally in under 2ms."""
        msg = "It is still failing with TypeError: cannot unpack non-iterable NoneType object"
        start = time.perf_counter()
        state = detect_conversational_state(msg)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        self.assertLess(elapsed_ms, 2.0, f"Detector took {elapsed_ms:.3f}ms, expected < 2ms")
        self.assertEqual(state["emotional_tone"], "frustrated")
        self.assertEqual(state["cognitive_load"], "high")
        self.assertEqual(state["preferred_pacing"], "direct_fix")

    def test_02_frustrated_directive_wording(self):
        """Frustrated signal produces the exact required grounded directive."""
        state = detect_conversational_state("Still failing with SyntaxError again! Ugh, this is stuck.")
        directive = build_adaptive_style_directive(state)
        expected = "User is hitting friction. Acknowledge briefly in one grounded sentence without cheerleading, skip filler, and lead immediately with the exact root-cause fix."
        self.assertEqual(directive, expected)

    def test_03_late_night_fatigued_directive_wording(self):
        """Late-night hours (0 <= hour < 5) inject the fatigued/low-friction directive."""
        state = detect_conversational_state("Let's look at the remaining items", current_hour=2)
        directive = build_adaptive_style_directive(state)
        expected = "Keep response crisp, scannable, and low-friction; avoid walls of options and offer to queue non-urgent items for tomorrow."
        self.assertEqual(directive, expected)
        self.assertEqual(state["emotional_tone"], "fatigued")
        self.assertEqual(state["cognitive_load"], "high")

    def test_04_excited_milestone_directive_wording(self):
        """Milestone celebration injects the momentum-matching directive."""
        state = detect_conversational_state("All 117 tests passed 100%! Finally it worked!")
        directive = build_adaptive_style_directive(state)
        expected = "Match the user's momentum, validate the win concretely, and highlight the immediate next step."
        self.assertEqual(directive, expected)
        self.assertEqual(state["emotional_tone"], "excited")
        self.assertEqual(state["cognitive_load"], "low")

    def test_05_focused_calm_directive_wording(self):
        """Calm / focused technical message injects the peer-collaborator directive."""
        state = detect_conversational_state("Please implement the schema migration for structured_memories", current_hour=14)
        directive = build_adaptive_style_directive(state)
        expected = "Respond as a sharp, grounded peer collaborator—clear, direct, and proportional to task complexity."
        self.assertEqual(directive, expected)
        self.assertIn(state["emotional_tone"], ("focused", "calm"))

    def test_06_turn_1_frustrated_system_instruction_injection(self):
        """Turn 1 frustrated message injects adaptive directive into outbound api_client.assist."""
        captured_calls = []

        def mock_assist(*args, **kwargs):
            captured_calls.append(kwargs)
            return {"reply": "Fixed the null pointer exception.", "model": "nvidia/nemotron-3.5-bolt-e4b"}

        mock_client = MagicMock()
        mock_client.assist.side_effect = mock_assist
        mock_client.is_available.return_value = True

        with patch.object(main, "api_client", mock_client), \
             patch("main._get_setting", side_effect=lambda k, d=None: True if k in ("adaptive_empathy_enabled", "hybrid_memory_extraction_enabled") else d):
            outcome = main.process_user_turn("Still failing with ModuleNotFoundError! Stuck on turn 1.")

            primary_calls = [c for c in captured_calls if c.get("task_type") != "structured_output"]
            self.assertEqual(len(primary_calls), 1)
            sys_inst = primary_calls[0].get("system_instruction", "")
            self.assertIn("ADAPTIVE CONVERSATIONAL DIRECTIVE", sys_inst)
            self.assertIn("User is hitting friction.", sys_inst)
            self.assertEqual(outcome["conversational_state"]["emotional_tone"], "frustrated")

    def test_07_adaptive_empathy_toggle_disabled(self):
        """When adaptive_empathy_enabled is False, no adaptive directive is injected."""
        captured_calls = []

        def mock_assist(*args, **kwargs):
            captured_calls.append(kwargs)
            return {"reply": "Standard assistant reply.", "model": "nvidia/nemotron-3.5-bolt-e4b"}

        mock_client = MagicMock()
        mock_client.assist.side_effect = mock_assist
        mock_client.is_available.return_value = True

        with patch.object(main, "api_client", mock_client), \
             patch("main._get_setting", side_effect=lambda k, d=None: False if k == "adaptive_empathy_enabled" else d):
            main.process_user_turn("Still failing with ModuleNotFoundError!")

            primary_calls = [c for c in captured_calls if c.get("task_type") != "structured_output"]
            self.assertEqual(len(primary_calls), 1)
            sys_inst = primary_calls[0].get("system_instruction", "")
            self.assertNotIn("ADAPTIVE CONVERSATIONAL DIRECTIVE", sys_inst)


class TestHybridExtractionAndResilience(BaseIsolatedStage5BTestCase):
    """Test parallel hybrid memory extraction, fence stripping, threshold filtering, and deduplication."""

    def test_08_hybrid_extraction_valid_json(self):
        """Valid JSON from MiniMax M3.1 Flash is parsed into structured memories and state."""
        mock_client = MagicMock()
        mock_client.assist.return_value = {
            "reply": json.dumps({
                "memories": [
                    {
                        "action": "store",
                        "category": "project",
                        "key": "nexus_core",
                        "value": "High throughput telemetry pipeline",
                        "confidence": 0.92
                    }
                ],
                "conversational_state": {
                    "emotional_tone": "focused",
                    "cognitive_load": "low",
                    "preferred_pacing": "collaborative_brainstorm",
                    "empathy_cue": "User is focused on telemetry pipeline"
                }
            })
        }
        res = extract_memories_and_state_hybrid("I am designing Nexus Core telemetry pipeline", mock_client)
        self.assertEqual(len(res["memories"]), 1)
        mem = res["memories"][0]
        self.assertEqual(mem["category"], "project")
        self.assertEqual(mem["key"], "nexus_core")
        self.assertEqual(mem["value"], "High throughput telemetry pipeline")
        self.assertEqual(mem["confidence"], 0.92)
        self.assertEqual(res["conversational_state"]["emotional_tone"], "focused")

    def test_09_markdown_fence_stripping(self):
        """Code fences (```json ... ```) are cleanly stripped and parsed."""
        fenced_payload = (
            "```json\n"
            "{\n"
            '  "memories": [\n'
            '    {\n'
            '      "action": "store",\n'
            '      "category": "tool",\n'
            '      "key": "postgres_db",\n'
            '      "value": "PostgreSQL 16",\n'
            '      "confidence": 0.88\n'
            '    }\n'
            '  ],\n'
            '  "conversational_state": {\n'
            '    "emotional_tone": "calm",\n'
            '    "cognitive_load": "low",\n'
            '    "preferred_pacing": "direct_fix",\n'
            '    "empathy_cue": "User stated database stack"\n'
            '  }\n'
            "}\n"
            "```"
        )
        mock_client = MagicMock()
        mock_client.assist.return_value = {"reply": fenced_payload}
        res = extract_memories_and_state_hybrid("We use PostgreSQL 16 for data storage", mock_client)
        self.assertEqual(len(res["memories"]), 1)
        self.assertEqual(res["memories"][0]["key"], "postgres_db")
        self.assertEqual(res["conversational_state"]["emotional_tone"], "calm")

    def test_10_malformed_json_graceful_fallback(self):
        """Malformed JSON or network failures return empty memories without raising."""
        mock_client = MagicMock()
        mock_client.assist.return_value = {"reply": "This is definitely not JSON { broken ..."}
        res = extract_memories_and_state_hybrid("Some complex prompt", mock_client)
        self.assertEqual(res["memories"], [])
        self.assertIsNone(res["conversational_state"])

        # Also verify client exceptions fall back gracefully
        mock_client.assist.side_effect = RuntimeError("Network timeout")
        res2 = extract_memories_and_state_hybrid("Another prompt", mock_client)
        self.assertEqual(res2["memories"], [])
        self.assertIsNone(res2["conversational_state"])

    def test_11_confidence_threshold_filtering(self):
        """Memories below min_confidence are excluded; memories at or above are retained."""
        payload = {
            "memories": [
                {"action": "store", "category": "preference", "key": "font_size", "value": "14px", "confidence": 0.65},
                {"action": "store", "category": "tool", "key": "editor", "value": "Neovim", "confidence": 0.85},
            ],
            "conversational_state": {"emotional_tone": "calm", "cognitive_load": "low", "preferred_pacing": "direct_fix", "empathy_cue": "Config"}
        }
        mock_client = MagicMock()
        mock_client.assist.return_value = {"reply": json.dumps(payload)}

        res = extract_memories_and_state_hybrid("Configuring Neovim", mock_client, min_confidence=0.75)
        self.assertEqual(len(res["memories"]), 1)
        self.assertEqual(res["memories"][0]["key"], "editor")

    def test_12_fast_skip_on_acknowledgments_and_slash_commands(self):
        """Trivial acknowledgments and slash commands skip the LLM call entirely."""
        mock_client = MagicMock()
        for ack in ("ok", "thanks", "thank you", "got it", "cool", "hi", "/help", "/settings"):
            res = extract_memories_and_state_hybrid(ack, mock_client)
            self.assertEqual(res["memories"], [])
            self.assertIsNone(res["conversational_state"])
        self.assertEqual(mock_client.assist.call_count, 0)

    def test_13_deduplication_regex_precedence(self):
        """Deterministic regex extraction takes precedence over duplicate (category, key) from LLM."""
        def mock_assist(*args, **kwargs):
            if kwargs.get("task_type") == "structured_output":
                return {
                    "reply": json.dumps({
                        "memories": [
                            # Duplicate of regex-extracted identity/name
                            {"action": "store", "category": "identity", "key": "name", "value": "Sayan", "confidence": 0.80},
                            # Novel memory
                            {"action": "store", "category": "goal", "key": "cloud_cert", "value": "AWS Solutions Architect", "confidence": 0.90}
                        ],
                        "conversational_state": {
                            "emotional_tone": "focused",
                            "cognitive_load": "low",
                            "preferred_pacing": "collaborative_brainstorm",
                            "empathy_cue": "User learning AWS"
                        }
                    })
                }
            return {"reply": "Nice to meet you, Sayan Dey!", "model": "nvidia/nemotron-3.5-bolt-e4b"}

        mock_client = MagicMock()
        mock_client.assist.side_effect = mock_assist
        mock_client.is_available.return_value = True

        with patch.object(main, "api_client", mock_client), \
             patch("main._get_setting", side_effect=lambda k, d=None: True if k in ("adaptive_empathy_enabled", "hybrid_memory_extraction_enabled") else d):
            outcome = main.process_user_turn("My name is Sayan Dey. I am aiming for AWS Solutions Architect certification.")

            learned = outcome.get("learned", [])
            # Must contain regex name "Sayan Dey" and NOT the hybrid duplicate "Sayan"
            name_memories = [m for m in learned if m.get("category") == "identity" and m.get("key") == "name"]
            self.assertEqual(len(name_memories), 1)
            self.assertEqual(name_memories[0]["value"], "Sayan Dey")

            # Novel hybrid goal memory must be learned
            goal_memories = [m for m in learned if m.get("key") == "cloud_cert"]
            self.assertEqual(len(goal_memories), 1)
            self.assertEqual(goal_memories[0]["value"], "AWS Solutions Architect")

    def test_14_no_op_write_prevention(self):
        """Novel LLM extractions skip writing when (category, key, value) already exists identically."""
        # Pre-seed structured memory
        memory_module.remember_structured("project", "hydra_engine", "Real-time audio graph", 0.95)

        def mock_assist(*args, **kwargs):
            if kwargs.get("task_type") == "structured_output":
                return {
                    "reply": json.dumps({
                        "memories": [
                            # Identical to pre-seeded memory
                            {"action": "store", "category": "project", "key": "hydra_engine", "value": "Real-time audio graph", "confidence": 0.90}
                        ],
                        "conversational_state": {"emotional_tone": "focused", "cognitive_load": "low", "preferred_pacing": "collaborative_brainstorm", "empathy_cue": "Audio"}
                    })
                }
            return {"reply": "Acknowledged.", "model": "nvidia/nemotron-3.5-bolt-e4b"}

        mock_client = MagicMock()
        mock_client.assist.side_effect = mock_assist
        mock_client.is_available.return_value = True

        with patch.object(main, "api_client", mock_client), \
             patch("main._get_setting", side_effect=lambda k, d=None: True if k in ("adaptive_empathy_enabled", "hybrid_memory_extraction_enabled") else d):
            outcome = main.process_user_turn("We are testing Hydra Engine audio graph.")
            # Because it is an identical write, it is skipped as a no-op
            self.assertEqual(outcome.get("learned", []), [])


class TestSessionResetAndState(BaseIsolatedStage5BTestCase):
    """Test session conversational state management and reset."""

    def test_15_session_reset_clears_state_and_history(self):
        """reset_session() resets in-memory conversation history and _session_conversational_state."""
        main.history = [{"role": "user", "content": "hello"}]
        main._session_conversational_state = {"emotional_tone": "frustrated", "cognitive_load": "high"}

        main.reset_session()
        self.assertEqual(len(main.history), 0)
        self.assertIsNone(main._session_conversational_state)


if __name__ == "__main__":
    unittest.main()
