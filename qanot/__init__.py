"""Qanot AI — Lightweight Python agent framework for Telegram bots."""

from __future__ import annotations

__version__ = "2.0.1"


# Public API — lazy imports to keep `import qanot` fast
def __getattr__(name: str):
    _imports = {
        "Agent": "qanot.agent",
        "Config": "qanot.config",
        "load_config": "qanot.config",
        "ProviderConfig": "qanot.config",
        "AgentDefinition": "qanot.config",
        "Plugin": "qanot.plugins.base",
        "tool": "qanot.plugins.base",
        "ToolDef": "qanot.plugins.base",
        "LLMProvider": "qanot.providers.base",
        "ProviderResponse": "qanot.providers.base",
        "StreamEvent": "qanot.providers.base",
        "ToolCall": "qanot.providers.base",
        "Usage": "qanot.providers.base",
    }
    if name in _imports:
        import importlib
        module = importlib.import_module(_imports[name])
        return getattr(module, name)
    raise AttributeError(f"module 'qanot' has no attribute {name!r}")


__all__ = [
    "Agent",
    "Config",
    "load_config",
    "ProviderConfig",
    "AgentDefinition",
    "Plugin",
    "tool",
    "ToolDef",
    "LLMProvider",
    "ProviderResponse",
    "StreamEvent",
    "ToolCall",
    "Usage",
]
