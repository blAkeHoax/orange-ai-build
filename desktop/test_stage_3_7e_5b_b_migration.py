"""
test_stage_3_7e_5b_b_migration.py - Acceptance and regression tests for C-01:
Safe migration of recommendations and event watermarks in brain/paths.py.

Verifies all 12 required scenarios:
1. Both JSON files migrate when destinations are absent and sources are valid.
2. Existing destination recommendations remain byte-for-byte unchanged.
3. Existing destination watermarks remain byte-for-byte unchanged.
4. Different source and destination files produce a conflict without overwriting either.
5. Malformed recommendations JSON is rejected.
6. Unsupported or malformed watermark JSON is rejected.
7. SHA-256 verification failure prevents promotion.
8. A destination created during migration is never overwritten.
9. A failed migration does not mark the entire migration complete.
10. A subsequent safe retry can complete the migration.
11. Source files remain unchanged.
12. Migration remains idempotent.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
import hashlib
from unittest.mock import patch

desktop_dir = os.path.dirname(os.path.abspath(__file__))
if desktop_dir not in sys.path:
    sys.path.insert(0, desktop_dir)

import brain.paths as paths_mod
from brain.paths import (
    migrate_legacy_data_if_needed,
    calculate_file_sha256,
    validate_recommendations_json,
    validate_event_watermarks_json,
)


def make_valid_recommendations_data():
    return {
        "version": 1,
        "recommendations": {
            "rec_test_1": {
                "rec_id": "rec_test_1",
                "category": "job_confirmation",
                "entity_id": "job_123",
                "recommendation_type": "approval_pending",
                "title": "Action requires confirmation",
                "explanation": "Test explanation",
                "suggested_action": "job confirm job_123 yes",
                "priority": "high",
                "evidence_fingerprint": "fp1",
                "state": "active",
                "created_at": "2026-10-31T12:00:00Z",
                "updated_at": "2026-10-31T12:00:00Z",
            }
        },
    }


def make_valid_watermarks_data():
    return {
        "version": 1,
        "created_at": "2026-10-31T12:00:00Z",
        "updated_at": "2026-10-31T12:00:00Z",
        "is_first_run_baseline": False,
        "watermarks": {
            "jobs_completed_at": None,
            "job_step_max_id": 5,
            "memories_updated_at": "2026-10-31T12:00:00Z",
            "last_startup_reconciliation": "2026-10-31T12:00:00Z",
        },
        "delivered_events": {
            "evt_1": {
                "event_id": "evt_1",
                "delivered_at": "2026-10-31T12:00:01Z",
                "event_type": "reminder_due",
                "source": "reminders",
            }
        },
    }


class TestStage37E5BBMigration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.source_dir = os.path.join(self.test_dir, "legacy_source")
        self.target_dir = os.path.join(self.test_dir, "localappdata_target")
        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.target_dir, exist_ok=True)

        # Reset module state
        paths_mod._MIGRATION_PERFORMED = False

    def tearDown(self):
        paths_mod._MIGRATION_PERFORMED = False
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _write_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def test_01_both_json_files_migrate_when_dest_absent(self):
        """1. Both JSON files migrate when destinations are absent and sources are valid."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        src_wm = os.path.join(self.source_dir, "event_watermarks.json")
        self._write_json(src_recs, make_valid_recommendations_data())
        self._write_json(src_wm, make_valid_watermarks_data())

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertEqual(report["status"], "success")
        self.assertIn("recommendations.json", report["migrated"])
        self.assertIn("event_watermarks.json", report["migrated"])

        dest_recs = os.path.join(self.target_dir, "recommendations.json")
        dest_wm = os.path.join(self.target_dir, "event_watermarks.json")

        self.assertTrue(os.path.isfile(dest_recs))
        self.assertTrue(os.path.isfile(dest_wm))

        self.assertEqual(calculate_file_sha256(src_recs), calculate_file_sha256(dest_recs))
        self.assertEqual(calculate_file_sha256(src_wm), calculate_file_sha256(dest_wm))

    def test_02_existing_destination_recommendations_unchanged(self):
        """2. Existing destination recommendations remain byte-for-byte unchanged."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        dest_recs = os.path.join(self.target_dir, "recommendations.json")

        src_data = make_valid_recommendations_data()
        src_data["recommendations"]["rec_from_source"] = {"rec_id": "rec_from_source", "category": "c", "title": "t"}
        self._write_json(src_recs, src_data)

        target_initial = make_valid_recommendations_data()
        target_initial["recommendations"]["rec_target_existing"] = {"rec_id": "rec_target_existing", "category": "c", "title": "t"}
        self._write_json(dest_recs, target_initial)
        initial_hash = calculate_file_sha256(dest_recs)

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertEqual(report["status"], "success")
        self.assertIn("recommendations.json", report["skipped"])
        final_hash = calculate_file_sha256(dest_recs)
        self.assertEqual(initial_hash, final_hash, "Target recommendations must remain byte-for-byte identical")

    def test_03_existing_destination_watermarks_unchanged(self):
        """3. Existing destination watermarks remain byte-for-byte unchanged."""
        src_wm = os.path.join(self.source_dir, "event_watermarks.json")
        dest_wm = os.path.join(self.target_dir, "event_watermarks.json")

        self._write_json(src_wm, make_valid_watermarks_data())

        target_wm = make_valid_watermarks_data()
        target_wm["watermarks"]["job_step_max_id"] = 9999
        self._write_json(dest_wm, target_wm)
        initial_hash = calculate_file_sha256(dest_wm)

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertEqual(report["status"], "success")
        self.assertIn("event_watermarks.json", report["skipped"])
        final_hash = calculate_file_sha256(dest_wm)
        self.assertEqual(initial_hash, final_hash, "Target watermarks must remain byte-for-byte identical")

    def test_04_different_source_and_destination_produce_conflict(self):
        """4. Different source and destination files produce a conflict without overwriting either."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        dest_recs = os.path.join(self.target_dir, "recommendations.json")

        self._write_json(src_recs, {"version": 1, "recommendations": {"r1": {"rec_id": "r1", "category": "a", "title": "A"}}})
        self._write_json(dest_recs, {"version": 1, "recommendations": {"r2": {"rec_id": "r2", "category": "b", "title": "B"}}})
        dest_hash_before = calculate_file_sha256(dest_recs)

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertIn("recommendations.json", report["skipped"])
        self.assertTrue(any(c["file"] == "recommendations.json" for c in report["conflicts"]))
        self.assertEqual(dest_hash_before, calculate_file_sha256(dest_recs))

    def test_05_malformed_recommendations_json_rejected(self):
        """5. Malformed recommendations JSON is rejected."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        with open(src_recs, "w", encoding="utf-8") as f:
            f.write("{ invalid json")

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertEqual(report["status"], "partial_error")
        self.assertTrue(any(e["file"] == "recommendations.json" for e in report["errors"]))
        dest_recs = os.path.join(self.target_dir, "recommendations.json")
        self.assertFalse(os.path.exists(dest_recs))

    def test_06_unsupported_or_malformed_watermarks_rejected(self):
        """6. Unsupported or malformed watermark JSON is rejected."""
        src_wm = os.path.join(self.source_dir, "event_watermarks.json")
        bad_wm = make_valid_watermarks_data()
        bad_wm["version"] = 999  # Unsupported version
        self._write_json(src_wm, bad_wm)

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertEqual(report["status"], "partial_error")
        self.assertTrue(any(e["file"] == "event_watermarks.json" for e in report["errors"]))
        dest_wm = os.path.join(self.target_dir, "event_watermarks.json")
        self.assertFalse(os.path.exists(dest_wm))

    def test_07_sha256_verification_failure_prevents_promotion(self):
        """7. SHA-256 verification failure prevents promotion."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        self._write_json(src_recs, make_valid_recommendations_data())

        original_calc = paths_mod.calculate_file_sha256
        call_count = [0]

        def corrupted_calc(p):
            call_count[0] += 1
            if call_count[0] == 2:  # Staged file check
                return "corrupted_hash"
            return original_calc(p)

        with patch("brain.paths.calculate_file_sha256", side_effect=corrupted_calc):
            report = migrate_legacy_data_if_needed(
                legacy_source_dir=self.source_dir,
                target_dir=self.target_dir,
                force=True,
            )

        self.assertEqual(report["status"], "partial_error")
        self.assertTrue(any("SHA-256 mismatch" in e.get("error", "") for e in report["errors"]))
        dest_recs = os.path.join(self.target_dir, "recommendations.json")
        self.assertFalse(os.path.exists(dest_recs))

    def test_08_destination_created_during_migration_never_overwritten(self):
        """8. A destination created during migration is never overwritten."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        self._write_json(src_recs, make_valid_recommendations_data())

        dest_recs = os.path.join(self.target_dir, "recommendations.json")

        original_calc = paths_mod.calculate_file_sha256
        def race_injector(p):
            # When staged hash is checked, create destination before promotion
            if ".migration_tmp_" in p:
                with open(dest_recs, "w", encoding="utf-8") as f:
                    f.write("{\"race_winner\": true}")
            return original_calc(p)

        with patch("brain.paths.calculate_file_sha256", side_effect=race_injector):
            report = migrate_legacy_data_if_needed(
                legacy_source_dir=self.source_dir,
                target_dir=self.target_dir,
                force=True,
            )

        self.assertIn("recommendations.json", report["skipped"])
        self.assertTrue(os.path.exists(dest_recs))
        with open(dest_recs, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("race_winner", content, "Destination created by race must be preserved")

    def test_09_failed_migration_does_not_mark_complete(self):
        """9. A failed migration does not mark the entire migration complete."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        with open(src_recs, "w", encoding="utf-8") as f:
            f.write("{ invalid json")

        report = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=False,
        )

        self.assertEqual(report["status"], "partial_error")
        self.assertFalse(paths_mod._MIGRATION_PERFORMED, "_MIGRATION_PERFORMED must remain False on failure")

    def test_10_subsequent_safe_retry_can_complete_migration(self):
        """10. A subsequent safe retry can complete the migration."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        # Step 1: Start with invalid file
        with open(src_recs, "w", encoding="utf-8") as f:
            f.write("corrupt")

        report1 = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=False,
        )
        self.assertEqual(report1["status"], "partial_error")

        # Step 2: Correct file
        self._write_json(src_recs, make_valid_recommendations_data())

        # Step 3: Run retry without force=True
        report2 = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=False,
        )
        self.assertEqual(report2["status"], "success")
        self.assertIn("recommendations.json", report2["migrated"])
        self.assertTrue(paths_mod._MIGRATION_PERFORMED)

    def test_11_source_files_remain_unchanged(self):
        """11. Source files remain unchanged."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        src_wm = os.path.join(self.source_dir, "event_watermarks.json")
        self._write_json(src_recs, make_valid_recommendations_data())
        self._write_json(src_wm, make_valid_watermarks_data())

        recs_sha_pre = calculate_file_sha256(src_recs)
        wm_sha_pre = calculate_file_sha256(src_wm)

        migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )

        self.assertEqual(recs_sha_pre, calculate_file_sha256(src_recs), "Source recs must remain unchanged")
        self.assertEqual(wm_sha_pre, calculate_file_sha256(src_wm), "Source watermarks must remain unchanged")

    def test_12_migration_remains_idempotent(self):
        """12. Migration remains idempotent."""
        src_recs = os.path.join(self.source_dir, "recommendations.json")
        src_wm = os.path.join(self.source_dir, "event_watermarks.json")
        self._write_json(src_recs, make_valid_recommendations_data())
        self._write_json(src_wm, make_valid_watermarks_data())

        # First run
        report1 = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )
        self.assertEqual(report1["status"], "success")
        self.assertEqual(len(report1["migrated"]), 2)

        # Second run
        report2 = migrate_legacy_data_if_needed(
            legacy_source_dir=self.source_dir,
            target_dir=self.target_dir,
            force=True,
        )
        self.assertEqual(report2["status"], "success")
        self.assertEqual(len(report2["migrated"]), 0)
        self.assertIn("recommendations.json", report2["skipped"])
        self.assertIn("event_watermarks.json", report2["skipped"])


if __name__ == "__main__":
    unittest.main()
