"""
tools/permissions.py - Centralized permission architecture for Orange AI Desktop.

Defines risk classifications, permission checking, and explicit confirmation
semantics. Enforces that high-risk tools cannot be executed automatically and
that confirmation-required actions cannot be bypassed.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Callable, Any, Optional
import sys


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


def check_permission(tool: Any, is_admin: bool = False) -> PermissionResult:
    """
    Evaluate whether a requested tool is permitted to execute.

    Rules:
    - HIGH_RISK actions are DENIED by default. Administrator privileges
      do NOT override or bypass this check.
    - CONFIRMATION_REQUIRED actions require explicit user approval.
      Administrator privileges do NOT override user consent.
    - SAFE actions are read-only and ALLOWED to execute directly.
    """
    risk = getattr(tool, "risk_level", RiskLevel.HIGH_RISK)

    if risk == RiskLevel.HIGH_RISK:
        return PermissionResult(
            status=PermissionStatus.DENIED,
            reason=f"Tool '{getattr(tool, 'name', 'unknown')}' is classified as HIGH_RISK and cannot be executed automatically.",
            risk_level=risk,
        )

    if risk == RiskLevel.CONFIRMATION_REQUIRED:
        return PermissionResult(
            status=PermissionStatus.REQUIRES_CONFIRMATION,
            reason=f"Tool '{getattr(tool, 'name', 'unknown')}' causes system or application state changes.",
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
) -> Any:
    """
    Enforce permission check and confirmation before invoking the tool function.
    """
    decision = check_permission(tool, is_admin=is_admin)

    if decision.status == PermissionStatus.DENIED:
        return f"Action blocked: {decision.reason}"

    if decision.status == PermissionStatus.REQUIRES_CONFIRMATION:
        details = ""
        if args:
            details = str(args[0])
        elif kwargs:
            details = ", ".join(f"{k}={v}" for k, v in kwargs.items())

        approved = request_confirmation(tool.name, details=details, prompt_fn=prompt_fn)
        if not approved:
            return f"Action cancelled: user declined confirmation to execute '{tool.name}'."

    return tool.func(*args, **kwargs)
