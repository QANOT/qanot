"""Telegram info handlers — status, help, context, usage, id, config."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram.types import Message
    from qanot.agent import Agent
    from qanot.config import Config

logger = logging.getLogger(__name__)


class InfoHandlersMixin:
    """Mixin providing info/diagnostic command handlers for TelegramAdapter."""

    bot: "Bot"
    agent: "Agent"
    config: "Config"

    # ── /status ───────────────────────────────────────────

    async def _handle_status(self, message: "Message") -> None:
        """Handle /status — show session info."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access
        status = self.agent.context.session_status()
        conv = self.agent.get_conversation(conv_key)

        # Provider health info
        provider = self.agent.provider
        provider_info = f"Provider: {self.config.provider}\nModel: {self.config.model}"
        if hasattr(provider, "status"):
            try:
                ps_data = provider.status()
                if isinstance(ps_data, list):
                    lines = []
                    for ps in ps_data:
                        icon = "\U0001f7e2" if ps.get("available") else "\U0001f534"
                        active = " \u25c0" if ps.get("active") else ""
                        err = f" ({ps['last_error']})" if ps.get("last_error") else ""
                        lines.append(f"{icon} {ps['name']} \u2014 {ps['model']}{err}{active}")
                    provider_info = "Providers:\n" + "\n".join(lines)
                elif isinstance(ps_data, dict):
                    stats = ps_data.get("stats", {})
                    provider_info = (
                        f"Routing: {ps_data.get('cheap_model', '?')} / "
                        f"{ps_data.get('primary_model', '?')}\n"
                        f"Savings: {stats.get('savings_pct', 0)}% "
                        f"({stats.get('routed_cheap', 0)} cheap / {stats.get('total', 0)} total)"
                    )
            except Exception as e:
                logger.debug("Failed to get provider stats: %s", e)

        status_text = (
            f"**Session Status**\n\n"
            f"Context: {status['context_percent']}%\n"
            f"Tokens: {status['total_tokens']:,}\n"
            f"Turns: {status['turn_count']}\n"
            f"Messages: {len(conv)}\n"
            f"Buffer: {'active' if status['buffer_active'] else 'inactive'}\n"
            f"{provider_info}"
        )
        await self._send_final(message.chat.id, status_text)

    # ── /help ─────────────────────────────────────────────

    async def _handle_help(self, message: "Message") -> None:
        """Handle /help — show available commands."""
        if not self._check_command_access(message):
            return

        help_text = (
            "**Buyruqlar:**\n\n"
            "**Suhbat:**\n"
            "/reset \u2014 Suhbatni tozalash (+ model: /reset opus)\n"
            "/resume \u2014 Oldingi suhbatni tiklash\n"
            "/compact \u2014 Kontekstni siqish\n"
            "/export \u2014 Sessiyani eksport qilish\n"
            "/joincall \u2014 Ovozli suhbatga qo'shilish\n"
            "/leavecall \u2014 Ovozli suhbatdan chiqish\n"
            "/callstatus \u2014 Qo'ng'iroq holati\n"
            "/stop \u2014 Joriy amalni to'xtatish\n\n"
            "**Sozlamalar:**\n"
            "/model \u2014 Model tanlash\n"
            "/think \u2014 Fikrlash darajasi\n"
            "/voice \u2014 Ovoz sozlamalari\n"
            "/voiceprovider \u2014 Ovoz provayderi\n"
            "/lang \u2014 Til sozlash\n"
            "/mode \u2014 Javob rejimi\n"
            "/routing \u2014 Model routing\n"
            "/group \u2014 Guruh rejimi\n"
            "/topic \u2014 Topic-agent bog'lash\n"
            "/exec \u2014 Xavfsizlik darajasi\n"
            "/code \u2014 Code execution (sandbox)\n\n"
            "**Ma'lumot:**\n"
            "/status \u2014 Sessiya holati\n"
            "/usage \u2014 Token sarfi va narxi\n"
            "/context \u2014 Kontekst tafsilotlari\n"
            "/id \u2014 Foydalanuvchi ID\n"
            "/config \u2014 Barcha sozlamalar\n"
            "/mcp \u2014 MCP serverlar holati\n"
            "/plugins \u2014 Pluginlar boshqaruvi\n"
            "/help \u2014 Shu yordam\n\n"
            "**Imkoniyatlar:**\n"
            "\U0001f4dd Matn \u2014 savol, buyruq, suhbat\n"
            "\U0001f399 Ovozli xabar \u2014 avtomatik transcribe\n"
            "\U0001f4f7 Rasm \u2014 tahlil qilish (vision)\n"
            "\U0001f4ce Fayl \u2014 PDF, doc, excel o\u2018qish\n"
            "\U0001f517 Link \u2014 avtomatik tushunish\n"
            "\U0001f3a8 Rasm yaratish \u2014 \"rasm chiz: ...\" deb yozing\n"
        )
        await self._send_final(message.chat.id, help_text)

    # ── /context ──────────────────────────────────────────

    async def _handle_context(self, message: "Message") -> None:
        """Handle /context — show detailed context usage."""
        access = self._check_command_access(message)
        if not access:
            return
        _, conv_key = access

        ctx = self.agent.context
        status = ctx.session_status()
        conv = self.agent.get_conversation(conv_key)

        # Count tokens by role
        user_msgs = sum(1 for m in conv if m.get("role") == "user")
        asst_msgs = sum(1 for m in conv if m.get("role") == "assistant")
        tool_msgs = sum(1 for m in conv if m.get("role") == "tool")

        pct = status["context_percent"]
        bar_len = 20
        filled = int(pct / 100 * bar_len)
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)

        text = (
            f"**Context Usage**\n\n"
            f"`[{bar}]` {pct}%\n\n"
            f"Tokens: {status['total_tokens']:,} / {ctx.max_tokens:,}\n"
            f"Turns: {status['turn_count']}\n"
            f"Buffer: {'active' if status['buffer_active'] else 'inactive'}\n"
            f"Compacted: {status.get('compacted', False)}\n\n"
            f"**Messages breakdown:**\n"
            f"User: {user_msgs} | Assistant: {asst_msgs} | Tool: {tool_msgs}\n"
            f"Total: {len(conv)}"
        )
        await self._send_final(message.chat.id, text)

    # ── /usage ────────────────────────────────────────────

    async def _handle_usage(self, message: "Message") -> None:
        """Handle /usage — show token cost and usage stats."""
        access = self._check_command_access(message)
        if not access:
            return
        user_id, _ = access

        stats = self.agent.cost_tracker.get_user_stats(str(user_id))
        total_cost = self.agent.cost_tracker.get_total_cost()

        text = (
            f"**Token Usage & Cost**\n\n"
            f"**Sizning sarfingiz:**\n"
            f"Input: {stats['input_tokens']:,} tokens\n"
            f"Output: {stats['output_tokens']:,} tokens\n"
            f"Cache read: {stats['cache_read_tokens']:,}\n"
            f"Cache write: {stats['cache_write_tokens']:,}\n"
            f"API calls: {stats['api_calls']:,}\n"
            f"Turns: {stats['turns']:,}\n"
            f"Cost: ${stats['total_cost']:.4f}\n\n"
            f"**Umumiy:** ${total_cost:.4f}"
        )
        await self._send_final(message.chat.id, text)

    # ── /id ───────────────────────────────────────────────

    async def _handle_id(self, message: "Message") -> None:
        """Handle /id — show user and chat info."""
        if not message.from_user:
            return

        user = message.from_user
        chat = message.chat
        is_owner = user.id in (self.config.allowed_users or [])

        uname = user.username or "yo'q"
        owner_str = "ha" if is_owner else "yo'q"
        title = chat.title or "DM"
        text = (
            f"**Foydalanuvchi:**\n"
            f"ID: `{user.id}`\n"
            f"Ism: {user.full_name}\n"
            f"Username: @{uname}\n"
            f"Owner: {owner_str}\n\n"
            f"**Chat:**\n"
            f"ID: `{chat.id}`\n"
            f"Type: {chat.type}\n"
            f"Title: {title}"
        )
        await self._send_final(message.chat.id, text)

    # ── /config ───────────────────────────────────────────

    async def _handle_config(self, message: "Message") -> None:
        """Handle /config — show all current config (read-only overview)."""
        if not self._check_command_access(message):
            return

        c = self.config
        text = (
            f"**Qanot AI Config**\n\n"
            f"**Model:** `{c.model}`\n"
            f"**Provider:** `{c.provider}`\n"
            f"**Routing:** `{'ON' if c.routing_enabled else 'OFF'}`\n"
            f"**Thinking:** `{c.thinking_level}` (budget: {c.thinking_budget})\n"
            f"**Response mode:** `{c.response_mode}`\n"
            f"**Voice mode:** `{c.voice_mode}`\n"
            f"**Voice provider:** `{c.voice_provider}`\n"
            f"**Voice name:** `{c.voice_name or 'default'}`\n"
            f"**STT language:** `{c.voice_language or 'auto'}`\n"
            f"**Group mode:** `{c.group_mode}`\n"
            f"**Exec security:** `{c.exec_security}`\n"
            f"**Code execution:** `{'ON' if c.code_execution else 'OFF'}`\n"
            f"**RAG:** `{c.rag_mode}` ({'ON' if c.rag_enabled else 'OFF'})\n"
            f"**Compaction:** `{c.compaction_mode}`\n"
            f"**Max context:** `{c.max_context_tokens:,}`\n"
            f"**Reactions:** `{'ON' if c.reactions_enabled else 'OFF'}`\n"
            f"**Dashboard:** `{'ON' if c.dashboard_enabled else 'OFF'}` (:{c.dashboard_port})\n"
            f"**Allowed users:** `{c.allowed_users}`\n"
        )
        await self._send_final(message.chat.id, text)
