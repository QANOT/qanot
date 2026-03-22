"""Plugin registry — install, remove, search, and lock file management.

Supports:
  - Install from git URL: qanot plugin install https://github.com/user/qanot-plugin-foo
  - Install from registry: qanot plugin install foo
  - Remove: qanot plugin remove foo
  - Search: qanot plugin search keyword
  - 3-tier resolution: bundled → ~/.qanot/plugins → workspace plugins/
  - Lock file: plugins.lock.json for version tracking
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default registry index URL (GitHub raw)
DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/QANOT/qanot-plugins/main/index.json"
)

# User-level plugins directory
USER_PLUGINS_DIR = Path.home() / ".qanot" / "plugins"

# Lock file name
LOCK_FILE = "plugins.lock.json"


@dataclass
class PluginEntry:
    """An entry in the registry index."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    author: str = ""
    git_url: str = ""
    homepage: str = ""
    tags: list[str] = field(default_factory=list)
    min_qanot_version: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> PluginEntry:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            version=data.get("version", "0.1.0"),
            author=data.get("author", ""),
            git_url=data.get("git_url", ""),
            homepage=data.get("homepage", ""),
            tags=data.get("tags", []),
            min_qanot_version=data.get("min_qanot_version", ""),
        )


@dataclass
class LockEntry:
    """A locked plugin installation record."""

    name: str
    version: str
    source: str  # "git", "registry", "local"
    source_url: str = ""
    installed_at: str = ""
    install_dir: str = ""
    sha256: str = ""  # integrity hash

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "source_url": self.source_url,
            "installed_at": self.installed_at,
            "install_dir": self.install_dir,
        }
        if self.sha256:
            d["sha256"] = self.sha256
        return d

    @classmethod
    def from_dict(cls, data: dict) -> LockEntry:
        return cls(**{k: data.get(k, "") for k in cls.__dataclass_fields__})


# ── Lock file management ──────────────────────────────────


def _lock_path(plugins_dir: Path) -> Path:
    return plugins_dir / LOCK_FILE


def read_lock(plugins_dir: Path) -> dict[str, LockEntry]:
    """Read lock file, return {name: LockEntry}."""
    path = _lock_path(plugins_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            name: LockEntry.from_dict(entry)
            for name, entry in raw.get("plugins", {}).items()
        }
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to read lock file: %s", e)
        return {}


def write_lock(plugins_dir: Path, entries: dict[str, LockEntry]) -> None:
    """Write lock file."""
    plugins_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "plugins": {name: entry.to_dict() for name, entry in entries.items()},
    }
    _lock_path(plugins_dir).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ── 3-tier plugin resolution ──────────────────────────────


def get_plugin_dirs() -> list[Path]:
    """Return plugin search directories in priority order (lowest → highest).

    1. Bundled (qanot package plugins/)
    2. User-level (~/.qanot/plugins/)
    3. Workspace (config plugins_dir)
    """
    from qanot.plugins.loader import BUILTIN_PLUGINS_DIR

    dirs = [BUILTIN_PLUGINS_DIR]
    if USER_PLUGINS_DIR.exists():
        dirs.append(USER_PLUGINS_DIR)
    return dirs


def find_plugin_3tier(
    name: str, workspace_plugins_dir: str | None = None
) -> Path | None:
    """Find a plugin across all 3 tiers. Workspace wins."""
    # Workspace (highest priority)
    if workspace_plugins_dir:
        ws_path = Path(workspace_plugins_dir) / name
        if ws_path.is_dir() and _is_valid_plugin(ws_path):
            return ws_path

    # User-level
    user_path = USER_PLUGINS_DIR / name
    if user_path.is_dir() and _is_valid_plugin(user_path):
        return user_path

    # Bundled (lowest priority)
    from qanot.plugins.loader import BUILTIN_PLUGINS_DIR

    builtin_path = BUILTIN_PLUGINS_DIR / name
    if builtin_path.is_dir() and _is_valid_plugin(builtin_path):
        return builtin_path

    return None


def _is_valid_plugin(path: Path) -> bool:
    """Check if a directory is a valid plugin (has plugin.py or __init__.py)."""
    return (path / "plugin.py").exists() or (path / "__init__.py").exists()


# ── Registry operations ───────────────────────────────────


def fetch_registry(registry_url: str = DEFAULT_REGISTRY_URL) -> list[PluginEntry]:
    """Fetch the plugin registry index."""
    try:
        req = urllib.request.Request(registry_url, headers={"User-Agent": "qanot"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        plugins = data.get("plugins", [])
        return [PluginEntry.from_dict(p) for p in plugins]
    except Exception as e:
        logger.warning("Failed to fetch registry: %s", e)
        return []


def search_registry(
    query: str, registry_url: str = DEFAULT_REGISTRY_URL
) -> list[PluginEntry]:
    """Search registry by keyword (matches name, description, tags)."""
    entries = fetch_registry(registry_url)
    if not entries:
        return []

    query_lower = query.lower()
    results = []
    for entry in entries:
        searchable = f"{entry.name} {entry.description} {' '.join(entry.tags)}".lower()
        if query_lower in searchable:
            results.append(entry)

    return results


# ── Install operations ────────────────────────────────────


def _is_git_url(source: str) -> bool:
    """Check if source looks like a git URL."""
    return (
        source.startswith("https://")
        or source.startswith("http://")
        or source.startswith("git@")
        or source.endswith(".git")
    )


def _is_http_url(source: str) -> bool:
    """Only allow HTTPS for git clones (block plain HTTP MITM)."""
    return source.startswith("http://") and not source.startswith("https://")


def install_plugin(
    source: str,
    plugins_dir: Path,
    registry_url: str = DEFAULT_REGISTRY_URL,
    user_level: bool = False,
    skip_security: bool = False,
) -> tuple[bool, str]:
    """Install a plugin from git URL or registry name.

    Args:
        source: Git URL or plugin name from registry
        plugins_dir: Workspace plugins directory
        registry_url: Registry index URL
        user_level: Install to ~/.qanot/plugins/ instead of workspace
        skip_security: Skip security scan (NOT recommended)

    Returns:
        (success, message)
    """
    target_dir = USER_PLUGINS_DIR if user_level else plugins_dir

    # Block plain HTTP (MITM risk)
    if _is_http_url(source):
        return False, "Refusing plain HTTP URL — use HTTPS to prevent MITM attacks"

    if _is_git_url(source):
        return _install_from_git(source, target_dir, skip_security=skip_security)

    # Try registry lookup
    entries = fetch_registry(registry_url)
    match = next((e for e in entries if e.name == source), None)

    if match and match.git_url:
        if _is_http_url(match.git_url):
            return False, f"Registry entry for '{source}' uses insecure HTTP URL"
        return _install_from_git(
            match.git_url, target_dir,
            registry_entry=match, skip_security=skip_security,
        )

    if match:
        return False, f"Plugin '{source}' found in registry but has no git_url"

    return False, (
        f"Plugin '{source}' not found. "
        f"Use a git URL or check 'qanot plugin search {source}'"
    )


def _install_from_git(
    git_url: str,
    target_dir: Path,
    registry_entry: PluginEntry | None = None,
    skip_security: bool = False,
) -> tuple[bool, str]:
    """Clone a git repo into the plugins directory with security checks."""
    from datetime import datetime
    from qanot.plugins.security import (
        sanitize_plugin_name,
        security_check,
        compute_plugin_hash,
        validate_dependencies,
    )

    # 1. Sanitize plugin name (prevent directory traversal)
    raw_name = git_url.rstrip("/").rsplit("/", 1)[-1]
    name, is_safe = sanitize_plugin_name(raw_name)
    if not is_safe:
        return False, f"Unsafe plugin name: '{raw_name}' (directory traversal or invalid chars)"

    plugin_path = target_dir / name

    if plugin_path.exists():
        return False, f"Plugin '{name}' already exists at {plugin_path}"

    target_dir.mkdir(parents=True, exist_ok=True)

    # 2. Clone (depth=1, no recursive submodules for safety)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--no-recurse-submodules",
             git_url, str(plugin_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            if plugin_path.exists():
                shutil.rmtree(plugin_path)
            return False, f"git clone failed: {result.stderr.strip()[:200]}"
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.TimeoutExpired:
        if plugin_path.exists():
            shutil.rmtree(plugin_path)
        return False, "git clone timed out (60s)"

    # 3. Remove .git directory
    git_dir = plugin_path / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)

    # 4. Validate plugin structure
    if not _is_valid_plugin(plugin_path):
        shutil.rmtree(plugin_path)
        return False, "Cloned repo has no plugin.py or __init__.py — not a valid Qanot plugin"

    # 5. Security scan (BEFORE any code execution)
    if not skip_security:
        is_safe_plugin, findings, summary = security_check(plugin_path)
        if not is_safe_plugin:
            # Format findings for user
            details = []
            for f in findings[:10]:  # Show top 10
                loc = f.get("file", "?")
                if "line" in f:
                    loc += f":{f['line']}"
                details.append(f"  [{f['severity']}] {loc}: {f['issue']}")
            detail_str = "\n".join(details)

            shutil.rmtree(plugin_path)
            return False, (
                f"SECURITY BLOCKED: {summary}\n{detail_str}\n\n"
                f"Use --force to skip security scan (NOT recommended)"
            )

    # 6. Read manifest and validate
    version = "0.1.0"
    manifest_path = plugin_path / "plugin.json"
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            version = m.get("version", version)

            # Validate manifest name
            manifest_name = m.get("name", "")
            if manifest_name:
                _, name_safe = sanitize_plugin_name(manifest_name)
                if name_safe and manifest_name != name:
                    new_path = target_dir / manifest_name
                    if not new_path.exists():
                        plugin_path.rename(new_path)
                        plugin_path = new_path
                        name = manifest_name
        except Exception:
            pass

    # 7. Safe dependency installation (allowlisted only)
    dep_msg = _install_deps_safe(plugin_path)

    # 8. Compute hash for integrity verification
    plugin_hash = compute_plugin_hash(plugin_path)

    # 9. Update lock file with hash
    lock = read_lock(target_dir)
    lock[name] = LockEntry(
        name=name,
        version=version,
        source="git",
        source_url=git_url,
        installed_at=datetime.now().isoformat(),
        install_dir=str(plugin_path),
    )
    # Store hash in the lock entry
    lock[name].sha256 = plugin_hash  # type: ignore[attr-defined]
    write_lock(target_dir, lock)

    msg = f"Plugin '{name}' v{version} installed to {plugin_path}"
    if dep_msg:
        msg += f"\n{dep_msg}"
    return True, msg


def _install_deps_safe(plugin_path: Path) -> str:
    """Install pip dependencies with allowlist validation.

    Only installs packages on the approved allowlist to prevent
    dependency confusion / supply chain attacks.
    """
    from qanot.plugins.security import validate_dependencies

    manifest_path = plugin_path / "plugin.json"
    if not manifest_path.exists():
        return ""

    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    deps = m.get("dependencies", [])
    if not deps or not isinstance(deps, list):
        return ""

    # Validate against allowlist
    allowed, blocked = validate_dependencies(deps)

    messages = []
    if blocked:
        messages.append(
            f"Blocked dependencies (not in allowlist): {', '.join(blocked)}"
        )
        logger.warning(
            "Plugin %s requests non-allowlisted deps: %s",
            plugin_path.name, blocked,
        )

    if not allowed:
        return "\n".join(messages) if messages else ""

    # Filter out already installed
    to_install = []
    for dep in allowed:
        pkg_name = dep.split(">=")[0].split("==")[0].split("<")[0].strip()
        try:
            __import__(pkg_name.replace("-", "_"))
        except ImportError:
            to_install.append(dep)

    if not to_install:
        if messages:
            return "\n".join(messages)
        return ""

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps", *to_install],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            messages.insert(0, f"Dependencies installed: {', '.join(to_install)}")
        else:
            messages.append(f"Warning: pip install failed for {', '.join(to_install)}")
    except Exception as e:
        messages.append(f"Warning: could not install dependencies: {e}")

    return "\n".join(messages)


# ── Remove operations ─────────────────────────────────────


def remove_plugin(name: str, plugins_dir: Path) -> tuple[bool, str]:
    """Remove an installed plugin.

    Searches workspace first, then user-level.
    Does NOT remove bundled plugins.
    """
    # Try workspace
    ws_path = plugins_dir / name
    if ws_path.is_dir():
        shutil.rmtree(ws_path)
        lock = read_lock(plugins_dir)
        lock.pop(name, None)
        write_lock(plugins_dir, lock)
        return True, f"Plugin '{name}' removed from {ws_path}"

    # Try user-level
    user_path = USER_PLUGINS_DIR / name
    if user_path.is_dir():
        shutil.rmtree(user_path)
        lock = read_lock(USER_PLUGINS_DIR)
        lock.pop(name, None)
        write_lock(USER_PLUGINS_DIR, lock)
        return True, f"Plugin '{name}' removed from {user_path}"

    # Check if it's bundled
    from qanot.plugins.loader import BUILTIN_PLUGINS_DIR

    if (BUILTIN_PLUGINS_DIR / name).is_dir():
        return False, f"Cannot remove bundled plugin '{name}'"

    return False, f"Plugin '{name}' not found"


# ── Info operations ────────────────────────────────────────


def plugin_info(name: str, plugins_dir: Path) -> dict[str, Any] | None:
    """Get detailed info about an installed plugin."""
    plugin_path = find_plugin_3tier(name, str(plugins_dir))
    if not plugin_path:
        return None

    info: dict[str, Any] = {
        "name": name,
        "path": str(plugin_path),
        "tier": _get_tier(plugin_path),
    }

    # Read manifest
    manifest_path = plugin_path / "plugin.json"
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            info.update(
                {
                    "version": m.get("version", "?"),
                    "description": m.get("description", ""),
                    "author": m.get("author", ""),
                    "dependencies": m.get("dependencies", []),
                    "required_config": m.get("required_config", []),
                }
            )
        except Exception:
            pass

    # Check lock file
    for search_dir in [plugins_dir, USER_PLUGINS_DIR]:
        lock = read_lock(search_dir)
        if name in lock:
            entry = lock[name]
            info["source"] = entry.source
            info["source_url"] = entry.source_url
            info["installed_at"] = entry.installed_at
            break

    # Count tools (inspect plugin.py)
    plugin_py = plugin_path / "plugin.py"
    if plugin_py.exists():
        content = plugin_py.read_text(encoding="utf-8")
        info["tool_count"] = content.count("@tool(")

    return info


def _get_tier(plugin_path: Path) -> str:
    """Determine which tier a plugin path belongs to."""
    from qanot.plugins.loader import BUILTIN_PLUGINS_DIR

    path_str = str(plugin_path.resolve())
    if path_str.startswith(str(BUILTIN_PLUGINS_DIR.resolve())):
        return "bundled"
    if path_str.startswith(str(USER_PLUGINS_DIR.resolve())):
        return "user"
    return "workspace"


# ── List all plugins across tiers ──────────────────────────


def list_all_plugins(workspace_plugins_dir: str | None = None) -> list[dict[str, Any]]:
    """List all discovered plugins across all tiers."""
    seen: dict[str, dict[str, Any]] = {}

    from qanot.plugins.loader import BUILTIN_PLUGINS_DIR

    search_dirs = [
        (BUILTIN_PLUGINS_DIR, "bundled"),
        (USER_PLUGINS_DIR, "user"),
    ]
    if workspace_plugins_dir:
        search_dirs.append((Path(workspace_plugins_dir), "workspace"))

    for search_dir, tier in search_dirs:
        if not search_dir.exists():
            continue
        for d in sorted(search_dir.iterdir()):
            if not d.is_dir() or not _is_valid_plugin(d):
                continue
            name = d.name
            entry: dict[str, Any] = {
                "name": name,
                "tier": tier,
                "path": str(d),
            }

            manifest_path = d / "plugin.json"
            if manifest_path.exists():
                try:
                    m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    entry["version"] = m.get("version", "?")
                    entry["description"] = m.get("description", "")
                    entry["author"] = m.get("author", "")
                except Exception:
                    pass

            # Higher-tier overrides lower
            seen[name] = entry

    return list(seen.values())
