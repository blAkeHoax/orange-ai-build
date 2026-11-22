"""
tools/permissions.py - Centralized permission architecture for Orange AI Desktop.

Defines risk classifications, permission checking, catastrophic command guards,
and explicit confirmation semantics. Enforces that catastrophic OS destruction is
permanently blocked, while governed tools follow either Supervised Full Access or
Unrestricted Autonomous Mode.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Callable, Any, Optional, Tuple, Union, List
from pathlib import Path
import os
import re
import sys
import tempfile


class RiskLevel(Enum):
    """Classification of tool risk based on actual implementation behavior."""
    SAFE = "SAFE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    HIGH_RISK = "HIGH_RISK"


class PermissionStatus(Enum):
    """Result status of a permission evaluation."""
    ALLOWED = "ALLOWED"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    DENIED = "DENIED"


@dataclass(frozen=True)
class PermissionResult:
    """Structured result from a permission check."""
    status: PermissionStatus
    reason: str
    risk_level: RiskLevel


DEFAULT_ACTIVE_WORKSPACES: List[str] = [
    r"D:\Orange AI",
    r"D:\CreatorPulse",
]


def is_catastrophic_command(command_or_path: str) -> Tuple[bool, str]:
    """
    Evaluate whether a shell command or file path contains irreversible OS-destruction patterns.
    Catastrophic commands are blocked unconditionally in ALL modes (including unrestricted).
    """
    if not command_or_path or not isinstance(command_or_path, str):
        return False, ""

    lowered = command_or_path.strip().lower()

    # 1. Drive formatting
    if re.search(r"\bformat(\.com|\.exe)?(\s+|$)", lowered) or "format-volume" in lowered:
        return True, "Irreversible drive format operation is permanently blocked."

    # 2. Critical Windows system directory destruction or direct mutation
    system_patterns = [
        r"c:\windows\system32",
        r"c:/windows/system32",
        r"\system32",
        r"/system32",
        r"c:\windows",
        r"c:/windows",
    ]
    destructive_verbs = ["rmdir", "del", "erase", "remove-item", "rd", "rm"]
    for sys_pat in system_patterns:
        if sys_pat in lowered:
            if any(verb in lowered for verb in destructive_verbs):
                return True, "Destruction of Windows system directories is permanently blocked."
            if "format" in lowered or "wipe" in lowered:
                return True, "Destruction of Windows system directories is permanently blocked."
            if lowered.startswith(r"c:\windows") or lowered.startswith(r"c:/windows"):
                return True, "Mutation or deletion of Windows system directories is permanently blocked."

    # 3. Orange AI core database direct destruction
    if "memory.db" in lowered:
        if any(verb in lowered for verb in destructive_verbs) or lowered.endswith("memory.db"):
            return True, "Direct deletion or destruction of active Orange AI memory.db is permanently blocked."

    # 4. Low-level disk partitioning destruction
    if "diskpart" in lowered and ("clean all" in lowered or "clean" in lowered):
        return True, "Low-level disk wiping commands are permanently blocked."

    return False, ""


def get_active_workspaces() -> List[Path]:
    """Retrieve canonical paths of active user workspaces."""
    workspaces: List[Path] = []
    for ws in DEFAULT_ACTIVE_WORKSPACES:
        try:
            p = Path(ws).resolve()
            workspaces.append(p)
        except Exception:
            pass

    # Include system temp directory for testing and staging
    try:
        workspaces.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass

    # Include any roots from ORANGE_ALLOWED_ROOTS
    env_roots = os.environ.get("ORANGE_ALLOWED_ROOTS", "")
    if env_roots:
        for r in env_roots.split(os.pathsep):
            r = r.strip()
            if r:
                try:
                    workspaces.append(Path(r).resolve())
                except Exception:
                    pass

    return workspaces


def is_scoped_in_workspaces(target: Optional[Union[str, Path]]) -> bool:
    """Check if the given file, directory, or target is inside active user workspaces."""
    if not target:
        return False
    try:
        target_path = Path(str(target)).resolve()
        for root in get_active_workspaces():
            if target_path == root:
                return True
            try:
                if target_path.is_relative_to(root):
                    return True
            except (ValueError, AttributeError):
                pass
    except Exception:
        return False
    return False


def get_full_access_mode() -> str:
    """Retrieve current full access trust mode ('supervised' | 'unrestricted')."""
    mode = os.environ.get("ORANGE_FULL_ACCESS_MODE")
    if mode in ("supervised", "unrestricted"):
        return mode
    try:
        from gui.settings import load_settings
        settings = load_settings()
        return settings.get("full_access_mode", "supervised")
    except Exception:
        return "supervised"


def set_full_access_mode(mode: str) -> None:
    """Set the full access trust mode ('supervised' | 'unrestricted')."""
    if mode not in ("supervised", "unrestricted"):
        raise ValueError(f"Invalid full access mode: {mode}")
    os.environ["ORANGE_FULL_ACCESS_MODE"] = mode


def check_permission(
    tool: Any,
    is_admin: bool = False,
    full_access_mode: Optional[str] = None,
    target: Optional[Union[str, Path]] = None,
) -> PermissionResult:
    """
    Evaluate whether a requested tool is permitted to execute under the active trust mode.

    Rules:
    - Irreversible catastrophic operations are blocked unconditionally (DENIED).
    - In 'unrestricted' mode: all non-catastrophic actions are ALLOWED without confirmation.
    - In 'supervised' mode (default):
      - SAFE read/diagnostic tools and actions scoped within active workspaces execute automatically (ALLOWED).
      - State-altering actions outside active workspaces require explicit user confirmation (REQUIRES_CONFIRMATION).
      - Actions classified as HIGH_RISK are DENIED unless in unrestricted mode.
    """
    tool_name = getattr(tool, "name", "unknown")
    risk = getattr(tool, "risk_level", RiskLevel.HIGH_RISK)

    # Catastrophic Guard
    if target:
        is_cat, cat_reason = is_catastrophic_command(str(target))
        if is_cat:
            return PermissionResult(
                status=PermissionStatus.DENIED,
                reason=f"Action blocked: {cat_reason}",
                risk_level=RiskLevel.HIGH_RISK,
            )

    active_mode = full_access_mode or get_full_access_mode()

    # Unrestricted Autonomous Mode ("Full Access — No Prompts")
    if active_mode == "unrestricted":
        return PermissionResult(
            status=PermissionStatus.ALLOWED,
            reason="Unrestricted Autonomous Mode active; execution permitted without confirmation.",
            risk_level=risk,
        )

    # Supervised Full Access Mode (Default)
    if risk == RiskLevel.HIGH_RISK:
        return PermissionResult(
            status=PermissionStatus.DENIED,
            reason=f"Tool '{tool_name}' is classified as HIGH_RISK and cannot be executed automatically.",
            risk_level=risk,
        )

    if risk == RiskLevel.CONFIRMATION_REQUIRED:
        tool_cat = getattr(tool, "category", "")
        if tool_cat == "files" and target and is_scoped_in_workspaces(target) and tool_name != "edit_file":
            return PermissionResult(
                status=PermissionStatus.ALLOWED,
                reason="File operation inside active workspace is pre-authorized under Supervised mode.",
                risk_level=risk,
            )
        return PermissionResult(
            status=PermissionStatus.REQUIRES_CONFIRMATION,
            reason=f"Tool '{tool_name}' causes system or application state changes.",
            risk_level=risk,
        )

    return PermissionResult(
        status=PermissionStatus.ALLOWED,
        reason="Read-only operation permitted.",
        risk_level=risk,
    )


def request_confirmation(
    tool_name: str,
    details: str = "",
    prompt_fn: Optional[Callable[[str], str]] = None,
) -> bool:
    """
    Prompt the user for explicit confirmation before executing a risky action.

    Security semantics:
    - Default is NO.
    - Empty input returns False.
    - 'n', 'N', or any unexpected input returns False.
    - Only explicit affirmative 'y' or 'Y' (or 'yes') returns True.
    """
    if prompt_fn is None:
        prompt_fn = input

    detail_str = f" ({details})" if details else ""
    prompt_text = (
        f"\n⚠️  Orange AI wants to perform: {tool_name}{detail_str}\n"
        f"   Risk level: CONFIRMATION_REQUIRED\n"
        f"   Proceed? [y/N]: "
    )

    try:
        response = prompt_fn(prompt_text)
        if response is None:
            return False
        cleaned = response.strip().lower()
        return cleaned in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
    except Exception:
        return False


def execute_with_permission(
    tool: Any,
    args: tuple,
    kwargs: dict,
    is_admin: bool = False,
    prompt_fn: Optional[Callable[[str], str]] = None,
    full_access_mode: Optional[str] = None,
) -> Any:
    """
    Enforce permission check and confirmation before invoking the tool function.
    """
    target = None
    if args:
        target = args[0]
    elif kwargs:
        target = kwargs.get("path") or kwargs.get("command") or kwargs.get("src") or kwargs.get("app_name") or kwargs.get("process_name")

    decision = check_permission(tool, is_admin=is_admin, full_access_mode=full_access_mode, target=target)

    if decision.status == PermissionStatus.DENIED:
        return f"Action blocked: {decision.reason}"

    if decision.status == PermissionStatus.REQUIRES_CONFIRMATION:
        details = str(target) if target is not None else ""
        approved = request_confirmation(tool.name, details=details, prompt_fn=prompt_fn)
        if not approved:
            return f"Action cancelled: user declined confirmation to execute '{tool.name}'."

    return tool.func(*args, **kwargs)
