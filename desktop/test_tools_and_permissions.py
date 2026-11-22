"""
test_tools_and_permissions.py - Unit test suite for Tool Registry & Permissions.

Verifies:
- Tool registration and duplicate prevention
- Safe metadata inspection without execution
- Risk classification and permission evaluation
- Explicit user confirmation semantics (y/Y vs n/N/empty/invalid)
- HIGH_RISK default blocking
- Execution safety (inspection does not trigger execution)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.permissions import (
    RiskLevel,
    PermissionStatus,
    PermissionResult,
    check_permission,
    request_confirmation,
    execute_with_permission,
)
from tools.registry import (
    Tool,
    ToolRegistry,
    create_default_registry,
    registry as default_reg,
)


def test_registry_operations():
    print("Running Registry Operations Tests...")
    reg = ToolRegistry()

    execution_tracker = []

    def dummy_tool():
        execution_tracker.append("executed")
        return "dummy_result"

    # Register tool
    tool = Tool(
        name="test_tool",
        description="A test tool",
        risk_level=RiskLevel.SAFE,
        func=dummy_tool,
        category="test",
    )
    reg.register(tool)
    assert reg.contains("test_tool") is True
    assert reg.contains("non_existent") is False

    # Duplicate registration must fail
    duplicate_raised = False
    try:
        reg.register(tool)
    except ValueError:
        duplicate_raised = True
    assert duplicate_raised is True, "Duplicate tool registration must raise ValueError"

    # Get tool
    retrieved = reg.get("test_tool")
    assert retrieved is not None
    assert retrieved.name == "test_tool"
    assert retrieved.risk_level == RiskLevel.SAFE

    # Unknown tool
    assert reg.get("non_existent") is None

    # Listing tools & metadata must NOT execute the tool
    all_tools = reg.list_tools()
    assert len(all_tools) == 1
    metadata = reg.list_metadata()
    assert len(metadata) == 1
    assert metadata[0]["name"] == "test_tool"
    assert metadata[0]["risk_level"] == "SAFE"
    assert metadata[0]["requires_confirmation"] is False
    assert len(execution_tracker) == 0, "Metadata inspection must not execute tools"

    print("  Registry operations passed.")


def test_permission_levels():
    print("Running Permission Evaluation Tests...")

    safe_tool = Tool("safe", "safe tool", RiskLevel.SAFE, lambda: "ok")
    conf_tool = Tool("conf", "conf tool", RiskLevel.CONFIRMATION_REQUIRED, lambda: "ok")
    high_tool = Tool("high", "high tool", RiskLevel.HIGH_RISK, lambda: "ok")

    # Safe tool is ALLOWED
    perm_safe = check_permission(safe_tool, is_admin=False)
    assert perm_safe.status == PermissionStatus.ALLOWED

    # Confirmation required is REQUIRES_CONFIRMATION (even for admin)
    perm_conf_user = check_permission(conf_tool, is_admin=False)
    assert perm_conf_user.status == PermissionStatus.REQUIRES_CONFIRMATION

    perm_conf_admin = check_permission(conf_tool, is_admin=True)
    assert perm_conf_admin.status == PermissionStatus.REQUIRES_CONFIRMATION, "Admin privileges must not bypass confirmation"

    # High risk is DENIED by default (even for admin)
    perm_high_user = check_permission(high_tool, is_admin=False)
    assert perm_high_user.status == PermissionStatus.DENIED

    perm_high_admin = check_permission(high_tool, is_admin=True)
    assert perm_high_admin.status == PermissionStatus.DENIED, "Admin privileges must not bypass high risk block"

    print("  Permission evaluation passed.")


def test_confirmation_semantics():
    print("Running Confirmation Semantics Tests...")

    # Affirmative inputs
    assert request_confirmation("test", prompt_fn=lambda _: "y") is True
    assert request_confirmation("test", prompt_fn=lambda _: "Y") is True
    assert request_confirmation("test", prompt_fn=lambda _: "yes") is True
    assert request_confirmation("test", prompt_fn=lambda _: " YES ") is True

    # Negative & invalid inputs (default to NO)
    assert request_confirmation("test", prompt_fn=lambda _: "n") is False
    assert request_confirmation("test", prompt_fn=lambda _: "N") is False
    assert request_confirmation("test", prompt_fn=lambda _: "no") is False
    assert request_confirmation("test", prompt_fn=lambda _: "") is False
    assert request_confirmation("test", prompt_fn=lambda _: "   ") is False
    assert request_confirmation("test", prompt_fn=lambda _: "maybe") is False
    assert request_confirmation("test", prompt_fn=lambda _: "sure") is False
    assert request_confirmation("test", prompt_fn=lambda _: "1") is False

    print("  Confirmation semantics passed.")


def test_execution_with_permission():
    print("Running Execution with Permission Tests...")

    executed = []
    tool = Tool(
        name="state_changer",
        description="modifies state",
        risk_level=RiskLevel.CONFIRMATION_REQUIRED,
        func=lambda val: executed.append(val) or f"done_{val}",
    )

    # Denied confirmation -> does not execute
    res_denied = execute_with_permission(tool, args=("arg1",), kwargs={}, prompt_fn=lambda _: "n")
    assert "user declined confirmation" in res_denied
    assert len(executed) == 0, "Declined confirmation must not execute tool"

    # Empty confirmation -> does not execute
    res_empty = execute_with_permission(tool, args=("arg2",), kwargs={}, prompt_fn=lambda _: "")
    assert "user declined confirmation" in res_empty
    assert len(executed) == 0

    # Approved confirmation -> executes tool
    res_approved = execute_with_permission(tool, args=("arg3",), kwargs={}, prompt_fn=lambda _: "y")
    assert res_approved == "done_arg3"
    assert executed == ["arg3"], "Approved confirmation must execute tool"

    # High risk tool execution attempt -> blocked
    high_executed = []
    high_tool = Tool(
        name="destroyer",
        description="destructive",
        risk_level=RiskLevel.HIGH_RISK,
        func=lambda: high_executed.append(1),
    )
    res_high = execute_with_permission(high_tool, args=(), kwargs={}, prompt_fn=lambda _: "y")
    assert "Action blocked" in res_high
    assert len(high_executed) == 0, "High risk tool must never execute"

    print("  Execution with permission passed.")


def test_default_registry_integrity():
    print("Running Default Registry Audit Tests...")

    assert default_reg.contains("get_time") is True
    assert default_reg.contains("open_app") is True
    assert default_reg.contains("close_process") is True

    # Check risk levels of key tools
    assert default_reg.get("get_time").risk_level == RiskLevel.SAFE
    assert default_reg.get("get_battery").risk_level == RiskLevel.SAFE
    assert default_reg.get("list_files").risk_level == RiskLevel.SAFE
    assert default_reg.get("open_app").risk_level == RiskLevel.CONFIRMATION_REQUIRED
    assert default_reg.get("open_folder").risk_level == RiskLevel.CONFIRMATION_REQUIRED
    assert default_reg.get("close_process").risk_level == RiskLevel.CONFIRMATION_REQUIRED

    # Safe tool executes directly through registry without prompt
    prompt_called = []
    time_res = default_reg.execute("get_time", prompt_fn=lambda p: prompt_called.append(p) or "y")
    assert len(prompt_called) == 0, "Safe tools must not prompt for confirmation"
    assert len(time_res) > 0

    # Confirmation-required tool prompts through registry
    conf_called = []
    app_res = default_reg.execute("open_app", "nonexistent_test_app", prompt_fn=lambda p: conf_called.append(p) or "n")
    assert len(conf_called) == 1, "Confirmation-required tool must trigger confirmation prompt"
    assert "user declined confirmation" in app_res

    print("  Default registry audit passed.")


if __name__ == "__main__":
    print("=" * 60)
    print("ORANGE AI: TOOL REGISTRY & PERMISSION TESTS")
    print("=" * 60)
    test_registry_operations()
    test_permission_levels()
    test_confirmation_semantics()
    test_execution_with_permission()
    test_default_registry_integrity()
    print("=" * 60)
    print("ALL REGISTRY & PERMISSION TESTS PASSED [OK]")
    print("=" * 60)
