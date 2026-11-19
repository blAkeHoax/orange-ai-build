"""
brain/briefing/recommendations.py - Contextual Recommendations Engine & Lifecycle Store.

Stage 3.7E.2D: Contextual Recommendations
Implements:
1. Recommendation models with stable identity and material evidence fingerprinting.
2. Deterministic eligibility and priority evaluation from authoritative sources.
3. Versioned JSON persistence (recommendations.json) with atomic writes, corruption recovery,
   and thread-safe shared lifecycle management across daily briefing and chat.
4. User lifecycle controls: dismiss (7-day), defer (24h/7d/custom), mark handled, show evidence.
Strictly read-only evaluation of underlying operational entities; zero mutations to jobs or SQLite.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Optional, Dict, Any, List, Tuple, Set, Union, FrozenSet

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

from brain.paths import get_project_root
from brain.jobs.models import Job, JobStep, JobStatus
from brain.briefing.models import (
    ProjectHealthAssessment,
    ProjectHealthStatus,
    ProjectBriefingContext,
    ReminderBriefingContext,
    JobBriefingContext,
)
from brain.conflict_resolver import is_session_retired
from brain.briefing.collector import (
    sanitize_briefing_text,
    _is_sensitive_personal_goal,
    _parse_timestamp,
    is_default_db_path,
)

logger = logging.getLogger(__name__)

RECOMMENDATIONS_FILENAME = "recommendations.json"
STORE_SCHEMA_VERSION = 1
DISMISSAL_SUPPRESSION_DAYS = 7
DEFERRAL_DEFAULT_HOURS = 24
FAILURE_RECENCY_HOURS = 48
STUCK_RUNNING_HOURS = 24
INACTIVITY_DAYS_THRESHOLD = 5

# Explicit immutable set of the 85 authoritative baseline recommendation IDs.
# These records must be strictly preserved byte-for-byte across all retention operations.
PROTECTED_BASELINE_REC_IDS: FrozenSet[str] = frozenset([
    "job_confirmation:00182864a197:approval_pending",
    "job_confirmation:00318a67efbd:approval_pending",
    "job_confirmation:03fa881074b2:approval_pending",
    "job_confirmation:0ad19f5d117b:approval_pending",
    "job_confirmation:13eb7b11a3ca:approval_pending",
    "job_confirmation:1506e85d85ff:approval_pending",
    "job_confirmation:176008a69214:approval_pending",
    "job_confirmation:17f185dd6f06:approval_pending",
    "job_confirmation:1b38e3113c1d:approval_pending",
    "job_confirmation:1c13562f99f0:approval_pending",
    "job_confirmation:246def73d64b:approval_pending",
    "job_confirmation:2aba784971aa:approval_pending",
    "job_confirmation:2cbcec9239a8:approval_pending",
    "job_confirmation:377c089c1ed8:approval_pending",
    "job_confirmation:3a6315473147:approval_pending",
    "job_confirmation:3f43d146852e:approval_pending",
    "job_confirmation:3ffa1385c6e2:approval_pending",
    "job_confirmation:451c9daffd60:approval_pending",
    "job_confirmation:4f151112a769:approval_pending",
    "job_confirmation:5242a9998279:approval_pending",
    "job_confirmation:5365b1687f29:approval_pending",
    "job_confirmation:555d7dfe5e63:approval_pending",
    "job_confirmation:5d51a241137c:approval_pending",
    "job_confirmation:5e34f134c0c1:approval_pending",
    "job_confirmation:61ba94a9f56e:approval_pending",
    "job_confirmation:6352d633e2b4:approval_pending",
    "job_confirmation:638e528e4c2c:approval_pending",
    "job_confirmation:72bc07545666:approval_pending",
    "job_confirmation:76f7c4765b1f:approval_pending",
    "job_confirmation:79737dd0eda9:approval_pending",
    "job_confirmation:7a050fe6ea0b:approval_pending",
    "job_confirmation:8973f5b36279:approval_pending",
    "job_confirmation:8aea147be0d3:approval_pending",
    "job_confirmation:91497786851b:approval_pending",
    "job_confirmation:94c721a0c901:approval_pending",
    "job_confirmation:98dddb56c1ac:approval_pending",
    "job_confirmation:9d43822e9a0d:approval_pending",
    "job_confirmation:a0236005874c:approval_pending",
    "job_confirmation:a316a3358980:approval_pending",
    "job_confirmation:a667619bd029:approval_pending",
    "job_confirmation:a95a5db81355:approval_pending",
    "job_confirmation:b0408524deb0:approval_pending",
    "job_confirmation:b073ca23cc73:approval_pending",
    "job_confirmation:b3986c2c65d2:approval_pending",
    "job_confirmation:b7d380c7aa44:approval_pending",
    "job_confirmation:c15056fac327:approval_pending",
    "job_confirmation:cc292bdf6f09:approval_pending",
    "job_confirmation:cc53fa89450c:approval_pending",
    "job_confirmation:ce0edc3e3c04:approval_pending",
    "job_confirmation:d2a2b739e5fc:approval_pending",
    "job_confirmation:d5a7091f331e:approval_pending",
    "job_confirmation:d72eb8927d65:approval_pending",
    "job_confirmation:db160f251e8c:approval_pending",
    "job_confirmation:e3c3e63bdb54:approval_pending",
    "job_confirmation:f280b93e0aad:approval_pending",
    "job_confirmation:f47f57eb5b96:approval_pending",
    "job_confirmation:f93b5af61bd8:approval_pending",
    "job_confirmation:f93bd1ccc288:approval_pending",
    "job_confirmation:fea6cf83b367:approval_pending",
    "job_confirmation:fea91e286771:approval_pending",
    "job_confirmation:ff55622bc3b0:approval_pending",
    "job_confirmation:ffbeb9350323:approval_pending",
    "job_stuck:2a59e08e853a:no_verified_progress",
    "job_stuck:2c3a8b17caa9:no_verified_progress",
    "job_stuck:2ff4052ae0f2:no_verified_progress",
    "job_stuck:30a5e8d77748:no_verified_progress",
    "job_stuck:41dad1f8863f:no_verified_progress",
    "job_stuck:45c1a5d74b3e:no_verified_progress",
    "job_stuck:51fe0478ff9e:no_verified_progress",
    "job_stuck:822419ba4930:no_verified_progress",
    "job_stuck:8ca8ee7a4139:no_verified_progress",
    "job_stuck:97c35bea663b:no_verified_progress",
    "job_stuck:9ca3d847e1f8:no_verified_progress",
    "job_stuck:b5d610248714:no_verified_progress",
    "job_stuck:be5c6f4ba376:no_verified_progress",
    "job_stuck:bf7f885c7f20:no_verified_progress",
    "job_stuck:d4129174f9f7:no_verified_progress",
    "project_paused:project_zephyr:review_pause",
    "project_stalled:alpha_release:review_inactivity",
    "project_stalled:app:review_inactivity",
    "project_stalled:orange_ai:review_inactivity",
    "project_stalled:orange_ai_desktop:review_inactivity",
    "project_stalled:project_zephyr:review_inactivity",
    "project_stalled:worker_service:review_inactivity",
    "reminder_overdue:1:review_commitment",
])


def get_recommendations_store_path() -> str:
    """Return the authoritative path to the persistent recommendations JSON file."""
    return os.path.join(get_project_root(), RECOMMENDATIONS_FILENAME)


class RecommendationPriority(str, Enum):
    """
    Deterministic priority tiers for contextual recommendations.
    CRITICAL is strictly reserved for future explicitly approved emergency protocols.
    """
    CRITICAL = "CRITICAL"  # Reserved; no current source qualifies
    HIGH = "HIGH"          # Significant blockers, repeated failures, pending confirmations
    NORMAL = "NORMAL"      # Meaningful overdue commitments, primary-project inactivity/pauses
    LOW = "LOW"            # Optional housekeeping

    @property
    def rank(self) -> int:
        """Numerical rank for deterministic sorting (lower is higher priority)."""
        mapping = {
            RecommendationPriority.CRITICAL: 1,
            RecommendationPriority.HIGH: 2,
            RecommendationPriority.NORMAL: 3,
            RecommendationPriority.LOW: 4,
        }
        return mapping.get(self, 99)


class RecommendationState(str, Enum):
    """Lifecycle states for recommendations."""
    ACTIVE = "ACTIVE"
    DISMISSED = "DISMISSED"
    DEFERRED = "DEFERRED"
    HANDLED = "HANDLED"
    STALE = "STALE"


class RecommendationCategory(str, Enum):
    """Categories describing the operational origin of the recommendation."""
    JOB_CONFIRMATION = "job_confirmation"
    JOB_FAILURE = "job_failure"
    JOB_STUCK = "job_stuck"
    REMINDER_OVERDUE = "reminder_overdue"
    PROJECT_BLOCKED = "project_blocked"
    PROJECT_PAUSED = "project_paused"
    PROJECT_STALLED = "project_stalled"


def normalize_local_now(dt: Optional[datetime]) -> datetime:
    """Ensure now is a naive local datetime for consistent comparisons."""
    if dt is None:
        return datetime.now()
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def normalize_project_key(name: str) -> str:
    """Normalize project name into a canonical alphanumeric key for stable IDs."""
    clean = re.sub(r"\(.*?\)", "", name).strip().lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"[-\s]+", "_", clean).strip("_")
    return clean or "unknown_project"


def calculate_material_fingerprint(material_data: Dict[str, Any]) -> str:
    """
    Generate a deterministic 16-character SHA-256 fingerprint from normalized material facts.
    Incidental timestamp refreshes or phrasing changes do not affect the fingerprint.
    """
    serialized = json.dumps(material_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


@dataclass
class Recommendation:
    """
    Informational recommendation model.
    Presents context, explanation, and non-executable suggested next steps to the user.
    """
    rec_id: str
    category: str
    entity_id: str
    recommendation_type: str
    title: str
    explanation: str
    suggested_action: str
    priority: RecommendationPriority
    evidence_fingerprint: str
    evidence: List[str] = field(default_factory=list)
    state: RecommendationState = RecommendationState.ACTIVE
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    suppressed_until: Optional[str] = None
    handled_fingerprint: Optional[str] = None
    last_surfaced_at: Optional[str] = None
    channels: List[str] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        """Short identifier prefix for concise CLI references."""
        parts = self.rec_id.split(":")
        if len(parts) >= 2:
            raw_ent = parts[1]
            return raw_ent[:8] if len(raw_ent) > 8 else raw_ent
        return self.rec_id[:8]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize recommendation to dictionary for JSON persistence."""
        return {
            "rec_id": self.rec_id,
            "category": self.category,
            "entity_id": self.entity_id,
            "recommendation_type": self.recommendation_type,
            "title": self.title,
            "explanation": self.explanation,
            "suggested_action": self.suggested_action,
            "priority": self.priority.value,
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence": self.evidence,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "suppressed_until": self.suppressed_until,
            "handled_fingerprint": self.handled_fingerprint,
            "last_surfaced_at": self.last_surfaced_at,
            "channels": self.channels,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recommendation":
        """Deserialize recommendation from dictionary with graceful fallbacks."""
        raw_prio = data.get("priority", RecommendationPriority.NORMAL.value)
        try:
            prio = RecommendationPriority(raw_prio)
        except ValueError:
            prio = RecommendationPriority.NORMAL

        raw_state = data.get("state", RecommendationState.ACTIVE.value)
        try:
            state = RecommendationState(raw_state)
        except ValueError:
            state = RecommendationState.ACTIVE

        return cls(
            rec_id=data.get("rec_id", ""),
            category=data.get("category", ""),
            entity_id=data.get("entity_id", ""),
            recommendation_type=data.get("recommendation_type", ""),
            title=data.get("title", ""),
            explanation=data.get("explanation", ""),
            suggested_action=data.get("suggested_action", ""),
            priority=prio,
            evidence_fingerprint=data.get("evidence_fingerprint", ""),
            evidence=data.get("evidence", []) or [],
            state=state,
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            suppressed_until=data.get("suppressed_until"),
            handled_fingerprint=data.get("handled_fingerprint"),
            last_surfaced_at=data.get("last_surfaced_at"),
            channels=data.get("channels", []) or [],
        )


class CrossProcessFileLock:
    """
    Standard-library cross-process mutual exclusion file lock.
    Windows-compatible via msvcrt.locking; POSIX-compatible fallback via fcntl.flock.
    Bounded acquisition timeout with reliable exception cleanup.
    """

    def __init__(self, lock_path: str, timeout: float = 5.0):
        self.lock_path = lock_path
        self.timeout = max(0.1, float(timeout))
        self.fd: Optional[int] = None
        self._depth = 0

    def acquire(self) -> bool:
        """Acquire the file lock within the timeout. Re-entrant within the same instance."""
        if self._depth > 0:
            self._depth += 1
            return True

        start_time = time.time()
        parent_dir = os.path.dirname(os.path.abspath(self.lock_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
                os.fsync(fd)
        except Exception:
            pass

        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if _HAS_MSVCRT:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif _HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                self._depth = 1
                return True
            except (OSError, IOError):
                if time.time() - start_time >= self.timeout:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise TimeoutError(
                        f"Timed out after {self.timeout:.1f}s waiting for cross-process lock: '{self.lock_path}'"
                    )
                time.sleep(0.02)

    def release(self) -> None:
        """Release the file lock, unlocking byte and closing file descriptor."""
        if self._depth > 1:
            self._depth -= 1
            return
        if self._depth == 1 and self.fd is not None:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if _HAS_MSVCRT:
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                elif _HAS_FCNTL:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except Exception:
                pass
            finally:
                try:
                    os.close(self.fd)
                except Exception:
                    pass
                self.fd = None
                self._depth = 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class RecommendationStore:
    """
    Persistent JSON store for Recommendation lifecycle state.
    
    Adheres strictly to Stage 3.7E.2D & Stage 3.7E.2D.1 specifications:
    - Dedicated JSON file in get_project_root() (recommendations.json).
    - Adjacent cross-process lockfile (recommendations.json.lock) via CrossProcessFileLock.
    - Thread-safe in-process access via RLock.
    - Full read-modify-write protection across processes: reloads latest state from disk under lock.
    - Atomic writes using temporary file and os.replace.
    - Safe first-time initialization on missing file.
    - Fail-closed on corruption: creates a timestamped backup copy and refuses to overwrite.
    - Preserves dismissal, deferral, and handled history.
    - Zero SQLite schema alterations or table creations.
    """

    _STRICT_TEST_ISOLATION: bool = False

    def __init__(self, file_path: Optional[str] = None):
        if RecommendationStore._STRICT_TEST_ISOLATION and file_path is None:
            raise RuntimeError(
                "RecommendationStore test isolation violation: Default production workspace path "
                "was accessed while strict test isolation is active! An explicit isolated file_path must be provided."
            )
        self.file_path = file_path or get_recommendations_store_path()
        self.lock_path = f"{self.file_path}.lock"
        self._lock = threading.RLock()
        self._file_lock = CrossProcessFileLock(self.lock_path, timeout=5.0)
        self._is_corrupt = False
        self._items: Dict[str, Recommendation] = {}
        self._load()

    @contextlib.contextmanager
    def lock(self, reload: bool = True):
        """
        Acquire in-process thread lock and cross-process file lock.
        Optionally reloads latest disk state to ensure freshness.
        """
        with self._lock:
            with self._file_lock:
                if reload:
                    self._load()
                yield self

    def _load(self) -> None:
        """Load recommendations from JSON disk storage with fail-closed safety."""
        with self._lock:
            if not os.path.exists(self.file_path):
                # Safe first-time initialization; preserve any in-memory additions
                self._is_corrupt = False
                return

            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    raise ValueError("Root JSON element is not an object.")

                version = data.get("version", 1)
                items_dict = data.get("recommendations", {})
                if not isinstance(items_dict, dict):
                    raise ValueError("'recommendations' field is not an object.")

                disk_items = {}
                for rec_id, rec_raw in items_dict.items():
                    if isinstance(rec_raw, dict):
                        rec = Recommendation.from_dict(rec_raw)
                        disk_items[rec.rec_id] = rec

                # Disk is authoritative; preserve in-memory candidates not yet persisted
                disk_items.update({k: v for k, v in self._items.items() if k not in disk_items})
                self._items = disk_items
                self._is_corrupt = False
            except Exception as e:
                # Corrupted existing file: preserve timestamped backup and fail closed
                self._is_corrupt = True
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = f"{self.file_path}.corrupt.{ts}"
                try:
                    import shutil
                    shutil.copy2(self.file_path, backup_path)
                    logger.error(
                        "[RECOMMENDATION STORE] Corrupted persistence file detected! Preserved backup at '%s': %s",
                        backup_path,
                        e,
                    )
                except Exception as copy_err:
                    logger.critical(
                        "[RECOMMENDATION STORE] Failed to backup corrupt file '%s': %s",
                        self.file_path,
                        copy_err,
                    )
                # In fail-closed mode, self._items remains empty and writes are blocked

    def save(self) -> bool:
        """
        Persist recommendation store atomically to disk under both thread
        and cross-process locks.
        """
        with self._lock:
            with self._file_lock:
                return self._save_unlocked()

    def _save_unlocked(self) -> bool:
        """Internal save implementation without acquiring locks (must be called under lock)."""
        if self._is_corrupt:
            logger.error("[RECOMMENDATION STORE] Cannot save: Store is in fail-closed corrupted state.")
            return False

        payload = {
            "version": STORE_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "recommendations": {rec_id: rec.to_dict() for rec_id, rec in self._items.items()},
        }

        temp_path = f"{self.file_path}.tmp"
        try:
            parent_dir = os.path.dirname(self.file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replacement with brief retry for transient Windows file locks
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    os.replace(temp_path, self.file_path)
                    return True
                except PermissionError:
                    if attempt < max_retries - 1:
                        time.sleep(0.05 * (attempt + 1))
                    else:
                        raise
        except Exception as e:
            logger.error("[RECOMMENDATION STORE] Atomic save failed: %s", e)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return False

    def get(self, rec_id: str) -> Optional[Recommendation]:
        """Retrieve a recommendation by ID."""
        with self._lock:
            return self._items.get(rec_id)

    def list_all(self) -> List[Recommendation]:
        """Return all persisted recommendations."""
        with self._lock:
            return list(self._items.values())

    def upsert_candidate(self, candidate: Recommendation, now: Optional[datetime] = None) -> Recommendation:
        """
        Register or reconcile an evaluated candidate recommendation against stored lifecycle state.
        
        Rules:
        - If previously HANDLED: reopens to ACTIVE only if candidate has a new material fingerprint.
        - If previously DISMISSED: checks if 7-day suppression has expired.
        - If previously DEFERRED: checks if deferral timestamp has expired.
        - If previously STALE: returns to ACTIVE since condition has reappeared.
        """
        local_now = normalize_local_now(now)
        now_iso = local_now.isoformat()

        with self._lock:
            existing = self._items.get(candidate.rec_id)
            if not existing:
                # First time seeing this recommendation
                candidate.created_at = now_iso
                candidate.updated_at = now_iso
                candidate.state = RecommendationState.ACTIVE
                self._items[candidate.rec_id] = candidate
                return candidate

            # Reconcile existing lifecycle state
            existing.title = candidate.title
            existing.explanation = candidate.explanation
            existing.suggested_action = candidate.suggested_action
            existing.priority = candidate.priority
            existing.evidence = candidate.evidence
            existing.updated_at = now_iso

            # Check material evidence changes
            if existing.state == RecommendationState.HANDLED:
                if candidate.evidence_fingerprint != existing.handled_fingerprint:
                    # Materially new evidence detected: reopen recommendation!
                    logger.info(
                        "[RECOMMENDATION STORE] Reopening HANDLED recommendation '%s': new evidence fingerprint %s != %s",
                        existing.rec_id,
                        candidate.evidence_fingerprint,
                        existing.handled_fingerprint,
                    )
                    existing.state = RecommendationState.ACTIVE
                    existing.evidence_fingerprint = candidate.evidence_fingerprint
                    existing.handled_fingerprint = None
                    existing.suppressed_until = None
            elif existing.state == RecommendationState.DISMISSED:
                # Check dismissal expiry
                if existing.suppressed_until:
                    supp_dt = _parse_timestamp(existing.suppressed_until)
                    if supp_dt and local_now >= supp_dt:
                        existing.state = RecommendationState.ACTIVE
                        existing.suppressed_until = None
            elif existing.state == RecommendationState.DEFERRED:
                # Check deferral expiry
                if existing.suppressed_until:
                    supp_dt = _parse_timestamp(existing.suppressed_until)
                    if supp_dt and local_now >= supp_dt:
                        existing.state = RecommendationState.ACTIVE
                        existing.suppressed_until = None
            elif existing.state == RecommendationState.STALE:
                # Condition has returned
                existing.state = RecommendationState.ACTIVE
                existing.evidence_fingerprint = candidate.evidence_fingerprint

            existing.evidence_fingerprint = candidate.evidence_fingerprint
            return existing

    def dismiss(self, rec_id: str, now: Optional[datetime] = None) -> bool:
        """
        Dismiss a recommendation: suppresses it for 7 days.
        Reloads latest state under cross-process lock before mutating.
        Returns True if recommendation was found and updated.
        """
        local_now = normalize_local_now(now)
        suppressed_target = local_now + timedelta(days=DISMISSAL_SUPPRESSION_DAYS)

        with self.lock(reload=True):
            rec = self._resolve_target(rec_id)
            if not rec:
                return False
            rec.state = RecommendationState.DISMISSED
            rec.suppressed_until = suppressed_target.isoformat()
            rec.updated_at = local_now.isoformat()
            return self._save_unlocked()

    @classmethod
    def validate_deferral_target(cls, duration_or_dt: Union[str, datetime], now: Optional[datetime] = None) -> Optional[datetime]:
        """
        Validate and resolve a deferral target datetime.
        Enforces:
        - Must be a future datetime (> now).
        - Supports: '24h', '7d', and valid ISO 8601 timestamps (with or without 'T').
        - Normalizes timezones to local naive datetime.
        - Rejects past, equal-to-now, invalid, or empty specifications.
        """
        local_now = normalize_local_now(now)
        if duration_or_dt is None:
            return None

        if isinstance(duration_or_dt, datetime):
            target_dt = normalize_local_now(duration_or_dt)
            if target_dt <= local_now:
                return None
            return target_dt

        clean = str(duration_or_dt).strip()
        if not clean:
            return None

        clean_lower = clean.lower()
        if clean_lower in ("24h", "24 hours", "1d", "1 day", "tomorrow"):
            return local_now + timedelta(hours=DEFERRAL_DEFAULT_HOURS)
        elif clean_lower in ("7d", "7 days", "1w", "1 week"):
            return local_now + timedelta(days=7)

        # Attempt ISO 8601 parse (handles '2026-11-15T15:30:00', '2026-11-15 15:30:00', '2026-11-15', etc.)
        try:
            dt = datetime.fromisoformat(clean)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            if dt <= local_now:
                return None
            return dt
        except Exception:
            return None

    def defer(self, rec_id: str, duration_or_dt: Union[str, datetime], now: Optional[datetime] = None) -> bool:
        """
        Defer a recommendation for 24h, 7d, or a custom target datetime.
        Validates target datetime before modifying lifecycle state.
        Returns False if validation fails or target is in the past.
        """
        local_now = normalize_local_now(now)
        target_dt = self.validate_deferral_target(duration_or_dt, now=local_now)
        if target_dt is None:
            logger.warning("[RECOMMENDATION STORE] Invalid or past deferral duration/date: '%s'", duration_or_dt)
            return False

        with self.lock(reload=True):
            rec = self._resolve_target(rec_id)
            if not rec:
                return False
            rec.state = RecommendationState.DEFERRED
            rec.suppressed_until = target_dt.isoformat()
            rec.updated_at = local_now.isoformat()
            return self._save_unlocked()

    def handle(self, rec_id: str, now: Optional[datetime] = None) -> bool:
        """
        Mark a recommendation as HANDLED.
        Locks it against re-surfacing until its material evidence fingerprint changes.
        """
        local_now = normalize_local_now(now)

        with self.lock(reload=True):
            rec = self._resolve_target(rec_id)
            if not rec:
                return False
            rec.state = RecommendationState.HANDLED
            rec.handled_fingerprint = rec.evidence_fingerprint
            rec.suppressed_until = None
            rec.updated_at = local_now.isoformat()
            return self._save_unlocked()

    def mark_stale(self, rec_id: str, now: Optional[datetime] = None) -> bool:
        """Mark a recommendation as STALE when the underlying operational condition no longer exists."""
        local_now = normalize_local_now(now)

        with self.lock(reload=True):
            rec = self._resolve_target(rec_id)
            if not rec:
                return False
            rec.state = RecommendationState.STALE
            rec.updated_at = local_now.isoformat()
            return self._save_unlocked()

    def record_surfaced(self, rec_id: str, channel: str, now: Optional[datetime] = None) -> bool:
        """Record that a recommendation was surfaced in a specific channel."""
        local_now = normalize_local_now(now)

        with self.lock(reload=True):
            rec = self._resolve_target(rec_id)
            if rec:
                rec.last_surfaced_at = local_now.isoformat()
                if channel not in rec.channels:
                    rec.channels.append(channel)
                rec.updated_at = local_now.isoformat()
                return self._save_unlocked()
            return False

    def prune_stale_or_missing(self, current_candidate_ids: Set[str], now: Optional[datetime] = None) -> bool:
        """Mark recommendations as STALE if their underlying issue is no longer present."""
        local_now = normalize_local_now(now)
        with self._lock:
            changed = False
            for rec_id, rec in self._items.items():
                if rec_id not in current_candidate_ids and rec.state == RecommendationState.ACTIVE:
                    rec.state = RecommendationState.STALE
                    rec.updated_at = local_now.isoformat()
                    changed = True
            if changed:
                return self.save()
            return False

    def prune_expired_records(
        self,
        retention_days: int = 30,
        now: Optional[datetime] = None,
        preserve_baseline: bool = True,
    ) -> int:
        """
        Prune expired recommendation records (STALE or DISMISSED) older than retention_days.

        Guarantees:
        - Baseline records in PROTECTED_BASELINE_REC_IDS are strictly immune when preserve_baseline=True.
        - Active or Handled recommendations are never pruned.
        - If 0 records are eligible for removal, disk storage is left completely untouched (no save).
        - Operates under thread and cross-process locks.
        - Returns count of pruned records.
        """
        retention_days = max(1, int(retention_days))
        ref_now = normalize_local_now(now)
        cutoff = ref_now - timedelta(days=retention_days)

        with self.lock(reload=True):
            if self._is_corrupt:
                logger.error("[RECOMMENDATION STORE] Cannot prune: Store is in fail-closed corrupted state.")
                return 0

            to_remove: List[str] = []
            for rec_id, rec in self._items.items():
                if preserve_baseline and rec_id in PROTECTED_BASELINE_REC_IDS:
                    continue
                if rec.state in (RecommendationState.STALE, RecommendationState.DISMISSED):
                    updated_dt = _parse_timestamp(rec.updated_at)
                    if updated_dt and updated_dt < cutoff:
                        to_remove.append(rec_id)

            if not to_remove:
                return 0

            for rec_id in to_remove:
                del self._items[rec_id]

            self._save_unlocked()
            logger.info(
                "[RECOMMENDATION STORE] Pruned %d expired recommendation records older than %d days.",
                len(to_remove),
                retention_days,
            )
            return len(to_remove)

    def _resolve_target(self, target: str) -> Optional[Recommendation]:
        """Resolve a recommendation by exact rec_id or by short_id prefix."""
        clean = target.strip()
        if clean in self._items:
            return self._items[clean]

        matches = [r for r in self._items.values() if r.short_id == clean or clean in r.rec_id]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            logger.warning("[RECOMMENDATION STORE] Ambiguous recommendation ID prefix '%s' matches %d items.", clean, len(matches))
            return None
        return None


class RecommendationEvaluator:
    """
    Pure, deterministic evaluator that inspects authoritative sources and generates
    eligible candidate recommendations.
    
    Enforces all Stage 3.7E.2D rules:
    - 7 eligible sources.
    - Restricted PROGRESSING and UNKNOWN.
    - Excluded general inactivity.
    - Repeated failure criteria: attempt >= 2, RETRYING/FAILED, <= 48h, verified error, un-superseded.
    - Strict privacy, session-retirement, sensitive-goal exclusion, and credential sanitization.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        job_store: Optional[Any] = None,
        store: Optional[RecommendationStore] = None,
    ):
        self.db_path = db_path
        self.job_store = job_store

        # Storage Isolation Safeguard (Stage 3.7E.5A-R / 3.7E.5A-R.1):
        # When a non-default database path is supplied (directly or via job_store),
        # an explicit store must also be provided.
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

        if is_custom_db and store is None:
            raise ValueError(
                "RecommendationEvaluator initialized with a non-default database but no explicit "
                "store was provided. Silent fallback to the production recommendation store "
                "is prohibited to prevent test or temporary data pollution."
            )

        if store is not None:
            self.store = store
        else:
            self.store = RecommendationStore()

    def evaluate(
        self,
        current_project: Optional[ProjectBriefingContext],
        secondary_projects: List[ProjectBriefingContext],
        overdue_reminders: List[ReminderBriefingContext],
        blocked_jobs: List[JobBriefingContext],
        active_jobs: List[JobBriefingContext],
        paused_jobs: List[JobBriefingContext],
        now: Optional[datetime] = None,
    ) -> List[Recommendation]:
        """
        Evaluate all candidate recommendations from provided context.
        Reconciles with RecommendationStore, updates STALE states, and returns
        active, eligible recommendations sorted deterministically.
        """
        local_now = normalize_local_now(now)
        candidates: List[Recommendation] = []

        # -------------------------------------------------------------
        # 1. WAITING_CONFIRMATION Jobs (High Priority)
        # -------------------------------------------------------------
        candidates.extend(self._evaluate_confirmation_jobs(blocked_jobs, local_now))

        # -------------------------------------------------------------
        # 2. Repeated Job Failures (High Priority)
        # -------------------------------------------------------------
        candidates.extend(self._evaluate_repeated_failures(local_now))

        # -------------------------------------------------------------
        # 3. Stuck-Running Jobs (High Priority)
        # -------------------------------------------------------------
        candidates.extend(self._evaluate_stuck_running_jobs(active_jobs, local_now))

        # -------------------------------------------------------------
        # 4. Project Health (BLOCKED, PAUSED, ACTIVE_NO_RECENT_PROGRESS)
        # -------------------------------------------------------------
        all_projects: List[ProjectBriefingContext] = []
        if current_project:
            all_projects.append(current_project)
        all_projects.extend(secondary_projects)

        candidates.extend(self._evaluate_project_health(all_projects, local_now))

        # -------------------------------------------------------------
        # 5. Overdue Reminders (Normal Priority)
        # -------------------------------------------------------------
        candidates.extend(self._evaluate_overdue_reminders(overdue_reminders, local_now))

        # Reconcile all generated candidates with lifecycle store under cross-process lock
        current_candidate_ids = {c.rec_id for c in candidates}
        active_eligible: List[Recommendation] = []

        with self.store.lock(reload=True):
            for cand in candidates:
                persisted = self.store.upsert_candidate(cand, now=local_now)
                if persisted.state == RecommendationState.ACTIVE:
                    active_eligible.append(persisted)

            # Mark previously active recommendations that have disappeared as STALE
            self.store.prune_stale_or_missing(current_candidate_ids, now=local_now)
            self.store._save_unlocked()

        # Deterministic sorting: Priority Rank ASC, then Primary Project, then rec_id ASC
        def sort_key(rec: Recommendation) -> Tuple[int, int, str]:
            is_primary = 0 if "orange_ai" in rec.rec_id or (current_project and normalize_project_key(current_project.name) in rec.rec_id) else 1
            return (rec.priority.rank, is_primary, rec.rec_id)

        active_eligible.sort(key=sort_key)
        return active_eligible

    # -----------------------------------------------------------------
    # Evaluator Subroutines
    # -----------------------------------------------------------------

    def _evaluate_confirmation_jobs(
        self,
        blocked_jobs: List[JobBriefingContext],
        now: datetime,
    ) -> List[Recommendation]:
        """Evaluate jobs waiting for human confirmation."""
        recs = []
        for j in blocked_jobs:
            if is_session_retired(j.goal, "job") or is_session_retired(j.goal):
                continue

            tool_name = j.pending_action or "action"
            clean_goal = sanitize_briefing_text(j.goal)
            rec_id = f"job_confirmation:{j.id}:approval_pending"

            material_data = {
                "entity_id": j.id,
                "status": "WAITING_CONFIRMATION",
                "tool_name": tool_name,
            }
            fp = calculate_material_fingerprint(material_data)

            recs.append(
                Recommendation(
                    rec_id=rec_id,
                    category=RecommendationCategory.JOB_CONFIRMATION.value,
                    entity_id=j.id,
                    recommendation_type="approval_pending",
                    title=f"Approval Required: Job [{j.short_id}]",
                    explanation=f"Autonomous job [{j.short_id}] ('{clean_goal[:50]}') requires your approval before executing tool '{tool_name}'.",
                    suggested_action=f"Review and confirm or decline the pending action for job [{j.short_id}].",
                    priority=RecommendationPriority.HIGH,
                    evidence_fingerprint=fp,
                    evidence=[f"Job [{j.short_id}] is halted in WAITING_CONFIRMATION for tool '{tool_name}'."],
                )
            )
        return recs

    def _evaluate_repeated_failures(self, now: datetime) -> List[Recommendation]:
        """
        Evaluate verified repeated job failures.
        Requires:
        - attempt_count >= 2
        - status in RETRYING or FAILED
        - failure timestamp within last 48 hours
        - verified failure evidence in step or last_error
        - no subsequent successful attempt superseding the failure
        """
        recs = []
        if not self.job_store:
            return recs

        try:
            # Query retrying and failed jobs
            failed_candidates = self.job_store.list_jobs(status=JobStatus.FAILED, limit=50)
            retrying_candidates = self.job_store.list_jobs(status=JobStatus.RETRYING, limit=50)
            target_jobs = failed_candidates + retrying_candidates

            local_now = normalize_local_now(now)
            cutoff_48h = local_now - timedelta(hours=FAILURE_RECENCY_HOURS)

            for job in target_jobs:
                if is_session_retired(job.goal, "job") or is_session_retired(job.goal):
                    continue

                if job.attempt_count < 2:
                    continue

                # Verify failure recency (< 48h)
                fail_time_str = job.completed_at or job.updated_at
                fail_dt = _parse_timestamp(fail_time_str)
                if not fail_dt:
                    continue

                if fail_dt < cutoff_48h:
                    continue

                # Check steps for supporting failure evidence
                steps = self.job_store.get_steps(job.id)
                has_failure_evidence = bool(job.last_error) or any(s.success is False for s in steps)
                if not has_failure_evidence:
                    continue

                # Verify un-superseded: make sure no subsequent attempt had a success=True
                is_superseded = False
                for s in reversed(steps):
                    if s.success is True:
                        is_superseded = True
                        break
                    if s.success is False:
                        break

                if is_superseded:
                    continue

                clean_goal = sanitize_briefing_text(job.goal)
                raw_err = job.last_error or "Repeated execution failure"
                clean_err = sanitize_briefing_text(raw_err)
                rec_id = f"job_failure:{job.id}:repeated_failure"

                material_data = {
                    "entity_id": job.id,
                    "attempt_count": job.attempt_count,
                    "status": job.status.value,
                    "error_snippet": clean_err[:80],
                }
                fp = calculate_material_fingerprint(material_data)

                recs.append(
                    Recommendation(
                        rec_id=rec_id,
                        category=RecommendationCategory.JOB_FAILURE.value,
                        entity_id=job.id,
                        recommendation_type="repeated_failure",
                        title=f"Repeated Failure: Job [{job.short_id}]",
                        explanation=f"Job [{job.short_id}] ('{clean_goal[:45]}') failed {job.attempt_count} times. Last error: {clean_err}.",
                        suggested_action=f"Inspect error logs for job [{job.short_id}] and verify parameters before resuming.",
                        priority=RecommendationPriority.HIGH,
                        evidence_fingerprint=fp,
                        evidence=[
                            f"Job [{job.short_id}] attempt {job.attempt_count}/{job.max_attempts} failed within 48h.",
                            f"Error: {clean_err}",
                        ],
                    )
                )
        except Exception as e:
            logger.warning("[RECOMMENDATION EVALUATOR] Error evaluating repeated failures: %s", e)

        return recs

    def _evaluate_stuck_running_jobs(
        self,
        active_jobs: List[JobBriefingContext],
        now: datetime,
    ) -> List[Recommendation]:
        """Evaluate running jobs that have made zero progress for > 24 hours."""
        recs = []
        if not self.job_store:
            return recs

        local_now = normalize_local_now(now)
        cutoff_24h = local_now - timedelta(hours=STUCK_RUNNING_HOURS)

        for aj in active_jobs:
            if is_session_retired(aj.goal, "job") or is_session_retired(aj.goal):
                continue
            if aj.status != JobStatus.RUNNING.value:
                continue

            try:
                job = self.job_store.get_job(aj.id)
                if not job:
                    continue

                steps = self.job_store.get_steps(job.id)
                last_activity_dt = None
                if steps:
                    last_activity_dt = _parse_timestamp(steps[-1].started_at)
                if not last_activity_dt:
                    last_activity_dt = _parse_timestamp(job.started_at) or _parse_timestamp(job.updated_at)

                if last_activity_dt and last_activity_dt < cutoff_24h:
                    clean_goal = sanitize_briefing_text(job.goal)
                    rec_id = f"job_stuck:{job.id}:no_verified_progress"

                    material_data = {
                        "entity_id": job.id,
                        "status": "RUNNING",
                        "step_count": job.step_count,
                    }
                    fp = calculate_material_fingerprint(material_data)

                    recs.append(
                        Recommendation(
                            rec_id=rec_id,
                            category=RecommendationCategory.JOB_STUCK.value,
                            entity_id=job.id,
                            recommendation_type="no_verified_progress",
                            title=f"Stalled Job: [{job.short_id}]",
                            explanation=f"Running job [{job.short_id}] ('{clean_goal[:45]}') has recorded no progress steps for over 24 hours.",
                            suggested_action=f"Inspect background execution for job [{job.short_id}] or pause it if unresponsive.",
                            priority=RecommendationPriority.HIGH,
                            evidence_fingerprint=fp,
                            evidence=[f"Running job [{job.short_id}] idle for > 24 hours."],
                        )
                    )
            except Exception as e:
                logger.warning("[RECOMMENDATION EVALUATOR] Error checking running job %s: %s", aj.id, e)

        return recs

    def _evaluate_project_health(
        self,
        projects: List[ProjectBriefingContext],
        now: datetime,
    ) -> List[Recommendation]:
        """
        Evaluate project-level recommendations based strictly on authoritative ProjectHealthAssessment.
        Rules:
        - BLOCKED: suggest reviewing verified blocker.
        - PAUSED: suggest reviewing the pause; never automatically resumes work!
        - ACTIVE_NO_RECENT_PROGRESS: suggest reviewing priorities/milestones.
        - PROGRESSING: RESTRICTED; no unsolicited corrective recommendations!
        - UNKNOWN: RESTRICTED; observation only!
        """
        recs = []
        for p in projects:
            if is_session_retired(p.name, "project"):
                continue

            h = p.health
            if not h:
                continue

            p_key = normalize_project_key(p.name)

            # Rule: PROGRESSING projects are healthy -> zero corrective recommendations!
            if h.status == ProjectHealthStatus.PROGRESSING:
                continue

            # Rule: UNKNOWN status is observation only -> never recommend solely due to UNKNOWN!
            if h.status == ProjectHealthStatus.UNKNOWN:
                continue

            # Rule: COMPLETED status -> observation only
            if h.status == ProjectHealthStatus.COMPLETED:
                continue

            clean_pname = sanitize_briefing_text(p.name)
            clean_exp = sanitize_briefing_text(h.explanation)

            if h.status == ProjectHealthStatus.BLOCKED:
                rec_id = f"project_blocked:{p_key}:review_blocker"
                material_data = {
                    "project": p_key,
                    "status": "BLOCKED",
                    "recent_failure": h.recent_failure_info or "",
                    "jobs": sorted(h.associated_job_ids),
                }
                fp = calculate_material_fingerprint(material_data)

                recs.append(
                    Recommendation(
                        rec_id=rec_id,
                        category=RecommendationCategory.PROJECT_BLOCKED.value,
                        entity_id=p_key,
                        recommendation_type="review_blocker",
                        title=f"Blocked Project: {clean_pname}",
                        explanation=clean_exp,
                        suggested_action=f"Review blockers and resolve pending confirmations or failed jobs on '{clean_pname}'.",
                        priority=RecommendationPriority.HIGH,
                        evidence_fingerprint=fp,
                        evidence=h.evidence,
                    )
                )

            elif h.status == ProjectHealthStatus.PAUSED:
                rec_id = f"project_paused:{p_key}:review_pause"
                material_data = {
                    "project": p_key,
                    "status": "PAUSED",
                    "jobs": sorted(h.associated_job_ids),
                }
                fp = calculate_material_fingerprint(material_data)

                recs.append(
                    Recommendation(
                        rec_id=rec_id,
                        category=RecommendationCategory.PROJECT_PAUSED.value,
                        entity_id=p_key,
                        recommendation_type="review_pause",
                        title=f"Paused Work: {clean_pname}",
                        explanation=clean_exp,
                        suggested_action=f"Review paused job state for '{clean_pname}' and resume when ready.",
                        priority=RecommendationPriority.NORMAL,
                        evidence_fingerprint=fp,
                        evidence=h.evidence,
                    )
                )

            elif h.status == ProjectHealthStatus.ACTIVE_NO_RECENT_PROGRESS:
                rec_id = f"project_stalled:{p_key}:review_inactivity"
                material_data = {
                    "project": p_key,
                    "status": "ACTIVE_NO_RECENT_PROGRESS",
                    "last_progress": h.last_progress_at or "",
                }
                fp = calculate_material_fingerprint(material_data)

                recs.append(
                    Recommendation(
                        rec_id=rec_id,
                        category=RecommendationCategory.PROJECT_STALLED.value,
                        entity_id=p_key,
                        recommendation_type="review_inactivity",
                        title=f"Inactivity Advisory: {clean_pname}",
                        explanation=clean_exp,
                        suggested_action=f"Review active goals and milestones for '{clean_pname}'.",
                        priority=RecommendationPriority.NORMAL,
                        evidence_fingerprint=fp,
                        evidence=h.evidence,
                    )
                )

        return recs

    def _evaluate_overdue_reminders(
        self,
        overdue_reminders: List[ReminderBriefingContext],
        now: datetime,
    ) -> List[Recommendation]:
        """Evaluate overdue reminders as actionable commitment recommendations."""
        recs = []
        for rem in overdue_reminders:
            if is_session_retired(rem.message, "reminder") or is_session_retired(rem.message):
                continue
            if _is_sensitive_personal_goal(rem.message):
                continue

            clean_msg = sanitize_briefing_text(rem.message)
            rec_id = f"reminder_overdue:{rem.id}:review_commitment"

            material_data = {
                "reminder_id": rem.id,
                "due_at": rem.due_at,
                "message": clean_msg,
            }
            fp = calculate_material_fingerprint(material_data)

            recs.append(
                Recommendation(
                    rec_id=rec_id,
                    category=RecommendationCategory.REMINDER_OVERDUE.value,
                    entity_id=str(rem.id),
                    recommendation_type="review_commitment",
                    title=f"Overdue Commitment: '{clean_msg[:35]}'",
                    explanation=f"Reminder #{rem.id} ('{clean_msg}') was scheduled for {rem.due_at} and has passed its deadline.",
                    suggested_action=f"Address or reschedule overdue reminder #{rem.id}.",
                    priority=RecommendationPriority.NORMAL,
                    evidence_fingerprint=fp,
                    evidence=[f"Reminder #{rem.id} due at {rem.due_at} is overdue."],
                )
            )

        return recs


def match_recommendation_relevance(
    rec: Union[Recommendation, Dict[str, Any]],
    user_text: str,
) -> Tuple[bool, bool]:
    """
    Determine if a recommendation is relevant to user text and whether it was an explicit inquiry.
    Returns:
        (is_relevant, is_explicit_inquiry)
    Enforces Stage 3.7E.2D.1 rules:
    - Generic words alone ('task', 'tasks', 'project', 'reminder') MUST NOT match.
    - Projects: exact canonical project name or alias.
    - Jobs: exact job ID, short ID, or explicit mention of job goal.
    - Reminders: exact reminder ID (#id) or explicit message keywords.
    """
    clean_lower = user_text.lower()
    if isinstance(rec, Recommendation):
        cat = rec.category.lower()
        entity_id = str(rec.entity_id).lower()
        title = rec.title.lower()
        explanation = rec.explanation.lower()
    else:
        cat = str(rec.get("category", "")).lower()
        entity_id = str(rec.get("entity_id", "")).lower()
        title = str(rec.get("title", "")).lower()
        explanation = str(rec.get("explanation", "")).lower()

    # 1. Project category
    if "project" in cat:
        p_key = entity_id
        p_clean = p_key.replace("_", " ")
        if p_clean and (p_clean in clean_lower or p_key in clean_lower):
            return True, True
        return False, False

    # 2. Job category
    if "job" in cat:
        short_id = entity_id[:8] if len(entity_id) >= 8 else entity_id
        if entity_id in clean_lower:
            return True, True
        if short_id and len(short_id) >= 4 and short_id in clean_lower:
            return True, True

        # Check explicit mention of job goal extracted from explanation
        m = re.search(r"\(([^)]+)\)", explanation)
        if m:
            goal_text = m.group(1).strip().lower()
            if len(goal_text) >= 5 and goal_text in clean_lower:
                return True, True

        # Operational job queries (e.g. 'jobs running', 'running jobs', 'background jobs', 'any jobs running')
        if re.search(r"\b(?:jobs?\s+running|running\s+(?:right\s+now|jobs?)|background\s+jobs?|job\s+status|pending\s+jobs?)\b", clean_lower):
            return True, False

        return False, False

    # 3. Reminder category
    if "reminder" in cat:
        if f"#{entity_id}" in clean_lower or f"reminder {entity_id}" in clean_lower:
            return True, True

        m = re.search(r"reminder '([^']+)'", explanation)
        if m:
            rem_msg = m.group(1).strip().lower()
            if len(rem_msg) >= 4 and rem_msg in clean_lower:
                return True, True
            # Also match if all distinctive content keywords (>=4 chars) appear in query
            sig_words = [w for w in re.findall(r"\b[a-z]{4,}\b", rem_msg) if w not in ("submit", "review", "check", "reminder", "remind", "update")]
            if sig_words and all(w in clean_lower for w in sig_words):
                return True, True

        return False, False

    return False, False
