"""
test_stage5e_skills.py - Unit Test Suite for Stage 5E Reusable Orange Skills.

Validates:
1. Built-in skill seeding & storage isolation:
   - Idempotently creates skills.json in isolated directory without touching %LOCALAPPDATA%.
   - Seeds repo-health-audit, system-diagnostics, and project-handoff-summary with builtin: True.
2. Custom skill creation & validation:
   - Validates slug format (alphanumeric, underscores, hyphens).
   - Prevents overwriting built-in skills.
3. Toggling & deletion protection:
   - Toggles enabled status cleanly.
   - Allows deleting custom skills.
   - Blocks deleting built-in skills with ValueError.
4. Guardrail & trust-mode enforcement in execute_skill:
   - SAFE skills execute without confirmation in supervised mode.
   - State-altering skills require confirmation in supervised mode (obeying prompt_fn y/n).
   - State-altering skills run without confirmation in unrestricted mode.
   - Catastrophic commands (format C:, System32 destruction) are BLOCKED unconditionally in all modes.
5. Registry integration:
   - run_skill is registered in ToolRegistry (tool count: 30) and executable via registry.execute.
"""

import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

DESKTOP_DIR = os.path.abspath(os.path.dirname(__file__))
if DESKTOP_DIR not in sys.path:
    sys.path.insert(0, DESKTOP_DIR)

from tools.skills import (
    get_skills_path,
    list_skills,
    create_skill,
    toggle_skill,
    delete_skill,
    execute_skill,
    run_skill,
    compute_skill_risk,
)
from tools.registry import registry
from tools.permissions import set_full_access_mode, RiskLevel


class TestStage5ESkills(unittest.TestCase):
    def setUp(self):
        # Isolate storage for every test so %LOCALAPPDATA% is NEVER touched
        self.temp_dir = tempfile.mkdtemp(prefix="orange_test_skills_")
        self.orig_env = os.environ.get("ORANGE_AI_USER_DATA_DIR")
        os.environ["ORANGE_AI_USER_DATA_DIR"] = self.temp_dir
        set_full_access_mode("supervised")

    def tearDown(self):
        if self.orig_env is not None:
            os.environ["ORANGE_AI_USER_DATA_DIR"] = self.orig_env
        else:
            os.environ.pop("ORANGE_AI_USER_DATA_DIR", None)
        set_full_access_mode("supervised")
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_builtin_skills_idempotent_seeding(self):
        """Verify built-in starter skills are seeded into an isolated skills.json file."""
        skills_file = get_skills_path(self.temp_dir)
        self.assertFalse(skills_file.exists())

        skills = list_skills(user_data_dir=self.temp_dir)
        self.assertTrue(skills_file.exists())
        self.assertEqual(len(skills), 3)

        slugs = {s["slug"] for s in skills}
        expected_slugs = {"repo-health-audit", "system-diagnostics", "project-handoff-summary"}
        self.assertEqual(slugs, expected_slugs)

        for s in skills:
            self.assertTrue(s["builtin"])
            self.assertTrue(s["enabled"])
            self.assertIn("steps", s)
            self.assertIn("prompt_template", s)
            self.assertIn(s["risk_level"], ["SAFE", "STATE_ALTERING"])

        # system-diagnostics contains only SAFE tools
        sys_diag = next(s for s in skills if s["slug"] == "system-diagnostics")
        self.assertEqual(sys_diag["risk_level"], "SAFE")
        self.assertEqual(sys_diag["step_count"], 4)

        # repo-health-audit contains run_command (state-altering/confirmation-required)
        repo_audit = next(s for s in skills if s["slug"] == "repo-health-audit")
        self.assertEqual(repo_audit["risk_level"], "STATE_ALTERING")

    def test_02_create_custom_skill_and_validation(self):
        """Verify creating custom skills, slug validation, and built-in protection."""
        # Invalid slug formats
        with self.assertRaises(ValueError):
            create_skill("invalid slug!", "Name", "Desc", [{"tool": "get_time", "args": {}}], user_data_dir=self.temp_dir)
        with self.assertRaises(ValueError):
            create_skill("", "Name", "Desc", [{"tool": "get_time", "args": {}}], user_data_dir=self.temp_dir)
        with self.assertRaises(ValueError):
            create_skill("has/slashes", "Name", "Desc", [{"tool": "get_time", "args": {}}], user_data_dir=self.temp_dir)

        # Valid custom skill
        custom = create_skill(
            slug="quick-time-check",
            name="Quick Time Check",
            description="Checks current time and date.",
            steps=[
                {"tool": "get_time", "args": {}},
                {"tool": "get_date", "args": {}},
            ],
            prompt_template="Current time and date report: {{input}}",
            user_data_dir=self.temp_dir,
        )
        self.assertEqual(custom["slug"], "quick-time-check")
        self.assertFalse(custom["builtin"])
        self.assertTrue(custom["enabled"])
        self.assertEqual(custom["risk_level"], "SAFE")
        self.assertEqual(custom["step_count"], 2)

        # Cannot overwrite a built-in skill
        with self.assertRaises(ValueError) as ctx:
            create_skill(
                slug="repo-health-audit",
                name="Hijacked Skill",
                description="Hacked",
                steps=[{"tool": "get_time", "args": {}}],
                user_data_dir=self.temp_dir,
            )
        self.assertIn("Cannot overwrite built-in skill", str(ctx.exception))

    def test_03_toggle_and_delete_skills(self):
        """Verify toggling skills on/off and delete protection for built-in skills."""
        # Toggle built-in skill off then on
        updated = toggle_skill("system-diagnostics", enabled=False, user_data_dir=self.temp_dir)
        self.assertFalse(updated["enabled"])

        updated2 = toggle_skill("system-diagnostics", user_data_dir=self.temp_dir)
        self.assertTrue(updated2["enabled"])

        # Create custom skill and delete it
        create_skill(
            slug="temp-skill",
            name="Temp",
            description="Temporary",
            steps=[{"tool": "get_time", "args": {}}],
            user_data_dir=self.temp_dir,
        )
        self.assertTrue(delete_skill("temp-skill", user_data_dir=self.temp_dir))
        all_slugs = {s["slug"] for s in list_skills(user_data_dir=self.temp_dir)}
        self.assertNotIn("temp-skill", all_slugs)

        # Attempt to delete built-in skill must fail
        with self.assertRaises(ValueError) as ctx:
            delete_skill("repo-health-audit", user_data_dir=self.temp_dir)
        self.assertIn("Cannot delete built-in skill", str(ctx.exception))

    def test_04_execute_safe_skill_supervised(self):
        """Verify SAFE skills execute completely without confirmation prompts in supervised mode."""
        prompt_mock = MagicMock()
        res = execute_skill(
            "system-diagnostics",
            input_text="Routine morning check",
            prompt_fn=prompt_mock,
            full_access_mode="supervised",
            user_data_dir=self.temp_dir,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "completed")
        self.assertEqual(len(res["step_results"]), 4)
        self.assertIn("system resource metrics: Routine morning check", res["output"])
        prompt_mock.assert_not_called()

    def test_05_execute_skill_supervised_confirmation_prompt(self):
        """Verify state-altering skills prompt in supervised mode and obey user y/n."""
        # Mock prompt returning 'n' (user declines)
        res_cancelled = execute_skill(
            "repo-health-audit",
            input_text="Workspace audit",
            prompt_fn=lambda _: "n",
            full_access_mode="supervised",
            user_data_dir=self.temp_dir,
        )
        self.assertFalse(res_cancelled["success"])
        self.assertEqual(res_cancelled["status"], "cancelled")
        self.assertIn("Execution cancelled", res_cancelled["output"])

        # Mock prompt returning 'y' (user confirms)
        with patch("tools.command.run_command", return_value="M test.txt\n"):
            res_confirmed = execute_skill(
                "repo-health-audit",
                input_text="Workspace audit",
                prompt_fn=lambda _: "y",
                full_access_mode="supervised",
                user_data_dir=self.temp_dir,
            )
            self.assertTrue(res_confirmed["success"])
            self.assertEqual(res_confirmed["status"], "completed")
            self.assertIn("run_command", res_confirmed["output"])

    def test_06_execute_skill_unrestricted_mode(self):
        """Verify state-altering skills execute without prompts in Unrestricted Autonomous Mode."""
        prompt_mock = MagicMock()
        with patch("tools.command.run_command", return_value="?? newfile.py\n"):
            res = execute_skill(
                "repo-health-audit",
                input_text="Automated scan",
                prompt_fn=prompt_mock,
                full_access_mode="unrestricted",
                user_data_dir=self.temp_dir,
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["status"], "completed")
            prompt_mock.assert_not_called()

    def test_07_execute_skill_catastrophic_blocked(self):
        """Verify catastrophic commands inside skill steps are blocked unconditionally in both modes."""
        custom_dangerous = create_skill(
            slug="wipe-drive",
            name="Wipe Drive",
            description="Dangerous",
            steps=[{"tool": "run_powershell", "args": {"command": "format C: /fs:NTFS"}}],
            user_data_dir=self.temp_dir,
        )

        # Supervised mode
        res_sup = execute_skill(
            custom_dangerous,
            full_access_mode="supervised",
            user_data_dir=self.temp_dir,
        )
        self.assertFalse(res_sup["success"])
        self.assertEqual(res_sup["status"], "blocked")
        self.assertIn("safety guardrail", res_sup["output"])

        # Unrestricted mode
        res_unr = execute_skill(
            custom_dangerous,
            full_access_mode="unrestricted",
            user_data_dir=self.temp_dir,
        )
        self.assertFalse(res_unr["success"])
        self.assertEqual(res_unr["status"], "blocked")
        self.assertIn("safety guardrail", res_unr["output"])

    def test_08_execute_disabled_or_missing_skill(self):
        """Verify attempting to run a disabled or missing skill returns structured status."""
        # Missing skill
        res_missing = execute_skill("non-existent-skill", user_data_dir=self.temp_dir)
        self.assertFalse(res_missing["success"])
        self.assertEqual(res_missing["status"], "not_found")

        # Disabled skill
        toggle_skill("system-diagnostics", enabled=False, user_data_dir=self.temp_dir)
        res_disabled = execute_skill("system-diagnostics", user_data_dir=self.temp_dir)
        self.assertFalse(res_disabled["success"])
        self.assertEqual(res_disabled["status"], "disabled")

    def test_09_run_skill_tool_in_registry(self):
        """Verify run_skill is registered in ToolRegistry and works through registry.execute."""
        tool = registry.get("run_skill")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.category, "skills")
        self.assertEqual(len(registry.list_tools()), 30)

        # Execute through registry
        output = registry.execute(
            "run_skill",
            slug="system-diagnostics",
            input="Registry invocation",
            full_access_mode="supervised",
            user_data_dir=self.temp_dir,
        )
        self.assertIn("system resource metrics", output)
        self.assertIn("get_cpu_usage", output)


if __name__ == "__main__":
    unittest.main()
