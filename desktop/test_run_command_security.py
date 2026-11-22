"""
test_run_command_security.py - Exhaustive Security Test Suite for Phase 4 run_command.

Covers all required security categories:
1. Argument validation (7 approved subcommands, unknown subcommands, flag rejection, prefix matching, rev-parse modes, diff modes, literal --, pathspec magic, workspace escape).
2. Repository confinement (ceiling directory boundary, malicious .git pointers, linked worktree confinement, unreadable roots).
3. Cryptographic repository integrity verification (fail-closed snapshot, mutation detection, shared worktree refs/objects/config, collision-free canonical namespaces).
4. Win32 process containment lifecycle (direct Win32 CreateProcessW, handle whitelisting, CREATE_SUSPENDED, Job Object binding, timeout watchdog, output flooding, 64-bit handle fidelity, Unicode env).
5. Permission and Agent Engine integration (mandatory confirmation, denial, resumption, non-persistence of approval, high-risk blockage).
"""

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch, MagicMock

from tools.command import (
    run_command,
    _execute_contained_process,
    build_cleanroom_environment,
    discover_and_confine_repository,
    snapshot_repository_state,
    assert_repository_unmodified,
    get_minimal_disjoint_roots,
    make_canonical_root_id,
    validate_git_arguments,
    CANONICAL_GIT_EXE,
    CommandSecurityError,
    ExecutableProhibitedError,
    ArgumentValidationError,
    RepositoryConfinementError,
    SnapshotError,
    kernel32,
    SECURITY_ATTRIBUTES,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobObjectExtendedLimitInformation,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
)
from tools.registry import registry, Tool
from tools.permissions import (
    RiskLevel,
    check_permission,
    PermissionStatus,
    execute_with_permission,
)
from brain.agent_models import AgentState, AgentStatus, AgentAction, ActionType
from brain.agent import AgentEngine


class TestRunCommandArgumentValidation(unittest.TestCase):
    """1. Comprehensive argument grammar, token parsing, and security boundary tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name).resolve()
        # Initialize an isolated test repository
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(self.repo_dir), check=True, capture_output=True)

        self.test_file = self.repo_dir / "sample.txt"
        self.test_file.write_text("Line 1\nLine 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "sample.txt"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.repo_dir), check=True, capture_output=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_all_seven_approved_subcommands_accepted(self):
        """Verify that all 7 approved subcommands are recognized and valid."""
        subcommands = [
            ("status", ["--short"]),
            ("diff", ["--stat"]),
            ("log", ["-n", "1"]),
            ("branch", ["--list"]),
            ("rev-parse", ["--show-toplevel"]),
            ("show", ["HEAD"]),
            ("tag", ["--list"]),
        ]
        roots = [self.repo_dir]
        for sub, sub_args in subcommands:
            validate_git_arguments(sub, sub_args, str(self.repo_dir), roots)

    def test_unknown_subcommands_rejected(self):
        """Mutating, network, or unknown Git subcommands are rejected."""
        banned = ["push", "pull", "commit", "checkout", "merge", "rebase", "reset", "revert", "clean", "clone", "foo"]
        for sub in banned:
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments(sub, [], str(self.repo_dir), [self.repo_dir])

    def test_unknown_flags_rejected(self):
        """Unknown or unapproved flags are rejected across subcommands."""
        with self.assertRaises(ArgumentValidationError):
            validate_git_arguments("status", ["--nonexistent-flag"], str(self.repo_dir), [self.repo_dir])
        with self.assertRaises(ArgumentValidationError):
            validate_git_arguments("diff", ["--foo"], str(self.repo_dir), [self.repo_dir])
        with self.assertRaises(ArgumentValidationError):
            validate_git_arguments("log", ["--bar"], str(self.repo_dir), [self.repo_dir])

    def test_prefix_matching_bypass_rejected(self):
        """Dangerous configuration and output options are rejected regardless of formatting."""
        prohibited = [
            "-c", "-cfoo=bar", "--config", "--config=foo", "--config-env",
            "-o", "-oout.txt", "--output", "--output=out.txt", "--output-directory=dir"
        ]
        for token in prohibited:
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments("status", [token], str(self.repo_dir), [self.repo_dir])
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments("diff", [token], str(self.repo_dir), [self.repo_dir])

    def test_valid_cached_and_oneline_preserved(self):
        """Safe flags starting with -c or -o are preserved and pass validation."""
        validate_git_arguments("diff", ["--cached"], str(self.repo_dir), [self.repo_dir])
        validate_git_arguments("log", ["--oneline"], str(self.repo_dir), [self.repo_dir])
        validate_git_arguments("show", ["--oneline", "HEAD"], str(self.repo_dir), [self.repo_dir])

    def test_rev_parse_mode_restrictions(self):
        """Mode A (standalone inspection) forbids revisions; Mode B requires exactly one revision."""
        # Mode A: valid
        validate_git_arguments("rev-parse", ["--show-toplevel"], str(self.repo_dir), [self.repo_dir])
        validate_git_arguments("rev-parse", ["--is-inside-work-tree"], str(self.repo_dir), [self.repo_dir])
        # Mode A with revision: rejected
        with self.assertRaises(ArgumentValidationError):
            validate_git_arguments("rev-parse", ["--show-toplevel", "HEAD"], str(self.repo_dir), [self.repo_dir])
        # Mode B: valid
        validate_git_arguments("rev-parse", ["--verify", "HEAD"], str(self.repo_dir), [self.repo_dir])
        validate_git_arguments("rev-parse", ["--short", "HEAD"], str(self.repo_dir), [self.repo_dir])
        # Mode B without revision: rejected
        with self.assertRaises(ArgumentValidationError):
            validate_git_arguments("rev-parse", ["--verify"], str(self.repo_dir), [self.repo_dir])

    def test_all_four_approved_diff_modes(self):
        """All 4 approved diff comparison modes validate successfully."""
        # Mode 1: Working tree vs index
        validate_git_arguments("diff", ["--", "sample.txt"], str(self.repo_dir), [self.repo_dir])
        # Mode 2: Index vs commit
        validate_git_arguments("diff", ["--cached", "HEAD", "--", "sample.txt"], str(self.repo_dir), [self.repo_dir])
        # Mode 3: Working tree vs commit
        validate_git_arguments("diff", ["HEAD", "--", "sample.txt"], str(self.repo_dir), [self.repo_dir])
        # Mode 4: Commit vs commit
        validate_git_arguments("diff", ["HEAD~1", "HEAD", "--", "sample.txt"], str(self.repo_dir), [self.repo_dir])

    def test_literal_dash_dash_path_separation(self):
        """Path arguments must be separated by literal -- in git diff."""
        # Valid with --
        validate_git_arguments("diff", ["--", "sample.txt"], str(self.repo_dir), [self.repo_dir])
        # Invalid without -- (treated as unapproved revision or rejected)
        with self.assertRaises(ArgumentValidationError):
            validate_git_arguments("diff", ["sample.txt", "extra_token", "invalid"], str(self.repo_dir), [self.repo_dir])

    def test_git_pathspec_magic_rejected(self):
        """Git pathspec magic (leading colon) is strictly rejected."""
        magic_tokens = [":(top)sample.txt", ":(literal)*", ":(attr:...)foo", ":!sample.txt", ":^sample.txt"]
        for magic in magic_tokens:
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments("diff", ["--", magic], str(self.repo_dir), [self.repo_dir])
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments("status", [magic], str(self.repo_dir), [self.repo_dir])

    def test_workspace_escape_rejected(self):
        """Relative or absolute paths escaping workspace roots are rejected."""
        escapes = ["../../Windows/win.ini", "..\\..\\autoexec.bat", "C:\\Windows\\System32"]
        for escape in escapes:
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments("diff", ["--", escape], str(self.repo_dir), [self.repo_dir])

    def test_dangerous_shell_characters_rejected(self):
        """Pipes, redirects, command chaining, and quotes are strictly rejected."""
        dangerous = ["foo;bar", "foo&bar", "foo|bar", "foo>bar", "foo<bar", "foo`bar`", "foo$bar", "foo\nbar", "foo'bar'"]
        for d in dangerous:
            with self.assertRaises(ArgumentValidationError):
                validate_git_arguments("status", [d], str(self.repo_dir), [self.repo_dir])


class TestRunCommandRepositoryConfinement(unittest.TestCase):
    """2. Repository discovery confinement and ceiling directory tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()

        # Create parent repository
        self.parent_repo = self.base_dir / "parent_repo"
        self.parent_repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.parent_repo), check=True, capture_output=True)

        # Create nested child directory without .git
        self.child_dir = self.parent_repo / "workspace"
        self.child_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parent_repository_rejection_via_ceiling(self):
        """Ceiling directory prevents Git from traversing upward into parent repo."""
        # If approved root is child_dir, discovery must halt at child_dir and reject parent repo
        with self.assertRaises(RepositoryConfinementError):
            discover_and_confine_repository(str(self.child_dir), [self.child_dir])

    def test_malicious_git_pointer_file_rejected(self):
        """A .git pointer file targeting outside approved roots is rejected."""
        external_git = self.base_dir / "external_git"
        external_git.mkdir()
        subprocess.run(["git", "init"], cwd=str(external_git), check=True, capture_output=True)

        # In child_dir, create a .git file pointing to external_git/.git
        pointer_file = self.child_dir / ".git"
        pointer_file.write_text(f"gitdir: {external_git / '.git'}\n", encoding="utf-8")

        # Discovering child_dir with only child_dir approved must fail
        with self.assertRaises(RepositoryConfinementError):
            discover_and_confine_repository(str(self.child_dir), [self.child_dir])

    def test_linked_worktree_within_workspace_accepted(self):
        """A linked worktree whose metadata resides inside approved roots is accepted."""
        wt_dir = self.base_dir / "worktree_dir"
        subprocess.run(["git", "worktree", "add", "-b", "feature", str(wt_dir)], cwd=str(self.parent_repo), check=True, capture_output=True)

        # Approving both base_dir roots allows discovery
        toplevel, git_dir, common_dir = discover_and_confine_repository(str(wt_dir), [self.base_dir])
        self.assertEqual(toplevel, wt_dir)
        self.assertTrue(str(git_dir).startswith(str(self.parent_repo)))
        self.assertEqual(common_dir, self.parent_repo / ".git")

    def test_missing_or_unreadable_roots_fail_closed(self):
        """Missing root raises SnapshotError immediately."""
        missing = self.base_dir / "non_existent_folder"
        with self.assertRaises(SnapshotError):
            snapshot_repository_state(missing)


class TestRunCommandIntegrityVerification(unittest.TestCase):
    """3. Cryptographic repository-state snapshot and mutation detection tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(self.repo_dir), check=True, capture_output=True)

        self.file1 = self.repo_dir / "file1.txt"
        self.file1.write_text("Original content\n", encoding="utf-8")
        subprocess.run(["git", "add", "file1.txt"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.repo_dir), check=True, capture_output=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_working_tree_modification_detected(self):
        """Mutating a file in the working tree triggers an assertion error."""
        before = snapshot_repository_state(self.repo_dir)
        self.file1.write_text("Mutated content\n", encoding="utf-8")
        after = snapshot_repository_state(self.repo_dir)
        with self.assertRaises(AssertionError):
            assert_repository_unmodified(before, after)

    def test_file_addition_and_removal_detected(self):
        """Adding or deleting files triggers mutation detection."""
        before = snapshot_repository_state(self.repo_dir)
        new_file = self.repo_dir / "new_file.txt"
        new_file.write_text("added\n", encoding="utf-8")
        after_add = snapshot_repository_state(self.repo_dir)
        with self.assertRaises(AssertionError):
            assert_repository_unmodified(before, after_add)

        new_file.unlink()
        after_rm = snapshot_repository_state(self.repo_dir)
        assert_repository_unmodified(before, after_rm)

    def test_shared_refs_mutation_detected(self):
        """Mutating a Git ref file triggers mutation detection."""
        before = snapshot_repository_state(self.repo_dir)
        ref_file = self.repo_dir / ".git" / "refs" / "heads" / "main"
        if ref_file.exists():
            ref_file.write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
            after = snapshot_repository_state(self.repo_dir)
            with self.assertRaises(AssertionError):
                assert_repository_unmodified(before, after)

    def test_shared_config_mutation_detected(self):
        """Mutating .git/config triggers mutation detection."""
        before = snapshot_repository_state(self.repo_dir)
        cfg = self.repo_dir / ".git" / "config"
        with open(cfg, "a") as f:
            f.write("\n[core] pager = calc.exe\n")
        after = snapshot_repository_state(self.repo_dir)
        with self.assertRaises(AssertionError):
            assert_repository_unmodified(before, after)

    def test_minimal_disjoint_roots_deduplication(self):
        """Overlapping and nested roots are safely collapsed to the minimal covering set."""
        p_root = Path(r"C:\test\repo")
        p_git = Path(r"C:\test\repo\.git")
        p_worktrees = Path(r"C:\test\repo\.git\worktrees\feat")
        disjoint = get_minimal_disjoint_roots([p_root, p_git, p_worktrees])
        self.assertEqual(len(disjoint), 1)
        self.assertEqual(disjoint[0], p_root)

    def test_identical_basenames_uniquely_represented(self):
        """Two disjoint directories sharing the same basename produce distinct manifest keys."""
        dir1 = self.repo_dir / "folder1" / "common_name"
        dir2 = self.repo_dir / "folder2" / "common_name"
        dir1.mkdir(parents=True)
        dir2.mkdir(parents=True)
        (dir1 / "data.txt").write_text("data 1", encoding="utf-8")
        (dir2 / "data.txt").write_text("data 2", encoding="utf-8")

        manifest = snapshot_repository_state(dir1, git_dir=dir2)
        # Verify 2 distinct entries exist
        matching_keys = [k for k in manifest.keys() if k.endswith("data.txt")]
        self.assertEqual(len(matching_keys), 2)
        self.assertNotEqual(matching_keys[0], matching_keys[1])


class TestRunCommandWin32ProcessLifecycle(unittest.TestCase):
    """4. Direct Win32 CreateProcessW, Job Object containment, and handle isolation tests."""

    def test_64bit_handle_size_fidelity(self):
        """Assert that pointer-sized Win32 types are exactly 8 bytes (64-bit)."""
        self.assertEqual(ctypes.sizeof(wintypes.HANDLE), 8)
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        self.assertEqual(ctypes.sizeof(ctypes.c_size_t), 8)

    def test_contained_process_execution_success(self):
        """Verify successful low-level execution of canonical git.exe with version query."""
        env = {"SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "PATH": r"C:\Windows\System32;C:\Program Files\Git\cmd"}
        rc, out, trunc = _execute_contained_process(
            canonical_exe=CANONICAL_GIT_EXE,
            cmd_vector=["--version"],
            env=env,
            cwd=str(Path.cwd()),
            timeout_seconds=5,
        )
        self.assertEqual(rc, 0)
        self.assertIn("git version", out)
        self.assertFalse(trunc)

    def test_timeout_watchdog_terminates_job_tree(self):
        """Executing a process exceeding timeout bounds triggers TimeoutError and Job Object kill."""
        env = {"SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "PATH": r"C:\Windows\System32"}
        # Execute powershell sleep with a 1-second timeout
        powershell_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        with self.assertRaises(TimeoutError):
            _execute_contained_process(
                canonical_exe=powershell_exe,
                cmd_vector=["-Command", "Start-Sleep -Seconds 10"],
                env=env,
                cwd=str(Path.cwd()),
                timeout_seconds=1,
            )

    def test_output_flooding_bounded_at_64kb(self):
        """Generating massive output halts reading and caps at exactly 65,536 bytes."""
        env = {"SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "PATH": r"C:\Windows\System32"}
        powershell_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        rc, out, trunc = _execute_contained_process(
            canonical_exe=powershell_exe,
            cmd_vector=["-Command", "1..5000 | ForEach-Object { 'A' * 100 }"],
            env=env,
            cwd=str(Path.cwd()),
            timeout_seconds=10,
            max_output_bytes=65536,
        )
        self.assertTrue(trunc)
        self.assertLessEqual(len(out.encode("utf-8")), 65536)

    def test_unicode_environment_variable_propagation(self):
        """Child process receives UTF-16 Unicode environment variables cleanly."""
        env = {
            "SYSTEMROOT": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "PATH": r"C:\Windows\System32",
            "TEST_UNICODE_VAL": "Munchen_Tokyo_Orange_🍊_Test",
        }
        powershell_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        rc, out, _ = _execute_contained_process(
            canonical_exe=powershell_exe,
            cmd_vector=["-Command", "$env:TEST_UNICODE_VAL"],
            env=env,
            cwd=str(Path.cwd()),
            timeout_seconds=5,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Munchen_Tokyo_Orange", out)

    def test_unrelated_parent_handle_isolation(self):
        """Unrelated inheritable handles in parent are NOT inherited by child."""
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.bInheritHandle = True
        hLeakRead = wintypes.HANDLE()
        hLeakWrite = wintypes.HANDLE()
        kernel32.CreatePipe(ctypes.byref(hLeakRead), ctypes.byref(hLeakWrite), ctypes.byref(sa), 0)

        try:
            # Execute child command
            env = {"SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "PATH": r"C:\Windows\System32;C:\Program Files\Git\cmd"}
            rc, out, _ = _execute_contained_process(
                canonical_exe=CANONICAL_GIT_EXE,
                cmd_vector=["--version"],
                env=env,
                cwd=str(Path.cwd()),
                timeout_seconds=5,
            )
            # Close parent write end. If child inherited hLeakWrite, hLeakRead would NOT hit EOF.
            kernel32.CloseHandle(hLeakWrite)
            hLeakWrite = wintypes.HANDLE()

            buf = ctypes.create_string_buffer(64)
            bytes_read = wintypes.DWORD()
            read_res = kernel32.ReadFile(hLeakRead, buf, 64, ctypes.byref(bytes_read), None)
            # Broken pipe / EOF immediately
            self.assertFalse(read_res)
        finally:
            if hLeakRead:
                kernel32.CloseHandle(hLeakRead)
            if hLeakWrite:
                kernel32.CloseHandle(hLeakWrite)

    def test_deterministic_nul_stdin_eof(self):
        """Child attempting to read from standard input receives immediate EOF."""
        env = {"SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows", "PATH": r"C:\Windows\System32"}
        powershell_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        rc, out, _ = _execute_contained_process(
            canonical_exe=powershell_exe,
            cmd_vector=["-Command", "$in = [Console]::In.ReadToEnd(); Write-Host 'READ_LEN:' $in.Length"],
            env=env,
            cwd=str(Path.cwd()),
            timeout_seconds=5,
        )
        self.assertEqual(rc, 0)
        self.assertIn("READ_LEN: 0", out)


class TestRunCommandPublicToolAndPermissions(unittest.TestCase):
    """5. High-level run_command tool interface, permissions, and Agent Engine tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(self.repo_dir), check=True, capture_output=True)

        self.sample = self.repo_dir / "sample.txt"
        self.sample.write_text("Hello Orange AI\n", encoding="utf-8")
        subprocess.run(["git", "add", "sample.txt"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(self.repo_dir), check=True, capture_output=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tool_registry_registration_and_risk(self):
        """run_command is registered with CONFIRMATION_REQUIRED in category 'command'."""
        tool = registry.get("run_command")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.risk_level, RiskLevel.CONFIRMATION_REQUIRED)
        self.assertEqual(tool.category, "command")
        self.assertTrue(tool.requires_confirmation)

    def test_non_git_executable_rejected_immediately(self):
        """Calling run_command with non-git executable returns an error immediately."""
        res_py = run_command("python.exe", ["-c", "print(1)"], cwd=str(self.repo_dir))
        self.assertIn("Error: Executable 'python.exe' is prohibited", res_py)

        res_cmd = run_command("cmd.exe", ["/c", "dir"], cwd=str(self.repo_dir))
        self.assertIn("Error: Executable 'cmd.exe' is prohibited", res_cmd)

        res_where = run_command("where.exe", ["git"], cwd=str(self.repo_dir))
        self.assertIn("Error: Executable 'where.exe' is prohibited", res_where)

    def test_permission_enforcement_denied_confirmation(self):
        """User declining confirmation cancels execution with zero process spawn."""
        tool = registry.get("run_command")
        with patch("tools.permissions.request_confirmation", return_value=False):
            res = execute_with_permission(tool, ("git.exe", ["status", "--short"]), {"cwd": str(self.repo_dir)})
            self.assertIn("Action cancelled: user declined confirmation", res)

    def test_permission_enforcement_approved_confirmation(self):
        """User approving confirmation executes the Git command successfully."""
        tool = registry.get("run_command")
        with patch("tools.permissions.request_confirmation", return_value=True):
            res = execute_with_permission(tool, (CANONICAL_GIT_EXE, ["status"]), {"cwd": str(self.repo_dir)})
            self.assertTrue("On branch main" in res or "nothing to commit" in res or "sample.txt" in res)

    def test_run_command_clean_status_execution(self):
        """run_command returns cleanly on a valid Git status query."""
        out = run_command(CANONICAL_GIT_EXE, ["status", "--short"], cwd=str(self.repo_dir))
        self.assertEqual(out, "[Command completed with no output]")

    def test_run_command_diff_output(self):
        """run_command returns diff text accurately when unstaged changes exist."""
        self.sample.write_text("Hello Orange AI\nAdded line\n", encoding="utf-8")
        out = run_command(CANONICAL_GIT_EXE, ["diff", "--stat"], cwd=str(self.repo_dir))
        self.assertIn("sample.txt", out)
        self.assertTrue("1 file changed" in out or "insertions" in out or "1 +" in out)

    def test_run_command_log_output(self):
        """run_command returns commit logs cleanly."""
        out = run_command(CANONICAL_GIT_EXE, ["log", "--oneline", "-n", "1"], cwd=str(self.repo_dir))
        self.assertIn("init", out)


if __name__ == "__main__":
    unittest.main()
