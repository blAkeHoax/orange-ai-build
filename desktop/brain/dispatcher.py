import re

from brain.intents import Intent
from brain.router import (
    OPEN_PREFIXES,
    CLOSE_PREFIXES,
    LIST_PREFIXES,
    READ_PREFIXES,
    FIND_PREFIXES,
    FIND_FOLDER_PREFIXES,
    CHECK_PROCESS_PREFIXES,
    WEB_SEARCH_PREFIXES,
)

from tools.registry import registry
from brain.reminder_parser import parse_reminder
from brain.reminders import add_reminder, cancel_reminder, list_pending
from brain.web_search import search_web


def _strip_prefix(text, prefixes):
    """
    Remove whichever of these prefixes the text actually starts with,
    then strip any leftover leading space.
    """
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def _folder_search_name(text):
    """Extract a folder name from supported direct and natural requests."""
    request = text.strip().rstrip(" ?")
    lowered = request.lower()
    for prefix in FIND_FOLDER_PREFIXES:
        if lowered.startswith(prefix):
            return request[len(prefix):].strip()

    match = re.fullmatch(
        r"(?:can you )?(?:open|find|search|locate) (?:the )?(.+?) folder",
        request,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else request


def dispatch(intent, user, prompt_fn=None):
    """
    Dispatch a recognized intent to the appropriate tool or capability.
    All tool calls are routed through the central ToolRegistry and Permission
    Architecture.
    """

    # =========================
    # APPS
    # =========================

    if intent == Intent.OPEN_APP:
        app = _strip_prefix(user, OPEN_PREFIXES).lower()
        return registry.execute("open_app", app, prompt_fn=prompt_fn)

    if intent == Intent.OPEN_FOLDER:
        path = user[12:].strip()
        return registry.execute("open_folder", path, prompt_fn=prompt_fn)

    if intent == Intent.CLOSE_APP:
        process = _strip_prefix(user, CLOSE_PREFIXES)
        return registry.execute("close_process", process, prompt_fn=prompt_fn)

    # =========================
    # FILES
    # =========================

    if intent == Intent.LIST_FILES:
        path = "."
        separator_index = user.lower().find(" in ")
        if separator_index != -1:
            path = user[separator_index + len(" in "):].strip()
        return registry.execute("list_files", path, prompt_fn=prompt_fn)

    if intent == Intent.READ_FILE:
        return registry.execute("read_file", _strip_prefix(user, READ_PREFIXES), prompt_fn=prompt_fn)

    if intent == Intent.FIND_FILE:
        return registry.execute("find_file", _strip_prefix(user, FIND_PREFIXES), prompt_fn=prompt_fn)

    if intent == Intent.FIND_FOLDER:
        return registry.execute("find_folder", _folder_search_name(user), prompt_fn=prompt_fn)

    # =========================
    # SYSTEM
    # =========================

    if intent == Intent.TIME:
        res = registry.execute("get_time", prompt_fn=prompt_fn)
        return f"Current time is {res}"

    if intent == Intent.DATE:
        res = registry.execute("get_date", prompt_fn=prompt_fn)
        return f"Today is {res}"

    if intent == Intent.CPU:
        res = registry.execute("get_cpu_usage", prompt_fn=prompt_fn)
        return f"CPU Usage: {res}"

    if intent == Intent.RAM:
        res = registry.execute("get_ram_usage", prompt_fn=prompt_fn)
        return f"RAM Usage: {res}"

    if intent == Intent.DISK:
        res = registry.execute("get_disk_usage", prompt_fn=prompt_fn)
        return f"Disk Usage: {res}"

    if intent == Intent.BATTERY:
        res = registry.execute("get_battery", prompt_fn=prompt_fn)
        return f"Battery: {res}"

    # =========================
    # PROCESSES
    # =========================

    if intent == Intent.SHOW_PROCESSES:
        return registry.execute("list_processes", prompt_fn=prompt_fn)

    if intent == Intent.CHECK_PROCESS:
        process = _strip_prefix(user, CHECK_PROCESS_PREFIXES)
        if registry.execute("is_running", process, prompt_fn=prompt_fn):
            return f"Yes, {process} is running."
        return f"No, {process} is not running."

    # =========================
    # REMINDERS
    # =========================

    if intent == Intent.SET_REMINDER:
        task, due_at = parse_reminder(user)
        if task is None:
            return (
                "I couldn't figure out the time for that reminder. "
                "Try something like 'remind me to call mom at 6pm'."
            )
        add_reminder(task, due_at)
        return f"Got it -- I'll remind you to {task} at {due_at.strftime('%I:%M %p on %B %d')}."

    if intent == Intent.LIST_REMINDERS:
        pending = list_pending()
        if not pending:
            return "You have no pending reminders."
        lines = []
        for r in pending:
            lines.append(f"- #{r['id']}: {r['message']} (due {r['due_at']})")
        return "Pending reminders:\n" + "\n".join(lines)

    if intent == Intent.CANCEL_REMINDER:
        reminder_id_text = user[len("cancel reminder"):].strip()
        try:
            reminder_id = int(reminder_id_text)
        except ValueError:
            return "Tell me the reminder number to cancel, e.g. 'cancel reminder 3'."
        if reminder_id <= 0:
            return "Reminder numbers must be positive."
        if cancel_reminder(reminder_id):
            return f"Cancelled reminder #{reminder_id}."
        return f"I couldn't find a pending reminder numbered #{reminder_id}."

    # =========================
    # WEB SEARCH
    # =========================

    if intent == Intent.WEB_SEARCH:
        query = _strip_prefix(user, WEB_SEARCH_PREFIXES)
        if not query:
            return "What would you like me to search for?"
        result = search_web(query)
        if result is None:
            return (
                "I couldn't reach the web search right now "
                "(no internet connection). "
                "Try again in a moment."
            )
        return result

    # =========================
    # NOT A TOOL COMMAND
    # =========================

    return None
