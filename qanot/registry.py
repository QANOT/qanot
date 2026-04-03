"""Tool registry — extracted from agent.py to break circular imports."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Awaitable

from qanot.context import truncate_tool_result
from qanot.plugins.base import validate_tool_params

logger = logging.getLogger(__name__)

TOOL_TIMEOUT = 30  # seconds per tool execution


class ToolRegistry:
    """Registry of available tools with lazy loading support.

    Tools are grouped by category. Core tools (always loaded) are sent
    with every API call. Extended tools are only sent when relevant,
    saving tokens on every request.
    """

    # Core tools: always sent to LLM (cheap, frequently used)
    CORE_CATEGORY = "core"
    # Extended: only loaded when the user's message hints they're needed
    EXTENDED_CATEGORIES = {"rag", "image", "web", "cron", "agent", "plugin"}

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable[[dict], Awaitable[str]]] = {}
        self._categories: dict[str, str] = {}  # tool_name -> category
        self._cached_definitions: list[dict] | None = None
        self._cached_core: list[dict] | None = None

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict], Awaitable[str]],
        category: str = "core",
    ) -> None:
        """Register a tool with its handler.

        Args:
            category: Tool category for lazy loading.
                "core" = always loaded (read_file, write_file, etc.)
                "rag", "image", "web", "cron", "agent", "plugin" = loaded on demand.
        """
        if name in self._tools:
            logger.warning("Tool '%s' already registered — overriding", name)
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": parameters,
        }
        self._handlers[name] = handler
        self._categories[name] = category
        self._cached_definitions = None
        self._cached_core = None

    def get_definitions(self) -> list[dict]:
        """Get ALL tool definitions (fallback, full list)."""
        if self._cached_definitions is None:
            self._cached_definitions = list(self._tools.values())
        return self._cached_definitions

    def get_lazy_definitions(self, user_message: str = "") -> list[dict]:
        """Get tool definitions -- returns ALL tools every time.

        Why not filter? Because Ollama (and most providers) cache the KV state
        when the prompt prefix is identical. Sending the same tools every time
        means prompt_eval is near-zero on subsequent calls (cache hit).

        Changing the tool set per message BREAKS the cache and causes
        full prompt re-evaluation every time -- much slower.

        OpenClaw uses the same strategy: consistent tool set = cache friendly.
        """
        return self.get_definitions()

    async def execute(
        self,
        name: str,
        input_data: dict,
        timeout: float = TOOL_TIMEOUT,
        *,
        workspace_dir: str = "",
    ) -> str:
        """Execute a tool by name with parameter validation and timeout protection."""
        # Validate input types to prevent type confusion attacks
        if not isinstance(name, str) or not name.strip():
            return json.dumps({"error": "Invalid tool name"})
        # Sanitize tool name: must be alphanumeric/underscore, max 64 chars
        name = name.strip()
        if len(name) > 64 or not all(c.isalnum() or c == '_' for c in name):
            logger.warning("Rejected invalid tool name: %r", name[:80])
            return json.dumps({"error": "Invalid tool name: must be alphanumeric/underscore, max 64 chars"})
        if not isinstance(input_data, dict):
            logger.warning("Tool %s received non-dict input: %s", name, type(input_data).__name__)
            return json.dumps({"error": "Tool input must be a JSON object"})
        handler = self._handlers.get(name)
        if not handler:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Validate parameters against schema before execution
        tool_def = self._tools.get(name, {})
        schema = tool_def.get("input_schema", {})
        if schema:
            errors = validate_tool_params(input_data, schema)
            if errors:
                logger.warning("Tool %s param validation: %s", name, errors)
                return json.dumps({"error": f"Invalid parameters: {'; '.join(errors)}"})

        try:
            result = await asyncio.wait_for(handler(input_data), timeout=timeout)
            # Truncate oversized results (persist to disk when workspace available)
            return truncate_tool_result(
                result, tool_name=name, workspace_dir=workspace_dir,
            )
        except asyncio.TimeoutError:
            logger.error("Tool %s timed out after %ds", name, timeout)
            return json.dumps({"error": f"Tool timed out after {timeout}s"})
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e, exc_info=True)
            # Sanitize error message to prevent leaking sensitive internals
            error_msg = str(e)
            # Truncate overly long error messages that may contain data dumps
            if len(error_msg) > 500:
                error_msg = error_msg[:500] + "... [truncated]"
            # Strip potential file system paths from error messages
            error_msg = re.sub(r'(/[\w./\-]+){3,}', '[path redacted]', error_msg)
            # Strip potential environment variable values or API keys
            error_msg = re.sub(r'(?:key|token|secret|password|auth)[=:]\s*\S+', '[credential redacted]', error_msg, flags=re.IGNORECASE)
            return json.dumps({"error": error_msg})

    def get_handler(self, name: str):
        """Get a tool handler by name. Returns None if not found."""
        return self._handlers.get(name)

    @property
    def tool_names(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())
