import sys
import os
import io
import re
import contextlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Dict, Any, List

# Windows consoles can still default to a legacy code page (for example
# cp1252), which cannot print the emoji used throughout Orange AI.  Configure
# standard output before the startup banner so a double-clicked EXE does not
# exit immediately with a UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from brain.personality import SYSTEM_PROMPT, greeting, mood_prompt_addendum
from brain.router import route
from brain.intents import Intent
from brain.dispatcher import dispatch
from brain.logger import logger
from brain.api_client import (
    OrangeAPIClient,
    OrangeAPIConnectionError,
    OrangeAPIResponseError,
    OrangeAPIError,
)
from brain.memory_classifier import classify_memory
from brain.memory import (
    remember,
    recall,
    forget,
    save_turn,
    load_history,
    remember_structured,
    build_profile,
    recall_structured,
    count_legacy,
    initialize_memory,
    retire_structured,
    recall_reconciled,
    format_profile_inspection,
    format_category_inspection,
    retract_entity,
    retrieve_relevant_context,
    budget_chat_memory,
)
from brain import scheduler
from brain import mood
from brain import planner
from brain.agent import AgentEngine, AgentStatus
from brain.jobs import (
    JobStore,
    JobManager,
    JobRuntime,
    JobStatus,
    AmbiguousJobIdError,
)
from tools.system import is_running_as_admin
import json

# Global runtime state initialized lazily or inside run_cli() to prevent
# database access or table creation during module import.
history = None
MAX_HISTORY_TURNS = 40
active_plan = None
api_client = None
agent_engine = None

job_store = None
job_runtime = None
job_manager = None


def get_job_manager() -> JobManager:
    """Return the global JobManager instance, initializing the job subsystem lazily."""
    global job_store, job_runtime, job_manager, agent_engine, api_client
    if job_manager is None:
        if api_client is None:
            api_client = OrangeAPIClient()
        if agent_engine is None:
            agent_engine = AgentEngine(api_client=api_client)
        if job_store is None:
            job_store = JobStore()
        if job_runtime is None:
            job_runtime = JobRuntime(store=job_store, engine=agent_engine)
        job_manager = JobManager(store=job_store, runtime=job_runtime)
    return job_manager


def _on_reminder_due(reminder):
    """
    Called from the background scheduler thread (NOT the main thread)
    whenever a reminder comes due.
    """
    print(f"\n\n⏰ Orange AI: Reminder -- {reminder['message']}\nYou: ", end="", flush=True)


def _on_job_event(job):
    """
    Called from the background scheduler thread when a background or retried job
    changes status (e.g. requires confirmation, completed, or failed).
    """
    if not job:
        return
    if job.status == JobStatus.WAITING_CONFIRMATION:
        pending = job.get_pending_confirmation()
        tool_name = pending.tool_name if pending else "action"
        print(
            f"\n\n⚠️  Orange AI [Job {job.short_id}]: Action '{tool_name}' requires confirmation.\n"
            f"   Use 'job confirm {job.short_id} yes' or 'job confirm {job.short_id} no'.\nYou: ",
            end="",
            flush=True,
        )
    elif job.status == JobStatus.COMPLETED:
        print(
            f"\n\n✅ Orange AI [Job {job.short_id}]: Completed: {job.final_response}\nYou: ",
            end="",
            flush=True,
        )
    elif job.status == JobStatus.FAILED:
        print(
            f"\n\n❌ Orange AI [Job {job.short_id}]: Failed: {job.last_error}\nYou: ",
            end="",
            flush=True,
        )



def _render_job_outcome(job):
    """Format and print the outcome of a job execution."""
    if not job:
        return
    if job.status == JobStatus.PENDING:
        print(f"\nOrange AI [Job {job.short_id}]: Another job is currently running. Queued in PENDING status.")
    elif job.status == JobStatus.WAITING_CONFIRMATION:
        pending = job.get_pending_confirmation()
        tool_name = pending.tool_name if pending else "action"
        args_str = str(pending.arguments) if pending and pending.arguments else ""
        print(f"\n⚠️  Orange AI [Job {job.short_id}]: Paused for confirmation to execute '{tool_name}' ({args_str}).")
        print(f"   Use 'job confirm {job.short_id} yes' to approve or 'job confirm {job.short_id} no' to decline.")
    elif job.status == JobStatus.COMPLETED:
        print(f"\nOrange AI [Job {job.short_id}]: {job.final_response or 'Completed successfully.'}")
    elif job.status == JobStatus.FAILED:
        print(f"\nOrange AI [Job {job.short_id}]: FAILED -- {job.last_error or 'Execution failed.'}")
    elif job.status == JobStatus.CANCELLED:
        print(f"\nOrange AI [Job {job.short_id}]: CANCELLED -- {job.final_response or 'Cancelled.'}")
    elif job.status == JobStatus.PAUSED:
        print(f"\nOrange AI [Job {job.short_id}]: PAUSED at clean step boundary. Use 'job resume {job.short_id}' to continue.")
    elif job.status == JobStatus.RETRYING:
        print(f"\nOrange AI [Job {job.short_id}]: RETRYING (attempt {job.attempt_count}/{job.max_attempts}). Next retry at: {job.next_retry_at}")
    else:
        print(f"\nOrange AI [Job {job.short_id}]: Status: {job.status.value}")


def handle_job_cli(user_input: str, manager: JobManager) -> bool:
    """
    Parse and execute CLI commands under the 'job' namespace.
    Returns True if user_input was handled as a job command; False otherwise.
    """
    cmd = user_input.strip()
    lower = cmd.lower()

    if not lower.startswith("job"):
        return False

    parts = cmd.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if lower == "job":
        print("\nOrange AI: Job command syntax:")
        print("   job <goal>                     - Create and immediately start a persistent job")
        print("   job create <goal>              - Create a job in PENDING status")
        print("   job start <id>                 - Start execution of a pending job")
        print("   job list [status]              - List jobs (optional status filter)")
        print("   job status <id>                - Show detailed job status")
        print("   job pause <id>                 - Cooperatively pause an active job")
        print("   job resume <id>                - Resume a paused job from checkpoint")
        print("   job cancel <id>                - Permanently cancel a job")
        print("   job confirm <id> yes/no        - Approve or reject a pending action")
        print("   job history <id>               - View chronological step audit trail")
        return True

    if sub == "create":
        goal = cmd[10:].strip()
        if not goal:
            print("\nOrange AI: Please specify a goal for the job. e.g. 'job create check system health'")
            return True
        job = manager.create(goal)
        print(f"\nOrange AI: Created job [{job.short_id}] in PENDING status.")
        print(f"   Goal: '{job.goal}'")
        print(f"   Use 'job start {job.short_id}' to execute.")
        return True

    if sub == "start":
        target = cmd[9:].strip()
        if not target:
            print("\nOrange AI: Please specify a job ID. e.g. 'job start <id>'")
            return True
        try:
            print(f"\n🍊 Job Engine: Starting job [{target}]...")
            res = manager.start(target)
            _render_job_outcome(res)
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix. Please provide a more specific ID.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error starting job: {e}")
        return True

    if sub == "list":
        filter_status = parts[2].strip().upper() if len(parts) > 2 else None
        try:
            jobs = manager.list_jobs(status=filter_status)
            if not jobs:
                status_msg = f" with status '{filter_status}'" if filter_status else ""
                print(f"\nOrange AI: No jobs found{status_msg}.")
                return True

            print("\n" + "="*85)
            print(f"{'ID':<10} {'STATUS':<22} {'ATTEMPTS':<10} {'STEPS':<8} {'UPDATED':<18} {'GOAL'}")
            print("="*85)
            for j in jobs:
                up_dt = j.updated_at[:19].replace("T", " ") if j.updated_at else "-"
                goal_summary = (j.goal[:24] + "...") if len(j.goal) > 27 else j.goal
                print(f"{j.short_id:<10} {j.status.value:<22} {f'{j.attempt_count}/{j.max_attempts}':<10} {j.step_count:<8} {up_dt:<18} {goal_summary}")
            print("="*85)
        except Exception as e:
            print(f"\nOrange AI: Error listing jobs: {e}")
        return True

    if sub == "status":
        target = cmd[10:].strip()
        if not target:
            print("\nOrange AI: Please specify a job ID. e.g. 'job status <id>'")
            return True
        try:
            job = manager.get_job(target)
            if not job:
                print(f"\nOrange AI: No job found matching ID '{target}'.")
                return True

            print("\n" + "="*60)
            print(f"🍊 Job Status: [{job.short_id}]")
            print("="*60)
            print(f"Full ID:       {job.id}")
            print(f"Goal:          {job.goal}")
            print(f"Status:        {job.status.value}")
            print(f"Priority:      {job.priority}")
            print(f"Attempts:      {job.attempt_count} / {job.max_attempts}")
            print(f"Steps:         {job.step_count}")
            print(f"Created:       {job.created_at}")
            print(f"Updated:       {job.updated_at}")
            if job.started_at:
                print(f"Started:       {job.started_at}")
            if job.completed_at:
                print(f"Completed:     {job.completed_at}")
            if job.next_retry_at:
                print(f"Next Retry:    {job.next_retry_at}")
            if job.last_error:
                print(f"Last Error:    {job.last_error}")
            if job.pending_confirmation_json:
                pending = job.get_pending_confirmation()
                tool_str = pending.tool_name if pending else "unknown"
                args_str = str(pending.arguments) if pending and pending.arguments else ""
                print(f"Pending Action: {tool_str} ({args_str})")
            if job.final_response:
                print(f"Final Result:  {job.final_response}")
            print("="*60)
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix. Please provide a more specific ID.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error fetching job status: {e}")
        return True

    if sub == "pause":
        target = cmd[9:].strip()
        if not target:
            print("\nOrange AI: Please specify a job ID. e.g. 'job pause <id>'")
            return True
        try:
            res = manager.pause(target)
            if res.status == JobStatus.PAUSED:
                print(f"\nOrange AI: Job [{res.short_id}] paused at safe step boundary.")
            else:
                print(f"\nOrange AI: Pause requested for job [{res.short_id}] (current status: {res.status.value}).")
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error pausing job: {e}")
        return True

    if sub == "resume":
        target = cmd[10:].strip()
        if not target:
            print("\nOrange AI: Please specify a job ID. e.g. 'job resume <id>'")
            return True
        try:
            print(f"\n🍊 Job Engine: Resuming job [{target}]...")
            res = manager.resume(target)
            _render_job_outcome(res)
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error resuming job: {e}")
        return True

    if sub == "cancel":
        target = cmd[10:].strip()
        if not target:
            print("\nOrange AI: Please specify a job ID. e.g. 'job cancel <id>'")
            return True
        try:
            res = manager.cancel(target)
            print(f"\nOrange AI: Job [{res.short_id}] has been cancelled.")
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error cancelling job: {e}")
        return True

    if sub == "confirm":
        rem = cmd[11:].strip().split()
        if len(rem) < 2 or rem[1].lower() not in ("yes", "no", "y", "n"):
            print("\nOrange AI: Please specify 'yes' or 'no': e.g. 'job confirm <id> yes' or 'job confirm <id> no'.")
            return True
        target = rem[0]
        choice = rem[1].lower() in ("yes", "y")
        try:
            res = manager.confirm(target, choice)
            if not choice:
                print(f"\nOrange AI: Job [{res.short_id}] confirmation declined; job CANCELLED.")
            else:
                _render_job_outcome(res)
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error confirming job: {e}")
        return True

    if sub == "history":
        target = cmd[11:].strip()
        if not target:
            print("\nOrange AI: Please specify a job ID. e.g. 'job history <id>'")
            return True
        try:
            job = manager.get_job(target)
            steps = manager.get_history(target)
            if not steps:
                label = f"job [{job.short_id}]" if job else f"ID '{target}'"
                print(f"\nOrange AI: No execution steps recorded for {label}.")
                return True

            job_id_label = job.short_id if job else target
            print("\n" + "="*70)
            print(f"Audit Trail for Job [{job_id_label}]: {len(steps)} step(s)")
            print("="*70)
            for idx, s in enumerate(steps, start=1):
                tool_label = f" (Tool: {s.tool_name})" if s.tool_name else ""
                status_label = "SUCCESS" if s.success else ("FAILED" if s.success is False else "PENDING")
                dur_label = f" [{s.duration_ms}ms]" if s.duration_ms is not None else ""
                print(f"#{idx} [Attempt {s.attempt}, Step {s.step_index}] {s.action_type}{tool_label} -> {status_label}{dur_label}")
                if s.reasoning:
                    print(f"   Reasoning: {s.reasoning}")
                if s.error:
                    print(f"   Error:     {s.error}")
                elif s.result_data_json:
                    try:
                        parsed = json.loads(s.result_data_json)
                        print(f"   Result:    {parsed}")
                    except Exception:
                        print(f"   Result:    {s.result_data_json}")
            print("="*70)
        except AmbiguousJobIdError:
            print(f"\nOrange AI: Ambiguous job ID '{target}'. Multiple jobs match this prefix.")
        except ValueError as e:
            print(f"\nOrange AI: {e}")
        except Exception as e:
            print(f"\nOrange AI: Error fetching job history: {e}")
        return True

    # Default 'job <goal>' (create and immediate start)
    goal = cmd[4:].strip()
    if not goal:
        print("\nOrange AI: Please specify a goal for the job. e.g. 'job check CPU usage'")
        return True

    try:
        job = manager.create(goal)
        print(f"\n🍊 Job Engine: Starting persistent job [{job.short_id}] '{job.goal}'...")
        res = manager.start(job.id)
        _render_job_outcome(res)
    except Exception as e:
        print(f"\nOrange AI: Error running job: {e}")

    return True


def handle_recommendation_cli(user_input: str) -> Optional[Dict[str, Any]]:
    """
    Parse and execute CLI commands under the 'rec' or 'recommendation' namespace.
    Modifies recommendation lifecycle metadata ONLY; never modifies underlying operational data.
    Returns response dict if handled, or None if not recognized.
    """
    cmd = user_input.strip()
    lowered = cmd.lower()
    parts = lowered.split()
    if not parts or parts[0] not in ("rec", "recommendation", "recommendations"):
        return None

    from brain.briefing.recommendations import RecommendationStore, RecommendationState
    store = RecommendationStore()

    sub = parts[1] if len(parts) > 1 else "list"

    if sub in ("list", "ls", "show"):
        all_recs = store.list_all()
        if not all_recs:
            return {"reply": "No recommendations currently tracked in lifecycle store.", "model": "recommendations", "type": "recommendations"}

        lines = ["Contextual Recommendations:"]
        active_items = [r for r in all_recs if r.state == RecommendationState.ACTIVE]
        other_items = [r for r in all_recs if r.state != RecommendationState.ACTIVE]

        if active_items:
            lines.append("\n--- Active Recommendations ---")
            for r in sorted(active_items, key=lambda x: (x.priority.rank, x.rec_id)):
                lines.append(f"[{r.short_id}] [{r.priority.value}] {r.title}")
                lines.append(f"   Suggestion: {r.suggested_action}")
        else:
            lines.append("\nNo active recommendations at this time.")

        if other_items:
            lines.append("\n--- Suppressed / Inactive ---")
            for r in other_items:
                supp = f" (until {r.suppressed_until})" if r.suppressed_until else ""
                lines.append(f"[{r.short_id}] [{r.state.value}{supp}] {r.title}")

        lines.append("\nCommands: 'rec dismiss <id>', 'rec snooze <id> [24h|7d]', 'rec handle <id>', 'rec why <id>'")
        res_payload = {"reply": "\n".join(lines), "model": "recommendations", "type": "recommendations"}
        if active_items:
            res_payload["recommendations"] = [r.to_dict() for r in active_items]
        return res_payload

    if sub in ("dismiss", "ignore"):
        if len(parts) < 3:
            return {"reply": "Please specify a recommendation ID: e.g. 'rec dismiss <id>'", "model": "recommendations", "type": "recommendations"}
        target = parts[2]
        success = store.dismiss(target)
        if success:
            return {"reply": f"✅ Recommendation [{target}] dismissed for 7 days.", "model": "recommendations", "type": "recommendations"}
        return {"reply": f"Recommendation [{target}] not found or could not be dismissed.", "model": "recommendations", "type": "recommendations"}

    if sub in ("snooze", "defer"):
        if len(parts) < 3:
            return {"reply": "Please specify a recommendation ID: e.g. 'rec snooze <id> 24h'", "model": "recommendations", "type": "recommendations"}
        target = parts[2]
        raw_parts = cmd.split()
        duration = " ".join(raw_parts[3:]) if len(raw_parts) > 3 else "24h"
        success = store.defer(target, duration)
        if success:
            rec = store._resolve_target(target)
            until_str = rec.suppressed_until if rec else duration
            return {"reply": f"✅ Recommendation [{target}] deferred until {until_str}.", "model": "recommendations", "type": "recommendations"}
        return {"reply": f"Error: Cannot defer [{target}]. Please specify a valid future date/time (e.g. '24h', '7d', or ISO format 'YYYY-MM-DDTHH:MM:SS').", "model": "recommendations", "type": "recommendations"}

    if sub in ("handle", "handled", "done", "resolve"):
        if len(parts) < 3:
            return {"reply": "Please specify a recommendation ID: e.g. 'rec handle <id>'", "model": "recommendations", "type": "recommendations"}
        target = parts[2]
        success = store.handle(target)
        if success:
            return {"reply": f"✅ Recommendation [{target}] marked handled. Will remain closed unless material evidence changes.", "model": "recommendations", "type": "recommendations"}
        return {"reply": f"Recommendation [{target}] not found or could not be marked handled.", "model": "recommendations", "type": "recommendations"}

    if sub in ("why", "evidence", "details", "info"):
        if len(parts) < 3:
            return {"reply": "Please specify a recommendation ID: e.g. 'rec why <id>'", "model": "recommendations", "type": "recommendations"}
        target = parts[2]
        rec = store._resolve_target(target)
        if not rec:
            return {"reply": f"Recommendation [{target}] not found.", "model": "recommendations", "type": "recommendations"}

        lines = [
            f"🍊 Recommendation Details: [{rec.short_id}]",
            f"Full ID:       {rec.rec_id}",
            f"Category:      {rec.category}",
            f"Priority:      {rec.priority.value}",
            f"State:         {rec.state.value}",
            f"Title:         {rec.title}",
            f"Explanation:   {rec.explanation}",
            f"Suggestion:    {rec.suggested_action}",
            f"Fingerprint:   {rec.evidence_fingerprint}",
        ]
        if rec.suppressed_until:
            lines.append(f"Suppressed:    Until {rec.suppressed_until}")
        if rec.handled_fingerprint:
            lines.append(f"Handled FP:    {rec.handled_fingerprint}")
        if rec.evidence:
            lines.append("\nSupporting Evidence:")
            for ev in rec.evidence:
                lines.append(f"- {ev}")
        return {"reply": "\n".join(lines), "model": "recommendations", "type": "recommendations", "recommendation": rec.to_dict()}

    return {"reply": f"Unknown recommendation command '{sub}'. Use 'rec list', 'rec dismiss <id>', 'rec snooze <id> [24h|7d]', 'rec handle <id>', or 'rec why <id>'.", "model": "recommendations", "type": "recommendations"}


def _check_contextual_chat_recommendation(user_text: str) -> Optional[str]:
    """
    Check if a single relevant recommendation should be appended to the chat response.
    Strictly gates on user discussing the associated project or job entity (Correction B).
    Never delivers unsolicited recommendations for general conversation.
    Gated by Stage 4.1 contextual advisories setting and user-configured cooldown.
    """
    try:
        from brain.briefing.settings import (
            is_contextual_advisories_enabled,
            get_advisory_cooldown_hours,
        )
        if not is_contextual_advisories_enabled():
            return None

        from brain.briefing.recommendations import (
            RecommendationStore,
            RecommendationState,
            match_recommendation_relevance,
        )
        from brain.briefing.collector import sanitize_briefing_text, _parse_timestamp
        store = RecommendationStore()
        active_recs = [r for r in store.list_all() if r.state == RecommendationState.ACTIVE]
        if not active_recs:
            return None

        # Check relevance: find recommendation matching the specific entity being discussed
        relevant_rec = None
        is_explicit = False
        for r in sorted(active_recs, key=lambda x: (x.priority.rank, x.rec_id)):
            matched, explicit = match_recommendation_relevance(r, user_text)
            if matched:
                relevant_rec = r
                is_explicit = explicit
                break

        if not relevant_rec:
            return None

        # Check user-configured cooldown: only bypass if user explicitly asked about this exact entity
        cooldown_hours = get_advisory_cooldown_hours()
        if "chat" in relevant_rec.channels and relevant_rec.last_surfaced_at:
            surfaced_dt = _parse_timestamp(relevant_rec.last_surfaced_at)
            if surfaced_dt:
                elapsed = (datetime.now() - surfaced_dt).total_seconds()
                if elapsed < cooldown_hours * 3600 and not is_explicit:
                    return None

        store.record_surfaced(relevant_rec.rec_id, channel="chat")

        prio_tag = f"[{relevant_rec.priority.value}]"
        clean_title = sanitize_briefing_text(relevant_rec.title)
        clean_exp = sanitize_briefing_text(relevant_rec.explanation)
        clean_sugg = sanitize_briefing_text(relevant_rec.suggested_action)

        advisory = (
            f"\n\n💡 **Orange AI Advisory** {prio_tag}: **{clean_title}**\n"
            f"   {clean_exp}\n"
            f"   *Suggestion:* {clean_sugg} *(ID: `{relevant_rec.short_id}`)*"
        )
        return advisory
    except Exception as e:
        logger.warning("[CHAT RECOMMENDATION] Failed to evaluate chat recommendation: %s", e)
        return None


def ensure_initialized():
    """Ensure core shared assistant state is initialized lazily and idempotently."""
    global history, api_client, agent_engine, job_manager
    from brain.paths import migrate_legacy_data_if_needed
    migrate_legacy_data_if_needed()
    initialize_memory()
    if history is None:
        history = load_history()
    if api_client is None:
        api_client = OrangeAPIClient()
    if agent_engine is None:
        agent_engine = AgentEngine(api_client=api_client)
    if job_manager is None:
        job_manager = get_job_manager()


def process_user_turn(
    user_input: str,
    prompt_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """
    Execute one complete user turn through Orange AI's multi-tier assistant engine.
    Safe for both CLI and GUI callers; accepts an optional prompt_fn to avoid blocking on stdin.
    """
    ensure_initialized()
    global history, api_client, agent_engine, job_manager, active_plan

    user = user_input.strip()
    if not user:
        return {"reply": "", "model": "system", "type": "empty"}

    learned = []
    retired = []

    # 1. Automatic Memory Extraction (non-blocking)
    try:
        memories = classify_memory(user)
        for memory in memories:
            action = memory.get("action", "store")
            if action == "retire":
                try:
                    deleted = retire_structured(
                        category=memory["category"],
                        key=memory["key"],
                        value=memory.get("value"),
                    )
                    if deleted > 0:
                        retired.append(memory)
                except Exception as e:
                    logger.error("Failed to retire memory %s/%s: %s", memory["category"], memory["key"], e)
            else:
                try:
                    remember_structured(
                        category=memory["category"],
                        key=memory["key"],
                        value=memory["value"],
                        confidence=memory["confidence"],
                    )
                    learned.append(memory)
                except Exception as e:
                    logger.error("Failed to store memory %s/%s: %s", memory["category"], memory["key"], e)
    except Exception as e:
        logger.error("Memory auto-classification error: %s", e)

    # 2. Mood sentiment nudge
    mood.apply_user_sentiment(user)

    # 3. Direct commands
    lowered = user.lower()

    if lowered == "exit":
        return {"reply": "Goodbye!", "model": "system", "type": "exit", "learned": learned, "retired": retired}

    if lowered == "mood":
        return {"reply": f"I'm {mood.mood_description()}", "model": "system", "type": "mood", "learned": learned, "retired": retired}

    if lowered.startswith("plan "):
        active_plan = planner.create_plan(user[5:])
        return {"reply": planner.format_plan(active_plan), "model": "planner", "type": "plan", "learned": learned, "retired": retired}

    if lowered == "show plan":
        if active_plan is None:
            return {"reply": "There is no active plan.", "model": "planner", "type": "plan", "learned": learned, "retired": retired}
        return {"reply": planner.format_plan(active_plan), "model": "planner", "type": "plan", "learned": learned, "retired": retired}

    if lowered == "run plan":
        if active_plan is None:
            return {"reply": "There is no active plan. Start with 'plan <goal>'.", "model": "planner", "type": "plan", "learned": learned, "retired": retired}
        plan_res = planner.execute_plan(active_plan, lambda intent, cmd: dispatch(intent, cmd, prompt_fn=prompt_fn))
        active_plan = None
        return {"reply": plan_res, "model": "planner", "type": "plan", "learned": learned, "retired": retired}

    if lowered == "cancel plan":
        active_plan = None
        return {"reply": "Active plan cancelled.", "model": "planner", "type": "plan", "learned": learned, "retired": retired}

    if lowered in ("show planner history", "show plan history"):
        return {"reply": planner.recent_outcomes(), "model": "planner", "type": "plan", "learned": learned, "retired": retired}

    # Daily Briefing configuration commands
    if lowered in ("briefing enable", "enable morning briefing", "enable briefing", "briefing on"):
        from brain.briefing.settings import set_morning_briefing_enabled
        set_morning_briefing_enabled(True)
        return {
            "reply": "🌅 Morning briefing enabled. Orange AI will present your daily briefing on first launch each morning.",
            "model": "briefing",
            "type": "briefing",
            "learned": learned,
            "retired": retired,
        }

    if lowered in ("briefing disable", "disable morning briefing", "disable briefing", "briefing off"):
        from brain.briefing.settings import set_morning_briefing_enabled
        set_morning_briefing_enabled(False)
        return {
            "reply": "🌅 Morning briefing disabled. You can still view it anytime with 'briefing'.",
            "model": "briefing",
            "type": "briefing",
            "learned": learned,
            "retired": retired,
        }

    if lowered in ("briefing status", "briefing config", "briefing settings"):
        from brain.briefing.settings import is_morning_briefing_enabled, get_last_briefing_date
        enabled = is_morning_briefing_enabled()
        last_date = get_last_briefing_date() or "Never"
        status_text = "Enabled" if enabled else "Disabled"
        return {
            "reply": f"Daily Briefing Configuration:\n- Morning Briefing: {status_text}\n- Last Delivered Date: {last_date}\n\nUse 'briefing enable' or 'briefing disable' to toggle.",
            "model": "briefing",
            "type": "briefing",
            "learned": learned,
            "retired": retired,
        }

    # Recommendation CLI commands (Stage 3.7E.2D)
    rec_result = handle_recommendation_cli(user)
    if rec_result:
        rec_result["learned"] = learned
        rec_result["retired"] = retired
        return rec_result

    # Job CLI commands
    if lowered.startswith("job"):
        with io.StringIO() as buf, contextlib.redirect_stdout(buf):
            handled = handle_job_cli(user, job_manager)
            job_output = buf.getvalue().strip()
        if handled:
            clean_job = job_output
            if clean_job.startswith("Orange AI:"):
                clean_job = clean_job[len("Orange AI:"):].strip()
            return {"reply": clean_job or "Job command processed.", "model": "job", "type": "job", "learned": learned, "retired": retired}

    # Controlled Agent Loop (Phase 4)
    if lowered.startswith("agent ") or lowered.startswith("task "):
        prefix_len = 6 if lowered.startswith("agent ") else 5
        agent_goal = user[prefix_len:].strip()
        if not agent_goal:
            return {
                "reply": "Please provide a goal, e.g. 'agent check cpu usage and battery status'.",
                "model": "agent",
                "type": "agent",
                "status": "error",
                "learned": learned,
                "retired": retired,
            }

        state = agent_engine.start_run(agent_goal, conversation_history=history)

        while state.status in (AgentStatus.WAITING_CONFIRMATION, AgentStatus.WAITING_USER):
            if state.status == AgentStatus.WAITING_CONFIRMATION:
                tool_name = state.pending_confirmation.tool_name if state.pending_confirmation else "tool"
                args_str = str(state.pending_confirmation.arguments) if state.pending_confirmation and state.pending_confirmation.arguments else ""
                prompt_text = f"Orange AI requests confirmation to execute '{tool_name}' ({args_str}). Proceed? [y/N]: "
                if prompt_fn:
                    conf_input = prompt_fn(prompt_text)
                else:
                    conf_input = input(f"\n⚠️  {prompt_text}")
                state = agent_engine.resume_run(state.run_id, conf_input, conversation_history=history)
            elif state.status == AgentStatus.WAITING_USER:
                prompt_text = f"Orange AI: {state.final_response}\nYou: "
                if prompt_fn:
                    user_reply = prompt_fn(prompt_text)
                else:
                    user_reply = input(f"\n❓ {prompt_text}")
                state = agent_engine.resume_run(state.run_id, user_reply, conversation_history=history)

        result_text = state.final_response or f"Task finished with status: {state.status.value}"
        return {"reply": result_text, "model": "agent", "type": "agent", "status": state.status.value, "learned": learned, "retired": retired}

    # Targeted Memory Retractions (Stage 3.5)
    if lowered.startswith("retire tool ") or lowered.startswith("forget tool "):
        tool_name = user[12:].strip()
        if not tool_name:
            return {"reply": "Please specify the tool to retire, e.g. 'retire tool Docker'.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        try:
            res = retract_entity("tool", tool_name, active_history=history)
            return {"reply": res["message"], "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        except Exception as e:
            logger.error("Error retracting tool '%s': %s", tool_name, e)
            return {"reply": f"An error occurred while retracting tool: {e}", "model": "memory", "type": "error", "learned": learned, "retired": retired}

    if lowered.startswith("forget project "):
        proj_name = user[15:].strip()
        if not proj_name:
            return {"reply": "Please specify the project to forget, e.g. 'forget project Zephyr'.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        try:
            res = retract_entity("project", proj_name, active_history=history)
            return {"reply": res["message"], "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        except Exception as e:
            logger.error("Error forgetting project '%s': %s", proj_name, e)
            return {"reply": f"An error occurred while forgetting project: {e}", "model": "memory", "type": "error", "learned": learned, "retired": retired}

    if lowered.startswith("forget preference "):
        pref_name = user[18:].strip()
        if not pref_name:
            return {"reply": "Please specify the preference to forget, e.g. 'forget preference coffee'.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        try:
            res = retract_entity("preference", pref_name, active_history=history)
            return {"reply": res["message"], "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        except Exception as e:
            logger.error("Error forgetting preference '%s': %s", pref_name, e)
            return {"reply": f"An error occurred while forgetting preference: {e}", "model": "memory", "type": "error", "learned": learned, "retired": retired}

    if lowered.startswith("forget "):
        keyword = user[7:].strip()
        if not keyword:
            return {"reply": "Tell me what to forget, e.g. 'forget old job'.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        if len(keyword) < 3:
            return {"reply": "Keyword must be at least 3 characters long.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        try:
            count = forget(keyword, active_history=history)
            if count:
                return {"reply": f"🗑️ Deleted {count} memory(ies) matching '{keyword}'.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
            return {"reply": f"No memories matched '{keyword}'.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        except Exception as e:
            logger.error("Error forgetting '%s': %s", keyword, e)
            return {"reply": f"An error occurred while forgetting: {e}", "model": "memory", "type": "error", "learned": learned, "retired": retired}

    # Intent routing
    intent = route(user)

    if intent == Intent.REMEMBER:
        memory_text = user[9:].strip()
        if memory_text:
            remember(memory_text)
            return {"reply": "✅ Memory saved.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}
        return {"reply": "Nothing to remember.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}

    if intent == Intent.SHOW_MEMORIES:
        total_legacy = count_legacy()
        legacy = recall(limit=50)
        structured = recall_structured()

        if not legacy and not structured:
            return {"reply": "No memories stored.", "model": "memory", "type": "memory", "learned": learned, "retired": retired}

        lines = ["Stored Memories:\n"]
        if structured:
            lines.append("--- Structured Memories ---")
            current_cat = None
            for item in structured:
                cat = item["category"].upper()
                if cat != current_cat:
                    current_cat = cat
                    lines.append(f"\n[{current_cat}]")
                lines.append(f"  {item['key']}: {item['value']}")
            lines.append("")
        if legacy:
            lines.append("--- Legacy Memories ---")
            for i, m in enumerate(legacy, 1):
                lines.append(f"{i}. {m}")
            if total_legacy > len(legacy):
                lines.append("\nShowing the 50 most recent legacy memories. Older legacy memories are not displayed.")
        return {"reply": "\n".join(lines).strip(), "model": "memory", "type": "memory", "learned": learned, "retired": retired}

    if intent == Intent.SHOW_PROFILE:
        return {"reply": format_profile_inspection(), "model": "memory", "type": "memory", "learned": learned, "retired": retired}

    if intent == Intent.SHOW_PROJECTS:
        return {"reply": format_category_inspection('project'), "model": "memory", "type": "memory", "learned": learned, "retired": retired}

    if intent == Intent.SHOW_TOOLS:
        return {"reply": format_category_inspection('tool'), "model": "memory", "type": "memory", "learned": learned, "retired": retired}

    if intent == Intent.SHOW_PREFERENCES:
        return {"reply": format_category_inspection('preference'), "model": "memory", "type": "memory", "learned": learned, "retired": retired}

    if intent == Intent.DAILY_BRIEFING:
        from brain.briefing import BriefingCollector, BriefingSynthesizer
        from brain.briefing.settings import set_last_briefing_date
        from datetime import datetime

        collector = BriefingCollector(job_store=job_manager.store if job_manager else None)
        ctx = collector.collect()
        synthesizer = BriefingSynthesizer(api_client=api_client)
        briefing = synthesizer.synthesize(ctx)

        # Record last delivered date for deduplication
        today_str = datetime.now().strftime("%Y-%m-%d")
        set_last_briefing_date(today_str)

        return {
            "reply": briefing.summary_markdown,
            "model": briefing.model_name,
            "type": "briefing",
            "is_fallback": briefing.is_fallback,
            "learned": learned,
            "retired": retired,
        }

    # Tool dispatch
    try:
        tool_result = dispatch(intent, user, prompt_fn=prompt_fn)
    except Exception as e:
        mood.apply_event("error")
        logger.error("Tool dispatch failed for intent %s: %s", intent, e)
        return {"reply": f"Something went wrong running that: {e}", "model": "tool", "type": "error", "learned": learned, "retired": retired}

    if tool_result is not None:
        mood.apply_event("success")
        return {"reply": tool_result, "model": "tool", "type": "tool", "learned": learned, "retired": retired}

    # Conversational inference via Orange API (fallback)
    budget_res = budget_chat_memory(
        query=user,
        max_chars=1500,
    )
    profile = budget_res.formatted_profile
    legacy_memories = budget_res.formatted_legacy or "None"

    current_energy = mood.get_energy()
    current_mood_label = mood.mood_label(current_energy)
    mood_block = mood_prompt_addendum(current_mood_label)
    permission_context = (
        "This Orange AI process is currently running with Windows administrator permissions."
        if is_running_as_admin()
        else "This Orange AI process is currently running without Windows administrator permissions."
    )

    verbosity_instruction = (
        "\nKeep this particular reply especially short -- a sentence or "
        "two unless the user is asking for something that genuinely needs "
        "more detail."
        if current_energy <= -0.6
        else ""
    )

    system_prompt = f"""
    {SYSTEM_PROMPT}
    {mood_block}
    {verbosity_instruction}

    ==================================================
    RUNTIME PERMISSIONS
    ==================================================

    {permission_context}
    State this status accurately if the user asks about UAC, administrator
    access, or permissions. Do not claim that UAC changes your programming,
    capabilities, memory, or model training; it only changes this process's
    operating-system permissions.

    ==================================================
    LEGACY MEMORIES
    ==================================================

    {legacy_memories}

    Use the stored user profile and legacy memories naturally in conversation.
    Always trust the user profile over legacy memories if both contain the same information.
    If the user corrects a stored fact, prefer the user's newest statement.
    Never invent personal information that is not stored.
    """

    try:
        result = api_client.assist(
            message=user,
            history=history,
            system_instruction=system_prompt,
            profile=profile,
        )
        reply = result["reply"]
        model_name = result.get("model", "api")
        mood.apply_event("success")
    except OrangeAPIConnectionError as e:
        mood.apply_event("error")
        logger.warning("Orange API connection error: %s", e)
        return {"reply": str(e), "model": "api", "type": "error", "is_connection_error": True, "learned": learned, "retired": retired}
    except OrangeAPIResponseError as e:
        mood.apply_event("error")
        logger.warning("Orange API response error: %s", e)
        return {"reply": str(e), "model": "api", "type": "error", "learned": learned, "retired": retired}
    except Exception as e:
        mood.apply_event("error")
        logger.error("Unexpected error during API call: %s", e)
        return {"reply": "An unexpected error occurred while communicating with Orange API.", "model": "api", "type": "error", "learned": learned, "retired": retired}

    history.append({"role": "user", "content": user})
    history.append({"role": "assistant", "content": reply})
    del history[:-MAX_HISTORY_TURNS]

    # Contextual Chat Recommendation (Stage 3.7E.2D - max 1 per relevant response)
    advisory = _check_contextual_chat_recommendation(user)
    rec_obj = None
    if advisory:
        reply = f"{reply}{advisory}"
        try:
            from brain.briefing.recommendations import RecommendationStore, match_recommendation_relevance, RecommendationState
            store = RecommendationStore()
            for r in store.list_all():
                if r.state == RecommendationState.ACTIVE:
                    matched, _ = match_recommendation_relevance(r, user)
                    if matched:
                        rec_obj = r.to_dict()
                        break
        except Exception:
            pass

    save_turn("user", user)
    save_turn("assistant", reply)

    outcome = {"reply": reply, "model": model_name, "type": "assistant", "learned": learned, "retired": retired}
    if rec_obj:
        outcome["recommendation"] = rec_obj
    return outcome


def deliver_morning_briefing_if_needed(
    client: Optional[OrangeAPIClient] = None,
    manager: Optional[JobManager] = None,
    output_fn: Optional[Callable[[str], Any]] = None,
    auto_mark_delivered: bool = True,
) -> bool:
    """
    Check and deliver proactive morning briefing if enabled and not already delivered today.
    Marks delivery date ONLY after successful synthesis when auto_mark_delivered=True.
    When auto_mark_delivered=False, date marking is managed by caller's confirmed presentation.
    Returns True if a morning briefing was successfully synthesized and delivered; False otherwise.
    """
    try:
        from brain.briefing.settings import should_deliver_morning_briefing, set_last_briefing_date
        from brain.briefing import BriefingCollector, BriefingSynthesizer
        from datetime import datetime

        if not should_deliver_morning_briefing():
            return False

        today_str = datetime.now().strftime("%Y-%m-%d")
        collector = BriefingCollector(job_store=manager.store if manager else None)
        ctx = collector.collect()
        synth = BriefingSynthesizer(api_client=client)
        briefing = synth.synthesize(ctx, timeout=2.5)

        # Reject missing or empty briefing output
        if not briefing or not briefing.summary_markdown or not briefing.summary_markdown.strip():
            logger.warning("Morning briefing synthesis returned missing or empty content; delivery aborted.")
            return False

        # Mark delivery date only after successful synthesis when auto_mark_delivered is True (CLI mode default)
        if auto_mark_delivered:
            set_last_briefing_date(today_str)

        out = output_fn or print
        try:
            out(f"\n🌅 Orange AI [Morning Briefing]:\n\n{briefing.summary_markdown}\n" + "-" * 50)
        except Exception as e:
            logger.warning("Error rendering morning briefing output: %s", e)
            if not auto_mark_delivered:
                return False

        return True
    except Exception as e:
        logger.warning("Morning briefing startup check failed: %s", e)
        return False


def run_cli():
    global active_plan, history, api_client, agent_engine, job_store, job_runtime, job_manager

    ensure_initialized()

    # Step 4: Crash recovery MUST execute before scheduler starts
    recovered_jobs = job_manager.recover_interrupted()

    # Start background scheduler thread after recovery has completed
    scheduler.start(
        on_reminder_due=_on_reminder_due,
        on_job_event=_on_job_event,
        job_manager=job_manager,
    )
    # Runs as a daemon thread (see brain/scheduler.py) -- it stops automatically
    # when the main process exits, no explicit shutdown needed on "exit".

    print("=" * 50)
    print("🍊 Orange AI Initialized")
    print(greeting())
    print(
        "Administrator permissions: enabled"
        if is_running_as_admin()
        else "Administrator permissions: not enabled"
    )
    if api_client.is_available():
        print(f"🔗 Orange API: connected ({api_client.api_url})")
    else:
        print(f"⚠️  Orange API: offline ({api_client.api_url})")
        print("   (start Orange API to enable conversational AI replies)")

    if recovered_jobs:
        print(f"🔧 Startup Recovery: Recovered {len(recovered_jobs)} interrupted job(s)")
        for rj in recovered_jobs:
            print(f"   - [{rj.short_id}] '{rj.goal[:35]}' -> {rj.status.value}")
    print("=" * 50)
    print("Type 'exit' to quit")
    print("Type 'remember <fact>' to save memory")
    print("Type 'forget <keyword>' to delete matching memories")
    print("Type 'show memories' to view memories")
    print("Type 'show profile' to view your profile")
    print("Type 'show projects' to view active projects")
    print("Type 'show tools' to view active tools")
    print("Type 'show preferences' to view preferences")
    print("Type 'retire tool <name>' to retire a tool")
    print("Type 'forget project <name>' to remove a project")
    print("Type 'forget preference <name>' to remove a preference")
    print("Type 'remind me to <task> at <time>' to set a reminder")
    print("Type 'show reminders' to view pending reminders")
    print("Type 'cancel reminder <number>' to cancel a reminder")
    print("Type 'mood' to check Orange AI's current simulated mood")
    print("Type 'plan <goal>' to create a multi-step command plan")
    print("Type 'run plan' to execute the current plan")
    print("Type 'agent <goal>' or 'task <goal>' to run an ephemeral agent task")
    print("Type 'job <goal>' to run a persistent autonomous job")
    print("Type 'job list' to view persistent jobs")
    print("Type 'job status <id>' to view job details")
    print("Type 'briefing' to view your daily briefing")
    print("Type 'briefing enable' or 'briefing disable' to toggle morning briefings")
    print("Type 'rec list' to view contextual recommendations")
    print("=" * 50)

    # Proactive Morning Briefing (User-controlled, non-blocking, once per day)
    deliver_morning_briefing_if_needed(client=api_client, manager=job_manager)

    try:
        while True:
            user = input("\nYou: ").strip()
            if not user:
                continue

            outcome = process_user_turn(user, prompt_fn=None)

            for m in outcome.get("learned", []):
                print(f"🧠 Learned: {m['category']} -> {m['key']} = {m['value']}")
            for m in outcome.get("retired", []):
                print(f"🧠 Retired: {m['category']} -> {m['key']} ({m['value']})")

            msg_type = outcome.get("type")
            reply = outcome.get("reply", "")
            model_name = outcome.get("model", "")

            if msg_type == "exit":
                print("\nOrange AI: Goodbye!")
                break
            elif msg_type == "agent":
                print(f"\nOrange AI [Agent]: {reply}")
            elif msg_type == "assistant":
                provider_tag = f"[{model_name}]" if model_name and model_name != "unknown" else "☁️"
                print(f"\nOrange AI {provider_tag}:", reply)
            elif msg_type == "briefing":
                print(f"\nOrange AI [Daily Briefing]:\n\n{reply}")
            else:
                if reply:
                    print(f"\nOrange AI: {reply}")
    finally:
        scheduler.stop(wait_timeout=3.0)


if __name__ == "__main__":
    if "--cli" in sys.argv:
        run_cli()
    else:
        from gui.app import launch_gui
        launch_gui()
