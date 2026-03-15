"""Built-in tools — file ops, web_search, run_command, memory_search, session_status."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.agent import ToolRegistry
from qanot.context import ContextTracker
from qanot.memory import memory_search as _memory_search

if TYPE_CHECKING:
    from qanot.rag.indexer import MemoryIndexer

logger = logging.getLogger(__name__)

MAX_OUTPUT = 50_000
COMMAND_TIMEOUT = 120
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — Telegram document upload limit


# ── Exec security levels ──
# "open"     — only blocklist (dangerous patterns blocked)
# "cautious" — blocklist + cautious patterns need user approval
# "strict"   — only allowlist commands permitted

_CAUTIOUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Package management (can install malware)
    (re.compile(r"\bpip\s+install\b"), "pip install"),
    (re.compile(r"\bnpm\s+install\b"), "npm install"),
    (re.compile(r"\bapt(-get)?\s+install\b"), "apt install"),
    (re.compile(r"\bbrew\s+install\b"), "brew install"),
    # File deletion (non-recursive)
    (re.compile(r"\brm\s+"), "file deletion (rm)"),
    # Network operations
    (re.compile(r"\bcurl\b"), "network request (curl)"),
    (re.compile(r"\bwget\b"), "network request (wget)"),
    (re.compile(r"\bssh\b"), "SSH connection"),
    (re.compile(r"\bscp\b"), "file transfer (scp)"),
    # Git push/force operations
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\bgit\s+reset\b"), "git reset"),
    # Process management
    (re.compile(r"\bkill\b"), "process kill"),
    (re.compile(r"\bpkill\b"), "process kill (pkill)"),
    # System config
    (re.compile(r"\bsudo\b"), "sudo (elevated privileges)"),
    (re.compile(r"\bsystemctl\b"), "systemd service control"),
    (re.compile(r"\blaunchctl\b"), "launchd service control"),
    # Docker
    (re.compile(r"\bdocker\b"), "Docker command"),
    # Database
    (re.compile(r"\bpsql\b"), "PostgreSQL client"),
    (re.compile(r"\bmysql\b"), "MySQL client"),
    (re.compile(r"\bmongosh?\b"), "MongoDB client"),
]


_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- Destructive filesystem operations ---
    # Matches both -rf and -fr flag orderings in a single pattern
    (re.compile(r"\brm\s+.*-[a-zA-Z]*(?:r[a-zA-Z]*f|f[a-zA-Z]*r)[a-zA-Z]*\s+/(\s|$|\*|\"|')"), "recursive delete of root (/)"),
    (re.compile(r"\brm\s+.*-[a-zA-Z]*(?:r[a-zA-Z]*f|f[a-zA-Z]*r)[a-zA-Z]*\s+~(/|\s|$)"), "recursive delete of home directory"),
    (re.compile(r"\brm\s+.*-[a-zA-Z]*(?:r[a-zA-Z]*f|f[a-zA-Z]*r)[a-zA-Z]*\s+\*\s*$"), "recursive delete of all files (rm -rf *)"),
    (re.compile(r"\bmkfs\b"), "filesystem format (mkfs)"),
    (re.compile(r"\bdd\s+if="), "raw disk write (dd)"),
    (re.compile(r"\bshred\b"), "secure file destruction (shred)"),

    # --- System control ---
    (re.compile(r"\bshutdown\b"), "system shutdown"),
    (re.compile(r"\breboot\b"), "system reboot"),
    (re.compile(r"\bpoweroff\b"), "system poweroff"),
    (re.compile(r"\bhalt\b"), "system halt"),
    (re.compile(r"\binit\s+[06]\b"), "system init runlevel change"),

    # --- Permission escalation ---
    (re.compile(r"\bchmod\s+777\s+/\s*$"), "chmod 777 on root"),
    (re.compile(r"\bchown\s+root\b"), "ownership change to root"),
    (re.compile(r"\bpasswd\b"), "password modification"),

    # --- Network attack tools ---
    (re.compile(r"\bnmap\b"), "network scanner (nmap)"),
    (re.compile(r"\bnikto\b"), "web vulnerability scanner (nikto)"),
    (re.compile(r"\bsqlmap\b"), "SQL injection tool (sqlmap)"),
    (re.compile(r"\bhydra\b"), "brute-force tool (hydra)"),
    (re.compile(r"\bmetasploit\b|\bmsfconsole\b|\bmsfvenom\b"), "exploitation framework (metasploit)"),

    # --- Data exfiltration: curl/wget pipe to shell ---
    (re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b"), "curl piped to shell execution"),
    (re.compile(r"\bwget\b.*\|\s*(ba)?sh\b"), "wget piped to shell execution"),
    (re.compile(r"\beval\s+\$\(\s*curl\b"), "eval with curl (remote code execution)"),
    (re.compile(r"\beval\s+\$\(\s*wget\b"), "eval with wget (remote code execution)"),

    # --- Fork bombs ---
    (re.compile(r":\(\)\s*\{.*\|.*&\s*\}\s*;?\s*:"), "fork bomb"),

    # --- Disk fill ---
    (re.compile(r"\byes\s*>"), "disk fill via yes"),
    (re.compile(r"\bcat\s+/dev/(u?random|zero)\s*>"), "disk fill via /dev/random or /dev/zero"),
    (re.compile(r"\bfallocate\b.*-l\s*\d{3,}[GT]"), "massive file allocation"),

    # --- History/log tampering ---
    (re.compile(r"\bhistory\s+-c\b"), "shell history clearing"),
    (re.compile(r">\s*/var/log\b"), "log file truncation"),
]


def _is_dangerous_command(command: str) -> str | None:
    """Check if a shell command matches known dangerous patterns.

    Returns an error message string if dangerous, None if safe.
    """
    for pattern, description in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return description
    return None


def _needs_approval(command: str) -> str | None:
    """Check if command needs user approval in cautious mode.

    Returns description if approval needed, None if safe.
    """
    for pattern, description in _CAUTIOUS_PATTERNS:
        if pattern.search(command):
            return description
    return None


def _matches_allowlist(command: str, allowlist: list[str]) -> bool:
    """Check if command matches any pattern in the allowlist.

    Allowlist entries are prefix matches: "git" matches "git status", "git log", etc.
    """
    stripped = command.strip()
    return any(stripped.startswith(pattern) for pattern in allowlist)


def register_builtin_tools(
    registry: ToolRegistry,
    workspace_dir: str,
    context: ContextTracker,
    rag_indexer: "MemoryIndexer | None" = None,
    get_user_id: "callable | None" = None,
    get_cost_tracker: "callable | None" = None,
    exec_security: str = "open",
    exec_allowlist: list[str] | None = None,
    approval_callback: "callable | None" = None,
) -> None:
    """Register all built-in tools.

    exec_security: "open" | "cautious" | "strict"
    exec_allowlist: commands allowed in strict mode (prefix match)
    approval_callback: async fn(user_id, command, reason) -> bool (for inline buttons)
    """

    # ── read_file ──
    async def read_file(params: dict) -> str:
        path = params.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        full = _resolve_path(path, workspace_dir)
        try:
            content = Path(full).read_text(encoding="utf-8")
            if len(content) > MAX_OUTPUT:
                content = content[:MAX_OUTPUT] + f"\n... (truncated, {len(content)} total chars)"
            return content
        except FileNotFoundError:
            return json.dumps({"error": f"File not found: {path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="read_file",
        description="Faylni o'qish. Istalgan yo'ldan (absolyut yoki workspace ichida).",
        parameters={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Fayl yo'li (absolyut yoki workspace ichida)"},
            },
        },
        handler=read_file,
    )

    # ── write_file ──
    async def write_file(params: dict) -> str:
        from qanot.fs_safe import validate_write_path
        path = params.get("path", "")
        content = params.get("content", "")
        if not path:
            return json.dumps({"error": "path is required"})
        full = _resolve_path(path, workspace_dir)
        # Security: block writes to system directories
        error = validate_write_path(full)
        if error:
            return json.dumps({"error": f"Write blocked: {error}", "path": full})
        try:
            Path(full).parent.mkdir(parents=True, exist_ok=True)
            Path(full).write_text(content, encoding="utf-8")
            return json.dumps({"success": True, "path": full, "bytes": len(content.encode())})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="write_file",
        description="Faylga yozish yoki yangi fayl yaratish. Istalgan yo'lga.",
        parameters={
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": "Fayl yo'li (absolyut yoki relative)"},
                "content": {"type": "string", "description": "Fayl tarkibi"},
            },
        },
        handler=write_file,
    )

    # ── list_files ──
    async def list_files(params: dict) -> str:
        path = params.get("path", ".")
        full = _resolve_path(path, workspace_dir)
        try:
            entries = []
            for item in sorted(Path(full).iterdir()):
                kind = "dir" if item.is_dir() else "file"
                size = item.stat().st_size if item.is_file() else 0
                entries.append({"name": item.name, "type": kind, "size": size})
            return json.dumps(entries, indent=2)
        except FileNotFoundError:
            return json.dumps({"error": f"Directory not found: {path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="list_files",
        description="Papka ichidagi fayllar ro'yxati. Istalgan yo'ldan.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Papka yo'li (default: workspace)"},
            },
        },
        handler=list_files,
    )

    # ── run_command ──
    async def run_command(params: dict) -> str:
        command = params.get("command", "").strip()
        if not command:
            return json.dumps({"error": "command is required"})

        # Level 1: Always block dangerous commands (all modes)
        danger = _is_dangerous_command(command)
        if danger:
            return json.dumps({
                "error": f"Command blocked for safety: {danger}",
                "hint": "If this command is needed, the user must run it manually.",
            })

        # Level 2: Strict mode — only allowlist
        if exec_security == "strict":
            if not _matches_allowlist(command, exec_allowlist or []):
                return json.dumps({
                    "error": f"Command not in allowlist (strict mode)",
                    "hint": "Add to exec_allowlist in config.json, or set exec_security to 'cautious'.",
                    "command": command,
                })

        # Level 3: Cautious mode — approval for risky commands
        if exec_security == "cautious":
            reason = _needs_approval(command)
            if reason and not params.get("approved"):
                # Try inline button approval if callback available
                approval_required_response = json.dumps({
                    "needs_approval": True,
                    "reason": reason,
                    "command": command,
                    "instruction": "Ask the user to approve this command. If they say yes, call run_command again with approved=true.",
                })
                if approval_callback:
                    user_id = get_user_id() if get_user_id else ""
                    try:
                        approved = await approval_callback(user_id, command, reason)
                        if not approved:
                            return json.dumps({
                                "error": f"Foydalanuvchi rad etdi: {reason}",
                                "status": "denied",
                                "command": command,
                            })
                        # Approved via inline button — continue execution
                    except Exception as e:
                        logger.warning("Approval callback failed: %s", e)
                        # Fallback to text-based approval
                        return approval_required_response
                else:
                    return approval_required_response

        timeout = params.get("timeout", COMMAND_TIMEOUT)
        cwd = params.get("cwd", workspace_dir)

        logger.info("Executing command [%s]: %s", exec_security, command)

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n--- stderr ---\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n--- exit code: {result.returncode} ---"
            if len(output) > MAX_OUTPUT:
                output = output[:MAX_OUTPUT] + "\n... (truncated)"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return json.dumps({"error": f"Command timed out ({timeout}s)"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="run_command",
        description="Shell buyruq bajarish. Pipe, redirect ruxsat. Xavfli buyruqlar bloklangan. Ba'zi buyruqlar (pip install, curl, sudo, h.k.) foydalanuvchi ruxsatini talab qiladi — agar needs_approval qaytsa, foydalanuvchidan so'ra va approved=true bilan qayta chaqir.",
        parameters={
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {"type": "string", "description": "Shell buyruq (pipe, redirect, && ishlatsa bo'ladi)"},
                "timeout": {"type": "integer", "description": "Timeout sekundlarda (default: 120)"},
                "cwd": {"type": "string", "description": "Ishchi papka (default: workspace)"},
                "approved": {"type": "boolean", "description": "Foydalanuvchi ruxsat berganini tasdiqlash (cautious mode uchun)"},
            },
        },
        handler=run_command,
    )

    # ── web_search — registered separately in tools/web.py (Brave API) ──
    # Falls back to DuckDuckGo if brave_api_key is not configured (registered in main.py)

    # ── memory_search ──
    async def mem_search(params: dict) -> str:
        query = params.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})

        uid = get_user_id() if get_user_id else ""

        # Use RAG-powered search when available, fall back to substring search
        if rag_indexer is not None:
            try:
                results = await rag_indexer.search(query, user_id=uid or None)
                if results:
                    return json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("RAG search failed, falling back to substring: %s", e)

        results = _memory_search(query, workspace_dir, user_id=str(uid))
        if not results:
            return json.dumps({"message": "Hech narsa topilmadi", "query": query})
        return json.dumps(results, ensure_ascii=False, indent=2)

    registry.register(
        name="memory_search",
        description="Xotira fayllaridan qidirish (daily notes, MEMORY.md, SESSION-STATE.md).",
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Qidiruv so'rovi"},
            },
        },
        handler=mem_search,
    )

    # ── session_status ──
    async def session_status(params: dict) -> str:
        status = context.session_status()
        # Include per-user cost if available
        if get_cost_tracker and get_user_id:
            uid = get_user_id()
            if uid:
                tracker = get_cost_tracker()
                if tracker:
                    status["user_cost"] = tracker.get_user_stats(uid)
                    status["total_cost"] = tracker.get_total_cost()
        return json.dumps(status, indent=2)

    registry.register(
        name="session_status",
        description="Joriy sessiya holati — context %, token soni, xarajat.",
        parameters={"type": "object", "properties": {}},
        handler=session_status,
    )

    # ── cost_status ──
    async def cost_status(params: dict) -> str:
        if not get_cost_tracker:
            return json.dumps({"error": "Cost tracking not available"})
        tracker = get_cost_tracker()
        if not tracker:
            return json.dumps({"error": "Cost tracking not initialized"})
        uid = get_user_id() if get_user_id else ""
        user_id = params.get("user_id", uid)
        if user_id:
            stats = tracker.get_user_stats(str(user_id))
            stats["user_id"] = str(user_id)
            return json.dumps(stats, indent=2)
        return json.dumps(tracker.get_all_stats(), indent=2)

    registry.register(
        name="cost_status",
        description="Token va xarajat statistikasi — har bir foydalanuvchi uchun alohida.",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Foydalanuvchi ID (default: joriy user)"},
            },
        },
        handler=cost_status,
    )


    # ── send_file ──
    async def send_file(params: dict) -> str:
        """Send a file from workspace to the user via Telegram."""
        path = params.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        full = _resolve_path(path, workspace_dir)
        if not os.path.isfile(full):
            return json.dumps({"error": f"File not found: {path}"})
        # Size check — Telegram limit 50MB
        size = os.path.getsize(full)
        if size > 50 * 1024 * 1024:
            return json.dumps({"error": f"File too large: {size / 1024 / 1024:.1f}MB (max 50MB)"})
        # Push to pending files queue (telegram adapter will send it)
        from qanot.agent import Agent
        if Agent._instance:
            user_id = get_user_id() if get_user_id else ""
            Agent._instance._pending_files.setdefault(user_id, []).append(full)
        return json.dumps({"success": True, "path": full, "size": size})

    registry.register(
        name="send_file",
        description="Foydalanuvchiga fayl yuborish (Telegram orqali). Workspace yoki absolyut yo'l.",
        parameters={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Fayl yo'li (SOUL.md, memory/2026-03-14.md, va h.k.)"},
            },
        },
        handler=send_file,
    )


def _resolve_path(path: str, workspace_dir: str) -> str:
    """Resolve a path — absolute paths used as-is, relative resolved from workspace."""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(workspace_dir, path))
