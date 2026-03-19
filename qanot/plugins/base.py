"""Plugin base class, @tool decorator, and plugin manifest."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    """Tool definition."""
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]


@dataclass
class PluginManifest:
    """Plugin manifest from plugin.json."""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    dependencies: list[str] = field(default_factory=list)  # pip packages
    plugin_deps: list[str] = field(default_factory=list)  # other qanot plugins
    required_config: list[str] = field(default_factory=list)  # required config keys
    min_qanot_version: str = ""
    homepage: str = ""
    license: str = "MIT"

    @classmethod
    def from_file(cls, path: Path) -> PluginManifest:
        """Load manifest from plugin.json file."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("plugin.json at %s is not a JSON object", path)
                return cls(name=path.parent.name)

            def _str(key: str, default: str = "") -> str:
                v = raw.get(key, default)
                return str(v) if v is not None else default

            def _list(key: str) -> list[str]:
                v = raw.get(key, [])
                if not isinstance(v, list):
                    logger.warning("plugin.json at %s: '%s' should be a list, got %s", path, key, type(v).__name__)
                    return []
                return [str(item) for item in v]

            return cls(
                name=_str("name", path.parent.name),
                version=_str("version", "0.1.0"),
                description=_str("description"),
                author=_str("author"),
                dependencies=_list("dependencies"),
                plugin_deps=_list("plugin_deps"),
                required_config=_list("required_config"),
                min_qanot_version=_str("min_qanot_version"),
                homepage=_str("homepage"),
                license=_str("license", "MIT"),
            )
        except Exception as e:
            logger.warning("Failed to parse plugin.json at %s: %s", path, e)
            return cls(name=path.parent.name)

    @classmethod
    def default(cls, name: str) -> PluginManifest:
        """Create a default manifest for plugins without plugin.json."""
        return cls(name=name)


def tool(
    name: str,
    description: str,
    parameters: dict | None = None,
):
    """Decorator to mark a method as a tool."""
    def decorator(func: Callable) -> Callable:
        func._tool_def = {  # type: ignore
            "name": name,
            "description": description,
            "parameters": parameters if parameters is not None else {"type": "object", "properties": {}},
        }
        return func
    return decorator


class Plugin(ABC):
    """Base class for Qanot AI plugins."""

    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    tools_md: str = ""  # TOOLS.md content
    soul_append: str = ""  # SOUL_APPEND.md content

    @abstractmethod
    def get_tools(self) -> list[ToolDef]:
        """Return list of tool definitions."""
        ...

    async def setup(self, config: dict) -> None:
        """Called on plugin load. Override to initialize resources."""
        pass

    async def teardown(self) -> None:
        """Called on shutdown. Override to cleanup resources."""
        pass

    async def on_error(self, tool_name: str, error: Exception) -> None:
        """Called when a tool execution fails. Override for custom error handling."""
        pass

    def _collect_tools(self) -> list[ToolDef]:
        """Auto-collect tools from decorated methods."""
        tools: list[ToolDef] = []
        for attr_name in dir(self):
            try:
                attr = getattr(self, attr_name)
            except Exception:
                continue
            if callable(attr) and hasattr(attr, "_tool_def"):
                tools.append(ToolDef(**attr._tool_def, handler=attr))
        return tools

    # ── Extensibility hooks (all optional, empty defaults) ──

    def get_wal_patterns(self) -> list[tuple[str, str, bool]]:
        """Return WAL patterns: [(regex, category, is_durable)].

        Core will register these automatically. No need to edit memory.py.
        """
        return []

    def get_prompt_sections(self) -> list[dict]:
        """Return prompt sections: [{"name": str, "content": str, "after": str}].

        Injected into system prompt. No need to edit prompt.py.
        """
        return []

    def get_template_dir(self) -> str | None:
        """Return path to workspace template files directory.

        Files copied on init_workspace() if they don't exist.
        """
        return None

    def get_cron_jobs(self) -> list[dict]:
        """Return cron jobs: [{"name": str, "schedule": str, "prompt": str, "mode": str}].

        Registered with scheduler on load.
        """
        return []

    def get_commands(self) -> list[dict]:
        """Return Telegram commands: [{"command": str, "description": str, "handler": Callable}]."""
        return []

    def get_template_vars(self) -> dict[str, str]:
        """Return custom template variables for prompt: {"{key}": "value"}."""
        return {}

    async def on_pre_turn(self, user_id: str, message: str) -> str | None:
        """Called before each agent turn. Return modified message or None."""
        return None

    async def on_post_turn(self, user_id: str, message: str, response: str) -> str | None:
        """Called after agent response. Return modified response or None."""
        return None


def validate_tool_params(params: dict, schema: dict) -> list[str]:
    """Validate tool parameters against JSON schema (lightweight).

    Returns list of error messages. Empty list = valid.
    Only validates: required fields, type checking for basic types.
    """
    if not isinstance(params, dict):
        return [f"Parameters must be a dict, got {type(params).__name__}"]
    if not isinstance(schema, dict):
        return [f"Schema must be a dict, got {type(schema).__name__}"]

    errors: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    if not isinstance(properties, dict):
        properties = {}
    if not isinstance(required, list):
        required = []
    else:
        required = [k for k in required if isinstance(k, str)]

    # Check required fields
    for key in required:
        if key not in params:
            errors.append(f"Missing required parameter: {key}")

    # Check types for provided fields
    for key, value in params.items():
        if key not in properties:
            continue
        prop = properties[key]
        if not isinstance(prop, dict):
            continue
        expected_type = prop.get("type")
        if expected_type and not _check_type(value, expected_type):
            errors.append(f"Parameter '{key}' expected {expected_type}, got {type(value).__name__}")

    return errors


_JSON_SCHEMA_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _check_type(value: Any, expected: str) -> bool:
    """Check if value matches JSON schema type."""
    # In Python, bool is a subclass of int, so we must check bool first
    # to avoid booleans passing as integers/numbers.
    if isinstance(value, bool):
        return expected == "boolean"
    if (expected_types := _JSON_SCHEMA_TYPE_MAP.get(expected)) is None:
        return True  # Unknown type, skip validation
    return isinstance(value, expected_types)
