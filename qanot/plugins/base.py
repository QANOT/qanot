"""Plugin base class and @tool decorator."""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class ToolDef:
    """Tool definition."""
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]


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
