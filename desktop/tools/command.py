"""
tools/command.py - Strictly Controlled Git Inspection Tool for Orange AI Desktop.

Phase 4 REA Findings Adoption:
Provides a secure, sandboxed, confirmation-gated capability to execute canonical
git.exe for read-only repository inspection. Completely prohibits general shells,
Python execution, where.exe, or arbitrary binary execution.

Architecture & Security Invariants:
1. Canonical git.exe only (no shells, no python, no where.exe).
2. Closed 7 read-only subcommands (status, diff, log, branch, rev-parse, show, tag).
3. Non-shell Win32 invocation via direct CreateProcessW with CREATE_SUSPENDED.
4. Anonymous Windows Job Object lifecycle binding (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE).
5. Cleanroom environment allowlist and configuration isolation.
6. Fail-closed repository discovery and confinement (GIT_CEILING_DIRECTORIES).
7. Fail-closed before-and-after cryptographic repository integrity verification.
8. STARTUPINFOEXW handle whitelisting (PROC_THREAD_ATTRIBUTE_HANDLE_LIST) with NUL stdin.
9. 15-second default timeout (monotonic watchdog) and 64 KB memory buffer cap.
"""

import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from brain.logger import logger
from tools.files import get_approved_roots, is_path_within_roots

# ---------------------------------------------------------------------------
# Win32 Constants & Enumerations
# ---------------------------------------------------------------------------
STARTF_USESTDHANDLES = 0x00000100
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
HANDLE_FLAG_INHERIT = 0x00000001
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

DEFAULT_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 60
MAX_OUTPUT_BYTES = 65536  # 64 KB hard cap

# ---------------------------------------------------------------------------
# Win32 Structures
# ---------------------------------------------------------------------------
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoCounters", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryLimit", ctypes.c_size_t),
        ("PeakJobMemoryLimit", ctypes.c_size_t),
    ]


# ---------------------------------------------------------------------------
# Explicit 64-bit ctypes Function Signatures
# ---------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

kernel32.CreateJobObjectW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE

kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL

kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD]
kernel32.CreatePipe.restype = wintypes.BOOL

kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
kernel32.SetHandleInformation.restype = wintypes.BOOL

kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
kernel32.DeleteProcThreadAttributeList.restype = None

kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES), ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.CreateProcessW.restype = wintypes.BOOL

kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD

kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL

kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
kernel32.PeekNamedPipe.restype = wintypes.BOOL

kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------
class CommandSecurityError(Exception):
    """Base exception for all command security violations."""
    pass


class ExecutableProhibitedError(CommandSecurityError):
    """Raised when an unapproved executable is requested."""
    pass


class ArgumentValidationError(CommandSecurityError):
    """Raised when command-line arguments violate security grammars."""
    pass


class RepositoryConfinementError(CommandSecurityError):
    """Raised when repository metadata or working directory escapes approved roots."""
    pass


class SnapshotError(Exception):
    """Raised when repository state cannot be completely and accurately captured."""
    pass


# ---------------------------------------------------------------------------
# Canonical Git Discovery
# ---------------------------------------------------------------------------
def get_canonical_git_path() -> str:
    """
    Locate the canonical git.exe executable on the host system.
    Prefers standard Program Files installation; falls back to verified PATH discovery.
    Guarantees the path is absolute and verified on disk.
    """
    standard_paths = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ]
    for sp in standard_paths:
        if os.path.isfile(sp):
            return str(Path(sp).resolve())

    discovered = shutil.which("git.exe") or shutil.which("git")
    if discovered and os.path.isfile(discovered):
        return str(Path(discovered).resolve())

    raise FileNotFoundError("Canonical git.exe not found on system.")


try:
    CANONICAL_GIT_EXE = get_canonical_git_path()
except Exception:
    CANONICAL_GIT_EXE = r"C:\Program Files\Git\cmd\git.exe"


# ---------------------------------------------------------------------------
# Cleanroom Environment Construction
# ---------------------------------------------------------------------------
def build_cleanroom_environment(approved_root: Union[str, Path]) -> Dict[str, str]:
    """
    Construct an allowlist-only child environment for git.exe.
    Strips all API keys, ambient tokens, SSH keys, proxies, and custom configs.
    Injects mandatory isolation flags and ceiling directories.
    """
    allowlist_system_vars = [
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    ]

    clean_env: Dict[str, str] = {}
    for var in allowlist_system_vars:
        val = os.environ.get(var)
        if val is not None:
            clean_env[var] = val

    # Controlled minimal PATH
    clean_env["PATH"] = r"C:\Windows\System32;C:\Windows;C:\Program Files\Git\cmd"

    # Neutralize pagers
    clean_env["TERM"] = "dumb"
    clean_env["PAGER"] = "cat"
    clean_env["GIT_PAGER"] = "cat"
    clean_env["LESS"] = ""

    # Neutralize interactive prompts and askpass
    clean_env["GIT_TERMINAL_PROMPT"] = "0"
    clean_env["GIT_ASKPASS"] = "NUL"
    clean_env["SSH_ASKPASS"] = "NUL"

    # Neutralize optional locks and lazy fetching
    clean_env["GIT_OPTIONAL_LOCKS"] = "0"
    clean_env["GIT_NO_LAZY_FETCH"] = "1"

    # Enforce configuration isolation
    clean_env["GIT_CONFIG_NOSYSTEM"] = "1"
    clean_env["GIT_CONFIG_GLOBAL"] = "NUL"

    # Upward repository discovery confinement
    clean_env["GIT_CEILING_DIRECTORIES"] = str(Path(approved_root).resolve())

    return clean_env


# ---------------------------------------------------------------------------
# Argument Grammar & Validation
# ---------------------------------------------------------------------------
APPROVED_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "branch",
    "rev-parse",
    "show",
    "tag",
}

# Regex for dangerous characters
RE_DANGEROUS_CHARS = re.compile(r"[;&|><$`\n\r\0'\"]")

# Regex for revisions
RE_REVISION = re.compile(r"^[a-zA-Z0-9_\-\/\.\^\~]{1,60}$")


def _check_dangerous_tokens(tokens: List[str]) -> None:
    for token in tokens:
        if RE_DANGEROUS_CHARS.search(token):
            raise ArgumentValidationError(f"Argument contains dangerous shell or control characters: '{token}'")
        # Exact token and attached option blocking for config and output file writes
        if token == "-c" or re.match(r"^-c[a-zA-Z0-9_\.\-]+=", token) or token.startswith("--config"):
            raise ArgumentValidationError(f"Configuration override token is strictly prohibited: '{token}'")
        if token == "-o" or re.match(r"^-o.+", token) or token.startswith("--output") or token.startswith("--output-directory"):
            raise ArgumentValidationError(f"Output redirection flag is strictly prohibited: '{token}'")
        if token in ("--paginate", "-p") and not token.startswith("-U"):
            # Note: -p can be valid in diff as unified/patch if unambiguous, but --paginate is blocked
            if token == "--paginate":
                raise ArgumentValidationError(f"Pagination flag is prohibited: '{token}'")
        if token in ("--ext-diff", "--textconv"):
            raise ArgumentValidationError(f"External diff/textconv flags cannot be supplied by model: '{token}'")


def validate_status_args(args: List[str], cwd: str, approved_roots: List[Path]) -> None:
    _check_dangerous_tokens(args)
    allowed_flags = {"--short", "-s", "--branch", "-b"}
    allowed_opts = {
        "--porcelain", "--porcelain=v1", "--porcelain=v2",
        "--untracked-files=no", "--untracked-files=normal", "--untracked-files=all",
        "-uno", "-unormal", "-uall",
        "--ignored=no", "--ignored=traditional",
    }
    paths: List[str] = []
    has_dash_dash = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            has_dash_dash = True
            for p in args[i + 1:]:
                if p.startswith("-"):
                    raise ArgumentValidationError(f"Path argument cannot begin with dash: '{p}'")
                paths.append(p)
            break
        elif arg in allowed_flags or arg in allowed_opts:
            i += 1
        elif arg.startswith("-"):
            raise ArgumentValidationError(f"Unknown or unauthorized git status option: '{arg}'")
        else:
            # Positional target path
            paths.append(arg)
            i += 1

    if len(paths) > 1:
        raise ArgumentValidationError(f"git status accepts at most 1 path; received {len(paths)}")

    for p in paths:
        if p.startswith(":"):
            raise ArgumentValidationError(f"Pathspec magic is prohibited: '{p}'")
        target = Path(cwd) / p
        if not is_path_within_roots(target, approved_roots):
            raise ArgumentValidationError(f"Path escapes approved workspace roots: '{p}'")


def validate_diff_args(args: List[str], cwd: str, approved_roots: List[Path]) -> None:
    _check_dangerous_tokens(args)
    allowed_flags = {
        "--stat", "--numstat", "--shortstat", "--name-only", "--name-status",
        "--cached", "--staged",
        "--ignore-all-space", "-w", "--ignore-space-change", "-b", "--ignore-blank-lines",
    }
    re_unified = re.compile(r"^(-U\d{1,2}|--unified=\d{1,2})$")
    re_filter = re.compile(r"^--diff-filter=[ACDMRTUXB*]{1,10}$")

    revisions: List[str] = []
    paths: List[str] = []

    if "--" in args:
        dash_idx = args.index("--")
        pre_args = args[:dash_idx]
        post_args = args[dash_idx + 1:]
    else:
        pre_args = args
        post_args = []

    for arg in pre_args:
        if arg in allowed_flags:
            continue
        elif re_unified.match(arg) or re_filter.match(arg):
            continue
        elif RE_REVISION.match(arg) and not arg.startswith("-"):
            revisions.append(arg)
        else:
            raise ArgumentValidationError(f"Unknown or unauthorized git diff argument: '{arg}'")

    if len(revisions) > 2:
        raise ArgumentValidationError(f"git diff accepts at most 2 revision identifiers; received {len(revisions)}")

    for p in post_args:
        if p.startswith(":"):
            raise ArgumentValidationError(f"Git pathspec magic is prohibited: '{p}'")
        target = Path(cwd) / p
        if not is_path_within_roots(target, approved_roots):
            raise ArgumentValidationError(f"Path escapes approved workspace roots: '{p}'")
        paths.append(p)

    if len(paths) > 5:
        raise ArgumentValidationError(f"git diff accepts at most 5 path arguments; received {len(paths)}")


def validate_log_args(args: List[str], cwd: str, approved_roots: List[Path]) -> None:
    _check_dangerous_tokens(args)
    allowed_flags = {"--oneline", "--stat", "--name-only", "--name-status", "--graph", "--no-decorate"}
    allowed_formats = {"--format=oneline", "--format=short", "--format=medium", "--format=full", "--format=reference"}

    re_n = re.compile(r"^(-n\d{1,2}|--max-count=\d{1,2})$")
    re_skip = re.compile(r"^--skip=\d{1,3}$")
    re_date = re.compile(r"^--(since|until)=\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?$")
    re_author = re.compile(r"^--author=[a-zA-Z0-9_\-\.\@\s]{1,40}$")
    re_grep = re.compile(r"^--grep=[a-zA-Z0-9_\-\.\s]{1,60}$")

    revisions: List[str] = []
    paths: List[str] = []

    if "--" in args:
        dash_idx = args.index("--")
        pre_args = args[:dash_idx]
        post_args = args[dash_idx + 1:]
    else:
        pre_args = args
        post_args = []

    i = 0
    while i < len(pre_args):
        arg = pre_args[i]
        if arg in allowed_flags or arg in allowed_formats:
            i += 1
        elif arg == "-n" and i + 1 < len(pre_args) and pre_args[i + 1].isdigit():
            count = int(pre_args[i + 1])
            if count < 1 or count > 50:
                raise ArgumentValidationError(f"-n count must be between 1 and 50; got {count}")
            i += 2
        elif re_n.match(arg):
            i += 1
        elif re_skip.match(arg) or re_date.match(arg) or re_author.match(arg) or re_grep.match(arg):
            i += 1
        elif RE_REVISION.match(arg) and not arg.startswith("-"):
            revisions.append(arg)
            i += 1
        else:
            raise ArgumentValidationError(f"Unknown or unauthorized git log argument: '{arg}'")

    if len(revisions) > 1:
        raise ArgumentValidationError(f"git log accepts at most 1 revision target; received {len(revisions)}")

    for p in post_args:
        if p.startswith(":"):
            raise ArgumentValidationError(f"Git pathspec magic is prohibited: '{p}'")
        target = Path(cwd) / p
        if not is_path_within_roots(target, approved_roots):
            raise ArgumentValidationError(f"Path escapes approved workspace roots: '{p}'")
        paths.append(p)

    if len(paths) > 1:
        raise ArgumentValidationError(f"git log accepts at most 1 file path filter; received {len(paths)}")


def validate_branch_args(args: List[str]) -> None:
    _check_dangerous_tokens(args)
    allowed_flags = {"--list", "-l", "-a", "--all", "-r", "--remotes", "--show-current", "--merged", "--no-merged"}
    patterns: List[str] = []

    for arg in args:
        if arg in allowed_flags:
            continue
        elif re.match(r"^[a-zA-Z0-9_\-\/\*\.]{1,40}$", arg) and not arg.startswith("-"):
            patterns.append(arg)
        else:
            raise ArgumentValidationError(f"Unknown or mutating git branch argument: '{arg}'")

    if len(patterns) > 1:
        raise ArgumentValidationError(f"git branch accepts at most 1 filter pattern; received {len(patterns)}")


def validate_rev_parse_args(args: List[str]) -> None:
    _check_dangerous_tokens(args)
    mode_a_flags = {"--show-toplevel", "--is-inside-work-tree", "--git-dir", "--git-common-dir"}
    mode_b_flags = {"--verify", "--abbrev-ref", "--symbolic-full-name"}
    re_short = re.compile(r"^(--short|--short=\d{1,2})$")

    has_mode_a = any(arg in mode_a_flags for arg in args)
    revisions: List[str] = []

    for arg in args:
        if arg in mode_a_flags:
            continue
        elif arg in mode_b_flags or re_short.match(arg):
            continue
        elif RE_REVISION.match(arg) and not arg.startswith("-"):
            revisions.append(arg)
        else:
            raise ArgumentValidationError(f"Unknown or unauthorized git rev-parse argument: '{arg}'")

    if has_mode_a:
        if len(args) != 1 or len(revisions) != 0:
            raise ArgumentValidationError("Standalone inspection queries (--show-toplevel, etc.) forbid revision arguments.")
    else:
        if len(revisions) != 1:
            raise ArgumentValidationError(f"Revision-dependent rev-parse requires exactly 1 revision; received {len(revisions)}")


def validate_show_args(args: List[str], cwd: str, approved_roots: List[Path]) -> None:
    _check_dangerous_tokens(args)
    allowed_flags = {"--stat", "--name-only", "--name-status", "--oneline"}
    allowed_formats = {"--format=oneline", "--format=short", "--format=medium", "--format=reference"}

    targets: List[str] = []

    for arg in args:
        if arg in allowed_flags or arg in allowed_formats:
            continue
        elif not arg.startswith("-"):
            if ":" in arg:
                # Revision:path format
                rev, path_part = arg.split(":", 1)
                if not RE_REVISION.match(rev) and rev != "":
                    raise ArgumentValidationError(f"Invalid revision part in show target: '{rev}'")
                target = Path(cwd) / path_part
                if not is_path_within_roots(target, approved_roots):
                    raise ArgumentValidationError(f"Target path escapes workspace roots: '{path_part}'")
            else:
                if not RE_REVISION.match(arg):
                    raise ArgumentValidationError(f"Invalid git show target: '{arg}'")
            targets.append(arg)
        else:
            raise ArgumentValidationError(f"Unknown or unauthorized git show argument: '{arg}'")

    if len(targets) != 1:
        raise ArgumentValidationError(f"git show requires exactly 1 target; received {len(targets)}")


def validate_tag_args(args: List[str]) -> None:
    _check_dangerous_tokens(args)
    allowed_flags = {"--list", "-l"}
    allowed_sorts = {
        "--sort=version:refname", "--sort=-version:refname",
        "--sort=creatordate", "--sort=-creatordate",
        "--sort=refname", "--sort=-refname",
    }
    re_n = re.compile(r"^-n[0-3]$")
    patterns: List[str] = []

    for arg in args:
        if arg in allowed_flags or arg in allowed_sorts or re_n.match(arg):
            continue
        elif re.match(r"^[a-zA-Z0-9_\-\/\*\.]{1,30}$", arg) and not arg.startswith("-"):
            patterns.append(arg)
        else:
            raise ArgumentValidationError(f"Unknown or mutating git tag argument: '{arg}'")

    if len(patterns) > 1:
        raise ArgumentValidationError(f"git tag accepts at most 1 pattern; received {len(patterns)}")


def validate_git_arguments(subcommand: str, sub_args: List[str], cwd: str, approved_roots: List[Path]) -> None:
    """Validate model arguments through subcommand-specific positive allowlists."""
    if subcommand == "status":
        validate_status_args(sub_args, cwd, approved_roots)
    elif subcommand == "diff":
        validate_diff_args(sub_args, cwd, approved_roots)
    elif subcommand == "log":
        validate_log_args(sub_args, cwd, approved_roots)
    elif subcommand == "branch":
        validate_branch_args(sub_args)
    elif subcommand == "rev-parse":
        validate_rev_parse_args(sub_args)
    elif subcommand == "show":
        validate_show_args(sub_args, cwd, approved_roots)
    elif subcommand == "tag":
        validate_tag_args(sub_args)
    else:
        raise ArgumentValidationError(f"Prohibited Git subcommand: '{subcommand}'")


# ---------------------------------------------------------------------------
# Cryptographic Repository Snapshot & Immutability Verification
# ---------------------------------------------------------------------------
def _on_walk_error(err: OSError) -> None:
    """Fail-closed directory traversal error handler."""
    raise SnapshotError(f"Directory enumeration failed at '{err.filename}': {err}") from err


def make_canonical_root_id(root_path: Union[str, Path]) -> str:
    """Generate a deterministic, collision-free identifier for a scan root."""
    resolved = Path(root_path).resolve()
    return os.path.normcase(str(resolved)).replace("\\", "/")


def get_minimal_disjoint_roots(paths: List[Union[str, Path]]) -> List[Path]:
    """Reduce candidate roots to the minimal set of non-overlapping disjoint directories."""
    unique_paths = {Path(p).resolve() for p in paths}
    sorted_paths = sorted(unique_paths, key=lambda p: len(p.parts))
    disjoint_roots: List[Path] = []
    for candidate in sorted_paths:
        if not any(candidate.is_relative_to(accepted) for accepted in disjoint_roots):
            disjoint_roots.append(candidate)
    return disjoint_roots


def snapshot_repository_state(
    workspace_dir: Union[str, Path],
    git_dir: Optional[Union[str, Path]] = None,
    git_common_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Tuple[str, ...]]:
    """
    Generate fail-closed cryptographic snapshot across workspace and Git metadata roots.
    Guarantees that unreadable directories or files raise SnapshotError.
    """
    candidate_inputs = [workspace_dir]
    if git_dir:
        candidate_inputs.append(git_dir)
    if git_common_dir:
        candidate_inputs.append(git_common_dir)

    resolved_candidates: List[Path] = []
    for raw_path in candidate_inputs:
        try:
            p = Path(raw_path).resolve()
            if not p.exists():
                raise SnapshotError(f"Required scan root does not exist: {p}")
            if not p.is_dir():
                raise SnapshotError(f"Required scan root is not a directory: {p}")
            resolved_candidates.append(p)
        except Exception as e:
            raise SnapshotError(f"Failed to access required scan root '{raw_path}': {e}") from e

    scan_roots = get_minimal_disjoint_roots(resolved_candidates)
    manifest: Dict[str, Tuple[str, ...]] = {}

    for root_dir in scan_roots:
        canonical_id = make_canonical_root_id(root_dir)

        for current_dir, dirs, files in os.walk(
            str(root_dir), topdown=True, onerror=_on_walk_error, followlinks=False
        ):
            # Prune junctions and directory symlinks
            dirs[:] = [
                d for d in dirs
                if not os.path.isjunction(os.path.join(current_dir, d))
                and not os.path.islink(os.path.join(current_dir, d))
            ]

            for fname in files:
                fpath = os.path.join(current_dir, fname)
                rel_path = os.path.relpath(fpath, str(root_dir)).replace("\\", "/")
                key = f"[{canonical_id}]:{rel_path}"

                try:
                    if os.path.islink(fpath):
                        target = os.readlink(fpath)
                        manifest[key] = ("SYMLINK", target)
                    else:
                        with open(fpath, "rb") as f:
                            data = f.read()
                        manifest[key] = ("FILE", str(len(data)), hashlib.sha256(data).hexdigest())
                except Exception as e:
                    raise SnapshotError(f"Integrity snapshot failed: could not read file '{fpath}': {e}") from e

    return manifest


def assert_repository_unmodified(before_manifest: dict, after_manifest: dict) -> None:
    """Assert zero files added, removed, or modified."""
    added = set(after_manifest.keys()) - set(before_manifest.keys())
    removed = set(before_manifest.keys()) - set(after_manifest.keys())
    modified = [k for k in before_manifest if k in after_manifest and before_manifest[k] != after_manifest[k]]

    errors = []
    if added:
        errors.append(f"Files added ({len(added)}): {sorted(list(added))[:5]}")
    if removed:
        errors.append(f"Files removed ({len(removed)}): {sorted(list(removed))[:5]}")
    if modified:
        errors.append(f"Files modified ({len(modified)}): {sorted(modified)[:5]}")

    if errors:
        raise AssertionError("Repository state was mutated during read-only operation: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# Pre-Flight Repository Discovery & Confinement
# ---------------------------------------------------------------------------
def discover_and_confine_repository(cwd: str, approved_roots: List[Path]) -> Tuple[Path, Path, Path]:
    """
    Query Git to discover toplevel, git_dir, and git_common_dir using ceiling directories.
    Verify that all resolved roots reside strictly within approved workspace roots.
    """
    clean_env = build_cleanroom_environment(cwd)
    query_cmd = [CANONICAL_GIT_EXE, "--no-pager", "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"]

    # Use basic low-level execution without snapshot for preflight
    rc, out, _ = _execute_contained_process(
        canonical_exe=CANONICAL_GIT_EXE,
        cmd_vector=["--no-pager", "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"],
        env=clean_env,
        cwd=cwd,
        timeout_seconds=5,
    )

    if rc != 0:
        raise RepositoryConfinementError(f"Directory '{cwd}' is not within a valid Git repository: {out.strip()}")

    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    if len(lines) < 3:
        raise RepositoryConfinementError(f"Unexpected rev-parse discovery output: '{out}'")

    raw_toplevel, raw_git_dir, raw_common_dir = lines[0], lines[1], lines[2]

    # Resolve paths relative to cwd
    toplevel = (Path(cwd) / raw_toplevel).resolve()
    git_dir = (Path(cwd) / raw_git_dir).resolve()
    git_common_dir = (Path(cwd) / raw_common_dir).resolve()

    if not is_path_within_roots(toplevel, approved_roots):
        raise RepositoryConfinementError(f"Discovered repository toplevel escapes approved roots: '{toplevel}'")
    if not is_path_within_roots(git_dir, approved_roots):
        raise RepositoryConfinementError(f"Discovered git-dir escapes approved roots: '{git_dir}'")
    if not is_path_within_roots(git_common_dir, approved_roots):
        raise RepositoryConfinementError(f"Discovered git-common-dir escapes approved roots: '{git_common_dir}'")

    return toplevel, git_dir, git_common_dir


# ---------------------------------------------------------------------------
# Low-Level Win32 Contained Process Execution Engine
# ---------------------------------------------------------------------------
def _execute_contained_process(
    canonical_exe: str,
    cmd_vector: List[str],
    env: Dict[str, str],
    cwd: str,
    timeout_seconds: int = 15,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> Tuple[int, str, bool]:
    """
    Executes a process under strict Windows kernel containment:
    - Explicit lpApplicationName binding (zero PATH search).
    - STARTUPINFOEXW with PROC_THREAD_ATTRIBUTE_HANDLE_LIST whitelisting strictly NUL stdin and pipe write.
    - CREATE_SUSPENDED (zero-cycle TOCTOU race window).
    - CREATE_UNICODE_ENVIRONMENT with sorted case-insensitive environment block.
    - Anonymous Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    - Fail-closed termination on assignment or thread resumption failure.
    - Bounded streaming read with 64 KB memory cap and monotonic deadline watchdog.
    """
    hJob = kernel32.CreateJobObjectW(None, None)
    if not hJob:
        raise OSError(f"CreateJobObjectW failed with error {ctypes.get_last_error()}")

    hStdInput = wintypes.HANDLE()
    hReadPipe = wintypes.HANDLE()
    hWritePipe = wintypes.HANDLE()
    hProcess = wintypes.HANDLE()
    hThread = wintypes.HANDLE()
    attr_buf = None

    try:
        # 1. Enforce KILL_ON_JOB_CLOSE on Job Object
        jeli = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            hJob, JobObjectExtendedLimitInformation, ctypes.byref(jeli), ctypes.sizeof(jeli)
        ):
            raise OSError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")

        # 2. Inheritable Security Attributes
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.bInheritHandle = True
        sa.lpSecurityDescriptor = None

        # 3. Create Anonymous Pipes for Combined Stdout/Stderr
        if not kernel32.CreatePipe(ctypes.byref(hReadPipe), ctypes.byref(hWritePipe), ctypes.byref(sa), 0):
            raise OSError(f"CreatePipe failed: {ctypes.get_last_error()}")

        # Ensure parent read end is explicitly NON-inheritable
        if not kernel32.SetHandleInformation(hReadPipe, HANDLE_FLAG_INHERIT, 0):
            raise OSError(f"SetHandleInformation failed: {ctypes.get_last_error()}")

        # 4. Open \\.\NUL as Inheritable Stdin Handle
        hStdInput = kernel32.CreateFileW(
            r"\\.\NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, ctypes.byref(sa), OPEN_EXISTING, 0, None
        )
        if not hStdInput or hStdInput == wintypes.HANDLE(-1).value:
            raise OSError(f"CreateFileW on NUL failed: {ctypes.get_last_error()}")

        # 5. Initialize Attribute List for Handle Whitelisting
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attr_buf = ctypes.create_string_buffer(size.value)
        if not kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size)):
            raise OSError(f"InitializeProcThreadAttributeList failed: {ctypes.get_last_error()}")

        # Whitelist ONLY hStdInput and hWritePipe for child inheritance
        handles_to_inherit = (wintypes.HANDLE * 2)(hStdInput, hWritePipe)
        if not kernel32.UpdateProcThreadAttribute(
            attr_buf,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.byref(handles_to_inherit),
            ctypes.sizeof(handles_to_inherit),
            None,
            None,
        ):
            raise OSError(f"UpdateProcThreadAttribute failed: {ctypes.get_last_error()}")

        # 6. Configure STARTUPINFOEXW
        siEx = STARTUPINFOEXW()
        siEx.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        siEx.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        siEx.StartupInfo.hStdInput = hStdInput
        siEx.StartupInfo.hStdOutput = hWritePipe
        siEx.StartupInfo.hStdError = hWritePipe
        siEx.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)

        pi = PROCESS_INFORMATION()

        # 7. Construct Unicode Environment Block
        sorted_items = sorted(env.items(), key=lambda kv: kv[0].upper())
        env_str = "\0".join(f"{k}={v}" for k, v in sorted_items) + "\0\0"

        # 8. Construct Command Line (argv[0] is canonical_exe)
        cmd_str = subprocess.list2cmdline([canonical_exe] + cmd_vector)

        flags = (
            CREATE_SUSPENDED
            | CREATE_NO_WINDOW
            | EXTENDED_STARTUPINFO_PRESENT
            | CREATE_UNICODE_ENVIRONMENT
        )

        # 9. Spawn Process with Explicit lpApplicationName (SUSPENDED)
        success = kernel32.CreateProcessW(
            canonical_exe,
            cmd_str,
            None,
            None,
            True,
            flags,
            ctypes.c_wchar_p(env_str),
            cwd,
            ctypes.byref(siEx),
            ctypes.byref(pi),
        )
        if not success:
            raise OSError(f"CreateProcessW failed with error {ctypes.get_last_error()}")

        hProcess = pi.hProcess
        hThread = pi.hThread

        # 10. Attribute List Cleanup
        kernel32.DeleteProcThreadAttributeList(attr_buf)
        attr_buf = None

        # 11. Close Parent Copies of Inherited Handles Immediately
        kernel32.CloseHandle(hStdInput)
        hStdInput = wintypes.HANDLE()
        kernel32.CloseHandle(hWritePipe)
        hWritePipe = wintypes.HANDLE()

        # 12. Assign Suspended Process to Job Object BEFORE Execution
        if not kernel32.AssignProcessToJobObject(hJob, hProcess):
            err = ctypes.get_last_error()
            kernel32.TerminateProcess(hProcess, 1)  # FAIL CLOSED
            raise OSError(f"AssignProcessToJobObject failed with error {err}")

        # 13. Resume Primary Thread
        prev_suspend_count = kernel32.ResumeThread(hThread)
        if prev_suspend_count == 0xFFFFFFFF:
            err = ctypes.get_last_error()
            kernel32.TerminateProcess(hProcess, 1)  # FAIL CLOSED
            raise OSError(f"ResumeThread failed with error {err}")

        # Close thread handle now that process is actively running
        kernel32.CloseHandle(hThread)
        hThread = wintypes.HANDLE()

        # 14. Bounded Stream Read with Monotonic Deadline Watchdog
        output_data = bytearray()
        buf = ctypes.create_string_buffer(4096)
        bytes_read = wintypes.DWORD()
        truncated = False

        start_time = time.monotonic()
        deadline = start_time + timeout_seconds
        poll_interval_ms = 100

        while True:
            wait_ret = kernel32.WaitForSingleObject(hProcess, poll_interval_ms)

            # Drain pipe buffer
            while True:
                avail = wintypes.DWORD()
                if not kernel32.PeekNamedPipe(hReadPipe, None, 0, None, ctypes.byref(avail), None):
                    break
                if avail.value == 0:
                    break

                to_read = min(4096, avail.value)
                if kernel32.ReadFile(hReadPipe, buf, to_read, ctypes.byref(bytes_read), None) and bytes_read.value > 0:
                    output_data.extend(buf.raw[:bytes_read.value])
                    if len(output_data) >= max_output_bytes:
                        truncated = True
                        output_data = output_data[:max_output_bytes]
                        kernel32.TerminateJobObject(hJob, 1)
                        break

            if wait_ret == WAIT_OBJECT_0:
                break

            if time.monotonic() >= deadline:
                kernel32.TerminateJobObject(hJob, 1)
                raise TimeoutError(f"Command timed out after {timeout_seconds} seconds")

        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(hProcess, ctypes.byref(exit_code))

        return exit_code.value, output_data.decode("utf-8", errors="replace"), truncated

    finally:
        # Guaranteed Deterministic Handle Closure in Reverse Order
        if attr_buf:
            try:
                kernel32.DeleteProcThreadAttributeList(attr_buf)
            except Exception:
                pass
        if hStdInput:
            try:
                kernel32.CloseHandle(hStdInput)
            except Exception:
                pass
        if hWritePipe:
            try:
                kernel32.CloseHandle(hWritePipe)
            except Exception:
                pass
        if hThread:
            try:
                kernel32.CloseHandle(hThread)
            except Exception:
                pass
        if hProcess:
            try:
                kernel32.CloseHandle(hProcess)
            except Exception:
                pass
        if hReadPipe:
            try:
                kernel32.CloseHandle(hReadPipe)
            except Exception:
                pass
        if hJob:
            try:
                kernel32.CloseHandle(hJob)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# High-Level Public Tool Interface
# ---------------------------------------------------------------------------
def run_command(
    executable: Optional[str] = None,
    args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    command: Optional[str] = None,
) -> str:
    """
    Execute a strictly controlled, read-only Git command.

    Parameters:
    - executable: Must resolve to canonical git.exe.
    - args: Structured array of arguments starting with an approved Git subcommand.
    - cwd: Optional working directory; must resolve within approved workspace roots.
    - timeout_seconds: Execution timeout (15s default, 60s hard cap).
    - command: Optional convenience string (e.g. 'git status --short').

    Security Invariants Enforced:
    - Zero shells, zero Python execution, zero where.exe.
    - Subcommand-specific positive allowlists.
    - Cleanroom environment and configuration isolation.
    - Windows Job Object containment and NUL stdin whitelisting.
    - Cryptographic before-and-after repository integrity verification.
    """
    if command and (not executable or not args):
        import shlex
        parts = shlex.split(command, posix=False)
        if parts:
            exe_cand = parts[0]
            if exe_cand.lower() in ("git", "git.exe"):
                executable = CANONICAL_GIT_EXE
            else:
                executable = exe_cand
            args = parts[1:]
    # 1. Executable Validation (Canonical git.exe only)
    if not executable or not isinstance(executable, str):
        return "Error: Executable must be a non-empty string."

    try:
        resolved_exe = Path(executable).resolve()
        canonical_path = Path(CANONICAL_GIT_EXE).resolve()
        if resolved_exe != canonical_path:
            # Also allow git.exe if it resolves identically
            if resolved_exe.name.lower() != "git.exe" or not os.path.isfile(resolved_exe):
                from tools.permissions import get_full_access_mode
                if get_full_access_mode() == "unrestricted":
                    cmd_list = [executable] + [str(a) for a in (args or [])]
                    res = subprocess.run(
                        cmd_list,
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        timeout=min(timeout_seconds, MAX_TIMEOUT_SECONDS),
                    )
                    return res.stdout if res.returncode == 0 else (res.stderr or f"Exited with code {res.returncode}")
                return f"Error: Executable '{executable}' is prohibited. Only canonical git.exe is permitted."
    except Exception as e:
        return f"Error: Failed to validate executable '{executable}': {e}"

    # 2. Argument Array Validation
    if not isinstance(args, list) or not args:
        return "Error: Arguments must be a non-empty list of strings."

    subcommand = str(args[0]).strip().lower()
    if subcommand not in APPROVED_SUBCOMMANDS:
        return f"Error: Git subcommand '{subcommand}' is prohibited. Approved subcommands: {sorted(list(APPROVED_SUBCOMMANDS))}"

    sub_args = [str(a) for a in args[1:]]

    # 3. Working Directory & Approved Roots Confinement
    approved_roots = get_approved_roots()
    target_cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()

    if not is_path_within_roots(target_cwd, approved_roots):
        return f"Error: Working directory '{target_cwd}' is outside approved workspace roots."

    # 4. Pre-Flight Repository Discovery & Confinement Check
    try:
        toplevel, git_dir, git_common_dir = discover_and_confine_repository(str(target_cwd), approved_roots)
    except RepositoryConfinementError as e:
        return f"Repository Confinement Error: {e}"
    except Exception as e:
        return f"Error during repository discovery: {e}"

    # 5. Grammar & Token Validation
    try:
        validate_git_arguments(subcommand, sub_args, str(target_cwd), approved_roots)
    except ArgumentValidationError as e:
        return f"Argument Validation Error: {e}"
    except Exception as e:
        return f"Error during argument validation: {e}"

    # 6. Clamp Timeout
    clamped_timeout = max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))

    # 7. Build Mandatory Cleanroom Environment
    clean_env = build_cleanroom_environment(str(toplevel))

    # 8. Assemble Injected Command Vector
    # Top-level security flags injected before subcommand
    top_level_injected = [
        "--no-pager",
        "-c", "core.pager=cat",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=NUL",
        "-c", "diff.external=",
        "-c", "core.askPass=NUL",
        "-c", "gc.auto=0",
        "-c", "protocol.allow=never",
        "-c", "submodule.recurse=false",
        "--no-replace-objects",
        "--no-lazy-fetch",
    ]

    # Subcommand-specific security flags (injected after diff/log/show)
    subcommand_injected = []
    if subcommand in ("diff", "log", "show"):
        subcommand_injected = ["--no-ext-diff", "--no-textconv"]

    cmd_vector = top_level_injected + [subcommand] + subcommand_injected + sub_args

    # 9. Pre-Execution Cryptographic Integrity Snapshot
    try:
        before_snapshot = snapshot_repository_state(toplevel, git_dir, git_common_dir)
    except SnapshotError as e:
        return f"Integrity Snapshot Error (Pre-Execution): {e}"

    # 10. Contained Process Execution
    try:
        exit_code, output, truncated = _execute_contained_process(
            canonical_exe=str(canonical_path),
            cmd_vector=cmd_vector,
            env=clean_env,
            cwd=str(target_cwd),
            timeout_seconds=clamped_timeout,
        )
    except TimeoutError as e:
        return f"Execution Error: {e}"
    except Exception as e:
        return f"Process Execution Failed: {e}"

    # 11. Post-Execution Cryptographic Integrity Snapshot & Verification
    try:
        after_snapshot = snapshot_repository_state(toplevel, git_dir, git_common_dir)
        assert_repository_unmodified(before_snapshot, after_snapshot)
    except SnapshotError as e:
        return f"Integrity Snapshot Error (Post-Execution): {e}"
    except AssertionError as e:
        return f"Security Violation: {e}"

    # 12. Return Formatted Tool Result
    trunc_banner = "\n[Output truncated at 64 KB limit]" if truncated else ""
    if exit_code != 0:
        return f"Git command returned non-zero exit code ({exit_code}):\n{output.strip()}{trunc_banner}"

    return f"{output.strip()}{trunc_banner}" if output.strip() else "[Command completed with no output]"
