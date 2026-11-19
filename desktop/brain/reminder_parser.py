"""
brain/reminder_parser.py - turns "remind me to call mom at 6pm" into a
(message, datetime) pair that brain/reminders.py can store.

Uses the `dateparser` library (pip install dateparser) since it handles
relative/natural phrasing ("at 6pm", "tomorrow at 5", "in 2 hours")
robustly and returns None on failure instead of raising, which makes the
calling code simple.
"""

import re
import dateparser

# Matches "remind me to <task> at/in/on <when>" and similar, splitting the
# task from the time expression. The time-related keyword ("at"/"in"/"on")
# is kept as part of the time text passed to dateparser, since dateparser
# understands "at 6pm" / "in 2 hours" / "on friday" natively -- stripping
# the keyword would actually make parsing worse, not better.
_REMINDER_SPLIT = re.compile(
    r"^remind me to (?P<task>.+?)\s+(?P<when>(?:at|in|on)\s+.+)$",
    re.IGNORECASE,
)


def parse_reminder(text: str):
    """
    Parse a reminder request into (task, due_at) where due_at is a
    datetime, or return (None, None) if the text couldn't be parsed.
    """
    match = _REMINDER_SPLIT.match(text.strip())

    if not match:
        return None, None

    task = match.group("task").strip()
    when_text = match.group("when").strip()

    due_at = dateparser.parse(
        when_text,
        settings={"PREFER_DATES_FROM": "future"},
    )

    if due_at is None or not task:
        return None, None

    return task, due_at