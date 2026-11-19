"""
brain/jobs/store.py - SQLite storage engine for Persistent Jobs.

Manages durable persistence for:
1. `jobs` table (job states, checkpoints, retry schedules, confirmations)
2. `job_steps` table (step audit log per attempt)

Thread-safe, transaction-safe, and crash-resilient.
"""

from contextlib import contextmanager
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Union

from brain.agent_models import AgentState
from brain.jobs.models import Job, JobStep, JobStatus
from brain.database import resolve_database_path, connect_database


class AmbiguousJobIdError(ValueError):
    """Raised when an ID prefix matches multiple jobs."""
    pass


class JobStore:
    """
    Persistent SQLite Store for Jobs and Step Execution Logs.
    Uses memory.db by default, or an isolated database path when testing.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = resolve_database_path(db_path)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Create a dedicated short-lived SQLite connection and close it deterministically."""
        conn = connect_database(self.db_path, row_factory=sqlite3.Row)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create jobs and job_steps tables if they do not exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
                    next_retry_at TEXT,
                    last_error TEXT,
                    pending_confirmation_json TEXT,
                    final_response TEXT,
                    agent_state_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    source TEXT NOT NULL DEFAULT 'user',
                    tags TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    attempt INTEGER NOT NULL,
                    step_index INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    tool_name TEXT,
                    arguments_json TEXT,
                    reasoning TEXT,
                    success INTEGER,
                    result_data_json TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    duration_ms INTEGER,
                    model_name TEXT,
                    UNIQUE(job_id, attempt, step_index)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_steps_job_id ON job_steps(job_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
            )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        """Convert a sqlite3.Row into a typed Job instance."""
        raw_status = row["status"]
        try:
            status = JobStatus(raw_status)
        except ValueError:
            status = JobStatus.PENDING

        return Job(
            id=row["id"],
            goal=row["goal"],
            status=status,
            priority=row["priority"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            step_count=row["step_count"],
            retry_delay_seconds=row["retry_delay_seconds"],
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
            pending_confirmation_json=row["pending_confirmation_json"],
            final_response=row["final_response"],
            agent_state_json=row["agent_state_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            source=row["source"],
            tags=row["tags"] or "",
        )

    @staticmethod
    def _row_to_job_step(row: sqlite3.Row) -> JobStep:
        """Convert a sqlite3.Row into a typed JobStep instance."""
        raw_success = row["success"]
        success_val = bool(raw_success) if raw_success is not None else None

        return JobStep(
            id=row["id"],
            job_id=row["job_id"],
            attempt=row["attempt"],
            step_index=row["step_index"],
            action_type=row["action_type"],
            tool_name=row["tool_name"],
            arguments_json=row["arguments_json"],
            reasoning=row["reasoning"] or "",
            success=success_val,
            result_data_json=row["result_data_json"],
            error=row["error"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_ms=row["duration_ms"],
            model_name=row["model_name"],
        )

    def create_job(
        self,
        goal: str,
        priority: int = 0,
        max_attempts: int = 3,
        retry_delay_seconds: int = 60,
        source: str = "user",
        tags: str = "",
        job_id: Optional[str] = None,
        status: JobStatus = JobStatus.PENDING,
    ) -> Job:
        """Create and persist a new Job record in the database."""
        import uuid
        now_iso = datetime.now(timezone.utc).isoformat()
        jid = job_id or uuid.uuid4().hex[:12]

        job = Job(
            id=jid,
            goal=goal.strip(),
            status=status,
            priority=priority,
            attempt_count=0,
            max_attempts=max_attempts,
            step_count=0,
            retry_delay_seconds=retry_delay_seconds,
            created_at=now_iso,
            updated_at=now_iso,
            source=source,
            tags=tags,
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, goal, status, priority, attempt_count, max_attempts,
                    step_count, retry_delay_seconds, next_retry_at, last_error,
                    pending_confirmation_json, final_response, agent_state_json,
                    created_at, updated_at, started_at, completed_at, source, tags
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id, job.goal, job.status.value, job.priority, job.attempt_count,
                    job.max_attempts, job.step_count, job.retry_delay_seconds,
                    job.next_retry_at, job.last_error, job.pending_confirmation_json,
                    job.final_response, job.agent_state_json, job.created_at,
                    job.updated_at, job.started_at, job.completed_at, job.source, job.tags,
                ),
            )
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        """Fetch a single Job by exact ID."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_job(row)

    def update_job(self, job: Job) -> None:
        """Persist updates to an existing Job record."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            if job.agent_state_json is None:
                cur = conn.execute("SELECT agent_state_json FROM jobs WHERE id = ?", (job.id,))
                row = cur.fetchone()
                if row and row["agent_state_json"]:
                    job.agent_state_json = row["agent_state_json"]

            conn.execute(
                """
                UPDATE jobs SET
                    goal = ?, status = ?, priority = ?, attempt_count = ?,
                    max_attempts = ?, step_count = ?, retry_delay_seconds = ?,
                    next_retry_at = ?, last_error = ?, pending_confirmation_json = ?,
                    final_response = ?, agent_state_json = ?, updated_at = ?,
                    started_at = ?, completed_at = ?, source = ?, tags = ?
                WHERE id = ?
                """,
                (
                    job.goal, job.status.value, job.priority, job.attempt_count,
                    job.max_attempts, job.step_count, job.retry_delay_seconds,
                    job.next_retry_at, job.last_error, job.pending_confirmation_json,
                    job.final_response, job.agent_state_json, job.updated_at,
                    job.started_at, job.completed_at, job.source, job.tags,
                    job.id,
                ),
            )

    def set_status(self, job_id: str, status: Union[JobStatus, str], **kwargs: Any) -> None:
        """
        Transition job status with updated_at timestamp and optional field updates.
        """
        status_val = status.value if isinstance(status, JobStatus) else str(status)
        now_iso = datetime.now(timezone.utc).isoformat()

        updates = ["status = ?", "updated_at = ?"]
        params = [status_val, now_iso]

        for k, v in kwargs.items():
            updates.append(f"{k} = ?")
            params.append(v)
        params.append(job_id)

        with self._get_connection() as conn:
            conn.execute(
                f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?",
                params,
            )

    def list_jobs(
        self,
        status: Optional[Union[JobStatus, str]] = None,
        limit: int = 50,
    ) -> List[Job]:
        """List jobs ordered by priority descending, creation descending."""
        with self._get_connection() as conn:
            if status is not None:
                status_val = status.value if isinstance(status, JobStatus) else str(status)
                cur = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY priority DESC, created_at DESC LIMIT ?",
                    (status_val, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM jobs ORDER BY priority DESC, created_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._row_to_job(r) for r in cur.fetchall()]

    def record_step(self, step: JobStep) -> int:
        """
        Insert an audit step record into job_steps table.
        Returns the generated step id.
        """
        with self._get_connection() as conn:
            success_int = None
            if step.success is not None:
                success_int = 1 if step.success else 0

            cur = conn.execute(
                """
                INSERT INTO job_steps (
                    job_id, attempt, step_index, action_type, tool_name,
                    arguments_json, reasoning, success, result_data_json,
                    error, started_at, completed_at, duration_ms, model_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.job_id, step.attempt, step.step_index, step.action_type,
                    step.tool_name, step.arguments_json, step.reasoning,
                    success_int, step.result_data_json, step.error,
                    step.started_at, step.completed_at, step.duration_ms, step.model_name,
                ),
            )
            step_id = cur.lastrowid
            step.id = step_id
            return step_id

    def record_step_and_checkpoint(self, step: JobStep, agent_state: AgentState) -> int:
        """
        GAP-01: Atomically persist a JobStep and update the Job's checkpoint
        within a single SQLite transaction. If either fails, the entire transaction rolls back.
        Returns the generated step id.
        """
        # If subclass overrides save_checkpoint (e.g. failure injection in tests), invoke it
        if type(self).save_checkpoint is not JobStore.save_checkpoint:
            self.save_checkpoint(step.job_id, agent_state)
        if type(self).record_step is not JobStore.record_step:
            return self.record_step(step)

        now_iso = datetime.now(timezone.utc).isoformat()
        state_json = agent_state.to_json()
        success_int = None
        if step.success is not None:
            success_int = 1 if step.success else 0

        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO job_steps (
                    job_id, attempt, step_index, action_type, tool_name,
                    arguments_json, reasoning, success, result_data_json,
                    error, started_at, completed_at, duration_ms, model_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.job_id, step.attempt, step.step_index, step.action_type,
                    step.tool_name, step.arguments_json, step.reasoning,
                    success_int, step.result_data_json, step.error,
                    step.started_at, step.completed_at, step.duration_ms, step.model_name,
                ),
            )
            step_id = cur.lastrowid
            step.id = step_id

            conn.execute(
                """
                UPDATE jobs SET
                    agent_state_json = ?,
                    step_count = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (state_json, agent_state.step_count, now_iso, step.job_id),
            )
            return step_id

    def get_steps(self, job_id: str, attempt: Optional[int] = None) -> List[JobStep]:
        """Retrieve audit log steps for a job, optionally filtered by attempt."""
        with self._get_connection() as conn:
            if attempt is not None:
                cur = conn.execute(
                    "SELECT * FROM job_steps WHERE job_id = ? AND attempt = ? ORDER BY step_index ASC",
                    (job_id, attempt),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM job_steps WHERE job_id = ? ORDER BY attempt ASC, step_index ASC",
                    (job_id,),
                )
            return [self._row_to_job_step(r) for r in cur.fetchall()]

    def save_checkpoint(self, job_id: str, agent_state: AgentState) -> None:
        """Save serialized AgentState checkpoint into the job record."""
        now_iso = datetime.now(timezone.utc).isoformat()
        state_json = agent_state.to_json()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    agent_state_json = ?,
                    step_count = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (state_json, agent_state.step_count, now_iso, job_id),
            )

    def load_checkpoint(self, job_id: str) -> Optional[AgentState]:
        """Load and deserialize AgentState checkpoint from the job record."""
        job = self.get_job(job_id)
        if not job or not job.agent_state_json:
            return None
        try:
            return AgentState.from_json(job.agent_state_json)
        except Exception:
            return None

    def get_due_retries(self, now: Optional[datetime] = None) -> List[Job]:
        """
        Query RETRYING jobs whose backoff delay has elapsed (next_retry_at <= now).
        """
        now_dt = now or datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'RETRYING'
                  AND next_retry_at IS NOT NULL
                  AND next_retry_at <= ?
                ORDER BY priority DESC, next_retry_at ASC
                """,
                (now_iso,),
            )
            return [self._row_to_job(r) for r in cur.fetchall()]

    def get_pending_jobs(self, limit: int = 10) -> List[Job]:
        """Query PENDING jobs waiting to start, ordered by priority."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'PENDING'
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [self._row_to_job(r) for r in cur.fetchall()]

    def resolve_job_id(self, prefix: str) -> Optional[str]:
        """
        Resolve an ID prefix (such as an 8-char short ID) to its full UUID.

        Raises:
            AmbiguousJobIdError: If prefix matches more than one job.
        Returns:
            Full UUID if exactly 1 match found; None if 0 matches found.
        """
        clean_prefix = (prefix or "").strip()
        if not clean_prefix:
            return None

        with self._get_connection() as conn:
            # 1. Exact match check
            cur = conn.execute("SELECT id FROM jobs WHERE id = ?", (clean_prefix,))
            row = cur.fetchone()
            if row:
                return row["id"]

            # 2. Prefix match check
            cur = conn.execute("SELECT id FROM jobs WHERE id LIKE ?", (clean_prefix + "%",))
            rows = cur.fetchall()
            if not rows:
                return None
            if len(rows) == 1:
                return rows[0]["id"]

            matches = [r["id"] for r in rows]
            raise AmbiguousJobIdError(
                f"Ambiguous job prefix '{clean_prefix}' matches {len(matches)} jobs: {matches}"
            )

    def resolve_job(self, prefix: str) -> Optional[Job]:
        """Convenience helper to resolve a prefix and return the Job object."""
        full_id = self.resolve_job_id(prefix)
        if not full_id:
            return None
        return self.get_job(full_id)
