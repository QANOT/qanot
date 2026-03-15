"""Safe file operations — jail writes to workspace root.

OpenClaw-inspired: writeFileWithinRoot with symlink/traversal protection.
"""

from __future__ import annotations

import os
import uuid

# System directories that should NEVER be written to
_SYSTEM_DIRS = frozenset({
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/proc", "/sys", "/dev", "/var/run",
    "/System", "/Library",  # macOS
    "C:\\Windows", "C:\\Program Files",  # Windows
})


class SafeWriteError(Exception):
    """Raised when a file write is rejected for security reasons."""

    def __init__(self, reason: str, path: str):
        self.reason = reason
        self.path = path
        super().__init__(f"Write blocked ({reason}): {path}")


def _is_under(path: str, directory: str) -> bool:
    """Return True if *path* equals *directory* or is anywhere beneath it."""
    return path == directory or path.startswith(directory + os.sep)


def is_path_within_root(root: str, path: str) -> bool:
    """Check if a resolved path is inside the root directory.

    Handles symlinks, .., and other traversal attempts.
    """
    try:
        return _is_under(os.path.realpath(path), os.path.realpath(root))
    except (OSError, ValueError):
        return False


def validate_write_path(path: str, root: str | None = None) -> str | None:
    """Validate a file path for writing.

    Args:
        path: The path to validate.
        root: If set, path must resolve inside this directory (jail mode).

    Returns:
        Error message if blocked, None if allowed.
    """
    # Reject empty paths
    if not path or not path.strip():
        return "Empty path"

    # Reject null bytes — some C-backed fs code treats them as string terminators,
    # and they can be used for path truncation / injection attacks.
    if "\x00" in path:
        return "Null byte in path"

    resolved = os.path.realpath(path)

    # Block system directories
    for sys_dir in _SYSTEM_DIRS:
        if _is_under(resolved, sys_dir):
            return f"System directory blocked: {sys_dir}"

    # Block symlinks (prevent escape via symlink target)
    if os.path.islink(path):
        return "Symlink write blocked"

    # Jail mode: must be inside root
    if root and not is_path_within_root(root, path):
        return "Path outside workspace root"

    return None  # Allowed


def safe_write_file(path: str, content: str, root: str | None = None) -> str:
    """Write a file safely with validation and atomic write.

    Args:
        path: Target file path.
        content: File content to write.
        root: If set, jail writes to this directory.

    Returns:
        The resolved path that was written to.

    Raises:
        SafeWriteError: If the write is rejected.
    """
    error = validate_write_path(path, root)
    if error:
        raise SafeWriteError(error, path)

    resolved = os.path.realpath(path)

    # Create parent directories
    parent = os.path.dirname(resolved)
    basename = os.path.basename(resolved)
    os.makedirs(parent, exist_ok=True)

    # Atomic write: write to temp file, then rename
    temp_path = os.path.join(parent, f".{basename}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, resolved)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    # Post-write verification (TOCTOU protection)
    if root and not is_path_within_root(root, resolved):
        # Race condition: path escaped root during write
        try:
            os.unlink(resolved)
        except OSError:
            pass
        raise SafeWriteError("path-mismatch", path)

    return resolved
