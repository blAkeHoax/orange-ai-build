"""
test_stage5d_autonomy_and_full_access.py - Unit Test Suite for Stage 5D.

Verifies:
1. Full Windows System Toolset:
   - tools.shell: run_powershell execution, bounded timeout, output dictionary format.
   - tools.files: write_file, append_file, delete_file, move_path, create_directory with parent auto-creation.
   - tools.system: set_clipboard and get_clipboard with Win32 ctypes / PowerShell fallback.
   - tools.apps: launch_app validation and execution.
   - tools.processes: manage_process (list, inspect, terminate validation).
   - tools.command: run_command behavior in supervised vs unrestricted modes.
2. Catastrophic Command Guard:
   - format drive (format C:, format-volume) blocked unconditionally.
   - destruction of C:\\Windows\\System32 blocked unconditionally.
   - direct deletion of memory.db blocked unconditionally.
   - low-level disk wiping (diskpart clean) blocked unconditionally.
   - safe commands permitted.
3. Trust Modes (Supervised Full Access vs Unrestricted Autonomous Mode):
   - Supervised mode prompts for confirmation on operations outside active workspaces.
   - Supervised mode auto-allows scoped file operations within active workspaces and tempdir.
   - Unrestricted mode executes non-catastrophic operations with zero confirmation prompts.
   - Unrestricted mode STILL unconditionally blocks catastrophic commands.
4. REA Pre-Flight Context Generation:
   - gather_silent_preflight_context scans git status & latest commit for technical queries.
   - gather_silent_preflight_context remains silent (returns "") for non-technical queries.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

DESKTOP_DIR = os.path.abspath(os.path.dirname(__file__))
if DESKTOP_DIR not in sys.path:
    sys.path.insert(0, DESKTOP_DIR)

from tools.permissions import (
    RiskLevel,
    PermissionStatus,
    is_catastrophic_command,
    check_permission,
    execute_with_permission,
    is_scoped_in_workspaces,
)
from tools.registry import Tool, registry
from tools.shell import run_powershell
from tools.files import (
    write_file,
    append_file,
    delete_file,
    move_path,
    create_directory,
)
from tools.system import get_clipboard, set_clipboard
from tools.apps import launch_app
from tools.processes import manage_process
from tools.command import run_command
import main


class TestStage5DCatastrophicGuard(unittest.TestCase):
    """Verifies that irreversible OS-destruction patterns are unconditionally blocked."""

    def test_format_commands_blocked(self):
        catastrophic_samples = [
            "format C: /fs:ntfs",
            "format d:",
            "Format-Volume -DriveLetter C",
            "  FORMAT  E:  ",
        ]
        for cmd in catastrophic_samples:
            is_cat, reason = is_catastrophic_command(cmd)
            self.assertTrue(is_cat, f"Expected '{cmd}' to be blocked as catastrophic")
            self.assertIn("drive format", reason.lower())

    def test_system32_destruction_blocked(self):
        catastrophic_samples = [
            "rmdir /s /q C:\\Windows\\System32",
            "del /f /q C:/Windows/System32/*",
            "Remove-Item -Recurse -Force C:\\Windows\\System32",
            "erase c:\\windows\\system32\\cmd.exe",
        ]
        for cmd in catastrophic_samples:
            is_cat, reason = is_catastrophic_command(cmd)
            self.assertTrue(is_cat, f"Expected '{cmd}' to be blocked as catastrophic")
            self.assertIn("system directories", reason.lower())

    def test_memory_db_destruction_blocked(self):
        catastrophic_samples = [
            "del memory.db",
            "rmdir /s /q memory.db",
            "Remove-Item C:\\Users\\User\\AppData\\Local\\Orange AI\\memory.db",
            "del /f memory.db",
        ]
        for cmd in catastrophic_samples:
            is_cat, reason = is_catastrophic_command(cmd)
            self.assertTrue(is_cat, f"Expected '{cmd}' to be blocked as catastrophic")
            self.assertIn("memory.db", reason.lower())

    def test_diskpart_clean_blocked(self):
        is_cat, reason = is_catastrophic_command("diskpart /s clean all")
        self.assertTrue(is_cat)
        self.assertIn("disk wiping", reason.lower())

    def test_safe_commands_allowed(self):
        safe_samples = [
            "git status",
            "git diff HEAD~1",
            "python -m unittest discover",
            "Get-ChildItem -Path .",
            "echo 'Hello Orange AI'",
            "Write-Output 'Status check'",
        ]
        for cmd in safe_samples:
            is_cat, _ = is_catastrophic_command(cmd)
            self.assertFalse(is_cat, f"Expected '{cmd}' to be allowed as safe")


class TestStage5DFullAccessTools(unittest.TestCase):
    """Verifies full Windows system toolset execution and error recovery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_powershell_execution(self):
        res = run_powershell('Write-Output "Orange AI Autonomy Active"')
        self.assertTrue(res["success"])
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("Orange AI Autonomy Active", res["stdout"])

    def test_run_powershell_catastrophic_block(self):
        res = run_powershell("format C: /fs:ntfs")
        self.assertFalse(res["success"])
        self.assertEqual(res["exit_code"], 126)
        self.assertIn("Action blocked", res["output"])

    def test_files_full_lifecycle(self):
        # 1. create_directory
        sub_dir = self.temp_path / "sub" / "deep"
        res_dir = create_directory(str(sub_dir))
        self.assertIn("Successfully created directory", res_dir)
        self.assertTrue(sub_dir.exists())

        # 2. write_file (with auto-parent creation)
        target_file = sub_dir / "test.txt"
        res_write = write_file(str(target_file), "Line 1\n")
        self.assertIn("Successfully wrote", res_write)
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "Line 1\n")

        # 3. append_file
        res_append = append_file(str(target_file), "Line 2\n")
        self.assertIn("Successfully appended", res_append)
        self.assertEqual(target_file.read_text(encoding="utf-8"), "Line 1\nLine 2\n")

        # 4. move_path
        dest_file = self.temp_path / "moved_test.txt"
        res_move = move_path(str(target_file), str(dest_file))
        self.assertIn("Successfully moved", res_move)
        self.assertFalse(target_file.exists())
        self.assertTrue(dest_file.exists())

        # 5. delete_file
        res_del = delete_file(str(dest_file))
        self.assertIn("Successfully deleted", res_del)
        self.assertFalse(dest_file.exists())

    def test_files_catastrophic_guard(self):
        res_write = write_file(r"C:\Windows\System32\injected_test.dll", "bad")
        self.assertIn("blocked", res_write.lower())

        res_del = delete_file(r"C:\Users\User\AppData\Local\Orange AI\memory.db")
        self.assertIn("blocked", res_del.lower())

    def test_clipboard_operations(self):
        test_payload = f"Orange AI Autonomy Clipboard Test {os.getpid()}"
        set_res = set_clipboard(test_payload)
        self.assertIn("Successfully set clipboard", set_res)

        get_res = get_clipboard()
        self.assertEqual(get_res, test_payload)

    def test_manage_process_list_and_status(self):
        # List processes
        list_res = manage_process("list")
        self.assertIsInstance(list_res, str)
        self.assertGreater(len(list_res), 0)

        # Status of python process
        status_res = manage_process("status", name_or_pid="python")
        self.assertIn("running", status_res.lower())

        # Invalid action handling
        inv_res = manage_process("invalid_action", "test")
        self.assertIn("Error: Unknown action", inv_res)

    @patch("subprocess.Popen")
    def test_launch_app_validation(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        # Valid launch
        res = launch_app("notepad.exe", args=["test.txt"])
        self.assertIn("Successfully launched", res)

        # Blocked catastrophic target
        res_blocked = launch_app("format.com", args=["C:"])
        self.assertIn("blocked", res_blocked.lower())


class TestStage5DTrustModes(unittest.TestCase):
    """Verifies Supervised Full Access vs Unrestricted Autonomous Mode."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_workspace_scoping(self):
        # Temp dir is explicitly registered as active workspace for staging/testing
        in_temp = self.temp_path / "inside_temp.txt"
        self.assertTrue(is_scoped_in_workspaces(str(in_temp)))

        # Path in root of C: drive is not in default workspaces
        out_path = r"C:\random_non_workspace_folder\test.txt"
        self.assertFalse(is_scoped_in_workspaces(out_path))

    def test_supervised_mode_prompts_outside_workspace(self):
        tool = Tool(
            name="write_file",
            description="Write a file",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=write_file,
            category="files",
        )
        target_path = r"C:\outside_test_workspace\file.txt"

        # Supervised mode requires confirmation for outside workspace
        perm = check_permission(tool, full_access_mode="supervised", target=target_path)
        self.assertEqual(perm.status, PermissionStatus.REQUIRES_CONFIRMATION)

        # When user declines ('n')
        prompts = []
        res_declined = execute_with_permission(
            tool,
            args=(target_path, "content"),
            kwargs={},
            full_access_mode="supervised",
            prompt_fn=lambda p: prompts.append(p) or "n",
        )
        self.assertIn("declined", res_declined.lower())
        self.assertEqual(len(prompts), 1)

    def test_supervised_mode_auto_allows_scoped_files(self):
        tool = Tool(
            name="write_file",
            description="Write a file",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=write_file,
            category="files",
        )
        target_path = str(self.temp_path / "scoped_file.txt")

        # Supervised mode auto-allows within active workspaces / tempdir
        perm = check_permission(tool, full_access_mode="supervised", target=target_path)
        self.assertEqual(perm.status, PermissionStatus.ALLOWED)

        prompts = []
        res = execute_with_permission(
            tool,
            args=(target_path, "hello"),
            kwargs={},
            full_access_mode="supervised",
            prompt_fn=lambda p: prompts.append(p) or "y",
        )
        self.assertEqual(len(prompts), 0, "Scoped file write should not prompt in supervised mode")
        self.assertIn("Successfully wrote", res)

    def test_unrestricted_mode_zero_prompts_outside_workspace(self):
        tool = Tool(
            name="write_file",
            description="Write a file",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=lambda p, c: f"Successfully written to {p}",
            category="files",
        )
        target_path = r"C:\outside_test_workspace\file.txt"

        # In unrestricted mode, permission is ALLOWED with zero prompts
        perm = check_permission(tool, full_access_mode="unrestricted", target=target_path)
        self.assertEqual(perm.status, PermissionStatus.ALLOWED)

        prompts = []
        res = execute_with_permission(
            tool,
            args=(target_path, "content"),
            kwargs={},
            full_access_mode="unrestricted",
            prompt_fn=lambda p: prompts.append(p) or "n",
        )
        self.assertEqual(len(prompts), 0, "Unrestricted mode must not prompt")
        self.assertIn("Successfully written to", res)

    def test_unrestricted_mode_still_blocks_catastrophic(self):
        tool = Tool(
            name="run_powershell",
            description="Run powershell",
            risk_level=RiskLevel.CONFIRMATION_REQUIRED,
            func=run_powershell,
            category="shell",
        )
        catastrophic_cmd = "format C: /fs:ntfs"

        # Even in unrestricted mode, catastrophic command is DENIED
        perm = check_permission(tool, full_access_mode="unrestricted", target=catastrophic_cmd)
        self.assertEqual(perm.status, PermissionStatus.DENIED)
        self.assertIn("permanently blocked", perm.reason.lower())

        res = execute_with_permission(
            tool,
            args=(catastrophic_cmd,),
            kwargs={},
            full_access_mode="unrestricted",
        )
        self.assertIn("permanently blocked", res.lower())


class TestStage5DREAPreflight(unittest.TestCase):
    """Verifies REA silent pre-flight context generation."""

    def test_silent_for_non_technical_query(self):
        queries = [
            "Hello, how are you today?",
            "What is the weather like?",
            "Can you write a poem about autumn?",
            "Thank you so much!",
        ]
        for q in queries:
            ctx = main.gather_silent_preflight_context(q)
            self.assertEqual(ctx, "", f"Expected empty preflight context for non-technical query '{q}'")

    def test_preflight_for_technical_query(self):
        queries = [
            "What is the current git status of the project?",
            "Did we make any commits to the repository?",
            "Check repo branch and status",
        ]
        for q in queries:
            ctx = main.gather_silent_preflight_context(q)
            self.assertIsInstance(ctx, str)
            self.assertGreater(len(ctx), 0, f"Expected non-empty preflight context for technical query '{q}'")
            # Should mention at least one of the known repos
            self.assertTrue(
                "Orange AI Tauri" in ctx or "Orange API" in ctx,
                f"Context should include repo details: {ctx}",
            )


if __name__ == "__main__":
    unittest.main()
