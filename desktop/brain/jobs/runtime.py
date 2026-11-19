"""
brain/jobs/runtime.py - Persistent Execution Runtime for Autonomous Jobs.

Wraps the controlled AgentEngine with durable lifecycle management:
Job lifecycle -> persistence -> AgentEngine -> checkpoint -> Job lifecycle

Adheres strictly to:
- Wraps AgentEngine without duplicating its reasoning or execution logic.
- Model-independent (no direct imports of Gemini, Ollama, or AIProvider).
- Critical checkpoint invariant: Every completed tool action must be persisted
  as a JobStep and AgentState checkpoint before proceeding to the next step.
- Database persistence errors fail the job immediately rather than proceeding.
- Fresh-start attempt replay on retry (never resumes failed attempts from checkpoint).
- WAITING_CONFIRMATION pauses durably and survives restarts without auto-approval.
- External side-effect uncertainty (LIM-01, LIM-03): Process crashes during external
  tool operations (e.g. launching apps) cannot guarantee exactly-once execution.
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Set, Dict, Any

from brain.logger import logger
from brain.agent import AgentEngine
from brain.agent_models import (
    AgentState,
    AgentStatus,
    AgentAction,
    ActionType,
    ToolResult,
)
from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore


class JobPersistenceError(RuntimeError):
    """Raised when writing a step or checkpoint to SQLite fails."""
    pass


class _JobPauseSignal(Exception):
    """Internal signal raised to exit execution loop at a safe step boundary."""
    pass


class JobRuntime:
    """
    Execution Runtime for Persistent Jobs.
    Coordinates AgentEngine execution with durable JobStore transactions.
    """

    def __init__(
        self,
        store: JobStore,
        engine: Optional[AgentEngine] = None,
    ):
        self.store = store
        self.engine = engine or AgentEngine()
        self._pause_requested: Set[str] = set()

    def execute_job(self, job: Job) -> Job:
        """
        Begin or replay an execution attempt for a Job.
        Transitions PENDING or RETRYING -> RUNNING, creates a fresh AgentRun attempt,
        and enforces the step-by-step persistence invariant.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        if job.status != JobStatus.RUNNING:
            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.started_at = job.started_at or now_iso
            job.updated_at = now_iso
            self.store.update_job(job)

        logger.info(
            "[JOB %s] Starting attempt %d/%d for goal: '%s'",
            job.id, job.attempt_count, job.max_attempts, job.goal,
        )

        state_ref: List[AgentState] = []

        def _job_observation_callback(state: AgentState, action: AgentAction, result: ToolResult) -> None:
            """DEF-04: Observation callback — persists step + checkpoint without monkey-patching."""
            if not state_ref:
                state_ref.append(state)
            else:
                state_ref[0] = state

            # 1 & 2. Atomically persist JobStep and AgentState checkpoint (GAP-01)
            try:
                step = JobStep(
                    job_id=job.id,
                    attempt=job.attempt_count,
                    step_index=state.step_count,
                    action_type=action.type.value if hasattr(action.type, "value") else str(action.type),
                    tool_name=action.tool_name,
                    reasoning=action.reasoning_summary or "",
                    success=result.success,
                    error=result.error,
                    duration_ms=result.metadata.get("duration_ms") if result.metadata else None,
                    model_name=result.metadata.get("model_name") if result.metadata else None,
                )
                step.set_arguments(action.arguments or {})
                step.set_result_data(result.data)
                self.store.record_step_and_checkpoint(step, state)
            except Exception as e:
                logger.error("[JOB %s] Database failure recording step and checkpoint: %s", job.id, e)
                raise JobPersistenceError(f"Database failure recording step and checkpoint: {e}") from e

            # 3. Check for cooperative pause request at safe boundary
            if job.id in self._pause_requested:
                self._pause_requested.discard(job.id)
                logger.info("[JOB %s] Cooperative pause boundary reached.", job.id)
                raise _JobPauseSignal()

        try:
            # DEF-04: Pass run-specific callback without monkey-patching _record_observation
            state = self.engine.start_run(job.goal, observation_callback=_job_observation_callback)
        except _JobPauseSignal:
            curr_state = state_ref[0] if state_ref else None
            job.status = JobStatus.PAUSED
            if curr_state:
                job.step_count = curr_state.step_count
                job.set_agent_state(curr_state)
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self.store.update_job(job)
            logger.info("[JOB %s] Paused at clean step boundary.", job.id)
            return job
        except JobPersistenceError as e:
            # Persistence failure: fail the job immediately
            return self._handle_persistence_failure(job, str(e))
        except Exception as e:
            logger.error("[JOB %s] Unexpected runtime exception during run: %s", job.id, e)
            return self._handle_attempt_failure(job, str(e))

        return self._process_run_outcome(job, state)

    def resume_job(self, job_id: str) -> Job:
        """
        Resume an existing Job paused at a clean persistence boundary.
        Loads checkpointed state and re-enters the execution loop.
        """
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        if job.status != JobStatus.PAUSED:
            logger.warning("[JOB %s] Cannot resume job in status '%s'", job.id, job.status.value)
            return job

        state = self.store.load_checkpoint(job.id)
        if not state:
            # GAP-02: Consistent, fail-safe handling for missing/corrupt checkpoint
            logger.error("[JOB %s] Cannot resume job: checkpoint missing or corrupt.", job.id)
            now_iso = datetime.now(timezone.utc).isoformat()
            job.status = JobStatus.FAILED
            job.last_error = "Cannot resume: checkpoint corrupted or missing for paused job."
            job.completed_at = now_iso
            job.updated_at = now_iso
            self.store.update_job(job)
            return job

        now_iso = datetime.now(timezone.utc).isoformat()
        job.status = JobStatus.RUNNING
        job.updated_at = now_iso
        self.store.update_job(job)

        # Restore state into engine
        state.status = AgentStatus.RUNNING
        self.engine._active_runs[state.run_id] = state

        def _job_observation_callback(s: AgentState, action: AgentAction, result: ToolResult) -> None:
            try:
                step = JobStep(
                    job_id=job.id,
                    attempt=job.attempt_count,
                    step_index=s.step_count,
                    action_type=action.type.value if hasattr(action.type, "value") else str(action.type),
                    tool_name=action.tool_name,
                    reasoning=action.reasoning_summary or "",
                    success=result.success,
                    error=result.error,
                    duration_ms=result.metadata.get("duration_ms") if result.metadata else None,
                    model_name=result.metadata.get("model_name") if result.metadata else None,
                )
                step.set_arguments(action.arguments or {})
                step.set_result_data(result.data)
                self.store.record_step_and_checkpoint(step, s)
            except Exception as e:
                raise JobPersistenceError(f"Database failure during resume: {e}") from e

            if job.id in self._pause_requested:
                s.status = AgentStatus.PAUSED

        try:
            state = self.engine._step_loop(state, observation_callback=_job_observation_callback)
        except _JobPauseSignal:
            job.status = JobStatus.PAUSED
            job.step_count = state.step_count
            job.set_agent_state(state)
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self.store.update_job(job)
            logger.info("[JOB %s] Paused at clean step boundary during resume.", job.id)
            return job
        except JobPersistenceError as e:
            return self._handle_persistence_failure(job, str(e))
        except Exception as e:
            return self._handle_attempt_failure(job, str(e))

        return self._process_run_outcome(job, state)

    def pause_job(self, job_id: str) -> Job:
        """
        Request cooperative pause for an active Job.
        Signals the execution loop to pause at the next persistence boundary.
        If job is PENDING, sets it to PAUSED immediately.
        """
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        if job.status == JobStatus.PENDING:
            job.status = JobStatus.PAUSED
            self.store.update_job(job)
            logger.info("[JOB %s] Pending job transitioned to PAUSED.", job.id)
            return job

        if job.status == JobStatus.RUNNING:
            self._pause_requested.add(job.id)
            logger.info("[JOB %s] Pause requested; waiting for next step boundary.", job.id)
            return job

        logger.info("[JOB %s] Job is in status '%s'; pause request ignored.", job.id, job.status.value)
        return job

    def cancel_job(self, job_id: str) -> Job:
        """
        Cancel an active, pending, or paused Job.
        Transitions permanently to CANCELLED and never retries.
        """
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        self._pause_requested.discard(job.id)

        now_iso = datetime.now(timezone.utc).isoformat()
        job.status = JobStatus.CANCELLED
        job.final_response = "Job cancelled by user request."
        job.completed_at = now_iso
        job.updated_at = now_iso
        self.store.update_job(job)
        logger.info("[JOB %s] Job cancelled.", job.id)
        return job

    def confirm_job(self, job_id: str, approved: bool) -> Job:
        """
        Resolve a pending confirmation for a Job in WAITING_CONFIRMATION.
        Survives application restart by recovering pending action and state from SQLite.
        """
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        if job.status != JobStatus.WAITING_CONFIRMATION:
            logger.warning("[JOB %s] Job is not waiting for confirmation (status: %s)", job.id, job.status.value)
            return job

        if not approved:
            # Explicit rejection -> CANCELLED, never retry
            now_iso = datetime.now(timezone.utc).isoformat()
            job.status = JobStatus.CANCELLED
            job.pending_confirmation_json = None
            job.final_response = "Job cancelled: user declined confirmation to execute action."
            job.completed_at = now_iso
            job.updated_at = now_iso
            self.store.update_job(job)
            logger.info("[JOB %s] Confirmation declined; job CANCELLED.", job.id)
            return job

        # Approved -> Restore state and resume execution
        state = self.store.load_checkpoint(job.id)
        if not state:
            logger.error("[JOB %s] Cannot approve confirmation: checkpoint missing.", job.id)
            job.status = JobStatus.FAILED
            job.last_error = "Checkpoint corrupted or missing during confirmation approval."
            self.store.update_job(job)
            return job

        now_iso = datetime.now(timezone.utc).isoformat()
        job.status = JobStatus.RUNNING
        job.pending_confirmation_json = None
        job.updated_at = now_iso
        self.store.update_job(job)

        # Restore state into engine
        self.engine._active_runs[state.run_id] = state

        def _job_observation_callback(s: AgentState, action: AgentAction, result: ToolResult) -> None:
            try:
                step = JobStep(
                    job_id=job.id,
                    attempt=job.attempt_count,
                    step_index=s.step_count,
                    action_type=action.type.value if hasattr(action.type, "value") else str(action.type),
                    tool_name=action.tool_name,
                    reasoning=action.reasoning_summary or "",
                    success=result.success,
                    error=result.error,
                    duration_ms=result.metadata.get("duration_ms") if result.metadata else None,
                    model_name=result.metadata.get("model_name") if result.metadata else None,
                )
                step.set_arguments(action.arguments or {})
                step.set_result_data(result.data)
                self.store.record_step_and_checkpoint(step, s)
            except Exception as e:
                raise JobPersistenceError(f"Database failure during confirmed action: {e}") from e

            if job.id in self._pause_requested:
                self._pause_requested.discard(job.id)
                raise _JobPauseSignal()

        try:
            state = self.engine.resume_run(state.run_id, "yes", observation_callback=_job_observation_callback)
        except _JobPauseSignal:
            job.status = JobStatus.PAUSED
            job.step_count = state.step_count
            job.set_agent_state(state)
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self.store.update_job(job)
            logger.info("[JOB %s] Paused at clean step boundary during confirmed run.", job.id)
            return job
        except JobPersistenceError as e:
            return self._handle_persistence_failure(job, str(e))
        except Exception as e:
            return self._handle_attempt_failure(job, str(e))

        return self._process_run_outcome(job, state)

    def _process_run_outcome(self, job: Job, state: AgentState) -> Job:
        """Map completed AgentState status to durable Job lifecycle transitions."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self._pause_requested.discard(job.id)

        # Case 1: Job completed successfully
        if state.status == AgentStatus.COMPLETED:
            job.status = JobStatus.COMPLETED
            job.final_response = state.final_response or "Task completed successfully."
            job.step_count = state.step_count
            job.completed_at = now_iso
            job.updated_at = now_iso
            job.set_agent_state(state)

            # Record final completion step if last action was FINISH or RESPOND
            if state.last_action and state.last_action.type in (ActionType.FINISH, ActionType.RESPOND):
                try:
                    finish_step = JobStep(
                        job_id=job.id,
                        attempt=job.attempt_count,
                        step_index=state.step_count,
                        action_type=state.last_action.type.value,
                        tool_name=None,
                        reasoning=state.last_action.reasoning_summary or "",
                        success=True,
                        result_data_json=json.dumps({"response": job.final_response}, ensure_ascii=False),
                    )
                    self.store.record_step(finish_step)
                except Exception as e:
                    logger.warning("[JOB %s] Notice recording completion step: %s", job.id, e)

            self.store.update_job(job)
            logger.info("[JOB %s] COMPLETED: %s", job.id, job.final_response)
            return job

        # Case 2: Paused for Confirmation
        if state.status == AgentStatus.WAITING_CONFIRMATION:
            job.status = JobStatus.WAITING_CONFIRMATION
            job.set_pending_confirmation(state.pending_confirmation)
            job.step_count = state.step_count
            job.updated_at = now_iso
            job.set_agent_state(state)
            self.store.update_job(job)
            logger.info("[JOB %s] Paused in WAITING_CONFIRMATION for tool '%s'", job.id, getattr(state.pending_confirmation, "tool_name", "unknown"))
            return job

        # Case 4: Explicitly cancelled (e.g. confirmation declined)
        if state.status == AgentStatus.CANCELLED:
            job.status = JobStatus.CANCELLED
            job.final_response = state.final_response or "Job cancelled."
            job.completed_at = now_iso
            job.updated_at = now_iso
            job.set_agent_state(state)
            self.store.update_job(job)
            logger.info("[JOB %s] CANCELLED.", job.id)
            return job

        # Case 5: Permission Denied -> FAILED (never retried)
        if state.status == AgentStatus.FAILED:
            job.status = JobStatus.FAILED
            job.last_error = state.final_response or "Action blocked by permission policy."
            job.completed_at = now_iso
            job.updated_at = now_iso
            job.set_agent_state(state)
            self.store.update_job(job)
            logger.warning("[JOB %s] FAILED (non-retriable): %s", job.id, job.last_error)
            return job

        # Case 6: Retriable limits hit (MAX_STEPS_REACHED or MAX_RETRIES_REACHED)
        if state.status in (AgentStatus.MAX_RETRIES_REACHED, AgentStatus.MAX_STEPS_REACHED):
            return self._handle_attempt_failure(job, state.final_response or f"Stopped with {state.status.value}")

        # Fallback for unexpected terminal state
        return self._handle_attempt_failure(job, state.final_response or f"Terminated with {state.status.value}")

    def _handle_attempt_failure(self, job: Job, error_msg: str) -> Job:
        """Handle attempt failure applying the locked exponential backoff retry policy."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        if job.attempt_count < job.max_attempts:
            # Calculate exponential backoff
            delay = job.retry_delay_seconds * (2 ** (job.attempt_count - 1))
            next_retry = now + timedelta(seconds=delay)

            job.status = JobStatus.RETRYING
            job.next_retry_at = next_retry.isoformat()
            job.last_error = error_msg
            job.updated_at = now_iso
            self.store.update_job(job)
            logger.info(
                "[JOB %s] Attempt %d failed. Retrying in %ds (attempt %d/%d). Error: %s",
                job.id, job.attempt_count, delay, job.attempt_count + 1, job.max_attempts, error_msg,
            )
            return job
        else:
            job.status = JobStatus.FAILED
            job.last_error = f"Max attempts ({job.max_attempts}) reached. Last error: {error_msg}"
            job.completed_at = now_iso
            job.updated_at = now_iso
            self.store.update_job(job)
            logger.warning("[JOB %s] FAILED permanently: %s", job.id, job.last_error)
            return job

    def _handle_persistence_failure(self, job: Job, error_msg: str) -> Job:
        """Immediately fail the job when a database checkpoint/step write fails."""
        now_iso = datetime.now(timezone.utc).isoformat()
        job.status = JobStatus.FAILED
        job.last_error = f"Persistence invariant violation: {error_msg}"
        job.completed_at = now_iso
        job.updated_at = now_iso
        try:
            self.store.update_job(job)
        except Exception:
            pass
        logger.error("[JOB %s] Job FAILED due to database persistence error: %s", job.id, error_msg)
        return job
