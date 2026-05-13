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
    get_thread_id: Callable[[], int | None] | None = None,
    get_poll_registry: Callable | None = None,
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


    # ── tg_send_poll ──
    async def tg_send_poll(params: dict) -> str:
        """Send a native Telegram poll into the current chat.

        Wraps Bot API's ``sendPoll`` for both regular and quiz polls.
        Regular: anonymous yes/no/multi-option vote.
        Quiz: ``correct_option_id`` triggers Telegram's quiz UX (tick
        animation, explanation reveal after vote). Bot API 9.6 added
        ``correct_option_ids`` plural for multi-correct quizzes — we
        accept either ``correct_option_id`` (single) or
        ``correct_option_ids`` (list).
        """
        bot = get_bot() if get_bot else None
        chat_id = get_chat_id() if get_chat_id else None
        if bot is None or chat_id is None:
            return json.dumps({"error": "Telegram bot/chat not available"})

        question = (params.get("question") or "").strip()
        options_raw = params.get("options")
        if not question:
            return json.dumps({"error": "question is required"})
        if not isinstance(options_raw, list) or len(options_raw) < 2:
            return json.dumps({
                "error": "options must be an array of 2-10 strings",
            })
        options = [str(o).strip() for o in options_raw if str(o).strip()]
        if not (2 <= len(options) <= 10):
            return json.dumps({
                "error": f"options must be 2-10 entries (got {len(options)})",
            })
        # Telegram caps: 300 chars per question, 100 per option.
        if len(question) > 300:
            return json.dumps({"error": "question too long (>300 chars)"})
        for i, opt in enumerate(options):
            if len(opt) > 100:
                return json.dumps({
                    "error": f"option #{i+1} too long (>100 chars)",
                })

        # Quiz vs regular. Accept singular or plural for forward-compat
        # with Bot API 9.6's multi-correct quizzes.
        correct_ids_raw = params.get("correct_option_ids")
        if correct_ids_raw is None and "correct_option_id" in params:
            single = params.get("correct_option_id")
            if isinstance(single, int) and single >= 0:
                correct_ids_raw = [single]
        is_quiz = bool(correct_ids_raw) and len(correct_ids_raw or []) > 0
        if is_quiz:
            try:
                correct_ids = [int(x) for x in correct_ids_raw]
            except (TypeError, ValueError):
                return json.dumps({
                    "error": "correct_option_ids must be integers",
                })
            for cid in correct_ids:
                if not (0 <= cid < len(options)):
                    return json.dumps({
                        "error": (
                            f"correct_option_ids index {cid} out of range "
                            f"(options has {len(options)} entries)"
                        ),
                    })

        # Compose the SendPoll method. We import lazily so the tool
        # registry can load without aiogram in test contexts.
        try:
            from aiogram.methods import SendPoll
        except ImportError as e:
            return json.dumps({"error": f"aiogram unavailable: {e}"})

        send_kwargs: dict = {
            "chat_id": chat_id,
            "question": question,
            "options": options,
        }
        # Thread targeting — when the user is reading inside a private-
        # chat thread or forum topic, the poll must land there too.
        thread_id_fn = get_thread_id if get_thread_id else None
        thread_id = thread_id_fn() if thread_id_fn else None
        if thread_id:
            send_kwargs["message_thread_id"] = thread_id

        if is_quiz:
            send_kwargs["type"] = "quiz"
            # The Bot API expects ``correct_option_id`` (singular int)
            # for classic single-correct quizzes; ``correct_option_ids``
            # for multi-correct (9.6+). aiogram exposes both fields.
            if len(correct_ids) == 1:
                send_kwargs["correct_option_id"] = correct_ids[0]
            else:
                send_kwargs["correct_option_ids"] = correct_ids
            explanation = params.get("explanation")
            if isinstance(explanation, str) and explanation.strip():
                send_kwargs["explanation"] = explanation.strip()[:200]

        # Other optional flags — let the caller turn things on without
        # cluttering the common case.
        #
        # Anonymity default: caller can force ``is_anonymous=False`` to
        # opt-in to per-vote ``poll_answer`` updates. For PRIVATE chats
        # we flip the default to false automatically — there's only one
        # voter so "anonymity" is meaningless, and we want the answer
        # routed back to the agent so the conversational quiz flow
        # works. In groups we keep Telegram's default (anonymous) unless
        # the caller explicitly opts out.
        explicit_anon = params.get("is_anonymous")
        if explicit_anon is False:
            send_kwargs["is_anonymous"] = False
        elif explicit_anon is None and chat_id > 0:
            # Private chat (positive chat_id in Telegram convention).
            send_kwargs["is_anonymous"] = False
        if params.get("allows_multiple_answers") is True and not is_quiz:
            # multi-answer is regular-poll only
            send_kwargs["allows_multiple_answers"] = True
        open_period = params.get("open_period")
        if isinstance(open_period, int) and 5 <= open_period <= 600:
            send_kwargs["open_period"] = open_period

        try:
            msg = await bot(SendPoll(**send_kwargs))
            message_id = int(getattr(msg, "message_id", 0) or 0)
            poll = getattr(msg, "poll", None)
            poll_id = str(getattr(poll, "id", "") or "") if poll else ""
        except Exception as e:
            return json.dumps({"error": f"Telegram sendPoll failed: {e}"})

        # Register the poll so the adapter's poll_answer handler can
        # route the user's tap back to the agent as a synthetic message.
        # Failures here are non-fatal — the poll is already in the user's
        # chat; the worst-case is the answer doesn't flow back through
        # the conversational loop.
        if poll_id and get_poll_registry:
            try:
                registry = get_poll_registry()
                if registry is not None:
                    await registry.register(
                        poll_id=poll_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        question=question,
                        options=options,
                        correct_option_ids=correct_ids if is_quiz else [],
                        message_id=message_id,
                        explanation=send_kwargs.get("explanation", ""),
                    )
            except Exception as e:
                # Log only — the poll is sent regardless.
                import logging
                logging.getLogger(__name__).warning(
                    "poll registry register failed for poll_id=%s: %s",
                    poll_id, e,
                )

        return json.dumps({
            "success": True,
            "message_id": message_id,
            "poll_id": poll_id,
            "poll_type": "quiz" if is_quiz else "regular",
            "option_count": len(options),
        }, ensure_ascii=False)

    registry.register(
        name="tg_send_poll",
        description=(
            "Send a native Telegram poll into the current chat. Works in "
            "private chats and groups; lands in the user's open thread "
            "when one is active. Set correct_option_id (or "
            "correct_option_ids for multi-correct) to make it a quiz with "
            "Telegram's built-in tick animation. Use this for English "
            "level tests, surveys, multiple-choice questions, voting — "
            "the user taps an option instead of typing the answer."
        ),
        parameters={
            "type": "object",
            "required": ["question", "options"],
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Poll question (1-300 chars).",
                },
                "options": {
                    "type": "array",
                    "description": "2-10 answer options, each 1-100 chars.",
                    "items": {"type": "string"},
                },
                "correct_option_id": {
                    "type": "integer",
                    "description": (
                        "Zero-based index of the correct answer. When set, "
                        "the poll becomes a quiz."
                    ),
                },
                "correct_option_ids": {
                    "type": "array",
                    "description": (
                        "Multi-correct quiz: list of zero-based indices. "
                        "Bot API 9.6+. Use this OR correct_option_id, not "
                        "both."
                    ),
                    "items": {"type": "integer"},
                },
                "explanation": {
                    "type": "string",
                    "description": (
                        "Quiz only. Shown after the user votes. Max 200 "
                        "chars. Useful for teaching: 'B is correct "
                        "because…'."
                    ),
                },
                "is_anonymous": {
                    "type": "boolean",
                    "description": (
                        "Default true. Set false to attribute votes to "
                        "user accounts (group polls)."
                    ),
                },
                "allows_multiple_answers": {
                    "type": "boolean",
                    "description": (
                        "Regular polls only. Lets users pick more than "
                        "one option. Ignored for quizzes."
                    ),
                },
                "open_period": {
                    "type": "integer",
                    "description": (
                        "Auto-close after N seconds (5-600). Useful for "
                        "timed quizzes."
                    ),
                },
            },
        },
        handler=tg_send_poll,
    )


    # ── tg_send_voice ──
    async def tg_send_voice(params: dict) -> str:
        """Generate audio from text and send as a Telegram voice message.

        Uses the configured TTS provider (Muxlisa / KotibAI / Aisha).
        Aisha supports uz/en/ru — preferred for English content
        (IELTS practice, language learning). Muxlisa/KotibAI are
        Uzbek-native.
        """
        bot = get_bot() if get_bot else None
        chat_id = get_chat_id() if get_chat_id else None
        if bot is None or chat_id is None:
            return json.dumps({"error": "Telegram bot/chat not available"})

        text = (params.get("text") or "").strip()
        if not text:
            return json.dumps({"error": "text is required"})
        # Telegram caption / TTS provider limits — keep prompts focused.
        if len(text) > 4000:
            return json.dumps({
                "error": f"text too long ({len(text)} chars); split into "
                         f"chunks of ≤4000 chars",
            })

        language = (params.get("language") or "").strip().lower() or None
        voice = (params.get("voice") or "").strip() or None
        # Optional override; defaults to config.voice_provider.
        provider = (params.get("provider") or "").strip().lower() or None

        # Pull the config + voice api key. We import lazily so the
        # tool registry can register without the qanot.config import
        # cycle blowing up in tests.
        try:
            from qanot.config import load_config
            import os as _os
            cfg_path = _os.environ.get("QANOT_CONFIG", "/data/config.json")
            cfg = load_config(cfg_path)
        except Exception as e:
            return json.dumps({"error": f"config load failed: {e}"})

        provider = provider or cfg.voice_provider or "muxlisa"
        # Auto-pick the best provider for the requested language unless
        # the caller pinned one. Order: OpenAI (best multi-lingual) →
        # Aisha (good uz/en/ru) → fall through to the config default.
        if language and language != "uz" and not params.get("provider"):
            api_key_openai = cfg.get_voice_api_key("openai")
            if api_key_openai:
                provider = "openai"
            else:
                api_key_aisha = cfg.get_voice_api_key("aisha")
                if api_key_aisha:
                    provider = "aisha"

        api_key = cfg.get_voice_api_key(provider)
        if not api_key:
            return json.dumps({
                "error": (
                    f"voice provider '{provider}' has no API key configured. "
                    f"Set voice_api_keys.{provider} in config.json."
                ),
            })

        # Default language by provider: Aisha and Kotib accept en/ru;
        # Muxlisa is uz only — if user asked for English from Muxlisa,
        # warn but proceed (output will have heavy accent).
        if not language:
            language = "uz"

        try:
            from qanot.voice import text_to_speech, download_audio
            result = await text_to_speech(
                text, api_key=api_key, provider=provider,
                language=language, voice=voice, mood="neutral",
            )
        except Exception as e:
            return json.dumps({"error": f"TTS failed: {e}"})

        # Resolve audio: providers either return raw bytes (audio_data)
        # or a CDN URL we have to fetch ourselves.
        audio_path = ""
        cleanup_path: str | None = None
        if result.audio_url:
            try:
                audio_path = await download_audio(result.audio_url)
                cleanup_path = audio_path
            except Exception as e:
                return json.dumps({
                    "error": f"audio download failed: {e}",
                })
        elif result.audio_data:
            import tempfile as _tempfile
            with _tempfile.NamedTemporaryFile(
                suffix=".mp3", delete=False,
            ) as tmp:
                tmp.write(result.audio_data)
                audio_path = tmp.name
                cleanup_path = audio_path
        else:
            return json.dumps({"error": "TTS returned no audio"})

        # Thread-aware send (Bot API 10.0). The poll/voice/file
        # delivery code paths all need this to land in the user's open
        # thread; we get the live thread id via the same callback as
        # tg_send_poll.
        thread_id_fn = get_thread_id if get_thread_id else None
        thread_id = thread_id_fn() if thread_id_fn else None

        try:
            from aiogram.types import FSInputFile
            voice_file = FSInputFile(audio_path)
            send_kwargs: dict = {"chat_id": chat_id, "voice": voice_file}
            if thread_id:
                send_kwargs["message_thread_id"] = thread_id
            sent = await bot.send_voice(**send_kwargs)
            return json.dumps({
                "success": True,
                "message_id": int(getattr(sent, "message_id", 0) or 0),
                "provider": provider,
                "language": language,
                "char_count": result.character_count or len(text),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Telegram send_voice failed: {e}"})
        finally:
            # Best-effort cleanup of the temp file. We don't return it
            # to the caller so leaving it on disk is just wasted space.
            if cleanup_path:
                try:
                    import os as _os
                    _os.unlink(cleanup_path)
                except OSError:
                    pass

    registry.register(
        timeout=120.0,  # OpenAI TTS for long passages can take 30-60s;
                        # download + Telegram upload adds another 10-20s.
                        # 120s gives comfortable margin without leaving
                        # a truly stuck call hanging the agent loop.
        name="tg_send_voice",
        description=(
            "Generate audio from text via TTS and send as a Telegram "
            "voice message into the current chat. Lands in the open "
            "thread when one is active. "
            "Providers: 'openai' (best English / IELTS quality, six "
            "native voices), 'aisha' (uz/en/ru with mood), 'muxlisa' "
            "and 'kotib' (Uzbek-native). For English content (IELTS "
            "Listening, language learning) pass language='en' — we "
            "auto-pick OpenAI when its key is configured, else Aisha. "
            "Use this when the user wants to LISTEN (language learning, "
            "accessibility, hands-free reply) rather than read text."
        ),
        parameters={
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to synthesise (1-4000 chars).",
                },
                "language": {
                    "type": "string",
                    "description": (
                        "ISO language code: 'uz', 'en', or 'ru'. "
                        "Defaults to 'uz'. Non-Uzbek auto-promotes the "
                        "provider to OpenAI (or Aisha if no OpenAI key)."
                    ),
                },
                "voice": {
                    "type": "string",
                    "description": (
                        "Optional voice override. OpenAI: 'alloy' | "
                        "'echo' | 'fable' | 'onyx' | 'nova' | 'shimmer'. "
                        "Aisha: 'gulnoza' (female) | 'jaxongir' (male). "
                        "Muxlisa: 'maftuna' | 'asomiddin'. KotibAI: "
                        "'aziza' | 'sherzod' | 'rachel' | 'arnold'."
                    ),
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "Optional provider override: 'openai', "
                        "'muxlisa', 'kotib', or 'aisha'. Defaults to "
                        "config.voice_provider, or auto-promotes to "
                        "'openai'/'aisha' for non-Uzbek language."
                    ),
                },
            },
        },
        handler=tg_send_voice,
    )


def _resolve_path(path: str, workspace_dir: str) -> str:
    """Resolve a path safely within workspace. Blocks escape attempts."""
    from qanot.fs_safe import resolve_workspace_path
    resolved, error = resolve_workspace_path(path, workspace_dir)
    if error:
        raise ValueError(error)
    return resolved
