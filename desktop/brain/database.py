"""
brain/database.py - Centralized SQLite database path resolver and connection factory.

Architecture:
1. Dynamic call-time path resolution:
   - Explicit db_path override (highest priority)
   - ORANGE_AI_USER_DATA_DIR environment variable override
   - Packaged frozen runtime: %LOCALAPPDATA%\\Orange AI\\memory.db (fallback ~/.orange_ai)
   - Development / source workspace: <workspace root>\\memory.db
2. Uniform SQLite connection configuration:
   - check_same_thread=False (safe for multi-threaded assistant/scheduler loops)
   - timeout=10.0
   - PRAGMA journal_mode=WAL;
   - PRAGMA busy_timeout=5000;
   - Configurable row_factory and foreign_keys
3. Explicit connection lifecycle and context managers for deterministic cleanup.
"""

from contextlib import contextmanager
import os
import sqlite3
import sys
import types
from typing import Optional, Any, Generator

DEFAULT_TIMEOUT = 10.0
DEFAULT_BUSY_TIMEOUT_MS = 5000
DATABASE_FILENAME = "memory.db"


def resolve_database_path(db_path: Optional[str] = None) -> str:
    """
    Authoritatively resolve the active SQLite database path at call time.

    Resolution Precedence:
    1. Explicit db_path argument (if provided and non-empty).
    2. Dynamic evaluation via get_user_data_dir() / "memory.db".
       - If ORANGE_AI_USER_DATA_DIR is set: <override dir>/memory.db
       - In frozen mode (PyInstaller): %LOCALAPPDATA%\\Orange AI\\memory.db
       - In development/source mode: <workspace root>/memory.db

    Returns an absolute, normalized path. Ensures the parent directory exists.
    """
    if db_path is not None and str(db_path).strip():
        resolved = os.path.abspath(os.path.normpath(str(db_path)))
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return resolved

    from brain.paths import get_user_data_dir
    base_dir = get_user_data_dir()
    resolved = os.path.abspath(os.path.normpath(os.path.join(base_dir, DATABASE_FILENAME)))
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return resolved


def get_database_path(db_path: Optional[str] = None) -> str:
    """Convenience alias for resolve_database_path."""
    return resolve_database_path(db_path)


def is_default_database_path(db_path: Optional[str]) -> bool:
    """
    Check whether a given db_path matches a standard authoritative default path
    (current dynamic default, development workspace memory.db, or AppData memory.db).
    Normalizes path casing, separators, and symbolic representations.
    """
    if db_path is None:
        return True
    try:
        norm_given = os.path.normcase(os.path.abspath(os.path.normpath(str(db_path))))
        # 1. Current dynamic default
        norm_current = os.path.normcase(os.path.abspath(os.path.normpath(resolve_database_path())))
        if norm_given == norm_current:
            return True
        # 2. Development workspace memory.db
        from brain.paths import get_project_root
        dev_default = os.path.normcase(os.path.abspath(os.path.normpath(os.path.join(get_project_root(), DATABASE_FILENAME))))
        if norm_given == dev_default:
            return True
        # 3. Packaged AppData memory.db
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            appdata_default = os.path.normcase(os.path.abspath(os.path.normpath(os.path.join(local_app_data, "Orange AI", DATABASE_FILENAME))))
            if norm_given == appdata_default:
                return True
        return False
    except Exception:
        return False


def get_connection_path(conn: Optional[sqlite3.Connection]) -> Optional[str]:
    """
    Inspect an open sqlite3.Connection and return its normalized filesystem path,
    or None if it's an in-memory, closed, or invalid database.
    """
    if conn is None:
        return None
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            if len(row) >= 3 and row[1] == "main" and row[2]:
                return os.path.abspath(os.path.normpath(str(row[2])))
    except Exception:
        pass
    return None


class DatabaseAwareModule(types.ModuleType):
    """
    Subclass of types.ModuleType that dynamically manages DB_PATH and _conn.
    Provides complete backward compatibility for legacy tests that access, assign,
    or patch DB_PATH and _conn, while guaranteeing dynamic call-time resolution
    in production and development environments.
    """
    @property
    def DB_PATH(self) -> str:
        override = getattr(self, "_override_db_path", None)
        if override is not None:
            return override
        if getattr(self, "_conn_is_external", False) and getattr(self, "_conn_path", None):
            return self._conn_path
        return resolve_database_path()

    @DB_PATH.setter
    def DB_PATH(self, value: Any) -> None:
        if value is None or is_default_database_path(value):
            self.__dict__["_override_db_path"] = None
        else:
            self.__dict__["_override_db_path"] = str(value)

    @DB_PATH.deleter
    def DB_PATH(self) -> None:
        self.__dict__["_override_db_path"] = None

    @property
    def _conn(self) -> Optional[sqlite3.Connection]:
        return self.__dict__.get("_conn", None)

    @_conn.setter
    def _conn(self, val: Optional[sqlite3.Connection]) -> None:
        self.__dict__["_conn"] = val
        if val is None:
            self.__dict__["_conn_path"] = None
            self.__dict__["_conn_is_external"] = False
        else:
            if getattr(self, "_internal_opening", False):
                self.__dict__["_conn_is_external"] = False
            else:
                self.__dict__["_conn_is_external"] = True
                self.__dict__["_conn_path"] = get_connection_path(val)

    @_conn.deleter
    def _conn(self) -> None:
        self.__dict__["_conn"] = None
        self.__dict__["_conn_path"] = None
        self.__dict__["_conn_is_external"] = False


def connect_database(
    db_path: Optional[str] = None,
    *,
    check_same_thread: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_wal: bool = True,
    foreign_keys: bool = False,
    row_factory: Any = None,
) -> sqlite3.Connection:
    """
    Open and configure a new SQLite connection with standardized policies.

    Policies applied:
    - Dynamic path resolution via resolve_database_path(db_path).
    - check_same_thread=check_same_thread (default False for multi-threaded safety).
    - timeout=timeout (default 10.0s).
    - PRAGMA journal_mode=WAL; (where applicable).
    - PRAGMA busy_timeout = busy_timeout_ms; (default 5000ms).
    - PRAGMA foreign_keys = ON; (if requested).
    - row_factory applied if provided (e.g. sqlite3.Row).
    """
    path = resolve_database_path(db_path)
    conn = sqlite3.connect(
        path,
        check_same_thread=check_same_thread,
        timeout=timeout,
    )

    if row_factory is not None:
        conn.row_factory = row_factory

    if enable_wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass

    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)};")
    except Exception:
        pass

    if foreign_keys:
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
        except Exception:
            pass

    return conn


def close_connection(conn: Optional[sqlite3.Connection]) -> None:
    """Safely commit and close a SQLite connection."""
    if conn is not None:
        try:
            conn.commit()
            conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


@contextmanager
def connection_context(
    db_path: Optional[str] = None,
    *,
    check_same_thread: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_wal: bool = True,
    foreign_keys: bool = False,
    row_factory: Any = None,
) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager providing a short-lived SQLite connection with guaranteed closure.
    """
    conn = connect_database(
        db_path,
        check_same_thread=check_same_thread,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
        enable_wal=enable_wal,
        foreign_keys=foreign_keys,
        row_factory=row_factory,
    )
    try:
        yield conn
    finally:
        close_connection(conn)


@contextmanager
def transaction(
    db_path: Optional[str] = None,
    *,
    check_same_thread: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_wal: bool = True,
    foreign_keys: bool = False,
    row_factory: Any = None,
) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager providing a transactional SQLite connection.
    Automatically commits on normal completion or rolls back on exception,
    then closes the connection deterministically.
    """
    conn = connect_database(
        db_path,
        check_same_thread=check_same_thread,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
        enable_wal=enable_wal,
        foreign_keys=foreign_keys,
        row_factory=row_factory,
    )
    try:
        with conn:
            yield conn
    finally:
        close_connection(conn)
