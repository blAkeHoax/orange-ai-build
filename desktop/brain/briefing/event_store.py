"""
brain/briefing/event_store.py - Event Watermark Persistence & Deduplication Foundation.

Stage 3.7E.3A: Watermark Persistence & Deduplication Foundation
Implements:
1. Versioned JSON persistence (event_watermarks.json) for operational event watermarks.
2. Cross-process (msvcrt / fcntl) and in-process (threading.RLock) mutual exclusion.
3. Reload-before-mutation concurrency mechanics to prevent cross-process lost updates.
4. Fail-closed handling for missing, malformed, or corrupt persistence files with non-destructive backups.
5. Explicit first-run baseline initialization (normal load never silently creates or seeds state).
6. Deterministic, cryptographic event identity generation (SHA-256) immune to presentation/scan wording.
7. Atomic writes using temporary files and atomic replacement.
8. Bounded 30-day retention pruning with injected clock support and watermark preservation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from typing import Optional, Dict, Any, List, Set, Tuple, Union

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

logger = logging.getLogger(__name__)

EVENT_WATERMARKS_FILENAME = "event_watermarks.json"
EVENT_STORE_SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
DEFAULT_LOCK_TIMEOUT = 5.0

# Canonical watermark keys recognized by Stage 3.7E.3
WATERMARK_JOBS_COMPLETED_AT = "jobs_completed_at"
WATERMARK_JOB_STEP_MAX_ID = "job_step_max_id"
WATERMARK_MEMORIES_UPDATED_AT = "memories_updated_at"
WATERMARK_LAST_STARTUP_RECONCILIATION = "last_startup_reconciliation"

CANONICAL_WATERMARK_KEYS = frozenset({
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
})

# Known incidental fields that must never influence event identity hashes
INCIDENTAL_METADATA_KEYS = frozenset({
    "scanned_at",
    "scan_time",
    "scan_timestamp",
    "briefing_date",
    "briefing_id",
    "presentation_text",
    "presentation_wording",
    "llm_summary",
    "llm_wording",
    "display_text",
    "synthesized_at",
    "viewed_at",
    "ui_channel",
    "turn_count",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EventStoreError(Exception):
    """Base exception for all event watermark store errors."""
    pass


class EventStoreNotFoundError(EventStoreError, FileNotFoundError):
    """Raised when the watermark store file does not exist during a normal load."""
    pass


class EventStoreCorruptError(EventStoreError):
    """Raised when watermark store contents cannot be parsed or are severely malformed."""
    pass


class EventStoreSchemaError(EventStoreError):
    """Raised when the watermark store has an unsupported version or invalid field types."""
    pass


class EventStoreLockTimeoutError(EventStoreError, TimeoutError):
    """Raised when lock acquisition times out across threads or processes."""
    pass


# ---------------------------------------------------------------------------
# Time Normalization Utilities
# ---------------------------------------------------------------------------

def normalize_utc_timestamp(dt_or_str: Optional[Union[datetime, str]]) -> Optional[str]:
    """
    Normalize a datetime object or ISO string into a canonical ISO 8601 UTC string.
    Returns None if input is None.
    """
    if dt_or_str is None:
        return None
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str
        if dt.tzinfo is None:
            # Assume local naive datetime, convert to UTC
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    if isinstance(dt_or_str, str):
        raw = dt_or_str.strip()
        if not raw:
            return None
        # Parse ISO string
        try:
            # Handle standard ISO with space or T
            normalized = raw.replace(" ", "T")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat()
        except Exception as e:
            raise ValueError(f"Invalid ISO 8601 timestamp string: '{raw}': {e}")
    raise TypeError(f"Expected datetime, str, or None, got {type(dt_or_str).__name__}")


def parse_utc_timestamp(iso_str: Optional[str]) -> Optional[datetime]:
    """Parse a normalized ISO string into a timezone-aware UTC datetime."""
    if not iso_str:
        return None
    norm = iso_str.strip().replace(" ", "T")
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Cross-Process File Lock
# ---------------------------------------------------------------------------

class CrossProcessFileLock:
    """
    Standard-library cross-process mutual exclusion file lock.
    Windows-compatible via msvcrt.locking; POSIX-compatible fallback via fcntl.flock.
    Bounded acquisition timeout with reliable exception cleanup.
    """

    def __init__(self, lock_path: str, timeout: float = DEFAULT_LOCK_TIMEOUT):
        self.lock_path = lock_path
        self.timeout = max(0.1, float(timeout))
        self.fd: Optional[int] = None
        self._depth = 0

    def acquire(self) -> bool:
        """Acquire the file lock within the timeout window. Re-entrant for the same instance."""
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
                elapsed = time.time() - start_time
                if elapsed >= self.timeout:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise EventStoreLockTimeoutError(
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


# ---------------------------------------------------------------------------
# Models & Event Identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventPayload:
    """
    Semantic payload defining an operational event.
    Presentation phrasing, scan timestamps, and UI details must never be included.
    """
    event_type: str
    entity_id: str
    material_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveredEventRecord:
    """Record of a successfully delivered event with deduplication metadata."""
    event_id: str
    delivered_at: str
    event_type: Optional[str] = None
    entity_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "delivered_at": self.delivered_at,
            "event_type": self.event_type,
            "entity_id": self.entity_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Union[Dict[str, Any], str], event_id_fallback: str = "") -> "DeliveredEventRecord":
        if isinstance(data, str):
            # Backward-compatible simple timestamp string
            return cls(
                event_id=event_id_fallback,
                delivered_at=data,
            )
        if isinstance(data, dict):
            return cls(
                event_id=data.get("event_id") or event_id_fallback,
                delivered_at=data.get("delivered_at", ""),
                event_type=data.get("event_type"),
                entity_id=data.get("entity_id"),
                metadata=data.get("metadata", {}) or {},
            )
        raise TypeError(f"Invalid delivered event record format: {type(data).__name__}")


@dataclass(frozen=True)
class EventStoreSnapshot:
    """
    Immutable read-only snapshot of the watermark store state.
    Allows inspection without mutating store state.
    """
    version: int
    created_at: str
    updated_at: str
    is_first_run_baseline: bool
    watermarks: Dict[str, Any]
    delivered_events: Dict[str, DeliveredEventRecord]

    def get_watermark(self, key: str, default: Any = None) -> Any:
        return self.watermarks.get(key, default)

    def is_delivered(self, event_id: str) -> bool:
        return event_id in self.delivered_events

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "is_first_run_baseline": self.is_first_run_baseline,
            "watermarks": dict(self.watermarks),
            "delivered_events": {k: v.to_dict() for k, v in self.delivered_events.items()},
        }


def clean_material_data(raw_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Clean and normalize material evidence dictionary:
    - Filters out known incidental keys (scan times, briefing dates, phrasing, summaries).
    - Recursively cleans nested dicts/lists.
    - Normalizes datetime objects to canonical UTC ISO strings.
    """
    if not raw_data:
        return {}
    if not isinstance(raw_data, dict):
        raise TypeError(f"material_data must be a dict, got {type(raw_data).__name__}")

    cleaned: Dict[str, Any] = {}
    for k, v in raw_data.items():
        k_str = str(k).strip()
        if k_str.lower() in INCIDENTAL_METADATA_KEYS:
            continue
        cleaned[k_str] = _normalize_value(v)
    return cleaned


def _normalize_value(val: Any) -> Any:
    """Helper to recursively normalize arbitrary values for deterministic serialization."""
    if isinstance(val, datetime):
        return normalize_utc_timestamp(val)
    if isinstance(val, dict):
        return clean_material_data(val)
    if isinstance(val, (list, tuple, set)):
        return [_normalize_value(item) for item in val]
    if isinstance(val, (int, float, bool)) or val is None:
        return val
    return str(val).strip()


def generate_event_id(
    event_type: str,
    entity_id: str,
    material_data: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generate a deterministic, cryptographic event identity string (SHA-256).
    
    Guarantees:
    - The same semantic event produces the exact same ID across scans and restarts.
    - Incidental timestamps (scan time, briefing date, presentation wording) are filtered.
    - Material changes produce distinct IDs.
    - Uses SHA-256 (never Python's non-deterministic hash()).
    - Returns a prefixed string: evt_{event_type}_{hash[:20]}.
    """
    if not event_type or not str(event_type).strip():
        raise ValueError("event_type must be a non-empty string.")
    if not entity_id or not str(entity_id).strip():
        raise ValueError("entity_id must be a non-empty string.")

    norm_type = re.sub(r"[^\w-]", "_", str(event_type).strip().lower()).strip("_")
    norm_entity = str(entity_id).strip()
    clean_mat = clean_material_data(material_data)

    serialized_mat = json.dumps(
        clean_mat,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    seed_string = f"{norm_type}:{norm_entity}:{serialized_mat}"
    digest = hashlib.sha256(seed_string.encode("utf-8")).hexdigest()
    return f"evt_{norm_type}_{digest[:20]}"


# ---------------------------------------------------------------------------
# Event Watermark Store
# ---------------------------------------------------------------------------

class EventWatermarkStore:
    """
    Persistent JSON store for Event Watermarks and Deduplication IDs.
    
    Adheres strictly to Stage 3.7E.3A specifications:
    - Dedicated JSON file in get_project_root() (event_watermarks.json).
    - Companion cross-process lock file (event_watermarks.json.lock) via CrossProcessFileLock.
    - In-process thread-safety via threading.RLock.
    - Fail-closed: missing file on normal load raises EventStoreNotFoundError.
    - Explicit first-run initialization via initialize_first_run().
    - Non-destructive backup on corruption (never overwrites existing backups).
    - Reload-before-mutation concurrency mechanics.
    - Atomic writes via temporary file and atomic replacement.
    - Bounded retention pruning with watermark preservation.
    """

    def __init__(self, file_path: Optional[str] = None):
        self.file_path = file_path or os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)
        self.lock_path = f"{self.file_path}.lock"
        self._lock = threading.RLock()
        self._file_lock = CrossProcessFileLock(self.lock_path, timeout=DEFAULT_LOCK_TIMEOUT)
        
        self._version = EVENT_STORE_SCHEMA_VERSION
        self._created_at: str = ""
        self._updated_at: str = ""
        self._is_first_run_baseline: bool = False
        self._watermarks: Dict[str, Any] = {
            WATERMARK_JOBS_COMPLETED_AT: None,
            WATERMARK_JOB_STEP_MAX_ID: 0,
            WATERMARK_MEMORIES_UPDATED_AT: None,
            WATERMARK_LAST_STARTUP_RECONCILIATION: None,
        }
        self._delivered_events: Dict[str, DeliveredEventRecord] = {}
        self._is_corrupt = False

    # -----------------------------------------------------------------------
    # Factory Methods
    # -----------------------------------------------------------------------

    @classmethod
    def load(cls, file_path: Optional[str] = None) -> "EventWatermarkStore":
        """
        Load an existing watermark store from disk.
        
        Fail-closed rules:
        - If file does not exist: raises EventStoreNotFoundError.
        - If JSON is malformed: creates non-destructive backup and raises EventStoreCorruptError.
        - If schema/types are invalid: creates non-destructive backup and raises EventStoreSchemaError.
        - Does NOT silently create an empty store or initialize timestamps to now.
        """
        store = cls(file_path=file_path)
        if not os.path.exists(store.file_path):
            raise EventStoreNotFoundError(
                f"Event watermark store not found at '{store.file_path}'. "
                "Explicit first-run baseline initialization is required via initialize_first_run()."
            )
        store._load_and_validate(reload=True)
        return store

    @classmethod
    def initialize_first_run(
        cls,
        file_path: Optional[str] = None,
        initial_watermarks: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> "EventWatermarkStore":
        """
        Explicitly establish a first-run baseline store.
        
        Requirements:
        - Detects if file already exists; raises FileExistsError to prevent accidental overwrite.
        - Creates a documented baseline state marking is_first_run_baseline=True.
        - Does not pretend historical events were scanned or delivered.
        - Atomically writes initial state to disk under both thread and file locks.
        """
        store = cls(file_path=file_path)
        with store.lock(reload=False):
            if os.path.exists(store.file_path):
                raise FileExistsError(
                    f"Event watermark store already exists at '{store.file_path}'. "
                    "Cannot re-initialize an existing store as a first-run baseline."
                )

            now_iso = normalize_utc_timestamp(now or datetime.now(timezone.utc))
            store._version = EVENT_STORE_SCHEMA_VERSION
            store._created_at = now_iso
            store._updated_at = now_iso
            store._is_first_run_baseline = True
            store._is_corrupt = False

            # Configure baseline watermarks
            init_w = initial_watermarks or {}
            store._watermarks = {
                WATERMARK_JOBS_COMPLETED_AT: normalize_utc_timestamp(init_w.get(WATERMARK_JOBS_COMPLETED_AT)),
                WATERMARK_JOB_STEP_MAX_ID: int(init_w.get(WATERMARK_JOB_STEP_MAX_ID, 0)),
                WATERMARK_MEMORIES_UPDATED_AT: normalize_utc_timestamp(init_w.get(WATERMARK_MEMORIES_UPDATED_AT)),
                WATERMARK_LAST_STARTUP_RECONCILIATION: normalize_utc_timestamp(init_w.get(WATERMARK_LAST_STARTUP_RECONCILIATION)),
            }
            # Copy any custom canonical watermarks passed
            for k, v in init_w.items():
                if k not in store._watermarks:
                    store._watermarks[k] = v

            store._delivered_events = {}
            success = store._save_unlocked()
            if not success:
                raise EventStoreError(f"Failed to atomically initialize first-run baseline at '{store.file_path}'.")

        logger.info("[EVENT STORE] Successfully initialized first-run baseline at '%s'.", store.file_path)
        return store

    # -----------------------------------------------------------------------
    # Locking & Context Management
    # -----------------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, reload: bool = True):
        """
        Acquire in-process thread lock and cross-process file lock.
        When reload=True, reloads latest disk state to ensure fresh state across processes.
        """
        with self._lock:
            with self._file_lock:
                if reload and os.path.exists(self.file_path):
                    self._load_and_validate(reload=True)
                yield self

    # -----------------------------------------------------------------------
    # Loading, Validation & Corruption Handling
    # -----------------------------------------------------------------------

    def _load_and_validate(self, reload: bool = True) -> None:
        """
        Internal load and validation routine. Must be invoked under lock.
        Parses JSON and verifies all required schema fields and types.
        On failure: creates a timestamped backup without overwriting and raises.
        """
        if not os.path.exists(self.file_path):
            raise EventStoreNotFoundError(f"Event watermark file does not exist: '{self.file_path}'")

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as read_err:
            self._handle_corrupt_file(f"Read error: {read_err}")
            raise EventStoreCorruptError(f"Could not read event store file: {read_err}") from read_err

        try:
            data = json.loads(content)
        except json.JSONDecodeError as json_err:
            self._handle_corrupt_file(f"JSON decode error: {json_err}")
            raise EventStoreCorruptError(f"Malformed JSON in event store: {json_err}") from json_err

        if not isinstance(data, dict):
            self._handle_corrupt_file("Root JSON element is not an object.")
            raise EventStoreSchemaError("Root JSON element must be an object.")

        # 1. Validate version
        version = data.get("version")
        if version != EVENT_STORE_SCHEMA_VERSION:
            self._handle_corrupt_file(f"Unsupported version {version} (expected {EVENT_STORE_SCHEMA_VERSION})")
            raise EventStoreSchemaError(
                f"Unsupported event store schema version: {version}. Expected {EVENT_STORE_SCHEMA_VERSION}."
            )

        # 2. Validate timestamps
        created_at = data.get("created_at")
        updated_at = data.get("updated_at")
        if not isinstance(created_at, str) or not created_at.strip():
            self._handle_corrupt_file("Missing or invalid 'created_at'.")
            raise EventStoreSchemaError("Field 'created_at' must be a non-empty ISO 8601 string.")
        if not isinstance(updated_at, str) or not updated_at.strip():
            self._handle_corrupt_file("Missing or invalid 'updated_at'.")
            raise EventStoreSchemaError("Field 'updated_at' must be a non-empty ISO 8601 string.")

        # 3. Validate baseline flag
        is_first_run_baseline = data.get("is_first_run_baseline")
        if not isinstance(is_first_run_baseline, bool):
            self._handle_corrupt_file("Field 'is_first_run_baseline' must be boolean.")
            raise EventStoreSchemaError("Field 'is_first_run_baseline' must be boolean.")

        # 4. Validate watermarks dictionary
        raw_watermarks = data.get("watermarks")
        if not isinstance(raw_watermarks, dict):
            self._handle_corrupt_file("'watermarks' field is not an object.")
            raise EventStoreSchemaError("Field 'watermarks' must be an object.")

        # Type-check individual watermarks
        step_id = raw_watermarks.get(WATERMARK_JOB_STEP_MAX_ID, 0)
        if not isinstance(step_id, int) or step_id < 0:
            self._handle_corrupt_file(f"Invalid '{WATERMARK_JOB_STEP_MAX_ID}': {step_id}")
            raise EventStoreSchemaError(f"Watermark '{WATERMARK_JOB_STEP_MAX_ID}' must be an integer >= 0.")

        # Check timestamp watermarks if present
        for ts_key in (WATERMARK_JOBS_COMPLETED_AT, WATERMARK_MEMORIES_UPDATED_AT, WATERMARK_LAST_STARTUP_RECONCILIATION):
            val = raw_watermarks.get(ts_key)
            if val is not None and not isinstance(val, str):
                self._handle_corrupt_file(f"Watermark '{ts_key}' must be string or null.")
                raise EventStoreSchemaError(f"Watermark '{ts_key}' must be an ISO 8601 string or null.")

        # 5. Validate delivered events (supports both delivered_events and legacy delivered_event_ids)
        raw_events = data.get("delivered_events")
        if raw_events is None:
            raw_events = data.get("delivered_event_ids", {})
        if not isinstance(raw_events, dict):
            self._handle_corrupt_file("'delivered_events' must be an object.")
            raise EventStoreSchemaError("Field 'delivered_events' must be an object.")

        parsed_events: Dict[str, DeliveredEventRecord] = {}
        for evt_id, evt_val in raw_events.items():
            if not isinstance(evt_id, str) or not evt_id.strip():
                self._handle_corrupt_file(f"Invalid event key in delivered_events: {evt_id}")
                raise EventStoreSchemaError("Delivered event key must be a non-empty string.")
            try:
                parsed_events[evt_id] = DeliveredEventRecord.from_dict(evt_val, event_id_fallback=evt_id)
            except Exception as rec_err:
                self._handle_corrupt_file(f"Invalid record for event '{evt_id}': {rec_err}")
                raise EventStoreSchemaError(f"Invalid delivered event record for '{evt_id}': {rec_err}")

        # Successfully validated
        self._version = version
        self._created_at = created_at
        self._updated_at = updated_at
        self._is_first_run_baseline = is_first_run_baseline
        self._watermarks = dict(raw_watermarks)
        self._delivered_events = parsed_events
        self._is_corrupt = False

    def _handle_corrupt_file(self, reason: str) -> None:
        """
        Create a non-destructive timestamped backup of the corrupt file.
        Guarantees that an existing backup is never overwritten.
        """
        self._is_corrupt = True
        if not os.path.exists(self.file_path):
            return

        base_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{self.file_path}.corrupt.{base_ts}"
        counter = 1
        while os.path.exists(backup_path):
            backup_path = f"{self.file_path}.corrupt.{base_ts}_{counter}"
            counter += 1

        try:
            shutil.copy2(self.file_path, backup_path)
            logger.critical(
                "[EVENT STORE] Corrupt watermark store detected: %s. Preserved backup at '%s'.",
                reason,
                backup_path,
            )
        except Exception as copy_err:
            logger.critical(
                "[EVENT STORE] Failed to create backup for corrupt file '%s': %s",
                self.file_path,
                copy_err,
            )

    # -----------------------------------------------------------------------
    # Persistence & Atomic Writes
    # -----------------------------------------------------------------------

    def save(self) -> bool:
        """Persist state atomically to disk under both thread and file locks."""
        with self._lock:
            with self._file_lock:
                return self._save_unlocked()

    def _save_unlocked(self) -> bool:
        """
        Internal save implementation without acquiring locks.
        Writes to a uniquely-named temporary file and performs atomic replacement.
        """
        if self._is_corrupt:
            logger.error("[EVENT STORE] Cannot save: Store is in fail-closed corrupt state.")
            return False

        payload = {
            "version": self._version,
            "created_at": self._created_at,
            "updated_at": self._updated_at,
            "is_first_run_baseline": self._is_first_run_baseline,
            "watermarks": dict(self._watermarks),
            "delivered_events": {k: v.to_dict() for k, v in self._delivered_events.items()},
        }

        # Unique temporary file to avoid collisions between threads/processes
        pid = os.getpid()
        tid = threading.get_ident()
        ts_ms = int(time.time() * 1000)
        temp_path = f"{self.file_path}.tmp.{pid}_{tid}_{ts_ms}"

        try:
            parent_dir = os.path.dirname(os.path.abspath(self.file_path))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replacement with retries for Windows file-system locking
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    os.replace(temp_path, self.file_path)
                    return True
                except PermissionError:
                    if attempt < max_retries - 1:
                        time.sleep(0.02 * (attempt + 1))
                    else:
                        raise
        except Exception as e:
            logger.error("[EVENT STORE] Atomic save failed: %s", e)
            return False
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # Public Watermark API
    # -----------------------------------------------------------------------

    def get_snapshot(self) -> EventStoreSnapshot:
        """
        Return an immutable snapshot of the watermark store.
        Snapshot reads do not mutate state.
        """
        with self._lock:
            return EventStoreSnapshot(
                version=self._version,
                created_at=self._created_at,
                updated_at=self._updated_at,
                is_first_run_baseline=self._is_first_run_baseline,
                watermarks=dict(self._watermarks),
                delivered_events=dict(self._delivered_events),
            )

    def get_watermark(self, name: str, default: Any = None) -> Any:
        """Read a single watermark value under thread lock."""
        with self._lock:
            return self._watermarks.get(name, default)

    def is_delivered(self, event_id: str) -> bool:
        """Query whether an event ID has already been recorded as delivered."""
        with self._lock:
            return event_id in self._delivered_events

    def advance_watermark(
        self,
        name: str,
        value: Any,
        allow_regression: bool = False,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Atomically advance a single watermark position under lock with reload.
        
        Monotonicity rules:
        - For integer watermarks (e.g., job_step_max_id): value must be >= current unless allow_regression=True.
        - For ISO timestamp watermarks: new timestamp must be >= current timestamp unless allow_regression=True.
        - Merely discovering an event must NOT call this method; call only after confirmed delivery.
        """
        return self.advance_watermarks({name: value}, allow_regression=allow_regression, now=now)

    def advance_watermarks(
        self,
        updates: Dict[str, Any],
        allow_regression: bool = False,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Atomically advance multiple watermark positions under both thread and file locks.
        Reloads the latest disk state before applying modifications.
        """
        if not updates:
            return True

        with self.lock(reload=True):
            if self._is_corrupt:
                raise EventStoreCorruptError("Cannot advance watermarks: Store is in corrupt state.")

            for name, val in updates.items():
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"Invalid watermark name: {name}")

                current = self._watermarks.get(name)

                # Integer watermarks
                if isinstance(val, int):
                    if current is not None and isinstance(current, int) and val < current and not allow_regression:
                        raise ValueError(
                            f"Monotonic regression rejected for watermark '{name}': new {val} < current {current}"
                        )
                    self._watermarks[name] = val

                # Timestamp watermarks
                elif isinstance(val, (datetime, str)):
                    norm_new = normalize_utc_timestamp(val)
                    if norm_new is None:
                        raise ValueError(f"Cannot set watermark '{name}' to empty timestamp.")

                    if current is not None and not allow_regression:
                        current_dt = parse_utc_timestamp(str(current))
                        new_dt = parse_utc_timestamp(norm_new)
                        if current_dt and new_dt and new_dt < current_dt:
                            raise ValueError(
                                f"Monotonic timestamp regression rejected for '{name}': {norm_new} < {current}"
                            )
                    self._watermarks[name] = norm_new

                elif val is None:
                    if not allow_regression and current is not None:
                        raise ValueError(f"Cannot clear watermark '{name}' to None without allow_regression=True.")
                    self._watermarks[name] = None
                else:
                    self._watermarks[name] = val

            self._updated_at = normalize_utc_timestamp(now or datetime.now(timezone.utc))
            # After an explicit advancement, this is no longer just a raw first-run baseline
            if self._is_first_run_baseline:
                self._is_first_run_baseline = False

            return self._save_unlocked()

    def record_delivery(
        self,
        event_id: str,
        delivered_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Record confirmed delivery of a single event ID under lock with reload.
        
        Semantics:
        - Idempotent: returns False if event_id is already recorded.
        - Confirmed delivery only: callers must invoke only AFTER delivery succeeds.
        """
        results = self.record_deliveries(
            [(event_id, metadata)],
            delivered_at=delivered_at,
            now=now,
        )
        return results == 1

    def record_deliveries(
        self,
        events: List[Tuple[str, Optional[Dict[str, Any]]]],
        delivered_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> int:
        """
        Record confirmed delivery of multiple event IDs in a single atomic transaction.
        Returns the count of newly recorded event IDs.
        """
        if not events:
            return 0

        with self.lock(reload=True):
            if self._is_corrupt:
                raise EventStoreCorruptError("Cannot record delivery: Store is in corrupt state.")

            deliv_time = normalize_utc_timestamp(delivered_at or now or datetime.now(timezone.utc))
            new_records = 0

            for item in events:
                if isinstance(item, tuple):
                    evt_id, meta = item[0], item[1] if len(item) > 1 else None
                else:
                    evt_id, meta = str(item), None

                if not evt_id or not str(evt_id).strip():
                    continue

                clean_id = str(evt_id).strip()
                if clean_id in self._delivered_events:
                    continue  # Already recorded

                cleaned_meta = clean_material_data(meta) if meta else {}
                rec = DeliveredEventRecord(
                    event_id=clean_id,
                    delivered_at=deliv_time,
                    event_type=cleaned_meta.get("event_type"),
                    entity_id=cleaned_meta.get("entity_id"),
                    metadata=cleaned_meta,
                )
                self._delivered_events[clean_id] = rec
                new_records += 1

            if new_records > 0:
                self._updated_at = normalize_utc_timestamp(now or datetime.now(timezone.utc))
                if self._is_first_run_baseline:
                    self._is_first_run_baseline = False
                self._save_unlocked()

            return new_records

    def prune_delivered_events(
        self,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        now: Optional[datetime] = None,
    ) -> int:
        """
        Prune deduplication records older than the retention window (default 30 days).
        
        Guarantees:
        - Pruning is deterministic based on the provided or current time.
        - Watermarks are NEVER pruned or modified by this method.
        - Recent event IDs within the retention window are preserved.
        - Operates under both thread and file locks with atomic save.
        """
        retention_days = max(1, int(retention_days))
        ref_dt = parse_utc_timestamp(normalize_utc_timestamp(now or datetime.now(timezone.utc)))
        if not ref_dt:
            return 0

        cutoff = ref_dt - timedelta(days=retention_days)

        with self.lock(reload=True):
            if self._is_corrupt:
                raise EventStoreCorruptError("Cannot prune: Store is in corrupt state.")

            to_remove: List[str] = []
            for evt_id, rec in self._delivered_events.items():
                deliv_dt = parse_utc_timestamp(rec.delivered_at)
                if deliv_dt and deliv_dt < cutoff:
                    to_remove.append(evt_id)

            if not to_remove:
                return 0

            for evt_id in to_remove:
                del self._delivered_events[evt_id]

            self._updated_at = normalize_utc_timestamp(now or datetime.now(timezone.utc))
            self._save_unlocked()
            logger.info("[EVENT STORE] Pruned %d delivered event records older than %d days.", len(to_remove), retention_days)
            return len(to_remove)
