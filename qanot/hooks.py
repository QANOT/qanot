"""Lifecycle hook registry — extensible event system for plugins and skills.

Hook points fire at key moments in the agent lifecycle. Plugins register
callbacks that run automatically. Error in one hook doesn't affect others.

Hook points:
    on_startup      — After agent is fully initialized
    on_shutdown     — Before agent shuts down
    on_pre_turn     — Before processing user message (can modify message)
    on_post_turn    — After response generated (can modify response)
    on_tool_use     — After a tool is called (logging/auditing)
    on_error        — When an error occurs during a turn
    on_compaction   — When context compaction is triggered
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

HOOK_POINTS = frozenset({
    "on_startup",
    "on_shutdown",
    "on_pre_turn",
    "on_post_turn",
    "on_tool_use",
    "on_error",
    "on_compaction",
})


class HookRegistry:
    """Registry for lifecycle hooks with priority ordering and error isolation."""

    def __init__(self):
        self._hooks: dict[str, list[tuple[int, str, Callable]]] = {
            hp: [] for hp in HOOK_POINTS
        }

    def register(
        self,
        hook_point: str,
        callback: Callable,
        *,
        name: str = "",
        priority: int = 100,
    ) -> None:
        """Register a callback for a hook point.

        Args:
            hook_point: One of HOOK_POINTS.
            callback: Async callable to invoke.
            name: Human-readable name for logging.
            priority: Lower number = runs first. Default 100.
        """
        if hook_point not in HOOK_POINTS:
            logger.warning("Unknown hook point: %s (available: %s)", hook_point, ", ".join(sorted(HOOK_POINTS)))
            return

        label = name or getattr(callback, "__qualname__", str(callback))
        self._hooks[hook_point].append((priority, label, callback))
        self._hooks[hook_point].sort(key=lambda x: x[0])
        logger.debug("Hook registered: %s → %s (priority=%d)", hook_point, label, priority)

    async def fire(self, hook_point: str, **kwargs: Any) -> Any:
        """Fire all callbacks for a hook point.

        Returns the last non-None result (for pre/post_turn message modification).
        Errors in individual hooks are logged but don't propagate.
        """
        hooks = self._hooks.get(hook_point)
        if not hooks:
            return None

        result = None
        for priority, label, callback in hooks:
            try:
                ret = await callback(**kwargs)
                if ret is not None:
                    result = ret
            except Exception as e:
                logger.warning("Hook %s:%s failed: %s", hook_point, label, e)

        return result

    def register_plugin(self, plugin) -> None:
        """Auto-register all lifecycle hooks from a Plugin instance."""
        from qanot.plugins.base import Plugin

        hook_methods = {
            "on_pre_turn": "on_pre_turn",
            "on_post_turn": "on_post_turn",
            "on_tool_use": "on_tool_use",
            "on_error": "on_error",
            "on_startup": "on_startup",
            "on_shutdown": "on_shutdown",
            "on_compaction": "on_compaction",
        }

        plugin_name = getattr(plugin, "name", type(plugin).__name__)

        for hook_point, method_name in hook_methods.items():
            method = getattr(plugin, method_name, None)
            if method is None:
                continue
            # Skip if it's the default empty implementation
            try:
                if method.__func__ is getattr(Plugin, method_name):
                    continue
            except AttributeError:
                continue

            self.register(hook_point, method, name=f"plugin:{plugin_name}")

    @property
    def summary(self) -> dict[str, int]:
        """Return count of registered hooks per point."""
        return {hp: len(hooks) for hp, hooks in self._hooks.items() if hooks}
