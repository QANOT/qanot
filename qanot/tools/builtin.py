"""Built-in tools — file ops, web_search, run_command, memory_search, session_status."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.agent import ToolRegistry
from qanot.context import ContextTracker
from qanot.memory import memory_search as _memory_search

if TYPE_CHECKING:
    from qanot.rag.indexer import MemoryIndexer

logger = logging.getLogger(__name__)

ALLOWED_COMMANDS = {"python3", "python", "curl", "ffmpeg", "zip", "unzip", "git", "ls", "cat", "head", "tail", "grep", "wc", "pip", "pip3"}
MAX_OUTPUT = 10000


def register_builtin_tools(
    registry: ToolRegistry,
    workspace_dir: str,
    context: ContextTracker,
    rag_indexer: "MemoryIndexer | None" = None,
) -> None:
    """Register all built-in tools."""

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
        description="Faylni o'qish. Workspace ichidagi fayllar uchun.",
        parameters={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Fayl yo'li (workspace ichida yoki absolyut)"},
            },
        },
        handler=read_file,
    )

    # ── write_file ──
    async def write_file(params: dict) -> str:
        path = params.get("path", "")
        content = params.get("content", "")
        if not path:
            return json.dumps({"error": "path is required"})
        full = _resolve_path(path, workspace_dir)
        try:
            Path(full).parent.mkdir(parents=True, exist_ok=True)
            Path(full).write_text(content, encoding="utf-8")
            return json.dumps({"success": True, "path": path, "bytes": len(content.encode())})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="write_file",
        description="Faylga yozish yoki yangi fayl yaratish.",
        parameters={
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": "Fayl yo'li"},
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
        description="Papka ichidagi fayllar ro'yxati.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Papka yo'li (default: workspace)"},
            },
        },
        handler=list_files,
    )

    # ── run_command ──
    # Shell metacharacters that indicate injection attempts
    _SHELL_METACHARS = set("|;&`$(){}><\n")

    async def run_command(params: dict) -> str:
        command = params.get("command", "").strip()
        if not command:
            return json.dumps({"error": "command is required"})

        # Security: reject shell metacharacters to prevent injection
        if any(c in command for c in _SHELL_METACHARS):
            return json.dumps({"error": "Shell operators (|, ;, &, $, `, etc.) are not allowed. Use separate commands."})

        # Parse command safely
        try:
            args = shlex.split(command)
        except ValueError as e:
            return json.dumps({"error": f"Invalid command syntax: {e}"})

        if not args:
            return json.dumps({"error": "command is required"})

        # Security: check executable against allowlist
        exe = args[0]
        if exe not in ALLOWED_COMMANDS:
            return json.dumps({"error": f"Command '{exe}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"})

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=workspace_dir,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n--- stderr ---\n{result.stderr}"
            if len(output) > MAX_OUTPUT:
                output = output[:MAX_OUTPUT] + "\n... (truncated)"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Command timed out (30s)"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="run_command",
        description="Buyruq bajarish (sandboxed). Ruxsat: python3, curl, ffmpeg, zip, git, pip.",
        parameters={
            "type": "object",
            "required": ["command"],
            "properties": {
                "command": {"type": "string", "description": "Bajariladigan buyruq"},
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

        # Use RAG-powered search when available, fall back to substring search
        if rag_indexer is not None:
            try:
                results = await rag_indexer.search(query)
                if results:
                    return json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("RAG search failed, falling back to substring: %s", e)

        results = _memory_search(query, workspace_dir)
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
        return json.dumps(context.session_status(), indent=2)

    registry.register(
        name="session_status",
        description="Joriy sessiya holati — context %, token soni.",
        parameters={"type": "object", "properties": {}},
        handler=session_status,
    )


def _resolve_path(path: str, workspace_dir: str) -> str:
    """Resolve a path relative to workspace or as absolute."""
    if os.path.isabs(path):
        return path
    return os.path.join(workspace_dir, path)
