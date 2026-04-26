"""Bootstrap helpers for assembling the Qanot AI runtime.

The submodules in this package extract setup code that used to live inside
``qanot.main.main()``. They are pure helpers: each one builds or wires a
specific subsystem (provider stack, tool registrations, plugin lifecycle)
and returns the result back to ``main()``.
"""

from __future__ import annotations

from qanot.bootstrap.provider_factory import (
    build_provider,
    find_gemini_key,
)
from qanot.bootstrap.plugin_loader import setup_plugins, teardown_plugins
from qanot.bootstrap.tool_registry import (
    register_post_agent_tools,
    register_pre_agent_tools,
)

__all__ = [
    "build_provider",
    "find_gemini_key",
    "register_pre_agent_tools",
    "register_post_agent_tools",
    "setup_plugins",
    "teardown_plugins",
]
