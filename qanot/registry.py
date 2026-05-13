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
        self._timeouts: dict[str, float] = {}  # tool_name -> seconds
        # Per-tool field-validation declarations:
        # ``{tool_name: {field_path: human_label}}``. Field paths may be
        # nested via "." (e.g. ``properties.title``); the registry walks
        # the input dict and submits each matching value to the memo
        # validator before dispatching the handler. Empty when no tool
        # has opted in.
        self._validate_fields: dict[str, dict[str, str]] = {}
        # Lazy callable returning the active validator runtime. Set by
        # ``set_memo_validator`` once the agent has wired up the memo
        # router + Anthropic client. Returning None means validation
        # is disabled — the registry passes input through unmodified.
        self._memo_validator: Callable[[], Awaitable[Any | None]] | None = None
        self._cached_definitions: list[dict] | None = None
        self._cached_core: list[dict] | None = None

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict], Awaitable[str]],
        category: str = "core",
        timeout: float | None = None,
        validate_fields: dict[str, str] | None = None,
    ) -> None:
        """Register a tool with its handler.

        Args:
            category: Tool category for lazy loading.
                "core" = always loaded (read_file, write_file, etc.)
                "rag", "image", "web", "cron", "agent", "plugin" = loaded on demand.
            timeout: Per-tool execution timeout in seconds. ``None`` uses
                the global ``TOOL_TIMEOUT`` (30s). Slow tools (TTS,
                video generation, large web fetches) should override —
                we don't want a longer global default because it makes
                buggy tools hang the agent loop.
            validate_fields: Map of input field paths to human-readable
                labels. Each matching value is run through the memo
                validator before the handler fires, so the agent can't
                ship rule-violating output (e.g. a Notion title format
                the user has banned). Field paths support nested keys
                via "." — e.g. ``{"title": "Notion page title",
                "properties.heading": "DOCX heading"}``. ``None`` =
                no fields validated (default). When the validator is
                not configured at the registry level, this declaration
                is a no-op — the tool runs as if unmarked.
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
        if timeout is not None:
            self._timeouts[name] = float(timeout)
        else:
            self._timeouts.pop(name, None)
        if validate_fields:
            self._validate_fields[name] = dict(validate_fields)
        else:
            self._validate_fields.pop(name, None)
        self._cached_definitions = None
        self._cached_core = None

    def set_memo_validator(
        self, validator_factory: Callable[[], Awaitable[Any | None]] | None,
    ) -> None:
        """Wire up the memo validator runtime.

        ``validator_factory`` is an async callable that returns the active
        ``MemoValidatorRuntime`` (or None when validation should be
        skipped — e.g. no current user, no scope, RAG disabled). The
        registry calls it once per validated tool invocation, so the
        factory has freedom to refresh state each turn.
        """
        self._memo_validator = validator_factory

    def get_definitions(self) -> list[dict]:
        """Get ALL tool definitions, sorted by name for prompt cache stability.

        Consistent ordering ensures the tool section of the API request is
        identical across calls, maximizing Anthropic prompt cache hits.
        """
        if self._cached_definitions is None:
            self._cached_definitions = sorted(
                self._tools.values(), key=lambda t: t["name"]
            )
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

        # Memo validator hook — runs on tools that declared validate_fields.
        # The validator rewrites text-bearing fields to comply with active
        # feedback memos (e.g. user-stated title format rules). Failures
        # in the validator are swallowed — the tool still fires with the
        # original input. This is the evaluator-optimizer layer for the
        # buried-bullet bug class; see qanot/memos/validator.py.
        await self._maybe_validate_input(name, input_data)

        # Per-tool override beats the default if the tool registered one
        # (e.g. tg_send_voice needs ~60-90s for long-text OpenAI TTS).
        effective_timeout = self._timeouts.get(name, timeout)

        try:
            result = await asyncio.wait_for(
                handler(input_data), timeout=effective_timeout,
            )
            # Truncate oversized results (persist to disk when workspace available)
            return truncate_tool_result(
                result, tool_name=name, workspace_dir=workspace_dir,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Tool %s timed out after %.1fs", name, effective_timeout,
            )
            return json.dumps({
                "error": f"Tool timed out after {effective_timeout:.0f}s",
            })
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

    async def _maybe_validate_input(self, name: str, input_data: dict) -> None:
        """Rewrite text-bearing fields of ``input_data`` to comply with
        active feedback memos. Mutates ``input_data`` in place.

        No-op when the tool didn't declare validate_fields, when no
        validator factory is wired, or when the factory returns None
        (e.g. no feedback memos in scope, so no rules to check).
        """
        fields = self._validate_fields.get(name)
        if not fields or self._memo_validator is None:
            return
        try:
            runtime = await self._memo_validator()
        except Exception as exc:  # noqa: BLE001 — validator factory must not break tools
            logger.warning("memo validator factory failed: %s", exc)
            return
        if runtime is None:
            return

        for path, label in fields.items():
            try:
                original = _read_nested(input_data, path)
            except (KeyError, TypeError, AttributeError):
                continue
            if not isinstance(original, str) or not original.strip():
                continue
            try:
                result = await runtime(original, field_context=label)
            except Exception as exc:  # noqa: BLE001 — never block on validator
                logger.warning(
                    "validator failed for tool=%s field=%s: %s",
                    name, path, exc,
                )
                continue
            if result is None or not getattr(result, "was_changed", False):
                continue
            new_value = result.verified
            try:
                _write_nested(input_data, path, new_value)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "validator could not write %s.%s back: %s",
                    name, path, exc,
                )
                continue
            logger.info(
                "validator rewrote %s.%s for tool execution: %d violation(s)",
                name, path, len(getattr(result, "violations", []) or []),
            )

    def get_handler(self, name: str):
        """Get a tool handler by name. Returns None if not found."""
        return self._handlers.get(name)

    @property
    def tool_names(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._tools.keys())


# ─── nested-path helpers for validate_fields ────────────────────


def _read_nested(data: dict, path: str) -> Any:
    """Look up ``data`` at the dotted ``path``. Returns the value or
    raises KeyError / TypeError if the path doesn't resolve.

    Examples:
        _read_nested({"a": 1}, "a") == 1
        _read_nested({"props": {"title": "x"}}, "props.title") == "x"
    """
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            raise TypeError(f"cannot descend into non-dict at {part!r}")
        cur = cur[part]  # raises KeyError on missing
    return cur


def _write_nested(data: dict, path: str, value: Any) -> None:
    """Write ``value`` at the dotted ``path``, creating intermediate
    dicts if needed. Raises if any intermediate node exists but is
    not a dict — we don't overwrite non-dict types blindly.
    """
    parts = path.split(".")
    cur: Any = data
    for part in parts[:-1]:
        if part not in cur:
            cur[part] = {}
        nxt = cur[part]
        if not isinstance(nxt, dict):
            raise TypeError(f"cannot descend into non-dict at {part!r}")
        cur = nxt
    cur[parts[-1]] = value
