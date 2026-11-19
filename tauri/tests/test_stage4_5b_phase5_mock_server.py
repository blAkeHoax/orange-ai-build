"""
test_stage4_5b_phase5_mock_server.py - Tests for Orange AI Mock Update Server.
Stage 4.5B Phase 5: Mock Server & Interruption Testing.

Validates that the controlled local mock update server correctly reproduces
all required operational scenarios:
1. Valid update metadata with platforms and signatures.
2. No-update responses (matching current version).
3. Malformed JSON metadata.
4. Missing fields in metadata.
5. Invalid version strings (non-semver).
6. Complete and successful update archive download.
7. Download interruption via abrupt socket termination.
8. HTTP 404, 500, and 503 error codes.
9. Connection timeout / hung response simulation.
10. Truncated archive delivery (mismatched Content-Length).
11. Corrupt archive payload delivery.
12. Delayed responses.
13. Retry behavior (transient failures followed by success).
14. Request history tracking and path counter verification.
15. Strict isolation: localhost only, zero modification to public repository.
"""

import os
import sys
import json
import time
import socket
import zipfile
import io
import http.client
import urllib.request
import urllib.error
import unittest
from pathlib import Path

# Add tests directory to path
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from mock_update_server import MockUpdateServer, create_dummy_update_zip


class TestStage4_5BPhase5MockServer(unittest.TestCase):
    """Unit tests for the controlled local mock update server."""

    @classmethod
    def setUpClass(cls):
        cls.server = MockUpdateServer(host="127.0.0.1", port=0)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()

    def test_01_health_and_ping_endpoint(self):
        """Verify the mock server health endpoint responds with 200 OK."""
        url = f"{self.server.base_url}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("status"), "healthy")

    def test_02_valid_update_metadata(self):
        """Verify the mock server produces compliant Tauri 2 update metadata."""
        self.server.set_metadata_mode("valid", target_version="2.0.1")
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("version"), "2.0.1")
            self.assertIn("platforms", data)
            self.assertIn("windows-x86_64", data["platforms"])
            win_plat = data["platforms"]["windows-x86_64"]
            self.assertIn("url", win_plat)
            self.assertIn("signature", win_plat)

    def test_03_no_update_response(self):
        """Verify the mock server returns version matching current version on no-update mode."""
        self.server.set_metadata_mode("no_update")
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("version"), "2.0.0")
            self.assertEqual(data.get("platforms"), {})

    def test_04_malformed_metadata_syntax_error(self):
        """Verify that malformed metadata causes JSON decoding to fail."""
        self.server.set_metadata_mode("malformed")
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            raw = resp.read()
            with self.assertRaises(json.JSONDecodeError):
                json.loads(raw.decode())

    def test_05_missing_fields_metadata(self):
        """Verify that missing_fields mode omits 'platforms' field."""
        self.server.set_metadata_mode("missing_fields")
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("version"), "2.0.1")
            self.assertNotIn("platforms", data)

    def test_06_invalid_version_metadata(self):
        """Verify that invalid_version mode returns a non-semver string."""
        self.server.set_metadata_mode("invalid_version")
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("version"), "not-a-valid-semver-string")

    def test_07_http_errors_404_500_503(self):
        """Verify that the mock server reliably emits HTTP 404, 500, and 503 errors."""
        for error_mode, expected_code in [("http_404", 404), ("http_500", 500), ("http_503", 503)]:
            self.server.set_metadata_mode(error_mode)
            req = urllib.request.Request(self.server.latest_url)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=3.0)
            self.assertEqual(ctx.exception.code, expected_code)

    def test_08_download_success_and_valid_zip(self):
        """Verify that download mode streams a valid zip archive with expected files."""
        self.server.set_metadata_mode("valid", target_version="2.0.1")
        self.server.set_download_mode("success")

        # Query metadata
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            meta = json.loads(resp.read().decode())
        download_url = meta["platforms"]["windows-x86_64"]["url"]

        # Download update archive
        req_dl = urllib.request.Request(download_url)
        with urllib.request.urlopen(req_dl, timeout=3.0) as resp_dl:
            self.assertEqual(resp_dl.status, 200)
            content_length = int(resp_dl.headers.get("Content-Length", 0))
            payload = resp_dl.read()
            self.assertEqual(len(payload), content_length)

            # Inspect zip contents
            zf = zipfile.ZipFile(io.BytesIO(payload))
            namelist = zf.namelist()
            self.assertIn("version.txt", namelist)
            self.assertEqual(zf.read("version.txt").decode(), "2.0.1")
            self.assertIn("resources/runtime/version.json", namelist)

    def test_09_download_interruption_socket_reset(self):
        """Verify that download interruption drops socket connection mid-stream."""
        self.server.set_download_mode("interrupted")
        url = f"{self.server.base_url}/download/Orange_AI_2.0.1_x64-setup.nsis.zip"

        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                # Read until connection resets or drops
                chunks = []
                while True:
                    chunk = resp.read(128)
                    if not chunk:
                        break
                    chunks.append(chunk)
            # If it didn't throw an exception, verify bytes read were truncated compared to declared
            received = sum(len(c) for c in chunks)
            self.assertLess(received, 1000)
        except (socket.error, ConnectionResetError, http.client.RemoteDisconnected, urllib.error.URLError) as e:
            # Expected connection interruption
            pass

    def test_10_truncated_archive_payload(self):
        """Verify that truncated mode sends fewer bytes than declared Content-Length, triggering IncompleteRead."""
        self.server.set_download_mode("truncated")
        url = f"{self.server.base_url}/download/Orange_AI_2.0.1_x64-setup.nsis.zip"

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            content_length = int(resp.headers.get("Content-Length", 0))
            self.assertGreater(content_length, 1000)
            with self.assertRaises((http.client.IncompleteRead, urllib.error.URLError)) as ctx:
                resp.read()

    def test_11_corrupt_archive_payload(self):
        """Verify that corrupt mode delivers non-zip binary data."""
        self.server.set_download_mode("corrupt")
        url = f"{self.server.base_url}/download/Orange_AI_2.0.1_x64-setup.nsis.zip"

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            payload = resp.read()
            with self.assertRaises(zipfile.BadZipFile):
                zipfile.ZipFile(io.BytesIO(payload))

    def test_12_delayed_response_simulation(self):
        """Verify that delayed mode introduces the specified sleep before responding."""
        self.server.set_metadata_mode("valid", delay=0.25)
        start_t = time.time()
        req = urllib.request.Request(self.server.latest_url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
        elapsed = time.time() - start_t
        self.assertGreaterEqual(elapsed, 0.20, "Response must reflect specified server delay")

    def test_13_transient_retry_then_success(self):
        """Verify that retry mode fails the first N attempts then succeeds."""
        self.server.state.metadata_mode = "retry"
        self.server.state.retry_fail_count = 2

        # Attempt 1: Must fail with 503
        with self.assertRaises(urllib.error.HTTPError) as ctx1:
            urllib.request.urlopen(urllib.request.Request(self.server.latest_url), timeout=2.0)
        self.assertEqual(ctx1.exception.code, 503)

        # Attempt 2: Must fail with 503
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            urllib.request.urlopen(urllib.request.Request(self.server.latest_url), timeout=2.0)
        self.assertEqual(ctx2.exception.code, 503)

        # Attempt 3: Must succeed with 200 OK
        with urllib.request.urlopen(urllib.request.Request(self.server.latest_url), timeout=2.0) as resp3:
            self.assertEqual(resp3.status, 200)
            data = json.loads(resp3.read().decode())
            self.assertEqual(data.get("version"), "2.0.1")

    def test_14_request_tracking_and_history(self):
        """Verify that the mock server accurately logs request paths and counts."""
        self.server.set_metadata_mode("valid")
        for _ in range(4):
            with urllib.request.urlopen(urllib.request.Request(self.server.latest_url), timeout=2.0):
                pass
        self.assertEqual(self.server.get_request_count("/latest.json"), 4)
        history = self.server.get_request_history()
        self.assertGreaterEqual(len(history), 4)
        self.assertEqual(history[-1]["path"], "/latest.json")

    def test_15_query_parameter_mode_override(self):
        """Verify that query parameters (?mode=...) override server default mode."""
        self.server.set_metadata_mode("valid")
        url_override = f"{self.server.latest_url}?mode=http_500"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(urllib.request.Request(url_override), timeout=2.0)
        self.assertEqual(ctx.exception.code, 500)


if __name__ == "__main__":
    import http.client
    unittest.main(verbosity=2)
