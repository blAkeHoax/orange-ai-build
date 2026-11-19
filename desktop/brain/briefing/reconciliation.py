"""
brain/briefing/reconciliation.py - Startup Reconciliation & Missed-Event Recovery.

Stage 3.7E.3C.2: Deterministic, callable startup-reconciliation service that compares
persisted event watermarks against authoritative database state and safely recovers
eligible events that occurred while Orange AI was not running.

Core Invariants:
1. Explicit First-Run Baseline: When no watermark store exists and no evidence of
   prior installation exists, initializes pre-baseline positions to prevent historical
   event floods, without falsely recording past deliveries.
2. Missing-Store Safeguard: If reliable evidence indicates a previously initialized store
   was unexpectedly removed (orphaned lock, backup files, corrupt copies), fails closed
   with STORE_MISSING_UNEXPECTED.
3. Bounded Recovery: Default batch limit (20 candidates) prevents notification floods.
   Overflow candidates remain unattempted and eligible for subsequent reconciliation.
4. Contiguous Advancement: Watermarks never advance past failed, ambiguous, or overflow
   candidates. Reminders never invent watermarks.
5. Non-Blocking Service Design: Synchronous, callable method designed for execution in a
   worker thread by future application lifecycle callers. Does not spawn background threads
   or connect to UI loops.
"""

from datetime import datetime, timezone
import glob
import logging
import os
from typing import Any, Dict, List, Optional, Set

from brain.briefing.models import (
    EventCandidate,
    EventPriority,
    DeliveryStatus,
    ReconciliationStatus,
    ReconciliationResult,
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
from brain.briefing.event_delivery import (
    BaseDeliveryAdapter,
    InProcessDeliveryAdapter,
    EventDeliveryOrchestrator,
    initialize_event_store_baseline,
    inspect_source_high_waters,
)
from brain.memory import get_db_path
from brain.paths import get_project_root

logger = logging.getLogger(__name__)

DEFAULT_RECONCILIATION_BATCH_LIMIT = 20


def detect_unexpected_missing_store(file_path: str) -> bool:
    """
    Detect whether there is reliable evidence that a watermark store previously existed
    at file_path and was unexpectedly removed or deleted.
    
    Checks:
    - Companion lock file exists ({file_path}.lock) while primary JSON is missing.
    - Backup file exists ({file_path}.bak) while primary JSON is missing.
    - Non-destructive corrupt backups exist ({file_path}.corrupt.*) in the same directory.
    - Temporary write files exist ({file_path}.tmp.*).
    
    If any evidence is found, returns True (fail-closed; do not treat as fresh install).
    """
    if os.path.exists(file_path):
        return False

    lock_file = f"{file_path}.lock"
    if os.path.exists(lock_file):
        return True

    bak_file = f"{file_path}.bak"
    if os.path.exists(bak_file):
        return True

    parent_dir = os.path.dirname(os.path.abspath(file_path))
    base_name = os.path.basename(file_path)

    corrupt_pattern = os.path.join(parent_dir, f"{base_name}.corrupt.*")
    if glob.glob(corrupt_pattern):
        return True

    tmp_pattern = os.path.join(parent_dir, f"{base_name}.tmp.*")
    if glob.glob(tmp_pattern):
        return True

    return False


def cleanup_orphaned_temp_files(
    target_files: Optional[List[str]] = None,
    max_age_seconds: float = 7200.0,
    now: Optional[datetime] = None,
) -> int:
    """
    Safely clean up orphaned temporary write files (.tmp.* or .tmp) older than max_age_seconds.
    
    Default max_age_seconds is 7200 (2 hours). Active temp files newer than this threshold
    are strictly preserved to avoid interfering with concurrent write operations.
    
    Guarantees:
    - Only targets temp files matching '{base_name}.tmp.*' or '{base_name}.tmp'.
    - NEVER touches primary files, companion locks (.lock), backups (.bak), or corrupt archives (.corrupt.*).
    - Ignores permission errors or concurrently deleted files safely.
    - Returns total count of successfully removed orphaned temp files.
    """
    ref_now = now or datetime.now(timezone.utc)
    ref_timestamp = ref_now.timestamp() if ref_now.tzinfo else ref_now.replace(tzinfo=timezone.utc).timestamp()
    
    if target_files is None:
        target_files = [
            os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME),
            os.path.join(get_project_root(), "recommendations.json"),
        ]
        
    cleaned_count = 0
    checked_paths: Set[str] = set()

    for target in target_files:
        if not target:
            continue
        parent_dir = os.path.dirname(os.path.abspath(target))
        base_name = os.path.basename(target)
        if not os.path.isdir(parent_dir):
            continue

        patterns = [
            os.path.join(parent_dir, f"{base_name}.tmp.*"),
            os.path.join(parent_dir, f"{base_name}.tmp"),
        ]
        
        for pat in patterns:
            for candidate_path in glob.glob(pat):
                abs_cand = os.path.abspath(candidate_path)
                if abs_cand in checked_paths:
                    continue
                checked_paths.add(abs_cand)

                if not os.path.isfile(abs_cand):
                    continue

                cand_base = os.path.basename(abs_cand)
                if not (cand_base.endswith(".tmp") or ".tmp." in cand_base):
                    continue

                if cand_base.endswith(".lock") or cand_base.endswith(".bak") or ".corrupt." in cand_base:
                    continue

                if abs_cand == os.path.abspath(target):
                    continue

                try:
                    mtime = os.path.getmtime(abs_cand)
                    age_seconds = ref_timestamp - mtime
                    if age_seconds > max_age_seconds:
                        os.remove(abs_cand)
                        cleaned_count += 1
                        logger.info(
                            "[RECONCILIATION] Cleaned up orphaned temporary file '%s' (age: %.1f hours)",
                            abs_cand,
                            age_seconds / 3600.0,
                        )
                except OSError as e:
                    logger.warning("[RECONCILIATION] Failed to clean orphaned temp file '%s': %s", abs_cand, e)

    return cleaned_count


class StartupReconciler:
    """
    Deterministic startup reconciliation service.
    
    Responsibilities:
    - Resolves and validates the event watermark store (with fail-closed error handling).
    - Detects genuine first-run conditions and establishes an explicit baseline.
    - Detects unexpected missing stores and fails closed with clear diagnostic telemetry.
    - Uses EventCandidateReader to collect post-watermark events across all sources.
    - Enforces deterministic priority sorting and conservative batch bounding (default 20).
    - Dispatches delivery via EventDeliveryOrchestrator with safe contiguous advancement.
    - Advances last_startup_reconciliation watermark upon cycle completion.
    - Returns a strongly-typed ReconciliationResult with complete operational telemetry.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        watermark_store: Optional[EventWatermarkStore] = None,
        watermark_store_path: Optional[str] = None,
        reader: Optional[EventCandidateReader] = None,
        orchestrator: Optional[EventDeliveryOrchestrator] = None,
        delivery_adapter: Optional[BaseDeliveryAdapter] = None,
        default_batch_limit: int = DEFAULT_RECONCILIATION_BATCH_LIMIT,
        allow_first_run_init: bool = True,
    ):
        self.db_path = db_path or get_db_path()
        self.watermark_store_path = watermark_store_path or os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)
        self.watermark_store = watermark_store
        self.reader = reader
        self.orchestrator = orchestrator
        self.delivery_adapter = delivery_adapter or InProcessDeliveryAdapter()
        self.default_batch_limit = max(1, int(default_batch_limit))
        self.allow_first_run_init = allow_first_run_init

    def reconcile(
        self,
        now: Optional[datetime] = None,
        batch_limit: Optional[int] = None,
        adapter: Optional[BaseDeliveryAdapter] = None,
    ) -> ReconciliationResult:
        """
        Execute a deterministic startup reconciliation run.
        
        Args:
            now: Optional reference datetime (defaults to current UTC).
            batch_limit: Optional maximum candidates to attempt this run (defaults to 20).
            adapter: Optional delivery adapter overriding the instance default.
            
        Returns:
            ReconciliationResult dataclass containing status, counts, IDs, advanced watermarks,
            and any errors encountered.
        """
        ref_now = now or datetime.now(timezone.utc)
        ref_now_iso = normalize_utc_timestamp(ref_now) or ""
        limit = max(1, int(batch_limit or self.default_batch_limit))
        delivery_iface = adapter or self.delivery_adapter

        # -------------------------------------------------------------------
        # 0. Cleanup Orphaned Temporary Files Older Than 2 Hours
        # -------------------------------------------------------------------
        orphaned_cleaned = cleanup_orphaned_temp_files(
            target_files=[self.watermark_store_path],
            now=ref_now,
        )

        # -------------------------------------------------------------------
        # 1. Resolve and Validate Watermark Store
        # -------------------------------------------------------------------
        store: Optional[EventWatermarkStore] = None

        if self.watermark_store is not None:
            store = self.watermark_store
        else:
            if not os.path.exists(self.watermark_store_path):
                # Check whether store was unexpectedly deleted
                if detect_unexpected_missing_store(self.watermark_store_path):
                    logger.error(
                        "[RECONCILIATION] Watermark store is missing unexpectedly at '%s'; "
                        "prior installation evidence detected. Failing closed.",
                        self.watermark_store_path,
                    )
                    return ReconciliationResult(
                        status=ReconciliationStatus.STORE_MISSING_UNEXPECTED,
                        reconciled_at=ref_now_iso,
                        details="Watermark store file is missing, but prior installation evidence "
                                "(orphaned lock, backup, or corrupt copy) was detected. "
                                "Manual inspection or recovery is required.",
                    )

                if not self.allow_first_run_init:
                    logger.warning(
                        "[RECONCILIATION] Watermark store missing at '%s' and first-run init disallowed.",
                        self.watermark_store_path,
                    )
                    return ReconciliationResult(
                        status=ReconciliationStatus.STORE_MISSING_UNEXPECTED,
                        reconciled_at=ref_now_iso,
                        details="Watermark store missing and first-run initialization is disabled.",
                    )

                # Genuine first-run installation: explicitly establish baseline
                try:
                    store = initialize_event_store_baseline(
                        file_path=self.watermark_store_path,
                        db_path=self.db_path,
                        now=ref_now,
                    )
                    snapshot = store.get_snapshot()
                    high_waters = dict(snapshot.watermarks)

                    logger.info(
                        "[RECONCILIATION] Explicit first-run baseline initialized successfully: %s",
                        high_waters,
                    )
                    return ReconciliationResult(
                        status=ReconciliationStatus.FIRST_RUN_BASELINE_ESTABLISHED,
                        is_first_run=True,
                        baseline_established=True,
                        advanced_watermarks=high_waters,
                        orphaned_temp_files_cleaned=orphaned_cleaned,
                        reconciled_at=ref_now_iso,
                        details="Explicit first-run baseline established from authoritative source high-waters.",
                    )
                except FileExistsError:
                    # Concurrently created by peer process; load existing
                    try:
                        store = EventWatermarkStore.load(self.watermark_store_path)
                    except Exception as e:
                        return ReconciliationResult(
                            status=ReconciliationStatus.STORE_CORRUPT,
                            errors={"error": str(e)},
                            reconciled_at=ref_now_iso,
                            details=f"Concurrent initialization produced unreadable store: {e}",
                        )
                except EventStoreLockTimeoutError as e:
                    return ReconciliationResult(
                        status=ReconciliationStatus.LOCK_TIMEOUT,
                        errors={"error": str(e)},
                        reconciled_at=ref_now_iso,
                        details="Lock timeout while establishing first-run baseline.",
                    )
                except Exception as e:
                    logger.error("[RECONCILIATION] Failed to initialize first-run baseline: %s", e)
                    return ReconciliationResult(
                        status=ReconciliationStatus.PERSISTENCE_FAILURE,
                        errors={"error": str(e)},
                        reconciled_at=ref_now_iso,
                        details=f"Failed to initialize first-run baseline: {e}",
                    )
            else:
                # Existing file on disk: load and validate
                try:
                    store = EventWatermarkStore.load(self.watermark_store_path)
                except (EventStoreCorruptError, EventStoreSchemaError) as e:
                    logger.error("[RECONCILIATION] Watermark store is corrupt or invalid schema: %s", e)
                    return ReconciliationResult(
                        status=ReconciliationStatus.STORE_CORRUPT,
                        errors={"error": str(e)},
                        reconciled_at=ref_now_iso,
                        details=f"Watermark store is corrupt or invalid schema: {e}",
                    )
                except EventStoreLockTimeoutError as e:
                    logger.error("[RECONCILIATION] Lock timeout loading store: %s", e)
                    return ReconciliationResult(
                        status=ReconciliationStatus.LOCK_TIMEOUT,
                        errors={"error": str(e)},
                        reconciled_at=ref_now_iso,
                        details="Lock timeout loading watermark store.",
                    )
                except EventStoreNotFoundError as e:
                    return ReconciliationResult(
                        status=ReconciliationStatus.STORE_MISSING_UNEXPECTED,
                        errors={"error": str(e)},
                        reconciled_at=ref_now_iso,
                        details=f"Watermark store not found: {e}",
                    )
                except Exception as e:
                    return ReconciliationResult(
                        status=ReconciliationStatus.STORE_CORRUPT,
                        errors={"error": str(e)},
                        reconciled_at=ref_now_iso,
                        details=f"Unexpected error loading watermark store: {e}",
                    )

        snapshot = store.get_snapshot()

        # -------------------------------------------------------------------
        # 2. Collect Candidate Events Across All Sources
        # -------------------------------------------------------------------
        reader = self.reader or EventCandidateReader(
            db_path=self.db_path,
            watermark_store_path=self.watermark_store_path,
        )

        try:
            raw_candidates = reader.collect_all_candidates(watermark_store=store, now=ref_now)
        except Exception as e:
            logger.error("[RECONCILIATION] Failed to collect candidates from sources: %s", e)
            return ReconciliationResult(
                status=ReconciliationStatus.SOURCE_COLLECTION_FAILURE,
                errors={"error": str(e)},
                reconciled_at=ref_now_iso,
                details=f"Source collection failure: {e}",
            )

        # -------------------------------------------------------------------
        # 3. Deterministic Sorting & Eligibility Separation
        # -------------------------------------------------------------------
        sorted_candidates = sort_candidates(raw_candidates)
        total_detected = len(sorted_candidates)

        eligible_candidates: List[EventCandidate] = []
        already_delivered_count = 0

        for c in sorted_candidates:
            if c.is_delivered or snapshot.is_delivered(c.event_id):
                already_delivered_count += 1
            else:
                eligible_candidates.append(c)

        if not eligible_candidates:
            # Safe empty scan: advance last_startup_reconciliation
            try:
                store.advance_watermark(WATERMARK_LAST_STARTUP_RECONCILIATION, ref_now_iso, now=ref_now)
            except EventStoreLockTimeoutError as e:
                logger.error("[RECONCILIATION] Lock timeout advancing reconciliation watermark: %s", e)
                return ReconciliationResult(
                    status=ReconciliationStatus.LOCK_TIMEOUT,
                    errors={"error": str(e)},
                    reconciled_at=ref_now_iso,
                    details="Lock timeout advancing reconciliation watermark.",
                )
            except Exception as e:
                logger.warning("[RECONCILIATION] Failed to advance reconciliation watermark: %s", e)

            # Prune delivered events older than 30 days
            pruned_count = 0
            try:
                pruned_count = store.prune_delivered_events(retention_days=30, now=ref_now)
            except Exception as e:
                logger.warning("[RECONCILIATION] Failed to prune delivered events: %s", e)

            return ReconciliationResult(
                status=ReconciliationStatus.NO_ELIGIBLE_CANDIDATES,
                total_detected=total_detected,
                eligible_count=0,
                selected_count=0,
                delivered_count=0,
                skipped_count=already_delivered_count,
                overflow_count=0,
                pruned_delivered_count=pruned_count,
                orphaned_temp_files_cleaned=orphaned_cleaned,
                advanced_watermarks={WATERMARK_LAST_STARTUP_RECONCILIATION: ref_now_iso},
                reconciled_at=ref_now_iso,
                details="No eligible missed candidates detected; existing watermarks remain authoritative.",
            )

        # -------------------------------------------------------------------
        # 4. Bounded Selection and Overflow Partitioning
        # -------------------------------------------------------------------
        selected_candidates = eligible_candidates[:limit]
        overflow_candidates = eligible_candidates[limit:]
        overflow_ids = [c.event_id for c in overflow_candidates]

        # -------------------------------------------------------------------
        # 5. Dispatch Delivery Orchestration
        # -------------------------------------------------------------------
        orchestrator = self.orchestrator or EventDeliveryOrchestrator(
            db_path=self.db_path,
            watermark_store=store,
            watermark_store_path=self.watermark_store_path,
            delivery_adapter=delivery_iface,
            default_batch_limit=limit,
        )

        try:
            # We pass all sorted_candidates with batch_limit=limit.
            # In EventDeliveryOrchestrator, unattempted overflow candidates act as
            # an unconfirmed barrier, guaranteeing watermarks do not jump past overflow!
            batch_res = orchestrator.execute_delivery_cycle(
                candidates=sorted_candidates,
                batch_limit=limit,
                adapter=delivery_iface,
                now=ref_now,
            )
        except EventStoreLockTimeoutError as e:
            logger.error("[RECONCILIATION] Lock timeout during delivery cycle: %s", e)
            return ReconciliationResult(
                status=ReconciliationStatus.LOCK_TIMEOUT,
                errors={"error": str(e)},
                total_detected=total_detected,
                eligible_count=len(eligible_candidates),
                selected_count=len(selected_candidates),
                overflow_count=len(overflow_candidates),
                overflow_event_ids=overflow_ids,
                reconciled_at=ref_now_iso,
                details="Lock timeout during delivery orchestration cycle.",
            )
        except (EventStoreError, OSError) as e:
            logger.error("[RECONCILIATION] Persistence error during delivery: %s", e)
            return ReconciliationResult(
                status=ReconciliationStatus.PERSISTENCE_FAILURE,
                errors={"error": str(e)},
                total_detected=total_detected,
                eligible_count=len(eligible_candidates),
                selected_count=len(selected_candidates),
                overflow_count=len(overflow_candidates),
                overflow_event_ids=overflow_ids,
                reconciled_at=ref_now_iso,
                details=f"Persistence error during delivery: {e}",
            )
        except Exception as e:
            logger.error("[RECONCILIATION] Delivery orchestrator failure: %s", e)
            return ReconciliationResult(
                status=ReconciliationStatus.DELIVERY_FAILURE,
                errors={"error": str(e)},
                total_detected=total_detected,
                eligible_count=len(eligible_candidates),
                selected_count=len(selected_candidates),
                overflow_count=len(overflow_candidates),
                overflow_event_ids=overflow_ids,
                reconciled_at=ref_now_iso,
                details=f"Orchestrator delivery failure: {e}",
            )

        # -------------------------------------------------------------------
        # 6. Record Reconciliation Watermark & Prune Delivered Retention
        # -------------------------------------------------------------------
        advanced_wms = dict(batch_res.advanced_watermarks)
        try:
            store.advance_watermark(WATERMARK_LAST_STARTUP_RECONCILIATION, ref_now_iso, now=ref_now)
            advanced_wms[WATERMARK_LAST_STARTUP_RECONCILIATION] = ref_now_iso
        except Exception as e:
            logger.warning("[RECONCILIATION] Failed to advance last_startup_reconciliation: %s", e)

        # Prune delivered events older than 30 days
        pruned_count = 0
        try:
            pruned_count = store.prune_delivered_events(retention_days=30, now=ref_now)
        except Exception as e:
            logger.warning("[RECONCILIATION] Failed to prune delivered events: %s", e)

        # -------------------------------------------------------------------
        # 7. Evaluate Final Status
        # -------------------------------------------------------------------
        if batch_res.failed_count > 0 or batch_res.ambiguous_count > 0 or len(overflow_candidates) > 0:
            if batch_res.delivered_count > 0:
                final_status = ReconciliationStatus.PARTIAL_SUCCESS
            else:
                final_status = ReconciliationStatus.DELIVERY_FAILURE
        else:
            final_status = ReconciliationStatus.SUCCESS

        detail_msg = (
            f"Reconciliation cycle completed: {batch_res.delivered_count} delivered, "
            f"{batch_res.failed_count} failed, {batch_res.ambiguous_count} ambiguous, "
            f"{len(overflow_candidates)} overflow deferred."
        )

        return ReconciliationResult(
            status=final_status,
            is_first_run=False,
            baseline_established=False,
            total_detected=total_detected,
            eligible_count=len(eligible_candidates),
            selected_count=len(selected_candidates),
            delivered_count=batch_res.delivered_count,
            failed_count=batch_res.failed_count,
            ambiguous_count=batch_res.ambiguous_count,
            skipped_count=batch_res.skipped_count,
            overflow_count=len(overflow_candidates),
            pruned_delivered_count=pruned_count,
            orphaned_temp_files_cleaned=orphaned_cleaned,
            advanced_watermarks=advanced_wms,
            delivered_event_ids=batch_res.delivered_event_ids,
            failed_event_ids=batch_res.failed_event_ids,
            ambiguous_event_ids=batch_res.ambiguous_event_ids,
            overflow_event_ids=overflow_ids,
            errors=batch_res.errors,
            reconciled_at=ref_now_iso,
            details=detail_msg,
        )
