"""
Orange AI Desktop - UI Settings & Preference Persistence.

Handles loading, validating, and persisting user interface preferences (such as
the active theme) safely. Does NOT use memory.db and does not store sensitive or
conversational data.
"""

import json
import os
from typing import Dict, Any
from brain.paths import get_project_root

SETTINGS_FILENAME = "gui_settings.json"
SUPPORTED_THEMES = ("light", "dark")
DEFAULT_THEME = "light"
DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme": DEFAULT_THEME,
    "hybrid_memory_extraction_enabled": True,
    "hybrid_memory_confidence_threshold": 0.75,
    "adaptive_empathy_enabled": True,
    "minimize_to_tray_enabled": True,
    "desktop_toasts_enabled": True,
    "full_access_mode": "supervised",
}


def get_settings_path() -> str:
    """Return the absolute path to the UI settings file."""
    p = os.path.join(get_project_root(), SETTINGS_FILENAME)
    alt = os.path.join(get_project_root(), "settings.json")
    if not os.path.exists(p) and os.path.exists(alt):
        return alt
    return p


def load_settings() -> Dict[str, Any]:
    """
    Load settings from disk. Returns a default dictionary if the file is missing,
    unreadable, or contains malformed JSON.
    """
    path = get_settings_path()
    res = dict(DEFAULT_SETTINGS)
    if not os.path.exists(path):
        return res

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return res
            res.update(data)
            if isinstance(data.get("hybrid_memory_extraction_enabled"), bool):
                res["hybrid_memory_extraction_enabled"] = data["hybrid_memory_extraction_enabled"]
            if isinstance(data.get("adaptive_empathy_enabled"), bool):
                res["adaptive_empathy_enabled"] = data["adaptive_empathy_enabled"]
            if "hybrid_memory_confidence_threshold" in data:
                try:
                    res["hybrid_memory_confidence_threshold"] = max(0.5, min(1.0, float(data["hybrid_memory_confidence_threshold"])))
                except (ValueError, TypeError):
                    pass
            if data.get("full_access_mode") in ("supervised", "unrestricted"):
                res["full_access_mode"] = data["full_access_mode"]
            return res
    except Exception:
        # Graceful fallback on corrupt, partial, or locked files
        return res


def save_settings(settings: Dict[str, Any]) -> bool:
    """
    Persist settings to disk atomically.
    Returns True on success, False on failure.
    """
    path = get_settings_path()
    try:
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(temp_path, path)
        return True
    except Exception:
        return False


def get_theme_preference() -> str:
    """
    Get the persisted theme preference.
    Guarantees returning either 'light' or 'dark'. Defaults to 'light'.
    """
    settings = load_settings()
    theme = str(settings.get("theme", "")).strip().lower()
    if theme in SUPPORTED_THEMES:
        return theme
    return DEFAULT_THEME


def set_theme_preference(theme: str) -> bool:
    """
    Update and persist the theme preference.
    Rejects unsupported theme values.
    """
    theme_clean = str(theme).strip().lower()
    if theme_clean not in SUPPORTED_THEMES:
        return False

    settings = load_settings()
    settings["theme"] = theme_clean
    return save_settings(settings)


def get_api_url() -> Optional[str]:
    """
    Get the configured Orange API endpoint URL, if any.
    Returns None if not explicitly configured in UI settings.
    """
    settings = load_settings()
    url = settings.get("api_url")
    return str(url).strip() if url else None


def set_api_url(url: str) -> bool:
    """
    Update and persist the Orange API endpoint URL.
    """
    settings = load_settings()
    settings["api_url"] = str(url).strip()
    return save_settings(settings)


# ============================================================================
# Proactive Personal Assistance Settings (Stage 4.1)
# ============================================================================

def get_morning_briefing_enabled() -> bool:
    """Return whether morning briefing delivery is enabled."""
    from brain.briefing.settings import is_morning_briefing_enabled
    return is_morning_briefing_enabled()


def set_morning_briefing_enabled(enabled: bool) -> bool:
    """Persist morning briefing enabled preference."""
    from brain.briefing.settings import set_morning_briefing_enabled as _set_mb
    return _set_mb(enabled)


def get_preferred_briefing_time() -> str:
    """Return preferred morning delivery time (HH:MM, 24-hour)."""
    from brain.briefing.settings import get_preferred_briefing_time as _get_pbt
    return _get_pbt()


def validate_briefing_time(time_str: Optional[str]) -> bool:
    """Validate strict 24-hour HH:MM time format."""
    from brain.briefing.settings import validate_briefing_time as _val
    return _val(time_str)


def set_preferred_briefing_time(time_str: str) -> bool:
    """Validate and persist preferred morning delivery time."""
    from brain.briefing.settings import set_preferred_briefing_time as _set_pbt
    return _set_pbt(time_str)


def get_contextual_advisories_enabled() -> bool:
    """Return whether contextual chat advisories are enabled."""
    from brain.briefing.settings import is_contextual_advisories_enabled
    return is_contextual_advisories_enabled()


def set_contextual_advisories_enabled(enabled: bool) -> bool:
    """Persist contextual chat advisories enabled preference."""
    from brain.briefing.settings import set_contextual_advisories_enabled as _set_ca
    return _set_ca(enabled)


def get_advisory_cooldown_hours() -> int:
    """Return minimum advisory cooldown hours (2, 4, or 8)."""
    from brain.briefing.settings import get_advisory_cooldown_hours as _get_cd
    return _get_cd()


def set_advisory_cooldown_hours(hours: int) -> bool:
    """Persist minimum advisory cooldown hours."""
    from brain.briefing.settings import set_advisory_cooldown_hours as _set_cd
    return _set_cd(hours)

