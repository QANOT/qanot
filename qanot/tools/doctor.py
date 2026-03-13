"""Doctor diagnostics tool — comprehensive system health check."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.agent import ToolRegistry

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.context import ContextTracker

logger = logging.getLogger(__name__)

# Thresholds
SESSION_STATE_WARN_KB = 100
DISK_WARN_MB = 100


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory in bytes. Returns 0 on error."""
    if not path.is_dir():
        return 0
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def _file_size_str(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def _check_config(config: "Config") -> dict:
    """Check configuration health."""
    details: list[str] = []
    status = "ok"

    # bot_token
    if config.bot_token:
        details.append("bot_token: present")
    else:
        details.append("bot_token: MISSING")
        status = "error"

    # API keys
    if config.providers:
        for pc in config.providers:
            if pc.api_key:
                details.append(f"provider '{pc.name}' ({pc.provider}): api_key present")
            else:
                details.append(f"provider '{pc.name}' ({pc.provider}): api_key MISSING")
                status = "error"
    elif config.api_key:
        details.append(f"provider '{config.provider}': api_key present")
    else:
        details.append(f"provider '{config.provider}': api_key MISSING")
        status = "error"

    # workspace_dir
    ws = Path(config.workspace_dir)
    if ws.is_dir() and os.access(ws, os.W_OK):
        details.append(f"workspace_dir: exists, writable ({config.workspace_dir})")
    elif ws.is_dir():
        details.append(f"workspace_dir: exists but NOT writable")
        status = "error"
    else:
        details.append(f"workspace_dir: MISSING ({config.workspace_dir})")
        status = "error"

    # sessions_dir
    sd = Path(config.sessions_dir)
    if sd.is_dir() and os.access(sd, os.W_OK):
        details.append(f"sessions_dir: exists, writable")
    elif sd.is_dir():
        details.append(f"sessions_dir: exists but NOT writable")
        status = "error"
    else:
        details.append(f"sessions_dir: MISSING ({config.sessions_dir})")
        status = "warning" if status == "ok" else status

    # Required workspace files
    required_files = ["SOUL.md", "TOOLS.md", "IDENTITY.md"]
    for fname in required_files:
        fpath = ws / fname
        if fpath.is_file():
            details.append(f"{fname}: present")
        else:
            details.append(f"{fname}: MISSING")
            status = "warning" if status == "ok" else status

    return {"status": status, "details": "; ".join(details)}


def _check_memory(config: "Config") -> tuple[dict, list[str]]:
    """Check memory system health. Returns (check_result, warnings)."""
    ws = Path(config.workspace_dir)
    details: list[str] = []
    warnings: list[str] = []
    status = "ok"

    # MEMORY.md
    mem_path = ws / "MEMORY.md"
    if mem_path.is_file():
        try:
            mem_path.read_text(encoding="utf-8")
            details.append(f"MEMORY.md: readable ({_file_size_str(mem_path.stat().st_size)})")
        except Exception as e:
            details.append(f"MEMORY.md: read error ({e})")
            status = "warning"
    else:
        details.append("MEMORY.md: not found")
        status = "warning"

    # SESSION-STATE.md size
    state_path = ws / "SESSION-STATE.md"
    if state_path.is_file():
        state_size = state_path.stat().st_size
        details.append(f"SESSION-STATE.md: {_file_size_str(state_size)}")
        if state_size > SESSION_STATE_WARN_KB * 1024:
            warnings.append(
                f"SESSION-STATE.md is {_file_size_str(state_size)}, consider compaction"
            )
            status = "warning"
    else:
        details.append("SESSION-STATE.md: not found")

    # Daily notes count (last 30 days)
    memory_dir = ws / "memory"
    daily_count = 0
    if memory_dir.is_dir():
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for f in memory_dir.iterdir():
            if f.suffix == ".md" and f.stem not in ("working-buffer",):
                try:
                    # Daily notes are named YYYY-MM-DD.md
                    note_date = datetime.strptime(f.stem, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if note_date >= cutoff:
                        daily_count += 1
                except ValueError:
                    pass
        mem_dir_size = _dir_size(memory_dir)
        details.append(
            f"daily notes (30d): {daily_count}; memory/ size: {_file_size_str(mem_dir_size)}"
        )
    else:
        details.append("memory/ directory: not found")

    return {"status": status, "details": "; ".join(details)}, warnings


def _check_context(context: "ContextTracker") -> dict:
    """Check context tracker health."""
    status_data = context.session_status()
    pct = status_data["context_percent"]
    status = "ok"
    if pct > 95:
        status = "error"
    elif pct > 80:
        status = "warning"

    details = (
        f"usage: {pct}%; "
        f"tokens: {status_data['total_tokens']}; "
        f"buffer_active: {status_data['buffer_active']}; "
        f"compaction_mode: active" if context.needs_compaction() else
        f"usage: {pct}%; "
        f"tokens: {status_data['total_tokens']}; "
        f"buffer_active: {status_data['buffer_active']}; "
        f"compaction_mode: idle"
    )

    return {"status": status, "details": details}


def _check_provider(config: "Config") -> dict:
    """Check provider configuration."""
    details: list[str] = []

    if config.providers:
        details.append(f"mode: multi-provider ({len(config.providers)} in failover chain)")
        for pc in config.providers:
            details.append(f"  {pc.name}: {pc.provider}/{pc.model}")
    else:
        details.append(f"mode: single; provider: {config.provider}; model: {config.model}")

    return {"status": "ok", "details": "; ".join(details)}


def _check_rag(config: "Config") -> dict:
    """Check RAG system health."""
    details: list[str] = []
    status = "ok"

    details.append(f"enabled: {config.rag_enabled}")

    if not config.rag_enabled:
        return {"status": "ok", "details": "RAG disabled"}

    db_path = Path(config.workspace_dir) / "rag.db"
    if db_path.is_file():
        db_size = db_path.stat().st_size
        details.append(f"rag.db: {_file_size_str(db_size)}")

        # Check FTS5 and cache count
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Check FTS5
            fts5_available = False
            try:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
                )
                fts5_available = cursor.fetchone() is not None
            except sqlite3.OperationalError:
                pass
            details.append(f"FTS5: {'available' if fts5_available else 'not available'}")

            # Embedding cache count
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM embedding_cache"
                )
                cache_count = cursor.fetchone()[0]
                details.append(f"embedding cache: {cache_count} entries")
            except sqlite3.OperationalError:
                details.append("embedding cache: table not found")

            conn.close()
        except Exception as e:
            details.append(f"db inspection error: {e}")
            status = "warning"
    else:
        details.append("rag.db: not found")
        status = "warning"

    return {"status": status, "details": "; ".join(details)}


def _check_sessions(config: "Config") -> dict:
    """Check session files health."""
    details: list[str] = []
    status = "ok"

    sd = Path(config.sessions_dir)
    if not sd.is_dir():
        return {"status": "warning", "details": "sessions directory not found"}

    # Directory size
    dir_size = _dir_size(sd)
    details.append(f"sessions dir size: {_file_size_str(dir_size)}")

    # Session files in last 7 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recent_count = 0
    latest_ts: float = 0.0
    try:
        for f in sd.iterdir():
            if f.suffix == ".jsonl" and f.is_file():
                mtime = f.stat().st_mtime
                if mtime > cutoff.timestamp():
                    recent_count += 1
                if mtime > latest_ts:
                    latest_ts = mtime
    except OSError as e:
        details.append(f"read error: {e}")
        status = "warning"

    details.append(f"session files (7d): {recent_count}")

    if latest_ts > 0:
        latest_dt = datetime.fromtimestamp(latest_ts, tz=timezone.utc)
        details.append(f"latest: {latest_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    else:
        details.append("latest: no session files found")

    return {"status": status, "details": "; ".join(details)}


def _check_disk(config: "Config") -> tuple[dict, list[str]]:
    """Check disk usage. Returns (check_result, warnings)."""
    details: list[str] = []
    warnings: list[str] = []
    status = "ok"

    ws = Path(config.workspace_dir)
    ws_size = _dir_size(ws)
    details.append(f"workspace size: {_file_size_str(ws_size)}")

    # Available disk space
    try:
        stat = os.statvfs(str(ws))
        free_bytes = stat.f_bavail * stat.f_frsize
        details.append(f"available disk: {_file_size_str(free_bytes)}")
        if free_bytes < DISK_WARN_MB * 1024 * 1024:
            warnings.append(
                f"Low disk space: {_file_size_str(free_bytes)} remaining"
            )
            status = "warning"
    except (OSError, AttributeError):
        # statvfs not available on all platforms
        details.append("available disk: unable to check")

    return {"status": status, "details": "; ".join(details)}, warnings


def register_doctor_tool(
    registry: ToolRegistry,
    config: "Config",
    context: "ContextTracker",
) -> None:
    """Register the doctor diagnostics tool."""

    async def doctor(params: dict) -> str:
        """Run comprehensive system health diagnostics."""
        warnings: list[str] = []
        checks: dict[str, dict] = {}

        # 1. Config
        checks["config"] = _check_config(config)

        # 2. Memory
        mem_check, mem_warnings = _check_memory(config)
        checks["memory"] = mem_check
        warnings.extend(mem_warnings)

        # 3. Context
        checks["context"] = _check_context(context)

        # 4. Provider
        checks["provider"] = _check_provider(config)

        # 5. RAG
        checks["rag"] = _check_rag(config)

        # 6. Sessions
        checks["sessions"] = _check_sessions(config)

        # 7. Disk
        disk_check, disk_warnings = _check_disk(config)
        checks["disk"] = disk_check
        warnings.extend(disk_warnings)

        # Determine overall status
        has_error = any(c["status"] == "error" for c in checks.values())
        has_warning = any(c["status"] == "warning" for c in checks.values()) or warnings

        if has_error:
            overall = "error"
        elif has_warning:
            overall = "warning"
        else:
            overall = "healthy"

        report = {
            "status": overall,
            "checks": checks,
            "warnings": warnings,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return json.dumps(report, indent=2, ensure_ascii=False)

    registry.register(
        name="doctor",
        description=(
            "Tizim diagnostikasi — config, memory, context, provider, RAG, "
            "sessions, disk holatini tekshiradi. Muammolarni aniqlaydi."
        ),
        parameters={"type": "object", "properties": {}},
        handler=doctor,
    )
