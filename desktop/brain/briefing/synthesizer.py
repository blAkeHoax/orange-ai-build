"""
brain/briefing/synthesizer.py - Briefing Synthesizer for Orange AI Desktop.

Stage 3.7B: Formulates budgeted briefing prompt and synthesizes a structured,
context-aware daily briefing via OrangeAPIClient.assist().
Strictly text generation only; never executes tools, autonomous agents, or DB writes.
Provides a deterministic local summary if Orange API is unavailable.
"""

import logging
from typing import Optional, Tuple

from brain.api_client import OrangeAPIClient
from brain.briefing.collector import sanitize_briefing_text
from brain.briefing.models import (
    DailyBriefingContext,
    DailyBriefing,
    PriorityLevel,
)

logger = logging.getLogger(__name__)

BRIEFING_SYSTEM_INSTRUCTION = """
You are Orange AI's Daily Briefing Synthesizer.

Your role is to produce a concise, professional, and actionable daily briefing for the user based EXCLUSIVELY on the verified facts provided below.

Strict Safety & Grounding Rules:
1. ONLY summarize the facts, projects, reminders, and jobs provided in the prompt.
2. DO NOT invent tasks, deadlines, milestones, accomplishments, or project relationships that are not explicitly provided.
3. DO NOT execute tools, propose shell commands, or generate code. This is an informational briefing only.
4. DO NOT make changes to memories, reminders, or jobs.
5. Highlight critical priorities first (overdue items and blocked confirmations).
6. Be direct, encouraging, calm, and concise.
7. Project Health Integrity: For any project with an assessment, report the EXACT status given (BLOCKED, PAUSED, PROGRESSING, ACTIVE_NO_RECENT_PROGRESS, COMPLETED, UNKNOWN). DO NOT independently determine, reinterpret, upgrade, downgrade, or override a project's health classification. An UNKNOWN status MUST remain UNKNOWN; explain the evidence limitation honestly without manufacturing confidence. Report any stuck-running job warning as a separate advisory.
8. Contextual Recommendations: When AUTHORITATIVE RECOMMENDATIONS are provided, explain them accurately and concisely under 'Suggested Next Steps'. DO NOT invent new recommendations, change their priority tier, or produce executable command payloads. Frame them as informational advice for the user.

Formatting:
Use standard markdown with clear section headers:
- ## 🌅 Executive Summary & Focus
- ## 🚨 Immediate Attention (Overdue & Confirmations)
- ## 📅 Today's Commitments & Active Work
- ## 🎯 Active Goals & Focus
- ## ✅ Recent Progress
- ## 🎯 Suggested Next Steps
""".strip()


class BriefingSynthesizer:
    """
    Synthesizes a DailyBriefing from a DailyBriefingContext.
    Executes pure text generation through OrangeAPIClient.assist() without tool invocation.
    """

    def __init__(self, api_client: Optional[OrangeAPIClient] = None):
        self.api_client = api_client or OrangeAPIClient()

    def synthesize(self, context: DailyBriefingContext, timeout: float = 30.0) -> DailyBriefing:
        """
        Synthesize the daily briefing.
        If Orange API is reachable, uses LLM narrative generation.
        If Orange API is unavailable or returns an error, generates a deterministic local summary.
        """
        prompt, system_instruction = self._build_prompt(context)

        # 1. Attempt API synthesis if available
        if self.api_client and self.api_client.is_available():
            try:
                res = self.api_client.assist(
                    message=prompt,
                    system_instruction=system_instruction,
                    profile=None,
                    timeout=timeout,
                )
                if res.get("success") and res.get("reply"):
                    reply_text = res["reply"].strip()
                    return DailyBriefing(
                        date=context.current_date,
                        summary_markdown=reply_text,
                        priorities=context.priorities,
                        raw_reply=reply_text,
                        model_name=res.get("model", "api"),
                        is_fallback=False,
                    )
            except Exception as e:
                logger.warning("[BRIEFING SYNTHESIZER] Orange API synthesis failed; using deterministic fallback: %s", e)

        # 2. Deterministic local synthesis fallback
        fallback_markdown = self._generate_deterministic_briefing(context)
        return DailyBriefing(
            date=context.current_date,
            summary_markdown=fallback_markdown,
            priorities=context.priorities,
            raw_reply=fallback_markdown,
            model_name="local_deterministic",
            is_fallback=True,
        )

    def _build_prompt(self, context: DailyBriefingContext, max_chars: int = 2500) -> Tuple[str, str]:
        """
        Construct a budgeted, privacy-filtered prompt from DailyBriefingContext.
        Guarantees that sensitive personal goals or untrusted dialogue instructions are excluded.
        """
        lines = [
            f"DATE: {context.current_date}",
            f"USER: {context.user_name}",
        ]

        # Primary Project
        if context.current_project:
            p_desc = f" ({context.current_project.description})" if context.current_project.description else ""
            lines.append(f"CURRENT PRIMARY PROJECT: {context.current_project.name}{p_desc}")
            if context.current_project.health:
                h = context.current_project.health
                status_str = h.status.value if hasattr(h.status, "value") else str(h.status)
                clean_exp = sanitize_briefing_text(h.explanation)
                lines.append(f"PRIMARY PROJECT HEALTH: [{status_str}] - {clean_exp}")
                if h.has_stuck_running_job:
                    lines.append("PRIMARY PROJECT WARNING: Running job has no recorded progress for > 24 hours.")
                if h.last_progress_at:
                    lines.append(f"LAST VERIFIED PROGRESS: {h.last_progress_at}")
                if h.evidence:
                    clean_ev = [sanitize_briefing_text(e) for e in h.evidence]
                    lines.append(f"HEALTH EVIDENCE: {'; '.join(clean_ev)}")
            if context.current_project.related_goals:
                lines.append(f"PROJECT GOALS: {', '.join(context.current_project.related_goals)}")
        else:
            lines.append("CURRENT PRIMARY PROJECT: None specified.")

        # Secondary Projects
        if context.secondary_projects:
            lines.append("SECONDARY PROJECTS:")
            for p in context.secondary_projects:
                desc = f" ({p.description})" if p.description else ""
                health_str = ""
                if p.health:
                    h_stat = p.health.status.value if hasattr(p.health.status, "value") else str(p.health.status)
                    clean_sec_exp = sanitize_briefing_text(p.health.explanation)
                    health_str = f" [Health: {h_stat} - {clean_sec_exp}]"
                    if p.health.has_stuck_running_job:
                        health_str += " (Warning: Running job > 24h idle)"
                lines.append(f"- {p.name}{desc}{health_str}")

        # General Goals (Unassociated with specific projects)
        if context.general_goals:
            lines.append(f"GENERAL ACTIVE GOALS: {', '.join(context.general_goals)}")

        # Priorities
        lines.append("\nCALCULATED PRIORITIES (Strict Rank Order):")
        for i, p in enumerate(context.priorities, 1):
            hint = f" -> Next: {p.action_hint}" if p.action_hint else ""
            lines.append(f"{i}. [{p.level.name}] {p.title}{hint}")

        # Recent Progress
        if context.recent_project_developments:
            lines.append("\nRECENT ACCOMPLISHMENTS (Last 24 Hours):")
            for dev in context.recent_project_developments:
                lines.append(f"- {dev}")

        # Actionable System Status
        if context.actionable_system_status:
            lines.append(f"\nSYSTEM ADVISORY: {context.actionable_system_status}")

        # Contextual Recommendations (Stage 3.7E.2D)
        recs = getattr(context, "recommendations", None)
        if recs:
            lines.append("\nAUTHORITATIVE RECOMMENDATIONS (Max 3):")
            for rec in recs:
                prio_str = rec.priority.value if hasattr(rec.priority, "value") else str(rec.priority)
                clean_title = sanitize_briefing_text(rec.title)
                clean_exp = sanitize_briefing_text(rec.explanation)
                clean_sugg = sanitize_briefing_text(rec.suggested_action)
                lines.append(f"- [{prio_str}] {clean_title}: {clean_exp} -> Suggestion: {clean_sugg} (ID: {rec.short_id})")

        prompt_body = "\n".join(lines)

        # Context budgeting: enforce character limit
        if len(prompt_body) > max_chars:
            prompt_body = prompt_body[:max_chars - 30] + "\n[Context truncated for budget]"

        user_message = f"Please synthesize today's daily briefing from the following verified facts:\n\n{prompt_body}"
        return user_message, BRIEFING_SYSTEM_INSTRUCTION

    def _generate_deterministic_briefing(self, context: DailyBriefingContext) -> str:
        """
        Generate a structured, predictable local markdown briefing when API is offline.
        Uses exact collector priorities without hallucination.
        """
        sections = []
        sections.append(f"# 🌅 Daily Briefing — {context.current_date}")

        # Focus & Project Health
        if context.current_project:
            p_desc = f": {context.current_project.description}" if context.current_project.description else ""
            sections.append(f"**Primary Focus:** {context.current_project.name}{p_desc}")
            if context.current_project.health:
                h = context.current_project.health
                status_str = h.status.value if hasattr(h.status, "value") else str(h.status)
                clean_exp = sanitize_briefing_text(h.explanation)
                sections.append(f"- **Health & Status:** `{status_str}` — {clean_exp}")
                if h.has_stuck_running_job:
                    sections.append("  - ⚠️ *Warning: Running background job has no progress recorded for > 24 hours.*")
                if h.last_progress_at:
                    sections.append(f"  - *Last Verified Progress:* {h.last_progress_at}")
        else:
            sections.append("**Primary Focus:** General maintenance & tasks")

        # Secondary Projects Health (if present)
        sec_with_health = [sp for sp in context.secondary_projects if sp.health]
        if sec_with_health:
            sections.append("\n## 📁 Secondary Projects")
            for sp in sec_with_health:
                h = sp.health
                status_str = h.status.value if hasattr(h.status, "value") else str(h.status)
                clean_sec_exp = sanitize_briefing_text(h.explanation)
                sections.append(f"- **{sp.name}:** `{status_str}` — {clean_sec_exp}")
                if h.has_stuck_running_job:
                    sections.append("  - ⚠️ *Warning: Running background job has no progress recorded for > 24 hours.*")

        # Critical / Immediate Attention (Tier 1)
        critical_items = [p for p in context.priorities if p.level == PriorityLevel.CRITICAL]
        if critical_items:
            sections.append("\n## 🚨 Immediate Attention Required")
            for item in critical_items:
                hint = f" *({item.action_hint})*" if item.action_hint else ""
                sections.append(f"- **{item.title}**{hint}")

        # High Priority (Tier 2 & 3: Due today & Active jobs)
        high_items = [p for p in context.priorities if p.level == PriorityLevel.HIGH]
        if high_items:
            sections.append("\n## 📅 Today's Commitments & Active Jobs")
            for item in high_items:
                hint = f" *({item.action_hint})*" if item.action_hint else ""
                sections.append(f"- {item.title}{hint}")

        # Medium Priority (Tier 4 & 5: Paused jobs & Project milestones)
        med_items = [p for p in context.priorities if p.level == PriorityLevel.MEDIUM and p.category != "project_current"]
        if med_items:
            sections.append("\n## ⏸️ Paused Work & Up Next")
            for item in med_items:
                hint = f" *({item.action_hint})*" if item.action_hint else ""
                sections.append(f"- {item.title}{hint}")

        # Active Goals & Focus (General Goals)
        if context.general_goals:
            sections.append("\n## 🎯 Active Goals & Focus")
            for g in context.general_goals:
                sections.append(f"- {g}")

        # Recent Progress
        if context.recent_project_developments:
            sections.append("\n## ✅ Recent Progress (Past 24h)")
            for dev in context.recent_project_developments:
                sections.append(f"- {dev}")

        # System Advisory
        if context.actionable_system_status:
            sections.append(f"\n## ⚠️ Hardware Notice\n- {context.actionable_system_status}")

        # Suggested Actions / Recommendations (Stage 3.7E.2D)
        sections.append("\n## 🎯 Suggested Next Steps")
        recs = getattr(context, "recommendations", None)
        if recs:
            for i, rec in enumerate(recs, 1):
                prio_str = rec.priority.value if hasattr(rec.priority, "value") else str(rec.priority)
                clean_title = sanitize_briefing_text(rec.title)
                clean_exp = sanitize_briefing_text(rec.explanation)
                clean_sugg = sanitize_briefing_text(rec.suggested_action)
                sections.append(f"{i}. **{clean_title}** `[{prio_str}]` — {clean_exp}")
                sections.append(f"   *Recommendation:* {clean_sugg} *(ID: `{rec.short_id}`)*")
        else:
            if critical_items:
                sections.append("1. Resolve critical overdue reminders or blocked job approvals first.")
            if high_items:
                sections.append("2. Complete scheduled tasks and check on running background jobs.")
            if context.current_project:
                sections.append(f"3. Advance milestone deliverables for **{context.current_project.name}**.")
            if not critical_items and not high_items and not context.current_project:
                sections.append("1. All systems and queues are clear. Ready for your instructions.")

        return "\n".join(sections)
