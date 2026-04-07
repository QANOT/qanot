"""Visible multi-agent collaboration in a Telegram group.

When group_orchestration is enabled, agent bots communicate via real
Telegram messages in a shared group. The main bot acts as supervisor,
delegating to specialist agent bots via @mentions. Bot-to-bot
communication uses Telegram's native bot-to-bot feature (April 2026).

This is ADDITIVE to the internal orchestrator (manager.py) — both
can be active simultaneously. Use group orchestration when the user
should see the work happening; use internal orchestration for
background programmatic tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

from qanot.orchestrator.loop_guard import LoopGuard

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message
    from qanot.agent_bot import AgentBot
    from qanot.config import Config
    from qanot.orchestrator.registry import SubagentRegistry

logger = logging.getLogger(__name__)

# Maximum recent messages to keep for context
MAX_CONTEXT_MESSAGES = 50

# Delegation wait timeout (seconds)
DELEGATION_WAIT_TIMEOUT = 300


class GroupOrchestrator:
    """Manages visible multi-agent collaboration in a Telegram group.

    Responsibilities:
    - Route messages to the correct AgentBot (by @mention or reply-to)
    - Track active delegation chains
    - Inject group context into agent prompts
    - Post status announcements (delegation, progress, completion)
    - Aggregate results when main bot collects sub-results
    """

    def __init__(
        self,
        config: Config,
        main_bot: Bot,
        agent_bots: dict[str, AgentBot],
        loop_guard: LoopGuard,
        registry: SubagentRegistry | None = None,
    ):
        self.config = config
        self.main_bot = main_bot
        self.agent_bots = agent_bots
        self.loop_guard = loop_guard
        self.registry = registry

        # @username -> AgentBot mapping (built lazily)
        self._username_cache: dict[str, AgentBot] = {}
        self._username_cache_built = False

        # Recent messages in the group (for context injection)
        self._recent_messages: list[dict[str, str]] = []

        # Pending delegation futures: message_id -> Future[str]
        self._pending_delegations: dict[int, asyncio.Future[str]] = {}

        # Lock for concurrent access
        self._lock = asyncio.Lock()

    async def build_username_cache(self) -> None:
        """Build @username -> AgentBot mapping from running bots."""
        for agent_id, agent_bot in self.agent_bots.items():
            try:
                username = await agent_bot._resolve_bot_username()
                if username:
                    self._username_cache[username.lower()] = agent_bot
                    logger.debug(
                        "Cached group agent: @%s -> %s", username, agent_id,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to resolve username for agent %s: %s",
                    agent_id, e,
                )
        self._username_cache_built = True
        logger.info(
            "Group orchestrator username cache: %d agents",
            len(self._username_cache),
        )

    async def route_message(self, message: Any) -> bool:
        """Route a message arriving in the orchestration group.

        Returns True if handled, False if should fall through to default.
        """
        if not self._username_cache_built:
            await self.build_username_cache()

        from_user = getattr(message, "from_user", None)
        if not from_user:
            return False

        is_bot = getattr(from_user, "is_bot", False)
        text = getattr(message, "text", None) or ""

        # Track every message for chain depth
        self.loop_guard.track_incoming(message)

        # Record for context
        sender_name = getattr(from_user, "full_name", None) or str(
            getattr(from_user, "id", "?"),
        )
        self._record_message(sender_name, text)

        if is_bot:
            return await self._handle_bot_message(message)
        return await self._handle_human_message(message)

    async def _handle_human_message(self, message: Any) -> bool:
        """Handle a message from a human in the orchestration group."""
        text = getattr(message, "text", None) or ""

        # Check if @mentioning a specific agent bot
        mentioned_bot = self._find_mentioned_bot(text)
        if mentioned_bot:
            # Route to the mentioned agent bot — it will handle it
            # via its own message handler (aiogram dispatcher)
            return False  # Let aiogram route to the agent bot's handler

        # No specific mention — let the main bot's default handler take it
        return False

    async def _handle_bot_message(self, message: Any) -> bool:
        """Handle a message from another bot in the orchestration group."""
        from_user = getattr(message, "from_user", None)
        sender_id = getattr(from_user, "id", 0)
        text = getattr(message, "text", None) or ""

        # Check if this resolves a pending delegation
        reply_msg = getattr(message, "reply_to_message", None)
        if reply_msg:
            reply_id = getattr(reply_msg, "message_id", 0)
            await self._resolve_delegation(reply_id, text)

        # Check if this message mentions another bot in our system
        mentioned_bot = self._find_mentioned_bot(text)
        if mentioned_bot:
            # A bot is talking to another bot — let the mentioned bot's
            # dispatcher handle it (if loop guard allows)
            return False  # Fall through to agent bot handlers

        return False

    async def delegate(
        self,
        from_bot: Bot,
        to_agent_id: str,
        task: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Delegate a task to a specific agent bot via the group.

        Sends a @mention message in the orchestration group so the
        target agent bot picks it up via its message handler.

        Returns the message_id of the delegation message.
        """
        target_bot = self.agent_bots.get(to_agent_id)
        if not target_bot:
            raise ValueError(f"Agent bot '{to_agent_id}' not found")

        target_username = await target_bot._resolve_bot_username()
        if not target_username:
            raise ValueError(
                f"Agent bot '{to_agent_id}' has no resolved username",
            )

        group_id = self.config.orchestration_group_id

        # Post delegation announcement
        agent_name = target_bot.agent_def.name or target_bot.agent_def.id
        announcement = (
            f"\U0001f504 <b>{agent_name}</b> (@{target_username}) is working on:\n"
            f"<i>{_truncate(task, 200)}</i>"
        )
        try:
            await from_bot.send_message(
                chat_id=group_id,
                text=announcement,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to post delegation announcement: %s", e)

        # Send the actual task as a message mentioning the target bot
        delegation_text = f"@{target_username} {task}"
        try:
            sent = await from_bot.send_message(
                chat_id=group_id,
                text=delegation_text,
                reply_to_message_id=reply_to_message_id,
            )
            sent_id = getattr(sent, "message_id", 0)

            # Track for loop guard
            self.loop_guard.track_response(sent, getattr(from_bot, "_id", 0))

            return sent_id
        except Exception as e:
            logger.error("Failed to send delegation message: %s", e)
            raise

    async def delegate_and_wait(
        self,
        from_bot: Bot,
        to_agent_id: str,
        task: str,
        timeout: int = DELEGATION_WAIT_TIMEOUT,
    ) -> str:
        """Delegate and block until the agent responds or timeout.

        Returns the agent's response text, or a timeout error message.
        """
        msg_id = await self.delegate(from_bot, to_agent_id, task)

        # Create a future that will be resolved when the agent replies
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        async with self._lock:
            self._pending_delegations[msg_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending_delegations.pop(msg_id, None)
            return (
                f"Kutish vaqti tugadi ({timeout}s). "
                f"Agent '{to_agent_id}' javob bermadi."
            )

    async def handle_agent_response(self, message: Any) -> None:
        """Called when an agent bot sends a response in the group.

        Checks if it resolves a pending delegation wait.
        """
        text = getattr(message, "text", None) or ""
        reply_msg = getattr(message, "reply_to_message", None)
        if reply_msg:
            reply_id = getattr(reply_msg, "message_id", 0)
            await self._resolve_delegation(reply_id, text)

    async def _resolve_delegation(self, reply_to_id: int, result_text: str) -> None:
        """Resolve a pending delegation future if one exists for this reply."""
        async with self._lock:
            future = self._pending_delegations.pop(reply_to_id, None)
        if future and not future.done():
            future.set_result(result_text)

    def get_agent_bot_by_username(self, username: str) -> AgentBot | None:
        """Lookup agent bot by Telegram @username."""
        return self._username_cache.get(username.lower())

    async def get_group_context(self, limit: int = 10) -> str:
        """Return recent group messages as context string.

        Format: "[sender_name]: text" per line, newest last.
        """
        messages = self._recent_messages[-limit:]
        if not messages:
            return ""
        lines = [f"[{m['sender']}]: {m['text']}" for m in messages]
        return "\n".join(lines)

    def _find_mentioned_bot(self, text: str) -> AgentBot | None:
        """Find an agent bot mentioned in the text via @username."""
        text_lower = text.lower()
        for username, agent_bot in self._username_cache.items():
            if f"@{username}" in text_lower:
                return agent_bot
        return None

    def _record_message(self, sender_name: str, text: str) -> None:
        """Record a message for context injection."""
        self._recent_messages.append({
            "sender": sender_name,
            "text": _truncate(text, 500),
            "timestamp": time.time(),
        })
        # Bound the list
        while len(self._recent_messages) > MAX_CONTEXT_MESSAGES:
            self._recent_messages.pop(0)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
