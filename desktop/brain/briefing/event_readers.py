"""
brain/briefing/event_readers.py - Read-Only Event Detection & Candidate Generation.

Stage 3.7E.3B: Event Readers & Candidate Generation
Implements:
1. Strictly read-only event detection from authoritative SQLite tables:
   - `jobs` (completed, failed, waiting_confirmation, paused, retrying)
   - `job_steps` (completed, failed step execution)
   - `reminders` (unfired, due/overdue commitments)
   - `structured_memories` (project and memory creation/updates)
2. Deterministic candidate generation with stable cryptographic identity (SHA-256) via EventStore.
3. Read-only watermark integration: filters candidates using watermark high-water positions
   without mutating the watermark store or advancing positions.
4. Fail-closed handling for missing, malformed, or corrupt watermark stores (never auto-initializes).
5. Comprehensive privacy, retirement, and sanitization safeguards:
   - Excludes session-retired entities (is_session_retired)
   - Filters sensitive personal/romantic goals (_is_sensitive_personal_goal)
   - Redacts credentials, tokens, and passwords (sanitize_briefing_text)
   - Never scans raw conversation history or stages executable actions
6. Deterministic multi-tier candidate ordering (Priority, Occurred, Type, Entity, Event ID).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set, Union

from brain.briefing.models import EventCandidate, EventPriority
from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreError,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EventStoreSchemaError,
    generate_event_id,
    normalize_utc_timestamp,
    parse_utc_timestamp,
    EVENT_WATERMARKS_FILENAME,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.collector import (
    sanitize_briefing_text,
    _is_sensitive_personal_goal,
)
from brain.conflict_resolver import is_session_retired
from brain.jobs.models import JobStatus
from brain.memory import get_db_path
from brain.paths import get_project_root

logger = logging.getLogger(__name__)

# Clock skew tolerance for timestamp validations
CLOCK_SKEW_TOLERANCE_MINUTES = 5


def parse_timestamp_naive(ts_str: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO timestamp into a naive local datetime for consistent comparisons.
    Handles timezone-aware UTC timestamps (from `jobs`/`job_steps`) and naive local timestamps.
    Returns None safely on invalid input.
    """
    if not ts_str:
        return None
    try:
        norm = ts_str.strip().replace(" ", "T")
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def safe_parse_utc(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp to UTC datetime safely, returning None on malformed strings."""
    if not ts_str:
        return None
    try:
        return parse_utc_timestamp(ts_str)
    except Exception:
        return None


def safe_normalize_utc(dt_or_str: Optional[Union[datetime, str]]) -> Optional[str]:
    """Normalize datetime or ISO string to UTC ISO safely, returning None or raw string on malformed inputs."""
    if not dt_or_str:
        return None
    try:
        return normalize_utc_timestamp(dt_or_str)
    except Exception:
        return str(dt_or_str).strip()



def sort_candidates(candidates: List[EventCandidate]) -> List[EventCandidate]:
    """
    Deterministically sort event candidates:
    1. Priority rank ascending (CRITICAL=1, HIGH=2, NORMAL=3, LOW=4)
    2. Occurrence timestamp descending (newest occurrences first)
    3. Event type ascending
    4. Entity ID ascending
    5. Event ID ascending
    """
    def _sort_key(c: EventCandidate) -> Tuple[int, str, str, str, str]:
        prio_rank = c.priority.rank if hasattr(c.priority, "rank") else 99
        # Normalize occurred_at to string for inverted chronological comparison
        occurred = c.occurred_at or ""
        return (prio_rank, occurred, c.event_type, c.entity_id, c.event_id)

    # Secondary sort: primary rank ascending, occurred_at descending
    return sorted(
        candidates,
        key=lambda c: (
            c.priority.rank if hasattr(c.priority, "rank") else 99,
            -(parse_timestamp_naive(c.occurred_at).timestamp() if parse_timestamp_naive(c.occurred_at) else 0),
            c.event_type,
            c.entity_id,
            c.event_id,
        )
    )


class EventCandidateReader:
    """
    Read-only event detector and candidate generator for Orange AI Desktop.
    
    Adheres strictly to Stage 3.7E.3B specifications:
    - Queries SQLite databases strictly in read-only mode (`?mode=ro`).
    - Uses EventWatermarkStore in read-only mode to limit candidate generation.
    - Fails closed if the watermark store is missing, corrupt, or invalid.
    - Never mutates the database, never advances watermarks, never records delivery.
    - Zero actions, zero notifications, zero background execution.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        watermark_store_path: Optional[str] = None,
    ):
        self.db_path = db_path or get_db_path()
        self.watermark_store_path = watermark_store_path or os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)

    def _get_ro_connection(self) -> sqlite3.Connection:
        """
        Open a strictly read-only connection to the database.
        Ensures file descriptors are safely bounded and WAL contention is minimized.
        """
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database file does not exist: '{self.db_path}'")

        uri_path = self.db_path.replace("\\", "/")
        if not uri_path.startswith("/"):
            uri_path = "/" + uri_path
        uri = f"file://{uri_path}?mode=ro"

        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -----------------------------------------------------------------------
    # Source A: Jobs
    # -----------------------------------------------------------------------

    def collect_job_candidates(
        self,
        snapshot: Optional[EventStoreSnapshot] = None,
        now: Optional[datetime] = None,
    ) -> List[EventCandidate]:
        """
        Detect supported job state events from the persistent `jobs` table:
        - `job_completed`: JobStatus.COMPLETED
        - `job_failed`: JobStatus.FAILED
        - `job_waiting_confirmation`: JobStatus.WAITING_CONFIRMATION
        - `job_paused`: JobStatus.PAUSED
        - `job_retrying`: JobStatus.RETRYING
        
        Applies read-only watermark filtering against `jobs_completed_at` for terminal jobs.
        RUNNING status alone is intentionally excluded from emitting progress events.

        Architectural Evaluation (Stage 3.7E.4 - SQL vs. Python Chronological Filtering):
        Pushing `completed_at > :watermark` directly into SQLite SQL WHERE clauses was formally evaluated.
        SQLite evaluates string columns using ASCII lexicographical comparison. Because legacy jobs,
        external tools, and user environments may record timestamps using varying ISO 8601 notations
        (e.g., '2026-10-31T12:00:00Z' vs '2026-10-31T12:00:00+00:00', or varying fractional second precisions),
        lexicographical SQL comparison can miscompare equivalent or past timestamps ('Z' > '+').
        In contrast, Python-level parsing via `safe_parse_utc()` guarantees exact microsecond-level,
        timezone-normalized chronological comparisons regardless of textual string formatting.
        Because table size in desktop deployment is bounded and query execution is fast, SQL-level
        filtering is deferred to maintain 100% semantic correctness.
        """
        candidates: List[EventCandidate] = []
        local_now = now or datetime.now()
        detected_iso = normalize_utc_timestamp(local_now) or ""

        # Read watermark
        watermark_comp_str = snapshot.get_watermark(WATERMARK_JOBS_COMPLETED_AT) if snapshot else None
        watermark_comp_dt = parse_utc_timestamp(watermark_comp_str) if watermark_comp_str else None

        conn = self._get_ro_connection()
        try:
            cur = conn.execute(
                "SELECT id, goal, status, priority, attempt_count, max_attempts, step_count, "
                "last_error, pending_confirmation_json, created_at, updated_at, started_at, "
                "completed_at, source, tags FROM jobs ORDER BY updated_at ASC"
            )
            rows = cur.fetchall()
            for r in rows:
                job_id = r["id"]
                raw_status = (r["status"] or "").strip().upper()
                goal_clean = sanitize_briefing_text(r["goal"] or "Untitled Task")
                short_id = job_id[:8] if len(job_id) >= 8 else job_id
                source_val = (r["source"] or "user").strip()

                # Privacy check: skip retired project/job entities
                if is_session_retired(goal_clean, "project") or is_session_retired(short_id, "job"):
                    continue

                # 1. Job Completed
                if raw_status == JobStatus.COMPLETED.value:
                    occurred_str = r["completed_at"] or r["updated_at"]
                    occurred_dt = safe_parse_utc(occurred_str)

                    # Watermark check
                    if watermark_comp_dt and occurred_dt and occurred_dt <= watermark_comp_dt:
                        continue

                    material = {
                        "job_id": job_id,
                        "status": "completed",
                        "completed_at": safe_normalize_utc(occurred_str),
                        "attempt_count": r["attempt_count"],
                        "step_count": r["step_count"],
                        "source": source_val,
                    }
                    evt_id = generate_event_id("job_completed", job_id, material)
                    is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                    candidates.append(
                        EventCandidate(
                            event_id=evt_id,
                            event_type="job_completed",
                            entity_id=job_id,
                            occurred_at=occurred_str or detected_iso,
                            detected_at=detected_iso,
                            priority=EventPriority.HIGH if r["priority"] >= 2 else EventPriority.NORMAL,
                            title=f"Job completed: {goal_clean}",
                            evidence=[f"Job '{short_id}' completed successfully at {occurred_str} (source: {source_val})."],
                            material_data=material,
                            source="jobs",
                            confidence="HIGH",
                            is_delivered=is_deliv,
                        )
                    )

                # 2. Job Failed
                elif raw_status == JobStatus.FAILED.value:
                    occurred_str = r["completed_at"] or r["updated_at"]
                    occurred_dt = safe_parse_utc(occurred_str)

                    # Watermark check
                    if watermark_comp_dt and occurred_dt and occurred_dt <= watermark_comp_dt:
                        continue

                    last_err = sanitize_briefing_text(r["last_error"] or "Unknown execution error")
                    material = {
                        "job_id": job_id,
                        "status": "failed",
                        "completed_at": safe_normalize_utc(occurred_str),
                        "attempt_count": r["attempt_count"],
                        "error": last_err,
                        "source": source_val,
                    }
                    evt_id = generate_event_id("job_failed", job_id, material)
                    is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                    candidates.append(
                        EventCandidate(
                            event_id=evt_id,
                            event_type="job_failed",
                            entity_id=job_id,
                            occurred_at=occurred_str or detected_iso,
                            detected_at=detected_iso,
                            priority=EventPriority.HIGH,
                            title=f"Job failed: {goal_clean}",
                            evidence=[f"Job '{short_id}' failed: {last_err}"],
                            material_data=material,
                            source="jobs",
                            confidence="HIGH",
                            is_delivered=is_deliv,
                        )
                    )

                # 3. Job Waiting Confirmation
                elif raw_status == JobStatus.WAITING_CONFIRMATION.value:
                    occurred_str = r["updated_at"]
                    pending_raw = sanitize_briefing_text(r["pending_confirmation_json"] or "")
                    material = {
                        "job_id": job_id,
                        "status": "waiting_confirmation",
                        "pending_action": pending_raw,
                        "source": source_val,
                    }
                    evt_id = generate_event_id("job_waiting_confirmation", job_id, material)
                    is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                    candidates.append(
                        EventCandidate(
                            event_id=evt_id,
                            event_type="job_waiting_confirmation",
                            entity_id=job_id,
                            occurred_at=occurred_str or detected_iso,
                            detected_at=detected_iso,
                            priority=EventPriority.CRITICAL,
                            title=f"Approval required for job: {goal_clean}",
                            evidence=[f"Job '{short_id}' is paused awaiting user confirmation."],
                            material_data=material,
                            source="jobs",
                            confidence="HIGH",
                            is_delivered=is_deliv,
                        )
                    )

                # 4. Job Paused
                elif raw_status == JobStatus.PAUSED.value:
                    occurred_str = r["updated_at"]
                    material = {
                        "job_id": job_id,
                        "status": "paused",
                        "updated_at": safe_normalize_utc(occurred_str),
                        "source": source_val,
                    }
                    evt_id = generate_event_id("job_paused", job_id, material)
                    is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                    candidates.append(
                        EventCandidate(
                            event_id=evt_id,
                            event_type="job_paused",
                            entity_id=job_id,
                            occurred_at=occurred_str or detected_iso,
                            detected_at=detected_iso,
                            priority=EventPriority.NORMAL,
                            title=f"Job paused: {goal_clean}",
                            evidence=[f"Job '{short_id}' was paused."],
                            material_data=material,
                            source="jobs",
                            confidence="HIGH",
                            is_delivered=is_deliv,
                        )
                    )

                # 5. Job Retrying
                elif raw_status == JobStatus.RETRYING.value:
                    occurred_str = r["updated_at"]
                    material = {
                        "job_id": job_id,
                        "status": "retrying",
                        "attempt_count": r["attempt_count"],
                        "max_attempts": r["max_attempts"],
                        "source": source_val,
                    }
                    evt_id = generate_event_id("job_retrying", job_id, material)
                    is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                    candidates.append(
                        EventCandidate(
                            event_id=evt_id,
                            event_type="job_retrying",
                            entity_id=job_id,
                            occurred_at=occurred_str or detected_iso,
                            detected_at=detected_iso,
                            priority=EventPriority.NORMAL,
                            title=f"Job retrying: {goal_clean}",
                            evidence=[f"Job '{short_id}' is retrying (attempt {r['attempt_count']} of {r['max_attempts']})."],
                            material_data=material,
                            source="jobs",
                            confidence="HIGH",
                            is_delivered=is_deliv,
                        )
                    )

        except Exception as e:
            logger.error("[EVENT READERS] Error collecting job candidates: %s", e)
            raise
        finally:
            conn.close()

        return candidates

    # -----------------------------------------------------------------------
    # Source A2: Job Steps
    # -----------------------------------------------------------------------

    def collect_job_step_candidates(
        self,
        snapshot: Optional[EventStoreSnapshot] = None,
        now: Optional[datetime] = None,
    ) -> List[EventCandidate]:
        """
        Detect completed or failed execution steps from `job_steps`.
        Uses `job_step_max_id` watermark to bound queries indexed by ID (`WHERE id > ?`).
        
        Crucial safeguard: Does NOT require completed_at to be non-NULL.
        Evaluates step.success (1 for success, 0 for failure). Steps with success=NULL are ignored.
        """
        candidates: List[EventCandidate] = []
        local_now = now or datetime.now()
        detected_iso = normalize_utc_timestamp(local_now) or ""

        # High-watermark for steps
        max_step_id = snapshot.get_watermark(WATERMARK_JOB_STEP_MAX_ID, 0) if snapshot else 0
        if not isinstance(max_step_id, int):
            max_step_id = 0

        conn = self._get_ro_connection()
        try:
            cur = conn.execute(
                "SELECT id, job_id, attempt, step_index, action_type, tool_name, "
                "success, error, started_at, completed_at FROM job_steps "
                "WHERE id > ? ORDER BY id ASC",
                (max_step_id,),
            )
            rows = cur.fetchall()
            for r in rows:
                step_id = r["id"]
                job_id = r["job_id"]
                success_val = r["success"]

                # If success is None, step execution is pending or interrupted
                if success_val is None:
                    continue

                action_type = (r["action_type"] or "action").strip()
                tool_name = (r["tool_name"] or "").strip()
                occurred_str = r["completed_at"] or r["started_at"]
                short_job = job_id[:8] if len(job_id) >= 8 else job_id

                if success_val == 1:
                    evt_type = "job_step_completed"
                    prio = EventPriority.LOW
                    material = {
                        "step_id": step_id,
                        "job_id": job_id,
                        "attempt": r["attempt"],
                        "step_index": r["step_index"],
                        "action_type": action_type,
                        "tool_name": tool_name,
                        "success": True,
                    }
                    title = f"Step {r['step_index']} completed ({tool_name or action_type})"
                    evidence = [f"Step {r['step_index']} (attempt {r['attempt']}) of job '{short_job}' completed successfully."]
                else:
                    evt_type = "job_step_failed"
                    prio = EventPriority.NORMAL
                    err_sanitized = sanitize_briefing_text(r["error"] or "Step execution failed")
                    material = {
                        "step_id": step_id,
                        "job_id": job_id,
                        "attempt": r["attempt"],
                        "step_index": r["step_index"],
                        "action_type": action_type,
                        "tool_name": tool_name,
                        "success": False,
                        "error": err_sanitized,
                    }
                    title = f"Step {r['step_index']} failed: {err_sanitized}"
                    evidence = [f"Step {r['step_index']} (attempt {r['attempt']}) of job '{short_job}' failed: {err_sanitized}"]

                evt_id = generate_event_id(evt_type, str(step_id), material)
                is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                candidates.append(
                    EventCandidate(
                        event_id=evt_id,
                        event_type=evt_type,
                        entity_id=str(step_id),
                        occurred_at=occurred_str or detected_iso,
                        detected_at=detected_iso,
                        priority=prio,
                        title=title,
                        evidence=evidence,
                        material_data=material,
                        source="job_steps",
                        confidence="HIGH",
                        is_delivered=is_deliv,
                    )
                )

        except Exception as e:
            logger.error("[EVENT READERS] Error collecting job step candidates: %s", e)
            raise
        finally:
            conn.close()

        return candidates

    # -----------------------------------------------------------------------
    # Source B: Reminders
    # -----------------------------------------------------------------------

    def collect_reminder_candidates(
        self,
        snapshot: Optional[EventStoreSnapshot] = None,
        now: Optional[datetime] = None,
    ) -> List[EventCandidate]:
        """
        Detect due and unfired reminders from `reminders` table.
        Reminders are local naive timestamps. If due_at <= now and fired == 0:
        generates candidate with reminder ID and due timestamp in material data.
        
        Zero mutations: Never marks fired, never deletes or alters reminders.
        """
        candidates: List[EventCandidate] = []
        local_now = now or datetime.now()
        check_now = local_now.astimezone().replace(tzinfo=None) if local_now.tzinfo is not None else local_now
        detected_iso = normalize_utc_timestamp(local_now) or ""

        conn = self._get_ro_connection()
        try:
            cur = conn.execute(
                "SELECT id, message, due_at, created_at, fired FROM reminders "
                "WHERE fired = 0 ORDER BY due_at ASC"
            )
            rows = cur.fetchall()
            for r in rows:
                rem_id = r["id"]
                msg_raw = r["message"] or ""
                due_raw = r["due_at"] or ""

                # Privacy & Sensitivity safeguards
                if is_session_retired(msg_raw, "reminder"):
                    continue
                if _is_sensitive_personal_goal(msg_raw):
                    continue

                due_dt = parse_timestamp_naive(due_raw)
                if not due_dt:
                    continue

                # Check if reminder is due
                if due_dt <= check_now:
                    msg_clean = sanitize_briefing_text(msg_raw)
                    overdue_seconds = (check_now - due_dt).total_seconds()
                    # If overdue by > 2 hours, elevate to CRITICAL, else HIGH
                    prio = EventPriority.CRITICAL if overdue_seconds > 7200 else EventPriority.HIGH

                    material = {
                        "reminder_id": rem_id,
                        "due_at": due_raw,
                        "message": msg_clean,
                    }
                    evt_id = generate_event_id("reminder_due", str(rem_id), material)
                    is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                    candidates.append(
                        EventCandidate(
                            event_id=evt_id,
                            event_type="reminder_due",
                            entity_id=str(rem_id),
                            occurred_at=due_raw,
                            detected_at=detected_iso,
                            priority=prio,
                            title=f"Reminder due: {msg_clean}",
                            evidence=[f"Reminder #{rem_id} was scheduled for {due_raw} and is unfired."],
                            material_data=material,
                            source="reminders",
                            confidence="HIGH",
                            is_delivered=is_deliv,
                        )
                    )

        except Exception as e:
            logger.error("[EVENT READERS] Error collecting reminder candidates: %s", e)
            raise
        finally:
            conn.close()

        return candidates

    # -----------------------------------------------------------------------
    # Source C: Structured Project and Memory Records
    # -----------------------------------------------------------------------

    def collect_memory_candidates(
        self,
        snapshot: Optional[EventStoreSnapshot] = None,
        now: Optional[datetime] = None,
    ) -> List[EventCandidate]:
        """
        Detect supported project and structured memory records from `structured_memories`.
        Uses `memories_updated_at` watermark to filter older unchanged records.
        
        Safeguards:
        - Excludes session-retired entities (is_session_retired)
        - Excludes sensitive personal/romantic goals (_is_sensitive_personal_goal)
        - Distinguishes creation (created_at == updated_at) from update (updated_at > created_at)
        - Projects are categorized conservatively based on category == 'project'
        - Never fabricates deletion events for hard deletes
        """
        candidates: List[EventCandidate] = []
        local_now = now or datetime.now()
        detected_iso = normalize_utc_timestamp(local_now) or ""

        # Watermark check
        watermark_mem_str = snapshot.get_watermark(WATERMARK_MEMORIES_UPDATED_AT) if snapshot else None
        watermark_mem_dt = parse_utc_timestamp(watermark_mem_str) if watermark_mem_str else None

        conn = self._get_ro_connection()
        try:
            cur = conn.execute(
                "SELECT id, category, memory_key, value, confidence, created_at, updated_at "
                "FROM structured_memories ORDER BY updated_at ASC"
            )
            rows = cur.fetchall()
            for r in rows:
                mem_id = r["id"]
                cat = (r["category"] or "").strip().lower()
                key = (r["memory_key"] or "").strip()
                val = (r["value"] or "").strip()
                up_at_raw = r["updated_at"] or ""
                cr_at_raw = r["created_at"] or ""

                # Privacy & Sensitivity safeguards
                if is_session_retired(val, cat) or is_session_retired(key, cat):
                    continue
                if _is_sensitive_personal_goal(val):
                    continue

                # Watermark filter
                up_dt = safe_parse_utc(up_at_raw)
                if watermark_mem_dt and up_dt and up_dt <= watermark_mem_dt:
                    continue

                val_clean = sanitize_briefing_text(val)
                key_clean = sanitize_briefing_text(key)
                is_create = (cr_at_raw == up_at_raw)
                is_project = (cat == "project")

                if is_project:
                    evt_type = "project_created" if is_create else "project_updated"
                    prio = EventPriority.NORMAL
                    title = f"Project {'created' if is_create else 'updated'}: {val_clean}"
                    evidence = [f"Project memory key='{key_clean}' recorded: {val_clean}."]
                else:
                    evt_type = "memory_created" if is_create else "memory_updated"
                    prio = EventPriority.LOW
                    title = f"Memory {'created' if is_create else 'updated'}: {cat}/{key_clean}"
                    evidence = [f"Structured memory [{cat}] key='{key_clean}' updated."]

                entity_id = f"{cat}:{key}"
                material = {
                    "memory_id": mem_id,
                    "category": cat,
                    "memory_key": key_clean,
                    "value": val_clean,
                    "updated_at": safe_normalize_utc(up_at_raw),
                }

                evt_id = generate_event_id(evt_type, entity_id, material)
                is_deliv = snapshot.is_delivered(evt_id) if snapshot else False

                candidates.append(
                    EventCandidate(
                        event_id=evt_id,
                        event_type=evt_type,
                        entity_id=entity_id,
                        occurred_at=up_at_raw or detected_iso,
                        detected_at=detected_iso,
                        priority=prio,
                        title=title,
                        evidence=evidence,
                        material_data=material,
                        source="structured_memories",
                        confidence="HIGH",
                        is_delivered=is_deliv,
                    )
                )

        except Exception as e:
            logger.error("[EVENT READERS] Error collecting memory candidates: %s", e)
            raise
        finally:
            conn.close()

        return candidates

    # -----------------------------------------------------------------------
    # Aggregated Candidate Discovery
    # -----------------------------------------------------------------------

    def collect_all_candidates(
        self,
        watermark_store: Optional[EventWatermarkStore] = None,
        now: Optional[datetime] = None,
    ) -> List[EventCandidate]:
        """
        Collect and deterministically order all candidate events across all 4 sources.
        
        Fail-Closed Watermark Rules:
        - If watermark_store is provided: validates that it is loaded and not corrupt.
        - If watermark_store is None: attempts to load from self.watermark_store_path.
        - If the watermark store file is missing, corrupt, or invalid:
          FAILS CLOSED immediately by propagating EventStoreNotFoundError / EventStoreCorruptError.
          Never returns an apparently successful empty list `[]` when the persistence store is broken.
          Never silently initializes a new store.
        """
        store = watermark_store
        if store is None:
            # Must load existing store from disk; fails closed if missing or corrupt
            store = EventWatermarkStore.load(self.watermark_store_path)

        # Snapshot is read-only; does not mutate store state
        snapshot = store.get_snapshot()

        all_candidates: List[EventCandidate] = []
        all_candidates.extend(self.collect_job_candidates(snapshot=snapshot, now=now))
        all_candidates.extend(self.collect_job_step_candidates(snapshot=snapshot, now=now))
        all_candidates.extend(self.collect_reminder_candidates(snapshot=snapshot, now=now))
        all_candidates.extend(self.collect_memory_candidates(snapshot=snapshot, now=now))

        return sort_candidates(all_candidates)
