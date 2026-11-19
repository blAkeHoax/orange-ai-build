"""
brain/paths.py - Centralized path resolution and per-user data directory management.

Ensures:
1. Predictable per-user writable application data directory (%LOCALAPPDATA%\\Orange AI) in frozen mode.
2. Development-mode compatibility with the workspace root.
3. Safe separation between read-only bundled assets (_MEIPASS) and writable user data.
4. Idempotent legacy data adoption and migration with SQLite integrity verification.
"""

import os
import sys
import shutil
import sqlite3
import json
import hashlib
import uuid
from typing import Optional, Dict, Any, List

_MIGRATION_PERFORMED = False


def get_user_data_dir() -> str:
    """
    Return the writable per-user application data directory.
    - If overridden via ORANGE_AI_USER_DATA_DIR environment variable, uses that.
    - In frozen mode (PyInstaller compiled EXE): uses %LOCALAPPDATA%\\Orange AI (fallback ~/.orange_ai).
    - In development/source mode: returns the workspace root (parent of brain/).
    Ensures the target directory exists.
    """
    override = os.environ.get("ORANGE_AI_USER_DATA_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            path = os.path.join(local_app_data, "Orange AI")
        else:
            path = os.path.join(os.path.expanduser("~"), ".orange_ai")
        os.makedirs(path, exist_ok=True)
        return path

    # Source / dev mode: workspace root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_project_root() -> str:
    """
    Returns the root directory for writable application databases and logs.
    Preserves backward compatibility with existing callers across the codebase.
    """
    return get_user_data_dir()


def get_assets_dir() -> str:
    """
    Return the directory containing application visual assets (icons, orb logos).
    Handles PyInstaller _MEIPASS extraction, onedir folder, and source workspace.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.exists(os.path.join(meipass, "assets")):
            return os.path.join(meipass, "assets")
        exe_dir = os.path.dirname(sys.executable)
        if os.path.exists(os.path.join(exe_dir, "assets")):
            return os.path.join(exe_dir, "assets")

    # Development / source mode
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "assets")


def get_asset_path(filename: str) -> str:
    """Return the absolute path to a specific asset file."""
    return os.path.join(get_assets_dir(), filename)


def verify_sqlite_integrity(db_path: str) -> bool:
    """Verify SQLite database integrity using PRAGMA integrity_check."""
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        rows = cursor.fetchall()
        conn.close()
        return len(rows) == 1 and rows[0][0].lower() == "ok"
    except Exception:
        return False


def count_authentic_memories(db_path: str) -> int:
    """
    Score the authenticity of a memory database based on verified records.
    Returns -1 if corrupt or unreadable; higher score indicates more authentic user context.
    """
    if not os.path.exists(db_path) or not verify_sqlite_integrity(db_path):
        return -1
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM structured_memories WHERE category='identity' AND memory_key='name' AND value='Sayan Dey'"
        )
        has_sayan = cur.fetchone()[0] > 0
        cur.execute("SELECT count(*) FROM structured_memories")
        s_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM memories")
        l_count = cur.fetchone()[0]
        conn.close()
        return (100 if has_sayan else 0) + s_count + l_count
    except Exception:
        return -1


def is_db_needing_restoration(db_path: str) -> bool:
    """
    Determine if a runtime memory database is uninitialized, empty, or contains only
    accidental / corrupted placeholder data (e.g. 0 legacy memories and <= 1 structured memory).
    """
    if not os.path.exists(db_path):
        return True
    if not verify_sqlite_integrity(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories")
        l_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM structured_memories")
        s_count = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM structured_memories WHERE category='identity' AND memory_key='name' AND value='Sayan Dey'"
        )
        has_authentic_name = cur.fetchone()[0] > 0
        conn.close()

        if not has_authentic_name and s_count <= 1 and l_count == 0:
            return True
        return False
    except Exception:
        return False


def find_legacy_source_dir(explicit_dir: Optional[str] = None) -> Optional[str]:
    """
    Deterministically discover an authentic legacy data source directory containing memory.db.
    Checks candidate locations in deterministic order:
    1. Explicitly provided directory (if valid and contains memory.db).
    2. ORANGE_AI_LEGACY_SOURCE_DIR environment variable.
    3. Directory containing sys.executable.
    4. Parent directory of directory containing sys.executable.
    5. Parent of brain/ (source / workspace root).
    6. Well-known adjacent locations (e.g. workspace root if running from installer/ or dist/).
    Returns the candidate directory with verified SQLite integrity and the highest authentic memory score.
    """
    if explicit_dir and os.path.exists(explicit_dir):
        return os.path.abspath(explicit_dir)

    candidates: List[str] = []

    env_dir = os.environ.get("ORANGE_AI_LEGACY_SOURCE_DIR")
    if env_dir and os.path.exists(env_dir):
        candidates.append(os.path.abspath(env_dir))

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(exe_dir)
        parent_exe_dir = os.path.dirname(exe_dir)
        candidates.append(parent_exe_dir)
        candidates.append(os.path.join(parent_exe_dir, "Orange AI Desktop"))

    dev_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(dev_root)
    candidates.append(os.path.join(os.path.dirname(dev_root), "Orange AI Desktop"))

    best_dir = None
    best_score = -1

    seen = set()
    for c_dir in candidates:
        norm = os.path.normpath(c_dir)
        if norm in seen or not os.path.exists(norm):
            continue
        seen.add(norm)

        db_path = os.path.join(norm, "memory.db")
        if os.path.exists(db_path):
            score = count_authentic_memories(db_path)
            if score > best_score:
                best_score = score
                best_dir = norm

    return best_dir


def merge_authentic_memories(src_db: str, dest_db: str) -> Dict[str, Any]:
    """
    Safely and transactionally restore authentic personal memories into an active runtime database.
    Guarantees:
    - Preserves existing conversation_history in dest_db completely intact.
    - Removes known false-positive placeholder identity record (e.g. 'justr catching up').
    - Inserts authentic structured memories without duplicating existing keys.
    - Inserts authentic legacy memories without duplicating existing content.
    - Executes in a single SQLite transaction with rollback on error.
    - Verifies SQLite integrity before and after merge.
    """
    if not os.path.exists(src_db) or not os.path.exists(dest_db):
        return {"status": "error", "message": "Source or destination database does not exist"}

    if not verify_sqlite_integrity(src_db) or not verify_sqlite_integrity(dest_db):
        return {"status": "error", "message": "Integrity check failed before merge"}

    src_conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src_cur = src_conn.cursor()

    src_cur.execute(
        "SELECT category, memory_key, value, confidence, created_at, updated_at FROM structured_memories"
    )
    src_structured = src_cur.fetchall()

    src_cur.execute("SELECT content, created_at FROM memories")
    src_legacy = src_cur.fetchall()
    src_conn.close()

    dest_conn = sqlite3.connect(dest_db, timeout=10.0)
    dest_conn.execute("PRAGMA journal_mode=WAL;")
    dest_conn.execute("PRAGMA busy_timeout = 5000;")

    imported_structured = 0
    updated_structured = 0
    imported_legacy = 0
    skipped_structured = 0
    skipped_legacy = 0

    try:
        with dest_conn:
            dest_cur = dest_conn.cursor()
            dest_cur.execute("SELECT id, category, memory_key, value FROM structured_memories")
            existing_struct = {}
            for row in dest_cur.fetchall():
                r_id, cat, key, val = row
                existing_struct[(cat.lower(), key.lower())] = (r_id, val)

            dest_cur.execute("SELECT content FROM memories")
            existing_legacy = {row[0].strip().lower() for row in dest_cur.fetchall() if row[0]}

            # Remove corrupted placeholder identity record if present
            for (cat_lower, key_lower), (r_id, val) in list(existing_struct.items()):
                if cat_lower == "identity" and key_lower == "name" and "justr" in val.lower():
                    dest_cur.execute("DELETE FROM structured_memories WHERE id = ?", (r_id,))
                    del existing_struct[(cat_lower, key_lower)]

            # Merge structured memories
            for cat, key, val, conf, c_at, u_at in src_structured:
                pair = (cat.lower(), key.lower())
                if pair in existing_struct:
                    r_id, ex_val = existing_struct[pair]
                    if ex_val != val:
                        dest_cur.execute(
                            "UPDATE structured_memories SET value = ?, confidence = ?, updated_at = ? WHERE id = ?",
                            (val, conf, u_at, r_id),
                        )
                        updated_structured += 1
                    else:
                        skipped_structured += 1
                else:
                    dest_cur.execute(
                        """
                        INSERT INTO structured_memories (category, memory_key, value, confidence, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (cat, key, val, conf, c_at, u_at),
                    )
                    imported_structured += 1
                    existing_struct[pair] = (-1, val)

            # Merge legacy memories
            for content, c_at in src_legacy:
                c_clean = content.strip().lower()
                if c_clean in existing_legacy:
                    skipped_legacy += 1
                else:
                    dest_cur.execute(
                        "INSERT INTO memories (content, created_at) VALUES (?, ?)",
                        (content, c_at),
                    )
                    imported_legacy += 1
                    existing_legacy.add(c_clean)

        dest_conn.close()

        if not verify_sqlite_integrity(dest_db):
            return {"status": "error", "message": "Integrity check failed after merge"}

        return {
            "status": "success",
            "imported_structured": imported_structured,
            "updated_structured": updated_structured,
            "skipped_structured": skipped_structured,
            "imported_legacy": imported_legacy,
            "skipped_legacy": skipped_legacy,
        }
    except Exception as e:
        try:
            dest_conn.close()
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


def calculate_file_sha256(file_path: str) -> str:
    """Calculate SHA-256 hex digest of a file in 64KB chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def validate_json_file(file_path: str) -> bool:
    """Validate that a file exists and contains a valid JSON object."""
    if not os.path.isfile(file_path):
        return False
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return isinstance(data, dict)
    except Exception:
        return False


def validate_recommendations_json(file_path: str) -> bool:
    """
    Validate recommendations.json against the current application schema:
    - Root must be a dictionary.
    - 'version' field must be an integer (>= 1).
    - 'recommendations' field must be a dictionary.
    - Each record in 'recommendations' must be a dictionary with rec_id, category, and title.
    """
    if not os.path.isfile(file_path):
        return False
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        version = data.get("version", 1)
        if not isinstance(version, int) or version < 1:
            return False
        recs = data.get("recommendations")
        if not isinstance(recs, dict):
            return False
        for rec_id, rec_data in recs.items():
            if not isinstance(rec_id, str) or not isinstance(rec_data, dict):
                return False
            if "rec_id" not in rec_data or "category" not in rec_data or "title" not in rec_data:
                return False
        return True
    except Exception:
        return False


def validate_event_watermarks_json(file_path: str) -> bool:
    """
    Validate event_watermarks.json against the current application schema:
    - Root must be a dictionary.
    - 'version' must equal 1 (EVENT_STORE_SCHEMA_VERSION).
    - 'is_first_run_baseline' must be boolean.
    - 'watermarks' must be a dictionary.
    - 'delivered_events' (or legacy 'delivered_event_ids') must be a dictionary.
    - 'created_at' and 'updated_at' must be non-empty strings.
    """
    if not os.path.isfile(file_path):
        return False
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        if data.get("version") != 1:
            return False
        if not isinstance(data.get("is_first_run_baseline"), bool):
            return False
        if not isinstance(data.get("watermarks"), dict):
            return False
        raw_events = data.get("delivered_events") if "delivered_events" in data else data.get("delivered_event_ids")
        if not isinstance(raw_events, dict):
            return False
        c_at = data.get("created_at")
        u_at = data.get("updated_at")
        if not isinstance(c_at, str) or not c_at.strip():
            return False
        if not isinstance(u_at, str) or not u_at.strip():
            return False
        return True
    except Exception:
        return False


def migrate_legacy_data_if_needed(
    legacy_source_dir: Optional[str] = None,
    target_dir: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Idempotent migration mechanism for existing user data.
    Migrates memory.db, jobs.db (if present), recommendations.json,
    event_watermarks.json, gui_settings.json, and orange_ai.log from a legacy
    location to the per-user data directory (%LOCALAPPDATA%\\Orange AI).

    Rules:
    - Never overwrites existing destination data without safe merging.
    - If destination memory.db is empty or contains only corrupted placeholder data,
      merges authentic memories while strictly preserving existing conversation history.
    - Pre-validates JSON schemas (recommendations, watermarks, settings) before migration.
    - Uses temporary staging files, fsync, and SHA-256 verification before atomic promotion.
    - Employs race-protected rename so destination files created during migration are never replaced.
    - Verifies SQLite database integrity before finalizing adoption.
    - Keeps source files completely intact.
    - Reports conflicts and errors safely without corrupting data.
    - If any file fails, migration is not marked completed, enabling safe retries.
    """
    global _MIGRATION_PERFORMED
    dest_dir = target_dir or get_user_data_dir()
    os.makedirs(dest_dir, exist_ok=True)

    report = {
        "destination_dir": dest_dir,
        "migrated": [],
        "merged": [],
        "skipped": [],
        "conflicts": [],
        "errors": [],
    }

    if _MIGRATION_PERFORMED and not legacy_source_dir and not target_dir and not force:
        report["status"] = "already_performed"
        return report

    src_dir = find_legacy_source_dir(legacy_source_dir)
    if not src_dir or not os.path.exists(src_dir):
        report["status"] = "no_source_dir"
        return report

    if os.path.abspath(src_dir) == os.path.abspath(dest_dir):
        report["status"] = "source_matches_destination"
        return report

    files_to_check = [
        "memory.db",
        "jobs.db",
        "recommendations.json",
        "event_watermarks.json",
        "gui_settings.json",
        "orange_ai.log",
    ]

    for fname in files_to_check:
        src_file = os.path.join(src_dir, fname)
        dest_file = os.path.join(dest_dir, fname)

        if not os.path.exists(src_file):
            continue

        if os.path.exists(dest_file):
            # Special check for memory.db: if destination is uninitialized or corrupted placeholder, merge authentic memories
            if fname == "memory.db" and is_db_needing_restoration(dest_file):
                merge_res = merge_authentic_memories(src_file, dest_file)
                if merge_res.get("status") == "success":
                    report["merged"].append({
                        "file": fname,
                        "source": src_file,
                        "details": merge_res,
                    })
                else:
                    report["errors"].append({
                        "file": fname,
                        "error": merge_res.get("message", "Merge failed"),
                    })
                continue

            # Destination already populated and intact - NEVER overwrite
            report["skipped"].append(fname)
            src_stat = os.stat(src_file)
            dest_stat = os.stat(dest_file)
            is_different = (src_stat.st_size != dest_stat.st_size)
            if not is_different:
                try:
                    is_different = (calculate_file_sha256(src_file) != calculate_file_sha256(dest_file))
                except Exception:
                    is_different = True

            if is_different:
                report["conflicts"].append({
                    "file": fname,
                    "source_size": src_stat.st_size,
                    "dest_size": dest_stat.st_size,
                    "action": "preserved_destination",
                })
            continue

        # Destination does not exist: pre-validate source before staging
        if fname.endswith(".db"):
            if not verify_sqlite_integrity(src_file):
                report["errors"].append({
                    "file": fname,
                    "error": f"Source SQLite database '{fname}' failed integrity check.",
                })
                continue
        elif fname == "recommendations.json":
            if not validate_recommendations_json(src_file):
                report["errors"].append({
                    "file": fname,
                    "error": f"Source recommendations file '{fname}' is malformed or invalid schema.",
                })
                continue
        elif fname == "event_watermarks.json":
            if not validate_event_watermarks_json(src_file):
                report["errors"].append({
                    "file": fname,
                    "error": f"Source event watermarks file '{fname}' is malformed or unsupported version.",
                })
                continue
        elif fname == "gui_settings.json":
            if not validate_json_file(src_file):
                report["errors"].append({
                    "file": fname,
                    "error": f"Source settings file '{fname}' is not valid JSON.",
                })
                continue

        # Destination does not exist and source is valid: stage copy via temporary file
        tmp_dest = f"{dest_file}.migration_tmp_{uuid.uuid4().hex[:8]}"
        try:
            src_sha = calculate_file_sha256(src_file)

            with open(src_file, "rb") as f_src, open(tmp_dest, "wb") as f_dst:
                while chunk := f_src.read(65536):
                    f_dst.write(chunk)
                f_dst.flush()
                try:
                    os.fsync(f_dst.fileno())
                except Exception:
                    pass

            if fname.endswith(".db"):
                if not verify_sqlite_integrity(tmp_dest):
                    if os.path.exists(tmp_dest):
                        os.remove(tmp_dest)
                    report["errors"].append({
                        "file": fname,
                        "error": "SQLite integrity check failed on staged copy.",
                    })
                    continue

            staged_sha = calculate_file_sha256(tmp_dest)
            if staged_sha != src_sha:
                if os.path.exists(tmp_dest):
                    os.remove(tmp_dest)
                report["errors"].append({
                    "file": fname,
                    "error": "SHA-256 mismatch between source and staged copy.",
                })
                continue

            # Race condition check: re-verify destination is still absent before promotion
            if os.path.exists(dest_file):
                if os.path.exists(tmp_dest):
                    os.remove(tmp_dest)
                report["skipped"].append(fname)
                report["conflicts"].append({
                    "file": fname,
                    "action": "preserved_destination_race",
                })
                continue

            # Atomic promotion: os.rename fails if destination was created concurrently
            try:
                os.rename(tmp_dest, dest_file)
                report["migrated"].append(fname)
            except FileExistsError:
                if os.path.exists(tmp_dest):
                    os.remove(tmp_dest)
                report["skipped"].append(fname)
                report["conflicts"].append({
                    "file": fname,
                    "action": "preserved_destination_race",
                })
            except Exception:
                # Fallback if os.rename had another OS-level error
                if not os.path.exists(dest_file):
                    os.replace(tmp_dest, dest_file)
                    report["migrated"].append(fname)
                else:
                    if os.path.exists(tmp_dest):
                        os.remove(tmp_dest)
                    report["skipped"].append(fname)
                    report["conflicts"].append({
                        "file": fname,
                        "action": "preserved_destination_race",
                    })

        except Exception as e:
            if os.path.exists(tmp_dest):
                try:
                    os.remove(tmp_dest)
                except Exception:
                    pass
            report["errors"].append({"file": fname, "error": str(e)})

    # Idempotent status: only mark completed if no errors occurred
    if not report["errors"]:
        _MIGRATION_PERFORMED = True
        report["status"] = "success"
    else:
        report["status"] = "partial_error"

    return report
