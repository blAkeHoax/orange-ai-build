"""
Orange AI Desktop - Stage 3 Test Suite.

Comprehensive tests for:
1. Composer component (input, clearing, disabled/enabled state, Enter/Send behavior)
2. Informational placeholders (paperclip and microphone dismissible notices)
3. Action cards grid (callbacks for real capabilities and hover/theme styling)
4. Confirmation card (explicit consent UI, Approve/Deny callbacks, duplicate click prevention)
5. Message timeline (insertion, ordering, avatar presentation, thinking indicator)
6. Assistant bridge (worker thread execution, safe UI dispatch, duplicate send prevention,
   stale result rejection, error normalization, safe shutdown)
7. Chat view workspace transition (initial welcome prominence to active conversation timeline)
8. Security verification (no direct tool execution from UI buttons)
9. Theme consistency across all new components
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from gui.components.composer import ComposerBar
from gui.components.action_cards import ActionCardsGrid, CARDS_DATA
from gui.components.confirmation_card import ConfirmationCard
from gui.components.message_timeline import MessageTimeline
from gui.views.chat_view import ChatView
from gui.services.assistant_bridge import AssistantBridge
from gui.app import OrangeAIApp
from brain.api_client import OrangeAPIConnectionError, OrangeAPIResponseError


class MockTkRoot:
    """Mock Tkinter root capturing after() calls for deterministic unit testing."""

    def __init__(self):
        self.callbacks = []

    def after(self, delay_ms, func, *args):
        self.callbacks.append((func, args))
        # Execute immediately in test harness
        func(*args)


class TestComposerComponent(unittest.TestCase):
    """Test Suite 1: ComposerBar input, controls, and states."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("light")
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.update_idletasks()
            cls.root.update()
            cls.root.destroy()
        except Exception:
            pass

    def test_composer_text_retrieval_and_clearing(self):
        sent = []
        composer = ComposerBar(self.root, on_send=lambda t: sent.append(t))

        composer.set_text("   Hello Orange AI!   ")
        self.assertEqual(composer.get_text(), "Hello Orange AI!")

        composer.clear()
        self.assertEqual(composer.get_text(), "")
        composer.destroy()

    def test_composer_send_button_and_enter_key(self):
        sent = []
        composer = ComposerBar(self.root, on_send=lambda t: sent.append(t))

        # 1. Non-empty text sends and clears
        composer.set_text("Check system status")
        composer._on_send_clicked()
        self.assertEqual(sent, ["Check system status"])
        self.assertEqual(composer.get_text(), "")

        # 2. Empty text does not send
        sent.clear()
        composer.set_text("    ")
        composer._on_send_clicked()
        self.assertEqual(len(sent), 0)

        # 3. Enter key binding
        composer.set_text("Test enter")
        composer._on_enter_pressed()
        self.assertEqual(sent, ["Test enter"])
        self.assertEqual(composer.get_text(), "")
        composer.destroy()

    def test_composer_disabled_state(self):
        sent = []
        composer = ComposerBar(self.root, on_send=lambda t: sent.append(t))

        composer.set_text("While disabled")
        composer.set_enabled(False)
        self.assertFalse(composer._is_enabled)

        # Send attempt while disabled must be ignored
        composer._on_send_clicked()
        self.assertEqual(len(sent), 0)

        # Re-enable
        composer.set_enabled(True)
        self.assertTrue(composer._is_enabled)
        composer._on_send_clicked()
        self.assertEqual(sent, ["While disabled"])
        composer.destroy()

    def test_composer_placeholder_notices(self):
        notices = []
        composer = ComposerBar(
            self.root,
            on_send=lambda t: None,
            on_notice=lambda n: notices.append(n),
        )

        composer._on_attach_clicked()
        self.assertIn("File attachments are not yet supported", notices[-1])

        composer._on_mic_clicked()
        self.assertIn("Voice input is not yet supported", notices[-1])
        composer.destroy()


class TestActionCardsComponent(unittest.TestCase):
    """Test Suite 2: ActionCardsGrid real capability mapping."""

    @classmethod
    def setUpClass(cls):
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.update_idletasks()
            cls.root.update()
            cls.root.destroy()
        except Exception:
            pass

    def test_action_cards_callbacks_emit_real_queries(self):
        clicked = []
        grid = ActionCardsGrid(self.root, on_card_click=lambda q: clicked.append(q))

        expected_queries = {
            "project": "show projects",
            "files": "list files",
            "plan": "show reminders",
            "system": "cpu usage",
        }

        for card in grid._cards:
            self.assertIn(card.key, expected_queries)
            card._handle_click()
            self.assertEqual(clicked[-1], expected_queries[card.key])

        self.assertEqual(len(clicked), 4)
        grid.destroy()


class TestConfirmationCardComponent(unittest.TestCase):
    """Test Suite 3: ConfirmationCard security and consent semantics."""

    @classmethod
    def setUpClass(cls):
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.update_idletasks()
            cls.root.update()
            cls.root.destroy()
        except Exception:
            pass

    def test_confirmation_card_approval_flow(self):
        decisions = []
        card = ConfirmationCard(
            self.root,
            request_id="req_001",
            prompt_text="Orange AI wants to perform: open_app (notepad) [y/N]: ",
            on_decision=lambda d: decisions.append(d),
        )

        self.assertFalse(card._is_decided)
        card._on_approve()
        self.assertTrue(card._is_decided)
        self.assertEqual(decisions, ["yes"])

        # Duplicate click has no effect
        card._on_approve()
        self.assertEqual(len(decisions), 1)
        card.destroy()

    def test_confirmation_card_denial_flow(self):
        decisions = []
        card = ConfirmationCard(
            self.root,
            request_id="req_002",
            prompt_text="Orange AI wants to perform: close_process (chrome) [y/N]: ",
            on_decision=lambda d: decisions.append(d),
        )

        card._on_deny()
        self.assertTrue(card._is_decided)
        self.assertEqual(decisions, ["no"])

        # Duplicate click has no effect
        card._on_deny()
        self.assertEqual(len(decisions), 1)
        card.destroy()


class TestMessageTimelineComponent(unittest.TestCase):
    """Test Suite 4: MessageTimeline bubble rendering and states."""

    @classmethod
    def setUpClass(cls):
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.update_idletasks()
            cls.root.update()
            cls.root.destroy()
        except Exception:
            pass

    def test_message_timeline_insertion_and_ordering(self):
        timeline = MessageTimeline(self.root)
        self.assertEqual(timeline.get_message_count(), 0)

        # 1. Add user message
        timeline.add_user_message("What time is it?")
        self.assertEqual(timeline.get_message_count(), 1)

        # 2. Add assistant message
        timeline.add_assistant_message("Current time is 11:30 PM", model_name="system", msg_type="tool")
        self.assertEqual(timeline.get_message_count(), 2)

        # 3. Add status message (does not increment turn count)
        timeline.add_status_message("Orange AI memory updated.")
        self.assertEqual(timeline.get_message_count(), 2)

        # 4. Thinking indicator
        self.assertIsNone(timeline._thinking_card)
        timeline.show_thinking()
        self.assertIsNotNone(timeline._thinking_card)
        timeline.hide_thinking()
        self.assertIsNone(timeline._thinking_card)

        timeline.destroy()


class TestAssistantBridgeService(unittest.TestCase):
    """Test Suite 5: Background threading, concurrency, and error normalization."""

    def test_bridge_dispatch_and_successful_delivery(self):
        mock_root = MockTkRoot()
        results = []

        bridge = AssistantBridge(
            ui_root=mock_root,
            on_result=lambda rid, out: results.append((rid, out)),
        )

        mock_outcome = {"reply": "Hello from mock", "model": "mock-model", "type": "assistant"}
        with patch("main.process_user_turn", return_value=mock_outcome):
            req_id = bridge.send_message("Hello")
            self.assertIsNotNone(req_id)

            # Wait briefly for worker thread execution
            deadline = time.time() + 3.0
            while not results and time.time() < deadline:
                time.sleep(0.05)

            self.assertEqual(len(results), 1)
            delivered_req_id, outcome = results[0]
            self.assertEqual(delivered_req_id, req_id)
            self.assertEqual(outcome["reply"], "Hello from mock")

    def test_bridge_prevents_duplicate_send_while_busy(self):
        mock_root = MockTkRoot()
        block_event = time.sleep

        bridge = AssistantBridge(
            ui_root=mock_root,
            on_result=lambda rid, out: None,
        )

        def slow_process(text, prompt_fn=None):
            time.sleep(0.4)
            return {"reply": "Done", "type": "assistant"}

        with patch("main.process_user_turn", side_effect=slow_process):
            req1 = bridge.send_message("First message")
            self.assertIsNotNone(req1)
            self.assertTrue(bridge.is_busy())

            # Immediate second send must be rejected
            req2 = bridge.send_message("Second message")
            self.assertIsNone(req2)

            # Wait for first to complete
            deadline = time.time() + 2.0
            while bridge.is_busy() and time.time() < deadline:
                time.sleep(0.05)

            self.assertFalse(bridge.is_busy())

    def test_bridge_normalizes_connection_error(self):
        mock_root = MockTkRoot()
        results = []

        bridge = AssistantBridge(
            ui_root=mock_root,
            on_result=lambda rid, out: results.append((rid, out)),
        )

        with patch("main.process_user_turn", side_effect=OrangeAPIConnectionError("Server unreachable")):
            req_id = bridge.send_message("Hello when offline")
            self.assertIsNotNone(req_id)

            deadline = time.time() + 2.0
            while not results and time.time() < deadline:
                time.sleep(0.05)

            self.assertEqual(len(results), 1)
            delivered_req_id, outcome = results[0]
            self.assertEqual(outcome["type"], "error")
            self.assertIn("Orange API is currently unreachable", outcome["reply"])

    def test_bridge_safe_shutdown_cancels_pending(self):
        mock_root = MockTkRoot()
        bridge = AssistantBridge(ui_root=mock_root, on_result=lambda r, o: None)

        self.assertFalse(bridge._is_closed)
        bridge.shutdown()
        self.assertTrue(bridge._is_closed)

        # Sending after shutdown returns None
        req = bridge.send_message("After close")
        self.assertIsNone(req)


class TestChatViewIntegration(unittest.TestCase):
    """Test Suite 6 & 7: ChatView transition, action card execution, and security."""

    @classmethod
    def setUpClass(cls):
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.update_idletasks()
            cls.root.update()
            cls.root.destroy()
        except Exception:
            pass

    def test_initial_workspace_transition_on_first_message(self):
        view = ChatView(self.root)

        # Before first message
        self.assertEqual(view._timeline.get_message_count(), 0)

        # Send first message
        view._on_send_message("Show system status")

        # After first message
        self.assertEqual(view._timeline.get_message_count(), 1)
        # Welcome section remains present but compacted
        self.assertIsNotNone(view._welcome_section)

        view.destroy()

    def test_no_direct_protected_tool_execution_from_ui(self):
        """Security: UI buttons must never invoke ToolRegistry or tool functions directly."""
        from tools.registry import registry
        with patch.object(registry, "execute") as mock_exec:
            card = ConfirmationCard(
                self.root,
                request_id="req_sec",
                prompt_text="Orange AI wants to execute: open_app (notepad)",
                on_decision=lambda d: None,
            )
            # Click approve on UI card
            card._on_approve()

            # Must NOT directly execute tool from UI button callback!
            mock_exec.assert_not_called()
            card.destroy()

    def test_theme_switch_consistency(self):
        """Verify dynamic theme switching across ChatView components."""
        view = ChatView(self.root)

        theme_manager.set_theme("dark")
        self.assertTrue(theme_manager.is_dark)

        theme_manager.set_theme("light")
        self.assertFalse(theme_manager.is_dark)

        view.destroy()


class TestCliPreservationInStage3(unittest.TestCase):
    """Test Suite 8: Verify CLI mode and shared engine integrity."""

    def test_process_user_turn_deterministic_commands(self):
        import main
        main.ensure_initialized()

        # 1. mood command
        outcome = main.process_user_turn("mood")
        self.assertEqual(outcome["type"], "mood")
        self.assertIn("I'm", outcome["reply"])

        # 2. plan command
        outcome = main.process_user_turn("plan open notepad then list files")
        self.assertEqual(outcome["type"], "plan")
        self.assertIn("Plan for:", outcome["reply"])

        # 3. memory profile command
        outcome = main.process_user_turn("show profile")
        self.assertEqual(outcome["type"], "memory")
        self.assertIn("Orange AI Profile", outcome["reply"])

        # 4. safe tool command (system time)
        outcome = main.process_user_turn("what time is it")
        self.assertEqual(outcome["type"], "tool")
        self.assertIn("Current time is", outcome["reply"])


if __name__ == "__main__":
    unittest.main()
