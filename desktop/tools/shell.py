"""
tools/shell.py - Windows Shell & PowerShell Execution Tool for Orange AI Desktop.

Enables full Windows PowerShell and shell execution for autonomous development,
build scripts, package management (pip, npm, cargo, winget, git, gh), and system tasks.
Protected by the centralized catastrophic command guard and trust modes.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

from brain.logger import logger
from tools.permissions import is_catastrophic_command


def run_powershell(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Execute a PowerShell command with captured stdout, stderr, exit_code, and bounded timeout.

    Parameters:
    - command: PowerShell command string or script block.
    - cwd: Working directory (defaults to current working directory).
    - timeout: Execution timeout in seconds (default: 60s, max: 300s).

    Returns:
    Structured dict containing exit_code, stdout, stderr, output, and success status.
    """
    if not command or not isinstance(command, str) or not command.strip():
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "Error: Command must be a non-empty string.",
            "output": "Error: Command must be a non-empty string.",
            "success": False,
            "error": "Error: Command must be a non-empty string.",
            "command": str(command),
        }

    cmd_str = command.strip()

    # Catastrophic command safety guard
    is_cat, cat_reason = is_catastrophic_command(cmd_str)
    if is_cat:
        logger.warning("[SHELL] Blocked catastrophic PowerShell command: %s (Reason: %s)", cmd_str, cat_reason)
        return {
            "exit_code": 126,
            "stdout": "",
            "stderr": f"Action blocked: {cat_reason}",
            "output": f"Action blocked: {cat_reason}",
            "success": False,
            "error": f"Action blocked: {cat_reason}",
            "command": cmd_str,
        }

    # Bound timeout between 1s and 300s
    bounded_timeout = max(1, min(int(timeout), 300))

    # Resolve working directory
    resolved_cwd = None
    if cwd:
        try:
            cwd_path = Path(cwd).resolve()
            if cwd_path.is_dir():
                resolved_cwd = str(cwd_path)
            else:
                return {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"Error: Working directory does not exist: '{cwd}'",
                    "output": f"Error: Working directory does not exist: '{cwd}'",
                    "success": False,
                    "error": f"Working directory does not exist: '{cwd}'",
                    "command": cmd_str,
                }
        except Exception as e:
            logger.warning("[SHELL] Failed to resolve cwd '%s': %s", cwd, e)

    powershell_exe = "powershell.exe"
    # Execute non-interactively with bypassed execution policy
    cmd_args = [
        powershell_exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        cmd_str,
    ]

    start_time = time.time()
    try:
        proc = subprocess.run(
            cmd_args,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=bounded_timeout,
            shell=False,
        )
        elapsed = time.time() - start_time
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        exit_code = proc.returncode

        output_text = stdout
        if stderr:
            output_text = f"{stdout}\n[STDERR]\n{stderr}".strip() if stdout else stderr

        return {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "output": output_text or "(no output)",
            "success": (exit_code == 0),
            "error": None if exit_code == 0 else (stderr or f"Process exited with code {exit_code}"),
            "command": cmd_str,
            "elapsed_seconds": round(elapsed, 3),
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        logger.warning("[SHELL] Command timed out after %ds: %s", bounded_timeout, cmd_str)
        return {
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Command execution timed out after {bounded_timeout} seconds.",
            "output": f"Command execution timed out after {bounded_timeout} seconds.",
            "success": False,
            "error": f"TimeoutExpired: Command timed out after {bounded_timeout} seconds.",
            "command": cmd_str,
            "elapsed_seconds": round(elapsed, 3),
        }
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error("[SHELL] Execution error for command '%s': %s", cmd_str, e)
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": str(e),
            "output": f"Execution error: {e}",
            "success": False,
            "error": str(e),
            "command": cmd_str,
            "elapsed_seconds": round(elapsed, 3),
        }
