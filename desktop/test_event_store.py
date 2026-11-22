"""
test_event_store.py - Test Suite for Event Watermark Persistence & Deduplication.

Stage 3.7E.3A: Watermark Persistence & Deduplication Foundation
Verifies:
1. Store lifecycle (missing file, explicit first run, baseline vs historical, valid reload, unsupported version).
2. Corruption handling (malformed JSON preserved, timestamped backup without overwriting, invalid types fail closed, no empty replacement).
3. Atomicity and concurrency (atomic writes, thread concurrency, cross-process concurrency, lost-update prevention, lock timeout, exception cleanup).
4. Watermark behavior (snapshot reads no mutation, explicit watermark advance, monotonicity, failed delivery no-op, confirmed delivery recording).
5. Event identity (stable across calls/restarts, normalization, material changes produce distinct IDs, incidental metadata ignored).
6. Retention (bounded 30-day pruning, recent entries preserved, watermarks never pruned, injected clock).
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

from brain.briefing.event_store import (
    EventWatermarkStore,
    EventStoreSnapshot,
    EventStoreError,
    EventStoreNotFoundError,
    EventStoreCorruptError,
    EventStoreSchemaError,
    EventStoreLockTimeoutError,
    CrossProcessFileLock,
    EventPayload,
    DeliveredEventRecord,
    generate_event_id,
    clean_material_data,
    normalize_utc_timestamp,
    parse_utc_timestamp,
    EVENT_WATERMARKS_FILENAME,
    EVENT_STORE_SCHEMA_VERSION,
    DEFAULT_RETENTION_DAYS,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_JOB_STEP_MAX_ID,
    WATERMARK_MEMORIES_UPDATED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)


class BaseEventStoreTestCase(unittest.TestCase):
    """Base test fixture providing isolated temporary directory."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="orange_test_event_store_")
        self.store_path = os.path.join(self.test_dir, EVENT_WATERMARKS_FILENAME)
        self.lock_path = f"{self.store_path}.lock"
        self.fixed_now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass


# ===========================================================================
# Suite 1: Store Lifecycle
# ===========================================================================

class TestEventStoreLifecycle(BaseEventStoreTestCase):
    """Tests normal loading, missing file rejection, explicit first-run initialization, and version checks."""

    def test_missing_file_fails_closed_during_normal_load(self):
        """Normal load() on a non-existent file must raise EventStoreNotFoundError and not create the file."""
        self.assertFalse(os.path.exists(self.store_path))
        with self.assertRaises(EventStoreNotFoundError):
            EventWatermarkStore.load(self.store_path)
        self.assertFalse(os.path.exists(self.store_path), "Missing file should not be created silently on load()")

    def test_explicit_first_run_initialization(self):
        """Explicit initialize_first_run() creates documented baseline state with correct initial watermarks."""
        store = EventWatermarkStore.initialize_first_run(
            file_path=self.store_path,
            initial_watermarks={
                WATERMARK_JOB_STEP_MAX_ID: 15,
                WATERMARK_JOBS_COMPLETED_AT: "2026-10-31T09:00:00Z",
            },
            now=self.fixed_now,
        )
        self.assertTrue(os.path.exists(self.store_path))
        snap = store.get_snapshot()
        self.assertEqual(snap.version, EVENT_STORE_SCHEMA_VERSION)
        self.assertTrue(snap.is_first_run_baseline)
        self.assertEqual(snap.created_at, "2026-10-31T10:00:00+00:00")
        self.assertEqual(snap.updated_at, "2026-10-31T10:00:00+00:00")
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 15)
        self.assertEqual(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T09:00:00+00:00")
        self.assertIsNone(snap.get_watermark(WATERMARK_MEMORIES_UPDATED_AT))
        self.assertEqual(len(snap.delivered_events), 0)

    def test_first_run_cannot_overwrite_existing_store(self):
        """Calling initialize_first_run() on an existing file must raise FileExistsError."""
        EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        self.assertTrue(os.path.exists(self.store_path))

        with self.assertRaises(FileExistsError):
            EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

    def test_first_run_baseline_distinguishable_from_advanced_state(self):
        """After explicitly advancing watermarks or recording deliveries, is_first_run_baseline becomes False."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        self.assertTrue(store.get_snapshot().is_first_run_baseline)

        # Advance watermark
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 20)
        snap = store.get_snapshot()
        self.assertFalse(snap.is_first_run_baseline, "Baseline flag must clear after operational advancement")

        # Reload from disk
        reloaded = EventWatermarkStore.load(self.store_path)
        self.assertFalse(reloaded.get_snapshot().is_first_run_baseline)
        self.assertEqual(reloaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 20)

    def test_valid_store_reloads_correctly(self):
        """A valid persisted store reloads all attributes without loss."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.record_delivery("evt_test_123", metadata={"event_type": "job_completed", "entity_id": "job_1"})
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 42)

        reloaded = EventWatermarkStore.load(self.store_path)
        self.assertTrue(reloaded.is_delivered("evt_test_123"))
        self.assertEqual(reloaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 42)

    def test_unsupported_schema_version_fails_closed(self):
        """Unsupported schema version in JSON raises EventStoreSchemaError."""
        bad_payload = {
            "version": 999,
            "created_at": "2026-10-31T10:00:00Z",
            "updated_at": "2026-10-31T10:00:00Z",
            "is_first_run_baseline": False,
            "watermarks": {},
            "delivered_events": {},
        }
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(bad_payload, f)

        with self.assertRaises(EventStoreSchemaError):
            EventWatermarkStore.load(self.store_path)


# ===========================================================================
# Suite 2: Corruption Handling
# ===========================================================================

class TestEventStoreCorruptionHandling(BaseEventStoreTestCase):
    """Tests non-destructive backup creation, refusal to overwrite backups, and fail-closed operation."""

    def test_malformed_json_preserved_and_backed_up(self):
        """Malformed JSON file is preserved on disk and copied to a non-destructive timestamped backup."""
        corrupt_content = '{"version": 1, "watermarks": { BROKEN JSON '
        with open(self.store_path, "w", encoding="utf-8") as f:
            f.write(corrupt_content)

        with self.assertRaises(EventStoreCorruptError):
            EventWatermarkStore.load(self.store_path)

        # Original file is preserved intact
        with open(self.store_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), corrupt_content)

        # Backup file was created
        backups = [f for f in os.listdir(self.test_dir) if "corrupt" in f]
        self.assertEqual(len(backups), 1)
        backup_full = os.path.join(self.test_dir, backups[0])
        with open(backup_full, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), corrupt_content)

    def test_backup_does_not_overwrite_prior_backups(self):
        """Repeated corruption detections do not overwrite existing backup files; unique filenames are generated."""
        corrupt_content = "{NOT VALID JSON}"
        with open(self.store_path, "w", encoding="utf-8") as f:
            f.write(corrupt_content)

        # Trigger load failure twice
        with self.assertRaises(EventStoreCorruptError):
            EventWatermarkStore.load(self.store_path)
        with self.assertRaises(EventStoreCorruptError):
            EventWatermarkStore.load(self.store_path)

        backups = [f for f in os.listdir(self.test_dir) if "corrupt" in f]
        self.assertGreaterEqual(len(backups), 2, "Second failure must not overwrite first backup")

    def test_invalid_field_types_fail_closed(self):
        """Invalid types for watermarks or timestamps raise EventStoreSchemaError."""
        invalid_payload = {
            "version": 1,
            "created_at": "2026-10-31T10:00:00Z",
            "updated_at": "2026-10-31T10:00:00Z",
            "is_first_run_baseline": "NOT_A_BOOLEAN",  # Invalid type
            "watermarks": {
                WATERMARK_JOB_STEP_MAX_ID: "not-an-int",
            },
            "delivered_events": {},
        }
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(invalid_payload, f)

        with self.assertRaises(EventStoreSchemaError):
            EventWatermarkStore.load(self.store_path)

    def test_failed_recovery_does_not_create_empty_replacement(self):
        """Failed load must never replace the corrupt file with an empty store."""
        corrupt_content = "INVALID DATA"
        with open(self.store_path, "w", encoding="utf-8") as f:
            f.write(corrupt_content)

        with self.assertRaises(EventStoreCorruptError):
            EventWatermarkStore.load(self.store_path)

        with open(self.store_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), corrupt_content)

    def test_operations_blocked_when_in_corrupt_state(self):
        """Mutative operations (advance, record, save) raise EventStoreCorruptError when store is corrupt."""
        store = EventWatermarkStore(self.store_path)
        store._is_corrupt = True

        with self.assertRaises(EventStoreCorruptError):
            store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 10)

        with self.assertRaises(EventStoreCorruptError):
            store.record_delivery("evt_abc")

        self.assertFalse(store.save(), "Save must return False on corrupt store")


# ===========================================================================
# Suite 3: Atomicity and Concurrency
# ===========================================================================

class TestAtomicityAndConcurrency(BaseEventStoreTestCase):
    """Tests atomic file replacements, cross-process and thread concurrency, and lost-update prevention."""

    def test_atomic_write_creates_valid_json(self):
        """Writes must result in valid JSON with temporary file cleanup."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.record_delivery("evt_1", metadata={"event_type": "job_completed"})

        # Check file validity
        with open(self.store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("evt_1", data["delivered_events"])

        # Temporary files should be cleaned up
        tmp_files = [f for f in os.listdir(self.test_dir) if ".tmp" in f]
        self.assertEqual(len(tmp_files), 0)

    def test_failed_write_preserves_valid_store(self):
        """If a write fails during JSON generation or temporary handling, the previous valid store remains intact."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 100)

        # Inject an un-serializable object into watermarks
        class Unserializable:
            pass

        with store.lock(reload=False):
            store._watermarks["bad_key"] = Unserializable()
            success = store._save_unlocked()
            self.assertFalse(success, "Save should fail gracefully")

        # Reload store from disk and verify it still has valid state
        reloaded = EventWatermarkStore.load(self.store_path)
        self.assertEqual(reloaded.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 100)
        self.assertNotIn("bad_key", reloaded.get_snapshot().watermarks)

    def test_thread_concurrent_mutations_no_lost_updates(self):
        """Concurrent threads recording different delivery IDs must not lose updates."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        num_threads = 10
        events_per_thread = 5

        def worker(thread_idx: int):
            for i in range(events_per_thread):
                evt_id = f"evt_thread_{thread_idx}_item_{i}"
                store.record_delivery(evt_id, metadata={"thread": thread_idx, "idx": i})
                time.sleep(0.005)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Reload and verify all events persisted
        reloaded = EventWatermarkStore.load(self.store_path)
        snap = reloaded.get_snapshot()
        expected_total = num_threads * events_per_thread
        self.assertEqual(len(snap.delivered_events), expected_total)

    def test_cross_process_locking_and_lost_update_prevention(self):
        """Two distinct store instances simulating concurrent processes must not overwrite each other."""
        store_a = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store_b = EventWatermarkStore.load(self.store_path)

        # Process A records event 1
        store_a.record_delivery("evt_from_proc_a")

        # Process B advances watermark
        store_b.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 50)

        # Process A records event 2
        store_a.record_delivery("evt_from_proc_a_2")

        # Process B checks snapshot: under lock reload, it sees Process A's events
        with store_b.lock(reload=True):
            self.assertTrue(store_b.is_delivered("evt_from_proc_a"))
            self.assertTrue(store_b.is_delivered("evt_from_proc_a_2"))
            self.assertEqual(store_b.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 50)

        # Process A checks snapshot: under lock reload, it sees Process B's watermark
        with store_a.lock(reload=True):
            self.assertEqual(store_a.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 50)

    def test_lock_timeout_bounded_and_reported(self):
        """Acquiring a file lock with a short timeout while another lock is held raises EventStoreLockTimeoutError."""
        lock_file = os.path.join(self.test_dir, "timeout.lock")
        lock1 = CrossProcessFileLock(lock_file, timeout=0.2)
        lock2 = CrossProcessFileLock(lock_file, timeout=0.2)

        lock1.acquire()
        try:
            start_t = time.time()
            with self.assertRaises(EventStoreLockTimeoutError) as ctx:
                lock2.acquire()
            elapsed = time.time() - start_t
            self.assertGreaterEqual(elapsed, 0.15)
            self.assertIn("Timed out", str(ctx.exception))
        finally:
            lock1.release()

    def test_lock_released_after_exception(self):
        """Lock must be reliably released when an exception occurs inside the context block."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        try:
            with store.lock(reload=False):
                raise RuntimeError("Simulated failure inside locked block")
        except RuntimeError:
            pass

        # Another lock acquisition must succeed immediately
        acquired = store._file_lock.acquire()
        self.assertTrue(acquired)
        store._file_lock.release()


# ===========================================================================
# Suite 4: Watermark Behavior & Monotonicity
# ===========================================================================

class TestWatermarkBehavior(BaseEventStoreTestCase):
    """Tests watermark reads, explicit advancements, monotonicity enforcement, and delivery recording."""

    def test_snapshot_reads_do_not_mutate_state(self):
        """Reading snapshot must not alter created_at, updated_at, or any stored values."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        snap1 = store.get_snapshot()

        # Mutate the returned snapshot copy
        snap1.watermarks["custom"] = 999
        snap1.delivered_events["custom"] = DeliveredEventRecord("custom", "2026-10-31T12:00:00Z")

        snap2 = store.get_snapshot()
        self.assertNotIn("custom", snap2.watermarks)
        self.assertNotIn("custom", snap2.delivered_events)

    def test_explicit_single_watermark_advancement(self):
        """Advancing a watermark explicitly persists the updated value and timestamp."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 25, now=self.fixed_now + timedelta(minutes=10))

        snap = store.get_snapshot()
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 25)
        self.assertEqual(snap.updated_at, "2026-10-31T10:10:00+00:00")

    def test_explicit_batch_watermark_advancement(self):
        """Batch advancement updates all requested watermarks in a single atomic write."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        updates = {
            WATERMARK_JOB_STEP_MAX_ID: 10,
            WATERMARK_JOBS_COMPLETED_AT: "2026-10-31T09:30:00Z",
            WATERMARK_MEMORIES_UPDATED_AT: "2026-10-31T09:45:00Z",
        }
        store.advance_watermarks(updates)

        snap = store.get_snapshot()
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 10)
        self.assertEqual(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T09:30:00+00:00")
        self.assertEqual(snap.get_watermark(WATERMARK_MEMORIES_UPDATED_AT), "2026-10-31T09:45:00+00:00")

    def test_monotonic_integer_regression_rejected(self):
        """Advancing job_step_max_id to a lower value raises ValueError unless allow_regression=True."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 50)

        with self.assertRaises(ValueError):
            store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 40)

        # Verify it remained 50
        self.assertEqual(store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 50)

        # Allowed with allow_regression=True
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 40, allow_regression=True)
        self.assertEqual(store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 40)

    def test_monotonic_timestamp_regression_rejected(self):
        """Advancing a timestamp watermark to an earlier datetime raises ValueError unless allow_regression=True."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.advance_watermark(WATERMARK_JOBS_COMPLETED_AT, "2026-10-31T12:00:00Z")

        with self.assertRaises(ValueError):
            store.advance_watermark(WATERMARK_JOBS_COMPLETED_AT, "2026-10-31T11:00:00Z")

        self.assertEqual(store.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-10-31T12:00:00+00:00")

    def test_discovery_alone_does_not_advance_watermarks(self):
        """Generating an event ID or inspecting entities must not advance watermarks."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        _ = generate_event_id("job_completed", "job_123", {"status": "success"})

        snap = store.get_snapshot()
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 0)
        self.assertIsNone(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT))
        self.assertEqual(len(snap.delivered_events), 0)

    def test_failed_delivery_does_not_record_or_advance(self):
        """If delivery fails, neither record_delivery nor advance_watermark should be invoked."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        evt_id = generate_event_id("job_failed", "job_456")

        # Simulate simulated delivery failure
        delivery_succeeded = False
        if delivery_succeeded:
            store.record_delivery(evt_id)
            store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 99)

        # Confirm store remains untouched
        self.assertFalse(store.is_delivered(evt_id))
        self.assertEqual(store.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 0)

    def test_confirmed_delivery_records_and_detects_duplicates(self):
        """Confirmed delivery records event ID; subsequent attempts detect it as duplicate."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        evt_id = generate_event_id("job_completed", "job_789")

        self.assertFalse(store.is_delivered(evt_id))
        first_call = store.record_delivery(evt_id, metadata={"event_type": "job_completed"})
        self.assertTrue(first_call)
        self.assertTrue(store.is_delivered(evt_id))

        # Second call returns False (idempotent duplicate detection)
        second_call = store.record_delivery(evt_id)
        self.assertFalse(second_call)

    def test_batch_delivery_recording(self):
        """record_deliveries records multiple events atomically and returns new record count."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        events = [
            ("evt_batch_1", {"event_type": "job_completed"}),
            ("evt_batch_2", {"event_type": "job_failed"}),
        ]
        count = store.record_deliveries(events)
        self.assertEqual(count, 2)
        self.assertTrue(store.is_delivered("evt_batch_1"))
        self.assertTrue(store.is_delivered("evt_batch_2"))

        # Re-recording same batch returns 0 new
        count_repeat = store.record_deliveries(events)
        self.assertEqual(count_repeat, 0)


# ===========================================================================
# Suite 5: Stable Event Identity
# ===========================================================================

class TestEventIdentity(unittest.TestCase):
    """Tests deterministic SHA-256 event ID generation, normalization, and material vs incidental differentiation."""

    def test_identity_stable_across_calls(self):
        """Repeated generation with the same semantic payload produces the exact same ID."""
        id1 = generate_event_id("job_completed", "job_101", {"status": "SUCCESS", "exit_code": 0})
        id2 = generate_event_id("job_completed", "job_101", {"status": "SUCCESS", "exit_code": 0})
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("evt_job_completed_"))

    def test_identity_stable_across_simulated_restarts(self):
        """IDs generated in separate invocations or processes remain identical."""
        payload = {"step_id": 42, "error": "timeout"}
        id_first = generate_event_id("step_failed", "step_42", payload)

        # Simulate restart with fresh dict
        payload_restart = {"error": "timeout", "step_id": 42}  # Different key order
        id_restart = generate_event_id("step_failed", "step_42", payload_restart)
        self.assertEqual(id_first, id_restart)

    def test_deterministic_input_normalization(self):
        """Input formatting variations (casing, whitespace, int types) normalize deterministically."""
        id_a = generate_event_id("JOB_COMPLETED ", " job_200 ", {"count": 5})
        id_b = generate_event_id("job_completed", "job_200", {"count": 5})
        self.assertEqual(id_a, id_b)

    def test_material_changes_produce_distinct_ids(self):
        """A material change in evidence produces a distinct event ID."""
        id_pass = generate_event_id("job_finished", "job_300", {"status": "success", "error": None})
        id_fail = generate_event_id("job_finished", "job_300", {"status": "failure", "error": "Disk full"})
        self.assertNotEqual(id_pass, id_fail)

    def test_incidental_metadata_ignored_in_identity(self):
        """Incidental keys (scan time, briefing date, phrasing) do NOT change the event ID."""
        base_material = {"job_name": "backup", "target": "local"}
        id_base = generate_event_id("job_completed", "job_400", base_material)

        # Add incidental metadata
        incidental_material = dict(base_material)
        incidental_material.update({
            "scanned_at": "2026-10-31T11:00:00Z",
            "scan_time": "11:00:00",
            "briefing_date": "2026-10-31",
            "presentation_text": "Your backup job completed successfully!",
            "llm_summary": "Backup finished cleanly without error.",
            "ui_channel": "chat",
        })
        id_with_incidental = generate_event_id("job_completed", "job_400", incidental_material)
        self.assertEqual(id_base, id_with_incidental, "Incidental metadata must be filtered out")

    def test_invalid_inputs_rejected(self):
        """Empty event_type or entity_id raises ValueError."""
        with self.assertRaises(ValueError):
            generate_event_id("", "entity_1")
        with self.assertRaises(ValueError):
            generate_event_id("job_completed", "")
        with self.assertRaises(TypeError):
            generate_event_id("job_completed", "entity_1", material_data="NOT_A_DICT")


# ===========================================================================
# Suite 6: Deduplication Retention
# ===========================================================================

class TestDeduplicationRetention(BaseEventStoreTestCase):
    """Tests bounded 30-day retention pruning, preservation of recent IDs, and watermark immutability."""

    def test_old_entries_pruned_beyond_retention_window(self):
        """Entries older than 30 days are pruned; entries within 30 days are preserved."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        t_now = self.fixed_now
        t_10d_ago = t_now - timedelta(days=10)
        t_35d_ago = t_now - timedelta(days=35)
        t_60d_ago = t_now - timedelta(days=60)

        store.record_delivery("evt_recent", delivered_at=t_10d_ago)
        store.record_delivery("evt_old_35", delivered_at=t_35d_ago)
        store.record_delivery("evt_old_60", delivered_at=t_60d_ago)

        self.assertEqual(len(store.get_snapshot().delivered_events), 3)

        # Prune with 30-day retention at t_now
        pruned_count = store.prune_delivered_events(retention_days=30, now=t_now)
        self.assertEqual(pruned_count, 2)

        snap = store.get_snapshot()
        self.assertEqual(len(snap.delivered_events), 1)
        self.assertTrue(store.is_delivered("evt_recent"))
        self.assertFalse(store.is_delivered("evt_old_35"))
        self.assertFalse(store.is_delivered("evt_old_60"))

    def test_watermarks_never_pruned(self):
        """Pruning delivered events must never modify or clear watermark positions."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)
        store.advance_watermark(WATERMARK_JOB_STEP_MAX_ID, 77)
        store.advance_watermark(WATERMARK_JOBS_COMPLETED_AT, "2026-09-01T00:00:00Z")  # Very old date

        t_now = self.fixed_now
        store.record_delivery("evt_old", delivered_at=t_now - timedelta(days=45))

        # Prune
        store.prune_delivered_events(retention_days=30, now=t_now)

        snap = store.get_snapshot()
        self.assertEqual(snap.get_watermark(WATERMARK_JOB_STEP_MAX_ID), 77)
        self.assertEqual(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-09-01T00:00:00+00:00")
        self.assertFalse(store.is_delivered("evt_old"))

    def test_retention_with_injected_deterministic_clock(self):
        """Injected test clocks verify exact day-boundary pruning."""
        store = EventWatermarkStore.initialize_first_run(file_path=self.store_path, now=self.fixed_now)

        event_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        store.record_delivery("evt_new_year", delivered_at=event_time)

        # 29 days later: should not prune
        clock_29d = datetime(2026, 1, 30, 12, 0, 0, tzinfo=timezone.utc)
        pruned_29 = store.prune_delivered_events(retention_days=30, now=clock_29d)
        self.assertEqual(pruned_29, 0)
        self.assertTrue(store.is_delivered("evt_new_year"))

        # 31 days later: should prune
        clock_31d = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
        pruned_31 = store.prune_delivered_events(retention_days=30, now=clock_31d)
        self.assertEqual(pruned_31, 1)
        self.assertFalse(store.is_delivered("evt_new_year"))


if __name__ == "__main__":
    unittest.main()
