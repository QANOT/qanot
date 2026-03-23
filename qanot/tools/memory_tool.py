"""Anthropic Memory Tool (memory_20250818) — client-side handler.

Implements the Anthropic memory tool protocol: view, create, str_replace,
insert, delete, rename operations on a /memories directory.

This tool is registered for ALL providers as a regular tool, but when
the Anthropic provider is active, the server-side type hint
(memory_20250818) is also injected so Claude uses its trained memory
behavior (auto-check on startup, save progress, etc.).

Other providers (GPT, Gemini, Groq) get the same tool without the
trained behavior — they can still use it when the agent decides to.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Memory tool definition matching Anthropic's memory_20250818 spec
MEMORY_TOOL_TYPE = "memory_20250818"
MEMORY_TOOL_NAME = "memory"

MAX_FILE_LINES = 999_999
MAX_VIEW_CHARS = 100_000


def _resolve_safe(memories_dir: Path, rel_path: str) -> Path | None:
    """Resolve a path safely within the memories directory."""
    # Normalize: /memories/foo.txt → foo.txt
    clean = rel_path.strip()
    if clean.startswith("/memories"):
        clean = clean[len("/memories"):]
    clean = clean.lstrip("/")

    if not clean:
        return memories_dir

    target = (memories_dir / clean).resolve()
    mem_resolved = memories_dir.resolve()

    if not str(target).startswith(str(mem_resolved)):
        return None  # path traversal attempt
    return target


def _format_dir_listing(path: Path, memories_dir: Path, depth: int = 2) -> str:
    """Format directory listing with sizes, up to given depth."""
    lines = []
    path = path.resolve()
    base = memories_dir.resolve()

    def _walk(p: Path, current_depth: int):
        if current_depth > depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            rel = "/" + str(entry.relative_to(base))
            rel = "/memories" + rel if rel != "/" else "/memories"
            if entry.is_file():
                size = entry.stat().st_size
                if size < 1024:
                    sz = f"{size}"
                elif size < 1024 * 1024:
                    sz = f"{size / 1024:.1f}K"
                else:
                    sz = f"{size / (1024 * 1024):.1f}M"
                lines.append(f"{sz}\t{rel}")
            elif entry.is_dir():
                lines.append(f"-\t{rel}/")
                _walk(entry, current_depth + 1)

    # Root entry
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    if total < 1024:
        root_sz = f"{total}"
    elif total < 1024 * 1024:
        root_sz = f"{total / 1024:.1f}K"
    else:
        root_sz = f"{total / (1024 * 1024):.1f}M"
    lines.insert(0, f"{root_sz}\t/memories")

    _walk(path, 1)

    return (
        "Here're the files and directories up to 2 levels deep in /memories, "
        "excluding hidden items and node_modules:\n" + "\n".join(lines)
    )


def _format_file_content(path: Path, view_range: list[int] | None = None) -> str:
    """Format file content with line numbers."""
    content = path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    if len(lines) > MAX_FILE_LINES:
        return f"File /memories/{path.name} exceeds maximum line limit of {MAX_FILE_LINES} lines."

    if view_range and len(view_range) == 2:
        start, end = view_range
        start = max(1, start)
        end = min(len(lines), end)
        selected = lines[start - 1:end]
        start_line = start
    else:
        selected = lines
        start_line = 1

    formatted = []
    for i, line in enumerate(selected, start=start_line):
        formatted.append(f"{i:6d}\t{line}")

    display = str(path.name)
    return f"Here's the content of /memories/{display} with line numbers:\n" + "\n".join(formatted)


def register_memory_tool(
    registry: ToolRegistry,
    workspace_dir: str,
) -> None:
    """Register the Anthropic-compatible memory tool for all providers."""
    memories_dir = Path(workspace_dir) / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)

    async def memory_handler(params: dict) -> str:
        command = params.get("command", "")

        if command == "view":
            return _handle_view(memories_dir, params)
        elif command == "create":
            return _handle_create(memories_dir, params)
        elif command == "str_replace":
            return _handle_str_replace(memories_dir, params)
        elif command == "insert":
            return _handle_insert(memories_dir, params)
        elif command == "delete":
            return _handle_delete(memories_dir, params)
        elif command == "rename":
            return _handle_rename(memories_dir, params)
        else:
            return f"Unknown memory command: {command}"

    registry.register(
        name=MEMORY_TOOL_NAME,
        description=(
            "Persistent memory — read and write files in /memories directory. "
            "Use to store important facts, user preferences, project context, "
            "and progress notes that persist across conversations. "
            "Commands: view, create, str_replace, insert, delete, rename."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert", "delete", "rename"],
                    "description": "The operation to perform",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path (e.g., /memories/notes.txt)",
                },
                "file_text": {
                    "type": "string",
                    "description": "Content for 'create' command",
                },
                "old_str": {
                    "type": "string",
                    "description": "Text to find for 'str_replace'",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement text for 'str_replace'",
                },
                "insert_line": {
                    "type": "integer",
                    "description": "Line number for 'insert' command",
                },
                "insert_text": {
                    "type": "string",
                    "description": "Text to insert for 'insert' command",
                },
                "view_range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional [start, end] line range for 'view'",
                },
                "old_path": {
                    "type": "string",
                    "description": "Source path for 'rename'",
                },
                "new_path": {
                    "type": "string",
                    "description": "Destination path for 'rename'",
                },
            },
            "required": ["command"],
        },
        handler=memory_handler,
        category="memory",
    )
    logger.info("Memory tool registered (memories dir: %s)", memories_dir)


# ── Command handlers ──────────────────────────────────────


def _handle_view(memories_dir: Path, params: dict) -> str:
    path_str = params.get("path", "/memories")
    target = _resolve_safe(memories_dir, path_str)
    if target is None:
        return f"The path {path_str} does not exist. Please provide a valid path."

    if not target.exists():
        return f"The path {path_str} does not exist. Please provide a valid path."

    if target.is_dir():
        return _format_dir_listing(target, memories_dir)
    else:
        view_range = params.get("view_range")
        return _format_file_content(target, view_range)


def _handle_create(memories_dir: Path, params: dict) -> str:
    path_str = params.get("path", "")
    file_text = params.get("file_text", "")

    if not path_str:
        return "Error: path is required for create command"

    target = _resolve_safe(memories_dir, path_str)
    if target is None:
        return f"Error: invalid path {path_str}"

    if target.exists():
        return f"Error: File {path_str} already exists"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_text, encoding="utf-8")
    return f"File created successfully at: {path_str}"


def _handle_str_replace(memories_dir: Path, params: dict) -> str:
    path_str = params.get("path", "")
    old_str = params.get("old_str", "")
    new_str = params.get("new_str", "")

    target = _resolve_safe(memories_dir, path_str)
    if target is None or not target.exists() or target.is_dir():
        return f"Error: The path {path_str} does not exist. Please provide a valid path."

    content = target.read_text(encoding="utf-8")
    count = content.count(old_str)

    if count == 0:
        return f"No replacement was performed, old_str `{old_str}` did not appear verbatim in {path_str}."

    if count > 1:
        lines = content.splitlines()
        line_nums = [i + 1 for i, line in enumerate(lines) if old_str in line]
        return (
            f"No replacement was performed. Multiple occurrences of old_str "
            f"`{old_str}` in lines: {line_nums}. Please ensure it is unique"
        )

    new_content = content.replace(old_str, new_str, 1)
    target.write_text(new_content, encoding="utf-8")
    return "The memory file has been edited."


def _handle_insert(memories_dir: Path, params: dict) -> str:
    path_str = params.get("path", "")
    insert_line = params.get("insert_line", 0)
    insert_text = params.get("insert_text", "")

    target = _resolve_safe(memories_dir, path_str)
    if target is None or not target.exists() or target.is_dir():
        return f"Error: The path {path_str} does not exist"

    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    n_lines = len(lines)

    if insert_line < 0 or insert_line > n_lines:
        return (
            f"Error: Invalid `insert_line` parameter: {insert_line}. "
            f"It should be within the range of lines of the file: [0, {n_lines}]"
        )

    # Insert at the specified line
    insert_lines = insert_text.splitlines(keepends=True)
    if insert_lines and not insert_lines[-1].endswith("\n"):
        insert_lines[-1] += "\n"

    new_lines = lines[:insert_line] + insert_lines + lines[insert_line:]
    target.write_text("".join(new_lines), encoding="utf-8")
    return f"The file {path_str} has been edited."


def _handle_delete(memories_dir: Path, params: dict) -> str:
    path_str = params.get("path", "")

    target = _resolve_safe(memories_dir, path_str)
    if target is None or not target.exists():
        return f"Error: The path {path_str} does not exist"

    if target == memories_dir.resolve():
        return "Error: Cannot delete the memories root directory"

    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()

    return f"Successfully deleted {path_str}"


def _handle_rename(memories_dir: Path, params: dict) -> str:
    old_path_str = params.get("old_path", "")
    new_path_str = params.get("new_path", "")

    old_target = _resolve_safe(memories_dir, old_path_str)
    new_target = _resolve_safe(memories_dir, new_path_str)

    if old_target is None or not old_target.exists():
        return f"Error: The path {old_path_str} does not exist"

    if new_target is None:
        return f"Error: Invalid destination path {new_path_str}"

    if new_target.exists():
        return f"Error: The destination {new_path_str} already exists"

    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    return f"Successfully renamed {old_path_str} to {new_path_str}"
