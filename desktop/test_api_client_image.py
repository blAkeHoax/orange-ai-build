"""
test_api_client_image.py - Negative Regression & Text-Only Contract Suite for Orange AI Desktop.

Verifies:
1. OrangeAPIClient serializes text-only assist requests correctly.
2. The serialized HTTP request body never contains an 'image' field.
3. Passing image attachments to assist() raises TypeError immediately.
4. Image pre-flight validation and MIME detection functions are completely absent.
5. Screenshot, OCR, and screen summarization tools are absent from the tool registry.
6. Screen-related tool files (ocr.py, screen.py, screenshot.py) are absent from disk.
7. Intent enum contains no SCREENSHOT, OCR, or SCREEN_SUMMARY intents.
8. Screen queries deterministically fall back to general CHAT.
"""

import os
import json
import unittest
from unittest.mock import patch, MagicMock

import brain.api_client as api_client_mod
from brain.api_client import OrangeAPIClient, OrangeAPIConnectionError, OrangeAPIResponseError
from brain.intents import Intent
from brain.router import route
from tools.registry import registry


class TestDesktopTextOnlyContract(unittest.TestCase):
    """Test suite verifying Desktop client adheres strictly to the text-only contract."""

    def setUp(self):
        self.client = OrangeAPIClient(api_url="http://mock-api.test")

    def _mock_http_response(self, status=200, json_body=None):
        """Helper to create a mock HTTP response context manager."""
        body = json.dumps(json_body or {"reply": "Text response.", "model": "openai/gpt-oss-20b", "success": True}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.read.return_value = body
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        return mock_resp

    # -------------------------------------------------------------------------
    # 1. Text-Only Request Serialization
    # -------------------------------------------------------------------------
    def test_text_only_request_serialization(self):
        """Verify standard text requests serialize cleanly without any image field."""
        with patch("urllib.request.urlopen", return_value=self._mock_http_response()) as mock_urlopen:
            resp = self.client.assist(
                message="Explain gravity",
                history=[{"role": "user", "content": "hello"}],
                profile="User is a physicist",
                system_instruction="Be concise",
                provider="nvidia",
                model="gpt_oss_20b",
                task_type="conversational",
                modalities=["text"],
            )
            self.assertTrue(resp["success"])
            self.assertEqual(resp["reply"], "Text response.")

            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode("utf-8"))

            self.assertEqual(payload["message"], "Explain gravity")
            self.assertEqual(payload["history"], [{"role": "user", "content": "hello"}])
            self.assertEqual(payload["profile"], "User is a physicist")
            self.assertEqual(payload["system_instruction"], "Be concise")
            self.assertEqual(payload["provider"], "nvidia")
            self.assertEqual(payload["model"], "gpt_oss_20b")
            self.assertEqual(payload["task_type"], "conversational")
            self.assertEqual(payload["modalities"], ["text"])
            self.assertNotIn("image", payload, "Payload must never contain an 'image' field.")

    # -------------------------------------------------------------------------
    # 2. Rejection of Image Attachments
    # -------------------------------------------------------------------------
    def test_assist_rejects_image_argument(self):
        """Calling assist(image=...) raises TypeError immediately."""
        with self.assertRaises(TypeError) as ctx:
            self.client.assist(message="Analyze this", image=b"\x89PNG\r\n\x1a\n")
        self.assertIn("Image attachments are not supported", str(ctx.exception))

    def test_assist_rejects_image_dict_argument(self):
        """Calling assist(image={'b64_json': '...'}) raises TypeError immediately."""
        with self.assertRaises(TypeError) as ctx:
            self.client.assist(message="Analyze this", image={"b64_json": "AAAA"})
        self.assertIn("Image attachments are not supported", str(ctx.exception))

    def test_assist_rejects_image_string_argument(self):
        """Calling assist(image='data:image/png;base64,...') raises TypeError immediately."""
        with self.assertRaises(TypeError) as ctx:
            self.client.assist(message="Analyze this", image="data:image/png;base64,AAAA")
        self.assertIn("Image attachments are not supported", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 3. Absence of Image Helpers in api_client
    # -------------------------------------------------------------------------
    def test_image_helpers_absent_from_api_client(self):
        """Verify detect_image_mime and prepare_image_attachment do not exist in api_client."""
        self.assertFalse(hasattr(api_client_mod, "detect_image_mime"))
        self.assertFalse(hasattr(api_client_mod, "prepare_image_attachment"))
        self.assertFalse(hasattr(api_client_mod, "_SUPPORTED_IMAGE_MIMES"))
        self.assertFalse(hasattr(api_client_mod, "_MAX_IMAGE_SIZE_BYTES"))
        self.assertFalse(hasattr(self.client, "prepare_image"))

    # -------------------------------------------------------------------------
    # 4. Absence of Screen Tools and Intent Decommissioning
    # -------------------------------------------------------------------------
    def test_screen_tools_absent_from_registry(self):
        """Verify take_screenshot, read_image_text, and summarize_screen are not registered."""
        self.assertFalse(registry.contains("take_screenshot"))
        self.assertFalse(registry.contains("read_image_text"))
        self.assertFalse(registry.contains("summarize_screen"))

    def test_screen_tool_files_deleted_from_disk(self):
        """Verify ocr.py, screen.py, and screenshot.py do not exist in tools directory."""
        tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        self.assertFalse(os.path.exists(os.path.join(tools_dir, "ocr.py")))
        self.assertFalse(os.path.exists(os.path.join(tools_dir, "screen.py")))
        self.assertFalse(os.path.exists(os.path.join(tools_dir, "screenshot.py")))

    def test_screen_intents_absent_from_enum(self):
        """Verify SCREENSHOT, OCR, and SCREEN_SUMMARY are absent from Intent enum."""
        intent_names = [e.name for e in Intent]
        self.assertNotIn("SCREENSHOT", intent_names)
        self.assertNotIn("OCR", intent_names)
        self.assertNotIn("SCREEN_SUMMARY", intent_names)

    def test_screen_queries_route_to_chat(self):
        """Former screenshot and OCR queries now fall back cleanly to Intent.CHAT."""
        self.assertEqual(route("take screenshot"), Intent.CHAT)
        self.assertEqual(route("capture screen"), Intent.CHAT)
        self.assertEqual(route("read screen text"), Intent.CHAT)
        self.assertEqual(route("summarize screen"), Intent.CHAT)


if __name__ == "__main__":
    unittest.main()
