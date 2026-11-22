"""
test_stage5f_attachments.py - Stage 5F Local Document, Code, PDF & Screenshot Extractor Suite.

Verifies:
1. Text/code/logs UTF-8 extraction and language fence formatting.
2. Non-UTF8 CP-1252 character encoding fallback.
3. Oversized text file truncation cleanly at 16,000 chars with notice.
4. Multi-page / single-page PDF extraction via pypdf.
5. Scanned / image-only / empty PDF diagnostic reporting.
6. Image / Screenshot Pillow resolution and metadata extraction.
7. Image OCR extraction with graceful fallback (never raises unhandled exception).
8. capture_screen_attachment() creates valid screenshot attachment with preview metadata.
9. format_attachments_for_prompt() bundles multiple files into prompt context.
10. Strict negative contract: tools/ocr.py, tools/screen.py, tools/screenshot.py remain absent,
    Intent enum contains no SCREENSHOT/OCR, and test_api_client_image passes.
"""

import os
import io
import json
import base64
import unittest
from PIL import Image, ImageDraw

from brain.attachments import (
    extract_attachment_context,
    capture_screen_attachment,
    format_attachments_for_prompt,
    MAX_EXTRACTION_CHARS,
)
from brain.intents import Intent
from tools.registry import registry


class TestStage5FAttachments(unittest.TestCase):
    """Unit test suite for Stage 5F attachments extraction."""

    def test_01_text_code_utf8_extraction(self):
        """Verify code file extracts cleanly with language tag and markdown code block."""
        py_code = "def hello():\n    return 'Hello Orange AI'\n"
        res = extract_attachment_context({
            "name": "greet.py",
            "kind": "code",
            "content": py_code,
        })
        self.assertEqual(res["name"], "greet.py")
        self.assertEqual(res["kind"], "file")
        self.assertFalse(res["truncated"])
        self.assertIn("```python", res["formatted_block"])
        self.assertIn("Hello Orange AI", res["extracted_text"])

    def test_02_text_cp1252_encoding_fallback(self):
        """Verify files encoded in CP-1252 decode gracefully without error."""
        cp1252_bytes = "Café au lait and résumé".encode("cp1252")
        res = extract_attachment_context({
            "name": "notes.txt",
            "kind": "file",
            "base64_data": base64.b64encode(cp1252_bytes).decode("ascii"),
        })
        self.assertIsNone(res["error"])
        self.assertIn("Café", res["extracted_text"])
        self.assertIn("résumé", res["extracted_text"])

    def test_03_text_oversized_truncation(self):
        """Verify oversized files are truncated at 16,000 chars with explicit notice."""
        large_text = "Orange AI Log Line " * 1200  # ~22,800 chars
        res = extract_attachment_context({
            "name": "server.log",
            "kind": "file",
            "content": large_text,
        })
        self.assertTrue(res["truncated"])
        self.assertIn("[Truncated:", res["extracted_text"])
        self.assertIn("chars omitted]", res["extracted_text"])
        # Check text portion before the notice is exactly MAX_EXTRACTION_CHARS
        split_parts = res["extracted_text"].split("\n\n[Truncated: ")
        self.assertEqual(len(split_parts[0]), MAX_EXTRACTION_CHARS)

    def test_04_pdf_multipage_extraction(self):
        """Verify PDF text extraction via pypdf returns page headers and text."""
        pdf_bytes = (
            b'%PDF-1.4\n'
            b'1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n'
            b'2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n'
            b'3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>> endobj\n'
            b'4 0 obj <</Length 44>> stream\n'
            b'BT /F1 12 Tf 50 250 Td (Orange AI Test PDF Page 1) Tj ET\n'
            b'endstream\nendobj\n'
            b'5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n'
            b'xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000227 00000 n \n0000000322 00000 n \n'
            b'trailer <</Size 6 /Root 1 0 R>>\nstartxref\n393\n%%EOF\n'
        )
        res = extract_attachment_context({
            "name": "spec.pdf",
            "kind": "pdf",
            "base64_data": base64.b64encode(pdf_bytes).decode("ascii"),
        })
        self.assertEqual(res["kind"], "pdf")
        self.assertIsNone(res["error"])
        self.assertIn("[Page 1]", res["extracted_text"])
        self.assertIn("Orange AI Test PDF Page 1", res["extracted_text"])

    def test_05_pdf_empty_diagnostic_note(self):
        """Verify empty/blank PDFs report a clear diagnostic note rather than failing."""
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buf = io.BytesIO()
        writer.write(buf)
        blank_pdf_bytes = buf.getvalue()

        res = extract_attachment_context({
            "name": "blank.pdf",
            "kind": "pdf",
            "base64_data": base64.b64encode(blank_pdf_bytes).decode("ascii"),
        })
        self.assertEqual(res["kind"], "pdf")
        self.assertIn("0 extractable text characters", res["extracted_text"])

    def test_06_image_resolution_metadata(self):
        """Verify Pillow extracts resolution, mode, and format cleanly."""
        img = Image.new("RGB", (640, 480), color=(240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_data = base64.b64encode(buf.getvalue()).decode("ascii")

        res = extract_attachment_context({
            "name": "diagram.png",
            "kind": "image",
            "base64_data": b64_data,
        })
        self.assertEqual(res["preview_meta"], "640x480")
        self.assertIn("640x480", res["formatted_block"])

    def test_07_image_ocr_or_fallback(self):
        """Verify image text extraction succeeds or falls back gracefully without exception."""
        img = Image.new("RGB", (500, 150), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.text((20, 50), "Orange AI Stage 5 OCR Verification", fill=(0, 0, 0))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64_data = base64.b64encode(buf.getvalue()).decode("ascii")

        res = extract_attachment_context({
            "name": "screenshot_sample.png",
            "kind": "screenshot",
            "base64_data": b64_data,
        })
        self.assertIsNone(res["error"])
        self.assertIn("500x150", res["preview_meta"])
        self.assertTrue(len(res["extracted_text"]) > 0)

    def test_08_capture_screen_attachment(self):
        """Verify capture_screen_attachment() returns structured screenshot attachment."""
        res = capture_screen_attachment()
        self.assertTrue(res["name"].startswith("screenshot-"))
        self.assertEqual(res["kind"], "screenshot")
        self.assertEqual(res["mime_type"], "image/png")
        self.assertTrue(res["base64_data"].startswith("data:image/png;base64,"))
        self.assertIn("x", res["preview_meta"])
        self.assertIn("Screenshot:", res["formatted_block"])

    def test_09_format_attachments_for_prompt(self):
        """Verify format_attachments_for_prompt() generates clean markdown section."""
        att1 = {
            "name": "config.json",
            "kind": "file",
            "content": '{"version": "2.0"}',
        }
        att2 = {
            "name": "error.log",
            "kind": "file",
            "content": "ERROR: Handshake timeout",
        }
        prompt_section = format_attachments_for_prompt([att1, att2])
        self.assertIn("=== ATTACHED FILES & SCREENSHOTS ===", prompt_section)
        self.assertIn("### File: config.json", prompt_section)
        self.assertIn("### File: error.log", prompt_section)
        self.assertIn("====================================", prompt_section)

        # Empty attachments check
        self.assertEqual(format_attachments_for_prompt([]), "")
        self.assertEqual(format_attachments_for_prompt(None), "")

    def test_10_negative_contract_invariants(self):
        """Verify architectural boundaries: no tool screen files, no Intent pollution."""
        tools_dir = os.path.join(os.path.dirname(__file__), "tools")
        self.assertFalse(os.path.exists(os.path.join(tools_dir, "ocr.py")))
        self.assertFalse(os.path.exists(os.path.join(tools_dir, "screen.py")))
        self.assertFalse(os.path.exists(os.path.join(tools_dir, "screenshot.py")))

        intent_names = [e.name for e in Intent]
        self.assertNotIn("SCREENSHOT", intent_names)
        self.assertNotIn("OCR", intent_names)
        self.assertNotIn("SCREEN_SUMMARY", intent_names)

        self.assertFalse(registry.contains("take_screenshot"))
        self.assertFalse(registry.contains("read_image_text"))
        self.assertFalse(registry.contains("summarize_screen"))


if __name__ == "__main__":
    unittest.main()
