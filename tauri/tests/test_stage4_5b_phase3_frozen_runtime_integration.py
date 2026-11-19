"""
test_stage4_5b_phase3_frozen_runtime_integration.py - Direct Integration Test Suite
for the newly re-frozen Python runtime binary (runtime_adapter.exe).

Exercises the actual compiled PyInstaller distribution:
  D:\\Orange AI\\Orange AI Tauri\\src-tauri\\resources\\runtime\\runtime_adapter.exe

Verifies:
  1. Real JSON-RPC 2.0 stdio handshake (system.handshake)
  2. Protocol contract adherence (status=ready, version=2.0.0, protocol=1.0, python=3.14.6)
  3. Stdout purity (zero debug logs/banners on stdout)
  4. Safe non-destructive command execution (system.ping, system.health)
  5. Clean process shutdown upon stdin EOF
  6. Phase 2 Pre-Update Quiescence on frozen runtime (system.shutdown_for_update)

CRITICAL SAFETY DIRECTIVE:
Enforces triple-layer database isolation guard. Absolutely NO test may access,
read, write, or touch %LOCALAPPDATA%\\Orange AI\\memory.db.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess
import sqlite3

# ---------------------------------------------------------------------------
# Triple-Layer Operational Database Safety Guard
# ---------------------------------------------------------------------------
class OperationalDatabaseAccessViolation(Exception):
    """Raised if any test attempts to access the operational user database."""
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
    if _REAL_SQLITE3_CONNECT is None:
        _REAL_SQLITE3_CONNECT = sqlite3.connect
        sqlite3.connect = _guard_sqlite3_connect


install_test_isolation_guards()

TAURI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FROZEN_EXE = os.path.join(TAURI_ROOT, "src-tauri", "resources", "runtime", "runtime_adapter.exe")


class TestFrozenRuntimeIntegration(unittest.TestCase):
    """Integration test suite executing the actual frozen runtime binary."""

    def setUp(self):
        install_test_isolation_guards()
        self.test_dir = tempfile.mkdtemp(prefix="orange_ai_frozen_int_")
        self.env = os.environ.copy()
        self.env["ORANGE_AI_USER_DATA_DIR"] = self.test_dir
        self.env["ORANGE_AI_TEST_MODE"] = "1"
        self.env.pop("PYTHONPATH", None)
        self.env.pop("PYTHONHOME", None)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_frozen_binary_exists_and_metadata(self):
        """Test 1: Verify frozen runtime binary exists, is non-empty, and has expected attributes."""
        self.assertTrue(os.path.exists(FROZEN_EXE), f"Frozen binary not found at {FROZEN_EXE}")
        size = os.path.getsize(FROZEN_EXE)
        self.assertGreater(size, 5 * 1024 * 1024, f"Binary size unexpectedly small: {size} bytes")

        # Verify bundled python314.dll
        internal_dir = os.path.join(os.path.dirname(FROZEN_EXE), "_internal")
        python_dll = os.path.join(internal_dir, "python314.dll")
        self.assertTrue(os.path.exists(python_dll), f"python314.dll not found in {internal_dir}")

    def test_02_frozen_runtime_real_handshake_and_stdout_purity(self):
        """Test 2: Launch frozen runtime, send real system.handshake, and verify stdout purity."""
        proc = subprocess.Popen(
            [FROZEN_EXE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self.env
        )

        handshake_req = {"jsonrpc": "2.0", "id": "frozen-handshake-1", "method": "system.handshake", "params": {}}
        proc.stdin.write(json.dumps(handshake_req) + "\n")
        proc.stdin.flush()

        stdout_lines = []
        handshake_resp = None
        start = time.time()

        while time.time() - start < 10:
            line = proc.stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue
            stdout_lines.append(line_str)
            try:
                data = json.loads(line_str)
            except Exception as e:
                self.fail(f"Corrupted non-JSON line detected on stdout: '{line_str}' (error: {e})")

            if data.get("id") == "frozen-handshake-1":
                handshake_resp = data
                break

        # Clean shutdown
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait(timeout=5)

        # Assertions on stdout lines
        self.assertGreaterEqual(len(stdout_lines), 1, "Expected at least 1 stdout JSON-RPC message")
        for line_str in stdout_lines:
            d = json.loads(line_str)
            self.assertEqual(d.get("jsonrpc"), "2.0", f"Missing jsonrpc 2.0 in line: {line_str}")

        self.assertIsNotNone(handshake_resp, "Did not receive response to system.handshake")
        res = handshake_resp.get("result", {})

        # Handshake contract assertions
        self.assertEqual(res.get("status"), "ready")
        self.assertEqual(res.get("runtime"), "Orange AI Python Runtime")
        self.assertEqual(res.get("version"), "2.0.0")
        self.assertEqual(res.get("protocol_version"), "1.0")
        self.assertEqual(res.get("python_version"), "3.14.6")
        self.assertIsInstance(res.get("pid"), int)
        self.assertGreater(res.get("pid"), 0)
        self.assertIn("chat", res.get("capabilities", []))
        self.assertIn("memory", res.get("capabilities", []))
        self.assertIn("planner", res.get("capabilities", []))
        self.assertIn("tools", res.get("capabilities", []))
        self.assertIn("pre_update_quiescence", res.get("capabilities", []))

    def test_03_frozen_runtime_safe_operations_and_clean_exit(self):
        """Test 3: Execute safe operational commands (system.ping, system.health) on frozen runtime."""
        proc = subprocess.Popen(
            [FROZEN_EXE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self.env
        )

        # 1. system.ping
        ping_req = {"jsonrpc": "2.0", "id": "frozen-ping-1", "method": "system.ping", "params": {}}
        proc.stdin.write(json.dumps(ping_req) + "\n")
        proc.stdin.flush()

        ping_resp = None
        start = time.time()
        while time.time() - start < 10:
            line = proc.stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue
            data = json.loads(line_str)
            if data.get("id") == "frozen-ping-1":
                ping_resp = data
                break

        self.assertIsNotNone(ping_resp, "Did not receive response to system.ping")
        self.assertEqual(ping_resp.get("result", {}).get("status"), "online")
        self.assertEqual(ping_resp.get("result", {}).get("version"), "2.0.0")

        # 2. system.health
        health_req = {"jsonrpc": "2.0", "id": "frozen-health-1", "method": "system.health", "params": {}}
        proc.stdin.write(json.dumps(health_req) + "\n")
        proc.stdin.flush()

        health_resp = None
        start = time.time()
        while time.time() - start < 10:
            line = proc.stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue
            data = json.loads(line_str)
            if data.get("id") == "frozen-health-1":
                health_resp = data
                break

        self.assertIsNotNone(health_resp, "Did not receive response to system.health")
        self.assertEqual(health_resp.get("result", {}).get("health"), "ok")

        # Clean shutdown
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 0, f"Process did not exit cleanly: {proc.returncode}")

    def test_04_frozen_runtime_shutdown_for_update_quiescence(self):
        """Test 4: Verify pre-update shutdown and checkpoint quiescence on frozen runtime."""
        # Create a disposable SQLite database in temporary directory
        disposable_db = os.path.join(self.test_dir, "disposable_test.db")
        conn = _REAL_SQLITE3_CONNECT(disposable_db)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, msg TEXT);")
        conn.execute("INSERT INTO test_data (msg) VALUES ('before_shutdown');")
        conn.commit()
        conn.close()

        proc = subprocess.Popen(
            [FROZEN_EXE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self.env
        )

        # Trigger system.shutdown_for_update targeting the disposable DB
        shutdown_req = {
            "jsonrpc": "2.0",
            "id": "frozen-shutdown-1",
            "method": "system.shutdown_for_update",
            "params": {"custom_db_path": disposable_db}
        }
        proc.stdin.write(json.dumps(shutdown_req) + "\n")
        proc.stdin.flush()

        shutdown_resp = None
        start = time.time()
        while time.time() - start < 10:
            line = proc.stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue
            data = json.loads(line_str)
            if data.get("id") == "frozen-shutdown-1":
                shutdown_resp = data
                break

        self.assertIsNotNone(shutdown_resp, "Did not receive response to system.shutdown_for_update")
        res = shutdown_resp.get("result", {})
        self.assertEqual(res.get("status"), "ready_for_update")
        self.assertEqual(res.get("checkpoint", {}).get("busy"), 0)
        self.assertEqual(res.get("wal_bytes"), 0)

        # Clean exit
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait(timeout=5)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
