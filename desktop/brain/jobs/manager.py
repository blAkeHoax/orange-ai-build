"""
brain/jobs/manager.py - High-Level Job Lifecycle Coordinator.

Provides the public orchestration API for:
- create()
- start()
- pause()
- resume()
- cancel()
- confirm()
- get_job()
- list_jobs()
- get_history()
- recover_interrupted()

Enforces:
- Locked concurrency rule: Maximum 1 active RUNNING job at any time.
- If another job is running, new or resumed jobs remain PENDING.
- Safe crash recovery without re-executing uncertain consequential actions.
- Synchronized state transitions to prevent check-then-act races (LIM-02).
"""

import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Union

from brain.logger import logger
from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore
from brain.jobs.runtime import JobRuntime


class JobManager:
    """
    High-level Coordinator for Persistent Jobs.
    Coordinates store persistence, runtime execution, and concurrency control.
    """

    # Status precondition: only these statuses may enter start() (DEF-01).
    _STARTABLE_STATUSES = (JobStatus.PENDING, JobStatus.RETRYING)

    def __init__(
        self,
        store: Optional[JobStore] = None,
        runtime: Optional[JobRuntime] = None,
    ):
        self.store = store or JobStore()
        self.runtime = runtime or JobRuntime(store=self.store)
        self._lock = threading.RLock()

    def _is_any_job_running(self, exclude_id: Optional[str] = None) -> bool:
        """Check if any job is currently in RUNNING status."""
        running_jobs = self.store.list_jobs(status=JobStatus.RUNNING)
        if exclude_id:
            running_jobs = [j for j in running_jobs if j.id != exclude_id]
        return len(running_jobs) > 0

    def create(
        self,
        goal: str,
        priority: int = 0,
        max_attempts: int = 3,
        retry_delay_seconds: int = 60,
        source: str = "user",
        tags: str = "",
    ) -> Job:
        """Create a new job in PENDING status."""
        job = self.store.create_job(
            goal=goal,
            priority=priority,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            source=source,
            tags=tags,
            status=JobStatus.PENDING,
        )
        logger.info("[JOB MANAGER] Created Job '%s' [PENDING]: '%s'", job.id, job.goal)
        return job

    def start(self, job_id: str) -> Job:
        """
        Start execution of a job.
        Enforces status preconditions (DEF-01) and atomic concurrency claim (LIM-02).
        """
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            raise ValueError(f"Job with ID or prefix '{job_id}' not found.")

        with self._lock:
            job = self.store.get_job(resolved_id)
            if not job:
                raise ValueError(f"Job '{resolved_id}' not found.")

            # DEF-01: Enforce valid job-status preconditions before execution.
            if job.status not in self._STARTABLE_STATUSES:
                if job.status == JobStatus.PAUSED:
                    raise ValueError(
                        f"Job '{job.short_id}' is PAUSED. Use 'job resume {job.short_id}' to continue from checkpoint."
                    )
                elif job.status == JobStatus.WAITING_CONFIRMATION:
                    raise ValueError(
                        f"Job '{job.short_id}' is WAITING_CONFIRMATION. Use 'job confirm {job.short_id} yes/no' to resolve."
                    )
                elif job.status == JobStatus.RUNNING:
                    raise ValueError(f"Job '{job.short_id}' is already RUNNING.")
                else:
                    raise ValueError(
                        f"Job '{job.short_id}' is in terminal state '{job.status.value}'. Cannot restart."
                    )

            if self._is_any_job_running(exclude_id=job.id):
                logger.warning(
                    "[JOB MANAGER] Concurrency limit: another job is currently RUNNING. Job '%s' remains PENDING.",
                    job.id,
                )
                if job.status != JobStatus.PENDING:
                    self.store.set_status(job.id, JobStatus.PENDING)
                    job.status = JobStatus.PENDING
                return job

            # LIM-02: Atomically claim slot by transitioning to RUNNING under lock
            now_iso = datetime.now(timezone.utc).isoformat()
            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.started_at = job.started_at or now_iso
            job.updated_at = now_iso
            self.store.update_job(job)

        return self.runtime.execute_job(job)

    def pause(self, job_id: str) -> Job:
        """Request cooperative pause for a job."""
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            raise ValueError(f"Job with ID or prefix '{job_id}' not found.")
        return self.runtime.pause_job(resolved_id)

    def resume(self, job_id: str) -> Job:
        """
        Resume a paused job.
        Enforces concurrency: If another job is RUNNING, cannot resume immediately.
        """
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            raise ValueError(f"Job with ID or prefix '{job_id}' not found.")

        with self._lock:
            if self._is_any_job_running(exclude_id=resolved_id):
                logger.warning(
                    "[JOB MANAGER] Concurrency limit: another job is currently RUNNING. Cannot resume '%s'.",
                    resolved_id,
                )
                job = self.store.get_job(resolved_id)
                return job or Job(id=resolved_id, goal="", status=JobStatus.PAUSED)

            return self.runtime.resume_job(resolved_id)

    def cancel(self, job_id: str) -> Job:
        """Cancel a job permanently."""
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            raise ValueError(f"Job with ID or prefix '{job_id}' not found.")
        return self.runtime.cancel_job(resolved_id)

    def confirm(self, job_id: str, approval: Union[str, bool]) -> Job:
        """
        Resolve confirmation for a job waiting for user permission.
        Enforces concurrency if approval is granted.
        """
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            raise ValueError(f"Job with ID or prefix '{job_id}' not found.")

        clean_approval = False
        if isinstance(approval, bool):
            clean_approval = approval
        elif isinstance(approval, str):
            clean_approval = approval.strip().lower() in ("y", "yes", "true", "1")

        with self._lock:
            if clean_approval and self._is_any_job_running(exclude_id=resolved_id):
                logger.warning(
                    "[JOB MANAGER] Concurrency limit: another job is currently RUNNING. Cannot confirm-resume '%s'.",
                    resolved_id,
                )
                job = self.store.get_job(resolved_id)
                return job or Job(id=resolved_id, goal="", status=JobStatus.WAITING_CONFIRMATION)

            return self.runtime.confirm_job(resolved_id, approved=clean_approval)

    def get_job(self, job_id: str) -> Optional[Job]:
        """Fetch a job by full UUID or short ID prefix."""
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            return None
        return self.store.get_job(resolved_id)

    def list_jobs(
        self,
        status: Optional[Union[JobStatus, str]] = None,
        limit: int = 50,
    ) -> List[Job]:
        """List jobs with optional status filter."""
        return self.store.list_jobs(status=status, limit=limit)

    def get_history(self, job_id: str) -> List[JobStep]:
        """Fetch full step audit logs for a job."""
        resolved_id = self.store.resolve_job_id(job_id)
        if not resolved_id:
            return []
        return self.store.get_steps(resolved_id)

    def recover_interrupted(self) -> List[Job]:
        """
        Identify and safely recover jobs left in RUNNING status due to a process crash.
        Enforces crash recovery policy:
        - If terminal step (FINISH/RESPOND) recorded: COMPLETED (DEF-02).
        - If checkpoint and step logs match with certainty: PAUSED at step boundary.
        - Repeated crashes at same boundary: capped at 2 recoveries, then FAILED (DEF-06).
        - If uncertainty exists: fresh-start attempt (RETRYING) or FAILED (max attempts).
        - Steps isolated strictly to current attempt (DEF-03).
        """
        interrupted_jobs = self.store.list_jobs(status=JobStatus.RUNNING)
        recovered = []

        for job in interrupted_jobs:
            current_attempt = job.attempt_count if job.attempt_count > 0 else 1
            # DEF-03: Only use steps from the CURRENT attempt.
            steps = self.store.get_steps(job.id, attempt=current_attempt)
            checkpoint = self.store.load_checkpoint(job.id)

            # DEF-02: Check for a recorded terminal step (FINISH/RESPOND)
            # BEFORE the general certainty check.
            if steps and steps[-1].action_type in ("FINISH", "RESPOND") and steps[-1].success is True:
                now_iso = datetime.now(timezone.utc).isoformat()
                job.status = JobStatus.COMPLETED
                try:
                    result_data = steps[-1].get_result_data()
                    if isinstance(result_data, dict):
                        job.final_response = result_data.get("response", "Task completed successfully.")
                    else:
                        job.final_response = "Task completed successfully."
                except Exception:
                    job.final_response = "Task completed successfully."
                job.completed_at = now_iso
                job.last_error = "Recovered from crash: terminal step was recorded; completing job."
                self.store.update_job(job)
                recovered.append(job)
                logger.info("[JOB MANAGER] Job '%s' terminal step found; recovered as COMPLETED.", job.id)
                continue

            certainty_established = False

            if checkpoint and steps:
                obs_count = len(checkpoint.observations)
                completed_steps_count = sum(1 for s in steps if s.success is not None)
                last_step_finished = (steps[-1].success is not None)

                if obs_count == completed_steps_count and last_step_finished:
                    certainty_established = True

            if certainty_established:
                # DEF-06: Bound repeated crash recovery.
                step_idx = steps[-1].step_index if steps else 0
                crash_count = 1
                if job.last_error:
                    m = re.search(r"\[crash_count:(\d+)@step:(\d+)\]", job.last_error)
                    if m:
                        prev_count, prev_step = int(m.group(1)), int(m.group(2))
                        if prev_step == step_idx:
                            crash_count = prev_count + 1

                # Cap repeated crashes at the same boundary (max 2 recoveries allowed; on 3rd crash, fail)
                if crash_count > 2:
                    job.status = JobStatus.FAILED
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    job.last_error = (
                        f"Repeated process crashes detected at step boundary (step {step_idx}, "
                        f"crashed {crash_count} times). Job marked FAILED."
                    )
                    logger.warning(
                        "[JOB MANAGER] Job '%s' exceeded crash recovery limit at step %d; marked FAILED.",
                        job.id, step_idx,
                    )
                else:
                    job.status = JobStatus.PAUSED
                    job.last_error = (
                        f"Recovered from process crash: safely paused at last verified step boundary. "
                        f"[crash_count:{crash_count}@step:{step_idx}]"
                    )
                    logger.info(
                        "[JOB MANAGER] Job '%s' certainty established; safely transitioned to PAUSED (crash %d).",
                        job.id, crash_count,
                    )
                self.store.update_job(job)
                recovered.append(job)
            else:
                job.attempt_count += 1
                if job.attempt_count < job.max_attempts:
                    job.status = JobStatus.RETRYING
                    delay = job.retry_delay_seconds * (2 ** (job.attempt_count - 1))
                    job.next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    job.last_error = "Recovered from process crash: execution uncertainty detected; fresh attempt scheduled."
                    logger.warning("[JOB MANAGER] Job '%s' uncertainty detected; scheduled fresh attempt as RETRYING.", job.id)
                else:
                    job.status = JobStatus.FAILED
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    job.last_error = f"Recovered from process crash: uncertainty detected and max attempts ({job.max_attempts}) reached."
                    logger.warning("[JOB MANAGER] Job '%s' uncertainty detected; max attempts reached; marked FAILED.", job.id)

                self.store.update_job(job)
                recovered.append(job)

        return recovered
