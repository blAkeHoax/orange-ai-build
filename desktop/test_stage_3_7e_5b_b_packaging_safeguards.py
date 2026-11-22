"""
test_stage_3_7e_5b_b_packaging_safeguards.py - Regression & validation tests for:
- H-01: PyInstaller dateparser packaging configuration in Orange AI.spec
- H-02: Safe promotion gating and verified backup mechanics in rebuild_exe.cmd
"""

import os
import sys
import unittest
import tempfile
import shutil
import hashlib

desktop_dir = os.path.dirname(os.path.abspath(__file__))
if desktop_dir not in sys.path:
    sys.path.insert(0, desktop_dir)


class TestPackagingSafeguards(unittest.TestCase):
    def test_h01_spec_dateparser_configuration(self):
        """H-01: Verify Orange AI.spec properly collects dateparser resources and submodules."""
        spec_path = os.path.join(desktop_dir, "Orange AI.spec")
        self.assertTrue(os.path.isfile(spec_path), "Orange AI.spec must exist")

        with open(spec_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check collection call
        self.assertIn("collect_all('dateparser')", content)

        # Check integration into Analysis inputs
        self.assertIn("dp_datas", content)
        self.assertIn("dp_hiddenimports", content)
        self.assertIn("'dateparser'", content)

        # Statically verify collect_all('dateparser') output
        from PyInstaller.utils.hooks import collect_all
        dp_datas, dp_binaries, dp_hidden = collect_all("dateparser")
        self.assertGreater(len(dp_datas), 200, "Dateparser must include >200 translation and locale data files")
        self.assertGreater(len(dp_hidden), 200, "Dateparser must include >200 hidden submodules")

    def test_h02_rebuild_cmd_control_flow(self):
        """H-02: Statically verify rebuild_exe.cmd separation of build and promotion."""
        cmd_path = os.path.join(desktop_dir, "rebuild_exe.cmd")
        self.assertTrue(os.path.isfile(cmd_path), "rebuild_exe.cmd must exist")

        with open(cmd_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify separation of labels
        self.assertIn(":BUILD_STAGE", content)
        self.assertIn(":PROMOTE_STAGE", content)

        # Verify argument check for --promote
        self.assertIn('if "%1"=="--promote" goto PROMOTE_STAGE', content)

        # Verify BUILD_STAGE exits without promoting
        build_section = content[content.find(":BUILD_STAGE"):content.find(":PROMOTE_STAGE")]
        self.assertNotIn("copy /y \"dist\\Orange AI.exe\" \"..\\installer\\Orange AI.exe\"", build_section)
        self.assertIn("exit /b 0", build_section)

        # Verify PROMOTE_STAGE contains backup and verification steps
        promote_section = content[content.find(":PROMOTE_STAGE"):]
        self.assertIn("BACKUP_EXE", promote_section)
        self.assertIn(".bak_!", promote_section)
        self.assertIn("ORIG_HASH", promote_section)
        self.assertIn("BAK_HASH", promote_section)
        self.assertIn("STAGED_HASH", promote_section)
        self.assertIn("FINAL_HASH", promote_section)

    def test_h02_promotion_logic_sandbox_simulation(self):
        """H-02: Test promotion algorithm safely in a temporary sandbox directory."""
        tmp_dir = tempfile.mkdtemp()
        try:
            staged_dir = os.path.join(tmp_dir, "dist")
            installer_dir = os.path.join(tmp_dir, "installer")
            os.makedirs(staged_dir, exist_ok=True)
            os.makedirs(installer_dir, exist_ok=True)

            staged_exe = os.path.join(staged_dir, "Orange AI.exe")
            installer_exe = os.path.join(installer_dir, "Orange AI.exe")

            # 1. Existing installer with original content
            orig_bytes = b"ORIGINAL_VERIFIED_INSTALLER_BINARY_V1"
            with open(installer_exe, "wb") as f:
                f.write(orig_bytes)
            orig_hash = hashlib.sha256(orig_bytes).hexdigest()

            # 2. Staged build with new content
            new_bytes = b"NEW_STAGED_BUILD_BINARY_V2"
            with open(staged_exe, "wb") as f:
                f.write(new_bytes)
            new_hash = hashlib.sha256(new_bytes).hexdigest()

            # Simulate the safe promotion procedure:
            # Step A: Validate staged file exists and is non-empty
            self.assertTrue(os.path.isfile(staged_exe))
            self.assertGreater(os.path.getsize(staged_exe), 0)

            # Step B: Backup existing installer
            backup_exe = os.path.join(installer_dir, "Orange AI.exe.bak_test")
            shutil.copy2(installer_exe, backup_exe)
            self.assertTrue(os.path.isfile(backup_exe))

            # Step C: Verify backup hash matches original
            with open(backup_exe, "rb") as f:
                bak_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(orig_hash, bak_hash, "Backup must be byte-for-byte identical to original")

            # Step D: Stage promotion copy on destination volume
            promo_tmp = os.path.join(installer_dir, "Orange AI.exe.promo_tmp")
            shutil.copy2(staged_exe, promo_tmp)

            with open(promo_tmp, "rb") as f:
                promo_tmp_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(new_hash, promo_tmp_hash, "Staged promotion file must match new build hash")

            # Step E: Atomic move
            os.replace(promo_tmp, installer_exe)

            # Step F: Verify final promoted binary
            with open(installer_exe, "rb") as f:
                final_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(new_hash, final_hash)

            # Step G: Confirm original backup remains preserved
            with open(backup_exe, "rb") as f:
                self.assertEqual(orig_hash, hashlib.sha256(f.read()).hexdigest())

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
