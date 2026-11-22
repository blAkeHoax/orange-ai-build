"""
test_stage_3_7e_5b_b_2_isolation.py - Regression Test Suite for GUI Watermark Store Isolation.

Stage 3.7E.5B-B.2: Verifies:
1. Each affected GUI test uses a temporary watermark store.
2. The coordinator never resolves to the production watermark path during tests.
3. Deferred 300ms startup reconciliation writes only to the isolated store.
4. GUI shutdown cancels or safely completes pending callbacks.
5. Temporary test stores are cleaned up safely.
6. Running GUI tests leaves the production watermark SHA-256 byte-for-byte unchanged.
"""

import os
import sys
import json
import time
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import customtkinter as ctk

from gui.app import OrangeAIApp
from gui.services.event_coordinator import EventCoordinator
from brain.briefing.event_store import EVENT_WATERMARKS_FILENAME, EventWatermarkStore
from brain.paths import get_project_root

PROD_WATERMARK_PATH = os.path.join(get_project_root(), EVENT_WATERMARKS_FILENAME)


class TestGUIWatermarkIsolation(unittest.TestCase):
    """Test Suite validating watermark store isolation for OrangeAIApp and EventCoordinator."""

    def setUp(self):
        self.prod_sha_before = self._calculate_file_sha256(PROD_WATERMARK_PATH)

    def tearDown(self):
        prod_sha_after = self._calculate_file_sha256(PROD_WATERMARK_PATH)
        self.assertEqual(
            self.prod_sha_before,
            prod_sha_after,
            "CRITICAL: Production watermark store was mutated during test execution!"
        )

    def _calculate_file_sha256(self, filepath: str) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def test_01_isolated_watermark_store_injected_successfully(self):
        """1. Verify that EventCoordinator can be initialized with an isolated watermark store path."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            isolated_wm = os.path.join(tmp_dir, "event_watermarks.json")
            orig_init = EventCoordinator.__init__

            def _mock_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
                store_path = watermark_store_path or isolated_wm
                orig_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)

            with patch.object(EventCoordinator, "__init__", _mock_init), \
                 patch("brain.scheduler.start"):
                app = OrangeAIApp()
                app.withdraw()
                try:
                    self.assertEqual(app._event_coordinator.watermark_store_path, isolated_wm)
                    self.assertNotEqual(app._event_coordinator.watermark_store_path, PROD_WATERMARK_PATH)
                finally:
                    app._on_close()

    def test_02_coordinator_never_resolves_to_production_path_when_isolated(self):
        """2. Verify that coordinator operations never target the production workspace path."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            isolated_wm = os.path.join(tmp_dir, "event_watermarks.json")
            orig_init = EventCoordinator.__init__

            def _mock_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
                store_path = watermark_store_path or isolated_wm
                orig_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)

            with patch.object(EventCoordinator, "__init__", _mock_init), \
                 patch("brain.scheduler.start"):
                app = OrangeAIApp()
                app.withdraw()
                try:
                    # Intentionally execute cycle on coordinator
                    res = app._event_coordinator._execute_cycle()
                    self.assertIsNotNone(res)
                    # The isolated file should be created/updated in tmp_dir
                    self.assertTrue(os.path.exists(isolated_wm))
                    # Production path must remain strictly un-accessed
                    self.assertEqual(self._calculate_file_sha256(PROD_WATERMARK_PATH), self.prod_sha_before)
                finally:
                    app._on_close()

    def test_03_deferred_startup_reconciliation_writes_only_to_isolated_store(self):
        """3. Verify that deferred startup reconciliation writes only to the isolated store."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            isolated_wm = os.path.join(tmp_dir, "event_watermarks.json")
            orig_init = EventCoordinator.__init__

            def _mock_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
                store_path = watermark_store_path or isolated_wm
                orig_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)

            with patch.object(EventCoordinator, "__init__", _mock_init), \
                 patch("brain.scheduler.start"):
                app = OrangeAIApp()
                app.withdraw()
                try:
                    self.assertIsNotNone(app._startup_recon_after_id)
                    # Run execution cycle to complete reconciliation
                    res = app._event_coordinator._execute_cycle()
                    self.assertIsNotNone(res)

                    # Confirm isolated store received the update
                    self.assertTrue(os.path.exists(isolated_wm))
                    with open(isolated_wm, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.assertIn("watermarks", data)

                    # Production store hash must NOT change
                    self.assertEqual(self._calculate_file_sha256(PROD_WATERMARK_PATH), self.prod_sha_before)
                finally:
                    app._on_close()

    def test_04_gui_shutdown_cancels_pending_after_callbacks(self):
        """4. Verify that GUI shutdown safely cancels pending callbacks and cleans up."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            isolated_wm = os.path.join(tmp_dir, "event_watermarks.json")
            orig_init = EventCoordinator.__init__

            def _mock_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
                store_path = watermark_store_path or isolated_wm
                orig_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)

            with patch.object(EventCoordinator, "__init__", _mock_init), \
                 patch("brain.scheduler.start"):
                app = OrangeAIApp()
                app.withdraw()

                self.assertIsNotNone(app._startup_recon_after_id)
                self.assertFalse(app._is_shutting_down)

                # Invoke close before 300ms expires
                app._on_close()

                self.assertTrue(app._is_shutting_down)
                self.assertIsNone(app._startup_recon_after_id)
                self.assertIsNone(app._morning_briefing_after_id)

    def test_05_temporary_test_store_cleaned_up_safely(self):
        """5. Verify that isolated temporary test directories clean up cleanly without hanging locks."""
        tmp_dir = tempfile.TemporaryDirectory()
        isolated_wm = os.path.join(tmp_dir.name, "event_watermarks.json")
        orig_init = EventCoordinator.__init__

        def _mock_init(coord_self, app, bridge=None, db_path=None, watermark_store_path=None, **kwargs):
            store_path = watermark_store_path or isolated_wm
            orig_init(coord_self, app, bridge=bridge, db_path=db_path, watermark_store_path=store_path, **kwargs)

        with patch.object(EventCoordinator, "__init__", _mock_init), \
             patch("brain.scheduler.start"):
            app = OrangeAIApp()
            app.withdraw()
            app._event_coordinator._execute_cycle()
            app._on_close()

        # Temporary directory should be cleanable without Windows file locking errors
        tmp_dir_path = tmp_dir.name
        tmp_dir.cleanup()
        self.assertFalse(os.path.exists(tmp_dir_path))

    def test_06_production_watermark_hash_remains_intact(self):
        """6. Re-verify production watermark store SHA-256 matches authoritative baseline."""
        current_sha = self._calculate_file_sha256(PROD_WATERMARK_PATH)
        expected_baseline = "7f4c5160d17896d5fe22633c22417b8b06baf61c0124210308f6f5a007d63aea"
        self.assertEqual(
            current_sha,
            expected_baseline,
            f"Production watermark hash {current_sha} differs from authoritative baseline {expected_baseline}"
        )


if __name__ == "__main__":
    unittest.main()
