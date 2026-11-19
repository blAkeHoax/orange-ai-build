"""
Phase 2 Binary & Supervisor Acceptance Test Suite
Tests the compiled Tauri 2 binary (app.exe):
  1. Single-instance mutex enforcement (second instance exits immediately with code 0)
  2. Windows Job Object child process cleanup (killing app.exe kills child python.exe immediately)
  3. Clean termination and exit code verification
"""

import os
import subprocess
import sys
import time
import psutil

TAURI_EXE = r"D:\Orange AI\Orange AI Tauri\src-tauri\target\x86_64-pc-windows-gnu\debug\app.exe"


def find_child_python_processes(parent_pid):
    """Find child python processes spawned by parent_pid."""
    children = []
    try:
        parent = psutil.Process(parent_pid)
        for child in parent.children(recursive=True):
            if "python" in child.name().lower():
                children.append(child)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return children


def test_single_instance_and_job_object():
    if not os.path.exists(TAURI_EXE):
        print(f"[SKIP] Binary does not exist yet at {TAURI_EXE}")
        return False

    print(f"\n--- [TEST] Starting Instance 1: {TAURI_EXE} ---")
    proc1 = subprocess.Popen([TAURI_EXE], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"Instance 1 PID: {proc1.pid}")

    try:
        # Give Tauri and Python up to 15s to start and initialize
        child_pythons = []
        for _ in range(15):
            time.sleep(1)
            if proc1.poll() is not None:
                raise AssertionError(f"Instance 1 died unexpectedly on startup with code {proc1.returncode}")
            child_pythons = find_child_python_processes(proc1.pid)
            if len(child_pythons) > 0:
                break

        print(f"Spawned child Python processes: {[p.pid for p in child_pythons]}")
        assert len(child_pythons) > 0, "No child Python process found under Instance 1 after 15s"
        py_pid = child_pythons[0].pid

        # Test Single Instance: Launch Instance 2
        print("\n--- [TEST] Starting Instance 2 (should exit immediately with code 0) ---")
        proc2 = subprocess.Popen([TAURI_EXE], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ret2 = proc2.wait(timeout=5)
            print(f"Instance 2 exited with return code: {ret2}")
            assert ret2 == 0, f"Instance 2 should have exited with code 0, got {ret2}"
            print("PASSED: Single instance mutex enforcement verified.")
        except subprocess.TimeoutExpired:
            proc2.kill()
            raise AssertionError("Instance 2 did not exit within 5 seconds; single-instance mutex failed!")

        # Verify Instance 1 is still running
        assert proc1.poll() is None, "Instance 1 exited when Instance 2 was launched"

        # Verify child python is still alive
        assert psutil.pid_exists(py_pid), f"Child python {py_pid} died prematurely"

        # Test Job Object cleanup: Kill Instance 1 abruptly (SIGKILL / TerminateProcess)
        print("\n--- [TEST] Terminating Instance 1 to test Job Object cleanup ---")
        proc1.kill()
        proc1.wait(timeout=5)
        print("Instance 1 terminated.")

        # Check if child Python process is terminated by Windows Job Object
        time.sleep(1)
        is_alive = psutil.pid_exists(py_pid)
        print(f"Child Python PID {py_pid} alive after parent termination? {is_alive}")
        assert not is_alive, f"ORPHAN DETECTED! Python PID {py_pid} is still running after parent was killed."
        print("PASSED: Windows Job Object killed child Python runtime instantly with zero orphans.")

        return True
    finally:
        if proc1.poll() is None:
            proc1.kill()


if __name__ == "__main__":
    success = test_single_instance_and_job_object()
    sys.exit(0 if success else 1)
