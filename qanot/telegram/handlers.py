"""Telegram command handlers — composed from focused mixin modules.

Split into:
  - lifecycle_handlers.py: start, reset, resume, compact, export, stop, approvals, callback router
  - settings_handlers.py: model, think, voice, routing, group, exec, code, plugins
  - info_handlers.py: status, help, context, usage, id, config
  - integration_handlers.py: joincall, leavecall, callstatus, mcp
"""

from __future__ import annotations

from qanot.telegram.lifecycle_handlers import LifecycleHandlersMixin
from qanot.telegram.settings_handlers import SettingsHandlersMixin, THINKING_LEVELS
from qanot.telegram.info_handlers import InfoHandlersMixin
from qanot.telegram.integration_handlers import IntegrationHandlersMixin


class HandlersMixin(
    LifecycleHandlersMixin,
    SettingsHandlersMixin,
    InfoHandlersMixin,
    IntegrationHandlersMixin,
):
    """Composed mixin providing all command handler methods for TelegramAdapter."""
    pass


__all__ = ["HandlersMixin", "THINKING_LEVELS"]
