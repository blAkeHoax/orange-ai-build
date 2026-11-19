from enum import Enum


class Intent(Enum):

    # ---------- Applications ----------
    OPEN_APP = "OPEN_APP"
    CLOSE_APP = "CLOSE_APP"

    # ---------- Files ----------
    OPEN_FOLDER = "OPEN_FOLDER"
    LIST_FILES = "LIST_FILES"
    READ_FILE = "READ_FILE"
    FIND_FILE = "FIND_FILE"
    FIND_FOLDER = "FIND_FOLDER"

    # ---------- Memory ----------
    REMEMBER = "REMEMBER"
    SHOW_MEMORIES = "SHOW_MEMORIES"
    SHOW_PROFILE = "SHOW_PROFILE"
    SHOW_PROJECTS = "SHOW_PROJECTS"
    SHOW_TOOLS = "SHOW_TOOLS"
    SHOW_PREFERENCES = "SHOW_PREFERENCES"

    # ---------- Reminders ----------
    SET_REMINDER = "SET_REMINDER"
    LIST_REMINDERS = "LIST_REMINDERS"
    CANCEL_REMINDER = "CANCEL_REMINDER"

    # ---------- Processes ----------
    SHOW_PROCESSES = "SHOW_PROCESSES"
    CHECK_PROCESS = "CHECK_PROCESS"

    # ---------- System ----------
    TIME = "TIME"
    DATE = "DATE"
    CPU = "CPU"
    RAM = "RAM"
    BATTERY = "BATTERY"
    DISK = "DISK"

    # ---------- AI ----------
    CHAT = "CHAT"
    WEB_SEARCH = "WEB_SEARCH"

    # ---------- Daily Briefing ----------
    DAILY_BRIEFING = "DAILY_BRIEFING"

    UNKNOWN = "UNKNOWN"
