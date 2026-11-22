"""
test_agent_engine.py - Comprehensive test suite for Phase 4 Controlled Agent Loop.

Covers items A through Z of Phase 4 specification:
A. AgentState creation
B. AgentAction validation
C. ToolResult normalization
D. AgentContext construction
E. LLMDecision parsing
F. Unknown tool rejection
G. Malformed action rejection
H. SAFE tool execution
I. Confirmation-required tool behavior
J. Confirmation pause
K. Confirmation resume (approval executes; rejection cancels run)
L. Denied / High-risk action behavior
M. MAX_AGENT_STEPS enforcement
N. MAX_RETRIES enforcement
O. Successful multi-step task
P. Failed tool handling
Q. Retry behavior
R. Re-planning behavior
S. Direct deterministic commands remain functional
X. No arbitrary code execution
Y. No permission bypass
Z. Model cannot invent tools
"""

import sys
import json
import inspect
from typing import List, Dict, Any, Optional

from brain.agent_models import (
    AgentState,
    AgentStatus,
    AgentAction,
    ActionType,
    ToolResult,
    LLMDecision,
    normalize_tool_result,
)
from brain.agent_context import build_agent_context, format_decision_prompt
from brain.agent import AgentEngine, MAX_AGENT_STEPS, MAX_RETRIES
from brain.router import route, Intent
from brain.dispatcher import dispatch
from tools.registry import registry, Tool
from tools.permissions import RiskLevel, check_permission, PermissionStatus


class MockOrangeAPIClient:
    """Mock API client that feeds predefined sequence of model replies."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None):
        self.responses = responses or []
        self.call_history: List[str] = []
        self._index = 0

    def is_available(self, timeout: float = 1.0) -> bool:
        return True

    def assist(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
        system_instruction: Optional[str] = None,
        profile: Optional[str] = None,
        provider: Optional[str] = None,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.call_history.append(message)
        if self._index < len(self.responses):
            resp = self.responses[self._index]
            self._index += 1
            return resp
        return {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "tool_name": None,
                    "arguments": {},
                    "question": None,
                    "response": "Default mock complete",
                    "reasoning_summary": "No more mock responses",
                },
                "summary": "Done",
            }),
            "model": "mock:test",
            "success": True,
        }


def test_agent_models_and_contracts():
    print("Testing Protocol Contracts (A, B, C, D, E)...")

    # A. AgentState creation
    state = AgentState(
        run_id="run123",
        goal="check system",
        original_request="check system status",
        status=AgentStatus.RUNNING,
    )
    assert state.run_id == "run123"
    assert state.status == AgentStatus.RUNNING
    assert state.step_count == 0
    assert state.retry_count == 0

    # B. AgentAction validation
    action = AgentAction(
        type=ActionType.CALL_TOOL,
        tool_name="get_cpu_usage",
        arguments={},
        reasoning_summary="Check CPU percent",
    )
    action_dict = action.to_dict()
    assert action_dict["type"] == "CALL_TOOL"
    assert action_dict["tool_name"] == "get_cpu_usage"
    rebuilt_action = AgentAction.from_dict(action_dict)
    assert rebuilt_action.type == ActionType.CALL_TOOL

    # C. ToolResult normalization
    # String success
    res1 = normalize_tool_result("get_time", "10:30 AM")
    assert res1.success is True
    assert res1.data == {"output": "10:30 AM"}
    assert res1.error is None

    # String error prefixes
    res_err1 = normalize_tool_result("open_app", "Error opening app: not found")
    assert res_err1.success is False
    assert "Error" in res_err1.error

    res_err2 = normalize_tool_result("find_file", "No file named 'secret.txt' found under C:\\")
    assert res_err2.success is False
    assert "No file named" in res_err2.error

    # Bool
    res_bool = normalize_tool_result("is_running", True)
    assert res_bool.success is True
    assert res_bool.data == {"result": True}

    # Dict
    res_dict = normalize_tool_result("metrics", {"usage": 42})
    assert res_dict.success is True
    assert res_dict.data["usage"] == 42

    # None
    res_none = normalize_tool_result("empty_tool", None)
    assert res_none.success is False
    assert res_none.error is not None

    # D. AgentContext construction
    ctx = build_agent_context(
        goal="test context",
        original_request="test context request",
        state=state,
        conversation_history=[{"role": "user", "content": "hi"}],
    )
    assert ctx.user_request == "test context request"
    assert len(ctx.available_tools) == 30  # All audited text desktop tools (including Stage 5E Orange Skills)
    assert any(c.startswith("You are the reasoning engine") for c in ctx.constraints)

    # E. LLMDecision parsing & prompt formatting
    prompt = format_decision_prompt(ctx)
    assert "USER GOAL:" in prompt
    assert "AVAILABLE TOOLS:" in prompt
    assert "SCHEMA REQUIREMENTS:" in prompt

    print("  Protocol Contracts passed [OK]")


def test_tool_validation_and_rejection():
    print("Testing Unknown Tool & Malformed Action Rejection (F, G, Z)...")

    # F & Z. Unknown tool rejection & model cannot invent tools
    mock_unknown = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "invented_magic_tool",
                    "arguments": {"foo": "bar"},
                    "reasoning_summary": "Trying an invented tool",
                },
                "summary": "Call magic",
            })
        },
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "Recovered after rejection",
                    "reasoning_summary": "Completed after correction",
                },
                "summary": "Finish",
            })
        },
    ])

    engine = AgentEngine(api_client=mock_unknown, max_retries=2)
    state = engine.start_run("run magic task")
    # First step failed on unknown tool, recorded observation, retried and finished
    assert len(state.observations) == 1
    assert state.observations[0]["result"]["success"] is False
    assert "does not exist in ToolRegistry" in state.observations[0]["result"]["error"]
    assert state.status == AgentStatus.COMPLETED

    # G. Malformed action rejection (invalid JSON)
    mock_malformed = MockOrangeAPIClient([
        {"reply": "This is plain text, not JSON!"},
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "Parsed valid JSON on retry",
                    "reasoning_summary": "Fixed format",
                },
                "summary": "Finish",
            })
        },
    ])
    engine_malformed = AgentEngine(api_client=mock_malformed, max_retries=2)
    state_malformed = engine_malformed.start_run("test malformed")
    assert state_malformed.status == AgentStatus.COMPLETED
    assert "Parsed valid JSON on retry" in state_malformed.final_response

    print("  Unknown Tool & Malformed Rejection passed [OK]")


def test_safe_tool_execution():
    print("Testing SAFE Tool Execution (H)...")

    mock_safe = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "get_cpu_usage",
                    "arguments": {},
                    "reasoning_summary": "Check CPU",
                },
                "summary": "Check CPU",
            })
        },
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "CPU checked successfully",
                    "reasoning_summary": "Done",
                },
                "summary": "Done",
            })
        },
    ])

    engine = AgentEngine(api_client=mock_safe)
    state = engine.start_run("check CPU usage")
    assert state.status == AgentStatus.COMPLETED
    assert len(state.observations) == 1
    assert state.observations[0]["result"]["success"] is True
    assert "get_cpu_usage" in state.completed_steps[0]

    print("  SAFE Tool Execution passed [OK]")


def test_confirmation_handling_pause_and_resumption():
    print("Testing Confirmation Pause & Resumption (I, J, K)...")

    # Sub-case 1: Confirmation Pause & Approval (Resume same run)
    mock_client1 = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "close_process",
                    "arguments": {"process_name": "dummy_nonexistent_proc"},
                    "reasoning_summary": "Close target process",
                },
                "summary": "Close dummy",
            })
        },
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "Process close verified",
                    "reasoning_summary": "Done",
                },
                "summary": "Finish",
            })
        },
    ])

    engine = AgentEngine(api_client=mock_client1)
    state1 = engine.start_run("close dummy proc")
    # Must pause in WAITING_CONFIRMATION
    assert state1.status == AgentStatus.WAITING_CONFIRMATION
    assert state1.pending_confirmation is not None
    assert state1.pending_confirmation.tool_name == "close_process"

    # Resume with user approval "y"
    resumed_state1 = engine.resume_run(state1.run_id, "y")
    assert resumed_state1.run_id == state1.run_id
    assert resumed_state1.status == AgentStatus.COMPLETED
    assert len(resumed_state1.observations) == 1
    assert resumed_state1.pending_confirmation is None

    # Sub-case 2: Confirmation Rejection (Correction 2: cancel run cleanly, no re-plan)
    mock_client2 = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "close_process",
                    "arguments": {"process_name": "notepad"},
                    "reasoning_summary": "Close notepad",
                },
                "summary": "Close notepad",
            })
        },
    ])

    engine2 = AgentEngine(api_client=mock_client2)
    state2 = engine2.start_run("close notepad")
    assert state2.status == AgentStatus.WAITING_CONFIRMATION

    # User declines with "n"
    declined_state2 = engine2.resume_run(state2.run_id, "n")
    assert declined_state2.status == AgentStatus.CANCELLED
    assert "user declined confirmation" in declined_state2.final_response
    assert len(declined_state2.observations) == 0  # Tool was never executed!

    print("  Confirmation Pause & Resumption passed [OK]")


def test_denied_and_high_risk_actions():
    print("Testing Denied & High-Risk Actions (L, X, Y)...")

    # Register temporary HIGH_RISK tool in registry
    high_risk_invoked = []
    high_tool = Tool(
        name="test_high_risk_action",
        description="Dangerous operation",
        risk_level=RiskLevel.HIGH_RISK,
        func=lambda: high_risk_invoked.append(1),
        category="system",
    )
    registry.register(high_tool)

    mock_high = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "test_high_risk_action",
                    "arguments": {},
                    "reasoning_summary": "Try high risk",
                },
                "summary": "Call high risk",
            })
        }
    ])

    engine = AgentEngine(api_client=mock_high)
    state = engine.start_run("trigger dangerous task")
    assert state.status == AgentStatus.FAILED
    assert "Action blocked" in state.final_response
    assert len(high_risk_invoked) == 0, "High risk tool must never execute!"

    # Clean up test tool from registry
    del registry._tools["test_high_risk_action"]

    print("  Denied & High-Risk Actions passed [OK]")


def test_step_and_retry_limits():
    print("Testing Step & Retry Limits (M, N, P, Q)...")

    # M. Max Steps Reached
    mock_infinite = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "get_time",
                    "arguments": {},
                    "reasoning_summary": "Check time repeatedly",
                },
                "summary": "Check time",
            })
        }
        for _ in range(5)
    ])
    engine_steps = AgentEngine(api_client=mock_infinite, max_steps=3)
    state_steps = engine_steps.start_run("loop forever")
    assert state_steps.status == AgentStatus.MAX_STEPS_REACHED
    assert state_steps.step_count == 3
    assert "maximum step limit" in state_steps.final_response

    # N. Max Retries Reached
    mock_errors = MockOrangeAPIClient([
        {"reply": "syntax error 1"},
        {"reply": "syntax error 2"},
        {"reply": "syntax error 3"},
    ])
    engine_retries = AgentEngine(api_client=mock_errors, max_retries=2)
    state_retries = engine_retries.start_run("trigger errors")
    assert state_retries.status == AgentStatus.MAX_RETRIES_REACHED

    print("  Step & Retry Limits passed [OK]")


def test_multi_step_and_replanning():
    print("Testing Multi-step Task & Dynamic Evaluation (O, R)...")

    mock_multistep = MockOrangeAPIClient([
        # Step 1: Check if process is running
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "is_running",
                    "arguments": {"process_name": "explorer"},
                    "reasoning_summary": "Check if explorer is running",
                },
                "summary": "Check explorer",
            })
        },
        # Step 2: Check RAM usage
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "get_ram_usage",
                    "arguments": {},
                    "reasoning_summary": "Check RAM load",
                },
                "summary": "Check RAM",
            })
        },
        # Step 3: Finish with summary
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "Explorer is running and RAM usage is healthy.",
                    "reasoning_summary": "All steps verified",
                },
                "summary": "Finish task",
            })
        },
    ])

    engine = AgentEngine(api_client=mock_multistep, max_steps=5)
    state = engine.start_run("inspect explorer and memory")
    assert state.status == AgentStatus.COMPLETED
    assert len(state.observations) == 2
    assert state.observations[0]["action"]["tool_name"] == "is_running"
    assert state.observations[1]["action"]["tool_name"] == "get_ram_usage"
    assert "Explorer is running" in state.final_response

    print("  Multi-step Task & Dynamic Evaluation passed [OK]")


def test_deterministic_commands_preserved():
    print("Testing Direct Deterministic Commands Preserved (S)...")

    # Verify existing router & dispatcher continue to execute directly without AgentEngine
    assert route("what time is it") == Intent.TIME
    res_time = dispatch(Intent.TIME, "what time is it")
    assert "Current time is" in res_time

    assert route("cpu usage") == Intent.CPU
    res_cpu = dispatch(Intent.CPU, "cpu usage")
    assert "CPU Usage:" in res_cpu

    assert route("list files") == Intent.LIST_FILES
    res_files = dispatch(Intent.LIST_FILES, "list files")
    assert len(res_files) > 0

    print("  Direct Deterministic Commands Preserved passed [OK]")


def test_regression_cpu_ram_battery_summary():
    print("Testing Regression: CPU + RAM + Battery + Summary multi-step task...")

    # Mock model sequence for exact failing user request:
    # "agent check my CPU usage, RAM usage, and battery status, then summarize my current system state"
    mock_responses = [
        # Step 1: Select get_cpu_usage
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "get_cpu_usage",
                    "arguments": {},
                    "reasoning_summary": "Check CPU usage first as planned",
                },
                "summary": "Check CPU",
            })
        },
        # Step 2: Select get_ram_usage
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "get_ram_usage",
                    "arguments": {},
                    "reasoning_summary": "Check RAM usage next",
                },
                "summary": "Check RAM",
            })
        },
        # Step 3: Select get_battery
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "get_battery",
                    "arguments": {},
                    "reasoning_summary": "Check battery status",
                },
                "summary": "Check Battery",
            })
        },
        # Step 4: Summarize system state with RESPOND / FINISH
        {
            "reply": json.dumps({
                "action": {
                    "type": "RESPOND",
                    "tool_name": None,
                    "arguments": {},
                    "response": "CPU is 25%, RAM is 60%, Battery is unavailable. System is running smoothly.",
                    "reasoning_summary": "All 3 system checks completed; provide synthesized summary.",
                },
                "summary": "Summarize system state",
            })
        },
    ]

    mock_client = MockOrangeAPIClient(mock_responses)
    engine = AgentEngine(api_client=mock_client, max_steps=10, max_retries=2)

    goal = "check my CPU usage, RAM usage, and battery status, then summarize my current system state"
    state = engine.start_run(goal)

    # 1. Verify model selected CPU tool and result is recorded
    assert len(state.observations) >= 3
    assert state.observations[0]["action"]["tool_name"] == "get_cpu_usage"
    assert state.observations[0]["result"]["success"] is True
    assert "output" in state.observations[0]["result"]["data"]

    # 2. Verify model selected RAM tool and result is recorded
    assert state.observations[1]["action"]["tool_name"] == "get_ram_usage"
    assert state.observations[1]["result"]["success"] is True
    assert "output" in state.observations[1]["result"]["data"]

    # 3. Verify model selected Battery tool and result is recorded
    assert state.observations[2]["action"]["tool_name"] == "get_battery"
    assert state.observations[2]["result"]["success"] is True
    assert "output" in state.observations[2]["result"]["data"]

    # 4. Verify exactly one tool was executed per loop iteration (boundary preserved)
    assert state.step_count == 4

    # 5. Verify final response is produced
    assert state.final_response is not None
    assert "CPU is 25%" in state.final_response

    # 6. Verify run ends COMPLETED
    assert state.status == AgentStatus.COMPLETED

    # 7. Verify MAX_RETRIES is NOT reached
    assert state.retry_count == 0
    assert state.status != AgentStatus.MAX_RETRIES_REACHED

    print("  Regression: CPU + RAM + Battery + Summary passed [OK]")


def test_robust_json_decision_extraction():
    print("Testing Robust JSON Decision Extraction...")
    from brain.agent import extract_json_decision

    # Preamble + fenced markdown
    r1 = 'Here is the action:\n```json\n{"action": {"type": "CALL_TOOL", "tool_name": "get_cpu_usage"}}\n```'
    assert extract_json_decision(r1)["action"]["tool_name"] == "get_cpu_usage"

    # Embedded in prose without fences
    r2 = 'Sure thing! {"action": {"type": "CALL_TOOL", "tool_name": "get_ram_usage"}} is the next step.'
    assert extract_json_decision(r2)["action"]["tool_name"] == "get_ram_usage"

    # Postamble text
    r3 = '{"action": {"type": "FINISH", "response": "Done!"}}\nHope that helps!'
    assert extract_json_decision(r3)["action"]["type"] == "FINISH"

    print("  Robust JSON Decision Extraction passed [OK]")


def test_agent_todowrite_integration():
    print("Testing Agent Engine TodoWrite Integration (REA Phase 3)...")
    from brain.agent_models import TaskStatus

    # Sequence:
    # Turn 1: Propose todo_write to declare tasks
    # Turn 2: Propose todo_write to update task progress
    # Turn 3: FINISH
    mock_client = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "todo_write",
                    "arguments": {
                        "todos": [
                            {"id": "1", "content": "Check RAM usage", "status": "in_progress", "active_form": "Checking RAM"},
                            {"id": "2", "content": "Check CPU usage", "status": "pending"},
                        ]
                    },
                    "reasoning_summary": "Declaring initial checklist",
                },
                "summary": "Declare tasks",
            })
        },
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "todo_write",
                    "arguments": {
                        "todos": [
                            {"id": "1", "content": "Check RAM usage", "status": "completed"},
                            {"id": "2", "content": "Check CPU usage", "status": "in_progress", "active_form": "Checking CPU"},
                        ]
                    },
                    "reasoning_summary": "Marking task 1 completed, task 2 in progress",
                },
                "summary": "Update task progress",
            })
        },
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "All checklist tasks evaluated successfully.",
                    "reasoning_summary": "Finished goal",
                },
                "summary": "Done",
            })
        },
    ])

    engine = AgentEngine(api_client=mock_client)
    state = engine.start_run("Audit system performance with checklist")

    assert state.status == AgentStatus.COMPLETED
    assert len(state.checklist) == 2
    assert state.checklist[0].status == TaskStatus.COMPLETED
    assert state.checklist[1].status == TaskStatus.IN_PROGRESS
    assert state.checklist[1].active_form == "Checking CPU"

    # Verify context injection into subsequent prompts
    assert len(mock_client.call_history) >= 2
    # Prompt for Turn 2 must include Turn 1's checklist
    turn2_prompt = mock_client.call_history[1]
    assert "CURRENT TASK CHECKLIST:" in turn2_prompt
    assert "Check RAM usage (IN PROGRESS: Checking RAM)" in turn2_prompt
    assert "Progress: 0/2 completed (0%)" in turn2_prompt

    # Prompt for Turn 3 must include Turn 2's updated checklist
    turn3_prompt = mock_client.call_history[2]
    assert "CURRENT TASK CHECKLIST:" in turn3_prompt
    assert "[x] 1. Check RAM usage" in turn3_prompt
    assert "[>] 2. Check CPU usage (IN PROGRESS: Checking CPU)" in turn3_prompt
    assert "Progress: 1/2 completed (50%)" in turn3_prompt

    print("  Agent Engine TodoWrite Integration passed [OK]")


def test_run_command_confirmation_integration():
    """Verify that Agent Engine pauses for confirmation when run_command is proposed."""
    print("Testing Agent Engine run_command confirmation handling...")
    mock_client = MockOrangeAPIClient([
        {
            "reply": json.dumps({
                "action": {
                    "type": "CALL_TOOL",
                    "tool_name": "run_command",
                    "arguments": {
                        "executable": "git.exe",
                        "args": ["status", "--short"]
                    },
                    "reasoning_summary": "Check git repository status"
                },
                "summary": "Check status"
            })
        },
        {
            "reply": json.dumps({
                "action": {
                    "type": "FINISH",
                    "response": "Git status inspected successfully.",
                    "reasoning_summary": "Done"
                },
                "summary": "Done"
            })
        }
    ])

    engine = AgentEngine(api_client=mock_client)
    state = engine.start_run("Check git status of repository")

    # Verify that run_command paused in WAITING_CONFIRMATION
    assert state.status == AgentStatus.WAITING_CONFIRMATION
    assert state.pending_confirmation is not None
    assert state.pending_confirmation.tool_name == "run_command"
    assert "requests permission to execute 'run_command'" in state.final_response

    # Verify declining confirmation cancels the run
    declined_state = engine.resume_run(state.run_id, "n")
    assert declined_state.status == AgentStatus.CANCELLED
    assert "user declined confirmation" in declined_state.final_response

    print("  Agent Engine run_command Confirmation Integration passed [OK]")


if __name__ == "__main__":
    print("=" * 60)
    print("ORANGE AI: AGENT ENGINE & CONTROLLED LOOP TESTS (PHASE 4)")
    print("=" * 60)
    test_agent_models_and_contracts()
    test_tool_validation_and_rejection()
    test_safe_tool_execution()
    test_confirmation_handling_pause_and_resumption()
    test_denied_and_high_risk_actions()
    test_step_and_retry_limits()
    test_multi_step_and_replanning()
    test_deterministic_commands_preserved()
    test_regression_cpu_ram_battery_summary()
    test_robust_json_decision_extraction()
    test_agent_todowrite_integration()
    test_run_command_confirmation_integration()
    print("=" * 60)
    print("ALL AGENT ENGINE TESTS PASSED [OK]")
    print("=" * 60)
