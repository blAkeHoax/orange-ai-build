import os
import sys
import tempfile
import shutil
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Union, Tuple, Iterable
import psutil
from brain.logger import logger

# The original hardcoded "F:\\" as the only place find_file would ever
# look, which only works on whatever machine had that exact drive layout.
# This makes it an explicit, overridable default instead of a silent
# assumption. Set ORANGE_SEARCH_PATH in your environment, or just pass
# search_path explicitly, to point this at wherever your files actually live.
DEFAULT_SEARCH_PATH = os.environ.get("ORANGE_SEARCH_PATH", os.path.expanduser("~"))

# Hardened read/edit limits and policy constraints
DEFAULT_MAX_LINES = 500
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB limit
SUPPORTED_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


class FileReadResult(str):
    """A string subclass representing the result of read_file.

    Inherits from str so it can be used directly as a string everywhere,
    preserving backward compatibility with callers expecting string returns.
    Carries structured pagination and file metadata attributes.
    """

    path: str
    start_line: int
    lines_returned: int
    has_more: bool
    next_start_line: Optional[int]
    encoding: str
    raw_content: str

    def __new__(
        cls,
        content: str,
        path: str = "",
        start_line: int = 1,
        lines_returned: int = 0,
        has_more: bool = False,
        next_start_line: Optional[int] = None,
        encoding: str = "utf-8",
        raw_content: str = "",
    ):
        instance = super().__new__(cls, content)
        instance.path = str(path)
        instance.start_line = int(start_line)
        instance.lines_returned = int(lines_returned)
        instance.has_more = bool(has_more)
        instance.next_start_line = int(next_start_line) if next_start_line is not None else None
        instance.encoding = str(encoding)
        instance.raw_content = str(raw_content)
        return instance

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "lines_returned": self.lines_returned,
            "has_more": self.has_more,
            "next_start_line": self.next_start_line,
            "encoding": self.encoding,
            "content": str(self),
            "raw_content": self.raw_content,
        }


class FileEditResult(str):
    """A string subclass representing the result of edit_file.

    Inherits from str so it can be used directly as a string everywhere,
    while providing structured metadata attributes (.path, .replacements,
    .backup_path, .encoding, .to_dict()).
    """

    path: str
    replacements: int
    backup_path: str
    encoding: str

    def __new__(
        cls,
        content: str,
        path: str = "",
        replacements: int = 0,
        backup_path: str = "",
        encoding: str = "utf-8",
    ):
        instance = super().__new__(cls, content)
        instance.path = str(path)
        instance.replacements = int(replacements)
        instance.backup_path = str(backup_path)
        instance.encoding = str(encoding)
        return instance

    def to_dict(self) -> dict:
        return {
            "status": "success",
            "path": self.path,
            "replacements": self.replacements,
            "backup_path": self.backup_path,
            "encoding": self.encoding,
            "message": str(self),
        }


def calculate_file_sha256(file_path: Union[str, Path]) -> str:
    """Calculate SHA-256 hex digest of a file in 64KB chunks."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_approved_roots() -> List[Path]:
    """Return canonical paths for all approved workspace and user data roots.

    Includes:
    1. The project root (workspace repository).
    2. The writable per-user data directory (%LOCALAPPDATA%\\Orange AI).
    3. The default user search path (home directory / ORANGE_SEARCH_PATH).
    4. The system temp directory (for controlled temporary testing).
    5. Any additional roots specified in the ORANGE_ALLOWED_ROOTS environment variable.
    """
    roots: List[Path] = []

    # 1. Project root & user data dir from brain.paths
    try:
        from brain.paths import get_project_root, get_user_data_dir
        roots.append(Path(get_project_root()))
        roots.append(Path(get_user_data_dir()))
    except Exception:
        roots.append(Path(__file__).resolve().parent.parent)

    # Explicit repository root
    roots.append(Path(__file__).resolve().parent.parent)

    # 2. LocalAppData Orange AI if on Windows
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Orange AI")

    # 3. Default search path & user home
    roots.append(Path(DEFAULT_SEARCH_PATH))
    roots.append(Path.home())

    # 4. Temp directory
    roots.append(Path(tempfile.gettempdir()))

    # 5. Environment variable ORANGE_ALLOWED_ROOTS
    env_roots = os.environ.get("ORANGE_ALLOWED_ROOTS", "")
    if env_roots:
        for r in env_roots.split(os.pathsep):
            r = r.strip()
            if r:
                roots.append(Path(r))

    # Deduplicate and canonicalize
    unique_roots: List[Path] = []
    for r in roots:
        try:
            resolved = r.resolve()
            if resolved not in unique_roots:
                unique_roots.append(resolved)
        except Exception:
            pass
    return unique_roots


def is_path_within_roots(
    target: Union[str, Path],
    roots: Iterable[Union[str, Path]],
) -> bool:
    """Check if the resolved target path is within any of the approved roots.

    Handles cross-drive paths, relative paths, and symlink escapes securely.
    """
    try:
        resolved_target = Path(target).resolve()
    except Exception:
        return False

    for root in roots:
        try:
            resolved_root = Path(root).resolve()
            if resolved_target == resolved_root:
                return True
            try:
                if resolved_target.is_relative_to(resolved_root):
                    return True
            except (ValueError, AttributeError):
                try:
                    resolved_target.relative_to(resolved_root)
                    return True
                except ValueError:
                    pass
        except Exception:
            continue
    return False


def is_binary_file(file_path: Union[str, Path], sample_size: int = 4096) -> bool:
    """Return True if the file appears to be binary based on null bytes or control char ratio."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(sample_size)
        if not chunk:
            return False
        if b"\x00" in chunk:
            return True
        # Check ratio of non-text control characters
        # Allow standard control chars: \a (7), \b (8), \t (9), \n (10), \v (11), \f (12), \r (13), \x1b (27)
        text_chars = bytes({7, 8, 9, 10, 11, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})
        non_text = chunk.translate(None, text_chars)
        return len(non_text) / len(chunk) > 0.30
    except Exception:
        return False


def get_designated_backup_dir(
    target_file: Path,
    custom_backup_dir: Optional[Union[str, Path]] = None,
    allowed_roots: Optional[List[Union[str, Path]]] = None,
) -> Path:
    """Determine the approved backup directory for file edits."""
    if custom_backup_dir:
        b_dir = Path(custom_backup_dir).resolve()
    elif "ORANGE_BACKUP_DIR" in os.environ:
        b_dir = Path(os.environ["ORANGE_BACKUP_DIR"]).resolve()
    else:
        try:
            from brain.paths import get_user_data_dir
            candidate = Path(get_user_data_dir()).resolve() / "backups"
            if allowed_roots is not None and not is_path_within_roots(candidate, allowed_roots):
                candidate = target_file.parent / ".backups"
            b_dir = candidate
        except Exception:
            b_dir = target_file.parent / ".backups"

    b_dir.mkdir(parents=True, exist_ok=True)
    return b_dir


def _read_paged_lines(
    file_path: Path,
    start_line: int,
    max_lines: int,
) -> Tuple[List[str], bool, Optional[int], str]:
    """Stream and extract lines between start_line and start_line + max_lines - 1.

    Attempts encoding fallback cascade (utf-8 -> utf-8-sig -> cp1252 -> latin-1).
    Streams lines with generator to strictly bound memory consumption.
    Returns: (lines, has_more, next_start_line, encoding_used)
    """
    # Check for UTF-8 BOM
    has_bom = False
    try:
        with open(file_path, "rb") as bf:
            header = bf.read(3)
            has_bom = (header == b"\xef\xbb\xbf")
    except Exception:
        pass

    if has_bom:
        encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    else:
        encodings = SUPPORTED_ENCODINGS

    target_end_line = start_line + max_lines - 1

    for enc in encodings:
        try:
            lines: List[str] = []
            has_more = False
            next_start_line = None
            with open(file_path, "r", encoding=enc, errors="strict") as f:
                for idx, line in enumerate(f, start=1):
                    if idx < start_line:
                        continue
                    if idx <= target_end_line:
                        lines.append(line)
                    else:
                        has_more = True
                        next_start_line = idx
                        break
            return lines, has_more, next_start_line, enc
        except UnicodeDecodeError:
            continue
        except Exception:
            raise

    # Fallback with replacement if all strict decodes failed
    lines = []
    has_more = False
    next_start_line = None
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx < start_line:
                continue
            if idx <= target_end_line:
                lines.append(line)
            else:
                has_more = True
                next_start_line = idx
                break
    return lines, has_more, next_start_line, "utf-8"


def _capture_snapshot_and_decode(file_path: Path) -> Tuple[bytes, str, str, str]:
    """Capture raw bytes in a single read, compute baseline SHA-256, and decode text.

    Returns: (raw_bytes, orig_sha, content_text, encoding_used)
    """
    with open(file_path, "rb") as bf:
        raw_bytes = bf.read()

    orig_sha = hashlib.sha256(raw_bytes).hexdigest()

    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    if has_bom:
        encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
    else:
        encodings = SUPPORTED_ENCODINGS

    for enc in encodings:
        try:
            content = raw_bytes.decode(enc)
            return raw_bytes, orig_sha, content, enc
        except UnicodeDecodeError:
            continue

    content = raw_bytes.decode("latin-1", errors="replace")
    return raw_bytes, orig_sha, content, "latin-1"


def _detect_encoding_and_read_text(file_path: Path) -> Tuple[str, str]:
    """Read the full text of file_path and return (text, encoding_used)."""
    _, _, content, enc = _capture_snapshot_and_decode(file_path)
    return content, enc


def list_files(path: str = ".") -> str:
    try:
        entries = os.listdir(path)
        if not entries:
            return "Folder is empty."
        return "\n".join(entries)
    except Exception as e:
        return str(e)


def read_file(
    path: str,
    start_line: int = 1,
    max_lines: int = DEFAULT_MAX_LINES,
    max_file_size: int = MAX_FILE_SIZE_BYTES,
    allowed_roots: Optional[List[Union[str, Path]]] = None,
) -> Union[FileReadResult, str]:
    """Safe, bounded, paginated text-file reader.

    Provides:
    - Bounded line pagination with start_line and max_lines.
    - Path traversal and workspace boundary verification.
    - Binary file detection and rejection.
    - Multi-encoding fallback cascade (utf-8, utf-8-sig, cp1252, latin-1).
    - File size limit protection (default 10 MB).
    - Strict memory ceiling via line streaming.
    - Backward-compatible FileReadResult string return.
    """
    if not path:
        return "Error: No file path provided."

    try:
        # Normalize pagination parameters
        try:
            start_line = int(start_line)
        except (ValueError, TypeError):
            start_line = 1

        try:
            max_lines = int(max_lines)
        except (ValueError, TypeError):
            max_lines = DEFAULT_MAX_LINES

        if start_line < 1:
            start_line = 1
        if max_lines < 1:
            max_lines = 1
        if max_lines > 5000:
            max_lines = 5000

        # Resolve target and verify security boundaries
        target_path = Path(path)
        roots_to_check = allowed_roots if allowed_roots is not None else get_approved_roots()

        if not is_path_within_roots(target_path, roots_to_check):
            return f"Error: Access denied. Path '{path}' escapes approved workspace roots."

        resolved_target = target_path.resolve()

        if not resolved_target.exists():
            return f"Error: [Errno 2] No such file or directory: '{path}'"

        if resolved_target.is_dir():
            return f"Error: Path '{path}' is a directory, not a file."

        # Verify file size limit
        try:
            file_size = resolved_target.stat().st_size
        except Exception as e:
            return f"Error: Unable to inspect file size: {e}"

        if file_size > max_file_size:
            if max_file_size >= 1024 * 1024:
                mb_limit = max_file_size / (1024 * 1024)
                limit_str = f"{int(mb_limit)} MB" if mb_limit.is_integer() else f"{mb_limit:.2f} MB"
            else:
                limit_str = f"{max_file_size} bytes"
            return (
                f"Error: File '{path}' exceeds the {limit_str} policy limit "
                f"({file_size} bytes > {max_file_size} bytes)."
            )

        # Binary file check
        if is_binary_file(resolved_target):
            return f"Error: Binary file detected at '{path}'. read_file only supports text files."

        # Handle 0-byte empty file
        if file_size == 0:
            return FileReadResult(
                "",
                path=str(resolved_target),
                start_line=start_line,
                lines_returned=0,
                has_more=False,
                next_start_line=None,
                encoding="utf-8",
                raw_content="",
            )

        # Stream requested lines with encoding fallback cascade
        lines, has_more, next_start_line, enc = _read_paged_lines(
            resolved_target, start_line, max_lines
        )

        raw_content = "".join(lines)
        lines_returned = len(lines)

        # Format string value and pagination metadata
        if not has_more and start_line == 1:
            # Full file fit into single requested page: return exact raw content
            content_str = raw_content
        else:
            # Paginated read: append structured metadata footer
            actual_end_line = start_line + lines_returned - 1 if lines_returned > 0 else start_line
            if has_more:
                footer = (
                    f"\n\n[File: {resolved_target} | Lines {start_line}-{actual_end_line} "
                    f"({lines_returned} lines returned) | More lines available: next start_line={next_start_line} | "
                    f"Encoding: {enc}]"
                )
            else:
                footer = (
                    f"\n\n[File: {resolved_target} | Lines {start_line}-{actual_end_line} "
                    f"({lines_returned} lines returned) | End of file reached | "
                    f"Encoding: {enc}]"
                )

            content_str = (raw_content + footer) if raw_content else footer.lstrip("\n")

        return FileReadResult(
            content_str,
            path=str(resolved_target),
            start_line=start_line,
            lines_returned=lines_returned,
            has_more=has_more,
            next_start_line=next_start_line,
            encoding=enc,
            raw_content=raw_content,
        )

    except Exception as e:
        return f"Error: {e}"


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    backup_dir: Optional[Union[str, Path]] = None,
    allowed_roots: Optional[List[Union[str, Path]]] = None,
    max_file_size: int = MAX_FILE_SIZE_BYTES,
) -> Union[FileEditResult, str]:
    """Perform exact-match text replacement in a file with verified backup and atomic write.

    Provides:
    - Exact character-by-character match validation.
    - Zero match rejection.
    - Multiple match rejection when replace_all=False.
    - Timestamped backup with integrity verification before modification.
    - Staging temporary write with flush and fsync.
    - Content validation before atomic replacement.
    - Atomic file replacement (os.replace).
    - Post-replacement validation with automatic rollback from backup on failure.
    - Original line endings (\\r\\n, \\n, \\r) and encoding preservation.
    - Workspace root escape and traversal protection.
    """
    if not path:
        return "Error: No file path provided."

    if old_string is None or not isinstance(old_string, str) or old_string == "":
        return "Error: Target string 'old_string' cannot be empty."

    if new_string is None:
        new_string = ""
    elif not isinstance(new_string, str):
        new_string = str(new_string)

    if isinstance(replace_all, str):
        replace_all = replace_all.strip().lower() in ("true", "1", "yes")
    else:
        replace_all = bool(replace_all)

    try:
        # Resolve path and verify workspace boundaries
        target_path = Path(path)
        roots_to_check = allowed_roots if allowed_roots is not None else get_approved_roots()

        if not is_path_within_roots(target_path, roots_to_check):
            return f"Error: Access denied. Path '{path}' escapes approved workspace roots."

        resolved_target = target_path.resolve()

        if not resolved_target.exists():
            return f"Error: [Errno 2] No such file or directory: '{path}'"

        if resolved_target.is_dir():
            return f"Error: Path '{path}' is a directory, not a file."

        # Verify file size limit
        try:
            file_size = resolved_target.stat().st_size
        except Exception as e:
            return f"Error: Unable to inspect file size: {e}"

        if file_size > max_file_size:
            if max_file_size >= 1024 * 1024:
                mb_limit = max_file_size / (1024 * 1024)
                limit_str = f"{int(mb_limit)} MB" if mb_limit.is_integer() else f"{mb_limit:.2f} MB"
            else:
                limit_str = f"{max_file_size} bytes"
            return (
                f"Error: File '{path}' exceeds the {limit_str} policy limit "
                f"({file_size} bytes > {max_file_size} bytes)."
            )

        # Binary file check
        if is_binary_file(resolved_target):
            return f"Error: Binary file detected at '{path}'. edit_file only supports text files."

        # Capture original file snapshot bytes and establish baseline hash
        raw_bytes, orig_sha, content, enc = _capture_snapshot_and_decode(resolved_target)

        # Exact match validation
        match_count = content.count(old_string)
        if match_count == 0:
            return f"Error: Target string not found in '{path}'."

        if match_count > 1 and not replace_all:
            return (
                f"Error: Found {match_count} occurrences of target string in '{path}'. "
                f"Set replace_all=True to replace all occurrences or provide a more specific target string."
            )

        # Compute replacement in memory exclusively from the captured snapshot
        if match_count == 1:
            new_content = content.replace(old_string, new_string, 1)
            replacements = 1
        else:
            new_content = content.replace(old_string, new_string)
            replacements = match_count

        # Create verified timestamped backup from original target
        b_dir = get_designated_backup_dir(resolved_target, custom_backup_dir=backup_dir, allowed_roots=roots_to_check)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{resolved_target.name}.bak_{timestamp}_{uuid.uuid4().hex[:6]}"
        backup_path = b_dir / backup_filename

        try:
            shutil.copy2(resolved_target, backup_path)
        except Exception as e:
            return f"Error: Failed to create backup file for '{path}': {e}"

        if not backup_path.exists():
            return f"Error: Backup file was not created: '{backup_path}'"

        # Verify backup integrity against the original snapshot baseline hash
        bak_sha = calculate_file_sha256(backup_path)
        if bak_sha != orig_sha:
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except Exception:
                    pass
            logger.warning(
                "[FILE_EDIT] Target '%s' was modified concurrently before backup creation "
                "(backup SHA %s != snapshot SHA %s). Aborting edit to prevent stale overwrite.",
                resolved_target,
                bak_sha,
                orig_sha,
            )
            return (
                f"Error: File '{path}' was modified concurrently before backup creation. "
                f"Edit aborted to prevent stale overwrite."
            )

        # Stage modified content to temporary file in the same directory
        temp_file = resolved_target.parent / f"{resolved_target.name}.edit_tmp_{uuid.uuid4().hex[:8]}"
        try:
            with open(temp_file, "w", encoding=enc, newline="") as tf:
                tf.write(new_content)
                tf.flush()
                try:
                    os.fsync(tf.fileno())
                except Exception:
                    pass

            # Validate temporary file content
            with open(temp_file, "r", encoding=enc, newline="") as tf:
                validated_text = tf.read()

            if validated_text != new_content:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
                return f"Error: Validation mismatch in temporary staging file for '{path}'."

            # Concurrency safety check immediately before atomic replacement
            current_target_sha = calculate_file_sha256(resolved_target)
            if current_target_sha != orig_sha:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
                logger.warning(
                    "[FILE_EDIT] Concurrent modification detected on '%s' before atomic replacement "
                    "(current SHA %s != snapshot SHA %s). Aborting edit to prevent stale write.",
                    resolved_target,
                    current_target_sha,
                    orig_sha,
                )
                return (
                    f"Error: File '{path}' was modified concurrently by another process "
                    f"between read and replacement. Edit aborted to prevent data loss."
                )

            # Atomic replacement
            os.replace(temp_file, resolved_target)

            # Preserve file mode from backup if possible
            try:
                shutil.copymode(backup_path, resolved_target)
            except Exception:
                pass

        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            logger.error("[FILE_EDIT] Atomic replacement failed for '%s': %s", resolved_target, e)
            return f"Error: Failed to atomically write modifications to '{path}': {e}"

        # Post-replacement verification
        try:
            with open(resolved_target, "r", encoding=enc, newline="") as vf:
                final_text = vf.read()

            if final_text != new_content:
                # Rollback from backup
                try:
                    shutil.copy2(backup_path, resolved_target)
                except Exception:
                    pass
                logger.error("[FILE_EDIT] Post-replacement verification failed for '%s'. Rolled back to backup.", resolved_target)
                return f"Error: Post-replacement verification failed for '{path}'. Rolled back to backup."
        except Exception as e:
            try:
                shutil.copy2(backup_path, resolved_target)
            except Exception:
                pass
            logger.error("[FILE_EDIT] Post-replacement read failed for '%s': %s. Rolled back.", resolved_target, e)
            return f"Error: Post-replacement read failed for '{path}': {e}. Rolled back to backup."

        # Audit logging
        logger.info(
            "[FILE_EDIT] Successfully edited '%s': %d replacement(s) made. Backup: '%s'. Encoding: %s",
            resolved_target,
            replacements,
            backup_path,
            enc,
        )

        result_msg = (
            f"Successfully replaced {replacements} occurrence(s) in '{resolved_target}'.\n"
            f"Backup created at: '{backup_path}'"
        )
        return FileEditResult(
            result_msg,
            path=str(resolved_target),
            replacements=replacements,
            backup_path=str(backup_path),
            encoding=enc,
        )

    except Exception as e:
        logger.error("[FILE_EDIT] Unexpected error editing '%s': %s", path, e)
        return f"Error: {e}"


def find_file(filename: str, search_path: str = DEFAULT_SEARCH_PATH) -> str:
    try:
        if not os.path.exists(search_path):
            return (
                f"Search path '{search_path}' doesn't exist on this machine. "
                f"Set ORANGE_SEARCH_PATH to a real folder."
            )

        matches = []
        target = filename.lower()

        for root, dirs, files in os.walk(search_path):
            for file in files:
                if file.lower() == target:
                    matches.append(os.path.join(root, file))

        if matches:
            return "\n".join(matches)

        return f"No file named '{filename}' found under {search_path}."

    except Exception as e:
        return str(e)


def _system_search_roots() -> list[str]:
    """Return mounted filesystem roots that can be searched on this PC."""
    roots = []
    for partition in psutil.disk_partitions(all=False):
        if "cdrom" in partition.opts.lower():
            continue
        if os.path.isdir(partition.mountpoint):
            roots.append(partition.mountpoint)
    return roots or [os.path.abspath(os.sep)]


def find_folder(
    folder_name: str,
    search_paths: list[str] | None = None,
    max_results: int = 20,
) -> str:
    """Find exact folder-name matches across mounted filesystems.

    This is read-only.  Permission-denied folders are skipped, and the result
    cap prevents a broad name from flooding the terminal.
    """
    target = folder_name.strip().lower()
    if not target:
        return "Tell me the folder name to search for."

    matches = []
    for search_path in search_paths or _system_search_roots():
        if not os.path.isdir(search_path):
            continue

        def on_error(_error):
            # Protected or unreadable directories are expected in a full-disk
            # scan; continue searching other locations.
            return None

        for root, directories, _files in os.walk(search_path, onerror=on_error):
            for directory in directories:
                if directory.lower() == target:
                    matches.append(os.path.join(root, directory))
                    if len(matches) >= max_results:
                        return (
                            f"Found at least {max_results} folders named '{folder_name}':\n"
                            + "\n".join(matches)
                            + "\nRefine the name to see fewer results."
                        )

    if matches:
        return f"Found {len(matches)} folder(s) named '{folder_name}':\n" + "\n".join(matches)
    return f"No folder named '{folder_name}' was found on mounted drives."


# ---------------------------------------------------------------------------
# Full Windows Filesystem Control (Stage 5D)
# ---------------------------------------------------------------------------

def write_file(path: str, content: str) -> str:
    """
    Create or overwrite a file with the given content across any local drive.
    Parent directories are automatically created if they do not exist.
    """
    if not path or not isinstance(path, str) or not path.strip():
        return "Error: Path must be a non-empty string."

    target_path = Path(path.strip()).resolve()

    from tools.permissions import is_catastrophic_command
    is_cat, cat_reason = is_catastrophic_command(str(target_path))
    if is_cat:
        return f"Action blocked: {cat_reason}"

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content if content is not None else "")
        return f"Successfully wrote {len(content or '')} characters to '{target_path}'."
    except Exception as e:
        return f"Error writing file '{target_path}': {e}"


def append_file(path: str, content: str) -> str:
    """
    Append text content to a file across any local drive.
    Creates the file and parent directories if they do not exist.
    """
    if not path or not isinstance(path, str) or not path.strip():
        return "Error: Path must be a non-empty string."

    target_path = Path(path.strip()).resolve()

    from tools.permissions import is_catastrophic_command
    is_cat, cat_reason = is_catastrophic_command(str(target_path))
    if is_cat:
        return f"Action blocked: {cat_reason}"

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(content if content is not None else "")
        return f"Successfully appended {len(content or '')} characters to '{target_path}'."
    except Exception as e:
        return f"Error appending to file '{target_path}': {e}"


def delete_file(path: str) -> str:
    """
    Delete a file or directory across any local drive.
    Protected by the catastrophic safety guard.
    """
    if not path or not isinstance(path, str) or not path.strip():
        return "Error: Path must be a non-empty string."

    target_path = Path(path.strip()).resolve()

    from tools.permissions import is_catastrophic_command
    is_cat, cat_reason = is_catastrophic_command(str(target_path))
    if is_cat:
        return f"Action blocked: {cat_reason}"

    if not target_path.exists():
        return f"Error: Path does not exist: '{target_path}'."

    try:
        if target_path.is_dir():
            shutil.rmtree(target_path)
            return f"Successfully deleted directory '{target_path}'."
        else:
            target_path.unlink()
            return f"Successfully deleted file '{target_path}'."
    except Exception as e:
        return f"Error deleting '{target_path}': {e}"


def move_path(src: str, dst: str) -> str:
    """
    Move or rename a file or directory from src to dst.
    """
    if not src or not dst:
        return "Error: Both src and dst must be non-empty strings."

    src_path = Path(src.strip()).resolve()
    dst_path = Path(dst.strip()).resolve()

    from tools.permissions import is_catastrophic_command
    for p in (src_path, dst_path):
        is_cat, cat_reason = is_catastrophic_command(str(p))
        if is_cat:
            return f"Action blocked: {cat_reason}"

    if not src_path.exists():
        return f"Error: Source path does not exist: '{src_path}'."

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
        return f"Successfully moved '{src_path}' to '{dst_path}'."
    except Exception as e:
        return f"Error moving '{src_path}' to '{dst_path}': {e}"


def create_directory(path: str) -> str:
    """
    Create a new directory and any missing parent directories across any local drive.
    """
    if not path or not isinstance(path, str) or not path.strip():
        return "Error: Path must be a non-empty string."

    target_path = Path(path.strip()).resolve()

    from tools.permissions import is_catastrophic_command
    is_cat, cat_reason = is_catastrophic_command(str(target_path))
    if is_cat:
        return f"Action blocked: {cat_reason}"

    try:
        target_path.mkdir(parents=True, exist_ok=True)
        return f"Successfully created directory '{target_path}'."
    except Exception as e:
        return f"Error creating directory '{target_path}': {e}"

