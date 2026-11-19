"""
gui/single_instance.py - Windows-native single-instance application mutex.

Stage 3.7E.4: Multi-instance GUI protection.
Prevents multiple concurrent Orange AI Desktop processes from running competing
event-delivery pipelines and presenting duplicate notifications to the user.

Guarantees:
- Windows-native named mutex via kernel32.CreateMutexW.
- OS-level auto-release on process termination or unhandled crash (no orphan locks).
- Fallback file-based mutual exclusion for non-Windows or environments without ctypes.
- User-facing alert when another instance is already running.
- Safe release during normal application shutdown.
- Does not interfere with CLI or standalone operation.
"""

import os
import sys
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Windows API Constants
ERROR_ALREADY_EXISTS = 183
DEFAULT_MUTEX_NAME = "Local\\OrangeAIDesktopSingleInstanceMutex"


class SingleInstanceGuard:
    """
    Manages single-instance mutex ownership for Orange AI Desktop.
    
    Guarantees:
    - Attempting to acquire when another instance is running returns False.
    - Safe release in finally or shutdown handlers.
    - Kernel auto-cleanup on abnormal termination.
    """

    def __init__(self, mutex_name: str = DEFAULT_MUTEX_NAME, lock_file_path: Optional[str] = None):
        self.mutex_name = mutex_name
        self.handle = None
        self._is_acquired = False
        self._file_lock_fd: Optional[int] = None
        self._file_lock_path: Optional[str] = lock_file_path

    @property
    def is_held(self) -> bool:
        """Whether the mutex or lock file is currently acquired by this process."""
        return self._is_acquired

    def acquire(self, force_file_lock: bool = False) -> bool:
        """
        Attempt to acquire single-instance ownership.
        Returns True if this is the only active instance; False if another instance exists.
        """
        # Allow explicit override via environment variable (e.g. for specialized testing)
        if os.environ.get("ORANGE_AI_ALLOW_MULTIPLE_INSTANCES") == "1":
            self._is_acquired = True
            return True

        if sys.platform == "win32" and not force_file_lock:
            try:
                import ctypes
                # CreateMutexW returns a handle; GetLastError returns ERROR_ALREADY_EXISTS if already created
                handle = ctypes.windll.kernel32.CreateMutexW(None, True, self.mutex_name)
                last_error = ctypes.windll.kernel32.GetLastError()
                if last_error == ERROR_ALREADY_EXISTS:
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                    self.handle = None
                    self._is_acquired = False
                    return False
                self.handle = handle
                self._is_acquired = True
                return True
            except Exception as e:
                logger.warning("[SINGLE INSTANCE] Windows CreateMutexW failed: %s; falling back to file lock.", e)

        # Fallback to file-based lock
        return self._acquire_file_lock()

    def _acquire_file_lock(self) -> bool:
        """Cross-platform fallback lock file."""
        from brain.paths import get_project_root
        if not self._file_lock_path:
            self._file_lock_path = os.path.join(get_project_root(), ".gui.instance.lock")
        try:
            self._file_lock_fd = os.open(self._file_lock_path, os.O_RDWR | os.O_CREAT, 0o666)
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(self._file_lock_fd, msvcrt.LK_NBLCK, 1)
                except (OSError, IOError):
                    os.close(self._file_lock_fd)
                    self._file_lock_fd = None
                    self._is_acquired = False
                    return False
            else:
                import fcntl
                try:
                    fcntl.flock(self._file_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, IOError):
                    os.close(self._file_lock_fd)
                    self._file_lock_fd = None
                    self._is_acquired = False
                    return False

            self._is_acquired = True
            return True
        except Exception as e:
            logger.warning("[SINGLE INSTANCE] File lock fallback failed: %s", e)
            return True

    def release(self) -> None:
        """Release the mutex or lock file."""
        if self.handle:
            try:
                import ctypes
                ctypes.windll.kernel32.ReleaseMutex(self.handle)
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None

        if self._file_lock_fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        os.lseek(self._file_lock_fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._file_lock_fd, msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._file_lock_fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                os.close(self._file_lock_fd)
            except Exception:
                pass
            self._file_lock_fd = None

        self._is_acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def check_single_instance(
    show_dialog: bool = True,
    mutex_name: str = DEFAULT_MUTEX_NAME,
    lock_file_path: Optional[str] = None,
    dialog_fn: Optional[Any] = None,
) -> Tuple[bool, Optional[SingleInstanceGuard]]:
    """
    Check if another instance of Orange AI Desktop is active.
    
    Returns:
        (True, guard): This instance has acquired sole ownership; guard must be kept alive.
        (False, None): Another instance is running; caller should exit.
    """
    guard = SingleInstanceGuard(mutex_name=mutex_name, lock_file_path=lock_file_path)
    if not guard.acquire():
        logger.warning("[GUI] Another instance of Orange AI Desktop is already running. Exiting.")
        if show_dialog:
            if dialog_fn:
                try:
                    dialog_fn(
                        "Orange AI Desktop - Already Running",
                        "Orange AI Desktop is already running.\n\nPlease switch to the active window.",
                    )
                except Exception:
                    pass
            elif sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0,
                        "Orange AI Desktop is already running.\n\nPlease switch to the active window.",
                        "Orange AI Desktop",
                        0x40 | 0x0,  # MB_ICONINFORMATION | MB_OK
                    )
                except Exception:
                    pass
        return False, None
    return True, guard
