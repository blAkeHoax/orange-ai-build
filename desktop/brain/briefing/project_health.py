"""
brain/briefing/project_health.py - Deterministic Project Health Evaluator.

Stage 3.7E.2A: Reconstructs the operational health and execution status of
projects on demand using existing authoritative SQLite records:
- `structured_memories`
- `jobs`
- `job_steps`

All operations are strictly read-only and deterministic; zero mutations or
persistent health caches are created. LLMs are never permitted to guess,
hallucinate progress, or infer project completion.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set

from brain.briefing.models import ProjectHealthStatus, ProjectHealthAssessment
from brain.briefing.collector import (
    sanitize_briefing_text,
    _is_sensitive_personal_goal,
)
from brain.conflict_resolver import is_session_retired
from brain.jobs.models import Job, JobStep, JobStatus
from brain.jobs.store import JobStore
from brain.memory import get_db_path

logger = logging.getLogger(__name__)

# Constants for evaluation thresholds
PROGRESS_RECENCY_HOURS = 48
FAILURE_RECENCY_HOURS = 48
STUCK_RUNNING_JOB_HOURS = 24
INACTIVITY_THRESHOLD_DAYS = 5
CLOCK_SKEW_TOLERANCE_MINUTES = 5


def parse_timestamp(ts_str: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO timestamp from database columns into a naive local datetime.
    
    Normalizes timezone-aware timestamps (e.g. UTC ISO from `jobs` and `job_steps`)
    to local time by converting to local timezone and stripping tzinfo.
    Preserves local naive timestamps (from `structured_memories` and `reminders`).
    Rejects None, empty strings, and malformed strings safely without throwing.
    """
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is not None:
            # Convert UTC-aware (or other offset) to local time, then make naive
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def match_job_to_projects(
    job: Job,
    projects: List[Tuple[str, bool]],  # list of (project_name, is_current)
) -> Optional[str]:
    """
    Determine if a job belongs to a specific project using conservative matching:
    1. Explicit project tag in job.tags (e.g. 'project:<name>' or exact '<name>').
    2. Conservative case-insensitive word-boundary matching on job.goal.
    3. Rejects ambiguous matches that match multiple distinct projects.
    4. Unmatched jobs return None (unassociated).
    """
    if not projects:
        return None

    # Precedence 1: Explicit project tag in job.tags
    if job.tags:
        raw_tags = re.split(r"[,;]", job.tags)
        explicit_tags = []
        for raw in raw_tags:
            t = raw.strip()
            if not t:
                continue
            m = re.match(r"^project\s*[:=]\s*(.+)$", t, re.IGNORECASE)
            if m:
                val = m.group(1).strip().strip("'\"")
                if val:
                    explicit_tags.append(val.lower())
            else:
                explicit_tags.append(t.lower())

        if explicit_tags:
            tag_matches = []
            for p_name, _ in projects:
                clean_p = p_name.strip().lower()
                if clean_p in explicit_tags:
                    tag_matches.append(p_name)
            if len(tag_matches) == 1:
                return tag_matches[0]
            elif len(tag_matches) > 1:
                # Ambiguous multiple distinct projects matched: reject
                return None

    # Precedence 2: Conservative case-insensitive word-boundary matching in job.goal
    clean_goal = job.goal or ""
    goal_matches = []
    for p_name, _ in projects:
        clean_name = p_name.strip()
        if not clean_name:
            continue
        # Strip trailing punctuation for safe word boundary construction
        norm_name = re.sub(r"\(.*?\)", "", clean_name).strip().rstrip(". ")
        if not norm_name:
            continue
        pattern = re.compile(rf"\b{re.escape(norm_name)}\b", re.IGNORECASE)
        if pattern.search(clean_goal):
            goal_matches.append(p_name)

    if len(goal_matches) == 1:
        return goal_matches[0]
    elif len(goal_matches) > 1:
        # Check if one project name is a specific superset of another (e.g. "Orange AI" vs "Orange")
        sorted_matches = sorted(goal_matches, key=lambda p: len(p.strip()), reverse=True)
        longest = sorted_matches[0]
        if all(m.strip().lower() in longest.strip().lower() for m in sorted_matches[1:]):
            return longest
        else:
            # Truly ambiguous multiple projects mentioned: reject
            return None

    return None


def evaluate_project_health(
    project_name: str,
    is_current: bool,
    associated_jobs: List[Job],
    steps_by_job: Optional[Dict[str, List[JobStep]]] = None,
    now: Optional[datetime] = None,
    explicit_completion: bool = False,
    project_updated_at: Optional[str] = None,
) -> ProjectHealthAssessment:
    """
    Pure, deterministic evaluation function for a single project.
    
    Accepts explicit records to enable 100% isolated synthetic unit testing
    without SQLite mutations or network dependencies.
    """
    local_now = now or datetime.now()
    steps_map = steps_by_job or {}
    evidence: List[str] = []
    job_ids = [j.short_id for j in associated_jobs]

    # Calculate status counts
    status_counts: Dict[str, int] = {}
    for j in associated_jobs:
        status_name = j.status.value if hasattr(j.status, "value") else str(j.status)
        status_counts[status_name] = status_counts.get(status_name, 0) + 1

    # -------------------------------------------------------------
    # STEP 1: Explicit Authoritative Project Completion
    # -------------------------------------------------------------
    if explicit_completion:
        evidence.append("Explicit authoritative completion record verified.")
        return ProjectHealthAssessment(
            project_name=project_name,
            is_current=is_current,
            status=ProjectHealthStatus.COMPLETED,
            explanation=f"Project '{project_name}' has been explicitly verified as completed.",
            associated_job_ids=job_ids,
            job_status_counts=status_counts,
            evidence=evidence,
            confidence="HIGH",
        )

    # Inspect all associated steps for progress timestamps
    latest_progress_dt: Optional[datetime] = None
    latest_progress_str: Optional[str] = None
    has_recent_progress = False

    recent_cutoff = local_now - timedelta(hours=PROGRESS_RECENCY_HOURS)
    skew_cutoff = local_now + timedelta(minutes=CLOCK_SKEW_TOLERANCE_MINUTES)

    for j in associated_jobs:
        steps = steps_map.get(j.id, [])
        for s in steps:
            if s.success is True:
                # Crucial safeguard: completed_at is routinely None; inspect started_at
                step_dt = parse_timestamp(s.started_at)
                if step_dt:
                    # Ignore future timestamps beyond clock skew tolerance
                    if step_dt > skew_cutoff:
                        continue
                    if latest_progress_dt is None or step_dt > latest_progress_dt:
                        latest_progress_dt = step_dt
                        latest_progress_str = s.started_at
                    if step_dt >= recent_cutoff:
                        has_recent_progress = True

    # Check completed jobs as verified progress events
    for j in associated_jobs:
        if j.status == JobStatus.COMPLETED and j.completed_at:
            comp_dt = parse_timestamp(j.completed_at)
            if comp_dt and comp_dt <= skew_cutoff:
                if latest_progress_dt is None or comp_dt > latest_progress_dt:
                    latest_progress_dt = comp_dt
                    latest_progress_str = j.completed_at
                if comp_dt >= recent_cutoff:
                    has_recent_progress = True

    # Stage 5D: Check Git repository commit timestamps for verified progress
    try:
        from tools.permissions import DEFAULT_ACTIVE_WORKSPACES
        import subprocess
        for ws in DEFAULT_ACTIVE_WORKSPACES:
            p_name = Path(ws).name.lower()
            if p_name in project_name.lower() or project_name.lower() in p_name:
                res = subprocess.run(
                    ["git", "-C", ws, "log", "-1", "--format=%cI"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                if res.returncode == 0 and res.stdout.strip():
                    git_dt = parse_timestamp(res.stdout.strip())
                    if git_dt and git_dt <= skew_cutoff:
                        if latest_progress_dt is None or git_dt > latest_progress_dt:
                            latest_progress_dt = git_dt
                            latest_progress_str = res.stdout.strip()
                        if git_dt >= recent_cutoff:
                            has_recent_progress = True
    except Exception:
        pass

    # -------------------------------------------------------------
    # Running Job Warning Check (> 24h with no progress)
    # -------------------------------------------------------------
    has_stuck_running_job = False
    stuck_warning_hours = local_now - timedelta(hours=STUCK_RUNNING_JOB_HOURS)
    for j in associated_jobs:
        if j.status == JobStatus.RUNNING:
            # Check latest step in current attempt or job updated_at
            job_steps = steps_map.get(j.id, [])
            last_activity_dt = None
            if job_steps:
                last_activity_dt = parse_timestamp(job_steps[-1].started_at)
            if not last_activity_dt:
                last_activity_dt = parse_timestamp(j.updated_at) or parse_timestamp(j.started_at)
            
            if last_activity_dt and last_activity_dt < stuck_warning_hours:
                has_stuck_running_job = True
                evidence.append(
                    f"Warning: Running job [{j.short_id}] has no progress recorded for more than {STUCK_RUNNING_JOB_HOURS} hours."
                )

    # -------------------------------------------------------------
    # STEP 2: BLOCKED Conditions (WAITING_CONFIRMATION & Unsuperseded Failures)
    # -------------------------------------------------------------
    waiting_jobs = [j for j in associated_jobs if j.status == JobStatus.WAITING_CONFIRMATION]
    active_jobs = [j for j in associated_jobs if j.status in (JobStatus.RUNNING, JobStatus.RETRYING)]
    paused_jobs = [j for j in associated_jobs if j.status == JobStatus.PAUSED]
    failed_jobs = [j for j in associated_jobs if j.status == JobStatus.FAILED]

    if waiting_jobs:
        first_wait = waiting_jobs[0]
        pending_act = first_wait.get_pending_confirmation()
        tool_desc = f"action '{pending_act.tool_name}'" if pending_act else "action"

        if has_recent_progress or active_jobs:
            explanation = (
                f"One associated job [{first_wait.short_id}] is waiting for your confirmation for {tool_desc}; "
                f"another job is progressing."
            )
            evidence.append(f"Partial blockage: Job [{first_wait.short_id}] WAITING_CONFIRMATION; concurrent work active.")
        else:
            explanation = f"Associated job [{first_wait.short_id}] requires confirmation before proceeding with {tool_desc}."
            evidence.append(f"Job [{first_wait.short_id}] is WAITING_CONFIRMATION.")

        return ProjectHealthAssessment(
            project_name=project_name,
            is_current=is_current,
            status=ProjectHealthStatus.BLOCKED,
            explanation=sanitize_briefing_text(explanation),
            last_progress_at=latest_progress_str,
            associated_job_ids=job_ids,
            job_status_counts=status_counts,
            has_stuck_running_job=has_stuck_running_job,
            evidence=evidence,
            confidence="HIGH",
        )

    # Check recent un-superseded FAILED jobs (< 48h)
    failure_cutoff = local_now - timedelta(hours=FAILURE_RECENCY_HOURS)
    recent_failures = []
    for fj in failed_jobs:
        f_dt = parse_timestamp(fj.completed_at) or parse_timestamp(fj.updated_at)
        if f_dt and f_dt >= failure_cutoff:
            recent_failures.append((fj, f_dt))

    if recent_failures:
        # Sort by recency DESC
        recent_failures.sort(key=lambda item: item[1], reverse=True)
        most_recent_failed, fail_dt = recent_failures[0]
        
        # Check if failure is superseded by newer active/progressing work
        is_superseded = False
        if has_recent_progress and latest_progress_dt and latest_progress_dt > fail_dt:
            is_superseded = True
            evidence.append(f"Historical failure in [{most_recent_failed.short_id}] was superseded by subsequent progress.")
        elif active_jobs:
            # Active job currently running after failure
            for aj in active_jobs:
                a_dt = parse_timestamp(aj.updated_at) or parse_timestamp(aj.started_at)
                if a_dt and a_dt >= fail_dt:
                    is_superseded = True
                    evidence.append(f"Failure in [{most_recent_failed.short_id}] is superseded by active job [{aj.short_id}].")
                    break

        if not is_superseded:
            err_msg = most_recent_failed.last_error or "Unknown failure"
            clean_err = sanitize_briefing_text(err_msg)
            evidence.append(f"Job [{most_recent_failed.short_id}] failed within {FAILURE_RECENCY_HOURS}h: {clean_err}")
            return ProjectHealthAssessment(
                project_name=project_name,
                is_current=is_current,
                status=ProjectHealthStatus.BLOCKED,
                explanation=f"Associated job [{most_recent_failed.short_id}] failed within the last 48 hours: {clean_err}",
                last_progress_at=latest_progress_str,
                associated_job_ids=job_ids,
                job_status_counts=status_counts,
                recent_failure_info=clean_err,
                has_stuck_running_job=has_stuck_running_job,
                evidence=evidence,
                confidence="HIGH",
            )

    # -------------------------------------------------------------
    # STEP 3: PROGRESSING (Verified recent progress within 48 hours)
    # -------------------------------------------------------------
    if has_recent_progress:
        evidence.append(f"Verified successful execution step recorded within last {PROGRESS_RECENCY_HOURS} hours.")
        return ProjectHealthAssessment(
            project_name=project_name,
            is_current=is_current,
            status=ProjectHealthStatus.PROGRESSING,
            explanation=f"Work on '{project_name}' is actively progressing with verified execution within the last 48 hours.",
            last_progress_at=latest_progress_str,
            associated_job_ids=job_ids,
            job_status_counts=status_counts,
            has_stuck_running_job=has_stuck_running_job,
            evidence=evidence,
            confidence="HIGH",
        )

    # -------------------------------------------------------------
    # STEP 4: PAUSED (Explicit paused work with no higher-priority blocker)
    # -------------------------------------------------------------
    if paused_jobs and not active_jobs:
        first_paused = paused_jobs[0]
        evidence.append(f"Associated job [{first_paused.short_id}] is explicitly PAUSED at step boundary.")
        return ProjectHealthAssessment(
            project_name=project_name,
            is_current=is_current,
            status=ProjectHealthStatus.PAUSED,
            explanation=f"Work on associated job [{first_paused.short_id}] is intentionally paused at a step boundary.",
            last_progress_at=latest_progress_str,
            associated_job_ids=job_ids,
            job_status_counts=status_counts,
            has_stuck_running_job=has_stuck_running_job,
            evidence=evidence,
            confidence="HIGH",
        )

    # -------------------------------------------------------------
    # STEP 5: All Associated Jobs Completed Safeguard -> UNKNOWN
    # -------------------------------------------------------------
    completed_jobs = [j for j in associated_jobs if j.status == JobStatus.COMPLETED]
    if associated_jobs and len(completed_jobs) == len(associated_jobs):
        count = len(completed_jobs)
        job_word = "job" if count == 1 else "jobs"
        explanation = (
            f"All {count} associated autonomous {job_word} are completed; "
            f"overall project completion has not been verified."
        )
        evidence.append(f"All {count} associated jobs completed successfully, but overall project completion is unverified.")
        return ProjectHealthAssessment(
            project_name=project_name,
            is_current=is_current,
            status=ProjectHealthStatus.UNKNOWN,
            explanation=explanation,
            last_progress_at=latest_progress_str,
            associated_job_ids=job_ids,
            job_status_counts=status_counts,
            evidence=evidence,
            confidence="MEDIUM",
        )

    # -------------------------------------------------------------
    # STEP 6: ACTIVE_NO_RECENT_PROGRESS (5-Day Inactivity Threshold)
    # -------------------------------------------------------------
    inactivity_cutoff = local_now - timedelta(days=INACTIVITY_THRESHOLD_DAYS)
    
    # Qualification:
    # - Current project qualifies if is_current is True and no progress in 5 days.
    # - Secondary project qualifies ONLY if there is evidence of active work (associated nonterminal jobs).
    nonterminal_jobs = [j for j in associated_jobs if j.status in (JobStatus.RUNNING, JobStatus.RETRYING, JobStatus.PENDING, JobStatus.PAUSED)]
    has_nonterminal_jobs = bool(nonterminal_jobs)
    
    qualifies_for_inactivity = False
    if is_current:
        # Check if project itself or its jobs had progress in 5 days
        if latest_progress_dt is None or latest_progress_dt < inactivity_cutoff:
            qualifies_for_inactivity = True
    elif has_nonterminal_jobs:
        if latest_progress_dt is None or latest_progress_dt < inactivity_cutoff:
            qualifies_for_inactivity = True

    if qualifies_for_inactivity:
        scope_desc = "Current primary project" if is_current else "Secondary project with active work"
        evidence.append(f"No verified progress recorded in the last {INACTIVITY_THRESHOLD_DAYS} days.")
        return ProjectHealthAssessment(
            project_name=project_name,
            is_current=is_current,
            status=ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS,
            explanation=f"{scope_desc} has no verified progress recorded in the last {INACTIVITY_THRESHOLD_DAYS} days.",
            last_progress_at=latest_progress_str,
            associated_job_ids=job_ids,
            job_status_counts=status_counts,
            has_stuck_running_job=has_stuck_running_job,
            evidence=evidence,
            confidence="MEDIUM",
        )

    # -------------------------------------------------------------
    # STEP 7: UNKNOWN (Insufficient or Contradictory Evidence)
    # -------------------------------------------------------------
    if not associated_jobs:
        explanation = f"No autonomous jobs or progress records are associated with project '{project_name}'."
        evidence.append("No associated job records found in database.")
    else:
        explanation = f"Insufficient or ambiguous recorded evidence to determine health for project '{project_name}'."
        evidence.append("Associated jobs present, but status criteria cannot be conclusively resolved.")

    return ProjectHealthAssessment(
        project_name=project_name,
        is_current=is_current,
        status=ProjectHealthStatus.UNKNOWN,
        explanation=explanation,
        last_progress_at=latest_progress_str,
        associated_job_ids=job_ids,
        job_status_counts=status_counts,
        has_stuck_running_job=has_stuck_running_job,
        evidence=evidence,
        confidence="INSUFFICIENT",
    )


class ProjectHealthEvaluator:
    """
    Coordinator that reconstructs project health dynamically from existing
    authoritative SQLite records (Option A: Existing-Data Reconstruction).
    
    Opens strictly read-only database connections and performs zero mutations.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        job_store: Optional[JobStore] = None,
    ):
        self.db_path = db_path or get_db_path()
        self.job_store = job_store or JobStore(db_path=self.db_path)

    def _get_ro_connection(self) -> sqlite3.Connection:
        """Open a strictly read-only connection to the database."""
        if not os.path.exists(self.db_path):
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        uri_path = self.db_path.replace("\\", "/")
        if not uri_path.startswith("/"):
            uri_path = "/" + uri_path
        uri = f"file://{uri_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _collect_projects(self) -> List[Tuple[str, bool, Optional[str]]]:
        """
        Extract active project entities from structured_memories.
        Applies session-retraction and forgetting safeguards.
        Returns list of tuples: (project_name, is_current, updated_at).
        """
        projects: List[Tuple[str, bool, Optional[str]]] = []
        conn = self._get_ro_connection()
        seen_names: Set[str] = set()

        try:
            cur = conn.execute(
                "SELECT memory_key, value, updated_at FROM structured_memories "
                "WHERE category = 'project' ORDER BY updated_at DESC"
            )
            for r in cur.fetchall():
                key = (r["memory_key"] or "").strip()
                val = (r["value"] or "").strip()
                up_at = r["updated_at"]

                # Privacy & Forgetting Safeguard
                if is_session_retired(val, "project") or is_session_retired(key, "project"):
                    continue

                clean_name = re.sub(r"\(.*?\)", "", val).strip().rstrip(". ")
                if not clean_name:
                    continue

                norm_name = clean_name.lower()
                if norm_name in seen_names:
                    continue
                seen_names.add(norm_name)

                is_curr = (key == "current")
                projects.append((clean_name, is_curr, up_at))
        except Exception as e:
            logger.warning("[PROJECT HEALTH] Error extracting projects: %s", e)
        finally:
            conn.close()

        return projects

    def evaluate_all(self, now: Optional[datetime] = None) -> List[ProjectHealthAssessment]:
        """
        Evaluate project health for all active projects discovered in memory.
        Also identifies unassociated active jobs as general system work.
        """
        local_now = now or datetime.now()
        raw_projects = self._collect_projects()
        if not raw_projects:
            return []

        # Fetch non-retired jobs
        all_jobs = []
        try:
            raw_jobs = self.job_store.list_jobs(limit=200)
            for j in raw_jobs:
                if is_session_retired(j.goal, "job") or is_session_retired(j.goal):
                    continue
                all_jobs.append(j)
        except Exception as e:
            logger.warning("[PROJECT HEALTH] Error listing jobs: %s", e)

        # Map jobs to projects
        project_tuples = [(p[0], p[1]) for p in raw_projects]
        jobs_by_project: Dict[str, List[Job]] = {p[0]: [] for p in raw_projects}
        unassociated_jobs: List[Job] = []

        for j in all_jobs:
            matched_name = match_job_to_projects(j, project_tuples)
            if matched_name and matched_name in jobs_by_project:
                jobs_by_project[matched_name].append(j)
            else:
                unassociated_jobs.append(j)

        # Fetch steps for associated jobs
        steps_by_job: Dict[str, List[JobStep]] = {}
        for p_name, p_jobs in jobs_by_project.items():
            for j in p_jobs:
                try:
                    steps = self.job_store.get_steps(j.id)
                    steps_by_job[j.id] = steps
                except Exception as e:
                    logger.warning("[PROJECT HEALTH] Error fetching steps for job %s: %s", j.id, e)

        # Run pure evaluation for each project
        assessments: List[ProjectHealthAssessment] = []
        for p_name, is_curr, up_at in raw_projects:
            p_jobs = jobs_by_project.get(p_name, [])
            assessment = evaluate_project_health(
                project_name=p_name,
                is_current=is_curr,
                associated_jobs=p_jobs,
                steps_by_job=steps_by_job,
                now=local_now,
                explicit_completion=False,  # Unreachable without dedicated schema
                project_updated_at=up_at,
            )
            assessments.append(assessment)

        return assessments

    def evaluate_project(
        self,
        project_name: str,
        now: Optional[datetime] = None,
    ) -> Optional[ProjectHealthAssessment]:
        """Evaluate a specific project by name."""
        all_assessments = self.evaluate_all(now=now)
        clean_target = project_name.strip().lower()
        for a in all_assessments:
            if a.project_name.strip().lower() == clean_target:
                return a
        return None

    def get_unassociated_active_jobs(self) -> List[Job]:
        """
        Return active jobs (RUNNING, RETRYING, PENDING, WAITING_CONFIRMATION)
        that are not associated with any known project (General System Work).
        """
        raw_projects = self._collect_projects()
        project_tuples = [(p[0], p[1]) for p in raw_projects]
        unassociated: List[Job] = []

        try:
            raw_jobs = self.job_store.list_jobs(limit=200)
            for j in raw_jobs:
                if is_session_retired(j.goal, "job") or is_session_retired(j.goal):
                    continue
                if j.status in (JobStatus.RUNNING, JobStatus.RETRYING, JobStatus.PENDING, JobStatus.WAITING_CONFIRMATION):
                    matched = match_job_to_projects(j, project_tuples)
                    if not matched:
                        unassociated.append(j)
        except Exception as e:
            logger.warning("[PROJECT HEALTH] Error finding unassociated jobs: %s", e)

        return unassociated
