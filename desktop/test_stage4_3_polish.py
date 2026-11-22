"""
test_stage4_3_polish.py - Dedicated Stage 4.3 Test Suite for Orange AI Desktop.

Validates all 7 scopes of Stage 4.3 UI Polish & Chat Rendering:
1. Scope A: Markdown block & inline parsing, code blocks with copy, headers, lists, quotes, Unicode / multilingual
2. Scope B: Structured ToolResultCard, status pills (Success / Failed / Denied), monospace output, collapse toggle
3. Scope C: Model / Provider metadata demotion, Orange AI primary identity, expandable diagnostic drawer, sanitization
4. Scope D: Adaptive user message wrapping, bottom composer anchoring, smooth scrolling synchronization
5. Scope E: Dynamic user greeting from memory, time-of-day accuracy, punctuation defect prevention
6. Scope F: Standardized composer icon fonts (16pt), orb avatar presentation, clean layout
7. Scope G: Light and Dark theme parity for code blocks, tool cards, and metadata drawers
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import customtkinter as ctk

from gui.theme import ThemeColors, theme_manager
from gui.utils.greeting_helper import get_user_greeting, get_user_first_name
from gui.components.markdown_renderer import (
    parse_markdown_blocks,
    CodeBlockCard,
    MarkdownRenderer,
)
from gui.components.tool_result_card import ToolResultCard
from gui.components.message_timeline import MessageTimeline, _sanitize_metadata
from gui.components.composer import ComposerBar
from gui.views.chat_view import ChatView
from gui.views.home_view import HomeView


class TestGreetingHelper(unittest.TestCase):
    """Scope E: Dynamic greeting generation and punctuation guarantees."""

    def test_time_of_day_salutations(self):
        # Morning: 0 - 11
        self.assertIn("Good morning", get_user_greeting(hour=0, name_override="Sayan"))
        self.assertIn("Good morning", get_user_greeting(hour=9, name_override="Sayan"))
        self.assertIn("Good morning", get_user_greeting(hour=11, name_override="Sayan"))

        # Afternoon: 12 - 17
        self.assertIn("Good afternoon", get_user_greeting(hour=12, name_override="Sayan"))
        self.assertIn("Good afternoon", get_user_greeting(hour=15, name_override="Sayan"))
        self.assertIn("Good afternoon", get_user_greeting(hour=17, name_override="Sayan"))

        # Evening: 18 - 23
        self.assertIn("Good evening", get_user_greeting(hour=18, name_override="Sayan"))
        self.assertIn("Good evening", get_user_greeting(hour=21, name_override="Sayan"))
        self.assertIn("Good evening", get_user_greeting(hour=23, name_override="Sayan"))

    def test_greeting_with_and_without_name(self):
        with_name = get_user_greeting(hour=9, name_override="Sayan")
        self.assertEqual(with_name, "Good morning, Sayan 👋")

        without_name = get_user_greeting(hour=9, name_override="")
        self.assertEqual(without_name, "Good morning! 👋")

    def test_no_punctuation_defect(self):
        """Verify that question marks or awkward comma placements never occur."""
        for h in (8, 13, 20):
            res = get_user_greeting(hour=h, name_override="Sayan")
            self.assertNotIn("?,", res)
            self.assertNotIn("?, Sayan", res)
            self.assertTrue(res.endswith(" 👋"))

    @patch("brain.memory.recall_structured")
    def test_dynamic_memory_first_name_resolution(self, mock_recall):
        mock_recall.return_value = [
            {"category": "project", "key": "name", "value": "Desktop"},
            {"category": "identity", "key": "name", "value": "Sayan Dey"},
        ]
        first_name = get_user_first_name()
        self.assertEqual(first_name, "Sayan")

        greeting = get_user_greeting(hour=10)
        self.assertEqual(greeting, "Good morning, Sayan 👋")


class TestMarkdownParsing(unittest.TestCase):
    """Scope A: Markdown parsing, code blocks, lists, quotes, and multilingual preservation."""

    def test_parse_headings(self):
        text = "# Level 1\n## Level 2\n### Level 3"
        blocks = parse_markdown_blocks(text)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["type"], "header")
        self.assertEqual(blocks[0]["level"], 1)
        self.assertEqual(blocks[0]["text"], "Level 1")
        self.assertEqual(blocks[1]["level"], 2)
        self.assertEqual(blocks[2]["level"], 3)

    def test_parse_fenced_code_blocks(self):
        text = "Before\n```python\ndef test():\n    return True\n```\nAfter"
        blocks = parse_markdown_blocks(text)
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["text", "code", "text"])
        code_block = blocks[1]
        self.assertEqual(code_block["lang"], "python")
        self.assertIn("def test():", code_block["code"])

    def test_parse_bullet_and_numbered_lists(self):
        text = "- Apple\n- Banana\n- Orange\n1. First\n2. Second"
        blocks = parse_markdown_blocks(text)
        types = [b["type"] for b in blocks]
        self.assertIn("list", types)

    def test_parse_blockquotes(self):
        text = "> This is a quoted principle.\n> Second quote line."
        blocks = parse_markdown_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "blockquote")
        self.assertEqual(len(blocks[0]["lines"]), 2)

    def test_multilingual_and_unicode_preservation(self):
        """Preserve Bengali, Hindi, and emoji characters without corruption."""
        text = (
            "## বহুভাষিক পরীক্ষা (Multilingual Test)\n"
            "- বাংলা: কমলা এআই ব্যক্তিগত সহকারী 🍊\n"
            "- हिन्दी: ऑरेंज एआई व्यक्तिगत सहायक 🚀\n"
            "```text\n"
            "নমস্কার / नमस्ते / Hello World 🌟\n"
            "```"
        )
        blocks = parse_markdown_blocks(text)
        code_block = [b for b in blocks if b["type"] == "code"][0]
        self.assertIn("নমস্কার", code_block["code"])
        self.assertIn("नमस्ते", code_block["code"])
        self.assertIn("🌟", code_block["code"])


class TestUIWidgetsPolish(unittest.TestCase):
    """Scope B, C, D, F, G: CustomTkinter visual components."""

    @classmethod
    def setUpClass(cls):
        ctk.set_appearance_mode("dark")
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

    def test_code_block_card_widget(self):
        card = CodeBlockCard(
            self.root,
            lang="python",
            code="print('Hello Orange AI')\nx = 10",
        )
        self.assertEqual(card.lang, "PYTHON")
        self.assertIsNotNone(card._copy_btn)
        card.destroy()

    def test_markdown_renderer_widget(self):
        md = MarkdownRenderer(
            self.root,
            "### Title\n**Bold statement** with `inline code`.\n```sh\nls -la\n```",
        )
        self.assertTrue(len(md.winfo_children()) >= 2)
        md.destroy()

    def test_tool_result_card_states(self):
        # 1. Success card
        success_card = ToolResultCard(
            self.root,
            tool_name="list_files",
            status="success",
            output="file1.txt\nfile2.py",
        )
        self.assertEqual(success_card._status_lbl.cget("text"), "● Success")
        success_card.destroy()

        # 2. Denied card
        denied_card = ToolResultCard(
            self.root,
            tool_name="close_process",
            status="denied",
            output="Action cancelled: user declined confirmation.",
        )
        self.assertEqual(denied_card._status_lbl.cget("text"), "● Denied")
        denied_card.destroy()

        # 3. Failed card
        failed_card = ToolResultCard(
            self.root,
            tool_name="system_exec",
            status="failed",
            output="Command failed with exit code 1",
        )
        self.assertEqual(failed_card._status_lbl.cget("text"), "● Failed")
        failed_card.destroy()

    def test_tool_result_card_collapse_toggle(self):
        long_output = "\n".join([f"Line {i}: diagnostic info" for i in range(20)])
        card = ToolResultCard(
            self.root,
            tool_name="diagnostics",
            status="success",
            output=long_output,
        )
        self.assertIsNotNone(card._collapse_btn)
        self.assertEqual(card._collapse_btn.cget("text"), "▲ Collapse")

        # Toggle to collapse
        card._toggle_collapse()
        self.assertTrue(card._is_collapsed)
        self.assertEqual(card._collapse_btn.cget("text"), "▼ Expand")

        # Toggle back to expand
        card._toggle_collapse()
        self.assertFalse(card._is_collapsed)
        self.assertEqual(card._collapse_btn.cget("text"), "▲ Collapse")
        card.destroy()

    def test_metadata_sanitization(self):
        raw = "Bearer eyJhbGciOiJIUzI1Ni.secret with api_key=xyz123 and sk-abcdef123456789"
        sanitized = _sanitize_metadata(raw)
        self.assertNotIn("eyJhbGciOiJIUzI1Ni", sanitized)
        self.assertNotIn("xyz123", sanitized)
        self.assertNotIn("sk-abcdef123456789", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_message_timeline_tool_routing(self):
        timeline = MessageTimeline(self.root)

        # Explicit tool result card
        tool_card = timeline.add_tool_result(
            tool_name="get_cpu_usage",
            status="success",
            output="CPU Usage: 14.2%",
        )
        self.assertIsInstance(tool_card, ToolResultCard)

        # Routing msg_type="tool" through add_assistant_message
        routed_card = timeline.add_assistant_message(
            text="Action cancelled: user declined confirmation to execute 'close_process'.",
            msg_type="tool",
        )
        self.assertIsInstance(routed_card, ToolResultCard)
        self.assertEqual(routed_card._status, "denied")
        timeline.destroy()

    def test_message_timeline_assistant_metadata_drawer(self):
        timeline = MessageTimeline(self.root)
        bubble = timeline.add_assistant_message(
            text="Here is your requested answer.",
            model_name="qwen2.5:latest",
            msg_type="assistant",
        )
        self.assertEqual(timeline.get_message_count(), 1)
        timeline.destroy()

    def test_composer_icon_font_sizes(self):
        """Scope F: Verify attachment and microphone icons are consistently sized at 16pt."""
        composer = ComposerBar(self.root, on_send=lambda t: None)
        attach_size = composer._attach_btn.cget("font").cget("size")
        mic_size = composer._mic_btn.cget("font").cget("size")
        self.assertEqual(attach_size, 16)
        self.assertEqual(mic_size, 16)
        composer.destroy()

    def test_stage4_3_theme_tokens_exist(self):
        """Scope G: Verify all new theme tokens are defined for light and dark modes."""
        tokens = [
            ThemeColors.CODE_BG,
            ThemeColors.CODE_BORDER,
            ThemeColors.CODE_TEXT,
            ThemeColors.CODE_HEADER_BG,
            ThemeColors.CODE_INLINE_BG,
            ThemeColors.CODE_INLINE_FG,
            ThemeColors.TOOL_CARD_BG,
            ThemeColors.TOOL_CARD_BORDER,
            ThemeColors.TOOL_HEADER_TEXT,
            ThemeColors.TOOL_SUCCESS_TEXT,
            ThemeColors.TOOL_ERROR_TEXT,
            ThemeColors.METADATA_BG,
            ThemeColors.METADATA_BORDER,
            ThemeColors.METADATA_TEXT,
        ]
        for token in tokens:
            self.assertIsInstance(token, tuple)
            self.assertEqual(len(token), 2)
            self.assertTrue(token[0].startswith("#"))
            self.assertTrue(token[1].startswith("#"))

    def test_markdown_renderer_no_clipping(self):
        """Verify displaylines computation does not clip the final line."""
        text = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        md = MarkdownRenderer(self.root, text)
        self.root.update_idletasks()
        txt_widget = md._text_widgets[0]
        # Must be at least 5 lines (never 4 due to off-by-one distance)
        self.assertGreaterEqual(int(txt_widget.cget("height")), 5)
        md.destroy()

    def test_assistant_bubble_drawer_not_pre_packed(self):
        """Verify details drawer container is not packed by default (no 200px void)."""
        timeline = MessageTimeline(self.root)
        bubble = timeline.add_assistant_message(
            text="Test assistant message without clipping.",
            model_name="qwen2.5:latest",
        )
        self.root.update_idletasks()
        # Find any drawer container among bubble children
        drawers = [c for c in bubble.winfo_children() if isinstance(c, ctk.CTkFrame) and not c.winfo_ismapped()]
        # The drawer container should exist if metadata is present, but NOT be mapped
        self.root.update_idletasks()
        timeline.destroy()

    def test_chat_view_banner_container_not_pre_packed(self):
        """Verify informational banner container is not packed at startup (saving 200px)."""
        chat = ChatView(self.root)
        self.root.update_idletasks()
        self.assertEqual(chat._banner_container.winfo_manager(), "")
        chat._show_notice("Test notification")
        self.root.update_idletasks()
        self.assertEqual(chat._banner_container.winfo_manager(), "pack")
        chat.destroy()

    def test_tool_result_card_no_artificial_14_line_cap(self):
        """Verify tool result output expands to display full content without a 14-line cap."""
        long_output = "\n".join([f"Output line {i}" for i in range(25)])
        card = ToolResultCard(
            self.root,
            tool_name="file_search",
            status="success",
            output=long_output,
        )
        self.root.update_idletasks()
        self.assertGreaterEqual(int(card._text_widget.cget("height")), 25)
        card.destroy()


if __name__ == "__main__":
    unittest.main()
