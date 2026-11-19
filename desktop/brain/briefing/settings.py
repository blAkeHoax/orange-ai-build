"""
brain/briefing/settings.py - User Configuration & Delivery State for Daily Briefing.

Stage 3.7C: Manages user preferences for proactive morning briefings and tracks
delivery dates for deduplication without modifying memory.db or introducing
database schema migrations.
"""

import json
import logging
import os
from datetime import datetime, time
from typing import Dict, Any, Optional

from gui.settings import load_settings, save_settings

logger = logging.getLogger(__name__)

KEY_MORNING_ENABLED = "morning_briefing_enabled"
KEY_LAST_BRIEFING_DATE = "last_briefing_date"
KEY_PREFERRED_BRIEFING_TIME = "preferred_briefing_time"
KEY_CONTEXTUAL_ADVISORIES_ENABLED = "contextual_advisories_enabled"
KEY_ADVISORY_COOLDOWN_HOURS = "advisory_cooldown_hours"

DEFAULT_BRIEFING_TIME = "08:30"
DEFAULT_CONTEXTUAL_ADVISORIES_ENABLED = True
DEFAULT_ADVISORY_COOLDOWN_HOURS = 4
SUPPORTED_COOLDOWN_HOURS = (2, 4, 8)


def validate_briefing_time(time_str: Optional[str]) -> bool:
    """Validate that time_str is strictly in 24-hour HH:MM format (00:00 to 23:59)."""
    if not isinstance(time_str, str):
        return False
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        return False
    if len(parts[0]) != 2 or len(parts[1]) != 2:
        return False
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        return 0 <= hh <= 23 and 0 <= mm <= 59
    except ValueError:
        return False


def get_preferred_briefing_time() -> str:
    """Return preferred morning delivery time in HH:MM (24-hour). Defaults to '08:30'."""
    try:
        settings = load_settings()
        val = str(settings.get(KEY_PREFERRED_BRIEFING_TIME, "")).strip()
        if validate_briefing_time(val):
            return val
        return DEFAULT_BRIEFING_TIME
    except Exception as e:
        logger.warning("Error reading preferred briefing time: %s", e)
        return DEFAULT_BRIEFING_TIME


def set_preferred_briefing_time(time_str: str) -> bool:
    """Persist preferred delivery time after strict HH:MM validation."""
    clean_val = str(time_str).strip()
    if not validate_briefing_time(clean_val):
        logger.warning("Rejected invalid briefing time: %r", time_str)
        return False
    try:
        settings = load_settings()
        settings[KEY_PREFERRED_BRIEFING_TIME] = clean_val
        return save_settings(settings)
    except Exception as e:
        logger.error("Error saving preferred briefing time: %s", e)
        return False


def is_contextual_advisories_enabled() -> bool:
    """Return whether contextual chat advisories are enabled. Defaults to True."""
    try:
        settings = load_settings()
        val = settings.get(KEY_CONTEXTUAL_ADVISORIES_ENABLED, DEFAULT_CONTEXTUAL_ADVISORIES_ENABLED)
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, str)):
            s = str(val).strip().lower()
            if s in ("true", "1", "yes"):
                return True
            if s in ("false", "0", "no"):
                return False
        return DEFAULT_CONTEXTUAL_ADVISORIES_ENABLED
    except Exception as e:
        logger.warning("Error reading contextual advisories setting: %s", e)
        return DEFAULT_CONTEXTUAL_ADVISORIES_ENABLED


def set_contextual_advisories_enabled(enabled: bool) -> bool:
    """Enable or disable contextual chat recommendations."""
    try:
        settings = load_settings()
        settings[KEY_CONTEXTUAL_ADVISORIES_ENABLED] = bool(enabled)
        return save_settings(settings)
    except Exception as e:
        logger.error("Error saving contextual advisories setting: %s", e)
        return False


def get_advisory_cooldown_hours() -> int:
    """Return minimum cooldown hours for contextual advisories. Defaults to 4."""
    try:
        settings = load_settings()
        val = settings.get(KEY_ADVISORY_COOLDOWN_HOURS, DEFAULT_ADVISORY_COOLDOWN_HOURS)
        try:
            val_int = int(val)
            if val_int in SUPPORTED_COOLDOWN_HOURS:
                return val_int
        except (ValueError, TypeError):
            pass
        return DEFAULT_ADVISORY_COOLDOWN_HOURS
    except Exception as e:
        logger.warning("Error reading advisory cooldown setting: %s", e)
        return DEFAULT_ADVISORY_COOLDOWN_HOURS


def set_advisory_cooldown_hours(hours: int) -> bool:
    """Persist minimum cooldown hours (must be 2, 4, or 8)."""
    try:
        hours_int = int(hours)
    except (ValueError, TypeError):
        return False
    if hours_int not in SUPPORTED_COOLDOWN_HOURS:
        logger.warning("Rejected unsupported advisory cooldown: %r", hours)
        return False
    try:
        settings = load_settings()
        settings[KEY_ADVISORY_COOLDOWN_HOURS] = hours_int
        return save_settings(settings)
    except Exception as e:
        logger.error("Error saving advisory cooldown setting: %s", e)
        return False


def is_morning_briefing_enabled() -> bool:
    """Return whether the proactive morning briefing experience is enabled by the user."""
    try:
        settings = load_settings()
        return bool(settings.get(KEY_MORNING_ENABLED, False))
    except Exception as e:
        logger.warning("Error reading morning briefing setting: %s", e)
        return False


def set_morning_briefing_enabled(enabled: bool) -> bool:
    """Enable or disable proactive morning briefing experience."""
    try:
        settings = load_settings()
        settings[KEY_MORNING_ENABLED] = bool(enabled)
        return save_settings(settings)
    except Exception as e:
        logger.error("Error saving morning briefing setting: %s", e)
        return False


def get_last_briefing_date() -> Optional[str]:
    """Return the date string (YYYY-MM-DD) when a briefing was last delivered, or None."""
    try:
        settings = load_settings()
        val = settings.get(KEY_LAST_BRIEFING_DATE)
        return str(val) if val else None
    except Exception:
        return None


def set_last_briefing_date(date_str: str) -> bool:
    """Record that a briefing was delivered on date_str (YYYY-MM-DD) for deduplication."""
    try:
        settings = load_settings()
        settings[KEY_LAST_BRIEFING_DATE] = date_str
        return save_settings(settings)
    except Exception as e:
        logger.error("Error recording last briefing date: %s", e)
        return False


def should_deliver_morning_briefing(now: Optional[datetime] = None) -> bool:
    """
    Check if a proactive morning briefing should be delivered.

    Returns True only when:
    1. Morning briefing is enabled by the user.
    2. The current local calendar date has not already been marked as delivered.
    3. The current local time is greater than or equal to the configured preferred
       briefing time (strict 24-hour HH:MM format, defaulting to '08:30').
    """
    if not is_morning_briefing_enabled():
        return False

    ref_now = now if now is not None else datetime.now()
    if ref_now.tzinfo is not None:
        ref_now = ref_now.astimezone().replace(tzinfo=None)

    current_date = ref_now.strftime("%Y-%m-%d")
    if get_last_briefing_date() == current_date:
        return False

    pref_time_str = get_preferred_briefing_time()
    try:
        parts = pref_time_str.split(":")
        hh = int(parts[0])
        mm = int(parts[1])
        target_time = time(hh, mm)
    except Exception:
        target_time = time(8, 30)

    if ref_now.time() < target_time:
        return False

    return True
