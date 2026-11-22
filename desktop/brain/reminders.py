"""
brain/reminders.py - reminder storage for Orange AI's proactive alerts.

This is intentionally a separate table/module from brain/memory.py's
structured_memories -- a reminder is a scheduled future event with a
"has this fired yet" state, not a fact about the user. Mixing the two
would make both harder to reason about.

Uses the same memory.db file and the same already-thread-tolerant
connection pattern (check_same_thread=False) as brain/memory.py, since
this module needs to be safely readable/writable from both the main
chat loop thread and the background scheduler thread (see
brain/scheduler.py).
"""

import os
import sqlite3
import sys
from datetime import datetime
from typing import Optional
from brain.database import (
    resolve_database_path,
    connect_database,
    get_connection_path,
    DatabaseAwareModule,
)

# Dedicated connection with standard timeout and lazy initialization.
# Threading story remains simple: short-lived queries are executed
# without holding locks across a sleep().
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
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        due_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        fired INTEGER NOT NULL DEFAULT 0
    )
    """)
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
    """Close and reset active SQLite reminders connection."""
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


def add_reminder(message: str, due_at: datetime) -> int:
    """Schedule a new reminder. due_at must be a datetime (use
    dateparser or a fixed format upstream -- this module doesn't parse
    natural language, it just stores an already-resolved datetime)."""
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO reminders(message, due_at, created_at, fired) VALUES (?, ?, ?, 0)",
        (message, due_at.isoformat(), datetime.now().isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def get_due_reminders(now: datetime) -> list[dict]:
    """Return all reminders whose due_at has passed and haven't fired yet."""
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, message, due_at FROM reminders WHERE fired = 0 AND due_at <= ?",
        (now.isoformat(),),
    )
    return [
        {"id": row[0], "message": row[1], "due_at": row[2]}
        for row in cursor.fetchall()
    ]


def mark_fired(reminder_id: int) -> None:
    """Mark a reminder as fired so it doesn't alert again."""
    conn = get_connection()
    conn.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))
    conn.commit()


def list_pending() -> list[dict]:
    """Return all reminders that haven't fired yet, soonest first."""
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, message, due_at FROM reminders WHERE fired = 0 ORDER BY due_at ASC"
    )
    return [
        {"id": row[0], "message": row[1], "due_at": row[2]}
        for row in cursor.fetchall()
    ]


def cancel_reminder(reminder_id: int) -> bool:
    """Cancel a pending reminder by id. Returns True if one was deleted."""
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM reminders WHERE id = ? AND fired = 0", (reminder_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def get_reminder(reminder_id: int) -> Optional[dict]:
    """Return a single reminder by id, or None if not found."""
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, message, due_at, created_at, fired FROM reminders WHERE id = ?",
        (reminder_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "message": row[1],
        "due_at": row[2],
        "created_at": row[3],
        "completed": bool(row[4]),
        "fired": int(row[4]),
    }


def list_reminders(include_completed: bool = False) -> list[dict]:
    """Return reminders ordered by due_at ASC.

    If include_completed is False, only pending (unfired) reminders are returned.
    If include_completed is True, both pending and completed (fired) reminders are returned.
    """
    conn = get_connection()
    if include_completed:
        cursor = conn.execute(
            "SELECT id, message, due_at, created_at, fired FROM reminders ORDER BY due_at ASC"
        )
    else:
        cursor = conn.execute(
            "SELECT id, message, due_at, created_at, fired FROM reminders WHERE fired = 0 ORDER BY due_at ASC"
        )
    return [
        {
            "id": row[0],
            "message": row[1],
            "due_at": row[2],
            "created_at": row[3],
            "completed": bool(row[4]),
            "fired": int(row[4]),
        }
        for row in cursor.fetchall()
    ]


sys.modules[__name__].__class__ = DatabaseAwareModule
