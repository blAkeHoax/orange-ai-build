"""
tools/todo.py - Declarative Task Checklist Management Tool for Orange AI Desktop.

Provides the `todo_write` tool allowing the autonomous Agent Engine to declare,
update, and track in-flight task checklists for multi-step goals.

Adheres strictly to:
- Declarative single-call checklist replacement (Claude Code pattern).
- Input validation, bounded limits (max 20 items, 256 chars/content, 8 KB total).
- Single active in_progress task invariant (retains first, demotes subsequent to pending).
- Automatic deterministic ID assignment for missing IDs.
- Zero external OS/filesystem/process side-effects (RiskLevel.SAFE).
- Direct state update when executing within an active AgentState context.
"""

import json
from typing import Any, Dict, List, Optional

from brain.agent_models import TaskItem, TaskStatus
from brain.logger import logger

MAX_CHECKLIST_ITEMS = 20
MAX_CONTENT_LENGTH = 256
MAX_ACTIVE_FORM_LENGTH = 128
MAX_PAYLOAD_BYTES = 8192  # 8 KB limit
VALID_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.COMPLETED.value,
}


def todo_write(todos: Any, state: Optional[Any] = None) -> Dict[str, Any]:
    """
    Update or replace the structured task checklist for the current agent run.

    Args:
        todos: List of task dictionaries, each containing:
            - id (str, optional): Unique task identifier (auto-assigned if missing)
            - content (str): Description of the task (max 256 chars)
            - status (str): 'pending' | 'in_progress' | 'completed'
            - active_form (str, optional): Present-continuous summary (max 128 chars)
        state: Optional active AgentState reference for direct in-memory mutation.

    Returns:
        Structured result dict containing:
        - success (bool)
        - total_tasks (int)
        - completed (int)
        - in_progress (int)
        - pending (int)
        - active_task (str or None)
        - checklist (list of dicts)
        OR
        - success (False) and error (str)
    """
    if not isinstance(todos, list):
        return {
            "success": False,
            "error": "Invalid format: 'todos' must be a list of task objects.",
        }

    # Validate payload byte size
    try:
        payload_bytes = json.dumps(todos, ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            return {
                "success": False,
                "error": (
                    f"Checklist payload exceeds 8 KB limit "
                    f"({len(payload_bytes)} bytes > {MAX_PAYLOAD_BYTES} bytes)."
                ),
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to serialize todos payload: {e}",
        }

    # Validate maximum items
    if len(todos) > MAX_CHECKLIST_ITEMS:
        return {
            "success": False,
            "error": (
                f"Checklist exceeds maximum allowed items ({MAX_CHECKLIST_ITEMS}). "
                f"Received {len(todos)} items."
            ),
        }

    # Validate individual items
    parsed_items: List[tuple] = []
    for idx, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            return {
                "success": False,
                "error": f"Task item at index {idx} is not a valid dictionary object.",
            }

        # Content validation
        content_raw = item.get("content")
        content_str = str(content_raw).strip() if content_raw is not None else ""
        if not content_str:
            return {
                "success": False,
                "error": f"Task item at index {idx} has empty or missing 'content'.",
            }
        if len(content_str) > MAX_CONTENT_LENGTH:
            return {
                "success": False,
                "error": (
                    f"Task item at index {idx} exceeds maximum content length "
                    f"({MAX_CONTENT_LENGTH} characters). Received {len(content_str)} characters."
                ),
            }

        # Active form validation
        active_form_raw = item.get("active_form")
        active_form_str = str(active_form_raw).strip() if active_form_raw is not None else None
        if active_form_str and len(active_form_str) > MAX_ACTIVE_FORM_LENGTH:
            return {
                "success": False,
                "error": (
                    f"Task item at index {idx} exceeds maximum active_form length "
                    f"({MAX_ACTIVE_FORM_LENGTH} characters). Received {len(active_form_str)} characters."
                ),
            }

        # Status validation
        raw_status = item.get("status", TaskStatus.PENDING.value)
        if isinstance(raw_status, TaskStatus):
            status_str = raw_status.value
        else:
            status_str = str(raw_status).strip().lower() if raw_status is not None else TaskStatus.PENDING.value

        if status_str not in VALID_STATUSES:
            return {
                "success": False,
                "error": (
                    f"Task item at index {idx} has invalid status '{raw_status}'. "
                    f"Must be one of: pending, in_progress, completed."
                ),
            }

        # Deterministic ID assignment
        item_id_raw = item.get("id")
        item_id = str(item_id_raw).strip() if item_id_raw is not None and str(item_id_raw).strip() else str(idx)

        parsed_items.append((item_id, content_str, status_str, active_form_str))

    # Normalize multiple in_progress items: retain first, demote subsequent to pending
    has_active = False
    normalized_items: List[TaskItem] = []
    for item_id, content_str, status_str, active_form_str in parsed_items:
        if status_str == TaskStatus.IN_PROGRESS.value:
            if not has_active:
                has_active = True
                final_status = TaskStatus.IN_PROGRESS
            else:
                final_status = TaskStatus.PENDING
        elif status_str == TaskStatus.COMPLETED.value:
            final_status = TaskStatus.COMPLETED
        else:
            final_status = TaskStatus.PENDING

        normalized_items.append(
            TaskItem(
                id=item_id,
                content=content_str,
                status=final_status,
                active_form=active_form_str,
            )
        )

    # Compute accurate metrics
    completed = sum(1 for it in normalized_items if it.status == TaskStatus.COMPLETED)
    in_progress = sum(1 for it in normalized_items if it.status == TaskStatus.IN_PROGRESS)
    pending = sum(1 for it in normalized_items if it.status == TaskStatus.PENDING)
    active_task = next((it.content for it in normalized_items if it.status == TaskStatus.IN_PROGRESS), None)

    # Direct in-memory mutation of active AgentState if context was passed
    if state is not None and hasattr(state, "checklist"):
        state.checklist = normalized_items
        logger.info(
            "[TODO] Updated checklist for run %s: %d total (%d completed, %d in_progress, %d pending)",
            getattr(state, "run_id", "unknown"),
            len(normalized_items),
            completed,
            in_progress,
            pending,
        )

    return {
        "success": True,
        "total_tasks": len(normalized_items),
        "completed": completed,
        "in_progress": in_progress,
        "pending": pending,
        "active_task": active_task,
        "checklist": [it.to_dict() for it in normalized_items],
    }
