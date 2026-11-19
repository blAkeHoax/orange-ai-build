"""
brain/briefing/collector.py - Read-Only Operational Context Collector.

Stage 3.7B: Discovers, aggregates, filters, and prioritizes operational context
from Memory 2.0, the Persistent Job Engine, Reminders, and System Vitals.
Strictly read-only; performs zero mutations on existing databases or memory.
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

from brain.briefing.models import (
    PriorityLevel,
    PriorityItem,
    ProjectBriefingContext,
    ReminderBriefingContext,
    JobBriefingContext,
    DailyBriefingContext,
)
from brain.conflict_resolver import is_session_retired
from brain.jobs.models import JobStatus, Job
from brain.jobs.store import JobStore
from brain.memory import get_db_path

logger = logging.getLogger(__name__)

# Personal / sensitive keywords to exclude from project daily briefings
_PERSONAL_TERMS_PATTERN = re.compile(
    r"\b(?:marry|marriage|wedding|girlfriend|boyfriend|spouse|wife|husband|dating|romantic)\b",
    re.IGNORECASE,
)

_TECHNICAL_RELATIONSHIP_PATTERN = re.compile(
    r"\b(?:entity[- ]relationship|database\s+relationship|table\s+relationship|data\s+relationship|"
    r"linear\s+relationship|statistical\s+relationship|relationship\s+between\s+(?:tables|entities|data|variables|columns|models|nodes|clusters)|"
    r"foreign\s+key\s+relationship|many-to-many|one-to-many|one-to-one)\b",
    re.IGNORECASE,
)

_PERSONAL_RELATIONSHIP_PATTERN = re.compile(
    r"\b(?:romantic|personal|family|dating|love|healthy|toxic)\s+relationship\b|"
    r"\b(?:my|our|in\s+a)\s+relationship\b|"
    r"\brelationship\s+(?:with|goals|status|advice|issues|problems)\b",
    re.IGNORECASE,
)


def _is_sensitive_personal_goal(text: str) -> bool:
    """
    Check if text contains sensitive personal or romantic content.
    Returns True if the goal should be excluded from project briefing context.
    Conservative privacy protection while safely preserving legitimate technical
    relationship contexts (e.g. entity-relationship diagrams, database relationships).
    """
    if not text:
        return False
    # 1. Unambiguous personal/romantic terms
    if _PERSONAL_TERMS_PATTERN.search(text):
        return True
    # 2. Explicit technical relationship terms
    if _TECHNICAL_RELATIONSHIP_PATTERN.search(text):
        return False
    # 3. Explicit personal relationship patterns
    if _PERSONAL_RELATIONSHIP_PATTERN.search(text):
        return True
    # 4. Standalone 'relationship': check if accompanied by personal terms
    if re.search(r"\brelationship\b", text, re.IGNORECASE):
        if re.search(r"\b(?:her|him|partner|love|couple|breakup|dating|ex)\b", text, re.IGNORECASE):
            return True
        # If no technical indicators are present, be conservative and treat as personal
        return True
    return False


def sanitize_briefing_text(text: Optional[str]) -> str:
    """
    Sanitize sensitive credentials, API keys, tokens, and passwords from briefing text.
    Preserves surrounding sentence context and ordinary words while redacting secrets.
    Does not modify database records or configuration.
    """
    if not text:
        return ""
    sanitized = text

    # 1. Redact Bearer authorization tokens
    sanitized = re.sub(
        r"(?i)\b(Bearer\s+)[A-Za-z0-9_\-\.=/+]+",
        r"\g<1>[REDACTED]",
        sanitized,
    )

    # 2. Redact well-known API key patterns (sk-..., ghp_..., AKIA..., AIza...)
    sanitized = re.sub(
        r"\b(?:sk|pk|ghp|gho|glpat|AKIA|AIza)[A-Za-z0-9_\-]{8,}\b",
        "[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(
        r"\bsk-[A-Za-z0-9_\-]{8,}\b",
        "[REDACTED]",
        sanitized,
    )

    # 3. Redact credential assignments: key=value, password: value, token=value
    sanitized = re.sub(
        r'(?i)\b(api[_-]?key|token|password|passwd|secret|access[_-]?token|client[_-]?secret)\s*([:=])\s*([^\s,;"]+)',
        r"\g<1>\g<2>[REDACTED]",
        sanitized,
    )

    return sanitized


# Technical / project goal relevance indicator pattern
_PROJECT_GOAL_RELEVANCE_PATTERN = re.compile(
    r"\b(?:project|code|build|release|app|launch|develop|study|learn|finish|deploy|test|work|career|"
    r"publish|complete|deliver|stage|milestone|feature|fix|diagram|schema|architecture|database|"
    r"data|analyze|analysis|model|modeling|implement|implementation|design|refactor|optimize|query)\b",
    re.IGNORECASE,
)


def _parse_timestamp(ts_str: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO timestamp from database columns.
    Documented DB assumptions:
    - reminders.due_at / memories: Naive local ISO timestamps (e.g. 2026-10-30T18:00:00).
    - jobs.completed_at / created_at: Timezone-aware UTC ISO timestamps (e.g. 2026-10-30T10:00:00+00:00).
    Converts any timezone-aware timestamp to local naive datetime for consistent local comparisons.
    """
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is not None:
            # Convert to local time, then strip tzinfo for consistent comparison with datetime.now()
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def is_default_db_path(db_path: Optional[str]) -> bool:
    """
    Check if a provided db_path corresponds to the default production database.
    Normalizes path casing, separators, and relative paths for consistent cross-platform matching.
    Matches against both canonical project root database and active get_db_path().
    """
    if db_path is None:
        return True
    try:
        from brain.paths import get_project_root
        norm_given = os.path.normcase(os.path.abspath(os.path.normpath(str(db_path))))
        norm_canonical = os.path.normcase(os.path.abspath(os.path.normpath(os.path.join(get_project_root(), "memory.db"))))
        norm_active = os.path.normcase(os.path.abspath(os.path.normpath(str(get_db_path()))))
        return norm_given in (norm_canonical, norm_active)
    except Exception:
        return False


class BriefingCollector:
    """
    Collects and prioritizes operational data for a context-aware daily briefing.
    All operations are strictly read-only.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        job_store: Optional[JobStore] = None,
        system_checker: Optional[Any] = None,
        recommendation_store: Optional[Any] = None,
    ):
        self.db_path = db_path or get_db_path()
        self.job_store = job_store or JobStore(db_path=self.db_path)
        self.system_checker = system_checker
        self.recommendation_store = recommendation_store

        # Storage Isolation Safeguard (Stage 3.7E.5A-R / 3.7E.5A-R.1):
        # When a non-default database path is supplied (directly or via job_store),
        # an explicit recommendation store must also be provided.
        # Both paths are evaluated independently; silent fallback to the production
        # recommendation store is prohibited.
        is_custom_db = False
        if db_path is not None:
            if not is_default_db_path(db_path):
                is_custom_db = True

        if job_store is not None:
            job_db_path = getattr(job_store, "db_path", None)
            if job_db_path and not is_default_db_path(job_db_path):
                is_custom_db = True

        if is_custom_db and self.recommendation_store is None:
            raise ValueError(
                "BriefingCollector initialized with a non-default database but no explicit "
                "recommendation_store was provided. Silent fallback to the production "
                "recommendation store is prohibited to prevent test or temporary data pollution."
            )

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

    def collect(self, now: Optional[datetime] = None) -> DailyBriefingContext:
        """
        Execute read-only collection and return a complete DailyBriefingContext.
        """
        local_now = now or datetime.now()
        start_of_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_today = local_now.replace(hour=23, minute=59, second=59, microsecond=999999)
        rolling_24h = local_now - timedelta(hours=24)

        # 1. User Identity & Projects
        user_name, current_project, secondary_projects = self._collect_identity_and_projects()

        # 2. Filtered Goals
        relevant_goals = self._collect_relevant_goals(current_project, secondary_projects)
        if current_project:
            current_project.related_goals = relevant_goals.get("current", [])
        for proj in secondary_projects:
            proj.related_goals = relevant_goals.get(proj.name.lower(), [])
        general_goals = relevant_goals.get("general", [])

        # 3. Reminders (Overdue vs Due Today, no duplicates)
        overdue_reminders, today_reminders = self._collect_reminders(local_now, start_of_today, end_of_today)

        # 4. Autonomous Jobs (Blocked, Active, Paused, Recent Completions)
        blocked_jobs, active_jobs, paused_jobs, recent_completed_jobs = self._collect_jobs(rolling_24h)

        # Associate active jobs with projects if matched
        self._map_jobs_to_projects(active_jobs + blocked_jobs + paused_jobs, current_project, secondary_projects)

        # 4b. Project Health Evaluation (Stage 3.7E.2C)
        project_health_assessments = []
        try:
            from brain.briefing.project_health import ProjectHealthEvaluator
            health_evaluator = ProjectHealthEvaluator(
                db_path=self.db_path,
                job_store=self.job_store,
            )
            raw_health = health_evaluator.evaluate_all(now=local_now)
            health_map = {h.project_name.lower(): h for h in raw_health}

            if current_project and current_project.name.lower() in health_map:
                current_project.health = health_map[current_project.name.lower()]
                project_health_assessments.append(current_project.health)

            for sp in secondary_projects:
                if sp.name.lower() in health_map:
                    sp.health = health_map[sp.name.lower()]
                    project_health_assessments.append(sp.health)
        except Exception as e:
            logger.warning("[BRIEFING COLLECTOR] Project health evaluation failed gracefully: %s", e)

        # 4c. Contextual Recommendations (Stage 3.7E.2D)
        recommendations = []
        try:
            from brain.briefing.recommendations import RecommendationEvaluator
            rec_evaluator = RecommendationEvaluator(
                db_path=self.db_path,
                job_store=self.job_store,
                store=self.recommendation_store,
            )
            all_recs = rec_evaluator.evaluate(
                current_project=current_project,
                secondary_projects=secondary_projects,
                overdue_reminders=overdue_reminders,
                blocked_jobs=blocked_jobs,
                active_jobs=active_jobs,
                paused_jobs=paused_jobs,
                now=local_now,
            )
            # Briefing delivery limit: maximum 3 recommendations
            recommendations = all_recs[:3]
            for r in recommendations:
                rec_evaluator.store.record_surfaced(r.rec_id, channel="briefing", now=local_now)
            rec_evaluator.store.save()
        except Exception as e:
            logger.warning("[BRIEFING COLLECTOR] Contextual recommendations evaluation failed gracefully: %s", e)

        # 5. Actionable System Status
        actionable_system = self._collect_actionable_system_status()

        # 6. Recent Project Developments
        recent_developments = self._collect_recent_developments(recent_completed_jobs)

        # 7. Deterministic Priority Calculation (Collector-Driven)
        priorities = self._calculate_priorities(
            overdue_reminders=overdue_reminders,
            blocked_jobs=blocked_jobs,
            today_reminders=today_reminders,
            active_jobs=active_jobs,
            paused_jobs=paused_jobs,
            current_project=current_project,
            secondary_projects=secondary_projects,
            actionable_system=actionable_system,
        )

        return DailyBriefingContext(
            user_name=user_name,
            current_date=local_now.strftime("%A, %B %d, %Y"),
            current_project=current_project,
            secondary_projects=secondary_projects,
            overdue_reminders=overdue_reminders,
            today_reminders=today_reminders,
            blocked_jobs=blocked_jobs,
            active_jobs=active_jobs,
            paused_jobs=paused_jobs,
            recent_completed_jobs=recent_completed_jobs,
            actionable_system_status=actionable_system,
            recent_project_developments=recent_developments,
            priorities=priorities,
            general_goals=general_goals,
            project_health_assessments=project_health_assessments,
            recommendations=recommendations,
        )

    def _collect_identity_and_projects(
        self,
    ) -> Tuple[str, Optional[ProjectBriefingContext], List[ProjectBriefingContext]]:
        """Retrieve user name, primary active project, and secondary projects."""
        user_name = "User"
        current_project: Optional[ProjectBriefingContext] = None
        secondary_projects: List[ProjectBriefingContext] = []

        conn = self._get_ro_connection()
        try:
            # Name
            cur = conn.execute(
                "SELECT value FROM structured_memories WHERE category = 'identity' AND memory_key = 'name'"
            )
            row = cur.fetchone()
            if row and row["value"]:
                user_name = row["value"].strip()

            # Projects
            cur = conn.execute(
                "SELECT memory_key, value FROM structured_memories WHERE category = 'project' ORDER BY updated_at DESC"
            )
            rows = cur.fetchall()
            seen_names = set()

            for r in rows:
                key = r["memory_key"].strip()
                val = r["value"].strip()

                # Privacy & forgetting: Check session retractions (text, category)
                if is_session_retired(val, "project") or is_session_retired(key, "project"):
                    continue

                if key == "current":
                    clean_name = re.sub(r"\(.*?\)", "", val).strip()
                    clean_name = clean_name.rstrip(". ")
                    current_project = ProjectBriefingContext(
                        name=clean_name or val,
                        is_current=True,
                        description=val if val != clean_name else None,
                    )
                    seen_names.add(clean_name.lower())
                else:
                    clean_name = re.sub(r"\(.*?\)", "", val).strip().rstrip(". ")
                    if clean_name.lower() not in seen_names:
                        secondary_projects.append(
                            ProjectBriefingContext(
                                name=clean_name or val,
                                is_current=False,
                                description=val if val != clean_name else None,
                            )
                        )
                        seen_names.add(clean_name.lower())
        except Exception as e:
            logger.warning("[BRIEFING COLLECTOR] Error collecting identity/projects: %s", e)
        finally:
            conn.close()

        return user_name, current_project, secondary_projects

    def _collect_relevant_goals(
        self,
        current_project: Optional[ProjectBriefingContext],
        secondary_projects: List[ProjectBriefingContext],
    ) -> Dict[str, List[str]]:
        """
        Collect goals from structured_memories, filtering out sensitive/personal goals.
        Maps relevant goals to current or secondary projects where matched,
        and collects unassociated general work goals into mapping['general'].
        Avoids duplicate goals.
        """
        mapping: Dict[str, List[str]] = {"current": [], "general": []}
        curr_name = current_project.name.lower() if current_project else ""
        seen_goals = set()

        conn = self._get_ro_connection()
        try:
            cur = conn.execute(
                "SELECT memory_key, value FROM structured_memories WHERE category = 'goal' ORDER BY updated_at DESC"
            )
            for r in cur.fetchall():
                val = r["value"].strip()
                key = r["memory_key"].strip()

                # Exclude retired (text, category)
                if is_session_retired(val, "goal") or is_session_retired(key, "goal"):
                    continue

                # Strict Privacy Safeguard: Exclude romantic/personal family goals
                if _is_sensitive_personal_goal(val) or _is_sensitive_personal_goal(key):
                    continue

                clean_val = sanitize_briefing_text(val)
                norm_val = clean_val.lower()
                if norm_val in seen_goals:
                    continue

                # Must be relevant to projects, work, learning, or explicitly mention project name
                is_project_relevant = False
                if curr_name and curr_name in norm_val:
                    mapping["current"].append(clean_val)
                    seen_goals.add(norm_val)
                    is_project_relevant = True
                else:
                    for sp in secondary_projects:
                        if sp.name.lower() in norm_val:
                            sp_key = sp.name.lower()
                            mapping.setdefault(sp_key, []).append(clean_val)
                            seen_goals.add(norm_val)
                            is_project_relevant = True
                            break

                if not is_project_relevant and (
                    _PROJECT_GOAL_RELEVANCE_PATTERN.search(clean_val)
                    or _TECHNICAL_RELATIONSHIP_PATTERN.search(clean_val)
                ):
                    mapping["general"].append(clean_val)
                    seen_goals.add(norm_val)
        except Exception as e:
            logger.warning("[BRIEFING COLLECTOR] Error collecting goals: %s", e)
        finally:
            conn.close()

        return mapping

    def _collect_reminders(
        self,
        now: datetime,
        start_of_today: datetime,
        end_of_today: datetime,
    ) -> Tuple[List[ReminderBriefingContext], List[ReminderBriefingContext]]:
        """
        Collect pending reminders partitioned into overdue vs due today without duplicates.
        """
        overdue: List[ReminderBriefingContext] = []
        today: List[ReminderBriefingContext] = []
        seen_ids = set()

        conn = self._get_ro_connection()
        try:
            cur = conn.execute(
                "SELECT id, message, due_at FROM reminders WHERE fired = 0 ORDER BY due_at ASC"
            )
            for r in cur.fetchall():
                rem_id = r["id"]
                raw_msg = r["message"].strip()
                due_str = r["due_at"]
                due_dt = _parse_timestamp(due_str)

                if not due_dt or rem_id in seen_ids:
                    continue
                seen_ids.add(rem_id)

                # Privacy: ignore reminders marked forgotten (text, category)
                if is_session_retired(raw_msg, "reminder") or is_session_retired(raw_msg):
                    continue

                # Redact any credentials/tokens before briefing inclusion
                msg = sanitize_briefing_text(raw_msg)

                if due_dt < now:
                    overdue.append(
                        ReminderBriefingContext(
                            id=rem_id,
                            message=msg,
                            due_at=due_str,
                            is_overdue=True,
                            is_today=False,
                        )
                    )
                elif start_of_today <= due_dt <= end_of_today:
                    today.append(
                        ReminderBriefingContext(
                            id=rem_id,
                            message=msg,
                            due_at=due_str,
                            is_overdue=False,
                            is_today=True,
                        )
                    )
        except Exception as e:
            logger.warning("[BRIEFING COLLECTOR] Error collecting reminders: %s", e)
        finally:
            conn.close()

        return overdue, today

    def _collect_jobs(
        self,
        rolling_24h: datetime,
    ) -> Tuple[List[JobBriefingContext], List[JobBriefingContext], List[JobBriefingContext], List[JobBriefingContext]]:
        """Collect blocked, active, paused, and recently completed autonomous jobs."""
        blocked: List[JobBriefingContext] = []
        active: List[JobBriefingContext] = []
        paused: List[JobBriefingContext] = []
        completed_24h: List[JobBriefingContext] = []

        try:
            all_jobs = self.job_store.list_jobs(limit=100)
            for j in all_jobs:
                # Privacy & forgetting: Check session retractions (text, category)
                if is_session_retired(j.goal, "job") or is_session_retired(j.goal):
                    continue

                # Redact any credentials/tokens before briefing inclusion
                clean_goal = sanitize_briefing_text(j.goal)
                clean_error = sanitize_briefing_text(j.last_error) if j.last_error else None

                ctx = JobBriefingContext(
                    id=j.id,
                    short_id=j.short_id,
                    goal=clean_goal,
                    status=j.status.value,
                    priority=j.priority,
                    last_error=clean_error,
                    completed_at=j.completed_at,
                    tags=j.tags or "",
                )

                if j.status == JobStatus.WAITING_CONFIRMATION:
                    conf = j.get_pending_confirmation()
                    if conf:
                        ctx.pending_action = conf.tool_name
                    blocked.append(ctx)
                elif j.status in (JobStatus.RUNNING, JobStatus.RETRYING):
                    active.append(ctx)
                elif j.status == JobStatus.PAUSED:
                    paused.append(ctx)
                elif j.status == JobStatus.COMPLETED and j.completed_at:
                    comp_dt = _parse_timestamp(j.completed_at)
                    if comp_dt and comp_dt >= rolling_24h:
                        completed_24h.append(ctx)
        except Exception as e:
            logger.warning("[BRIEFING COLLECTOR] Error collecting jobs: %s", e)

        return blocked, active, paused, completed_24h

    def _map_jobs_to_projects(
        self,
        jobs: List[JobBriefingContext],
        current_project: Optional[ProjectBriefingContext],
        secondary_projects: List[ProjectBriefingContext],
    ) -> None:
        """
        Map active or blocked jobs to projects using conservative matching precedence:
        1. Prefer an explicit project tag in jobs.tags (e.g. 'project:<name>' or exact project name tag).
        2. Otherwise, use conservative case-insensitive word-boundary matching for project names in job goals.
        3. Reject ambiguous or weak matches rather than assigning a job to the wrong project.
        """
        if not jobs:
            return

        all_projects = []
        if current_project:
            all_projects.append(current_project)
        all_projects.extend(secondary_projects)

        if not all_projects:
            return

        for j in jobs:
            matched_project: Optional[ProjectBriefingContext] = None

            # Precedence 1: Explicit project tag in job.tags
            if j.tags:
                raw_tags = re.split(r"[,;]", j.tags)
                explicit_tag_values = []
                for raw in raw_tags:
                    t = raw.strip()
                    if not t:
                        continue
                    m = re.match(r"^project\s*[:=]\s*(.+)$", t, re.IGNORECASE)
                    if m:
                        val = m.group(1).strip().strip("'\"")
                        if val:
                            explicit_tag_values.append(val.lower())
                    else:
                        explicit_tag_values.append(t.lower())

                if explicit_tag_values:
                    explicit_matches = []
                    for p in all_projects:
                        p_name_lower = p.name.strip().lower()
                        if p_name_lower in explicit_tag_values:
                            explicit_matches.append(p)
                    if len(explicit_matches) == 1:
                        matched_project = explicit_matches[0]
                    elif len(explicit_matches) > 1:
                        # Ambiguous tags matching multiple distinct projects: reject
                        continue

            # Precedence 2: Conservative case-insensitive word-boundary matching in job.goal
            if matched_project is None:
                goal_matches = []
                for p in all_projects:
                    clean_name = p.name.strip()
                    if not clean_name:
                        continue
                    pattern = re.compile(rf"\b{re.escape(clean_name)}\b", re.IGNORECASE)
                    if pattern.search(j.goal):
                        goal_matches.append(p)

                if len(goal_matches) == 1:
                    matched_project = goal_matches[0]
                elif len(goal_matches) > 1:
                    # Check if one project name is a more specific superset of another
                    sorted_matches = sorted(goal_matches, key=lambda p: len(p.name.strip()), reverse=True)
                    longest = sorted_matches[0]
                    if all(m.name.strip().lower() in longest.name.strip().lower() for m in sorted_matches[1:]):
                        matched_project = longest
                    else:
                        # Truly ambiguous multiple projects mentioned: reject rather than misassigning
                        matched_project = None

            if matched_project:
                matched_project.active_jobs.append(f"[{j.short_id}] {j.goal}")

    def _collect_actionable_system_status(self) -> Optional[str]:
        """Collect system vitals only if materially actionable (low battery or high resource pressure)."""
        if self.system_checker:
            try:
                return self.system_checker()
            except Exception:
                return None

        try:
            from tools.system import get_battery_status, get_ram_usage
            # Check battery
            bat = get_battery_status()
            if bat and isinstance(bat, dict):
                pct = bat.get("percent")
                plugged = bat.get("power_plugged")
                if pct is not None and pct < 20 and not plugged:
                    return f"Battery is critically low ({pct}%) and unplugged."

            # Check RAM
            ram = get_ram_usage()
            if ram and isinstance(ram, dict):
                ram_pct = ram.get("percent")
                if ram_pct is not None and ram_pct > 92.0:
                    return f"System memory pressure is critical ({ram_pct:.1f}% RAM used)."
        except Exception:
            pass

        return None

    def _collect_recent_developments(self, completed_jobs: List[JobBriefingContext]) -> List[str]:
        """Summarize recent achievements from jobs completed in the previous 24 hours."""
        developments = []
        for j in completed_jobs:
            developments.append(f"Completed autonomous job [{j.short_id}]: {j.goal}")
        return developments

    def _calculate_priorities(
        self,
        overdue_reminders: List[ReminderBriefingContext],
        blocked_jobs: List[JobBriefingContext],
        today_reminders: List[ReminderBriefingContext],
        active_jobs: List[JobBriefingContext],
        paused_jobs: List[JobBriefingContext],
        current_project: Optional[ProjectBriefingContext],
        secondary_projects: List[ProjectBriefingContext],
        actionable_system: Optional[str],
    ) -> List[PriorityItem]:
        """
        Calculate deterministic priority ranking.
        Priority tiers:
        1. Overdue reminders and jobs waiting for confirmation (CRITICAL)
        2. Commitments due today (HIGH)
        3. Running and retrying jobs (HIGH)
        4. Paused jobs requiring attention (MEDIUM)
        5. Current project and relevant unfinished work (MEDIUM)
        6. Secondary projects and actionable system information (LOW)
        Preserves numerical job priority as secondary sorting within each category.
        """
        priorities: List[PriorityItem] = []

        # Tier 1: Overdue reminders & Blocked jobs (CRITICAL)
        for rem in sorted(overdue_reminders, key=lambda r: r.due_at):
            priorities.append(
                PriorityItem(
                    category="reminder_overdue",
                    title=f"Overdue: {rem.message}",
                    level=PriorityLevel.CRITICAL,
                    details=f"Was due at {rem.due_at}",
                    action_hint="Reschedule or complete this overdue commitment.",
                )
            )

        for job in sorted(blocked_jobs, key=lambda j: (-j.priority, j.id)):
            act_text = f" ({job.pending_action})" if job.pending_action else ""
            priorities.append(
                PriorityItem(
                    category="job_waiting_confirmation",
                    title=f"Job [{job.short_id}] requires approval{act_text}: {job.goal}",
                    level=PriorityLevel.CRITICAL,
                    details=f"Priority: {job.priority} | Status: WAITING_CONFIRMATION",
                    secondary_priority=job.priority,
                    action_hint=f"Review and confirm with 'job confirm {job.short_id} yes/no'.",
                )
            )

        # Tier 2: Commitments due today (HIGH)
        for rem in sorted(today_reminders, key=lambda r: r.due_at):
            priorities.append(
                PriorityItem(
                    category="reminder_today",
                    title=f"Due Today: {rem.message}",
                    level=PriorityLevel.HIGH,
                    details=f"Scheduled for {rem.due_at}",
                    action_hint="Complete before end of day.",
                )
            )

        # Tier 3: Running and retrying jobs (HIGH)
        for job in sorted(active_jobs, key=lambda j: (-j.priority, j.id)):
            priorities.append(
                PriorityItem(
                    category="job_active",
                    title=f"Active Job [{job.short_id}] ({job.status}): {job.goal}",
                    level=PriorityLevel.HIGH,
                    details=f"Priority: {job.priority} | Status: {job.status}" + (f" | Error: {job.last_error}" if job.last_error else ""),
                    secondary_priority=job.priority,
                    action_hint=f"Check progress with 'job status {job.short_id}'.",
                )
            )

        # Tier 4: Paused jobs requiring attention (MEDIUM)
        for job in sorted(paused_jobs, key=lambda j: (-j.priority, j.id)):
            priorities.append(
                PriorityItem(
                    category="job_paused",
                    title=f"Paused Job [{job.short_id}]: {job.goal}",
                    level=PriorityLevel.MEDIUM,
                    details=f"Priority: {job.priority} | Paused at step boundary",
                    secondary_priority=job.priority,
                    action_hint=f"Resume with 'job resume {job.short_id}'.",
                )
            )

        # Tier 5: Current project and unfinished work (MEDIUM)
        if current_project:
            desc = f" - {current_project.description}" if current_project.description else ""
            goals_text = f" (Goals: {', '.join(current_project.related_goals)})" if current_project.related_goals else ""
            priorities.append(
                PriorityItem(
                    category="project_current",
                    title=f"Primary Project: {current_project.name}{desc}{goals_text}",
                    level=PriorityLevel.MEDIUM,
                    details="Current primary project focus.",
                    action_hint=f"Advance core deliverables for {current_project.name}.",
                )
            )

        # Tier 6: Secondary projects & Actionable system alerts (LOW)
        for proj in secondary_projects:
            desc = f" ({proj.description})" if proj.description else ""
            priorities.append(
                PriorityItem(
                    category="project_secondary",
                    title=f"Secondary Project: {proj.name}{desc}",
                    level=PriorityLevel.LOW,
                    details="Secondary project in progress.",
                )
            )

        if actionable_system:
            priorities.append(
                PriorityItem(
                    category="system",
                    title=f"System Notice: {actionable_system}",
                    level=PriorityLevel.LOW,
                    details="Actionable hardware/OS alert.",
                    action_hint="Connect AC power or free system resources.",
                )
            )

        return priorities
