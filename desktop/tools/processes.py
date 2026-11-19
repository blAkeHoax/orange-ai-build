import psutil


def list_processes() -> str:
    processes = []

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            processes.append(f"{proc.info['pid']} - {proc.info['name']}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return "\n".join(processes) if processes else "No processes found."


def is_running(process_name: str) -> bool:
    target = process_name.lower()

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"]
            if name and target in name.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return False


def _normalize(name: str) -> str:
    """Strip a trailing .exe and lowercase, so 'Code' and 'code.exe' match."""
    name = name.lower().strip()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def close_process(process_name: str) -> str:
    """
    Close all processes whose name EXACTLY matches process_name
    (ignoring case and a trailing .exe).

    The original version used substring matching and called .kill()
    (SIGKILL) on the first match found. That's risky: typing "close code"
    could match "vscode.exe" or "code_helper.exe" as well as "code.exe",
    and which one gets hit first is not guaranteed to be consistent
    between runs. This version:
      - only matches exact process names, so partial overlaps can't
        take out the wrong app
      - tries a graceful terminate() first, and only escalates to
        kill() if the process doesn't exit in time
      - closes every matching process (e.g. several browser tabs/procs
        sharing one name), not just the first one found
    """
    target = _normalize(process_name)
    matched = []

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info["name"]
            if name and _normalize(name) == target:
                matched.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not matched:
        return f"No process named '{process_name}' is running."

    closed_names = []
    for proc in matched:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gone, alive = psutil.wait_procs(matched, timeout=3)

    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for proc in gone + alive:
        closed_names.append(proc.info.get("name", process_name))

    return f"Closed {len(closed_names)} process(es) matching '{process_name}'."