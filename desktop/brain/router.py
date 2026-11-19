import re

from brain.intents import Intent


# ==========================================================
# COMMAND PREFIXES
# ==========================================================

OPEN_PREFIXES = (
    "open ",
    "launch ",
    "start ",
)

CLOSE_PREFIXES = (
    "close ",
    "terminate ",
    "kill ",
)

LIST_PREFIXES = (
    "list files",
    "show files",
)

READ_PREFIXES = (
    "read file",
)

FIND_PREFIXES = (
    "find file",
    "search file",
)

FIND_FOLDER_PREFIXES = (
    "find folder ",
    "search folder ",
    "locate folder ",
)

PROCESS_PREFIXES = (
    "show processes",
)

CHECK_PROCESS_PREFIXES = (
    "is running",
    "check process",
)

# Explicit trigger for live web search queries.
WEB_SEARCH_PREFIXES = (
    "search the web for",
    "search the web",
    "search online for",
    "search online",
    "google ",
    "look up ",
    "look online for",
)


# ==========================================================
# SYSTEM REQUESTS
# ==========================================================

TIME_PATTERNS = (
    "what time is it",
    "what's the time",
    "tell me the time",
    "current time",
    "time now",
)

DATE_PATTERNS = (
    "what date is it",
    "today's date",
    "current date",
    "what day is it",
)

BATTERY_PATTERNS = (
    "battery status",
    "battery level",
    "battery percentage",
    "battery percent",
    "how much battery",
    "remaining battery",
    "battery remaining",
    "check battery",
)

CPU_PATTERNS = (
    "cpu usage",
    "processor usage",
    "processor load",
    "cpu load",
    "cpu status",
    "processor status",
)

RAM_PATTERNS = (
    "ram usage",
    "memory usage",
    "memory status",
    "ram status",
    "free memory",
    "available memory",
)

DISK_PATTERNS = (
    "disk usage",
    "storage usage",
    "disk space",
    "storage space",
    "free disk",
    "free storage",
    "available storage",
)


# ==========================================================
# HELPER
# ==========================================================

def contains(text, patterns):
    return any(pattern in text for pattern in patterns)


# ==========================================================
# ROUTER
# ==========================================================

def route(user_input):

    text = user_input.lower().strip()

    if not text:
        return Intent.CHAT

    # ======================================================
    # MEMORY
    # ======================================================

    if text.startswith("remember "):
        return Intent.REMEMBER

    if text in (
        "show memories",
        "list memories",
    ):
        return Intent.SHOW_MEMORIES

    if text in (
        "show profile",
        "view profile",
    ):
        return Intent.SHOW_PROFILE

    if text in (
        "show projects",
        "list projects",
        "view projects",
    ):
        return Intent.SHOW_PROJECTS

    if text in (
        "show tools",
        "list tools",
        "view tools",
    ):
        return Intent.SHOW_TOOLS

    if text in (
        "show preferences",
        "list preferences",
        "view preferences",
    ):
        return Intent.SHOW_PREFERENCES

    # ======================================================
    # REMINDERS
    # ======================================================

    if text.startswith("remind me"):
        return Intent.SET_REMINDER

    if text in (
        "show reminders",
        "list reminders",
        "my reminders",
    ):
        return Intent.LIST_REMINDERS

    if text.startswith("cancel reminder"):
        return Intent.CANCEL_REMINDER

    # ======================================================
    # APPS
    # ======================================================

    if text.startswith("open folder"):
        return Intent.OPEN_FOLDER

    if text.startswith(OPEN_PREFIXES):
        return Intent.OPEN_APP

    if text.startswith(CLOSE_PREFIXES):
        return Intent.CLOSE_APP

    # ======================================================
    # FILES
    # ======================================================

    if text.startswith(LIST_PREFIXES):
        return Intent.LIST_FILES

    if text.startswith(READ_PREFIXES):
        return Intent.READ_FILE

    if text.startswith(FIND_PREFIXES):
        return Intent.FIND_FILE

    if text.startswith(FIND_FOLDER_PREFIXES):
        return Intent.FIND_FOLDER

    # Treat a natural request such as "can you open Movies folder?" as a
    # search request.  The dispatcher returns matching paths; it does not
    # open an arbitrary match automatically.
    if re.fullmatch(
        r"(?:can you )?(?:open|find|search|locate) (?:the )?.+ folder\s*\??",
        text,
    ):
        return Intent.FIND_FOLDER

    # ======================================================
    # PROCESSES
    # ======================================================

    if text.startswith(PROCESS_PREFIXES):
        return Intent.SHOW_PROCESSES

    if text.startswith(CHECK_PROCESS_PREFIXES):
        return Intent.CHECK_PROCESS

    # ======================================================
    # WEB SEARCH
    # ======================================================

    if text.startswith(WEB_SEARCH_PREFIXES):
        return Intent.WEB_SEARCH

    # ======================================================
    # SYSTEM
    # ======================================================

    if contains(text, TIME_PATTERNS):
        return Intent.TIME

    if contains(text, DATE_PATTERNS):
        return Intent.DATE

    if contains(text, BATTERY_PATTERNS):
        return Intent.BATTERY

    if contains(text, CPU_PATTERNS):
        return Intent.CPU

    if contains(text, RAM_PATTERNS):
        return Intent.RAM

    if contains(text, DISK_PATTERNS):
        return Intent.DISK

    # ======================================================
    # CONVERSATION
    # ======================================================

    # ======================================================
    # DAILY BRIEFING
    # ======================================================

    if text in (
        "briefing",
        "daily briefing",
        "show briefing",
        "get briefing",
        "today's briefing",
        "todays briefing",
    ):
        return Intent.DAILY_BRIEFING

    return Intent.CHAT
