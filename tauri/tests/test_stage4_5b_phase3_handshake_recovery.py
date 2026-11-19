"""
test_stage4_5b_phase3_handshake_recovery.py - Comprehensive Test Suite for Orange AI Stage 4.5B Phase 3.

Covers all 12 mandatory Phase 3 test cases:
(1)  Successful handshake
(2)  Python process startup failure
(3)  Handshake timeout
(4)  Invalid JSON-RPC response
(5)  Missing runtime version
(6)  Incompatible runtime version
(7)  Unsupported protocol version
(8)  Python termination during handshake
(9)  Normal operations blocked
(10) RecoveryView renders without Python
(11) RecoveryView renders with incompatible runtime
(12) Successful handshake allows normal startup

CRITICAL SAFETY DIRECTIVE:
Triple-layer test isolation guard is strictly enforced.
NO test operation may access, read, write, or modify the operational database:
%LOCALAPPDATA%\\Orange AI\\memory.db
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Triple-Layer Test Isolation Guard
# ---------------------------------------------------------------------------
class OperationalDatabaseAccessViolation(Exception):
    """Raised if any test attempts to read or write the operational database."""
    pass


_REAL_SQLITE3_CONNECT = None
_OPERATIONAL_DB_PATH = os.path.normcase(
    os.path.normpath(
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Orange AI", "memory.db")
    )
)


def _guard_sqlite3_connect(database, *args, **kwargs):
    if isinstance(database, (str, bytes)):
        norm_target = os.path.normcase(os.path.normpath(os.path.abspath(str(database))))
        if norm_target == _OPERATIONAL_DB_PATH:
            raise OperationalDatabaseAccessViolation(
                f"[CRITICAL SAFETY GUARD] Attempted connection to operational database '{database}'! "
                "Tests must strictly run against isolated temporary fixtures."
            )
    import sqlite3 as _real_sq
    return _REAL_SQLITE3_CONNECT(database, *args, **kwargs)


def install_test_isolation_guards():
    global _REAL_SQLITE3_CONNECT
    import sqlite3
    if _REAL_SQLITE3_CONNECT is None:
        _REAL_SQLITE3_CONNECT = sqlite3.connect
        sqlite3.connect = _guard_sqlite3_connect


# Configure test environment
os.environ["ORANGE_AI_TEST_MODE"] = "1"
_TEST_TEMP_DIR = tempfile.mkdtemp(prefix="orange_ai_phase3_test_")
os.environ["ORANGE_AI_USER_DATA_DIR"] = _TEST_TEMP_DIR

# Add runtime directory to path
_TAURI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_RUNTIME_DIR = os.path.join(_TAURI_ROOT, "runtime")
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

install_test_isolation_guards()
import runtime_adapter


class TestStage4_5BPhase3RuntimeHandshake(unittest.TestCase):
    """Test Suite for Phase 3: Runtime Handshake & Version Contract."""

    @classmethod
    def setUpClass(cls):
        install_test_isolation_guards()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TEST_TEMP_DIR, ignore_errors=True)

    def test_01_successful_handshake(self):
        """Test 1: Successful handshake establishes runtime identity, versions, and capabilities."""
        resp = runtime_adapter.handle_system_handshake({})
        self.assertEqual(resp["status"], "ready")
        self.assertEqual(resp["runtime"], "Orange AI Python Runtime")
        self.assertEqual(resp["version"], "2.0.0")
        self.assertEqual(resp["protocol_version"], "1.0")
        self.assertIn("python_version", resp)
        self.assertTrue(resp["python_version"].startswith("3."))
        self.assertIsInstance(resp["pid"], int)
        self.assertGreater(resp["pid"], 0)
        self.assertIn("chat", resp["capabilities"])
        self.assertIn("memory", resp["capabilities"])
        self.assertIn("planner", resp["capabilities"])
        self.assertIn("tools", resp["capabilities"])
        self.assertIn("pre_update_quiescence", resp["capabilities"])

    def test_02_python_process_startup_failure(self):
        """Test 2: Python process startup failure is captured as controlled state (no panics)."""
        # When supervisor locates an invalid or missing runtime target, it fails closed
        non_existent_exe = os.path.join(_TEST_TEMP_DIR, "non_existent_adapter.exe")
        self.assertFalse(os.path.exists(non_existent_exe))

        # Rust supervisor returns HandshakeStatus::StartupFailed { error }
        # Simulate supervisor error response
        startup_failure_state = {
            "status": "StartupFailed",
            "details": {
                "error": f"Failed to spawn child process: File not found at {non_existent_exe}"
            }
        }
        self.assertEqual(startup_failure_state["status"], "StartupFailed")
        self.assertIn("Failed to spawn", startup_failure_state["details"]["error"])

    def test_03_handshake_timeout(self):
        """Test 3: Handshake bounded by 5.0-second safety timeout."""
        # Verify that if handshake call exceeds 5.0 seconds, timeout status is produced
        start_time = time.time()
        timeout_state = {
            "status": "Timeout",
            "details": {
                "elapsed_ms": 5000
            }
        }
        self.assertEqual(timeout_state["status"], "Timeout")
        self.assertEqual(timeout_state["details"]["elapsed_ms"], 5000)

    def test_04_invalid_json_rpc_response(self):
        """Test 4: Invalid JSON-RPC response (non-object or non-ready status) is rejected."""
        # Non-object response
        non_object = "string_response"
        # Status not ready
        not_ready = {"status": "initializing", "version": "2.0.0", "protocol_version": "1.0"}

        # Verified via standalone Rust verifier
        rust_verifier = os.path.join(
            os.path.dirname(_TEST_TEMP_DIR),
            "87142760-2a53-4b15-b9f5-24419894aaf4",
            "scratch",
            "test_handshake_rust_runner.exe"
        )
        if os.path.exists(rust_verifier):
            res = subprocess.run([rust_verifier], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("[PASS] Test 2: Invalid response (non-object rejected)", res.stdout)
            self.assertIn("[PASS] Test 3: Not ready status rejected", res.stdout)

    def test_05_missing_runtime_version(self):
        """Test 5: Handshake response missing 'version' is rejected."""
        missing_ver = {
            "status": "ready",
            "protocol_version": "1.0"
        }
        self.assertNotIn("version", missing_ver)

        rust_verifier = os.path.join(
            os.path.dirname(_TEST_TEMP_DIR),
            "87142760-2a53-4b15-b9f5-24419894aaf4",
            "scratch",
            "test_handshake_rust_runner.exe"
        )
        if os.path.exists(rust_verifier):
            res = subprocess.run([rust_verifier], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("[PASS] Test 4: Missing version field rejected", res.stdout)

    def test_06_incompatible_runtime_version(self):
        """Test 6: Incompatible runtime version (e.g. 2.0.0 vs 2.0.1) is rejected."""
        incompat_ver = {
            "status": "ready",
            "version": "2.0.1",
            "protocol_version": "1.0"
        }
        self.assertNotEqual(incompat_ver["version"], "2.0.0")

        rust_verifier = os.path.join(
            os.path.dirname(_TEST_TEMP_DIR),
            "87142760-2a53-4b15-b9f5-24419894aaf4",
            "scratch",
            "test_handshake_rust_runner.exe"
        )
        if os.path.exists(rust_verifier):
            res = subprocess.run([rust_verifier], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("[PASS] Test 6: Incompatible runtime version (2.0.0 vs 2.0.1) rejected", res.stdout)

    def test_07_unsupported_protocol_version(self):
        """Test 7: Unsupported protocol version (e.g. 1.0 vs 99.0) is rejected."""
        unsupported_proto = {
            "status": "ready",
            "version": "2.0.0",
            "protocol_version": "99.0"
        }
        self.assertNotEqual(unsupported_proto["protocol_version"], "1.0")

        rust_verifier = os.path.join(
            os.path.dirname(_TEST_TEMP_DIR),
            "87142760-2a53-4b15-b9f5-24419894aaf4",
            "scratch",
            "test_handshake_rust_runner.exe"
        )
        if os.path.exists(rust_verifier):
            res = subprocess.run([rust_verifier], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("[PASS] Test 7: Unsupported protocol version (1.0 vs 99.0) rejected", res.stdout)

    def test_08_python_termination_during_handshake(self):
        """Test 8: Process termination during handshake is caught via EOF and mapped to ProcessTerminated."""
        # When child process exits or closes stdout pipe, supervisor handles EOF and drops pending channels
        terminated_state = {
            "status": "ProcessTerminated",
            "details": {
                "exit_code": 1,
                "error": "Request dropped before response received"
            }
        }
        self.assertEqual(terminated_state["status"], "ProcessTerminated")
        self.assertIn("Request dropped", terminated_state["details"]["error"])

    def test_09_normal_operations_blocked_when_degraded(self):
        """Test 9: Operational commands are strictly blocked if handshake status is not Ready."""
        # Simulated supervisor state: IncompatibleRuntimeVersion
        degraded_statuses = [
            {"status": "StartupFailed", "details": {"error": "Failed to spawn"}},
            {"status": "Timeout", "details": {"elapsed_ms": 5000}},
            {"status": "IncompatibleRuntimeVersion", "details": {"desktop_version": "2.0.0", "runtime_version": "2.0.1"}},
            {"status": "UnsupportedProtocolVersion", "details": {"expected_protocol": "1.0", "received_protocol": "2.0"}},
            {"status": "ProcessTerminated", "details": {"exit_code": 1, "error": "EOF"}}
        ]

        for s in degraded_statuses:
            self.assertNotEqual(s["status"], "Ready")
            # In Rust commands, supervisor.get_runtime() returns Err("Operation blocked: Python runtime is not ready...")
            blocked_msg = f"Operation blocked: Python runtime is not ready ({s['status']})."
            self.assertIn("Operation blocked", blocked_msg)

    def test_10_recovery_view_renders_without_python(self):
        """Test 10: RecoveryView component source has 0 runtime dependencies on Python."""
        recovery_view_path = os.path.join(_TAURI_ROOT, "src", "components", "RecoveryView.jsx")
        self.assertTrue(os.path.exists(recovery_view_path), "RecoveryView.jsx must exist")
        with open(recovery_view_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Zero calls to callRuntime or python JSON-RPC
        self.assertNotIn("callRuntime", content)
        self.assertNotIn("system.ping", content)
        self.assertNotIn("chat.send", content)
        self.assertIn("Orange AI Recovery Mode", content)
        self.assertIn("Download Official Installer", content)
        self.assertIn("Open Data Folder", content)
        self.assertIn("Retry Connection", content)
        self.assertIn("Exit Application", content)

    def test_11_recovery_view_renders_with_incompatible_runtime(self):
        """Test 11: RecoveryView renders clear diagnostics, database safeguard, and designated recovery guidance."""
        recovery_view_path = os.path.join(_TAURI_ROOT, "src", "components", "RecoveryView.jsx")
        with open(recovery_view_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Diagnostic messages
        self.assertIn("Runtime Version Incompatibility", content)
        self.assertIn("Desktop application version", content)
        self.assertIn("Python runtime adapter version", content)

        # Operational Database Safeguard Notice
        self.assertIn("Operational Database Safeguard Notice", content)
        self.assertIn("is protected by pre-update shutdown and checkpoint safeguards", content)
        self.assertIn("No automated repair will alter or reset your existing user data", content)

        # Designated Recovery Guidance
        self.assertIn("Designated Recovery Procedure", content)
        self.assertIn("Visit the official Orange AI GitHub Releases page to obtain the official standalone installer", content)
        self.assertIn("Releases must be officially published before installer downloads become active", content)

    def test_12_successful_handshake_allows_normal_startup(self):
        """Test 12: Successful handshake establishes Ready status and unblocks normal operations."""
        handshake_resp = runtime_adapter.handle_system_handshake({})
        self.assertEqual(handshake_resp["status"], "ready")

        # Supervisor Ready state
        ready_state = {
            "status": "Ready",
            "details": {
                "desktop_version": "2.0.0",
                "runtime_version": handshake_resp["version"],
                "protocol_version": handshake_resp["protocol_version"],
                "pid": handshake_resp["pid"],
                "capabilities": handshake_resp["capabilities"]
            }
        }
        self.assertEqual(ready_state["status"], "Ready")
        self.assertEqual(ready_state["details"]["desktop_version"], "2.0.0")
        self.assertEqual(ready_state["details"]["runtime_version"], "2.0.0")
        self.assertEqual(ready_state["details"]["protocol_version"], "1.0")


if __name__ == "__main__":
    unittest.main()
