from datetime import datetime
import ctypes
import psutil


def is_running_as_admin() -> bool:
    """Return whether this Windows process currently has elevated rights."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        # Returning False is the safe fallback if the Windows UAC API cannot
        # be queried.
        return False


def get_time() -> str:
    """Get the current time."""
    return datetime.now().strftime("%I:%M %p")


def get_date() -> str:
    """Get today's date."""
    return datetime.now().strftime("%d %B %Y")


def get_cpu_usage() -> str:
    """Get current CPU usage as a percentage."""
    return f"{psutil.cpu_percent(interval=1)}%"


def get_ram_usage() -> str:
    """Get current RAM usage as a percentage."""
    memory = psutil.virtual_memory()
    return f"{memory.percent}%"


def get_disk_usage() -> str:
    """Get disk usage: used space, free space, and percentage used."""
    disk = psutil.disk_usage("/")
    return (
        f"Used: {round(disk.used / (1024**3), 2)} GB | "
        f"Free: {round(disk.free / (1024**3), 2)} GB | "
        f"Usage: {disk.percent}%"
    )


def get_battery() -> str:
    """Get battery percentage and charging status."""
    battery = psutil.sensors_battery()

    if battery is None:
        return "Battery information unavailable."

    status = "Charging" if battery.power_plugged else "Not Charging"
    return f"{battery.percent}% ({status})"


# ---------------------------------------------------------------------------
# Windows Clipboard Control (Stage 5D)
# ---------------------------------------------------------------------------

def get_clipboard() -> str:
    """Read text content from the Windows clipboard."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_wchar_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

        CF_UNICODETEXT = 13
        if not user32.OpenClipboard(None):
            return _get_clipboard_fallback()

        try:
            h_data = user32.GetClipboardData(CF_UNICODETEXT)
            if not h_data:
                return ""
            data_ptr = kernel32.GlobalLock(h_data)
            text = str(data_ptr) if data_ptr else ""
            kernel32.GlobalUnlock(h_data)
            return text
        finally:
            user32.CloseClipboard()
    except Exception:
        return _get_clipboard_fallback()


def _get_clipboard_fallback() -> str:
    """Fallback clipboard reader via PowerShell."""
    try:
        import subprocess
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.stdout.rstrip("\r\n")
    except Exception:
        return ""


def set_clipboard(text: str) -> str:
    """Set text content onto the Windows clipboard."""
    val = text if text is not None else ""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        if not user32.OpenClipboard(None):
            return _set_clipboard_fallback(val)

        try:
            user32.EmptyClipboard()
            data_bytes = (val + "\0").encode("utf-16le")
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data_bytes))
            if not h_mem:
                return _set_clipboard_fallback(val)

            p_mem = kernel32.GlobalLock(h_mem)
            if p_mem:
                ctypes.memmove(p_mem, data_bytes, len(data_bytes))
                kernel32.GlobalUnlock(h_mem)
                user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                return f"Successfully set clipboard ({len(val)} characters)."
            else:
                return _set_clipboard_fallback(val)
        finally:
            user32.CloseClipboard()
    except Exception:
        return _set_clipboard_fallback(val)


def _set_clipboard_fallback(text: str) -> str:
    """Fallback clipboard writer via PowerShell."""
    try:
        import subprocess
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
            input=text,
            text=True,
            capture_output=True,
            timeout=5,
        )
        return f"Successfully set clipboard ({len(text)} characters)."
    except Exception as e:
        return f"Error setting clipboard: {e}"
