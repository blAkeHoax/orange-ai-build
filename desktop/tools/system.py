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
