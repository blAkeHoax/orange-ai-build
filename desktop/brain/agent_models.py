"""
brain/agent_models.py - Core protocol contracts for Orange AI Controlled Agent Loop.

Defines the five standard data contracts:
1. AgentState
2. AgentAction
3. ToolResult & normalizer
4. AgentContext
5. LLMDecision
"""

import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Union


class AgentStatus(Enum):
    """Lifecycle statuses for an Agent run."""
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    MAX_STEPS_REACHED = "MAX_STEPS_REACHED"
    MAX_RETRIES_REACHED = "MAX_RETRIES_REACHED"


class ActionType(Enum):
    """Permitted action types proposed by LLM or Agent Engine."""
    CALL_TOOL = "CALL_TOOL"
    ASK_USER = "ASK_USER"
    REQUEST_CONFIRMATION = "REQUEST_CONFIRMATION"
    RESPOND = "RESPOND"
    FINISH = "FINISH"


class TaskStatus(Enum):
    """Lifecycle statuses for individual checklist items."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class TaskItem:
    """A structured subtask within an agent's in-flight checklist."""
    id: str
    content: str
    status: TaskStatus = TaskStatus.PENDING
    active_form: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "active_form": self.active_form,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskItem":
        raw_status = data.get("status", "pending")
        if isinstance(raw_status, TaskStatus):
            status = raw_status
        else:
            try:
                status = TaskStatus(str(raw_status).lower())
            except ValueError:
                status = TaskStatus.PENDING

        return cls(
            id=str(data.get("id", "")),
            content=str(data.get("content", "")).strip(),
            status=status,
            active_form=data.get("active_form"),
        )

    def to_json(self) -> str:
        """Serialize TaskItem to a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "TaskItem":
        """Deserialize TaskItem from a JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class AgentAction:
    """A single action proposed by the LLM or scheduled by the engine."""
    type: ActionType
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    question: Optional[str] = None
    response: Optional[str] = None
    reasoning_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "question": self.question,
            "response": self.response,
            "reasoning_summary": self.reasoning_summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentAction":
        raw_type = data.get("type", "RESPOND")
        try:
            action_type = ActionType(raw_type)
        except ValueError:
            # Fallback if model output was malformed
            action_type = ActionType.RESPOND

        return cls(
            type=action_type,
            tool_name=data.get("tool_name"),
            arguments=data.get("arguments") or {},
            question=data.get("question"),
            response=data.get("response"),
            reasoning_summary=data.get("reasoning_summary", ""),
        )

    def to_json(self) -> str:
        """Serialize AgentAction to a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "AgentAction":
        """Deserialize AgentAction from a JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class ToolResult:
    """Normalized output contract for any executed tool."""
    success: bool
    tool_name: str
    data: Any
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolResult":
        return cls(
            success=bool(data.get("success", False)),
            tool_name=data.get("tool_name", "unknown"),
            data=data.get("data"),
            error=data.get("error"),
            metadata=data.get("metadata") or {},
        )

    def to_json(self) -> str:
        """Serialize ToolResult to a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "ToolResult":
        """Deserialize ToolResult from a JSON string."""
        return cls.from_dict(json.loads(json_str))


def normalize_tool_result(tool_name: str, raw_output: Any) -> ToolResult:
    """
    Adapter layer: Normalizes arbitrary tool returns (strings, bools, dicts)
    into a standardized ToolResult without modifying the 19 existing tools.
    """
    if isinstance(raw_output, ToolResult):
        return raw_output

    if raw_output is None:
        return ToolResult(
            success=False,
            tool_name=tool_name,
            data=None,
            error="Tool produced no result (None).",
        )

    if isinstance(raw_output, bool):
        return ToolResult(
            success=True,
            tool_name=tool_name,
            data={"result": raw_output},
            error=None,
        )

    if isinstance(raw_output, dict):
        if "error" in raw_output and raw_output["error"]:
            return ToolResult(
                success=False,
                tool_name=tool_name,
                data=raw_output,
                error=str(raw_output["error"]),
            )
        return ToolResult(
            success=True,
            tool_name=tool_name,
            data=raw_output,
            error=None,
        )

    if isinstance(raw_output, (list, tuple)):
        return ToolResult(
            success=True,
            tool_name=tool_name,
            data=list(raw_output),
            error=None,
        )

    # Output is string
    out_str = str(raw_output).strip()
    lower_str = out_str.lower()

    # Detect common failure signatures returned by existing desktop tools
    failure_prefixes = (
        "error",
        "action blocked",
        "action cancelled",
        "folder not found",
        "no file named",
        "no process named",
        "i don't know how to open",
        "i couldn't find",
    )

    if any(lower_str.startswith(prefix) for prefix in failure_prefixes):
        return ToolResult(
            success=False,
            tool_name=tool_name,
            data=None,
            error=out_str,
        )

    return ToolResult(
        success=True,
        tool_name=tool_name,
        data={"output": out_str},
        error=None,
    )


@dataclass
class AgentContext:
    """Context package constructed for LLM decision reasoning."""
    user_request: str
    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    relevant_memories: List[Dict[str, Any]] = field(default_factory=list)
    user_profile: str = ""
    current_state: str = ""
    available_tools: List[Dict[str, Any]] = field(default_factory=list)
    previous_actions: List[Dict[str, Any]] = field(default_factory=list)
    previous_results: List[Dict[str, Any]] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    checklist: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_request": self.user_request,
            "conversation_history": self.conversation_history,
            "relevant_memories": self.relevant_memories,
            "user_profile": self.user_profile,
            "current_state": self.current_state,
            "available_tools": self.available_tools,
            "previous_actions": self.previous_actions,
            "previous_results": self.previous_results,
            "constraints": self.constraints,
            "checklist": self.checklist,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentContext":
        return cls(
            user_request=data.get("user_request", ""),
            conversation_history=data.get("conversation_history") or [],
            relevant_memories=data.get("relevant_memories") or [],
            user_profile=data.get("user_profile", ""),
            current_state=data.get("current_state", ""),
            available_tools=data.get("available_tools") or [],
            previous_actions=data.get("previous_actions") or [],
            previous_results=data.get("previous_results") or [],
            constraints=data.get("constraints") or [],
            checklist=data.get("checklist") or [],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "AgentContext":
        return cls.from_dict(json.loads(json_str))


@dataclass
class LLMDecision:
    """Structured decision returned by model parsing (no artificial confidence)."""
    action: AgentAction
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "summary": self.summary,
        }


@dataclass
class AgentState:
    """State of an in-flight or completed agent run."""
    run_id: str
    goal: str
    original_request: str
    status: AgentStatus
    context: Optional[AgentContext] = None
    plan: Optional[Any] = None
    checklist: List[TaskItem] = field(default_factory=list)
    current_step: int = 0
    completed_steps: List[str] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    last_action: Optional[AgentAction] = None
    last_result: Optional[ToolResult] = None
    step_count: int = 0
    retry_count: int = 0
    pending_confirmation: Optional[AgentAction] = None
    waiting_for_user: bool = False
    final_response: Optional[str] = None

    def _serialize_plan(self) -> Optional[Dict[str, Any]]:
        """Safely serialize plan if present."""
        if not self.plan:
            return None
        if isinstance(self.plan, dict):
            return self.plan
        if hasattr(self.plan, "steps") and hasattr(self.plan, "goal"):
            steps_data = []
            for s in self.plan.steps:
                intent_val = getattr(getattr(s, "intent", None), "value", str(getattr(s, "intent", "CHAT")))
                steps_data.append({
                    "text": getattr(s, "text", ""),
                    "intent": intent_val,
                    "status": getattr(s, "status", "pending"),
                    "result": getattr(s, "result", ""),
                })
            return {
                "goal": getattr(self.plan, "goal", ""),
                "steps": steps_data,
            }
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "original_request": self.original_request,
            "status": self.status.value,
            "context": self.context.to_dict() if self.context else None,
            "plan": self._serialize_plan(),
            "checklist": [item.to_dict() for item in self.checklist],
            "current_step": self.current_step,
            "completed_steps": self.completed_steps,
            "observations": self.observations,
            "last_action": self.last_action.to_dict() if self.last_action else None,
            "last_result": self.last_result.to_dict() if self.last_result else None,
            "step_count": self.step_count,
            "retry_count": self.retry_count,
            "pending_confirmation": self.pending_confirmation.to_dict() if self.pending_confirmation else None,
            "waiting_for_user": self.waiting_for_user,
            "final_response": self.final_response,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentState":
        raw_status = data.get("status", "RUNNING")
        try:
            status = AgentStatus(raw_status)
        except ValueError:
            status = AgentStatus.RUNNING

        last_action = None
        if data.get("last_action"):
            last_action = AgentAction.from_dict(data["last_action"])

        last_result = None
        if data.get("last_result"):
            last_result = ToolResult.from_dict(data["last_result"])

        pending_confirmation = None
        if data.get("pending_confirmation"):
            pending_confirmation = AgentAction.from_dict(data["pending_confirmation"])

        context = None
        if data.get("context"):
            context = AgentContext.from_dict(data["context"])

        checklist: List[TaskItem] = []
        raw_checklist = data.get("checklist")
        if isinstance(raw_checklist, list):
            for item in raw_checklist:
                if isinstance(item, dict):
                    try:
                        checklist.append(TaskItem.from_dict(item))
                    except Exception:
                        pass
                elif isinstance(item, TaskItem):
                    checklist.append(item)

        plan = None
        if data.get("plan"):
            plan_data = data["plan"]
            if isinstance(plan_data, dict) and "steps" in plan_data:
                try:
                    from brain import planner
                    from brain.intents import Intent
                    steps = []
                    for s in plan_data.get("steps", []):
                        raw_intent = s.get("intent", "CHAT")
                        try:
                            step_intent = Intent(raw_intent)
                        except ValueError:
                            step_intent = Intent.CHAT
                        steps.append(planner.PlanStep(
                            text=s.get("text", ""),
                            intent=step_intent,
                            status=s.get("status", "pending"),
                            result=s.get("result", ""),
                        ))
                    plan = planner.Plan(goal=plan_data.get("goal", ""), steps=steps)
                except Exception:
                    plan = plan_data
            else:
                plan = plan_data

        return cls(
            run_id=data.get("run_id", ""),
            goal=data.get("goal", ""),
            original_request=data.get("original_request", ""),
            status=status,
            context=context,
            plan=plan,
            checklist=checklist,
            current_step=data.get("current_step", 0),
            completed_steps=data.get("completed_steps") or [],
            observations=data.get("observations") or [],
            last_action=last_action,
            last_result=last_result,
            step_count=data.get("step_count", 0),
            retry_count=data.get("retry_count", 0),
            pending_confirmation=pending_confirmation,
            waiting_for_user=bool(data.get("waiting_for_user", False)),
            final_response=data.get("final_response"),
        )

    def to_json(self) -> str:
        """Serialize AgentState to a JSON string for durable checkpointing."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "AgentState":
        """Deserialize AgentState from a JSON string checkpoint."""
        return cls.from_dict(json.loads(json_str))

