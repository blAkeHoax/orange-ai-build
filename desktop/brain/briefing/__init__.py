"""
brain/briefing - Context-Aware Daily Briefing engine for Orange AI Desktop.

Stateless, read-only context collection and synthesis pipeline.
Stage 3.7B Implementation.
"""

from brain.briefing.models import (
    PriorityLevel,
    PriorityItem,
    ProjectBriefingContext,
    ReminderBriefingContext,
    JobBriefingContext,
    DailyBriefingContext,
    DailyBriefing,
)
from brain.briefing.collector import BriefingCollector
from brain.briefing.synthesizer import BriefingSynthesizer
from brain.briefing.settings import (
    is_morning_briefing_enabled,
    set_morning_briefing_enabled,
    get_last_briefing_date,
    set_last_briefing_date,
    should_deliver_morning_briefing,
)

__all__ = [
    "PriorityLevel",
    "PriorityItem",
    "ProjectBriefingContext",
    "ReminderBriefingContext",
    "JobBriefingContext",
    "DailyBriefingContext",
    "DailyBriefing",
    "BriefingCollector",
    "BriefingSynthesizer",
    "is_morning_briefing_enabled",
    "set_morning_briefing_enabled",
    "get_last_briefing_date",
    "set_last_briefing_date",
    "should_deliver_morning_briefing",
]
