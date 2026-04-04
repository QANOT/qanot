"""Monitor group mirroring for agent interactions.

Mirrors agent-to-agent interactions to a Telegram monitoring group.
Each agent posts as its own bot (if it has a bot_token), making it
look like a real chat between bots.

Ported from tools/delegate.py's _mirror_to_group / _send_typing_to_group.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qanot.config import Config

logger = logging.getLogger(__name__)

# Cache Bot instances by token to avoid recreating them.
# Uses setdefault for race-safe lazy init.
_group_bot_cache: dict[str, object] = {}


def _get_agent_bot_token(config: Config, agent_id: str) -> str:
    """Get the bot token for an agent. Falls back to main bot token."""
    for ad in config.agents:
        if ad.id == agent_id and ad.bot_token:
            return ad.bot_token
    return config.bot_token


def _get_agent_name(config: Config, agent_id: str) -> str:
    """Get human-readable name for an agent."""
    if not agent_id or agent_id == "main":
        return config.bot_name or "Main Bot"
    for ad in config.agents:
        if ad.id == agent_id:
            return ad.name or ad.id
    return agent_id


async def _get_group_bot(config: Config, agent_id: str):
    """Get or create an aiogram Bot for posting to group as a specific agent."""
    bot_token = _get_agent_bot_token(config, agent_id)
    bot = _group_bot_cache.get(bot_token)
    if bot is None:
        from aiogram import Bot
        bot = _group_bot_cache.setdefault(bot_token, Bot(token=bot_token))
    return bot


async def send_typing_to_group(config: Config, agent_id: str) -> None:
    """Send typing indicator in the monitoring group as the agent's bot."""
    monitor_group = getattr(config, "monitor_group_id", 0)
    if not monitor_group:
        return
    try:
        bot = await _get_group_bot(config, agent_id)
        await bot.send_chat_action(chat_id=monitor_group, action="typing")
    except Exception as e:
        logger.debug("Typing indicator in monitor group failed: %s", e)


async def mirror_to_group(
    config: Config,
    from_agent: str,
    to_agent: str,
    text: str,
    *,
    direction: str = "message",
) -> None:
    """Mirror an agent interaction message to the monitoring group.

    Posts as the from_agent's own bot (if it has a token), making it
    look like a real chat between bots in the group.

    Args:
        config: Bot configuration
        from_agent: Agent posting the message
        to_agent: Target agent (mentioned in message)
        text: Message content
        direction: "delegate" | "converse" | "turn" | "result" | "message"
    """
    monitor_group = getattr(config, "monitor_group_id", 0)
    if not monitor_group:
        return

    try:
        bot = await _get_group_bot(config, from_agent)

        if direction == "delegate":
            msg = f"\U0001f4cb <i>{_get_agent_name(config, to_agent)}</i> ga vazifa:\n\n{text[:3000]}"
        elif direction == "converse":
            msg = f"\U0001f4ac <i>{_get_agent_name(config, to_agent)}</i> bilan suhbat boshladim:\n\n{text[:3000]}"
        else:
            msg = text[:3500]

        await bot.send_message(
            chat_id=monitor_group,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug("Failed to mirror to monitoring group: %s", e)


async def handle_set_monitor_group(config: Config, params: dict) -> str:
    """Set a Telegram group for live agent monitoring."""
    group_id = params.get("group_id", 0)
    if not group_id:
        return json.dumps({"error": "group_id is required (negative number for groups)"})

    try:
        group_id_int = int(group_id)
    except (ValueError, TypeError):
        return json.dumps({"error": f"group_id must be an integer, got: {type(group_id).__name__}"})

    if group_id_int == 0:
        return json.dumps({"error": "group_id cannot be zero"})

    if group_id_int > 0:
        logger.warning(
            "set_monitor_group called with positive group_id=%d; "
            "Telegram group IDs are typically negative", group_id_int,
        )

    config.monitor_group_id = group_id_int

    # Persist to config.json (atomic, best-effort)
    try:
        from qanot.config import get_config_path, read_config_json, write_config_json
        if Path(get_config_path()).exists():
            raw = read_config_json()
            raw["monitor_group_id"] = config.monitor_group_id
            write_config_json(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to persist monitor_group_id to config: %s", e)
        return json.dumps({
            "status": "partial",
            "monitor_group_id": config.monitor_group_id,
            "warning": f"Group set in memory but failed to persist: {e}",
        })

    return json.dumps({
        "status": "configured",
        "monitor_group_id": config.monitor_group_id,
        "message": (
            f"Monitoring group set to {group_id_int}. "
            "All agent interactions will be mirrored there. "
            "Make sure all agent bots are added to this group."
        ),
    })


def cleanup_bot_cache() -> None:
    """Clear cached Bot instances (call on shutdown)."""
    _group_bot_cache.clear()
