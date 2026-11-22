"""
tools/skills.py - Reusable "Orange Skills" Registry, Persistence & Execution Engine.

Manages built-in starter skills and custom user-defined routines persisted in
<user_data_dir>/skills.json. Enforces the same catastrophic command guard and trust
modes (Supervised Full Access vs Unrestricted Autonomous Mode) as all Orange AI Desktop tools.
"""

from typing import List, Dict, Any, Optional, Callable, Union
from pathlib import Path
import os
import re
import json
import logging
import uuid

from tools.permissions import (
    RiskLevel,
    is_catastrophic_command,
    execute_with_permission,
    get_full_access_mode,
)

logger = logging.getLogger("OrangeAI.Skills")

BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {
        "slug": "repo-health-audit",
        "name": "Repository Health Audit",
        "description": "Inspect git status, recent commits, and test suite readiness across active workspaces.",
        "enabled": True,
        "builtin": True,
        "steps": [
            {"tool": "run_command", "args": {"command": "git status --short"}}
        ],
        "prompt_template": "Summarize repository health, uncommitted files, and recommended next actions for: {{input}}"
    },
    {
        "slug": "system-diagnostics",
        "name": "System Diagnostics",
        "description": "Captures CPU, RAM, disk, battery, and active process health snapshot.",
        "enabled": True,
        "builtin": True,
        "steps": [
            {"tool": "get_cpu_usage", "args": {}},
            {"tool": "get_ram_usage", "args": {}},
            {"tool": "get_disk_usage", "args": {}},
            {"tool": "get_battery", "args": {}}
        ],
        "prompt_template": "Provide a clear health summary of current system resource metrics: {{input}}"
    },
    {
        "slug": "project-handoff-summary",
        "name": "Project Handoff Summary",
        "description": "Gathers workspace git state and structured memories to generate a crisp progress handoff.",
        "enabled": True,
        "builtin": True,
        "steps": [
            {"tool": "run_command", "args": {"command": "git log -3 --oneline"}}
        ],
        "prompt_template": "Generate a crisp development progress and handoff summary based on recent activity: {{input}}"
    }
]


def get_skills_path(custom_dir: Optional[Union[str, Path]] = None) -> Path:
    """
    Resolve the path to skills.json.
    Honors explicit custom_dir, ORANGE_AI_USER_DATA_DIR env, or standard LOCALAPPDATA.
    Never writes to production %LOCALAPPDATA% during unit tests.
    """
    if custom_dir:
        return Path(custom_dir) / "skills.json"
    override = os.environ.get("ORANGE_AI_USER_DATA_DIR")
    if override:
        return Path(override) / "skills.json"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Orange AI" / "skills.json"
    return Path.home() / ".orange_ai" / "skills.json"


def _load_skills_file(path: Path) -> List[Dict[str, Any]]:
    """
    Load skills from JSON storage. Idempotently seeds built-in skills if missing.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        initial_skills = [dict(s) for s in BUILTIN_SKILLS]
        _save_skills_file(path, initial_skills)
        return initial_skills

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except Exception as e:
        logger.warning("Failed to load skills file '%s': %s. Re-seeding built-ins.", path, e)
        data = []

    # Idempotent seed check: ensure all built-in skills exist
    existing_slugs = {s.get("slug") for s in data if isinstance(s, dict)}
    updated = False
    for builtin in BUILTIN_SKILLS:
        if builtin["slug"] not in existing_slugs:
            data.append(dict(builtin))
            updated = True
        else:
            # Enforce builtin: True on built-in slugs
            for s in data:
                if s.get("slug") == builtin["slug"] and not s.get("builtin"):
                    s["builtin"] = True
                    updated = True

    if updated:
        _save_skills_file(path, data)

    return data


def _save_skills_file(path: Path, skills: List[Dict[str, Any]]) -> None:
    """
    Atomically persist skills to JSON storage.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".tmp_{uuid.uuid4().hex[:8]}")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(skills, f, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def compute_skill_risk(skill: Dict[str, Any]) -> str:
    """
    Evaluate the effective risk level of a skill based on its steps.
    Returns 'SAFE' if all steps execute SAFE tools; otherwise 'STATE_ALTERING'.
    """
    from tools.registry import registry
    steps = skill.get("steps", [])
    if not steps:
        return "SAFE"

    for step in steps:
        tool_name = step.get("tool")
        tool_obj = registry.get(tool_name)
        if not tool_obj:
            return "STATE_ALTERING"
        if tool_obj.risk_level != RiskLevel.SAFE:
            return "STATE_ALTERING"
    return "SAFE"


def list_skills(user_data_dir: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    """
    List all registered skills with metadata, step count, and effective risk badge.
    """
    path = get_skills_path(user_data_dir)
    raw_skills = _load_skills_file(path)
    result = []
    for s in raw_skills:
        skill_dict = dict(s)
        skill_dict["step_count"] = len(skill_dict.get("steps", []))
        skill_dict["risk_level"] = compute_skill_risk(skill_dict)
        result.append(skill_dict)
    return result


def create_skill(
    slug: str,
    name: str,
    description: str,
    steps: List[Dict[str, Any]],
    prompt_template: str = "",
    user_data_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Create and persist a new custom user skill.
    Validates slug uniqueness and alphanumeric format.
    """
    slug = (slug or "").strip().lower()
    if not slug or not re.match(r"^[a-z0-9_-]+$", slug):
        raise ValueError("Invalid slug format. Use lowercase alphanumeric characters, underscores, or hyphens.")

    if not isinstance(steps, list) or len(steps) == 0:
        raise ValueError("A skill must include at least one step.")

    for i, step in enumerate(steps):
        if not isinstance(step, dict) or "tool" not in step:
            raise ValueError(f"Step {i+1} must be an object with a 'tool' property.")

    path = get_skills_path(user_data_dir)
    skills = _load_skills_file(path)

    for existing in skills:
        if existing.get("slug") == slug:
            if existing.get("builtin"):
                raise ValueError(f"Cannot overwrite built-in skill '{slug}'.")
            # Replace custom skill
            skills.remove(existing)
            break

    new_skill = {
        "slug": slug,
        "name": (name or slug).strip(),
        "description": (description or "").strip(),
        "enabled": True,
        "builtin": False,
        "steps": steps,
        "prompt_template": (prompt_template or "").strip(),
    }
    skills.append(new_skill)
    _save_skills_file(path, skills)

    skill_result = dict(new_skill)
    skill_result["step_count"] = len(steps)
    skill_result["risk_level"] = compute_skill_risk(skill_result)
    return skill_result


def toggle_skill(
    slug: str,
    enabled: Optional[bool] = None,
    user_data_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Enable or disable a skill by slug.
    """
    slug = (slug or "").strip().lower()
    path = get_skills_path(user_data_dir)
    skills = _load_skills_file(path)

    target_skill = None
    for s in skills:
        if s.get("slug") == slug:
            target_skill = s
            break

    if not target_skill:
        raise ValueError(f"Skill '{slug}' not found.")

    if enabled is None:
        target_skill["enabled"] = not target_skill.get("enabled", True)
    else:
        target_skill["enabled"] = bool(enabled)

    _save_skills_file(path, skills)

    updated = dict(target_skill)
    updated["step_count"] = len(updated.get("steps", []))
    updated["risk_level"] = compute_skill_risk(updated)
    return updated


def delete_skill(
    slug: str,
    user_data_dir: Optional[Union[str, Path]] = None,
) -> bool:
    """
    Delete a custom skill. Rejects deleting built-in skills (builtin: true).
    """
    slug = (slug or "").strip().lower()
    path = get_skills_path(user_data_dir)
    skills = _load_skills_file(path)

    target_skill = None
    for s in skills:
        if s.get("slug") == slug:
            target_skill = s
            break

    if not target_skill:
        raise ValueError(f"Skill '{slug}' not found.")

    if target_skill.get("builtin"):
        raise ValueError(f"Cannot delete built-in skill '{slug}'. Disable it instead.")

    skills.remove(target_skill)
    _save_skills_file(path, skills)
    return True


def execute_skill(
    slug_or_dict: Union[str, Dict[str, Any]],
    input_text: str = "",
    prompt_fn: Optional[Callable[[str], str]] = None,
    full_access_mode: Optional[str] = None,
    user_data_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Execute a skill's sequential steps through tools.permissions.execute_with_permission.
    Honors is_catastrophic_command, trust mode (supervised vs unrestricted), and confirmation prompts.
    Haults cleanly if any step is BLOCKED or CANCELLED.
    """
    if isinstance(slug_or_dict, str):
        slug = slug_or_dict.strip().lower()
        skills = list_skills(user_data_dir=user_data_dir)
        target = next((s for s in skills if s.get("slug") == slug), None)
        if not target:
            return {
                "success": False,
                "status": "not_found",
                "slug": slug,
                "step_results": [],
                "output": f"Orange Skill '{slug}' was not found.",
            }
        if not target.get("enabled", True):
            return {
                "success": False,
                "status": "disabled",
                "slug": slug,
                "step_results": [],
                "output": f"Orange Skill '{slug}' is currently disabled.",
            }
        skill = target
    else:
        skill = slug_or_dict
        slug = skill.get("slug", "custom-skill")

    steps = skill.get("steps", [])
    step_results = []
    from tools.registry import registry

    for i, step in enumerate(steps):
        tool_name = step.get("tool")
        args_dict = dict(step.get("args", {}))

        # 1. Catastrophic command check on all step arguments
        for k, val in args_dict.items():
            if isinstance(val, str):
                is_cat, cat_reason = is_catastrophic_command(val)
                if is_cat:
                    step_results.append({
                        "tool": tool_name,
                        "status": "blocked",
                        "output": f"Action blocked: {cat_reason}",
                    })
                    return {
                        "success": False,
                        "status": "blocked",
                        "slug": slug,
                        "step_results": step_results,
                        "output": f"Execution halted: Step '{tool_name}' was blocked by safety guardrail ({cat_reason}).",
                    }

        # 2. Look up tool in registry
        tool_obj = registry.get(tool_name)
        if not tool_obj:
            step_results.append({
                "tool": tool_name,
                "status": "failed",
                "output": f"Error: Tool '{tool_name}' is not registered in ToolRegistry.",
            })
            return {
                "success": False,
                "status": "failed",
                "slug": slug,
                "step_results": step_results,
                "output": f"Execution halted: Tool '{tool_name}' is not registered.",
            }

        # 3. Execute via execute_with_permission (honoring trust mode & prompt_fn)
        step_res = execute_with_permission(
            tool=tool_obj,
            args=(),
            kwargs=args_dict,
            prompt_fn=prompt_fn,
            full_access_mode=full_access_mode,
        )

        # 4. Check for permission denial or user cancellation
        if isinstance(step_res, str) and step_res.startswith("Action blocked:"):
            step_results.append({
                "tool": tool_name,
                "status": "blocked",
                "output": step_res,
            })
            return {
                "success": False,
                "status": "blocked",
                "slug": slug,
                "step_results": step_results,
                "output": f"Execution halted: {step_res}",
            }

        if isinstance(step_res, str) and step_res.startswith("Action cancelled:"):
            step_results.append({
                "tool": tool_name,
                "status": "cancelled",
                "output": step_res,
            })
            return {
                "success": False,
                "status": "cancelled",
                "slug": slug,
                "step_results": step_results,
                "output": f"Execution cancelled: {step_res}",
            }

        step_results.append({
            "tool": tool_name,
            "status": "completed",
            "output": str(step_res),
        })

    # All steps completed successfully
    prompt_template = skill.get("prompt_template", "")
    lines = []
    if prompt_template:
        header = prompt_template.replace("{{input}}", input_text or "(none)")
        lines.append(header)
        lines.append("")
    for sr in step_results:
        lines.append(f"- **{sr['tool']}**: {sr['output']}")

    return {
        "success": True,
        "status": "completed",
        "slug": slug,
        "step_results": step_results,
        "output": "\n".join(lines),
    }


def run_skill(
    slug: str,
    input: str = "",
    user_data_dir: Optional[Union[str, Path]] = None,
    full_access_mode: Optional[str] = None,
    prompt_fn: Optional[Callable[[str], str]] = None,
) -> str:
    """
    Callable tool wrapper for ToolRegistry registration.
    Allows LLM multi-step goal loop and scheduled Agent Jobs to execute Orange Skills.
    """
    res = execute_skill(
        slug_or_dict=slug,
        input_text=input,
        prompt_fn=prompt_fn,
        full_access_mode=full_access_mode,
        user_data_dir=user_data_dir,
    )
    if not res.get("success"):
        return f"Skill '{slug}' execution failed ({res.get('status')}): {res.get('output')}"
    return res.get("output", f"Skill '{slug}' completed successfully.")
