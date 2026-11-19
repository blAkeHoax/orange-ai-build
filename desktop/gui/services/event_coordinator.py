"""
gui/services/event_coordinator.py - GUI & Scheduler Proactive Event Integration.

Stage 3.7E.3C.3: GUI & Scheduler Event Integration
Connects the event-awareness pipeline to the Orange AI Desktop application lifecycle:
1. Startup reconciliation integration without blocking the Tkinter main thread.
2. Single-flight worker with coalescing mechanics for concurrent triggers.
3. Thread-safe ChatView delivery adapter with verified UI acknowledgement.
4. Authoritative deduplication preventing duplicate notifications between direct scheduler callbacks and the event pipeline.
5. Fail-closed handling for missing or corrupt watermark stores.
6. Bounded execution, clean shutdown, and late-callback safety.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, Callable

from brain.briefing.models import (
    EventCandidate,
    EventPriority,
    DeliveryStatus,
    DeliveryResult,
    ReconciliationStatus,
    ReconciliationResult,
)
from brain.briefing.event_delivery import (
    BaseDeliveryAdapter,
    EventDeliveryOrchestrator,
)
from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreError,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EventStoreSchemaError,
    EventStoreLockTimeoutError,
    generate_event_id,
    normalize_utc_timestamp,
    EVENT_WATERMARKS_FILENAME,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.event_readers import EventCandidateReader
from brain.briefing.reconciliation import (
    StartupReconciler,
    detect_unexpected_missing_store,
    DEFAULT_RECONCILIATION_BATCH_LIMIT,
)
from brain.memory import get_db_path
from brain.paths import get_project_root

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notification Formatting
# ---------------------------------------------------------------------------

def format_event_candidate(candidate: EventCandidate) -> Tuple[Optional[str], str]:
    """
    Format an EventCandidate into a concise, sanitized display text and message type.
    
    Returns:
        Tuple[Optional[str], str]: (display_text, msg_type)
        If display_text is None, the event is treated as quiet (recorded in watermark
        store for contiguous progression without polluting the chat timeline).
    """
    evt_type = (candidate.event_type or "").lower().strip()
    material = candidate.material_data or {}

    # 1. Reminders
    if evt_type == "reminder_due":
        rem_id = material.get("reminder_id") or candidate.entity_id
        msg = material.get("message") or candidate.title
        text = f"⏰ Reminder #{rem_id}: {msg}" if rem_id else f"⏰ Reminder: {msg}"
        return text, "reminder"

    # 2. Jobs - Waiting Confirmation
    if evt_type == "job_waiting_confirmation":
        short_id = candidate.entity_id[:8] if len(candidate.entity_id) >= 8 else candidate.entity_id
        pending_act = material.get("pending_action") or "action"
        text = (
            f"⚠️ Orange AI [Job {short_id}]: Action '{pending_act}' requires confirmation.\n"
            f"Type 'job confirm {short_id} yes' to approve or 'job confirm {short_id} no' to decline."
        )
        return text, "job"

    # 3. Jobs - Completed
    if evt_type == "job_completed":
        short_id = candidate.entity_id[:8] if len(candidate.entity_id) >= 8 else candidate.entity_id
        resp = material.get("final_response") or candidate.title
        return f"✅ Orange AI [Job {short_id}]: Completed: {resp}", "job"

    # 4. Jobs - Failed
    if evt_type == "job_failed":
        short_id = candidate.entity_id[:8] if len(candidate.entity_id) >= 8 else candidate.entity_id
        err = material.get("error") or candidate.title
        return f"❌ Orange AI [Job {short_id}]: Failed: {err}", "job"

    # 5. Job Steps - Failed
    if evt_type == "job_step_failed":
        raw_job = material.get("job_id") or ""
        short_id = raw_job[:8] if len(raw_job) >= 8 else (raw_job or candidate.entity_id)
        err = material.get("error") or candidate.title
        return f"❌ Orange AI [Job {short_id}]: Step failed: {err}", "job"

    # 6. Job Steps - Completed (Quiet Progression)
    if evt_type == "job_step_completed":
        # Section 7: Avoid dumping every successful internal job step into the conversation.
        # Quiet delivery allows the watermark store to advance job_step_max_id without UI spam.
        return None, ""

    # 7. Structured / Project Memories
    if "memory" in evt_type:
        return f"🧠 Orange AI: Memory updated: {candidate.title}", "system"

    # 8. Generic Operational Fallback
    return f"ℹ️ Orange AI: {candidate.title}", "system"


# ---------------------------------------------------------------------------
# ChatView Delivery Adapter
# ---------------------------------------------------------------------------

class ChatViewDeliveryAdapter(BaseDeliveryAdapter):
    """
    Delivery adapter integrating EventDeliveryOrchestrator with Tkinter ChatView.
    
    Guarantees:
    - Thread-safe: marshals UI operations through AssistantBridge.dispatch_with_ack().
    - Acknowledged: confirms UI presentation before returning SUCCESS.
    - Ambiguous: returns AMBIGUOUS on timeout, bridge closure, or shutdown so unconfirmed events are never lost.
    - Non-chatty: suppresses mundane internal step events (job_step_completed) without blocking contiguous step watermark advancement.
    """

    def __init__(
        self,
        app: Any,
        bridge: Optional[Any] = None,
        timeout: float = 5.0,
    ):
        self.app = app
        self.bridge = bridge
        self.timeout = max(0.5, float(timeout))

    def deliver(self, candidate: EventCandidate) -> DeliveryResult:
        """
        Attempt delivery of a single candidate event to the ChatView timeline.
        """
        text, msg_type = format_event_candidate(candidate)

        # 1. Quiet events (e.g. successful internal steps)
        if text is None:
            logger.debug("[EVENT ADAPTER] Quiet progression acknowledged for '%s'", candidate.event_id)
            return DeliveryResult(
                event_id=candidate.event_id,
                status=DeliveryStatus.SUCCESS,
                channel="chat_view",
                delivered_at=datetime.now(timezone.utc).isoformat(),
            )

        # 2. Check shutdown status
        if getattr(self.app, "_is_shutting_down", False):
            logger.info("[EVENT ADAPTER] Rejected delivery of '%s': Application is shutting down.", candidate.event_id)
            return DeliveryResult(
                event_id=candidate.event_id,
                status=DeliveryStatus.AMBIGUOUS,
                error="Application is shutting down",
                channel="chat_view",
            )

        # 3. Check in-flight reservation via EventCoordinator (Correction B)
        coord = getattr(self.app, "_event_coordinator", None)
        if coord and hasattr(coord, "reserve_event"):
            if not coord.reserve_event(candidate.event_id):
                logger.debug("[EVENT ADAPTER] Event '%s' already delivered or in flight; suppressing.", candidate.event_id)
                return DeliveryResult(
                    event_id=candidate.event_id,
                    status=DeliveryStatus.SUCCESS,
                    channel="chat_view",
                    delivered_at=datetime.now(timezone.utc).isoformat(),
                )

        try:
            # Helper to mark reminder fired upon confirmed UI delivery (Correction A)
            def _mark_reminder_fired_if_needed():
                if candidate.source == "reminders" or candidate.event_type == "reminder_due":
                    rem_id_val = None
                    if candidate.material_data and "reminder_id" in candidate.material_data:
                        rem_id_val = candidate.material_data["reminder_id"]
                    elif candidate.entity_id:
                        rem_id_val = candidate.entity_id
                    if rem_id_val is not None:
                        from brain.reminders import mark_fired
                        mark_fired(int(rem_id_val))
                        logger.info("[EVENT ADAPTER] Confirmed UI delivery for reminder #%s; marked fired in SQLite.", rem_id_val)

            # 4. Define the UI presentation action executed on Tkinter thread
            def _ui_present():
                if getattr(self.app, "_is_shutting_down", False):
                    raise RuntimeError("Application shutting down during UI presentation")
                if not hasattr(self.app, "_views_cache") or "chat" not in self.app._views_cache:
                    raise RuntimeError("ChatView not available in views cache")

                chat_view = self.app._views_cache["chat"]
                outcome = {
                    "reply": text,
                    "model": "system",
                    "type": msg_type,
                }
                chat_view.on_assistant_result(f"evt_{candidate.event_id}", outcome)
                return True

            # 5. Dispatch with acknowledgement
            bridge = self.bridge or getattr(self.app, "_assistant_bridge", None)
            if bridge and hasattr(bridge, "dispatch_with_ack"):
                success, res, err = bridge.dispatch_with_ack(_ui_present, timeout=self.timeout)
                if success:
                    try:
                        _mark_reminder_fired_if_needed()
                    except Exception as ex:
                        logger.error("[EVENT ADAPTER] Failed to mark reminder fired for '%s': %s", candidate.event_id, ex)
                        return DeliveryResult(
                            event_id=candidate.event_id,
                            status=DeliveryStatus.AMBIGUOUS,
                            error=f"Persistence error marking reminder fired: {ex}",
                            channel="chat_view",
                        )
                    logger.debug("[EVENT ADAPTER] Successfully confirmed UI delivery for '%s'", candidate.event_id)
                    return DeliveryResult(
                        event_id=candidate.event_id,
                        status=DeliveryStatus.SUCCESS,
                        channel="chat_view",
                        delivered_at=datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    logger.warning("[EVENT ADAPTER] UI delivery unconfirmed for '%s': %s", candidate.event_id, err)
                    # Differentiate between ambiguous timeout/shutdown and definite UI failure
                    if err and "timed out" in err.lower():
                        status = DeliveryStatus.AMBIGUOUS
                    elif err and "closed" in err.lower():
                        status = DeliveryStatus.AMBIGUOUS
                    else:
                        status = DeliveryStatus.FAILURE
                    return DeliveryResult(
                        event_id=candidate.event_id,
                        status=status,
                        error=err or "UI delivery unconfirmed",
                        channel="chat_view",
                    )

            # 6. Direct fallback if bridge is missing
            if hasattr(self.app, "after") and not getattr(self.app, "_is_shutting_down", False):
                ack_event = threading.Event()
                holder = {"success": False, "error": None}

                def _fallback_task():
                    try:
                        _ui_present()
                        holder["success"] = True
                    except Exception as ex:
                        holder["error"] = str(ex)
                    finally:
                        ack_event.set()

                try:
                    self.app.after(0, _fallback_task)
                    if ack_event.wait(timeout=self.timeout):
                        if holder["success"]:
                            try:
                                _mark_reminder_fired_if_needed()
                            except Exception as ex:
                                return DeliveryResult(
                                    event_id=candidate.event_id,
                                    status=DeliveryStatus.AMBIGUOUS,
                                    error=f"Persistence error marking reminder fired: {ex}",
                                    channel="chat_view",
                                )
                            return DeliveryResult(
                                event_id=candidate.event_id,
                                status=DeliveryStatus.SUCCESS,
                                channel="chat_view",
                                delivered_at=datetime.now(timezone.utc).isoformat(),
                            )
                        return DeliveryResult(
                            event_id=candidate.event_id,
                            status=DeliveryStatus.FAILURE,
                            error=holder["error"] or "Presentation failed",
                            channel="chat_view",
                        )
                    return DeliveryResult(
                        event_id=candidate.event_id,
                        status=DeliveryStatus.AMBIGUOUS,
                        error="UI dispatch timed out",
                        channel="chat_view",
                    )
                except Exception as e:
                    return DeliveryResult(
                        event_id=candidate.event_id,
                        status=DeliveryStatus.AMBIGUOUS,
                        error=str(e),
                        channel="chat_view",
                    )

            return DeliveryResult(
                event_id=candidate.event_id,
                status=DeliveryStatus.AMBIGUOUS,
                error="No UI dispatcher available",
                channel="chat_view",
            )
        finally:
            if coord and hasattr(coord, "release_event"):
                coord.release_event(candidate.event_id)


# ---------------------------------------------------------------------------
# Event Integration Coordinator
# ---------------------------------------------------------------------------

class EventCoordinator:
    """
    Coordinates proactive event reconciliation and lifecycle delivery for Orange AI Desktop.
    
    Guarantees:
    1. Single-Flight Execution: At most one reconciliation/delivery cycle executes per instance.
    2. Coalescing: Multiple rapid triggers request a rerun without spawning unbounded threads.
    3. Non-Blocking Startup: Startup recovery runs in a bounded background thread.
    4. Authoritative Deduplication: Tracks and records delivered events in EventWatermarkStore.
    5. Clean Shutdown: Bounded thread termination and discard of unconfirmed late results.
    """

    def __init__(
        self,
        app: Any,
        db_path: Optional[str] = None,
        watermark_store_path: Optional[str] = None,
        bridge: Optional[Any] = None,
        reconciler: Optional[StartupReconciler] = None,
        delivery_adapter: Optional[BaseDeliveryAdapter] = None,
        batch_limit: int = DEFAULT_RECONCILIATION_BATCH_LIMIT,
    ):
        self.app = app
        self.db_path = db_path or get_db_path()
        self.watermark_store_path = watermark_store_path or os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)
        self.bridge = bridge or getattr(app, "_assistant_bridge", None)
        self.batch_limit = max(1, int(batch_limit))

        self.delivery_adapter = delivery_adapter or ChatViewDeliveryAdapter(app, bridge=self.bridge)
        self.reconciler = reconciler or StartupReconciler(
            db_path=self.db_path,
            watermark_store_path=self.watermark_store_path,
            delivery_adapter=self.delivery_adapter,
            default_batch_limit=self.batch_limit,
            allow_first_run_init=True,
        )

        self._lock = threading.Lock()
        self._is_running: bool = False
        self._rerun_requested: bool = False
        self._is_closed: bool = False
        self._worker_thread: Optional[threading.Thread] = None

        # In-memory session tracking for direct callback deduplication
        self._delivered_in_session: Set[str] = set()

        # In-flight event reservation tracking (Correction B)
        self._in_flight_events: Set[str] = set()
        self._reservation_lock = threading.Lock()

    def reserve_event(self, event_id: str) -> bool:
        """
        Attempt to acquire an in-flight reservation for event_id.
        Returns True if reservation was acquired; False if already delivered or already in-flight.
        Never holds the reservation lock while awaiting UI acknowledgement.
        """
        with self._reservation_lock:
            if event_id in self._in_flight_events:
                return False
            if self.is_event_delivered(event_id):
                return False
            self._in_flight_events.add(event_id)
            return True

    def release_event(self, event_id: str):
        """Release an in-flight reservation in finally block."""
        with self._reservation_lock:
            self._in_flight_events.discard(event_id)

    def is_event_delivered(self, event_id: str) -> bool:
        """Check if an event ID has already been delivered in session or persisted store."""
        with self._lock:
            if event_id in self._delivered_in_session:
                return True

        if not os.path.exists(self.watermark_store_path):
            return False

        try:
            store = EventWatermarkStore.load(self.watermark_store_path)
            return store.is_event_delivered(event_id)
        except Exception:
            return False

    def record_direct_delivery(
        self,
        event_id: str,
        event_type: str,
        entity_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        watermark_advancements: Optional[Dict[str, Any]] = None,
    ):
        """
        Record delivery of an event presented through a direct path (e.g. immediate scheduler callback),
        ensuring the event pipeline does not duplicate it.
        """
        with self._lock:
            self._delivered_in_session.add(event_id)

        try:
            if not os.path.exists(self.watermark_store_path):
                # Baseline not yet established; initialize first
                if detect_unexpected_missing_store(self.watermark_store_path):
                    logger.warning("[EVENT COORDINATOR] Cannot record direct delivery: Store unexpectedly missing.")
                    return
                store = EventWatermarkStore.initialize_first_run(self.watermark_store_path)
            else:
                store = EventWatermarkStore.load(self.watermark_store_path)

            now_iso = datetime.now(timezone.utc).isoformat()
            store.record_delivery(
                event_id=event_id,
                metadata=metadata or {},
            )

            if watermark_advancements:
                for k, v in watermark_advancements.items():
                    store.advance_watermark(k, v)

            logger.debug("[EVENT COORDINATOR] Recorded direct delivery for '%s'", event_id)
        except Exception as e:
            logger.warning("[EVENT COORDINATOR] Failed to record direct delivery for '%s': %s", event_id, e)

    def trigger_reconciliation(self, reason: str = "manual") -> bool:
        """
        Trigger a non-blocking reconciliation and delivery cycle.
        
        Returns:
            bool: True if a cycle was scheduled or queued; False if coordinator is closed.
        """
        with self._lock:
            if self._is_closed:
                logger.debug("[EVENT COORDINATOR] Reconciliation rejected: coordinator closed.")
                return False

            if self._is_running:
                logger.debug("[EVENT COORDINATOR] Reconciliation in progress; coalesced trigger for '%s'.", reason)
                self._rerun_requested = True
                return True

            self._is_running = True
            self._rerun_requested = False

        thread = threading.Thread(
            target=self._worker_loop,
            name=f"EventReconciliationWorker-{reason}",
            daemon=True,
        )
        with self._lock:
            self._worker_thread = thread
        thread.start()
        return True

    def shutdown(self, timeout: float = 2.0):
        """Cleanly cancel pending reconciliation and wait for worker termination."""
        with self._lock:
            self._is_closed = True
            self._rerun_requested = False
            worker = self._worker_thread

        current = threading.current_thread()
        timeout_val = max(0.1, float(timeout))

        if worker is not None and worker.is_alive() and worker is not current:
            logger.info("[EVENT COORDINATOR] Waiting up to %.1fs for worker '%s' to finish...", timeout_val, worker.name)
            worker.join(timeout=timeout_val)
            if worker.is_alive():
                logger.warning("[EVENT COORDINATOR] Worker '%s' did not finish within timeout; continuing shutdown.", worker.name)

    def _worker_loop(self):
        """Worker thread executing reconciliation cycles until no reruns are requested."""
        try:
            while True:
                with self._lock:
                    if self._is_closed:
                        break

                self._execute_cycle()

                with self._lock:
                    if self._is_closed:
                        break
                    if self._rerun_requested:
                        self._rerun_requested = False
                        continue
                    break
        except Exception as e:
            logger.error("[EVENT COORDINATOR] Unexpected exception in reconciliation worker loop: %s", e)
        finally:
            with self._lock:
                self._is_running = False
                self._rerun_requested = False
                self._worker_thread = None

    def _execute_cycle(self) -> Optional[ReconciliationResult]:
        """Execute a single reconciliation run and log structured diagnostics."""
        try:
            res = self.reconciler.reconcile(
                batch_limit=self.batch_limit,
                adapter=self.delivery_adapter,
            )

            # Structured diagnostic logging
            if res.status == ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED:
                logger.info("[EVENT COORDINATOR] First-run baseline established successfully.")
            elif res.status == ReconciliationStatus.SUCCESS:
                logger.info(
                    "[EVENT COORDINATOR] Reconciliation SUCCESS: Delivered %d events (IDs: %s).",
                    res.delivered_count,
                    res.delivered_event_ids,
                )
            elif res.status == ReconciliationStatus.NO_ELIGIBLE_CANDIDATES:
                logger.debug("[EVENT COORDINATOR] Reconciliation check complete: No eligible candidates.")
            elif res.status == ReconciliationStatus.PARTIAL_SUCCESS:
                logger.warning(
                    "[EVENT COORDINATOR] Reconciliation PARTIAL_SUCCESS: Delivered %d, failed %d.",
                    res.delivered_count,
                    res.failed_count,
                )
            elif res.status == ReconciliationStatus.STORE_MISSING_UNEXPECTED:
                logger.error("[EVENT COORDINATOR] Watermark store unexpectedly missing with prior installation evidence! Failing closed.")
            elif res.status == ReconciliationStatus.STORE_CORRUPT:
                logger.error("[EVENT COORDINATOR] Watermark store is corrupt! Failing closed.")
            elif res.status == ReconciliationStatus.SOURCE_COLLECTION_FAILURE:
                logger.error("[EVENT COORDINATOR] Source collection failure during reconciliation: %s", res.errors)
            elif res.status == ReconciliationStatus.DELIVERY_FAILURE:
                logger.warning("[EVENT COORDINATOR] Delivery failure during reconciliation: %s", res.errors)
            elif res.status == ReconciliationStatus.LOCK_TIMEOUT:
                logger.warning("[EVENT COORDINATOR] Lock timeout during reconciliation: %s", res.details)

            # Record delivered event IDs in session set for instant check
            if res.delivered_count > 0:
                with self._lock:
                    for eid in res.delivered_event_ids:
                        self._delivered_in_session.add(eid)

            return res
        except Exception as e:
            logger.error("[EVENT COORDINATOR] Reconciliation cycle failed with unexpected error: %s", e)
            return None
