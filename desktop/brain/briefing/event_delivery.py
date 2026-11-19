"""
brain/briefing/event_delivery.py - Delivery Orchestration & Safe Watermark Advancement.

Stage 3.7E.3C.1: Delivery Orchestration & Safe Watermark Advancement
Implements:
1. Explicit first-run watermark baseline initialization based on authoritative source snapshots.
2. In-process delivery contract distinguishing Confirmed Success, Failure, and Ambiguous outcomes.
3. Candidate consumption from EventCandidateReader with deterministic deduplication and sorting.
4. Confirmed delivery recording into EventWatermarkStore (only after verified success).
5. Safe contiguous watermark advancement that never skips undelivered events, handles equal
   timestamps without peer skipping, and keeps source watermarks strictly independent.
6. Fail-closed error handling: missing/corrupt store, delivery recording failures, and lock timeouts.
7. Zero GUI, scheduler, background polling, or external API dependencies.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set, Union, Callable

from brain.briefing.models import (
    EventCandidate,
    EventPriority,
    DeliveryStatus,
    DeliveryResult,
    EventDeliveryBatchResult,
)
from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreError,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EventStoreSchemaError,
    EventStoreLockTimeoutError,
    normalize_utc_timestamp,
    parse_utc_timestamp,
    EVENT_WATERMARKS_FILENAME,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.event_readers import (
    EventCandidateReader,
    sort_candidates,
    safe_parse_utc,
    safe_normalize_utc,
)
from brain.memory import get_db_path
from brain.paths import get_project_root

logger = logging.getLogger(__name__)

DEFAULT_BATCH_LIMIT = 50


# ---------------------------------------------------------------------------
# First-Run Baseline Initialization
# ---------------------------------------------------------------------------

def inspect_source_high_waters(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Inspect authoritative source database records to establish pre-baseline positions:
    - jobs_completed_at: MAX(jobs.completed_at) from persistent jobs, or None if none.
    - job_step_max_id: MAX(job_steps.id), or 0 if none.
    - memories_updated_at: MAX(structured_memories.updated_at), or None if none.
    - last_startup_reconciliation: left unset (None) for Stage 3.7E.3C.2.
    
    Zero mutations: Queries strictly in read-only mode (`?mode=ro`).
    """
    path = db_path or get_db_path()
    if not os.path.exists(path):
        return {
            WATERMARK_JOBS_COMPLETED_AT: None,
            WATERMARK_JOB_STEP_MAX_ID: 0,
            WATERMARK_MEMORIES_UPDATED_AT: None,
            WATERMARK_LAST_STARTUP_RECONCILIATION: None,
        }

    uri_path = path.replace("\\", "/")
    if not uri_path.startswith("/"):
        uri_path = "/" + uri_path
    uri = f"file://{uri_path}?mode=ro"

    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        cur = conn.cursor()

        # 1. jobs_completed_at: MAX(COALESCE(completed_at, updated_at)) for terminal jobs (COMPLETED, FAILED)
        cur.execute("""
            SELECT MAX(COALESCE(completed_at, updated_at))
            FROM jobs
            WHERE status IN ('COMPLETED', 'FAILED');
        """)
        row_job = cur.fetchone()
        raw_job_ts = row_job[0] if row_job and row_job[0] else None
        job_comp_at = safe_normalize_utc(raw_job_ts)

        # 2. job_step_max_id
        cur.execute("SELECT MAX(id) FROM job_steps;")
        row_step = cur.fetchone()
        step_max_id = int(row_step[0]) if row_step and row_step[0] is not None else 0

        # 3. memories_updated_at: MAX(COALESCE(NULLIF(updated_at, ''), created_at))
        cur.execute("SELECT MAX(COALESCE(NULLIF(updated_at, ''), created_at)) FROM structured_memories;")
        row_mem = cur.fetchone()
        raw_mem_ts = row_mem[0] if row_mem and row_mem[0] else None
        mem_updated_at = safe_normalize_utc(raw_mem_ts)

        return {
            WATERMARK_JOBS_COMPLETED_AT: job_comp_at,
            WATERMARK_JOB_STEP_MAX_ID: step_max_id,
            WATERMARK_MEMORIES_UPDATED_AT: mem_updated_at,
            WATERMARK_LAST_STARTUP_RECONCILIATION: None,
        }
    except Exception as e:
        logger.error("[EVENT DELIVERY] Error inspecting source high-waters: %s", e)
        raise
    finally:
        conn.close()


def initialize_event_store_baseline(
    file_path: Optional[str] = None,
    db_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> EventWatermarkStore:
    """
    Explicitly initialize the event watermark store with current database high-waters.
    
    Guarantees:
    - Never automatically invoked during normal load or candidate scanning.
    - Existing records prior to initialization are marked as pre-baseline positions
      so historical floods are avoided without falsely recording past deliveries.
    - Idempotent overwrite prevention: raises FileExistsError if the watermark file already exists.
    - Atomically writes baseline under cross-process lock via EventWatermarkStore.
    """
    store_file = file_path or os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)
    if os.path.exists(store_file):
        raise FileExistsError(
            f"Event watermark store already exists at '{store_file}'. "
            "Refusing to overwrite existing watermark store."
        )

    high_waters = inspect_source_high_waters(db_path=db_path)
    now_dt = now or datetime.now(timezone.utc)
    store = EventWatermarkStore.initialize_first_run(
        file_path=store_file,
        initial_watermarks=high_waters,
        now=now_dt,
    )
    logger.info(
        "[EVENT DELIVERY] Explicitly initialized event watermark baseline at '%s' with positions: %s",
        store_file,
        high_waters,
    )
    return store


# ---------------------------------------------------------------------------
# Delivery Adapter Abstraction
# ---------------------------------------------------------------------------

class BaseDeliveryAdapter:
    """
    Abstract contract for candidate event delivery.
    Implementations must return an explicit DeliveryResult distinguishing:
    - SUCCESS: Confirmed delivered
    - FAILURE: Explicit failure (retryable)
    - AMBIGUOUS: Timeout, exception, or undetermined outcome (retryable, not recorded)
    """

    def deliver(self, candidate: EventCandidate) -> DeliveryResult:
        raise NotImplementedError


class InProcessDeliveryAdapter(BaseDeliveryAdapter):
    """
    Configurable in-process delivery adapter for deterministic testing and orchestration.
    Invokes an injected callback or maintains an internal delivery log.
    """

    def __init__(
        self,
        handler: Optional[Callable[[EventCandidate], Union[DeliveryResult, DeliveryStatus, bool]]] = None,
        channel_name: str = "in_process",
    ):
        self.handler = handler
        self.channel_name = channel_name
        self.delivered_candidates: List[EventCandidate] = []

    def deliver(self, candidate: EventCandidate) -> DeliveryResult:
        if self.handler is None:
            # Default behavior: record candidate in-memory and succeed
            self.delivered_candidates.append(candidate)
            return DeliveryResult(
                event_id=candidate.event_id,
                status=DeliveryStatus.SUCCESS,
                channel=self.channel_name,
                delivered_at=datetime.now(timezone.utc).isoformat(),
            )

        try:
            res = self.handler(candidate)
            if isinstance(res, DeliveryResult):
                if res.status == DeliveryStatus.SUCCESS:
                    self.delivered_candidates.append(candidate)
                return res

            if isinstance(res, DeliveryStatus):
                if res == DeliveryStatus.SUCCESS:
                    self.delivered_candidates.append(candidate)
                return DeliveryResult(
                    event_id=candidate.event_id,
                    status=res,
                    channel=self.channel_name,
                    delivered_at=datetime.now(timezone.utc).isoformat() if res == DeliveryStatus.SUCCESS else None,
                )

            if isinstance(res, bool):
                status = DeliveryStatus.SUCCESS if res else DeliveryStatus.FAILURE
                if status == DeliveryStatus.SUCCESS:
                    self.delivered_candidates.append(candidate)
                return DeliveryResult(
                    event_id=candidate.event_id,
                    status=status,
                    channel=self.channel_name,
                    delivered_at=datetime.now(timezone.utc).isoformat() if res else None,
                )

            # Missing or unrecognized return type is strictly AMBIGUOUS
            return DeliveryResult(
                event_id=candidate.event_id,
                status=DeliveryStatus.AMBIGUOUS,
                error=f"Delivery handler returned unexpected type: {type(res).__name__}",
                channel=self.channel_name,
            )
        except Exception as e:
            logger.warning("[EVENT DELIVERY] Delivery handler raised exception for '%s': %s", candidate.event_id, e)
            return DeliveryResult(
                event_id=candidate.event_id,
                status=DeliveryStatus.AMBIGUOUS,
                error=str(e),
                channel=self.channel_name,
            )


# ---------------------------------------------------------------------------
# Safe Watermark Advancement Algorithm
# ---------------------------------------------------------------------------

def calculate_safe_watermark_advancements(
    all_candidates: List[EventCandidate],
    confirmed_delivered_ids: Set[str],
    snapshot: EventStoreSnapshot,
) -> Dict[str, Any]:
    """
    Calculate the safe watermark advancement frontiers across independent sources:
    
    Rules:
    1. A source watermark must NEVER advance beyond an undelivered event.
    2. Contiguous advancement: The watermark advances only to the highest position
       where all preceding candidates from that source are confirmed delivered.
    3. Equal timestamps: If multiple source candidates share timestamp T, ALL peers
       at timestamp T must be confirmed delivered before the watermark can advance to T.
    4. Contiguous step IDs: For job steps, watermark advances only as far as consecutive
       increasing IDs are confirmed delivered.
    5. Source isolation: A failure in one source does NOT block safe advancement in another.
    6. Reminders: Reminders have no source watermark; they record delivery IDs only.
    """
    advancements: Dict[str, Any] = {}

    # -----------------------------------------------------------------------
    # 1. Job Steps: Integer Contiguous Frontier
    # -----------------------------------------------------------------------
    step_candidates = [c for c in all_candidates if c.source == "job_steps"]
    if step_candidates:
        # Sort by step ID integer ascending
        def _get_step_id(c: EventCandidate) -> int:
            try:
                if "step_id" in c.material_data:
                    return int(c.material_data["step_id"])
                return int(c.entity_id)
            except Exception:
                return 0

        sorted_steps = sorted(step_candidates, key=_get_step_id)
        current_step_wm = snapshot.get_watermark(WATERMARK_JOB_STEP_MAX_ID, 0)
        if not isinstance(current_step_wm, int):
            current_step_wm = 0

        safe_step_wm = current_step_wm
        for c in sorted_steps:
            s_id = _get_step_id(c)
            if s_id <= current_step_wm:
                continue

            if c.event_id in confirmed_delivered_ids:
                # Contiguous confirmation
                safe_step_wm = s_id
            else:
                # Undelivered step breaks the contiguous frontier
                break

        if safe_step_wm > current_step_wm:
            advancements[WATERMARK_JOB_STEP_MAX_ID] = safe_step_wm

    # -----------------------------------------------------------------------
    # 2. Jobs: Terminal Completed/Failed Timestamps with Peer Grouping
    # -----------------------------------------------------------------------
    terminal_job_candidates = [
        c for c in all_candidates
        if c.source == "jobs" and c.event_type in ("job_completed", "job_failed")
    ]
    if terminal_job_candidates:
        # Group candidates by normalized ISO timestamp
        jobs_by_ts: Dict[str, List[EventCandidate]] = {}
        for c in terminal_job_candidates:
            norm_ts = safe_normalize_utc(c.occurred_at) or ""
            if norm_ts:
                jobs_by_ts.setdefault(norm_ts, []).append(c)

        # Sort distinct timestamps chronologically
        sorted_job_timestamps = sorted(
            jobs_by_ts.keys(),
            key=lambda ts: safe_parse_utc(ts) or datetime.min.replace(tzinfo=timezone.utc),
        )

        current_job_wm = snapshot.get_watermark(WATERMARK_JOBS_COMPLETED_AT)
        current_job_wm_dt = parse_utc_timestamp(current_job_wm) if current_job_wm else None
        safe_job_wm = current_job_wm

        for ts_str in sorted_job_timestamps:
            ts_dt = parse_utc_timestamp(ts_str)
            if current_job_wm_dt and ts_dt and ts_dt <= current_job_wm_dt:
                continue

            # Check all peer candidates sharing this exact timestamp
            peer_candidates = jobs_by_ts[ts_str]
            all_peers_delivered = all(p.event_id in confirmed_delivered_ids for p in peer_candidates)
            if all_peers_delivered:
                safe_job_wm = ts_str
            else:
                # If any peer candidate at timestamp ts_str failed, stop advancement
                break

        if safe_job_wm and safe_job_wm != current_job_wm:
            advancements[WATERMARK_JOBS_COMPLETED_AT] = safe_job_wm

    # -----------------------------------------------------------------------
    # 3. Structured Memories: Updated_at Timestamps with Peer Grouping
    # -----------------------------------------------------------------------
    memory_candidates = [c for c in all_candidates if c.source == "structured_memories"]
    if memory_candidates:
        mem_by_ts: Dict[str, List[EventCandidate]] = {}
        for c in memory_candidates:
            norm_ts = safe_normalize_utc(c.occurred_at) or ""
            if norm_ts:
                mem_by_ts.setdefault(norm_ts, []).append(c)

        sorted_mem_timestamps = sorted(
            mem_by_ts.keys(),
            key=lambda ts: safe_parse_utc(ts) or datetime.min.replace(tzinfo=timezone.utc),
        )

        current_mem_wm = snapshot.get_watermark(WATERMARK_MEMORIES_UPDATED_AT)
        current_mem_wm_dt = parse_utc_timestamp(current_mem_wm) if current_mem_wm else None
        safe_mem_wm = current_mem_wm

        for ts_str in sorted_mem_timestamps:
            ts_dt = parse_utc_timestamp(ts_str)
            if current_mem_wm_dt and ts_dt and ts_dt <= current_mem_wm_dt:
                continue

            peer_candidates = mem_by_ts[ts_str]
            all_peers_delivered = all(p.event_id in confirmed_delivered_ids for p in peer_candidates)
            if all_peers_delivered:
                safe_mem_wm = ts_str
            else:
                break

        if safe_mem_wm and safe_mem_wm != current_mem_wm:
            advancements[WATERMARK_MEMORIES_UPDATED_AT] = safe_mem_wm

    return advancements


# ---------------------------------------------------------------------------
# Delivery Orchestrator
# ---------------------------------------------------------------------------

class EventDeliveryOrchestrator:
    """
    Deterministic delivery orchestrator for Orange AI event candidates.
    
    Responsibilities:
    - Consumes candidates from EventCandidateReader or an external list.
    - Enforces deterministic multi-tier candidate sorting.
    - Skips previously delivered events based on snapshot.is_delivered(event_id).
    - Dispatches delivery via an injected BaseDeliveryAdapter.
    - Records successful delivery IDs through EventWatermarkStore.record_delivery().
    - Atomically advances watermarks only up to the contiguous confirmed frontier.
    - Fails closed on missing or corrupt watermark stores.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        watermark_store: Optional[EventWatermarkStore] = None,
        watermark_store_path: Optional[str] = None,
        reader: Optional[EventCandidateReader] = None,
        delivery_adapter: Optional[BaseDeliveryAdapter] = None,
        default_batch_limit: int = DEFAULT_BATCH_LIMIT,
    ):
        self.db_path = db_path or get_db_path()
        self.watermark_store_path = watermark_store_path or os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)
        self.watermark_store = watermark_store
        self.reader = reader or EventCandidateReader(db_path=self.db_path, watermark_store_path=self.watermark_store_path)
        self.delivery_adapter = delivery_adapter or InProcessDeliveryAdapter()
        self.default_batch_limit = max(1, int(default_batch_limit))

    def _resolve_store(self) -> EventWatermarkStore:
        """Resolve and validate the watermark store. Fails closed if missing or corrupt."""
        if self.watermark_store is not None:
            return self.watermark_store

        # Must load from disk; raises EventStoreNotFoundError / EventStoreCorruptError
        return EventWatermarkStore.load(self.watermark_store_path)

    def execute_delivery_cycle(
        self,
        candidates: Optional[List[EventCandidate]] = None,
        batch_limit: Optional[int] = None,
        adapter: Optional[BaseDeliveryAdapter] = None,
        now: Optional[datetime] = None,
    ) -> EventDeliveryBatchResult:
        """
        Execute an orchestrated delivery cycle:
        1. Resolve and validate watermark store (fail-closed).
        2. Collect candidates if not provided.
        3. Sort candidates deterministically.
        4. Deliver candidates up to batch_limit through adapter.
        5. Record confirmed deliveries in watermark store.
        6. Calculate and apply safe contiguous watermark advancements.
        7. Return structured EventDeliveryBatchResult.
        """
        store = self._resolve_store()
        snapshot = store.get_snapshot()
        limit = max(1, int(batch_limit or self.default_batch_limit))
        delivery_iface = adapter or self.delivery_adapter
        ref_now = now or datetime.now(timezone.utc)

        # 1. Collect or use provided candidates
        if candidates is None:
            raw_candidates = self.reader.collect_all_candidates(watermark_store=store, now=ref_now)
        else:
            raw_candidates = list(candidates)

        # 2. Deterministic multi-tier sort
        sorted_all = sort_candidates(raw_candidates)

        result = EventDeliveryBatchResult(total_candidates=len(sorted_all))

        # Track IDs confirmed delivered (both prior and new) for watermark frontier calculation
        confirmed_delivered_ids: Set[str] = set()
        for c in sorted_all:
            if c.is_delivered or snapshot.is_delivered(c.event_id):
                confirmed_delivered_ids.add(c.event_id)

        # 3. Process candidates within batch limit
        attempted_count = 0
        for candidate in sorted_all:
            # Check deduplication
            if candidate.is_delivered or snapshot.is_delivered(candidate.event_id):
                result.skipped_count += 1
                continue

            if attempted_count >= limit:
                # Remaining candidates exceed batch limit and remain pending
                break

            attempted_count += 1

            # Dispatch delivery attempt
            try:
                delivery_res = delivery_iface.deliver(candidate)
            except Exception as e:
                logger.error("[EVENT DELIVERY] Unexpected exception from delivery adapter: %s", e)
                delivery_res = DeliveryResult(
                    event_id=candidate.event_id,
                    status=DeliveryStatus.AMBIGUOUS,
                    error=str(e),
                )

            # Evaluate delivery status contract
            if delivery_res.status == DeliveryStatus.SUCCESS:
                # Record delivery into watermark store
                recorded = store.record_delivery(
                    event_id=candidate.event_id,
                    delivered_at=parse_utc_timestamp(delivery_res.delivered_at) if delivery_res.delivered_at else ref_now,
                    metadata=candidate.material_data,
                    now=ref_now,
                )
                if recorded:
                    result.delivered_count += 1
                    result.delivered_event_ids.append(candidate.event_id)
                    confirmed_delivered_ids.add(candidate.event_id)
                else:
                    # Idempotent skip if already present
                    result.skipped_count += 1
                    confirmed_delivered_ids.add(candidate.event_id)

            elif delivery_res.status == DeliveryStatus.FAILURE:
                result.failed_count += 1
                result.failed_event_ids.append(candidate.event_id)
                if delivery_res.error:
                    result.errors[candidate.event_id] = delivery_res.error

            elif delivery_res.status == DeliveryStatus.AMBIGUOUS:
                result.ambiguous_count += 1
                result.ambiguous_event_ids.append(candidate.event_id)
                if delivery_res.error:
                    result.errors[candidate.event_id] = delivery_res.error

            else:
                # Any unrecognized status is strictly treated as ambiguous
                result.ambiguous_count += 1
                result.ambiguous_event_ids.append(candidate.event_id)
                result.errors[candidate.event_id] = f"Unrecognized delivery status: {delivery_res.status}"

        # 4. Calculate safe contiguous watermark advancements
        safe_advancements = calculate_safe_watermark_advancements(
            all_candidates=sorted_all,
            confirmed_delivered_ids=confirmed_delivered_ids,
            snapshot=snapshot,
        )

        # 5. Apply safe watermark advancements
        if safe_advancements:
            store.advance_watermarks(safe_advancements, now=ref_now)
            result.advanced_watermarks = dict(safe_advancements)
            logger.info("[EVENT DELIVERY] Safely advanced watermarks: %s", safe_advancements)

        return result
