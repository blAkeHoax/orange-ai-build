"""
Regression test suite for Orange AI post-forget recall defect.
Verifies that calling forget(keyword, active_history) purges matching records
across memories, structured_memories, conversation_history, and active in-memory history,
and that simulated restarts and mocked model requests do NOT recall the forgotten token.
"""

import os
import sys
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

desktop_dir = os.path.dirname(os.path.abspath(__file__))
if desktop_dir not in sys.path:
    sys.path.insert(0, desktop_dir)
api_dir = os.path.join(os.path.dirname(desktop_dir), "Orange API")
if api_dir not in sys.path:
    sys.path.insert(0, api_dir)

import brain.memory as memory_module
from brain.conflict_resolver import clear_session_retractions
from test_memory_phase1 import BaseIsolatedMemoryTestCase
from app.services.assist_service import AssistService


class TestPostForgetRecall(BaseIsolatedMemoryTestCase):
    """
    Validates end-to-end forgetting of sensitive/temporary tokens (like ORANGE-7319)
    across persistent tables, active session history, restart reloads, and model payloads.
    """

    def setUp(self):
        super().setUp()
        clear_session_retractions()

    def tearDown(self):
        clear_session_retractions()
        super().tearDown()

    def test_post_forget_recall_full_lifecycle(self):
        token = "ORANGE-7319"
        unrelated_token = "PERSISTENT-9999"

        # 1. Seed data across all three tables
        # A. Legacy memories
        memory_module.remember(f"Secret code is {token}")
        memory_module.remember(f"Unrelated note {unrelated_token}")

        # B. Structured memories
        memory_module.remember_structured("preference", "test_code", token)
        memory_module.remember_structured("identity", "name", "Orange User")

        # C. Conversation history
        memory_module.save_turn("user", "Hello, I am testing Orange AI.")
        memory_module.save_turn("assistant", "Hello! Ready to help.")
        memory_module.save_turn("user", f"Remember my temporary test code: {token}")
        memory_module.save_turn("assistant", f"Your temporary test code is {token}.")
        memory_module.save_turn("user", f"Also keep this unrelated note: {unrelated_token}")
        memory_module.save_turn("assistant", f"Acknowledged {unrelated_token}.")

        # 2. Verify pre-forget state in DB
        cur = self.test_conn.cursor()
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM memories WHERE content LIKE ?", (f"%{token}%",)).fetchone()[0], 1)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM structured_memories WHERE value LIKE ?", (f"%{token}%",)).fetchone()[0], 1)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM conversation_history WHERE content LIKE ?", (f"%{token}%",)).fetchone()[0], 2)

        # Pre-forget active in-memory history
        active_history = memory_module.load_history(limit=40)
        self.assertEqual(len(active_history), 6)
        self.assertTrue(any(token in turn["content"] for turn in active_history))

        # 3. Execute forget(token, active_history=active_history)
        deleted_count = memory_module.forget(token, active_history=active_history)
        self.assertGreaterEqual(deleted_count, 4)  # 1 legacy + 1 structured + 2 history

        # 4. Confirm NO matching records remain in any of the three persistent tables
        self.assertEqual(
            cur.execute("SELECT COUNT(*) FROM memories WHERE content LIKE ?", (f"%{token}%",)).fetchone()[0],
            0,
            "Legacy memories must not contain forgotten token"
        )
        self.assertEqual(
            cur.execute("SELECT COUNT(*) FROM structured_memories WHERE value LIKE ? OR memory_key LIKE ?", (f"%{token}%", f"%{token}%")).fetchone()[0],
            0,
            "Structured memories must not contain forgotten token"
        )
        self.assertEqual(
            cur.execute("SELECT COUNT(*) FROM conversation_history WHERE content LIKE ?", (f"%{token}%",)).fetchone()[0],
            0,
            "Conversation history must not contain forgotten token"
        )

        # 5. Confirm active session history was pruned in-place
        self.assertFalse(
            any(token in turn["content"] for turn in active_history),
            "Active session history must not contain forgotten token"
        )
        self.assertEqual(len(active_history), 4, "Only the 2 matching turns should have been pruned")

        # 6. Confirm unrelated records remain completely intact
        self.assertEqual(
            cur.execute("SELECT COUNT(*) FROM memories WHERE content LIKE ?", (f"%{unrelated_token}%",)).fetchone()[0],
            1,
            "Unrelated legacy memory must be preserved"
        )
        self.assertEqual(
            cur.execute("SELECT COUNT(*) FROM structured_memories WHERE memory_key='name'").fetchone()[0],
            1,
            "Unrelated structured memory must be preserved"
        )
        self.assertEqual(
            cur.execute("SELECT COUNT(*) FROM conversation_history WHERE content LIKE ?", (f"%{unrelated_token}%",)).fetchone()[0],
            2,
            "Unrelated conversation history turns must be preserved"
        )
        self.assertTrue(any(unrelated_token in turn["content"] for turn in active_history))

        # 7. Simulate restart: load history fresh from the database
        reloaded_history = memory_module.load_history(limit=40)
        self.assertEqual(len(reloaded_history), 4)
        self.assertFalse(
            any(token in turn["content"] for turn in reloaded_history),
            "Reloaded history after simulated restart must not contain forgotten token"
        )
        self.assertTrue(any(unrelated_token in turn["content"] for turn in reloaded_history))

        # 8. Verify mocked model request payload does NOT contain forgotten token
        # Build prompt using AssistService
        assist_service = AssistService()
        with patch.object(assist_service.llm, "generate_response", return_value=("Mock reply", "mock:model")) as mock_gen:
            user_msg = "What was my temporary test code?"
            assist_service.assist(
                message=user_msg,
                history=[memory_module.Message(**t) if hasattr(memory_module, 'Message') else t for t in reloaded_history] if False else None,
                # In assist_service, history expects schemas.Message objects
            )
            # Or directly verify LLMService request construction
            from app.models.schemas import Message
            typed_history = [Message(role=t["role"], content=t["content"]) for t in reloaded_history]
            assist_service.assist(
                message=user_msg,
                history=typed_history,
                profile="",
                system_instruction="System prompt",
            )
            self.assertTrue(mock_gen.called)
            call_kwargs = mock_gen.call_args[1]
            sent_history = call_kwargs.get("history")
            self.assertIsNotNone(sent_history)
            for turn in sent_history:
                self.assertNotIn(token, turn["content"])
                self.assertNotIn(token, str(turn))

    def test_forget_sql_injection_and_wildcard_escaping(self):
        """Verifies that special characters like % and _ are escaped and not treated as wildcards."""
        cur = self.test_conn.cursor()
        memory_module.save_turn("assistant", "Score is 100% complete")
        memory_module.save_turn("assistant", "Score is 1000 complete")

        # forget("%") should fail min-length check (<3 chars)
        res = memory_module.forget("%")
        self.assertEqual(res, 0)

        # forget("100%") should only delete the exact "100%" turn, not "1000"
        active_hist = memory_module.load_history()
        deleted = memory_module.forget("100%", active_history=active_hist)
        self.assertEqual(deleted, 1)

        # Confirm "1000 complete" is still present
        remaining = memory_module.load_history()
        self.assertEqual(len(remaining), 1)
        self.assertIn("1000 complete", remaining[0]["content"])


if __name__ == "__main__":
    unittest.main()
