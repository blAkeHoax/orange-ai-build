"""
test_stage_3_7e_4_hardening.py - Operational Hardening Test Suite.

Stage 3.7E.4: Controlled Operational Hardening
Verifies:
1. Multi-instance GUI protection (Windows mutex acquisition, competing rejection, release).
2. Delivered event pruning (30-day retention) preserving watermarks.
3. Recommendation store retention with byte-for-byte baseline immunity.
4. Orphaned temporary file cleanup (>2h removed, <2h preserved, locks/backups preserved).
5. Scheduler callback return contract (True, False, None handling).
"""

from datetime import datetime, timezone, timedelta
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from gui.single_instance import SingleInstanceGuard, check_single_instance
from brain.briefing.event_store import (
    EventWatermarkStore,
    DeliveredEventRecord,
    WATERMARK_JOBS_COMPLETED_AT,
    WATERMARK_LAST_STARTUP_RECONCILIATION,
)
from brain.briefing.models import (
    EventCandidate,
    EventPriority,
    ReconciliationStatus,
    ReconciliationResult,
)
from brain.briefing.reconciliation import (
    StartupReconciler,
    cleanup_orphaned_temp_files,
)
from brain.briefing.recommendations import (
    RecommendationStore,
    Recommendation,
    RecommendationState,
    RecommendationPriority,
    PROTECTED_BASELINE_REC_IDS,
)
import brain.scheduler as scheduler


class TestSingleInstanceGuard(unittest.TestCase):
    """Test suite for single-instance GUI process guard."""

    def setUp(self):
        self.test_mutex_name = f"Local\\OrangeAITestMutex_{int(time.time() * 1000)}"
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_lock_file = os.path.join(self.tmp_dir.name, "instance.lock")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_mutex_acquire_and_release(self):
        guard1 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)
        guard2 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)

        # First instance acquires successfully
        self.assertTrue(guard1.acquire())
        self.assertTrue(guard1.is_held)

        # Second instance fails to acquire while first is active
        self.assertFalse(guard2.acquire())
        self.assertFalse(guard2.is_held)

        # First instance releases
        guard1.release()
        self.assertFalse(guard1.is_held)

        # Second instance can now acquire
        self.assertTrue(guard2.acquire())
        self.assertTrue(guard2.is_held)
        guard2.release()

    def test_file_lock_fallback_acquire_and_release(self):
        # Force file lock mode via force_file_lock=True
        guard1 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)
        guard2 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)

        self.assertTrue(guard1.acquire(force_file_lock=True))
        self.assertTrue(guard1.is_held)

        self.assertFalse(guard2.acquire(force_file_lock=True))
        self.assertFalse(guard2.is_held)

        guard1.release()
        self.assertFalse(guard1.is_held)

        self.assertTrue(guard2.acquire(force_file_lock=True))
        self.assertTrue(guard2.is_held)
        guard2.release()

    def test_check_single_instance_helper(self):
        guard1 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)
        self.assertTrue(guard1.acquire())

        alert_called = []
        def mock_alert(title, msg):
            alert_called.append((title, msg))

        # Check competing instance with mock alert
        can_proceed, competing_guard = check_single_instance(
            mutex_name=self.test_mutex_name,
            lock_file_path=self.test_lock_file,
            show_dialog=True,
            dialog_fn=mock_alert,
        )

        self.assertFalse(can_proceed)
        self.assertIsNone(competing_guard)
        self.assertEqual(len(alert_called), 1)
        self.assertIn("Already Running", alert_called[0][0])

        guard1.release()

    def test_allow_multiple_instances_override(self):
        with patch.dict(os.environ, {"ORANGE_AI_ALLOW_MULTIPLE_INSTANCES": "1"}):
            guard1 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)
            guard2 = SingleInstanceGuard(mutex_name=self.test_mutex_name, lock_file_path=self.test_lock_file)

            self.assertTrue(guard1.acquire())
            # Under env override, second guard still succeeds
            self.assertTrue(guard2.acquire())
            guard1.release()
            guard2.release()


class TestOrphanedTempFileCleanup(unittest.TestCase):
    """Test safe cleanup of abandoned temporary write files."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.primary_file = os.path.join(self.tmp_dir.name, "store.json")
        with open(self.primary_file, "w", encoding="utf-8") as f:
            f.write("{}")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cleanup_orphaned_temp_files(self):
        ref_now = datetime.now(timezone.utc)
        old_mtime = (ref_now - timedelta(hours=3)).timestamp()
        recent_mtime = (ref_now - timedelta(minutes=10)).timestamp()

        # 1. Old temp file matching store.json.tmp.* (>2h) -> MUST BE DELETED
        old_tmp_1 = os.path.join(self.tmp_dir.name, "store.json.tmp.123_456")
        with open(old_tmp_1, "w", encoding="utf-8") as f:
            f.write("tmp data 1")
        os.utime(old_tmp_1, (old_mtime, old_mtime))

        # 2. Old temp file matching store.json.tmp (>2h) -> MUST BE DELETED
        old_tmp_2 = os.path.join(self.tmp_dir.name, "store.json.tmp")
        with open(old_tmp_2, "w", encoding="utf-8") as f:
            f.write("tmp data 2")
        os.utime(old_tmp_2, (old_mtime, old_mtime))

        # 3. Recent temp file (<2h) -> MUST BE PRESERVED
        recent_tmp = os.path.join(self.tmp_dir.name, "store.json.tmp.789_012")
        with open(recent_tmp, "w", encoding="utf-8") as f:
            f.write("recent tmp data")
        os.utime(recent_tmp, (recent_mtime, recent_mtime))

        # 4. Lock file (>2h) -> MUST BE PRESERVED
        lock_file = os.path.join(self.tmp_dir.name, "store.json.lock")
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write("lock")
        os.utime(lock_file, (old_mtime, old_mtime))

        # 5. Backup file (>2h) -> MUST BE PRESERVED
        bak_file = os.path.join(self.tmp_dir.name, "store.json.bak")
        with open(bak_file, "w", encoding="utf-8") as f:
            f.write("backup")
        os.utime(bak_file, (old_mtime, old_mtime))

        # 6. Corrupt archive (>2h) -> MUST BE PRESERVED
        corrupt_file = os.path.join(self.tmp_dir.name, "store.json.corrupt.20261031_120000")
        with open(corrupt_file, "w", encoding="utf-8") as f:
            f.write("corrupt")
        os.utime(corrupt_file, (old_mtime, old_mtime))

        # Execute cleanup
        cleaned = cleanup_orphaned_temp_files(
            target_files=[self.primary_file],
            max_age_seconds=7200.0,
            now=ref_now,
        )

        self.assertEqual(cleaned, 2)
        # Old temp files deleted
        self.assertFalse(os.path.exists(old_tmp_1))
        self.assertFalse(os.path.exists(old_tmp_2))
        # Non-temp or recent files preserved
        self.assertTrue(os.path.exists(recent_tmp))
        self.assertTrue(os.path.exists(lock_file))
        self.assertTrue(os.path.exists(bak_file))
        self.assertTrue(os.path.exists(corrupt_file))
        self.assertTrue(os.path.exists(self.primary_file))


class TestOperationalRetention(unittest.TestCase):
    """Test suite for event watermark pruning and recommendation store baseline protection."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.watermark_path = os.path.join(self.tmp_dir.name, "event_watermarks.json")
        self.rec_path = os.path.join(self.tmp_dir.name, "recommendations.json")
        # Enforce strict isolation so test cannot touch production store
        RecommendationStore._STRICT_TEST_ISOLATION = True

    def tearDown(self):
        RecommendationStore._STRICT_TEST_ISOLATION = False
        self.tmp_dir.cleanup()

    def test_watermark_store_30_day_retention_pruning(self):
        store = EventWatermarkStore.initialize_first_run(self.watermark_path)
        ref_now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Set up a watermark to verify it is NOT touched
        store.advance_watermark(WATERMARK_JOBS_COMPLETED_AT, "2026-11-10T10:00:00+00:00", now=ref_now)

        # Add 1 old delivered event (>30d ago: Oct 10)
        old_record = DeliveredEventRecord(
            event_id="job_completed:job_old_123:abc",
            delivered_at="2026-10-10T10:00:00+00:00",
            event_type="job_completed",
            entity_id="job_old_123",
        )
        # Add 1 recent delivered event (<30d ago: Nov 10)
        recent_record = DeliveredEventRecord(
            event_id="job_completed:job_recent_456:def",
            delivered_at="2026-11-10T10:00:00+00:00",
            event_type="job_completed",
            entity_id="job_recent_456",
        )

        with store.lock(reload=False):
            store._delivered_events[old_record.event_id] = old_record
            store._delivered_events[recent_record.event_id] = recent_record
            store._save_unlocked()

        # Prune older than 30 days
        pruned_count = store.prune_delivered_events(retention_days=30, now=ref_now)
        self.assertEqual(pruned_count, 1)

        # Reload and verify
        reloaded = EventWatermarkStore.load(self.watermark_path)
        snap = reloaded.get_snapshot()

        # Old is gone
        self.assertFalse(snap.is_delivered(old_record.event_id))
        # Recent is preserved
        self.assertTrue(snap.is_delivered(recent_record.event_id))
        # Watermark is intact
        self.assertEqual(snap.get_watermark(WATERMARK_JOBS_COMPLETED_AT), "2026-11-10T10:00:00+00:00")

    def test_reconciliation_cycle_prunes_delivered_events(self):
        # Create empty sqlite db
        db_path = os.path.join(self.tmp_dir.name, "test_memory.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, goal TEXT, status TEXT, priority INTEGER, attempt_count INTEGER, max_attempts INTEGER, step_count INTEGER, last_error TEXT, pending_confirmation_json TEXT, created_at TEXT, updated_at TEXT, started_at TEXT, completed_at TEXT, source TEXT, tags TEXT)")
        conn.execute("CREATE TABLE job_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, step_index INTEGER NOT NULL, action_type TEXT NOT NULL, tool_name TEXT, arguments_json TEXT, reasoning TEXT, success INTEGER, result_data_json TEXT, error TEXT, started_at TEXT NOT NULL, completed_at TEXT, duration_ms INTEGER, model_name TEXT)")
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, due_at TEXT, fired INTEGER, created_at TEXT)")
        conn.execute("CREATE TABLE structured_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, memory_key TEXT NOT NULL, value TEXT NOT NULL, confidence REAL DEFAULT 1.0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(category, memory_key))")
        conn.commit()
        conn.close()

        store = EventWatermarkStore.initialize_first_run(self.watermark_path)
        ref_now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Add an old delivered record (>30d)
        old_rec = DeliveredEventRecord(
            event_id="job_completed:job_old:xyz",
            delivered_at="2026-10-01T12:00:00+00:00",
            event_type="job_completed",
            entity_id="job_old",
        )
        with store.lock(reload=False):
            store._delivered_events[old_rec.event_id] = old_rec
            store._save_unlocked()

        reconciler = StartupReconciler(
            db_path=db_path,
            watermark_store=store,
            watermark_store_path=self.watermark_path,
        )

        res = reconciler.reconcile(now=ref_now)
        self.assertEqual(res.pruned_delivered_count, 1)
        self.assertFalse(store.get_snapshot().is_delivered("job_completed:job_old:xyz"))

    def test_recommendation_store_preserves_baseline_immunity(self):
        store = RecommendationStore(file_path=self.rec_path)
        ref_now = datetime(2026, 11, 15, 12, 0, 0)
        old_iso = (ref_now - timedelta(days=40)).isoformat()

        # Take one ID from PROTECTED_BASELINE_REC_IDS
        baseline_id = next(iter(PROTECTED_BASELINE_REC_IDS))

        # 1. Baseline record marked STALE >30d ago
        rec_baseline = Recommendation(
            rec_id=baseline_id,
            category="job_confirmation",
            entity_id="00182864a197",
            recommendation_type="approval_pending",
            title="Baseline Title",
            explanation="Baseline explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="baseline_fp_1234",
            state=RecommendationState.STALE,
            created_at=old_iso,
            updated_at=old_iso,
        )

        # 2. Non-baseline STALE record >30d ago
        rec_stale_nonbaseline = Recommendation(
            rec_id="job_confirmation:new_test_1:approval_pending",
            category="job_confirmation",
            entity_id="new_test_1",
            recommendation_type="approval_pending",
            title="Nonbaseline Stale",
            explanation="Stale explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="stale_fp_12345678",
            state=RecommendationState.STALE,
            created_at=old_iso,
            updated_at=old_iso,
        )

        # 3. Non-baseline ACTIVE record >30d ago (ACTIVE must never be pruned)
        rec_active_nonbaseline = Recommendation(
            rec_id="job_confirmation:new_test_2:approval_pending",
            category="job_confirmation",
            entity_id="new_test_2",
            recommendation_type="approval_pending",
            title="Nonbaseline Active",
            explanation="Active explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="active_fp_1234567",
            state=RecommendationState.ACTIVE,
            created_at=old_iso,
            updated_at=old_iso,
        )

        # 4. Non-baseline HANDLED record >30d ago (HANDLED must never be pruned)
        rec_handled_nonbaseline = Recommendation(
            rec_id="job_confirmation:new_test_3:approval_pending",
            category="job_confirmation",
            entity_id="new_test_3",
            recommendation_type="approval_pending",
            title="Nonbaseline Handled",
            explanation="Handled explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="handled_fp_123456",
            state=RecommendationState.HANDLED,
            created_at=old_iso,
            updated_at=old_iso,
        )

        # 5. Non-baseline DISMISSED record >30d ago (DISMISSED >30d is eligible)
        rec_dismissed_nonbaseline = Recommendation(
            rec_id="job_confirmation:new_test_4:approval_pending",
            category="job_confirmation",
            entity_id="new_test_4",
            recommendation_type="approval_pending",
            title="Nonbaseline Dismissed",
            explanation="Dismissed explanation",
            suggested_action="Action",
            priority=RecommendationPriority.HIGH,
            evidence_fingerprint="dismissed_fp_1234",
            state=RecommendationState.DISMISSED,
            created_at=old_iso,
            updated_at=old_iso,
        )

        with store.lock(reload=False):
            store._items[rec_baseline.rec_id] = rec_baseline
            store._items[rec_stale_nonbaseline.rec_id] = rec_stale_nonbaseline
            store._items[rec_active_nonbaseline.rec_id] = rec_active_nonbaseline
            store._items[rec_handled_nonbaseline.rec_id] = rec_handled_nonbaseline
            store._items[rec_dismissed_nonbaseline.rec_id] = rec_dismissed_nonbaseline
            store.save()

        # Run pruning with baseline preservation enabled
        pruned_count = store.prune_expired_records(retention_days=30, now=ref_now, preserve_baseline=True)

        # Only the 2 non-baseline expired records (stale and dismissed) should be pruned
        self.assertEqual(pruned_count, 2)

        # Verify items in store
        self.assertIsNotNone(store.get(baseline_id), "Baseline record MUST be preserved!")
        self.assertIsNone(store.get(rec_stale_nonbaseline.rec_id))
        self.assertIsNotNone(store.get(rec_active_nonbaseline.rec_id))
        self.assertIsNotNone(store.get(rec_handled_nonbaseline.rec_id))
        self.assertIsNone(store.get(rec_dismissed_nonbaseline.rec_id))

    def test_zero_write_on_no_eligible_pruning(self):
        store = RecommendationStore(file_path=self.rec_path)
        ref_now = datetime(2026, 11, 15, 12, 0, 0)
        old_iso = (ref_now - timedelta(days=40)).isoformat()

        # Only add baseline records
        for bid in list(PROTECTED_BASELINE_REC_IDS)[:5]:
            store._items[bid] = Recommendation(
                rec_id=bid,
                category="job_confirmation",
                entity_id="ent_test",
                recommendation_type="approval_pending",
                title="Title",
                explanation="Exp",
                suggested_action="Action",
                priority=RecommendationPriority.HIGH,
                evidence_fingerprint="fp_test",
                state=RecommendationState.STALE,
                created_at=old_iso,
                updated_at=old_iso,
            )
        store.save()

        orig_mtime = os.path.getmtime(self.rec_path)
        time.sleep(0.05)

        with patch.object(store, "_save_unlocked", wraps=store._save_unlocked) as mock_save:
            pruned = store.prune_expired_records(retention_days=30, now=ref_now, preserve_baseline=True)
            self.assertEqual(pruned, 0)
            mock_save.assert_not_called()

        self.assertEqual(os.path.getmtime(self.rec_path), orig_mtime)


class TestSchedulerCallbackContract(unittest.TestCase):
    """Test explicit scheduler callback return contracts."""

    def test_callback_returning_false_does_not_mark_fired(self):
        mock_reminder = {"id": 101, "message": "Test reminder", "due_at": "2026-10-31T12:00:00"}

        with patch("brain.scheduler.get_due_reminders", return_value=[mock_reminder]), \
             patch("brain.scheduler.mark_fired") as mock_mark_fired:

            def on_due(reminder):
                return False  # Do NOT mark fired

            scheduler.check_once(on_due, auto_mark_fired=True)
            mock_mark_fired.assert_not_called()

    def test_callback_returning_true_marks_fired(self):
        mock_reminder = {"id": 102, "message": "Test reminder", "due_at": "2026-10-31T12:00:00"}

        with patch("brain.scheduler.get_due_reminders", return_value=[mock_reminder]), \
             patch("brain.scheduler.mark_fired") as mock_mark_fired:

            def on_due(reminder):
                return True  # Confirmed fired

            scheduler.check_once(on_due, auto_mark_fired=False)
            mock_mark_fired.assert_called_once_with(102)

    def test_callback_returning_none_with_auto_mark_fired(self):
        mock_reminder = {"id": 103, "message": "Test reminder", "due_at": "2026-10-31T12:00:00"}

        with patch("brain.scheduler.get_due_reminders", return_value=[mock_reminder]), \
             patch("brain.scheduler.mark_fired") as mock_mark_fired:

            def on_due_none(reminder):
                return None  # Legacy contract

            # With auto_mark_fired=True, marks fired
            scheduler.check_once(on_due_none, auto_mark_fired=True)
            mock_mark_fired.assert_called_once_with(103)

        with patch("brain.scheduler.get_due_reminders", return_value=[mock_reminder]), \
             patch("brain.scheduler.mark_fired") as mock_mark_fired:

            # With auto_mark_fired=False, does NOT mark fired
            scheduler.check_once(on_due_none, auto_mark_fired=False)
            mock_mark_fired.assert_not_called()


if __name__ == "__main__":
    unittest.main()
