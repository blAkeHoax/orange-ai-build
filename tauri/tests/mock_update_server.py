"""
mock_update_server.py - Controlled Local Mock Update Server for Orange AI Desktop 2.0.
Stage 4.5B Phase 5: Mock Server & Interruption Testing.

Provides an isolated, lightweight HTTP server using Python's standard library
for testing the Tauri desktop updater client without touching network endpoints,
modifying public distribution repositories, or generating cryptographic keys.

Supported Scenarios:
1. Valid update metadata (latest.json) with configurable versions and signatures.
2. No-update responses (version <= current, or empty platforms).
3. Malformed metadata (syntax errors in JSON).
4. Missing required fields (omitted 'version', 'platforms', or 'url').
5. Invalid version information (non-semver or downgrade strings).
6. Download success (serves complete zip/nsis archive with valid Content-Length).
7. Download interruption (abruptly closes TCP socket mid-stream after sending N bytes).
8. HTTP error codes (404 Not Found, 500 Internal Server Error, 503 Service Unavailable).
9. Connection timeouts / hung requests (delays headers/body beyond client timeout).
10. Truncated update archives (sends fewer bytes than declared in Content-Length).
11. Invalid archive contents (delivers corrupt/garbage non-zip binary payload).
12. Delayed responses (configurable server sleep before responding).
13. Repeated requests and retry behavior (tracks request count, fails first N times then succeeds).
"""

import os
import sys
import json
import time
import socket
import struct
import zipfile
import io
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional, List


def create_dummy_update_zip(version: str = "2.0.1", content: str = "Dummy Update Payload") -> bytes:
    """Create a minimal in-memory zip archive containing a mock update file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version.txt", version)
        zf.writestr("payload.bin", content * 100)
        zf.writestr("resources/runtime/version.json", json.dumps({"runtime_version": version, "protocol_version": "1.0"}))
    return buf.getvalue()


class MockUpdateServerState:
    """Thread-safe mutable state controlling mock server behaviors."""

    def __init__(self):
        self.lock = threading.Lock()
        self.metadata_mode: str = "valid"  # valid, no_update, malformed, missing_fields, invalid_version, http_500, http_404, http_503, delayed, retry
        self.download_mode: str = "success"  # success, interrupted, truncated, corrupt, timeout, http_404, http_500
        self.target_version: str = "2.0.1"
        self.current_version: str = "2.0.0"
        self.delay_seconds: float = 0.0
        self.retry_fail_count: int = 2
        self.request_counts: Dict[str, int] = {}
        self.request_history: List[Dict[str, Any]] = []
        self.custom_metadata: Optional[Dict[str, Any]] = None
        self.custom_payload: Optional[bytes] = None

    def reset(self):
        with self.lock:
            self.metadata_mode = "valid"
            self.download_mode = "success"
            self.target_version = "2.0.1"
            self.current_version = "2.0.0"
            self.delay_seconds = 0.0
            self.retry_fail_count = 2
            self.request_counts.clear()
            self.request_history.clear()
            self.custom_metadata = None
            self.custom_payload = None

    def record_request(self, path: str, method: str, headers: Dict[str, str]):
        with self.lock:
            self.request_counts[path] = self.request_counts.get(path, 0) + 1
            self.request_history.append({
                "path": path,
                "method": method,
                "headers": dict(headers),
                "timestamp": time.time(),
                "count": self.request_counts[path],
            })


_GLOBAL_SERVER_STATE = MockUpdateServerState()


class MockUpdateRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler executing configurable update server scenarios."""

    server_version = "OrangeAIMockUpdateServer/1.0"

    def log_message(self, format, *args):
        # Suppress noisy standard HTTP access logging during automated test runs
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        _GLOBAL_SERVER_STATE.record_request(path, "GET", self.headers)

        # Allow query-string override for mode: e.g. /latest.json?mode=malformed
        req_mode = query.get("mode", [None])[0]

        if path.endswith("/latest.json") or path == "/latest.json":
            self.handle_metadata_request(req_mode)
        elif path.startswith("/download/"):
            self.handle_download_request(req_mode)
        elif path == "/health" or path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","server":"mock_update_server"}')
        else:
            self.send_error(404, f"Path not found: {path}")

    def handle_metadata_request(self, override_mode: Optional[str] = None):
        with _GLOBAL_SERVER_STATE.lock:
            mode = override_mode or _GLOBAL_SERVER_STATE.metadata_mode
            target_version = _GLOBAL_SERVER_STATE.target_version
            delay = _GLOBAL_SERVER_STATE.delay_seconds
            req_count = _GLOBAL_SERVER_STATE.request_counts.get(self.path.split("?")[0], 1)
            retry_threshold = _GLOBAL_SERVER_STATE.retry_fail_count
            custom_meta = _GLOBAL_SERVER_STATE.custom_metadata

        if delay > 0:
            time.sleep(delay)

        if mode == "http_500":
            self.send_error(500, "Simulated internal server error")
            return
        elif mode == "http_404":
            self.send_error(404, "Simulated release manifest not found")
            return
        elif mode == "http_503":
            self.send_error(503, "Simulated service unavailable")
            return
        elif mode == "timeout":
            # Sleep 5 seconds to induce client timeout
            time.sleep(5.0)
            return
        elif mode == "retry":
            if req_count <= retry_threshold:
                self.send_error(503, f"Transient failure {req_count}/{retry_threshold}")
                return

        host = self.headers.get("Host", "127.0.0.1")
        download_url = f"http://{host}/download/Orange_AI_{target_version}_x64-setup.nsis.zip"

        if custom_meta is not None:
            data = json.dumps(custom_meta).encode("utf-8")
        elif mode == "malformed":
            data = b'{"version": "' + target_version.encode("utf-8") + b'", "notes": INVALID_JSON_SYNTAX'
        elif mode == "missing_fields":
            # Missing platforms and url
            payload = {
                "version": target_version,
                "notes": "Missing platforms field metadata",
            }
            data = json.dumps(payload).encode("utf-8")
        elif mode == "invalid_version":
            payload = {
                "version": "not-a-valid-semver-string",
                "notes": "Invalid version metadata",
                "pub_date": "2026-11-19T18:00:00Z",
                "platforms": {
                    "windows-x86_64": {
                        "signature": "dW5zaWduZWQ=",
                        "url": download_url,
                    }
                },
            }
            data = json.dumps(payload).encode("utf-8")
        elif mode == "no_update":
            # Target version equals current version, or empty platforms
            payload = {
                "version": _GLOBAL_SERVER_STATE.current_version,
                "notes": "No updates available",
                "pub_date": "2026-11-19T18:00:00Z",
                "platforms": {},
            }
            data = json.dumps(payload).encode("utf-8")
        else:
            # Valid update response
            payload = {
                "version": target_version,
                "notes": "### Improvements\n- Stage 4.5B Phase 5 Verified Update\n- System Hardening",
                "pub_date": "2026-11-19T18:00:00Z",
                "platforms": {
                    "windows-x86_64": {
                        "signature": "dW5zaWduZWQtZm9yLW1vY2stdGVzdGluZw==",
                        "url": download_url,
                    }
                },
            }
            data = json.dumps(payload).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def handle_download_request(self, override_mode: Optional[str] = None):
        with _GLOBAL_SERVER_STATE.lock:
            mode = override_mode or _GLOBAL_SERVER_STATE.download_mode
            target_version = _GLOBAL_SERVER_STATE.target_version
            delay = _GLOBAL_SERVER_STATE.delay_seconds
            custom_payload = _GLOBAL_SERVER_STATE.custom_payload

        if delay > 0:
            time.sleep(delay)

        if mode == "http_404":
            self.send_error(404, "Update archive not found")
            return
        elif mode == "http_500":
            self.send_error(500, "Server error streaming update archive")
            return
        elif mode == "timeout":
            time.sleep(5.0)
            return

        # Prepare payload
        if custom_payload is not None:
            archive_data = custom_payload
        elif mode == "corrupt":
            archive_data = b"NOT_A_VALID_ZIP_ARCHIVE_DATA_GARBAGE_PAYLOAD" * 50
        else:
            archive_data = create_dummy_update_zip(target_version)

        total_bytes = len(archive_data)

        if mode == "truncated":
            # Declare large content length, but send only fraction then terminate cleanly
            declared_length = total_bytes + 500000
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(declared_length))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(archive_data[: total_bytes // 2])
            return

        if mode == "interrupted":
            # Send partial chunk, then forcefully close the underlying TCP socket
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(total_bytes))
            self.send_header("Connection", "close")
            self.end_headers()
            # Send first 256 bytes
            chunk = archive_data[: min(256, total_bytes)]
            self.wfile.write(chunk)
            self.wfile.flush()
            # Abruptly shut down and close connection
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            self.close_connection = True
            return

        # Standard successful download
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(total_bytes))
        self.send_header("Connection", "close")
        self.end_headers()

        # Stream in 1KB chunks to simulate network streaming
        chunk_size = 1024
        for i in range(0, total_bytes, chunk_size):
            self.wfile.write(archive_data[i : i + chunk_size])
            self.wfile.flush()


class MockUpdateServer:
    """Wrapper managing the background HTTP server thread lifecycle."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self.httpd: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.state = _GLOBAL_SERVER_STATE

    def start(self):
        """Bind ephemeral port and start daemon background server thread."""
        self.httpd = HTTPServer((self.host, self.port), MockUpdateRequestHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """Shut down the server and join thread."""
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread:
            self.thread.join(timeout=2.0)
            self.thread = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def latest_url(self) -> str:
        return f"{self.base_url}/latest.json"

    def set_metadata_mode(self, mode: str, target_version: str = "2.0.1", delay: float = 0.0):
        with self.state.lock:
            self.state.metadata_mode = mode
            self.state.target_version = target_version
            self.state.delay_seconds = delay

    def set_download_mode(self, mode: str, delay: float = 0.0):
        with self.state.lock:
            self.state.download_mode = mode
            self.state.delay_seconds = delay

    def set_custom_metadata(self, metadata: Dict[str, Any]):
        with self.state.lock:
            self.state.custom_metadata = metadata

    def set_custom_payload(self, payload: bytes):
        with self.state.lock:
            self.state.custom_payload = payload

    def reset(self):
        self.state.reset()

    def get_request_count(self, path: str) -> int:
        with self.state.lock:
            return self.state.request_counts.get(path, 0)

    def get_request_history(self) -> List[Dict[str, Any]]:
        with self.state.lock:
            return list(self.state.request_history)


if __name__ == "__main__":
    server = MockUpdateServer(port=8888)
    server.start()
    print(f"Mock Update Server running at {server.base_url}")
    print(f"Metadata endpoint: {server.latest_url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.stop()
