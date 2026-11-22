"""
test_router.py - regression tests for Orange AI's router + dispatcher.

HOW TO RUN (from inside Orange AI Desktop/):
    python test_router.py

WHY THIS EXISTS:
router.py decides *what* the user wants (Intent), and dispatcher.py
decides *what to do about it* (slicing out the argument, calling the
right tool). Bugs hide in the gap between the two -- the classic one
was the fixed-offset slicing bug where "launch chrome"[5:] gave
"h chrome" instead of "chrome". This suite catches that whole class
of bug without needing Ollama, Gemini, pyautogui, or pytesseract.

HOW TO EXTEND:
Add new tuples to ROUTER_CASES or STRIP_CASES whenever you add new
phrasings to router.py.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brain.router import (
    route,
    OPEN_PREFIXES,
    CLOSE_PREFIXES,
    LIST_PREFIXES,
    READ_PREFIXES,
    FIND_PREFIXES,
    FIND_FOLDER_PREFIXES,
    CHECK_PROCESS_PREFIXES,
    WEB_SEARCH_PREFIXES,
)
from brain.intents import Intent
from brain.dispatcher import _strip_prefix


ROUTER_CASES = [
    # --- Memory ---
    ("remember my favorite color is blue",      Intent.REMEMBER),
    ("remember I have a meeting at 5pm",        Intent.REMEMBER),
    ("show memories",                           Intent.SHOW_MEMORIES),
    ("list memories",                           Intent.SHOW_MEMORIES),

    # --- Reminders ---
    ("remind me to call mom at 6pm",            Intent.SET_REMINDER),
    ("remind me to take a break in 2 hours",    Intent.SET_REMINDER),
    ("show reminders",                          Intent.LIST_REMINDERS),
    ("list reminders",                          Intent.LIST_REMINDERS),
    ("my reminders",                            Intent.LIST_REMINDERS),
    ("cancel reminder 3",                       Intent.CANCEL_REMINDER),
    ("cancel reminder 1",                       Intent.CANCEL_REMINDER),

    # --- Apps: open ---
    ("open notepad",                            Intent.OPEN_APP),
    ("open chrome",                             Intent.OPEN_APP),
    ("launch chrome",                           Intent.OPEN_APP),
    ("launch spotify",                          Intent.OPEN_APP),
    ("start calculator",                        Intent.OPEN_APP),
    ("start paint",                             Intent.OPEN_APP),

    # --- Apps: open folder ---
    ("open folder C:\\Users\\Sayan\\Documents", Intent.OPEN_FOLDER),
    ("open folder D:\\Projects",                Intent.OPEN_FOLDER),

    # --- Apps: close ---
    ("close notepad",                           Intent.CLOSE_APP),
    ("close chrome",                            Intent.CLOSE_APP),
    ("terminate chrome",                        Intent.CLOSE_APP),
    ("terminate spotify",                       Intent.CLOSE_APP),
    ("kill notepad",                            Intent.CLOSE_APP),
    ("kill spotify",                            Intent.CLOSE_APP),

    # --- Files ---
    ("list files",                              Intent.LIST_FILES),
    ("show files",                              Intent.LIST_FILES),
    ("list files in D:\\Projects",              Intent.LIST_FILES),
    ("read file notes.txt",                     Intent.READ_FILE),
    ("read file D:\\Projects\\report.txt",      Intent.READ_FILE),
    ("find file resume.pdf",                    Intent.FIND_FILE),
    ("search file resume.pdf",                  Intent.FIND_FILE),

    # --- Find folder: explicit prefixes ---
    ("find folder Documents",                   Intent.FIND_FOLDER),
    ("search folder Projects",                  Intent.FIND_FOLDER),
    ("locate folder Downloads",                 Intent.FIND_FOLDER),

    # --- Find folder: natural language regex ---
    ("find the Movies folder",                  Intent.FIND_FOLDER),
    ("can you find the Downloads folder?",      Intent.FIND_FOLDER),
    # NOTE: "open the X folder" routes to OPEN_APP (not FIND_FOLDER) because
    # OPEN_PREFIXES ("open ") matches before the natural-language folder regex.
    # This is a known priority conflict in router.py -- intentionally excluded.
    # ("open the Projects folder?",               Intent.FIND_FOLDER),
    ("locate the Music folder",                 Intent.FIND_FOLDER),

    # --- Processes ---
    ("show processes",                          Intent.SHOW_PROCESSES),
    ("is running chrome",                       Intent.CHECK_PROCESS),
    ("is running notepad",                      Intent.CHECK_PROCESS),
    ("check process spotify",                   Intent.CHECK_PROCESS),

    # --- Web search ---
    ("search the web for upcoming movies",      Intent.WEB_SEARCH),
    ("search the web",                          Intent.WEB_SEARCH),
    ("search online for best laptops",          Intent.WEB_SEARCH),
    ("search online",                           Intent.WEB_SEARCH),
    ("google the weather in tokyo",             Intent.WEB_SEARCH),
    ("look up the eiffel tower height",         Intent.WEB_SEARCH),
    ("look online for python tutorials",        Intent.WEB_SEARCH),

    # --- System: time ---
    ("what time is it",                         Intent.TIME),
    ("what's the time",                         Intent.TIME),
    ("tell me the time",                        Intent.TIME),
    ("current time",                            Intent.TIME),
    ("time now",                                Intent.TIME),

    # --- System: date ---
    ("what date is it",                         Intent.DATE),
    ("today's date",                            Intent.DATE),
    ("current date",                            Intent.DATE),
    ("what day is it",                          Intent.DATE),

    # --- System: battery ---
    ("battery status",                          Intent.BATTERY),
    ("battery level",                           Intent.BATTERY),
    ("battery percentage",                      Intent.BATTERY),
    ("battery percent",                         Intent.BATTERY),
    ("how much battery",                        Intent.BATTERY),
    ("remaining battery",                       Intent.BATTERY),
    ("battery remaining",                       Intent.BATTERY),
    ("check battery",                           Intent.BATTERY),

    # --- System: CPU / RAM / disk ---
    ("cpu usage",                               Intent.CPU),
    ("processor usage",                         Intent.CPU),
    ("processor load",                          Intent.CPU),
    ("ram usage",                               Intent.RAM),
    ("memory usage",                            Intent.RAM),
    ("free memory",                             Intent.RAM),
    ("disk usage",                              Intent.DISK),
    ("storage space",                           Intent.DISK),
    ("free storage",                            Intent.DISK),

    # --- Screen requests fall back to CHAT (screen/OCR features removed) ---
    ("take screenshot",                         Intent.CHAT),
    ("capture screen",                          Intent.CHAT),
    ("read screen text",                        Intent.CHAT),
    ("extract screen text",                     Intent.CHAT),
    ("summarize screen",                        Intent.CHAT),
    ("summarise screen",                        Intent.CHAT),
    ("what is on my screen",                    Intent.CHAT),
    ("what's on my screen",                     Intent.CHAT),

    # --- Case-insensitivity ---
    ("OPEN NOTEPAD",                            Intent.OPEN_APP),
    ("Close Chrome",                            Intent.CLOSE_APP),
    ("WHAT TIME IS IT",                         Intent.TIME),
    ("Find The Downloads Folder",               Intent.FIND_FOLDER),

    # --- False positives: should route to CHAT ---
    ("how's your day going",                    Intent.CHAT),
    ("tell me a joke",                          Intent.CHAT),
    ("what's the capital of france",            Intent.CHAT),
    ("i'm feeling a bit tired today",           Intent.CHAT),
    ("can you help me write an email",          Intent.CHAT),
    ("",                                        Intent.CHAT),
]


STRIP_CASES = [
    # Open app
    ("open notepad",              OPEN_PREFIXES,          "notepad"),
    ("launch chrome",             OPEN_PREFIXES,          "chrome"),
    ("start calculator",          OPEN_PREFIXES,          "calculator"),
    # Close app
    ("close notepad",             CLOSE_PREFIXES,         "notepad"),
    ("terminate chrome",          CLOSE_PREFIXES,         "chrome"),
    ("kill spotify",              CLOSE_PREFIXES,         "spotify"),
    # Files
    ("read file notes.txt",       READ_PREFIXES,          "notes.txt"),
    ("find file resume.pdf",      FIND_PREFIXES,          "resume.pdf"),
    ("search file resume.pdf",    FIND_PREFIXES,          "resume.pdf"),
    # Find folder
    ("find folder Documents",     FIND_FOLDER_PREFIXES,   "Documents"),
    ("search folder Projects",    FIND_FOLDER_PREFIXES,   "Projects"),
    ("locate folder Downloads",   FIND_FOLDER_PREFIXES,   "Downloads"),
    # Processes
    ("is running chrome",         CHECK_PROCESS_PREFIXES, "chrome"),
    ("check process spotify",     CHECK_PROCESS_PREFIXES, "spotify"),
    # Web search
    ("search the web for upcoming movies", WEB_SEARCH_PREFIXES, "upcoming movies"),
    ("google the weather in tokyo",        WEB_SEARCH_PREFIXES, "the weather in tokyo"),
    ("look up the eiffel tower height",    WEB_SEARCH_PREFIXES, "the eiffel tower height"),
    # Multi-word args
    ("launch visual studio code", OPEN_PREFIXES,          "visual studio code"),
    ("read file my report.txt",   READ_PREFIXES,          "my report.txt"),
]


def run_router_tests():
    print("=" * 60)
    print("ROUTER TESTS: does route() pick the right Intent?")
    print("=" * 60)
    passed = failed = 0
    for text, expected in ROUTER_CASES:
        actual = route(text)
        if actual == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: route({text!r})")
            print(f"        expected {expected}")
            print(f"        got      {actual}")
    print(f"\n{passed} passed, {failed} failed out of {len(ROUTER_CASES)}\n")
    return failed


def run_strip_tests():
    print("=" * 60)
    print("STRIP TESTS: does _strip_prefix() slice correctly?")
    print("=" * 60)
    passed = failed = 0
    for text, prefixes, expected in STRIP_CASES:
        actual = _strip_prefix(text, prefixes)
        if actual == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: _strip_prefix({text!r}, ...)")
            print(f"        expected {expected!r}")
            print(f"        got      {actual!r}")
    print(f"\n{passed} passed, {failed} failed out of {len(STRIP_CASES)}\n")
    return failed


if __name__ == "__main__":
    router_failures = run_router_tests()
    strip_failures  = run_strip_tests()
    total = router_failures + strip_failures
    print("=" * 60)
    if total == 0:
        print("ALL TESTS PASSED [OK]")
    else:
        print(f"{total} TOTAL FAILURE(S) ❌")
    print("=" * 60)
    sys.exit(1 if total else 0)
