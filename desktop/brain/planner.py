"""A small, auditable goal planner for Orange AI.

Plans are deliberately limited to commands Orange AI already understands.
This prevents a vague goal from becoming an improvised destructive action.
"""

from dataclasses import dataclass, field
from datetime import datetime
import os
import re
import sqlite3

from brain.intents import Intent
from brain.router import route
from typing import Optional
import sys
from brain.database import (
    resolve_database_path,
    connect_database,
    get_connection_path,
    DatabaseAwareModule,
)

_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[str] = None
_conn_is_external: bool = False
_override_db_path: Optional[str] = None
_internal_opening: bool = False


def __getattr__(name: str):
    if name == "DB_PATH":
        return get_db_path()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def get_db_path(db_path: Optional[str] = None) -> str:
    """Return the absolute path to the active SQLite database."""
    if db_path is not None and str(db_path).strip():
        return resolve_database_path(db_path)
    override = getattr(sys.modules[__name__], "_override_db_path", None)
    if override is not None:
        return resolve_database_path(override)
    if _conn_is_external and _conn_path:
        return _conn_path
    return resolve_database_path()


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS planner_outcomes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal TEXT NOT NULL,
            step_text TEXT NOT NULL,
            intent TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    global _conn, _conn_path, _conn_is_external
    mod = sys.modules[__name__]

    if _conn_is_external and _conn is not None and db_path is None:
        return _conn

    target_path = resolve_database_path(db_path or getattr(mod, "_override_db_path", None))

    if _conn is not None:
        if _conn_path == target_path:
            return _conn
        if not _conn_is_external:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _conn_path = None
        _conn_is_external = False

    mod._internal_opening = True
    try:
        new_conn = connect_database(target_path)
        _conn = new_conn
        _conn_path = target_path
        _conn_is_external = False
        _init_db(new_conn)
        return new_conn
    finally:
        mod._internal_opening = False


def close() -> None:
    """Close and reset active SQLite planner connection."""
    global _conn, _conn_path, _conn_is_external
    if _conn is not None and not _conn_is_external:
        try:
            _conn.commit()
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    _conn_is_external = False


@dataclass
class PlanStep:
    text: str
    intent: Intent
    status: str = "pending"
    result: str = ""


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)


def _split_goal(goal: str) -> list[str]:
    """Split an explicit sequence without guessing at arbitrary prose."""
    return [
        part.strip(" .")
        for part in re.split(r"\s+(?:and\s+)?then\s+", goal, flags=re.IGNORECASE)
        if part.strip(" .")
    ]


def create_plan(goal: str) -> Plan:
    """Understand a goal by mapping each stated action to an existing intent."""
    steps = [PlanStep(text=part, intent=route(part)) for part in _split_goal(goal)]
    return Plan(goal=goal.strip(), steps=steps)


def format_plan(plan: Plan) -> str:
    if not plan.steps:
        return "I need a goal to plan. Try: plan open notepad then list files"

    lines = [f"Plan for: {plan.goal}"]
    for index, step in enumerate(plan.steps, start=1):
        state = "ready" if step.intent != Intent.CHAT else "needs clarification"
        lines.append(f"{index}. [{state}] {step.text}")

    if any(step.intent == Intent.CHAT for step in plan.steps):
        lines.append("I can only execute Orange AI commands. Reword unclear steps as commands.")
    else:
        lines.append("Type 'run plan' to execute these steps, or 'cancel plan' to discard it.")
    return "\n".join(lines)


def execute_plan(plan: Plan, dispatch_command) -> str:
    """Execute known commands, observe their result, and persist each outcome."""
    if not plan.steps:
        return "There is no active plan."

    if any(step.intent == Intent.CHAT for step in plan.steps):
        return "This plan has unclear steps. Update the goal before running it."

    conn = get_connection()
    lines = []
    for index, step in enumerate(plan.steps, start=1):
        try:
            result = dispatch_command(step.intent, step.text)
            step.status = "completed" if result is not None else "failed"
            step.result = result or "The command produced no result."
        except Exception as error:
            step.status = "failed"
            step.result = f"{type(error).__name__}: {error}"

        conn.execute(
            """
            INSERT INTO planner_outcomes(goal, step_text, intent, status, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (plan.goal, step.text, step.intent.value, step.status, step.result,
             datetime.now().isoformat()),
        )
        lines.append(f"{index}. {step.status}: {step.result}")

        if step.status == "failed":
            break

    conn.commit()
    return "Plan results:\n" + "\n".join(lines)


def recent_outcomes(limit: int = 10) -> str:
    """Expose the learned execution history in a human-readable form."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT goal, step_text, status, created_at
        FROM planner_outcomes
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return "No planner outcomes recorded yet."
    return "\n".join(
        f"- [{status}] {step} (goal: {goal}; {created_at})"
        for goal, step, status, created_at in rows
    )


sys.modules[__name__].__class__ = DatabaseAwareModule
