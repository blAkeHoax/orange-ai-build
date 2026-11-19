"""
brain/jobs/models.py - Data models and lifecycle contracts for Persistent Jobs.

Defines:
1. JobStatus (enum of 8 locked states)
2. Job (dataclass for persistent job records with UUID PK and 8-char display helper)
3. JobStep (dataclass for step audit logs)
"""

import json
import uuid
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from brain.agent_models import AgentAction, AgentState, ToolResult


class JobStatus(Enum):
    """
    Locked lifecycle states for persistent autonomous Jobs.
    """
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    PAUSED = "PAUSED"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def get_short_id(full_id: str, length: int = 8) -> str:
    """Helper to return the 8-character prefix of a UUID for display."""
    if not full_id:
        return ""
    return full_id[:length]


@dataclass
class Job:
    """
    Persistent Job representation.
    Survives application restarts and holds checkpointed execution state.
    """
    id: str
    goal: str
    status: JobStatus = JobStatus.PENDING
    priority: int = 0
    attempt_count: int = 0
    max_attempts: int = 3
    step_count: int = 0
    retry_delay_seconds: int = 60
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    pending_confirmation_json: Optional[str] = None
    final_response: Optional[str] = None
    agent_state_json: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    source: str = "user"
    tags: str = ""

    @property
    def short_id(self) -> str:
        """8-character identifier prefix for CLI presentation."""
        return get_short_id(self.id)

    def get_pending_confirmation(self) -> Optional[AgentAction]:
        """Deserialize pending confirmation AgentAction if present."""
        if not self.pending_confirmation_json:
            return None
        try:
            return AgentAction.from_json(self.pending_confirmation_json)
        except Exception:
            return None

    def set_pending_confirmation(self, action: Optional[AgentAction]) -> None:
        """Serialize pending confirmation AgentAction."""
        if action is None:
            self.pending_confirmation_json = None
        else:
            self.pending_confirmation_json = action.to_json()

    def get_agent_state(self) -> Optional[AgentState]:
        """Deserialize checkpointed AgentState if present."""
        if not self.agent_state_json:
            return None
        try:
            return AgentState.from_json(self.agent_state_json)
        except Exception:
            return None

    def set_agent_state(self, state: Optional[AgentState]) -> None:
        """Serialize checkpointed AgentState."""
        if state is None:
            self.agent_state_json = None
        else:
            self.agent_state_json = state.to_json()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize Job to a dictionary with primitive/explicit types."""
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status.value,
            "priority": self.priority,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "step_count": self.step_count,
            "retry_delay_seconds": self.retry_delay_seconds,
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
            "pending_confirmation_json": self.pending_confirmation_json,
            "final_response": self.final_response,
            "agent_state_json": self.agent_state_json,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "source": self.source,
            "tags": self.tags,
            "short_id": self.short_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        """Deserialize Job from a dictionary with safe defaults."""
        raw_status = data.get("status", "PENDING")
        try:
            status = JobStatus(raw_status)
        except ValueError:
            status = JobStatus.PENDING

        return cls(
            id=data.get("id", ""),
            goal=data.get("goal", ""),
            status=status,
            priority=int(data.get("priority", 0)),
            attempt_count=int(data.get("attempt_count", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            step_count=int(data.get("step_count", 0)),
            retry_delay_seconds=int(data.get("retry_delay_seconds", 60)),
            next_retry_at=data.get("next_retry_at"),
            last_error=data.get("last_error"),
            pending_confirmation_json=data.get("pending_confirmation_json"),
            final_response=data.get("final_response"),
            agent_state_json=data.get("agent_state_json"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            source=data.get("source", "user"),
            tags=data.get("tags", ""),
        )

    def to_json(self) -> str:
        """Serialize Job to a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "Job":
        """Deserialize Job from a JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class JobStep:
    """
    Durable step execution record.
    Preserves audit history for every action attempted within a Job.
    """
    id: Optional[int] = None
    job_id: str = ""
    attempt: int = 1
    step_index: int = 0
    action_type: str = "CALL_TOOL"
    tool_name: Optional[str] = None
    arguments_json: Optional[str] = None
    reasoning: str = ""
    success: Optional[bool] = None
    result_data_json: Optional[str] = None
    error: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    model_name: Optional[str] = None

    def get_arguments(self) -> Dict[str, Any]:
        """Deserialize arguments dictionary."""
        if not self.arguments_json:
            return {}
        try:
            data = json.loads(self.arguments_json)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def set_arguments(self, args: Dict[str, Any]) -> None:
        """Serialize arguments dictionary."""
        self.arguments_json = json.dumps(args, ensure_ascii=False)

    def get_result_data(self) -> Any:
        """Deserialize tool result data."""
        if not self.result_data_json:
            return None
        try:
            return json.loads(self.result_data_json)
        except Exception:
            return self.result_data_json

    def set_result_data(self, data: Any) -> None:
        """Serialize tool result data."""
        self.result_data_json = json.dumps(data, ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize JobStep to a dictionary."""
        return {
            "id": self.id,
            "job_id": self.job_id,
            "attempt": self.attempt,
            "step_index": self.step_index,
            "action_type": self.action_type,
            "tool_name": self.tool_name,
            "arguments_json": self.arguments_json,
            "reasoning": self.reasoning,
            "success": self.success,
            "result_data_json": self.result_data_json,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "model_name": self.model_name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobStep":
        """Deserialize JobStep from a dictionary."""
        raw_success = data.get("success")
        success_val = bool(raw_success) if raw_success is not None else None

        return cls(
            id=data.get("id"),
            job_id=data.get("job_id", ""),
            attempt=int(data.get("attempt", 1)),
            step_index=int(data.get("step_index", 0)),
            action_type=data.get("action_type", "CALL_TOOL"),
            tool_name=data.get("tool_name"),
            arguments_json=data.get("arguments_json"),
            reasoning=data.get("reasoning", ""),
            success=success_val,
            result_data_json=data.get("result_data_json"),
            error=data.get("error"),
            started_at=data.get("started_at", datetime.now(timezone.utc).isoformat()),
            completed_at=data.get("completed_at"),
            duration_ms=data.get("duration_ms"),
            model_name=data.get("model_name"),
        )

    def to_json(self) -> str:
        """Serialize JobStep to a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "JobStep":
        """Deserialize JobStep from a JSON string."""
        return cls.from_dict(json.loads(json_str))
