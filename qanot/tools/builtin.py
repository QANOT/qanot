"""Built-in tools — file ops, web_search, run_command, memory_search, session_status."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.registry import ToolRegistry
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

    # --- Encoded payloads piped to shell (defeats argv inspection by design) ---
    (re.compile(r"\bbase64\s+(-d|--decode|--d)\b[^|;]*\|\s*(ba)?sh\b"),
     "base64-decoded shell execution"),
    (re.compile(r"\bxxd\s+-r\b[^|;]*\|\s*(ba)?sh\b"), "xxd-decoded shell execution"),
    (re.compile(r"\b(echo|printf)\b[^|;]*\|\s*base64\s+(-d|--decode)\b[^|;]*\|\s*(ba)?sh\b"),
     "echo+base64 piped to shell"),
    (re.compile(r"\$\([^)]*\bbase64\s+(-d|--decode|--d)\b"),
     "base64 decode in command substitution"),
    (re.compile(r"\$\([^)]*\bxxd\s+-r\b"),
     "xxd decode in command substitution"),
]


def _first_match(command: str, patterns: list[tuple[re.Pattern[str], str]]) -> str | None:
    """Return the description of the first pattern that matches command, or None."""
    for pattern, description in patterns:
        if pattern.search(command):
            return description
    return None


# Interpreters whose inline-eval flags (-c / -e / -E) execute attacker-supplied
# code. Ported pattern from OpenClaw's argv-aware safety model: regex on shell
# strings is unreliable, so we tokenise with shlex and inspect argv shape.
_INLINE_EVAL_INTERPRETERS = frozenset({
    "python", "python2", "python3",
    "ruby", "perl", "php", "node", "deno",
    "bash", "sh", "zsh", "fish", "ksh", "csh", "tcsh", "dash",
    "lua", "tclsh",
})

# argv tokens treated as shell operators when traversing a command's tokens.
_SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", "&", "|&"})


def _detect_inline_eval(command: str) -> str | None:
    """Return interpreter basename if any chained segment uses inline-eval.

    Walks tokenised argv chains separated by shell operators. For each
    segment, if argv[0] is an interpreter and any later arg is `-c`, `-e`,
    `-E`, `-c=...`, `-e=...`, or the concat form `-cFOO` / `-eFOO`, we treat
    that segment as inline-eval. Catches the bypasses regex-on-shell-strings
    misses (`python3 -c "..."`, `bash -c "$(... | base64 -d)"`,
    `git log && python -c "evil"`).
    """
    # Use shlex.shlex with explicit punctuation so `;` `|` `&` `&&` `||` come
    # back as separate operator tokens — `shlex.split` glues `;` to adjacent
    # words, which would hide chained interpreter eval after a benign first
    # segment (e.g. `ls; bash -c 'evil'`).
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="|&;")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        # Unparseable / unbalanced quotes — let the regex layer + cautious
        # default-deny handle it.
        return None

    def _check_segment(argv: list[str]) -> str | None:
        if not argv:
            return None
        binary = os.path.basename(argv[0])
        if binary not in _INLINE_EVAL_INTERPRETERS:
            return None
        for arg in argv[1:]:
            if arg in {"-c", "-e", "-E", "--command", "--eval"}:
                return binary
            if arg.startswith(("-c=", "-e=", "-E=", "--command=", "--eval=")):
                return binary
            # Concatenated form: `-cFOO` or `-eFOO`
            if len(arg) > 2 and arg[0] == "-" and arg[1] in ("c", "e", "E"):
                return binary
        return None

    argv: list[str] = []
    for tok in tokens:
        # `punctuation_chars` returns runs as a single token, so `&&` / `||`
        # arrive intact alongside single `;` `|` `&`.
        if tok in _SHELL_OPERATORS:
            hit = _check_segment(argv)
            if hit:
                return hit
            argv = []
        else:
            argv.append(tok)
    return _check_segment(argv)


def _is_dangerous_command(command: str) -> str | None:
    """Return description if command matches a dangerous pattern, else None."""
    return _first_match(command, _DANGEROUS_PATTERNS)


def _needs_approval(command: str) -> str | None:
    """Return description if command needs user approval in cautious mode, else None."""
    pattern_hit = _first_match(command, _CAUTIOUS_PATTERNS)
    if pattern_hit:
        return pattern_hit
    interpreter = _detect_inline_eval(command)
    if interpreter:
        return f"interpreter inline-eval ({interpreter} -c/-e)"
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
    get_user_id: Callable[[], str | None] | None = None,
    get_cost_tracker: Callable | None = None,
    exec_security: str = "open",
    exec_allowlist: list[str] | None = None,
    approval_callback: Callable | None = None,
    get_bot: Callable | None = None,
    get_chat_id: Callable[[], int | None] | None = None,
) -> None:
    """Register all built-in tools.

    exec_security: "open" | "cautious" | "strict"
    exec_allowlist: commands allowed in strict mode (prefix match)
    approval_callback: async fn(user_id, command, reason) -> bool (for inline buttons)
    """

    # ── read_file ──
    async def read_file(params: dict) -> str:
        from qanot.fs_safe import validate_read_path
        path = params.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        try:
            full = _resolve_path(path, workspace_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        error = validate_read_path(full)
        if error:
            return json.dumps({"error": f"Read blocked: {error}", "path": full})
        try:
            fsize = Path(full).stat().st_size
            if fsize > MAX_OUTPUT * 10:
                return json.dumps({"error": f"File too large ({fsize} bytes). Use run_command with head/tail."})
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
        description="Read a file from any path (absolute or within workspace).",
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
        try:
            full = _resolve_path(path, workspace_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        # Security + atomic write via safe_write_file
        try:
            from qanot.fs_safe import safe_write_file
            written_path = safe_write_file(full, content, root=workspace_dir)
            return json.dumps({"success": True, "path": written_path, "bytes": len(content.encode())})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="write_file",
        description="Write content to a file or create a new file at any path.",
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
        from qanot.fs_safe import validate_read_path
        path = params.get("path", ".")
        try:
            full = _resolve_path(path, workspace_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        error = validate_read_path(full)
        if error:
            return json.dumps({"error": f"Read blocked: {error}", "path": full})
        try:
            entries = []
            for item in sorted(Path(full).iterdir()):
                kind = "dir" if item.is_dir() else "file"
                size = item.stat().st_size if not item.is_dir() else 0
                entries.append({"name": item.name, "type": kind, "size": size})
            return json.dumps(entries, indent=2)
        except FileNotFoundError:
            return json.dumps({"error": f"Directory not found: {path}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="list_files",
        description="List files and directories in a folder. Any path supported.",
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

        try:
            timeout = max(1, min(int(params.get("timeout", COMMAND_TIMEOUT)), COMMAND_TIMEOUT))
        except (TypeError, ValueError):
            timeout = COMMAND_TIMEOUT
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
        description="Execute a shell command. Pipes, redirects allowed. Dangerous commands are blocked. Some commands (pip install, curl, sudo, etc.) require user approval — if needs_approval is returned, ask the user and call again with approved=true.",
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
        description="Search memory files (daily notes, MEMORY.md, SESSION-STATE.md).",
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
        description="Current session status — context %, token count, cost.",
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
        description="Token and cost statistics — per-user breakdown.",
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
        from qanot.fs_safe import validate_read_path
        path = params.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        try:
            full = _resolve_path(path, workspace_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        error = validate_read_path(full)
        if error:
            return json.dumps({"error": f"Read blocked: {error}", "path": full})
        if not os.path.isfile(full):
            return json.dumps({"error": f"File not found: {path}"})
        # Size check — Telegram limit 50MB
        size = os.path.getsize(full)
        if size > MAX_FILE_SIZE:
            return json.dumps({"error": f"File too large: {size / 1024 / 1024:.1f}MB (max {MAX_FILE_SIZE // (1024 * 1024)}MB)"})
        # Direct send via Telegram bot (immediate feedback to agent)
        bot = get_bot() if get_bot else None
        chat_id = get_chat_id() if get_chat_id else None
        if bot and chat_id:
            try:
                from aiogram.types import FSInputFile
                doc = FSInputFile(full)
                await bot.send_document(chat_id=chat_id, document=doc)
                return json.dumps({"success": True, "sent": True, "path": full, "size": size})
            except Exception as e:
                return json.dumps({"error": f"Telegram send failed: {e}", "path": full, "size": size})
        # Fallback: queue for post-response delivery (bot not available)
        from qanot.agent import Agent
        if Agent._instance:
            user_id = get_user_id() if get_user_id else ""
            Agent._instance._pending_files.setdefault(user_id, []).append(full)
        return json.dumps({"success": True, "sent": False, "queued": True, "path": full, "size": size})

    registry.register(
        name="send_file",
        description="Send a file to the user via Telegram. Workspace or absolute path.",
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
    """Resolve a path safely within workspace. Blocks escape attempts."""
    from qanot.fs_safe import resolve_workspace_path
    resolved, error = resolve_workspace_path(path, workspace_dir)
    if error:
        raise ValueError(error)
    return resolved
