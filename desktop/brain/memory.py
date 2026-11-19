"""
Orange AI Memory 2.0

Backwards compatible with Memory 1.x

Features
--------
• Long-term memory
• Structured memory
• Conversation history
• Automatic timestamps
• Memory categories
• Memory search
• Migration support
"""

import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Optional


# ==========================================================
# DATABASE
# ==========================================================

from brain.paths import get_project_root
from brain.database import (
    resolve_database_path,
    connect_database,
    get_connection_path,
    DatabaseAwareModule,
    DEFAULT_TIMEOUT,
    DEFAULT_BUSY_TIMEOUT_MS,
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
    """Return the absolute path to the active SQLite memory database."""
    if db_path is not None and str(db_path).strip():
        return resolve_database_path(db_path)
    override = getattr(sys.modules[__name__], "_override_db_path", None)
    if override is not None:
        return resolve_database_path(override)
    if _conn_is_external and _conn_path:
        return _conn_path
    return resolve_database_path()


def connect_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Open an SQLite connection configured with the standard timeout and busy handler policy.
    Sets timeout=10.0, PRAGMA journal_mode=WAL;, and PRAGMA busy_timeout = 5000;.
    Safe and idempotent. Delegates to brain.database.
    """
    target_path = resolve_database_path(db_path or get_db_path())
    return connect_database(target_path)


def _init_tables(conn: sqlite3.Connection) -> None:
    """
    Create core tables and perform schema validation idempotently.
    Called on first connection initialization.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "created_at" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN created_at TEXT DEFAULT ''")
            conn.commit()
    except Exception:
        pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS structured_memories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            memory_key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(category, memory_key)
        )
        """
    )
    conn.commit()


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Return the active SQLite connection, lazily opening and initializing tables on first access.
    - If an external connection was injected (e.g. test fixture _conn = self.test_conn)
      and db_path is None, returns that connection directly without closing.
    - If target path matches the active connection, returns it.
    - If target path changes and the previous connection was managed internally,
      cleanly commits and closes the stale connection.
    """
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
        _init_tables(new_conn)
        return new_conn
    finally:
        mod._internal_opening = False


def close() -> None:
    """Safely commit and close the active SQLite connection if managed."""
    global _conn, _conn_path, _conn_is_external
    if _conn is not None and not _conn_is_external:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    _conn_is_external = False



# ==========================================================
# LEGACY MEMORY API
# ==========================================================

def remember(text: str) -> int:
    """
    Save a normal memory.

    This keeps old code working.
    """
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO memories(
            content,
            created_at
        )
        VALUES (?, ?)
        """,
        (
            text,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def recall(limit: int = 50) -> list[str]:
    """
    Return memories.

    Oldest first.
    """
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT content
        FROM memories
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [row[0] for row in cursor.fetchall()]
    rows.reverse()
    return rows


def count_legacy() -> int:
    """
    Return total count of legacy memories.
    """
    conn = get_connection()
    cursor = conn.execute("SELECT COUNT(*) FROM memories")
    row = cursor.fetchone()
    return row[0] if row else 0


def escape_like(s: str) -> str:
    """
    Escape SQLite LIKE metacharacters: backslash, percent, and underscore.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def prune_active_history(active_history: list, keyword: str) -> int:
    """
    Prune turns matching keyword from the active in-memory conversation history in-place.
    Matches case-insensitively against turn content.
    Returns the number of turns removed.
    """
    if not isinstance(active_history, list):
        return 0

    clean_kw = keyword.strip()
    if len(clean_kw) < 3:
        return 0

    target = clean_kw.lower()
    kept = []
    removed = 0
    for turn in active_history:
        if isinstance(turn, dict) and target in str(turn.get("content", "")).lower():
            removed += 1
        else:
            kept.append(turn)

    if removed > 0:
        active_history[:] = kept

    return removed


def forget(keyword: str, active_history: Optional[list] = None) -> int:
    """
    Delete memories matching keyword from legacy memories, structured memories,
    and conversation history.
    Optionally synchronizes an active in-memory conversation history list in-place
    upon successful database deletion.
    Returns the total number of deleted records across all three persistent stores.
    Enforces a minimum keyword length of 3 characters (trimmed) and escapes LIKE wildcards.
    Ensures atomic execution: all persistent deletions succeed together or roll back on error.
    If database deletion fails, active_history is not modified.
    """
    clean_kw = keyword.strip()
    if len(clean_kw) < 3:
        return 0

    escaped = escape_like(clean_kw)
    pattern = f"%{escaped}%"
    conn = get_connection()

    with conn:
        cursor_legacy = conn.execute(
            """
            DELETE
            FROM memories
            WHERE content LIKE ? ESCAPE '\\'
            """,
            (pattern,),
        )
        legacy_count = cursor_legacy.rowcount

        cursor_structured = conn.execute(
            """
            DELETE
            FROM structured_memories
            WHERE category LIKE ? ESCAPE '\\'
               OR memory_key LIKE ? ESCAPE '\\'
               OR value LIKE ? ESCAPE '\\'
            """,
            (pattern, pattern, pattern),
        )
        structured_count = cursor_structured.rowcount

        cursor_history = conn.execute(
            """
            DELETE
            FROM conversation_history
            WHERE content LIKE ? ESCAPE '\\'
            """,
            (pattern,),
        )
        history_count = cursor_history.rowcount

    # Synchronize active in-memory history ONLY after the SQLite transaction commits successfully
    if active_history is not None:
        prune_active_history(active_history, clean_kw)

    return legacy_count + structured_count + history_count


def clear_all():
    conn = get_connection()
    conn.execute("DELETE FROM memories")
    conn.execute("DELETE FROM structured_memories")
    conn.commit()
    from brain.conflict_resolver import clear_session_retractions
    clear_session_retractions()

    # ==========================================================
# STRUCTURED MEMORY API
# ==========================================================

def should_preserve_existing_project_description(existing_val: str, new_val: str) -> bool:
    """
    Returns True if existing_val has richer descriptive metadata and new_val is merely a bare-name mention.
    Returns False if new_val contains an explicit updated description or notes.
    """
    if not existing_val:
        return False

    e_clean = existing_val.strip()
    n_clean = new_val.strip()

    if e_clean.lower() == n_clean.lower():
        return True

    e_has_notes = bool(re.search(r"\(.*?\)", e_clean) or " - " in e_clean or ": " in e_clean)
    n_has_notes = bool(re.search(r"\(.*?\)", n_clean) or " - " in n_clean or ": " in n_clean)

    if e_has_notes and not n_has_notes:
        e_bare = re.sub(r"\(.*?\)", "", e_clean).strip().lower()
        e_words = set(re.findall(r"\w+", e_bare))
        n_words = set(re.findall(r"\w+", n_clean.lower()))

        if n_words.issubset(e_words) or len(n_words - {"project", "a", "the", "my"}) == 0:
            return True

        if len(e_clean) > len(n_clean) + 15:
            meaningful_e = e_words - {"project", "a", "the", "my", "legacy"}
            meaningful_n = n_words - {"project", "a", "the", "my", "legacy"}
            if meaningful_e & meaningful_n:
                return True

    return False


def remember_structured(
    category: str,
    key: str,
    value: str,
    confidence: float = 1.0,
):
    """
    Save or update structured memory.
    """
    cat_clean = category.lower().strip()
    key_clean = key.lower().strip()
    val_clean = value.strip()

    # Stage 3.4 & 3.6C: Project description preservation & primary focus management
    if cat_clean == "project":
        existing_val = get_memory("project", key_clean)
        if key_clean == "current":
            from brain.memory_classifier import _slugify_key
            if existing_val:
                old_slug = _slugify_key(existing_val)
                new_slug = _slugify_key(val_clean)
                if old_slug == new_slug:
                    # Same project: preserve richer descriptive notes if present
                    if should_preserve_existing_project_description(existing_val, val_clean):
                        val_clean = existing_val
        else:
            if existing_val and should_preserve_existing_project_description(existing_val, val_clean):
                val_clean = existing_val

    # Unregister from session retractions if previously retired
    from brain.conflict_resolver import unregister_session_retraction
    unregister_session_retraction(cat_clean, val_clean)
    if key_clean and key_clean != val_clean:
        unregister_session_retraction(cat_clean, key_clean)

    now = datetime.now().isoformat()
    conn = get_connection()

    conn.execute(
        """
        INSERT INTO structured_memories(
            category,
            memory_key,
            value,
            confidence,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(category, memory_key)
        DO UPDATE SET
            value = excluded.value,
            confidence = excluded.confidence,
            updated_at = excluded.updated_at
        """,
        (
            cat_clean,
            key_clean,
            val_clean,
            confidence,
            now,
            now,
        ),
    )
    conn.commit()


def get_memory(category: str, key: str):
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT value
        FROM structured_memories
        WHERE category = ?
        AND memory_key = ?
        """,
        (
            category.lower(),
            key.lower(),
        ),
    )
    row = cursor.fetchone()
    if row:
        return row[0]
    return None


def update_memory(
    category: str,
    key: str,
    value: str,
):
    """
    Update an existing memory.
    """
    remember_structured(
        category,
        key,
        value,
    )


def delete_memory(
    category: str,
    key: str,
):
    conn = get_connection()
    cursor = conn.execute(
        """
        DELETE
        FROM structured_memories
        WHERE category = ?
        AND memory_key = ?
        """,
        (
            category.lower().strip(),
            key.lower().strip(),
        ),
    )
    conn.commit()
    return cursor.rowcount


def _execute_retraction_deletions(
    conn: sqlite3.Connection,
    category: str,
    key: str,
    value: Optional[str] = None,
    include_history: bool = False,
) -> int:
    """
    Execute all persistent deletions for entity retraction in a caller-owned transaction.
    Does NOT commit or manage transactions; the caller must wrap this in 'with conn:' or handle commit/rollback.
    Returns the count of structured records deleted.
    """
    clean_cat = category.strip().lower()
    clean_key = key.strip().lower()
    if not clean_cat or not clean_key:
        return 0

    cursor = conn.execute(
        """
        DELETE FROM structured_memories
        WHERE category = ? AND memory_key = ?
        """,
        (clean_cat, clean_key),
    )
    deleted_count = cursor.rowcount

    # Stage 3.6C: If a project is retracted and the current primary project matches it, clear current as well
    if clean_cat == "project":
        curr_row = conn.execute(
            "SELECT value FROM structured_memories WHERE category = 'project' AND memory_key = 'current'"
        ).fetchone()
        if curr_row and curr_row[0]:
            from brain.memory_classifier import _slugify_key
            curr_slug = _slugify_key(curr_row[0])
            if curr_slug == clean_key or (value and curr_slug == _slugify_key(value)):
                conn.execute(
                    "DELETE FROM structured_memories WHERE category = 'project' AND memory_key = 'current'"
                )

    # Targeted legacy fact reconciliation (exact phrase only, never broad substring)
    if value:
        val_clean = value.strip().lower()
        if clean_cat == "tool":
            conn.execute(
                """
                DELETE FROM memories
                WHERE lower(trim(content)) IN (?, ?, ?, ?)
                """,
                (
                    f"i use {val_clean}",
                    f"i use {clean_key}",
                    f"my preferred tool is {val_clean}",
                    f"i am using {val_clean}",
                ),
            )
        elif clean_cat == "preference":
            conn.execute(
                """
                DELETE FROM memories
                WHERE lower(trim(content)) IN (?, ?, ?, ?)
                """,
                (
                    f"i like {val_clean}",
                    f"i love {val_clean}",
                    f"i prefer {val_clean}",
                    f"my favorite is {val_clean}",
                ),
            )
        elif clean_cat == "project":
            conn.execute(
                """
                DELETE FROM memories
                WHERE lower(trim(content)) IN (?, ?, ?, ?)
                """,
                (
                    f"i am working on {val_clean}",
                    f"i'm working on {val_clean}",
                    f"my current project is {val_clean}",
                    f"working on {val_clean}",
                ),
            )

        if include_history:
            if clean_cat == "tool":
                conn.execute(
                    """
                    DELETE FROM conversation_history
                    WHERE lower(trim(content)) IN (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"i use {val_clean}",
                        f"i use {val_clean}.",
                        f"i am using {val_clean}",
                        f"i'm using {val_clean}",
                        f"my preferred tool is {val_clean}",
                        f"i stopped using {val_clean}",
                        f"i am no longer using {val_clean}",
                        f"i'm no longer using {val_clean}",
                    ),
                )
            elif clean_cat == "project":
                conn.execute(
                    """
                    DELETE FROM conversation_history
                    WHERE lower(trim(content)) IN (?, ?, ?, ?, ?)
                    """,
                    (
                        f"i am working on {val_clean}",
                        f"i'm working on {val_clean}",
                        f"my current project is {val_clean}",
                        f"working on {val_clean}",
                        f"i stopped working on {val_clean}",
                    ),
                )
            elif clean_cat == "preference":
                conn.execute(
                    """
                    DELETE FROM conversation_history
                    WHERE lower(trim(content)) IN (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"i like {val_clean}",
                        f"i love {val_clean}",
                        f"i prefer {val_clean}",
                        f"my favorite is {val_clean}",
                        f"i don't like {val_clean}",
                        f"i do not like {val_clean}",
                    ),
                )

    return deleted_count


def retire_structured(category: str, key: str, value: Optional[str] = None) -> int:
    """
    Safely and idempotently retire a structured memory by exact category and memory_key.
    Also reconciles exact matching legacy assertions if applicable.
    Never uses broad substring deletion.
    Returns the number of structured records deleted.
    """
    clean_cat = category.strip().lower()
    clean_key = key.strip().lower()
    if not clean_cat or not clean_key:
        return 0

    conn = get_connection()
    with conn:
        return _execute_retraction_deletions(
            conn, clean_cat, clean_key, value, include_history=False
        )


def list_category(category: str):
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT
            memory_key,
            value,
            confidence
        FROM structured_memories
        WHERE category = ?
        ORDER BY memory_key
        """,
        (category.lower(),),
    )
    return [
        {
            "key": key,
            "value": value,
            "confidence": confidence,
        }
        for key, value, confidence in cursor.fetchall()
    ]


def search_memory(query: str):
    query = f"%{query}%"
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT
            category,
            memory_key,
            value,
            confidence
        FROM structured_memories
        WHERE
            category LIKE ?
            OR memory_key LIKE ?
            OR value LIKE ?
        ORDER BY
            category,
            memory_key
        """,
        (
            query,
            query,
            query,
        ),
    )
    return [
        {
            "category": category,
            "key": key,
            "value": value,
            "confidence": confidence,
        }
        for category, key, value, confidence
        in cursor.fetchall()
    ]


def recall_structured() -> list[dict]:
    """
    Return all structured memories ordered by category and memory_key.
    """
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT category, memory_key, value, confidence
        FROM structured_memories
        ORDER BY category, memory_key
        """
    )
    return [
        {
            "category": row[0],
            "key": row[1],
            "value": row[2],
            "confidence": row[3],
        }
        for row in cursor.fetchall()
    ]


def recall_reconciled(limit: int = 20) -> list[str]:
    """
    Return legacy memories reconciled against current structured memories.
    Suppresses legacy statements that are directly superseded or duplicated by structured memories,
    while preserving unrelated legacy notes and facts.
    """
    raw_memories = recall(limit=limit)
    if not raw_memories:
        return []

    structured = recall_structured()
    if not structured:
        return raw_memories

    active_by_cat: dict[str, list[tuple[str, str]]] = {}
    for s in structured:
        c = s["category"].lower()
        k = s["key"].lower()
        v = s["value"].lower()
        active_by_cat.setdefault(c, []).append((k, v))

    reconciled = []
    for mem in raw_memories:
        if not _is_legacy_conflict(mem, active_by_cat):
            reconciled.append(mem)

    return reconciled


# ==========================================================
# MEMORY MIGRATION
# ==========================================================

def migrate_existing_memories():
    """
    Migrate old memories into structured memories safely and idempotently.

    Existing memories are NOT deleted.
    Does not overwrite existing structured memories.
    """
    from brain.memory_classifier import _slugify_key

    memories = recall(limit=100000)

    for memory in memories:

        text = memory.lower().strip()

        if text.startswith("my name is"):
            val = memory[11:].strip()
            if get_memory("identity", "name") is None:
                remember_structured(
                    category="identity",
                    key="name",
                    value=val,
                )
            continue

        if text.startswith("i am your creator"):
            if get_memory("relationship", "creator") is None:
                remember_structured(
                    category="relationship",
                    key="creator",
                    value="User",
                )
            continue

        if text.startswith("i like"):
            val = memory[6:].strip()
            key = _slugify_key(val, prefix="like_")
            if get_memory("preference", key) is None:
                remember_structured(
                    category="preference",
                    key=key,
                    value=val,
                )
            continue

        if text.startswith("my favorite"):
            fav_match = re.search(r"my favorite (.+?) is (.+)", memory, re.IGNORECASE)
            if fav_match:
                item = fav_match.group(1).strip()
                val = fav_match.group(2).strip()
                key = f"favorite_{_slugify_key(item)}"
            else:
                val = memory[11:].strip()
                key = f"favorite_{_slugify_key(val)}"

            if get_memory("preference", key) is None:
                remember_structured(
                    category="preference",
                    key=key,
                    value=val,
                )
            continue

        # ==========================================================
# CONVERSATION HISTORY
# ==========================================================

def save_turn(role: str, content: str):
    """
    Save one conversation turn.
    """
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO conversation_history(
            role,
            content,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            role,
            content,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()


def load_history(limit: int = 40):
    """
    Load recent conversation history.

    Oldest first.
    """
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT
            role,
            content
        FROM conversation_history
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    rows.reverse()
    return [
        {
            "role": role,
            "content": content,
        }
        for role, content in rows
    ]


def clear_history():
    conn = get_connection()
    conn.execute(
        "DELETE FROM conversation_history"
    )
    conn.commit()


# ==========================================================
# MEMORY STATISTICS
# ==========================================================

def memory_stats():
    """
    Return useful memory statistics.
    """
    conn = get_connection()
    legacy_count = conn.execute(
        "SELECT COUNT(*) FROM memories"
    ).fetchone()[0]

    structured_count = conn.execute(
        "SELECT COUNT(*) FROM structured_memories"
    ).fetchone()[0]

    conversation_count = conn.execute(
        "SELECT COUNT(*) FROM conversation_history"
    ).fetchone()[0]

    return {
        "legacy_memories": legacy_count,
        "structured_memories": structured_count,
        "conversation_turns": conversation_count,
        "total": (
            legacy_count
            + structured_count
        ),
    }


# ==========================================================
# EXPORT USER PROFILE
# ==========================================================

def build_profile():
    """
    Build a structured profile for the LLM prompt.
    """
    conn = get_connection()
    cursor = conn.execute(
        """
        SELECT
            category,
            memory_key,
            value
        FROM structured_memories
        ORDER BY
            category,
            memory_key
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return ""

    profile = []
    current_category = None
    for category, key, value in rows:
        if category != current_category:
            current_category = category
            profile.append(
                f"\n[{category.upper()}]"
            )
        profile.append(
            f"{key}: {value}"
        )

    return "\n".join(profile)


# ==========================================================
# INITIALIZATION
# ==========================================================

def initialize_memory(db_path: str = None) -> sqlite3.Connection:
    """
    Explicitly initialize memory database connection and run startup tasks.
    Safe to call every launch.
    """
    conn = get_connection(db_path)
    migrate_existing_memories()
    return conn


# ==========================================================
# CLEAN SHUTDOWN
# ==========================================================

def close():
    global _conn, _conn_path
    if _conn is not None:
        try:
            _conn.commit()
            _conn.close()
        except Exception:
            pass
        finally:
            _conn = None
            _conn_path = None


# ==========================================================
# STAGE 3.5: MEMORY INSPECTION & TARGETED RETRACTION
# ==========================================================

def inspect_profile() -> dict:
    """
    Return human-readable profile facts organized by category,
    hiding internal keys, generated slugs, and hash suffixes.
    Strictly read-only; performs no database writes.
    """
    structured = recall_structured()
    profile: dict[str, list[str]] = {
        "identity": [],
        "projects": [],
        "tools": [],
        "preferences": [],
        "goals": [],
    }
    for item in structured:
        cat = item["category"].lower()
        key = item["key"].lower()
        val = item["value"].strip()
        if not val:
            continue

        if cat == "identity":
            attr = key.replace("_", " ").title()
            profile["identity"].append(f"{attr}: {val}")
        elif cat == "project":
            profile["projects"].append(val)
        elif cat == "tool":
            profile["tools"].append(val)
        elif cat == "preference":
            profile["preferences"].append(val)
        elif cat == "goal":
            profile["goals"].append(val)
        else:
            profile.setdefault(cat, []).append(val)

    return profile


def format_profile_inspection() -> str:
    """
    Format the user's structured profile into a human-readable display string.
    Omits internal keys and hash suffixes.
    Returns a helpful empty notice if no profile data exists.
    """
    profile = inspect_profile()
    has_any = any(len(items) > 0 for items in profile.values())
    if not has_any:
        return "Orange AI: No profile information recorded yet."

    lines = ["Orange AI Profile:\n"]
    category_titles = [
        ("identity", "Identity"),
        ("projects", "Projects"),
        ("tools", "Tools"),
        ("preferences", "Preferences"),
        ("goals", "Goals"),
    ]

    for cat_key, title in category_titles:
        items = profile.get(cat_key, [])
        if items:
            lines.append(f"{title}:")
            for item in items:
                lines.append(f"  • {item}")
            lines.append("")

    return "\n".join(lines).strip()


def inspect_category(category: str) -> list[str]:
    """
    Return active human-readable values for a specific category.
    Strictly read-only; hides internal keys and hashes.
    """
    records = list_category(category)
    return [r["value"].strip() for r in records if r.get("value", "").strip()]


def format_category_inspection(category: str) -> str:
    """
    Format inspection of a specific category for user display.
    Provides helpful empty-state messages when no active facts exist.
    """
    cat_clean = category.strip().lower()
    values = inspect_category(cat_clean)

    display_names = {
        "project": ("Projects", "active projects"),
        "tool": ("Tools", "active tools"),
        "preference": ("Preferences", "active preferences"),
        "identity": ("Identity", "identity information"),
        "goal": ("Goals", "active goals"),
    }
    title, plural_noun = display_names.get(cat_clean, (cat_clean.title(), f"active {cat_clean} items"))

    if not values:
        return f"Orange AI: No {plural_noun} recorded."

    lines = [f"Active {title}:"]
    for val in values:
        lines.append(f"  • {val}")
    return "\n".join(lines)


def prune_targeted_history(
    active_history: list,
    category: str,
    entity_name: str,
    canonical_key: Optional[str] = None,
) -> int:
    """
    Prune only turns from active in-memory conversation history that are unambiguous,
    memory-bearing assertions, retirement statements, or system acknowledgements for
    the specified retracted entity.
    Preserves all unrelated turns, even if they mention the entity keyword in passing
    (e.g., 'The deployment report discusses Docker container security.').
    Preserves third-party statements, questions, and hypotheticals.
    Returns the number of turns removed.
    """
    if not isinstance(active_history, list) or not entity_name:
        return 0

    from brain.memory_classifier import _is_uncertain_or_question

    cat_clean = category.strip().lower()
    name_clean = entity_name.strip()
    name_escaped = re.escape(name_clean)
    key_clean = (canonical_key or "").strip().lower()

    # Define narrow, explainable regex patterns for memory-bearing statements
    if cat_clean == "tool":
        pos_pattern = re.compile(
            r"\b(?:i\s+use|(?:i\s+am|i'm)\s+(?:using|working\s+with)|my\s+preferred\s+tool\s+is)\s+"
            + name_escaped + r"(?:\s+(?:for|as|in)\b|[.,;!]|$)",
            re.IGNORECASE,
        )
        ret_pattern = re.compile(
            r"\b(?:i\s+stopped\s+using|(?:i\s+am|i'm)\s+no\s+longer\s+using|(?:i\s+am|i'm)\s+not\s+using|i\s+no\s+longer\s+use|i\s+(?:don't|do\s+not)\s+use)\s+"
            + name_escaped + r"(?:\s+(?:anymore|for|as|in)\b|[.,;!]|$)",
            re.IGNORECASE,
        )
        cmd_pattern = re.compile(r"^\s*(?:retire|forget)\s+tool\s+" + name_escaped + r"(?:[.,;!]|$)", re.IGNORECASE)
    elif cat_clean == "project":
        pos_pattern = re.compile(
            r"\b(?:(?:i\s+am|i'm)\s+working\s+on|my\s+current\s+project\s+is|working\s+on)\s+"
            + name_escaped + r"(?:[.,;!]|$)",
            re.IGNORECASE,
        )
        ret_pattern = re.compile(
            r"\b(?:i\s+stopped\s+working\s+on|(?:i\s+am|i'm)\s+no\s+longer\s+working\s+on)\s+"
            + name_escaped + r"(?:[.,;!]|$)",
            re.IGNORECASE,
        )
        cmd_pattern = re.compile(r"^\s*forget\s+project\s+" + name_escaped + r"(?:[.,;!]|$)", re.IGNORECASE)
    elif cat_clean == "preference":
        pos_pattern = re.compile(
            r"\b(?:i\s+(?:like|love|prefer)|my\s+favorite\s+\w+\s+is)\s+"
            + name_escaped + r"(?:[.,;!]|$)",
            re.IGNORECASE,
        )
        ret_pattern = re.compile(
            r"\b(?:i\s+(?:don't|do\s+not)\s+like|i\s+no\s+longer\s+like|i\s+dislike)\s+"
            + name_escaped + r"(?:\s+anymore|[.,;!]|$)",
            re.IGNORECASE,
        )
        cmd_pattern = re.compile(r"^\s*forget\s+preference\s+" + name_escaped + r"(?:[.,;!]|$)", re.IGNORECASE)
    else:
        pos_pattern = re.compile(r"\b" + name_escaped + r"\b", re.IGNORECASE)
        ret_pattern = None
        cmd_pattern = None

    kept = []
    removed = 0

    for turn in active_history:
        if not isinstance(turn, dict):
            kept.append(turn)
            continue

        content = str(turn.get("content", "")).strip()
        role = str(turn.get("role", "")).lower()

        is_match = False
        # Assistant acknowledgement matching
        if role == "assistant":
            lower_content = content.lower()
            if key_clean and f"learned: {cat_clean} -> {key_clean}" in lower_content:
                is_match = True
            elif key_clean and f"retired: {cat_clean} -> {key_clean}" in lower_content:
                is_match = True
            elif f"retired {cat_clean} '{name_clean.lower()}'" in lower_content:
                is_match = True

        # User memory assertion matching
        if not is_match and role == "user":
            # Questions, hypotheticals, and uncertain statements are strictly preserved
            if not (content.endswith("?") or _is_uncertain_or_question(content)):
                if pos_pattern.search(content):
                    is_match = True
                elif ret_pattern and ret_pattern.search(content):
                    is_match = True
                elif cmd_pattern and cmd_pattern.search(content):
                    is_match = True

        if is_match:
            removed += 1
        else:
            kept.append(turn)

    if removed > 0:
        active_history[:] = kept

    return removed


def resolve_retraction_target(
    category: str,
    name: str,
) -> tuple[Optional[dict], list[str], str]:
    """
    Resolve a user-supplied name against existing structured memory records in the category.
    Returns:
        (matched_record, candidates, status)
        where status is 'matched', 'not_found', or 'ambiguous'.
    """
    cat_clean = category.strip().lower()
    raw_query = name.strip()
    if not raw_query:
        return None, [], "not_found"

    records = list_category(cat_clean)
    if not records:
        return None, [], "not_found"

    q_lower = raw_query.lower()
    if cat_clean == "project":
        for prefix in ("project ", "a ", "the "):
            if q_lower.startswith(prefix) and len(q_lower) > len(prefix):
                q_lower = q_lower[len(prefix):].strip()
                break
    elif cat_clean == "tool":
        for prefix in ("tool ", "a ", "the "):
            if q_lower.startswith(prefix) and len(q_lower) > len(prefix):
                q_lower = q_lower[len(prefix):].strip()
                break
    elif cat_clean == "preference":
        for prefix in ("preference ", "like "):
            if q_lower.startswith(prefix) and len(q_lower) > len(prefix):
                q_lower = q_lower[len(prefix):].strip()
                break

    # 1. Exact match on value (case-insensitive)
    exact_val_matches = [
        r for r in records
        if r["value"].strip().lower() == q_lower or r["value"].strip().lower() == raw_query.lower()
    ]
    if len(exact_val_matches) == 1:
        return exact_val_matches[0], [], "matched"

    # 2. Canonical key match
    from brain.memory_classifier import _slugify_key
    if cat_clean == "tool":
        cand_key = _slugify_key(raw_query, prefix="tool_").lower()
    elif cat_clean == "preference":
        cand_key = _slugify_key(raw_query, prefix="like_").lower()
    else:
        cand_key = _slugify_key(raw_query).lower()

    exact_key_matches = [
        r for r in records
        if r["key"].lower() == cand_key or r["key"].lower() == f"favorite_{_slugify_key(raw_query).lower()}"
    ]
    if len(exact_key_matches) == 1:
        return exact_key_matches[0], [], "matched"

    # 3. Normalized slug comparison on value
    slug_matches = [
        r for r in records
        if _slugify_key(r["value"]).lower() == _slugify_key(raw_query).lower()
    ]
    if len(slug_matches) == 1:
        return slug_matches[0], [], "matched"

    # 4. Partial substring matches
    partial_matches = []
    for r in records:
        val_lower = r["value"].strip().lower()
        key_lower = r["key"].strip().lower()
        if q_lower in val_lower or q_lower in key_lower:
            partial_matches.append(r)

    if len(partial_matches) == 1:
        return partial_matches[0], [], "matched"
    elif len(partial_matches) > 1:
        candidates = [r["value"] for r in partial_matches]
        return None, candidates, "ambiguous"

    return None, [], "not_found"


def retract_entity(
    category: str,
    name: str,
    active_history: Optional[list] = None,
) -> dict:
    """
    Safely and transactionally retract a specific structured entity by category and name.
    Preserves unrelated structured memories, legacy memories, and historical turns.
    Returns a result dict with:
      - status: 'removed', 'not_found', or 'ambiguous'
      - message: human-readable confirmation or clarification request
      - deleted_structured: count of structured records deleted
      - pruned_history_turns: count of in-session turns pruned
    """
    cat_clean = category.strip().lower()
    name_clean = name.strip()

    target_rec, candidates, status = resolve_retraction_target(cat_clean, name_clean)

    if status == "not_found":
        return {
            "status": "not_found",
            "category": cat_clean,
            "name": name_clean,
            "deleted_structured": 0,
            "pruned_history_turns": 0,
            "message": f"No active {cat_clean} matching '{name_clean}' was found.",
        }

    if status == "ambiguous":
        cand_list = ", ".join(f"'{c}'" for c in candidates)
        return {
            "status": "ambiguous",
            "category": cat_clean,
            "name": name_clean,
            "candidates": candidates,
            "deleted_structured": 0,
            "pruned_history_turns": 0,
            "message": f"Multiple matching {cat_clean}s found: {cand_list}. Please specify the exact name.",
        }

    # Status == 'matched'
    key = target_rec["key"]
    val = target_rec["value"]

    # 1. Execute all persistent deletions in ONE atomic SQLite transaction
    conn = get_connection()
    with conn:
        deleted_structured = _execute_retraction_deletions(
            conn, cat_clean, key, val, include_history=True
        )

    # 2. Synchronize active in-memory history ONLY after the entire database transaction commits successfully
    pruned_turns = 0
    if active_history is not None:
        pruned_turns = prune_targeted_history(
            active_history=active_history,
            category=cat_clean,
            entity_name=val,
            canonical_key=key,
        )

    # Stage 3.6C: Register in process-lifetime session suppression registry (Tier 2)
    from brain.conflict_resolver import register_session_retraction
    register_session_retraction(cat_clean, name_clean)
    register_session_retraction(cat_clean, val)
    if key and key != val:
        register_session_retraction(cat_clean, key)

    return {
        "status": "removed",
        "category": cat_clean,
        "name": val,
        "key": key,
        "deleted_structured": deleted_structured,
        "pruned_history_turns": pruned_turns,
        "message": f"🗑️ Retired {cat_clean} '{val}'.",
    }
# ==========================================================
# STAGE 3.6A: UNIFIED RELEVANCE-RANKED RETRIEVAL ENGINE
# ==========================================================

STOP_WORDS = frozenset({
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "because", "been", "before", "being", "both", "but", "by",
    "can", "could", "did", "do", "does", "each", "few", "for", "from", "had",
    "has", "have", "he", "her", "here", "him", "his", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "more", "most", "my", "no", "nor",
    "not", "of", "on", "only", "or", "other", "our", "out", "own", "same",
    "she", "should", "so", "some", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "through", "to",
    "too", "under", "up", "us", "very", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "will", "with", "would",
    "you", "your",
})


def tokenize_query(text: str) -> list[str]:
    """
    Extract meaningful search tokens from a query string.
    Normalizes case and whitespace, removes punctuation-only tokens,
    and filters common English stop-words while preserving domain identifiers,
    acronyms, version numbers, and technical terms.
    """
    if not text or not isinstance(text, str):
        return []

    # Find words with letters, digits, dots, hyphens (e.g. 'v2.0', 'c++', 'k8s')
    raw_tokens = re.findall(r"\b[\w\.\-\+]{1,50}\b", text.lower())
    clean_tokens = []
    for tok in raw_tokens:
        tok_clean = tok.strip(".-_")
        if not tok_clean:
            continue
        if tok_clean not in STOP_WORDS:
            clean_tokens.append(tok_clean)

    return clean_tokens


def _calculate_recency_score(iso_timestamp: Optional[str]) -> float:
    """
    Calculate a modest, strictly bounded recency score between 0.0 and 0.3.
    Relevance dominates recency; recency serves only as a gentle tie-breaker
    between facts with equal term relevance.
    Handles missing or malformed timestamps safely without raising exceptions.
    """
    if not iso_timestamp or not isinstance(iso_timestamp, str):
        return 0.0

    try:
        dt = datetime.fromisoformat(iso_timestamp)
        now = datetime.now()
        age_days = (now - dt).total_seconds() / 86400.0
        if age_days < 0:
            return 0.3
        elif age_days <= 1.0:
            return 0.3
        elif age_days <= 7.0:
            return 0.2
        elif age_days <= 30.0:
            return 0.1
        else:
            return 0.05
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _is_legacy_conflict(
    content: str,
    active_by_cat: dict[str, list[tuple[str, str]]],
) -> bool:
    """
    Check if a legacy memory string conflicts with current active structured memories,
    reusing the Stage 3.4/3.5 reconciliation rules and modernizing via conflict_resolver.
    """
    from brain.conflict_resolver import is_legacy_reconciled, get_session_retractions

    structured_list = [
        {"category": c, "key": k, "value": v, "source": "structured"}
        for c, items in active_by_cat.items()
        for k, v in items
    ]
    return is_legacy_reconciled(content, structured_list, get_session_retractions())


def retrieve_relevant_context(
    query: str,
    limit: int = 10,
    categories: Optional[list[str]] = None,
    include_legacy: bool = True,
    include_structured: bool = True,
) -> list[dict]:
    """
    Retrieve and rank useful memories from both structured_memories and legacy memories
    based on query relevance, distinct term coverage, and modest recency weighting.

    Ranking Architecture:
    - Base Relevance: Sum of matching term points (exact value token: +4.0 structured, +3.5 legacy;
      fact token: +3.0; substring: +1.5 to +2.0; key/category: +1.5; verbatim phrase: +5.0).
    - Coverage Multiplier: M = 1.0 + 1.5 * (distinct_matched_terms / total_query_terms).
      For full query coverage (coverage = 1.0), M = 2.5.
      For one-third coverage (coverage = 1/3), M = 1.5.
      The relative coverage multiplier boost between full and 1/3 coverage is 2.5 / 1.5 = 1.667x.
    - Bounded Recency: Gently breaks ties between facts with equal relevance (+0.0 to +0.3 max).
      Relevance strictly dominates recency (minimum term match is >= 3.0 vs max recency of 0.3).
    - Deterministic Ordering: Sorted by (-score, source_priority, -id, key_or_content).

    Guarantees:
    - Purely read-only; performs zero database mutations.
    - Deterministic ordering with stable tie-breaking.
    - Relevance strictly dominates recency.
    - Honors caller-specified limit, returning empty list for limit <= 0.
    - Returns empty list for empty, whitespace-only, punctuation-only, or stop-word-only queries.
    - Reconciles legacy records against active structured memories, preventing superseded facts.
    - Preserves source provenance ('source': 'structured' | 'legacy').
    """
    if limit <= 0:
        return []

    tokens = tokenize_query(query)
    if not tokens:
        return []

    conn = get_connection()
    unique_query_tokens = list(dict.fromkeys(tokens))
    total_query_terms = len(unique_query_tokens)
    query_lower = query.lower().strip()

    # Pre-fetch active structured memories for reconciliation if legacy is included
    active_by_cat: dict[str, list[tuple[str, str]]] = {}
    if include_legacy:
        structured_all = recall_structured()
        for s in structured_all:
            c = s["category"].lower()
            k = s["key"].lower()
            v = s["value"].lower()
            active_by_cat.setdefault(c, []).append((k, v))

    candidates: list[dict] = []

    # 1. Fetch & score structured memories
    if include_structured:
        if categories:
            cat_list = [c.lower().strip() for c in categories if c.strip()]
            placeholders = ",".join("?" for _ in cat_list)
            sql = f"""
                SELECT id, category, memory_key, value, confidence, created_at, updated_at
                FROM structured_memories
                WHERE category IN ({placeholders})
            """
            cursor = conn.execute(sql, cat_list)
        else:
            cursor = conn.execute(
                """
                SELECT id, category, memory_key, value, confidence, created_at, updated_at
                FROM structured_memories
                """
            )

        for row in cursor.fetchall():
            rec_id, cat, key, val, conf, created_at, updated_at = row
            val_clean = val.strip()
            val_lower = val_clean.lower()
            key_lower = key.lower()
            cat_lower = cat.lower()

            fact_text = f"{cat_lower} {key_lower.replace('_', ' ')} {val_lower}"
            fact_tokens = set(re.findall(r"\b[\w\.\-\+]{1,50}\b", fact_text))

            matched_terms = set()
            term_score = 0.0

            for q_term in unique_query_tokens:
                term_matched = False
                if q_term in val_lower.split():
                    term_score += 4.0
                    term_matched = True
                elif q_term in fact_tokens:
                    term_score += 3.0
                    term_matched = True
                elif q_term in val_lower:
                    term_score += 2.0
                    term_matched = True
                elif q_term in key_lower or q_term in cat_lower:
                    term_score += 1.5
                    term_matched = True

                if term_matched:
                    matched_terms.add(q_term)

            if not matched_terms:
                continue

            # Phrase match bonus
            if len(unique_query_tokens) > 1 and query_lower in val_lower:
                term_score += 5.0

            # Multiplier M = 1.0 + 1.5 * coverage (M=2.5 for full coverage, M=1.5 for 1/3 coverage; ratio 1.667x)
            coverage = len(matched_terms) / total_query_terms
            relevance = term_score * (1.0 + 1.5 * coverage)

            confidence_factor = max(0.2, min(1.0, float(conf or 1.0)))
            relevance *= confidence_factor

            recency = _calculate_recency_score(updated_at or created_at)
            final_score = relevance + recency

            candidates.append({
                "id": rec_id,
                "category": cat,
                "key": key,
                "value": val_clean,
                "confidence": conf,
                "created_at": created_at,
                "updated_at": updated_at,
                "source": "structured",
                "score": round(final_score, 4),
                "matched_terms": sorted(list(matched_terms)),
                "text": f"[{cat}] {val_clean}",
            })

    # 2. Fetch & score legacy memories
    if include_legacy:
        cursor = conn.execute(
            """
            SELECT id, content, created_at
            FROM memories
            ORDER BY id DESC
            """
        )

        for row in cursor.fetchall():
            rec_id, content, created_at = row
            content_clean = content.strip()
            content_lower = content_clean.lower()

            # Apply Stage 3.4/3.5 reconciliation suppression
            if _is_legacy_conflict(content_clean, active_by_cat):
                continue

            content_tokens = set(re.findall(r"\b[\w\.\-\+]{1,50}\b", content_lower))

            matched_terms = set()
            term_score = 0.0

            for q_term in unique_query_tokens:
                term_matched = False
                if q_term in content_lower.split():
                    term_score += 3.5
                    term_matched = True
                elif q_term in content_tokens:
                    term_score += 3.0
                    term_matched = True
                elif q_term in content_lower:
                    term_score += 1.5
                    term_matched = True

                if term_matched:
                    matched_terms.add(q_term)

            if not matched_terms:
                continue

            # Phrase match bonus
            if len(unique_query_tokens) > 1 and query_lower in content_lower:
                term_score += 5.0

            # Multiplier M = 1.0 + 1.5 * coverage (M=2.5 for full coverage, M=1.5 for 1/3 coverage; ratio 1.667x)
            coverage = len(matched_terms) / total_query_terms
            relevance = term_score * (1.0 + 1.5 * coverage)

            recency = _calculate_recency_score(created_at)
            final_score = relevance + recency

            candidates.append({
                "id": rec_id,
                "content": content_clean,
                "created_at": created_at,
                "source": "legacy",
                "score": round(final_score, 4),
                "matched_terms": sorted(list(matched_terms)),
                "text": content_clean,
            })

    # 3. Deduplication, cross-category conflict resolution, and deterministic tie-breaking
    def sort_key(item: dict):
        src_priority = 0 if item["source"] == "structured" else 1
        return (
            -item["score"],
            src_priority,
            -item["id"],
            item.get("key", "") or item.get("content", ""),
        )

    candidates.sort(key=sort_key)

    from brain.conflict_resolver import resolve_memory_conflicts
    resolved, report = resolve_memory_conflicts(candidates, query=query)

    return resolved[:limit]


# ==========================================================
# STAGE 3.6B: CONTEXT BUDGETING & COMPACTION RE-EXPORTS
# ==========================================================
from brain.context_budget import (
    ContextBudgetManager,
    MemoryBudgetItem,
    BudgetResult,
    budget_chat_memory,
    budget_agent_memory,
    DEFAULT_CHAT_BUDGET_CHARS,
    DEFAULT_AGENT_BUDGET_CHARS,
    render_structured_profile,
    render_legacy_memories,
    render_agent_relevant_lines,
    safe_bounded_fallback_profile,
)

# ==========================================================
# STAGE 3.6C: CONFLICT RESOLUTION RE-EXPORTS
# ==========================================================
from brain.conflict_resolver import (
    resolve_memory_conflicts,
    is_legacy_reconciled,
    register_session_retraction,
    get_session_retractions,
    clear_session_retractions,
    FactSlot,
    SlotCardinality,
    ConflictRelation,
    ConflictResolutionReport,
)

sys.modules[__name__].__class__ = DatabaseAwareModule

