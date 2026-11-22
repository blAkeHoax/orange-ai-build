"""
test_stage_3_7e_5a_r_isolation.py - Storage Isolation & Recovery Regression Test Suite.

Stage 3.7E.5A-R:
Verifies that:
1. Default database with default store preserves existing behavior.
2. Custom database with explicit temporary recommendation store works.
3. Custom database without a recommendation store fails explicitly.
4. Custom database cannot mutate production recommendations.json.
5. An exception during collector initialization cannot cause production fallback.
6. RecommendationEvaluator respects explicit store injection.
7. Existing CLI and briefing functionality remains compatible.
8. Existing production recommendation records are not modified during tests.
"""

import os
import json
import sqlite3
import tempfile
import shutil
import hashlib
import unittest
from datetime import datetime

from brain.briefing.collector import BriefingCollector, is_default_db_path
from brain.briefing.recommendations import (
    RecommendationEvaluator,
    RecommendationStore,
    PROTECTED_BASELINE_REC_IDS,
)
from brain.jobs.store import JobStore
from brain.memory import get_db_path

EXPECTED_BASELINE_HASH = "8ecff69a4cf9bdee074352ba3941f7760d69394403ed9a671e312c28e38cb2fb"
EXPECTED_BASELINE_COUNT = 85


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestStage37E5ARIsolation(unittest.TestCase):
    """Test suite proving explicit storage isolation for BriefingCollector and RecommendationEvaluator."""

    @classmethod
    def setUpClass(cls):
        prod_rec_path = os.path.join(r"D:\Orange AI\Orange AI Desktop", "recommendations.json")
        cls.prod_rec_path = prod_rec_path
        if not os.path.exists(prod_rec_path):
            raise FileNotFoundError(f"Production recommendations file missing: {prod_rec_path}")
        initial_hash = _file_sha256(prod_rec_path)
        with open(prod_rec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        initial_count = len(data.get("recommendations", {}))
        assert initial_hash == EXPECTED_BASELINE_HASH, f"Pre-test hash mismatch: {initial_hash} != {EXPECTED_BASELINE_HASH}"
        assert initial_count == EXPECTED_BASELINE_COUNT, f"Pre-test count mismatch: {initial_count} != {EXPECTED_BASELINE_COUNT}"

    @classmethod
    def tearDownClass(cls):
        final_hash = _file_sha256(cls.prod_rec_path)
        with open(cls.prod_rec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        final_count = len(data.get("recommendations", {}))
        assert final_hash == EXPECTED_BASELINE_HASH, f"Post-test hash mismatch! File was modified: {final_hash}"
        assert final_count == EXPECTED_BASELINE_COUNT, f"Post-test count mismatch! File was modified: {final_count}"

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="orange_test_isolation_")
        self.tmp_db = os.path.join(self.tmp_dir, "test_memory.db")
        self.tmp_rec_file = os.path.join(self.tmp_dir, "test_recs.json")

        # Initialize minimal SQLite schema for custom DB
        conn = sqlite3.connect(self.tmp_db)
        conn.execute("CREATE TABLE structured_memories (id INTEGER PRIMARY KEY, category TEXT, memory_key TEXT, value TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, message TEXT, due_at TEXT, created_at TEXT, fired INTEGER DEFAULT 0)")
        conn.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, goal TEXT, status TEXT, priority INTEGER DEFAULT 0,
                attempt_count INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
                step_count INTEGER DEFAULT 0, retry_delay_seconds INTEGER DEFAULT 60,
                next_retry_at TEXT, last_error TEXT, pending_confirmation_json TEXT,
                final_response TEXT, agent_state_json TEXT, created_at TEXT,
                updated_at TEXT, started_at TEXT, completed_at TEXT, source TEXT DEFAULT 'user',
                tags TEXT DEFAULT ''
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_1_default_database_with_default_store_preserves_behavior(self):
        """1. Default database with default store preserves existing production behavior."""
        # Does not raise ValueError
        collector = BriefingCollector()
        self.assertIsNotNone(collector)
        self.assertEqual(os.path.abspath(collector.db_path), os.path.abspath(get_db_path()))
        self.assertIsNone(collector.recommendation_store)

    def test_2_custom_database_with_explicit_store_works(self):
        """2. Custom database with explicit temporary recommendation store works and isolates writes."""
        tmp_store = RecommendationStore(file_path=self.tmp_rec_file)
        collector = BriefingCollector(
            db_path=self.tmp_db,
            recommendation_store=tmp_store,
        )
        self.assertEqual(collector.db_path, self.tmp_db)
        self.assertIs(collector.recommendation_store, tmp_store)

        # Collect should execute and persist only into tmp_store
        ctx = collector.collect(now=datetime(2026, 10, 31, 12, 0, 0))
        self.assertIsNotNone(ctx)

        # Production file must remain untouched
        self.assertEqual(_file_sha256(self.prod_rec_path), EXPECTED_BASELINE_HASH)

    def test_3_custom_database_without_recommendation_store_fails_explicitly(self):
        """3. Custom database without a recommendation store fails immediately with ValueError."""
        # Direct custom db_path
        with self.assertRaises(ValueError) as cm:
            BriefingCollector(db_path=self.tmp_db)
        self.assertIn("non-default database but no explicit recommendation_store was provided", str(cm.exception))

        # Custom db_path via custom JobStore
        custom_job_store = JobStore(db_path=self.tmp_db)
        with self.assertRaises(ValueError) as cm2:
            BriefingCollector(job_store=custom_job_store)
        self.assertIn("non-default database but no explicit recommendation_store was provided", str(cm2.exception))

    def test_4_custom_database_cannot_mutate_production_recommendations(self):
        """4. Custom database cannot mutate production recommendations.json under any circumstances."""
        pre_hash = _file_sha256(self.prod_rec_path)
        with self.assertRaises(ValueError):
            BriefingCollector(db_path=self.tmp_db)

        post_hash = _file_sha256(self.prod_rec_path)
        self.assertEqual(pre_hash, post_hash)
        self.assertEqual(post_hash, EXPECTED_BASELINE_HASH)

    def test_5_exception_during_initialization_prevents_production_fallback(self):
        """5. An exception during collector initialization prevents instance creation and production fallback."""
        collector = None
        try:
            collector = BriefingCollector(db_path=self.tmp_db)
        except ValueError:
            pass
        self.assertIsNone(collector)
        self.assertEqual(_file_sha256(self.prod_rec_path), EXPECTED_BASELINE_HASH)

    def test_6_recommendation_evaluator_respects_explicit_store_injection(self):
        """6. RecommendationEvaluator respects explicit store injection and rejects custom DB without store."""
        tmp_store = RecommendationStore(file_path=self.tmp_rec_file)

        # Custom DB with explicit store succeeds
        evaluator = RecommendationEvaluator(db_path=self.tmp_db, store=tmp_store)
        self.assertIs(evaluator.store, tmp_store)

        # Custom DB without store raises ValueError
        with self.assertRaises(ValueError) as cm:
            RecommendationEvaluator(db_path=self.tmp_db, store=None)
        self.assertIn("non-default database but no explicit store was provided", str(cm.exception))

        # Custom job_store without store raises ValueError
        custom_job_store = JobStore(db_path=self.tmp_db)
        with self.assertRaises(ValueError) as cm2:
            RecommendationEvaluator(job_store=custom_job_store, store=None)
        self.assertIn("non-default database but no explicit store was provided", str(cm2.exception))

    def test_7_is_default_db_path_normalizes_correctly(self):
        """7. is_default_db_path correctly identifies default vs custom databases across path variants."""
        prod_db = get_db_path()
        self.assertTrue(is_default_db_path(None))
        self.assertTrue(is_default_db_path(prod_db))
        self.assertTrue(is_default_db_path(prod_db.lower()))
        self.assertTrue(is_default_db_path(prod_db.replace("\\", "/")))

        self.assertFalse(is_default_db_path(self.tmp_db))
        self.assertFalse(is_default_db_path(":memory:"))
        self.assertFalse(is_default_db_path("custom_memory.db"))

    def test_8_existing_production_recommendation_records_not_modified(self):
        """8. Existing production recommendation records are completely preserved."""
        with open(self.prod_rec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        recs = data.get("recommendations", {})
        self.assertEqual(len(recs), EXPECTED_BASELINE_COUNT)
        for baseline_id in PROTECTED_BASELINE_REC_IDS:
            self.assertIn(baseline_id, recs)
            self.assertIn(recs[baseline_id]["state"], ["ACTIVE", "DISMISSED", "DEFERRED", "HANDLED", "STALE"])

    def test_9_edge_case_a_default_db_custom_job_store_without_store(self):
        """9 (Test A): Default db_path with custom job_store.db_path raises ValueError without explicit store."""
        prod_db = get_db_path()
        custom_js = JobStore(db_path=self.tmp_db)

        # BriefingCollector
        with self.assertRaises(ValueError) as cm1:
            BriefingCollector(db_path=prod_db, job_store=custom_js, recommendation_store=None)
        self.assertIn("non-default database but no explicit recommendation_store was provided", str(cm1.exception))

        # RecommendationEvaluator
        with self.assertRaises(ValueError) as cm2:
            RecommendationEvaluator(db_path=prod_db, job_store=custom_js, store=None)
        self.assertIn("non-default database but no explicit store was provided", str(cm2.exception))

    def test_10_edge_case_b_custom_db_default_job_store_without_store(self):
        """10 (Test B): Custom db_path with default job_store.db_path raises ValueError without explicit store."""
        default_js = JobStore(db_path=get_db_path())

        # BriefingCollector
        with self.assertRaises(ValueError) as cm1:
            BriefingCollector(db_path=self.tmp_db, job_store=default_js, recommendation_store=None)
        self.assertIn("non-default database but no explicit recommendation_store was provided", str(cm1.exception))

        # RecommendationEvaluator
        with self.assertRaises(ValueError) as cm2:
            RecommendationEvaluator(db_path=self.tmp_db, job_store=default_js, store=None)
        self.assertIn("non-default database but no explicit store was provided", str(cm2.exception))

    def test_11_edge_case_c_default_db_default_job_store_without_store(self):
        """11 (Test C): Default db_path and default job_store.db_path preserve standard functional behavior."""
        prod_db = get_db_path()
        default_js = JobStore(db_path=prod_db)

        # BriefingCollector
        collector = BriefingCollector(db_path=prod_db, job_store=default_js, recommendation_store=None)
        self.assertIsNotNone(collector)
        self.assertEqual(os.path.abspath(collector.db_path), os.path.abspath(prod_db))
        self.assertIsNone(collector.recommendation_store)

        # RecommendationEvaluator
        evaluator = RecommendationEvaluator(db_path=prod_db, job_store=default_js, store=None)
        self.assertIsNotNone(evaluator)
        self.assertIsInstance(evaluator.store, RecommendationStore)

    def test_12_edge_case_d_custom_db_custom_job_store_with_explicit_store(self):
        """12 (Test D): Custom db_path and custom job_store with explicit store succeed without writing to prod."""
        tmp_db2 = os.path.join(self.tmp_dir, "test_job_store.db")
        conn = sqlite3.connect(tmp_db2)
        conn.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, goal TEXT, status TEXT, priority INTEGER DEFAULT 0,
                attempt_count INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
                step_count INTEGER DEFAULT 0, retry_delay_seconds INTEGER DEFAULT 60,
                next_retry_at TEXT, last_error TEXT, pending_confirmation_json TEXT,
                final_response TEXT, agent_state_json TEXT, created_at TEXT,
                updated_at TEXT, started_at TEXT, completed_at TEXT, source TEXT DEFAULT 'user',
                tags TEXT DEFAULT ''
            )
        """)
        conn.commit()
        conn.close()

        custom_js = JobStore(db_path=tmp_db2)
        tmp_store = RecommendationStore(file_path=self.tmp_rec_file)

        # BriefingCollector
        collector = BriefingCollector(
            db_path=self.tmp_db,
            job_store=custom_js,
            recommendation_store=tmp_store,
        )
        self.assertEqual(collector.db_path, self.tmp_db)
        self.assertIs(collector.recommendation_store, tmp_store)
        ctx = collector.collect(now=datetime(2026, 10, 31, 12, 0, 0))
        self.assertIsNotNone(ctx)

        # RecommendationEvaluator
        evaluator = RecommendationEvaluator(
            db_path=self.tmp_db,
            job_store=custom_js,
            store=tmp_store,
        )
        self.assertIs(evaluator.store, tmp_store)

        # Verify production storage was untouched
        self.assertEqual(_file_sha256(self.prod_rec_path), EXPECTED_BASELINE_HASH)


if __name__ == "__main__":
    unittest.main()
