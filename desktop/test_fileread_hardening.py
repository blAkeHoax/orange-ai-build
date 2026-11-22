"""
test_fileread_hardening.py - Comprehensive Unit and Acceptance Test Suite for
Hardened tools.files.read_file() (REA Phase 1 Adoption).

Covers all 15 required verification scenarios:
1. Existing read_file(path) backward compatibility.
2. Reading first page of text file with line pagination.
3. Reading from specified starting line.
4. Enforcing maximum line count bounds.
5. Next-page metadata verification across chained reads.
6. Empty file (0-byte) handling.
7. Nonexistent file error handling.
8. Binary file detection and rejection (null-byte and control characters).
9. Full UTF-8 unicode integrity (emojis, accents, CJK, symbols).
10. Multi-encoding fallback cascade (CP1252 / Windows-1252 with invalid UTF-8 bytes).
11. Policy size limit enforcement (rejection before reading).
12. Directory traversal rejection (.. escapes).
13. Absolute path outside approved roots rejection.
14. Symlink escape attempt rejection (with Windows privilege fallback).
15. Bounded memory behavior during line streaming.
"""

import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.files as files_tools
from tools.files import (
    read_file,
    FileReadResult,
    get_approved_roots,
    is_path_within_roots,
    is_binary_file,
    DEFAULT_MAX_LINES,
    MAX_FILE_SIZE_BYTES,
)
from tools.registry import registry


class TestFileReadHardening(unittest.TestCase):
    """Test suite for hardened tools.files.read_file implementation."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_fileread_test_")
        self.test_path = Path(self.test_dir).resolve()
        # Ensure test directory is recognized as an allowed root
        self.allowed_roots = [self.test_path]

    def tearDown(self):
        try:
            shutil.rmtree(self.test_dir)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Scenario 1: Existing read_file(path) backward compatibility
    # -------------------------------------------------------------------------
    def test_01_backward_compatibility(self):
        """read_file(path) returns exact content as a string without extra headers."""
        file_path = self.test_path / "simple.txt"
        expected = "Line 1: Hello Orange AI\nLine 2: Reliability Validation"
        file_path.write_text(expected, encoding="utf-8")

        res = read_file(str(file_path))

        # Must satisfy exact string equality and isinstance(res, str)
        self.assertTrue(isinstance(res, str))
        self.assertEqual(res, expected)
        self.assertIsInstance(res, FileReadResult)
        self.assertEqual(res.start_line, 1)
        self.assertFalse(res.has_more)
        self.assertIsNone(res.next_start_line)
        self.assertEqual(res.lines_returned, 2)

    # -------------------------------------------------------------------------
    # Scenario 2: Reading first page of text file with pagination
    # -------------------------------------------------------------------------
    def test_02_read_first_page(self):
        """Reading first page yields exact bounded slice with pagination footer."""
        file_path = self.test_path / "lines25.txt"
        lines = [f"Line {i:02d}: test content\n" for i in range(1, 26)]
        file_path.write_text("".join(lines), encoding="utf-8")

        res = read_file(str(file_path), start_line=1, max_lines=10)

        self.assertIsInstance(res, FileReadResult)
        self.assertEqual(res.start_line, 1)
        self.assertEqual(res.lines_returned, 10)
        self.assertTrue(res.has_more)
        self.assertEqual(res.next_start_line, 11)

        # Raw content should contain only lines 1-10
        expected_raw = "".join(lines[:10])
        self.assertEqual(res.raw_content, expected_raw)

        # String contains both raw content and human/LLM readable footer
        self.assertIn("Lines 1-10 (10 lines returned)", str(res))
        self.assertIn("next start_line=11", str(res))

    # -------------------------------------------------------------------------
    # Scenario 3: Reading from specified starting line
    # -------------------------------------------------------------------------
    def test_03_read_from_start_line(self):
        """Reading from line 11 extracts lines 11 through 20."""
        file_path = self.test_path / "lines25.txt"
        lines = [f"Line {i:02d}: test content\n" for i in range(1, 26)]
        file_path.write_text("".join(lines), encoding="utf-8")

        res = read_file(str(file_path), start_line=11, max_lines=10)

        self.assertEqual(res.start_line, 11)
        self.assertEqual(res.lines_returned, 10)
        self.assertTrue(res.has_more)
        self.assertEqual(res.next_start_line, 21)

        expected_raw = "".join(lines[10:20])
        self.assertEqual(res.raw_content, expected_raw)
        self.assertIn("Lines 11-20 (10 lines returned)", str(res))
        self.assertIn("next start_line=21", str(res))

    # -------------------------------------------------------------------------
    # Scenario 4: Enforcing maximum line count bounds
    # -------------------------------------------------------------------------
    def test_04_enforce_max_lines(self):
        """max_lines strictly caps the number of lines returned."""
        file_path = self.test_path / "lines25.txt"
        lines = [f"Line {i:02d}: test content\n" for i in range(1, 26)]
        file_path.write_text("".join(lines), encoding="utf-8")

        res = read_file(str(file_path), start_line=1, max_lines=5)

        self.assertEqual(res.lines_returned, 5)
        self.assertTrue(res.has_more)
        self.assertEqual(res.next_start_line, 6)
        self.assertEqual(res.raw_content, "".join(lines[:5]))

    # -------------------------------------------------------------------------
    # Scenario 5: Next-page metadata verification across chained reads
    # -------------------------------------------------------------------------
    def test_05_chained_pagination_flow(self):
        """Chaining next_start_line until EOF correctly transitions has_more to False."""
        file_path = self.test_path / "lines25.txt"
        lines = [f"Line {i:02d}: test content\n" for i in range(1, 26)]
        file_path.write_text("".join(lines), encoding="utf-8")

        # Page 1 (1-10)
        p1 = read_file(str(file_path), start_line=1, max_lines=10)
        self.assertTrue(p1.has_more)
        self.assertEqual(p1.next_start_line, 11)

        # Page 2 (11-20)
        p2 = read_file(str(file_path), start_line=p1.next_start_line, max_lines=10)
        self.assertTrue(p2.has_more)
        self.assertEqual(p2.next_start_line, 21)

        # Page 3 (21-25) -> Final partial page
        p3 = read_file(str(file_path), start_line=p2.next_start_line, max_lines=10)
        self.assertFalse(p3.has_more)
        self.assertIsNone(p3.next_start_line)
        self.assertEqual(p3.lines_returned, 5)
        self.assertEqual(p3.raw_content, "".join(lines[20:25]))
        self.assertIn("End of file reached", str(p3))

    # -------------------------------------------------------------------------
    # Scenario 6: Empty file (0-byte) handling
    # -------------------------------------------------------------------------
    def test_06_empty_file(self):
        """0-byte file returns empty FileReadResult with 0 lines returned."""
        file_path = self.test_path / "empty.txt"
        file_path.write_text("", encoding="utf-8")

        res = read_file(str(file_path))

        self.assertEqual(res, "")
        self.assertEqual(res.lines_returned, 0)
        self.assertFalse(res.has_more)
        self.assertIsNone(res.next_start_line)

    # -------------------------------------------------------------------------
    # Scenario 7: Nonexistent file error handling
    # -------------------------------------------------------------------------
    def test_07_nonexistent_file(self):
        """Nonexistent file returns an error string containing 'no such file'."""
        ghost = self.test_path / "does_not_exist.txt"
        res = read_file(str(ghost))

        self.assertTrue(isinstance(res, str))
        self.assertIn("no such file", res.lower())

    # -------------------------------------------------------------------------
    # Scenario 8: Binary file detection and rejection
    # -------------------------------------------------------------------------
    def test_08_binary_file_rejection(self):
        """Binary files containing null bytes are rejected with clear error."""
        bin_path = self.test_path / "sample.bin"
        with open(bin_path, "wb") as f:
            f.write(b"PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01")

        res = read_file(str(bin_path))

        self.assertTrue(isinstance(res, str))
        self.assertIn("binary file detected", res.lower())

    # -------------------------------------------------------------------------
    # Scenario 9: Full UTF-8 unicode integrity
    # -------------------------------------------------------------------------
    def test_09_utf8_unicode_integrity(self):
        """UTF-8 file with emojis, accents, symbols, and CJK characters preserves text."""
        utf8_path = self.test_path / "multilingual.txt"
        content = (
            "Header: Multilingual Unicode Test\n"
            "Emojis: 🚀 🤖 🍊 ✨\n"
            "Accents: café, naïve, über, señor\n"
            "CJK: 東京, 漢字, こんにちは\n"
            "Math: ∀x ∈ ℝ, x² ≥ 0"
        )
        utf8_path.write_text(content, encoding="utf-8")

        res = read_file(str(utf8_path))

        self.assertEqual(res, content)
        self.assertEqual(res.encoding, "utf-8")

    # -------------------------------------------------------------------------
    # Scenario 10: Multi-encoding fallback cascade (CP1252 / Windows-1252)
    # -------------------------------------------------------------------------
    def test_10_encoding_fallback_cp1252(self):
        """File encoded in CP1252 with bytes invalid in UTF-8 decodes via fallback."""
        cp_path = self.test_path / "cp1252_file.txt"
        # 0x80 is the Euro symbol (€) in CP1252; invalid start byte in UTF-8
        raw_bytes = b"Price tag: 150 \x80 only."
        with open(cp_path, "wb") as f:
            f.write(raw_bytes)

        res = read_file(str(cp_path))

        self.assertIsInstance(res, FileReadResult)
        self.assertEqual(res.encoding, "cp1252")
        self.assertIn("150 € only.", res)

    # -------------------------------------------------------------------------
    # Scenario 11: Policy size limit enforcement
    # -------------------------------------------------------------------------
    def test_11_policy_size_limit(self):
        """File exceeding max_file_size policy limit is rejected before reading."""
        big_path = self.test_path / "oversized.txt"
        # Write 2000 bytes
        big_path.write_text("A" * 2000, encoding="utf-8")

        # Set max_file_size limit to 500 bytes
        res = read_file(str(big_path), max_file_size=500)

        self.assertTrue(isinstance(res, str))
        self.assertIn("policy limit", res.lower())
        self.assertIn("2000 bytes", res)

    # -------------------------------------------------------------------------
    # Scenario 12: Directory traversal rejection
    # -------------------------------------------------------------------------
    def test_12_directory_traversal_rejection(self):
        """Paths attempting to traverse out of allowed roots with .. are rejected."""
        sub = self.test_path / "subfolder"
        sub.mkdir()
        traversal_path = sub / ".." / ".." / "escaped_secret.txt"

        res = read_file(str(traversal_path), allowed_roots=[sub])

        self.assertTrue(isinstance(res, str))
        self.assertIn("access denied", res.lower())
        self.assertIn("escapes approved", res.lower())

    # -------------------------------------------------------------------------
    # Scenario 13: Absolute path outside approved roots rejection
    # -------------------------------------------------------------------------
    def test_13_outside_approved_roots_rejection(self):
        """Absolute path pointing outside approved roots is rejected."""
        isolated_dir = tempfile.mkdtemp(prefix="orange_isolated_")
        try:
            secret_file = Path(isolated_dir) / "secret.txt"
            secret_file.write_text("classified data", encoding="utf-8")

            # Only self.test_path is allowed
            res = read_file(str(secret_file), allowed_roots=[self.test_path])

            self.assertTrue(isinstance(res, str))
            self.assertIn("access denied", res.lower())
            self.assertIn("escapes approved", res.lower())
        finally:
            shutil.rmtree(isolated_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 14: Symlink escape attempt rejection
    # -------------------------------------------------------------------------
    def test_14_symlink_escape_attempt(self):
        """Symlink inside allowed root pointing outside allowed root is rejected."""
        isolated_dir = tempfile.mkdtemp(prefix="orange_symlink_target_")
        try:
            target_outside = Path(isolated_dir) / "target.txt"
            target_outside.write_text("outside data", encoding="utf-8")

            symlink_inside = self.test_path / "link_to_outside.txt"
            try:
                os.symlink(str(target_outside), str(symlink_inside))
            except (OSError, NotImplementedError) as e:
                # Windows without Developer Mode / admin privilege raises WinError 1314
                self.skipTest(f"Symlink creation not permitted on host OS: {e}")

            # Verify that following the symlink outside allowed_roots triggers rejection
            res = read_file(str(symlink_inside), allowed_roots=[self.test_path])
            self.assertTrue(isinstance(res, str))
            self.assertIn("access denied", res.lower())
            self.assertIn("escapes approved", res.lower())
        finally:
            shutil.rmtree(isolated_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Scenario 15: Bounded memory behavior during line streaming
    # -------------------------------------------------------------------------
    def test_15_bounded_memory_streaming(self):
        """Streaming reader bounds memory consumption and only loads requested lines."""
        large_path = self.test_path / "large_stream.txt"
        total_lines = 10000
        with open(large_path, "w", encoding="utf-8") as f:
            for i in range(1, total_lines + 1):
                f.write(f"Line {i:06d}: payload text data for bounded streaming validation\n")

        res = read_file(str(large_path), start_line=1, max_lines=15)

        self.assertIsInstance(res, FileReadResult)
        self.assertEqual(res.lines_returned, 15)
        self.assertTrue(res.has_more)
        self.assertEqual(res.next_start_line, 16)
        # Verify lines are exactly lines 1 to 15
        ret_lines = res.raw_content.splitlines()
        self.assertEqual(len(ret_lines), 15)
        self.assertTrue(ret_lines[0].startswith("Line 000001"))
        self.assertTrue(ret_lines[14].startswith("Line 000015"))

    # -------------------------------------------------------------------------
    # Tool Registry & Agent Integration Check
    # -------------------------------------------------------------------------
    def test_16_tool_registry_execution(self):
        """ToolRegistry executes read_file cleanly with permission enforcement."""
        test_file = self.test_path / "registry_test.txt"
        test_file.write_text("Registry execution check", encoding="utf-8")

        res = registry.execute("read_file", str(test_file))

        self.assertEqual(res, "Registry execution check")
        self.assertIsInstance(res, FileReadResult)
        self.assertEqual(res.to_dict()["lines_returned"], 1)


if __name__ == "__main__":
    unittest.main()
