"""
brain/mood.py - simulated emotional state for Orange AI.

IMPORTANT HONESTY NOTE (read this before extending this module):
This does NOT give Orange AI real emotions -- there's no known way to do
that, and that's not what this is. This is a single tracked number
("energy", -1.0 to +1.0) that shifts in response to events (errors,
successes, idle time) and is used to change BOTH the wording AND the
actual behavior of responses (verbosity, which brain answers, whether
extra commentary gets added). It's a deterministic state machine whose
output gets woven into prompts and branching logic -- nothing more. Don't
let the word "mood" in the code obscure that.

WHY A SEPARATE MODULE (not bolted onto memory.py):
Same reasoning as reminders.py -- this is operational state about HOW
Orange AI behaves right now, not a fact about the user. Keeping it
separate keeps each module's job clear.

STATE PERSISTENCE:
Energy persists across restarts (stored in memory.db) and decays toward
neutral over time on its own -- see decay_toward_neutral(). This means
mood isn't reset every launch, but also can't get permanently stuck at
an extreme after one bad session.
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
    CREATE TABLE IF NOT EXISTS mood_state(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        energy REAL NOT NULL DEFAULT 0.0,
        updated_at TEXT NOT NULL
    )
    """)
    # CHECK (id = 1) enforces a single-row table -- there is exactly one
    # mood, not one per anything else. INSERT OR IGNORE below seeds that
    # one row only if it doesn't already exist (e.g. first ever run).
    conn.execute(
        "INSERT OR IGNORE INTO mood_state(id, energy, updated_at) VALUES (1, 0.0, ?)",
        (datetime.now().isoformat(),),
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
    """Close and reset active SQLite mood connection."""
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

# Tunable constants -- kept at the top so they're easy to find and adjust.
ENERGY_MIN = -1.0
ENERGY_MAX = 1.0
ERROR_PENALTY = -0.15
SUCCESS_BONUS = 0.05
POSITIVE_WORD_BONUS = 0.08
CLOUD_FALLBACK_PENALTY = -0.05
DECAY_RATE_PER_HOUR = 0.1
# Energy drifts 10% of the way back to 0.0 for every hour since the last
# update, when decay_toward_neutral() is called. This is what stops a
# bad run from leaving Orange AI "stuck" in a low mood indefinitely, and
# stops a great run from leaving it permanently hyped.

POSITIVE_WORDS = {
    "thanks", "thank", "great", "awesome", "perfect", "nice", "good job",
    "well done", "love it", "excellent", "amazing", "brilliant",
}

NEGATIVE_WORDS = {
    "wrong", "broken", "useless", "stupid", "terrible", "bad", "annoying",
    "frustrated", "hate", "doesn't work", "not working",
}


def _clamp(value: float) -> float:
    return max(ENERGY_MIN, min(ENERGY_MAX, value))


def get_energy() -> float:
    """Current energy value, after applying any time-based decay since
    the last update."""
    decay_toward_neutral()
    conn = get_connection()
    row = conn.execute("SELECT energy FROM mood_state WHERE id = 1").fetchone()
    return row[0] if row else 0.0


def _set_energy(value: float) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE mood_state SET energy = ?, updated_at = ? WHERE id = 1",
        (_clamp(value), datetime.now().isoformat()),
    )
    conn.commit()


def decay_toward_neutral() -> None:
    """Pull energy back toward 0.0 based on elapsed time since the last
    update. Called automatically by get_energy(), so callers don't need
    to remember to call this separately."""
    conn = get_connection()
    row = conn.execute("SELECT energy, updated_at FROM mood_state WHERE id = 1").fetchone()
    if not row:
        return

    energy, updated_at_str = row
    updated_at = datetime.fromisoformat(updated_at_str)
    hours_elapsed = (datetime.now() - updated_at).total_seconds() / 3600

    if hours_elapsed <= 0:
        return

    decay_amount = energy * min(1.0, DECAY_RATE_PER_HOUR * hours_elapsed)
    new_energy = energy - decay_amount

    conn.execute(
        "UPDATE mood_state SET energy = ?, updated_at = ? WHERE id = 1",
        (_clamp(new_energy), datetime.now().isoformat()),
    )
    conn.commit()


def apply_event(event: str) -> float:
    """
    Adjust energy based on a named event. Returns the new energy value.
    Recognized events: "error", "success", "cloud_fallback".
    """
    current = get_energy()

    deltas = {
        "error": ERROR_PENALTY,
        "success": SUCCESS_BONUS,
        "cloud_fallback": CLOUD_FALLBACK_PENALTY,
    }

    delta = deltas.get(event, 0.0)
    _set_energy(current + delta)
    return get_energy()


def apply_user_sentiment(user_text: str) -> float:
    """
    Look for simple positive/negative words in what the user just said
    and nudge energy accordingly. Deliberately simple (substring match,
    not real sentiment analysis) -- this is meant to be a light touch,
    not a core feature to over-engineer.
    """
    lowered = user_text.lower()
    current = get_energy()
    delta = 0.0

    if any(word in lowered for word in POSITIVE_WORDS):
        delta += POSITIVE_WORD_BONUS

    if any(word in lowered for word in NEGATIVE_WORDS):
        delta -= POSITIVE_WORD_BONUS

    if delta != 0.0:
        _set_energy(current + delta)

    return get_energy()


def mood_label(energy: float = None) -> str:
    """Human-readable label for a given (or current) energy value."""
    if energy is None:
        energy = get_energy()

    if energy <= -0.6:
        return "low"
    if energy <= -0.2:
        return "tired"
    if energy < 0.2:
        return "neutral"
    if energy < 0.6:
        return "upbeat"
    return "energetic"


def mood_description() -> str:
    """A short first-person-ish description, mainly for the 'mood' debug
    command and for injecting into the system prompt."""
    energy = get_energy()
    label = mood_label(energy)

    descriptions = {
        "low": "running a bit low on energy after some rough moments",
        "tired": "a little tired, things have been a bit bumpy",
        "neutral": "feeling pretty steady and even-keeled",
        "upbeat": "in a good mood, things have been going smoothly",
        "energetic": "feeling great, energetic and a bit more talkative than usual",
    }

    return f"{label} ({energy:+.2f}) -- {descriptions[label]}"

sys.modules[__name__].__class__ = DatabaseAwareModule
