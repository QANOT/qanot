"""Telegram integration handlers — voice calls, MCP servers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram.types import Message
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)


class IntegrationHandlersMixin:
    """Mixin providing integration command handlers for TelegramAdapter."""

    bot: "Bot"
    agent: "Agent"
    config: "Config"

    # ── /joincall ─────────────────────────────────────────

    async def _handle_joincall(self, message: "Message") -> None:
        """Handle /joincall — join the group's voice chat as AI participant."""
        access = self._check_command_access(message)
        if not access:
            return
        user_id, conv_key = access

        vcm = getattr(self, "voicecall_manager", None)
        if not vcm:
            await self._send_final(
                message.chat.id,
                "Voice call o'chirilgan. Config'da `voicecall_enabled: true` qiling.",
            )
            return

        if not self._is_group_chat(message):
            await self._send_final(message.chat.id, "Bu buyruq faqat guruhlarda ishlaydi.")
            return

        status = await vcm.join_call(message.chat.id, user_id)
        await self._send_final(message.chat.id, status)

    # ── /leavecall ────────────────────────────────────────

    async def _handle_leavecall(self, message: "Message") -> None:
        """Handle /leavecall — leave the voice chat."""
        if not self._check_command_access(message):
            return

        vcm = getattr(self, "voicecall_manager", None)
        if not vcm:
            await self._send_final(message.chat.id, "Voice call o'chirilgan.")
            return

        status = await vcm.leave_call(message.chat.id)
        await self._send_final(message.chat.id, status)

    # ── /callstatus ───────────────────────────────────────

    async def _handle_callstatus(self, message: "Message") -> None:
        """Handle /callstatus — show active voice call info."""
        if not self._check_command_access(message):
            return

        vcm = getattr(self, "voicecall_manager", None)
        if not vcm:
            await self._send_final(message.chat.id, "Voice call o'chirilgan.")
            return

        active = vcm._active_calls
        if not active:
            await self._send_final(message.chat.id, "Hozir faol qo'ng'iroqlar yo'q.")
            return

        import time as _time
        lines = ["**Faol qo'ng'iroqlar:**"]
        for cid, session in active.items():
            elapsed = int(_time.monotonic() - session.started_at)
            minutes = elapsed // 60
            seconds = elapsed % 60
            speaking = " (gapirmoqda)" if session.is_speaking else ""
            lines.append(f"  Chat `{cid}`: {minutes}:{seconds:02d}{speaking}")
        await self._send_final(message.chat.id, "\n".join(lines))

    # ── /mcp ──────────────────────────────────────────────

    async def _handle_mcp(self, message: "Message") -> None:
        """Handle /mcp — show MCP servers, enable/disable via inline buttons."""
        if not self._check_command_access(message):
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        mcp_mgr = getattr(self, "_mcp_manager", None)
        servers = self.config.mcp_servers or []

        if not servers and not mcp_mgr:
            await self._send_final(
                message.chat.id,
                "\U0001f50c **MCP Serverlar**\n\n"
                "Hech qanday MCP server sozlanmagan.\n\n"
                "config.json ga qo'shing:\n"
                '```\n"mcp_servers": [\n'
                '  {"name": "context7", "command": "uvx", "args": ["context7-mcp"]}\n'
                "]\n```",
            )
            return

        # Build status display
        lines = ["\U0001f50c **MCP Serverlar**\n"]

        connected = mcp_mgr.connected_servers if mcp_mgr else []

        for cfg in servers:
            name = cfg.get("name", "unnamed")
            cmd = cfg.get("command", "?")
            is_connected = name in connected
            icon = "\U0001f7e2" if is_connected else "\U0001f534"

            tool_count = 0
            if mcp_mgr and is_connected:
                srv = mcp_mgr._servers.get(name)
                if srv:
                    tool_count = len(srv.tools)

            tools_str = f" ({tool_count} tools)" if tool_count else ""
            lines.append(f"{icon} **{name}** \u2014 `{cmd}`{tools_str}")

            # List tool names for connected servers
            if mcp_mgr and is_connected:
                srv = mcp_mgr._servers.get(name)
                if srv and srv.tools:
                    tool_names = ", ".join(t["name"] for t in srv.tools[:10])
                    if len(srv.tools) > 10:
                        tool_names += f" +{len(srv.tools) - 10} more"
                    lines.append(f"  \u2514 {tool_names}")

        total = mcp_mgr.total_tools if mcp_mgr else 0
        lines.append(f"\n**Jami:** {len(connected)}/{len(servers)} server, {total} tools")

        await self._send_final(message.chat.id, "\n".join(lines))
