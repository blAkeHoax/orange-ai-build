"""
brain/briefing/models.py - Data structures for Context-Aware Daily Briefing.

Stage 3.7B: Strongly-typed envelopes for collected operational context,
deterministic priority items, and generated daily briefings.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional


class ProjectHealthStatus(str, Enum):
    """
    Locked health and execution states for projects.
    Stage 3.7E.2: Derived dynamically; never persisted as a cache.
    """
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    PROGRESSING = "PROGRESSING"
    ACTIVE_NO_RECENT_PROGRESS = "ACTIVE_NO_RECENT_PROGRESS"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ProjectHealthAssessment:
    """
    Strongly-typed deterministic assessment of a project's execution health and status.
    Derived on-demand from structured memories, persistent jobs, and step audit logs.
    """
    project_name: str
    is_current: bool
    status: ProjectHealthStatus
    explanation: str
    last_progress_at: Optional[str] = None
    associated_job_ids: List[str] = field(default_factory=list)
    job_status_counts: Dict[str, int] = field(default_factory=dict)
    recent_failure_info: Optional[str] = None
    has_stuck_running_job: bool = False
    evidence: List[str] = field(default_factory=list)
    confidence: str = "HIGH"
    unresolved_ambiguity: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_name": self.project_name,
            "is_current": self.is_current,
            "status": self.status.value,
            "explanation": self.explanation,
            "last_progress_at": self.last_progress_at,
            "associated_job_ids": self.associated_job_ids,
            "job_status_counts": self.job_status_counts,
            "recent_failure_info": self.recent_failure_info,
            "has_stuck_running_job": self.has_stuck_running_job,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "unresolved_ambiguity": self.unresolved_ambiguity,
        }


class PriorityLevel(Enum):
    """
    Deterministic priority tiers for briefing items.
    Lower numerical values represent higher urgency.
    """
    CRITICAL = 1  # Overdue reminders, jobs waiting for confirmation
    HIGH = 2      # Commitments due today, running and retrying jobs
    MEDIUM = 3    # Paused jobs, current project and unfinished work
    LOW = 4       # Secondary projects, actionable system alerts


@dataclass
class PriorityItem:
    """A single actionable priority item calculated deterministically by the collector."""
    category: str
    title: str
    level: PriorityLevel
    details: Optional[str] = None
    secondary_priority: int = 0
    action_hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "title": self.title,
            "level": self.level.name,
            "details": self.details,
            "secondary_priority": self.secondary_priority,
            "action_hint": self.action_hint,
        }


@dataclass
class ProjectBriefingContext:
    """Project status and related deliverables."""
    name: str
    is_current: bool
    description: Optional[str] = None
    related_goals: List[str] = field(default_factory=list)
    active_jobs: List[str] = field(default_factory=list)
    health: Optional[ProjectHealthAssessment] = None


@dataclass
class ReminderBriefingContext:
    """Scheduled commitment with date and overdue/today flags."""
    id: int
    message: str
    due_at: str
    is_overdue: bool
    is_today: bool


@dataclass
class JobBriefingContext:
    """Autonomous job operational status."""
    id: str
    short_id: str
    goal: str
    status: str
    priority: int
    pending_action: Optional[str] = None
    last_error: Optional[str] = None
    completed_at: Optional[str] = None
    tags: str = ""


@dataclass
class DailyBriefingContext:
    """
    Aggregated operational context envelope collected strictly read-only.
    Contains no secrets, personal romantic/family goals, or untrusted execution code.
    """
    user_name: str
    current_date: str
    current_project: Optional[ProjectBriefingContext] = None
    secondary_projects: List[ProjectBriefingContext] = field(default_factory=list)
    overdue_reminders: List[ReminderBriefingContext] = field(default_factory=list)
    today_reminders: List[ReminderBriefingContext] = field(default_factory=list)
    blocked_jobs: List[JobBriefingContext] = field(default_factory=list)       # WAITING_CONFIRMATION
    active_jobs: List[JobBriefingContext] = field(default_factory=list)        # RUNNING, RETRYING
    paused_jobs: List[JobBriefingContext] = field(default_factory=list)        # PAUSED
    recent_completed_jobs: List[JobBriefingContext] = field(default_factory=list)  # Rolling 24h
    actionable_system_status: Optional[str] = None
    recent_project_developments: List[str] = field(default_factory=list)
    priorities: List[PriorityItem] = field(default_factory=list)
    general_goals: List[str] = field(default_factory=list)
    project_health_assessments: List[ProjectHealthAssessment] = field(default_factory=list)
    recommendations: List[Any] = field(default_factory=list)


@dataclass
class DailyBriefing:
    """The generated daily briefing result."""
    date: str
    summary_markdown: str
    priorities: List[PriorityItem]
    raw_reply: str
    model_name: str
    is_fallback: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "summary_markdown": self.summary_markdown,
            "priorities": [p.to_dict() for p in self.priorities],
            "model_name": self.model_name,
            "is_fallback": self.is_fallback,
        }


class EventPriority(str, Enum):
    """
    Deterministic priority tiers for event candidates.
    Matches Stage 3.7E.3B candidate classification.
    """
    CRITICAL = "CRITICAL"  # Immediate user attention required (waiting confirmation, overdue commitments)
    HIGH = "HIGH"          # Significant state changes (job failure, critical reminder due)
    NORMAL = "NORMAL"      # Routine operational completions (job completion, project updates)
    LOW = "LOW"            # Granular audit events (individual step completions, non-project memory updates)

    @property
    def rank(self) -> int:
        """Numerical rank for deterministic sorting (lower is higher priority)."""
        mapping = {
            EventPriority.CRITICAL: 1,
            EventPriority.HIGH: 2,
            EventPriority.NORMAL: 3,
            EventPriority.LOW: 4,
        }
        return mapping.get(self, 99)


@dataclass
class EventCandidate:
    """
    Typed, deterministic candidate event generated from verified persistent data.
    Designed for Stage 3.7E.3B read-only event detection and later delivery orchestration.
    """
    event_id: str
    event_type: str
    entity_id: str
    occurred_at: str
    detected_at: str
    priority: EventPriority
    title: str
    evidence: List[str] = field(default_factory=list)
    material_data: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    confidence: str = "HIGH"
    is_delivered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event candidate to a deterministic dictionary."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "entity_id": self.entity_id,
            "occurred_at": self.occurred_at,
            "detected_at": self.detected_at,
            "priority": self.priority.value if hasattr(self.priority, "value") else str(self.priority),
            "title": self.title,
            "evidence": list(self.evidence),
            "material_data": dict(self.material_data),
            "source": self.source,
            "confidence": self.confidence,
            "is_delivered": self.is_delivered,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventCandidate":
        """Deserialize event candidate from dictionary with safe defaults."""
        raw_prio = data.get("priority", EventPriority.NORMAL.value)
        try:
            prio = EventPriority(raw_prio)
        except ValueError:
            prio = EventPriority.NORMAL

        return cls(
            event_id=data.get("event_id", ""),
            event_type=data.get("event_type", ""),
            entity_id=data.get("entity_id", ""),
            occurred_at=data.get("occurred_at", ""),
            detected_at=data.get("detected_at", ""),
            priority=prio,
            title=data.get("title", ""),
            evidence=list(data.get("evidence", [])) or [],
            material_data=dict(data.get("material_data", {})) or {},
            source=data.get("source", ""),
            confidence=data.get("confidence", "HIGH"),
            is_delivered=bool(data.get("is_delivered", False)),
        )


class DeliveryStatus(str, Enum):
    """
    Deterministic delivery outcome status for event delivery adapters.
    Matches Stage 3.7E.3C.1 delivery contract.
    """
    SUCCESS = "SUCCESS"       # Explicitly confirmed delivery
    FAILURE = "FAILURE"       # Confirmed delivery failure (eligible for retry)
    AMBIGUOUS = "AMBIGUOUS"   # Timeout, exception, missing or unknown status (eligible for retry, NOT recorded as delivered)


@dataclass
class DeliveryResult:
    """Explicit result of a single candidate delivery attempt."""
    event_id: str
    status: DeliveryStatus
    error: Optional[str] = None
    channel: str = "in_process"
    delivered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "error": self.error,
            "channel": self.channel,
            "delivered_at": self.delivered_at,
        }


@dataclass
class EventDeliveryBatchResult:
    """
    Structured outcome of an orchestrated event delivery run.
    Records delivery counts, retryable failures, and safe watermark advancements.
    """
    total_candidates: int = 0
    delivered_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    ambiguous_count: int = 0
    advanced_watermarks: Dict[str, Any] = field(default_factory=dict)
    delivered_event_ids: List[str] = field(default_factory=list)
    failed_event_ids: List[str] = field(default_factory=list)
    ambiguous_event_ids: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_candidates": self.total_candidates,
            "delivered_count": self.delivered_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "ambiguous_count": self.ambiguous_count,
            "advanced_watermarks": dict(self.advanced_watermarks),
            "delivered_event_ids": list(self.delivered_event_ids),
            "failed_event_ids": list(self.failed_event_ids),
            "ambiguous_event_ids": list(self.ambiguous_event_ids),
            "errors": dict(self.errors),
        }


class ReconciliationStatus(str, Enum):
    """
    Status outcomes for startup event reconciliation.
    Stage 3.7E.3C.2: Captures explicit first-run baselines, complete and partial
    recovery, safe empty scans, and fail-closed error states.
    """
    SUCCESS = "SUCCESS"
    NO_ELIGIBLE_CANDIDATES = "NO_ELIGIBLE_CANDIDATES"
    FIRST_RUN_BASELINE_ESTABLISHED = "FIRST_RUN_BASELINE_ESTABLISHED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    STORE_MISSING_UNEXPECTED = "STORE_MISSING_UNEXPECTED"
    STORE_CORRUPT = "STORE_CORRUPT"
    SOURCE_COLLECTION_FAILURE = "SOURCE_COLLECTION_FAILURE"
    DELIVERY_FAILURE = "DELIVERY_FAILURE"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


@dataclass
class ReconciliationResult:
    """
    Structured outcome of a startup reconciliation run.
    Provides complete operational telemetry: candidates detected, attempted,
    delivered, failed, ambiguous, and deferred due to batch limits.
    """
    status: ReconciliationStatus
    is_first_run: bool = False
    baseline_established: bool = False
    total_detected: int = 0
    eligible_count: int = 0
    selected_count: int = 0
    delivered_count: int = 0
    failed_count: int = 0
    ambiguous_count: int = 0
    skipped_count: int = 0
    overflow_count: int = 0
    pruned_delivered_count: int = 0
    orphaned_temp_files_cleaned: int = 0
    advanced_watermarks: Dict[str, Any] = field(default_factory=dict)
    delivered_event_ids: List[str] = field(default_factory=list)
    failed_event_ids: List[str] = field(default_factory=list)
    ambiguous_event_ids: List[str] = field(default_factory=list)
    overflow_event_ids: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    reconciled_at: Optional[str] = None
    details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "is_first_run": self.is_first_run,
            "baseline_established": self.baseline_established,
            "total_detected": self.total_detected,
            "eligible_count": self.eligible_count,
            "selected_count": self.selected_count,
            "delivered_count": self.delivered_count,
            "failed_count": self.failed_count,
            "ambiguous_count": self.ambiguous_count,
            "skipped_count": self.skipped_count,
            "overflow_count": self.overflow_count,
            "pruned_delivered_count": self.pruned_delivered_count,
            "orphaned_temp_files_cleaned": self.orphaned_temp_files_cleaned,
            "advanced_watermarks": dict(self.advanced_watermarks),
            "delivered_event_ids": list(self.delivered_event_ids),
            "failed_event_ids": list(self.failed_event_ids),
            "ambiguous_event_ids": list(self.ambiguous_event_ids),
            "overflow_event_ids": list(self.overflow_event_ids),
            "errors": dict(self.errors),
            "reconciled_at": self.reconciled_at,
            "details": self.details,
        }



