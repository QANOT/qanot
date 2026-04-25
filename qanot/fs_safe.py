"""Safe file operations — jail writes to workspace root.

Pattern ported from OpenClaw's sandbox FS guards
(src/agents/sandbox/validate-sandbox-security.ts): hardcoded denylist of
host system dirs + per-user credential subpaths, plus filename patterns
covering keys/credentials. Symlinks resolved canonically before checks.
"""

from __future__ import annotations

import fnmatch
import os
import uuid

# System directories that should NEVER be read or written
_SYSTEM_DIRS = frozenset({
    "/etc", "/private/etc",
    "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/proc", "/sys", "/dev",
    "/var/run", "/private/var/run", "/run",
    "/var/lib",
    "/System", "/Library",  # macOS
    "C:\\Windows", "C:\\Program Files",  # Windows
})

# Files that should NEVER be written by the agent
# config.json contains api_key, bot_token — agent writing here can break the bot
_BLOCKED_FILENAMES = frozenset({
    "config.json",
})

# Per-user credential directories — block reads and writes anywhere they
# appear under a home dir (~, /Users/<u>, /home/<u>, /root)
_BLOCKED_HOME_SUBPATHS = frozenset({
    ".aws", ".cargo", ".config", ".docker", ".gnupg",
    ".kube", ".netrc", ".npm", ".pgpass", ".ssh",
    ".terraform.d",
})

# Filename patterns (fnmatch-style) that always indicate sensitive material
_BLOCKED_FILENAME_PATTERNS = (
    "id_*",          # SSH private keys (id_rsa, id_ed25519, id_ecdsa, id_dsa)
    "*.pem", "*.key", "*.pfx", "*.p12", "*.crt", "*.cer",
    "credentials",
    ".env", ".env.*",
    "kubeconfig",
    ".pgpass", ".netrc", ".npmrc",
    "*.kdbx",        # KeePass DB
    "*_rsa", "*_dsa", "*_ed25519", "*_ecdsa",  # alt SSH key naming
)


def _is_in_blocked_home_subpath(resolved: str) -> bool:
    """Return True if resolved path is inside a blocked home subdirectory.

    Walks the path components looking for `<home>/<blocked_dir>` shapes:
    - /Users/<user>/.ssh/...
    - /home/<user>/.aws/...
    - /root/.gnupg/...
    - $HOME/.docker/...
    """
    home = os.path.expanduser("~")
    if home and home != "~":
        for sub in _BLOCKED_HOME_SUBPATHS:
            blocked_root = os.path.join(home, sub)
            if resolved == blocked_root or resolved.startswith(blocked_root + os.sep):
                return True

    parts = resolved.split(os.sep)
    home_parents = {"Users", "home"}
    for i, part in enumerate(parts):
        # /Users/<u>/<sub>/... or /home/<u>/<sub>/...
        if part in home_parents and i + 2 < len(parts):
            if parts[i + 2] in _BLOCKED_HOME_SUBPATHS:
                return True
        # /root/<sub>/... (no user segment between /root and the subdir)
        if part == "root" and i + 1 < len(parts):
            if parts[i + 1] in _BLOCKED_HOME_SUBPATHS:
                return True
    return False


def _basename_blocked(basename: str) -> bool:
    """Match basename against credential-style patterns."""
    if not basename:
        return False
    for pat in _BLOCKED_FILENAME_PATTERNS:
        if fnmatch.fnmatchcase(basename, pat):
            return True
    return False


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


def resolve_workspace_path(path: str, workspace_dir: str) -> tuple[str, str | None]:
    """Resolve a path within a workspace directory, blocking traversal.

    Handles both relative and absolute paths:
    - Relative paths are resolved relative to workspace_dir
    - Absolute paths are allowed if they resolve within workspace_dir

    Returns:
        (resolved_path, error) — error is None if path is valid.
    """
    from pathlib import Path as _Path
    ws = _Path(workspace_dir).resolve()
    p = _Path(path)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (ws / path).resolve()
    try:
        resolved.relative_to(ws)
    except ValueError:
        return str(resolved), "Path resolves outside workspace directory"
    return str(resolved), None


def validate_read_path(path: str) -> str | None:
    """Validate a file path for reading.

    Blocks reads from system directories, credential dirs (~/.ssh, ~/.aws,
    etc.), and credential-style filenames (id_rsa, *.pem, .env, kubeconfig).

    Returns:
        Error message if blocked, None if allowed.
    """
    if not path or not path.strip():
        return "Empty path"
    if "\x00" in path:
        return "Null byte in path"
    resolved = os.path.realpath(path)
    for sys_dir in _SYSTEM_DIRS:
        if _is_under(resolved, sys_dir):
            return f"System directory blocked: {sys_dir}"
    if _is_in_blocked_home_subpath(resolved):
        return f"Credential directory blocked: {resolved}"
    basename = os.path.basename(resolved)
    if basename == "config.json":
        return "Sensitive file blocked: config.json"
    if _basename_blocked(basename):
        return f"Credential-style filename blocked: {basename}"
    return None


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

    # Block protected filenames (config.json etc.)
    basename = os.path.basename(resolved)
    if basename in _BLOCKED_FILENAMES:
        return f"Protected file — cannot be modified by agent: {basename}"

    if _basename_blocked(basename):
        return f"Credential-style filename blocked: {basename}"

    # Block system directories
    for sys_dir in _SYSTEM_DIRS:
        if _is_under(resolved, sys_dir):
            return f"System directory blocked: {sys_dir}"

    # Block writes anywhere under per-user credential dirs (~/.ssh, ~/.aws, …)
    if _is_in_blocked_home_subpath(resolved):
        return f"Credential directory blocked: {resolved}"

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
