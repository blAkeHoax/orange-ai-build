import subprocess
import os
import shutil

# Apps that Windows can resolve by name alone, no path needed.
_SIMPLE_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
}

# Apps that usually need a real install path. We try common locations
# and shutil.which() instead of hardcoding one machine's path.
_SEARCH_APPS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
}


def _application_search_roots() -> list[str]:
    """Common machine-wide and per-user install locations on Windows."""
    candidates = (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        os.environ.get("WINDIR"),
    )
    return [path for path in candidates if path and os.path.isdir(path)]


def _find_installed_app(app_name: str) -> str | None:
    """Find an explicitly requested executable in standard install folders."""
    target = app_name.lower().strip()
    if not target.endswith(".exe"):
        target += ".exe"

    for search_root in _application_search_roots():
        for root, _directories, files in os.walk(search_root, onerror=lambda _error: None):
            for filename in files:
                if filename.lower() == target:
                    return os.path.join(root, filename)
    return None


def _resolve_search_app(app_name: str) -> str | None:
    """Try shutil.which() first (works if it's on PATH), then known paths."""
    which_result = shutil.which(app_name)
    if which_result:
        return which_result

    for candidate_path in _SEARCH_APPS.get(app_name, []):
        if os.path.exists(candidate_path):
            return candidate_path

    return None


# Side-effect note (LIM-01 / LIM-03): Spawning OS processes has external side effects
# that cannot be guaranteed exactly-once across process crashes.
def open_app(app_name: str) -> str:
    """
    Open a desktop application by name, e.g. 'notepad', 'chrome', 'calculator'.

    The original version hardcoded Chrome's path to one specific
    install location (r"C:\\Program Files\\Google\\Chrome\\..."), which
    breaks on any machine where Chrome is installed elsewhere (e.g. a
    per-user install under AppData, or a different drive). This version
    tries shutil.which() first and falls back to a list of common paths.
    """
    app_name = app_name.lower().strip()

    if app_name in _SIMPLE_APPS:
        try:
            subprocess.Popen(_SIMPLE_APPS[app_name])
            return f"Opening {app_name}."
        except Exception as e:
            return f"Error opening {app_name}: {e}"

    if app_name in _SEARCH_APPS:
        path = _resolve_search_app(app_name)
        if path:
            try:
                subprocess.Popen(path)
                return f"Opening {app_name}."
            except Exception as e:
                return f"Error opening {app_name}: {e}"
        return (
            f"I couldn't find {app_name} installed in any of the usual "
            f"locations on this machine."
        )

    discovered_path = _find_installed_app(app_name)
    if discovered_path:
        try:
            subprocess.Popen(discovered_path)
            return f"Opening {app_name}."
        except Exception as e:
            return f"Error opening {app_name}: {e}"

    return f"I don't know how to open '{app_name}'."


# Side-effect note (LIM-01 / LIM-03): Explorer windows cannot be idempotently tracked.
def open_folder(path: str) -> str:
    try:
        if not os.path.exists(path):
            return f"Folder not found: {path}"

        subprocess.Popen(f'explorer "{path}"')
        return f"Opening folder: {path}"

    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Direct Application Launch Control (Stage 5D)
# ---------------------------------------------------------------------------

def launch_app(target: str, args: str | list[str] | None = None) -> str:
    """
    Launch any application or executable with optional arguments across all local drives.
    """
    if not target or not isinstance(target, str) or not target.strip():
        return "Error: Target must be a non-empty string."

    target_str = target.strip()
    cmd = [target_str]
    if args:
        if isinstance(args, list):
            cmd.extend([str(a) for a in args])
        elif isinstance(args, str):
            cmd.append(args)

    from tools.permissions import is_catastrophic_command
    full_cmd = " ".join(cmd)
    is_cat, cat_reason = is_catastrophic_command(full_cmd)
    if is_cat:
        return f"Action blocked: {cat_reason}"

    try:
        subprocess.Popen(cmd, shell=False)
        return f"Successfully launched '{target_str}'."
    except FileNotFoundError:
        resolved = shutil.which(target_str) or _find_installed_app(target_str)
        if resolved:
            try:
                cmd[0] = resolved
                subprocess.Popen(cmd, shell=False)
                return f"Successfully launched '{target_str}' ({resolved})."
            except Exception as e:
                return f"Error launching '{target_str}': {e}"
        return f"Error: Application or executable '{target_str}' not found."
    except Exception as e:
        return f"Error launching '{target_str}': {e}"
