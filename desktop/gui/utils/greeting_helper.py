"""
gui/utils/greeting_helper.py - Dynamic User Greeting Generator.

Generates clean, grammatically correct personalized greetings based on:
1. Time of day (morning, afternoon, evening)
2. Stored user identity in memory (dynamic first-name resolution)
3. Safe fallback when identity is absent or uninitialized

Eliminates punctuation defects (such as commas appended after question marks).
"""

from datetime import datetime
from typing import Optional


def get_user_first_name() -> Optional[str]:
    """Retrieve user's first name from structured identity memories if available."""
    try:
        import brain.memory as mem
        memories = mem.recall_structured()
        for m in memories:
            if m.get("category") == "identity" and m.get("key") == "name":
                val = str(m.get("value", "")).strip()
                if val:
                    # Clean trailing punctuation if any (e.g., "Sayan Dey.")
                    clean_val = val.rstrip(".!,?")
                    parts = clean_val.split()
                    if parts:
                        return parts[0]
    except Exception:
        pass
    return None


def get_user_greeting(hour: Optional[int] = None, name_override: Optional[str] = None) -> str:
    """
    Generate a grammatically correct, personalized greeting based on time of day.
    
    Time of day rules:
    - 00:00 - 11:59: "Good morning"
    - 12:00 - 17:59: "Good afternoon"
    - 18:00 - 23:59: "Good evening"
    
    Format:
    - When name is present: "{Salutation}, {First_Name} 👋"
    - When name is absent:  "{Salutation}! 👋"
    
    Guarantees:
    - Never appends a name or comma after a question mark or exclamation point.
    - Zero awkward punctuation or double commas.
    """
    if hour is None:
        hour = datetime.now().hour

    if hour < 12:
        salutation = "Good morning"
    elif hour < 18:
        salutation = "Good afternoon"
    else:
        salutation = "Good evening"

    name = name_override if name_override is not None else get_user_first_name()
    if name:
        clean_name = str(name).strip().rstrip(".!,?")
        if clean_name:
            return f"{salutation}, {clean_name} 👋"

    return f"{salutation}! 👋"
