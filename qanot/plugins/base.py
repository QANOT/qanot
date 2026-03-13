"""Plugin base class, @tool decorator, and plugin manifest."""

from __future__ import annotations

import functools
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
            return cls(
                name=raw.get("name", path.parent.name),
                version=raw.get("version", "0.1.0"),
                description=raw.get("description", ""),
                author=raw.get("author", ""),
                dependencies=raw.get("dependencies", []),
                plugin_deps=raw.get("plugin_deps", []),
                required_config=raw.get("required_config", []),
                min_qanot_version=raw.get("min_qanot_version", ""),
                homepage=raw.get("homepage", ""),
                license=raw.get("license", "MIT"),
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
            "parameters": parameters or {"type": "object", "properties": {}},
        }
        @functools.wraps(func)
        async def wrapper(self_or_params, *args, **kwargs):
            return await func(self_or_params, *args, **kwargs)
        wrapper._tool_def = func._tool_def  # type: ignore
        return wrapper
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
            attr = getattr(self, attr_name, None)
            if callable(attr) and hasattr(attr, "_tool_def"):
                td = attr._tool_def
                tools.append(ToolDef(
                    name=td["name"],
                    description=td["description"],
                    parameters=td["parameters"],
                    handler=attr,
                ))
        return tools


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


def _check_type(value: Any, expected: str) -> bool:
    """Check if value matches JSON schema type."""
    # In Python, bool is a subclass of int, so we must check bool first
    # to avoid booleans passing as integers/numbers.
    if isinstance(value, bool):
        return expected == "boolean"
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    expected_types = type_map.get(expected)
    if expected_types is None:
        return True  # Unknown type, skip validation
    return isinstance(value, expected_types)
