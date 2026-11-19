"""
scripts/build_runtime.py - Build and verify standalone Python runtime for Orange AI Desktop 2.0 (Tauri).

Compiles runtime_adapter.py and the protected Orange AI Desktop engine into a self-contained
standalone binary distribution located in src-tauri/resources/runtime/.
"""

import os
import sys
import subprocess
import json
import time
import shutil

TAURI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def resolve_python_exe(tauri_dir):
    """
    Resolve the Python executable to run PyInstaller:
    1. If ORANGE_BUILD_PYTHON is set:
       a. Check if it's an explicit file path that exists on disk.
       b. Check if it's an executable name resolvable via PATH using shutil.which().
       c. If neither matches, raise a fatal ValueError (do not silently fall back).
    2. Local development virtual-environment fallback.
    3. sys.executable fallback.
    """
    override = os.environ.get("ORANGE_BUILD_PYTHON", "").strip()
    if override:
        if os.path.isabs(override) or os.path.exists(override):
            if os.path.isfile(override):
                return os.path.abspath(override), f"explicit environment path ({override})"
            raise ValueError(f"ORANGE_BUILD_PYTHON points to non-file path: {override}")
        resolved = shutil.which(override)
        if resolved:
            return os.path.abspath(resolved), f"PATH lookup ({override})"
        raise ValueError(f'ORANGE_BUILD_PYTHON specified "{override}" but it was not found on PATH or disk.')

    workspace_root = os.path.abspath(os.path.join(tauri_dir, ".."))
    candidates = [
        os.path.join(workspace_root, "Orange AI Desktop", "venv", "Scripts", "python.exe"),
        os.path.join(workspace_root, "desktop", "venv", "Scripts", "python.exe"),
        os.path.join(tauri_dir, "venv", "Scripts", "python.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c), f"local virtual environment ({c})"

    return sys.executable, "current interpreter (sys.executable)"


PYTHON_EXE, PYTHON_SOURCE = resolve_python_exe(TAURI_DIR)
SPEC_FILE = os.path.join(TAURI_DIR, "scripts", "runtime_adapter.spec")
WORK_DIR = os.path.join(TAURI_DIR, "build_runtime")
DIST_DIR = os.path.join(TAURI_DIR, "src-tauri", "resources")
OUTPUT_EXE = os.path.join(DIST_DIR, "runtime", "runtime_adapter.exe")


def build_runtime():
    print("==================================================================")
    print("--- [WORKSTREAM B] COMPILING STANDALONE PYTHON RUNTIME (PYINSTALLER) ---")
    print("==================================================================")
    print(f"Python interpreter: {PYTHON_EXE} [via {PYTHON_SOURCE}]")
    print(f"Spec file:   {SPEC_FILE}")
    print(f"Work dir:    {WORK_DIR}")
    print(f"Dist dir:    {DIST_DIR}")
    print(f"Target EXE:  {OUTPUT_EXE}")

    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(DIST_DIR, exist_ok=True)

    cmd = [
        PYTHON_EXE,
        "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath", WORK_DIR,
        "--distpath", DIST_DIR,
        SPEC_FILE,
    ]

    print(f"\nExecuting: {' '.join(cmd)}")
    t0 = time.time()
    res = subprocess.run(cmd, cwd=TAURI_DIR)
    duration = time.time() - t0

    if res.returncode != 0:
        print(f"\n[ERROR] PyInstaller compilation failed with code {res.returncode}")
        sys.exit(res.returncode)

    print(f"\n[SUCCESS] PyInstaller build completed in {duration:.1f}s")

    if not os.path.exists(OUTPUT_EXE):
        print(f"[ERROR] Expected output executable not found: {OUTPUT_EXE}")
        sys.exit(1)

    exe_size_mb = os.path.getsize(OUTPUT_EXE) / (1024 * 1024)
    print(f"[VERIFIED] Output binary present: {OUTPUT_EXE} ({exe_size_mb:.2f} MB)")


def verify_standalone_runtime():
    print("\n==================================================================")
    print("--- [WORKSTREAM B] VERIFYING STANDALONE RUNTIME VIA JSON-RPC ---")
    print("==================================================================")
    print(f"Testing binary: {OUTPUT_EXE}")

    # Launch standalone runtime without developer environment in PATH
    env = os.environ.copy()
    # Remove Python development paths from environment to guarantee standalone independence
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="orange_test_build_")
    env["LOCALAPPDATA"] = tmp_dir
    env["ORANGE_AI_USER_DATA_DIR"] = os.path.join(tmp_dir, "Orange AI")

    proc = subprocess.Popen(
        [OUTPUT_EXE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )

    try:
        # Read lines until we receive the response to test-ping-1
        ping_req = json.dumps({"jsonrpc": "2.0", "id": "test-ping-1", "method": "system.ping", "params": {}}) + "\n"
        print(f"Sending JSON-RPC request to standalone stdin: {ping_req.strip()}")
        proc.stdin.write(ping_req)
        proc.stdin.flush()

        # Read JSON-RPC responses (filtering out initial notifications like runtime.ready)
        resp = None
        for _ in range(5):
            line = proc.stdout.readline().strip()
            if not line:
                continue
            print(f"Received JSON-RPC line from standalone stdout:\n  {line}")
            data = json.loads(line)
            if data.get("id") == "test-ping-1":
                resp = data
                break
            elif "method" in data:
                print(f"  [Notification Event] {data['method']}")

        if not resp or "result" not in resp:
            raise ValueError(f"Did not receive valid ping response: {resp}")

        result = resp["result"]
        print(f"\n[TEST RESULT] Standalone Ping Succeeded!")
        print(f"  Version:   {result.get('version')}")
        print(f"  Status:    {result.get('status')}")
        print(f"  PID:       {result.get('pid')}")
        print(f"  Data Dir:  {result.get('user_data_dir')}")

        # Send get_health request
        health_req = json.dumps({"jsonrpc": "2.0", "id": "test-health-1", "method": "system.health", "params": {}}) + "\n"
        proc.stdin.write(health_req)
        proc.stdin.flush()

        health_resp = None
        for _ in range(5):
            line = proc.stdout.readline().strip()
            if not line:
                continue
            print(f"Received JSON-RPC line for health:\n  {line}")
            data = json.loads(line)
            if data.get("id") == "test-health-1":
                health_resp = data
                break

        if not health_resp or "result" not in health_resp:
            raise ValueError(f"Did not receive valid health response: {health_resp}")

        print(f"\n[TEST RESULT] Standalone Health Succeeded!")
        print(f"  Health:    {health_resp.get('result', {}).get('health')}")
        print(f"  API:       {json.dumps(health_resp.get('result', {}).get('api'))}")

        # Clean shutdown via EOF
        proc.stdin.close()
        proc.wait(timeout=5)
        print(f"\n[SUCCESS] Standalone runtime verified and shut down cleanly.")

    except Exception as e:
        print(f"[ERROR] Standalone verification failed: {e}")
        proc.kill()
        sys.exit(1)


if __name__ == "__main__":
    build_runtime()
    verify_standalone_runtime()
