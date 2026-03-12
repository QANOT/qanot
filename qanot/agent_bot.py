"""Lightweight per-agent Telegram bot for multi-bot architecture.

Each AgentDefinition with a bot_token gets its own AgentBot instance
running alongside the main TelegramAdapter. Agent bots:
- Process messages through a dedicated Agent with agent-specific config
- Share the project board with the main agent and other agent bots
- Can delegate to other agents via delegation tools
- Have their own conversation isolation per user
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.types import Message

if TYPE_CHECKING:
    from qanot.config import AgentDefinition, Config
    from qanot.agent import Agent, ToolRegistry
    from qanot.providers.base import LLMProvider

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000


class AgentBot:
    """Lightweight Telegram bot for a single agent definition.

    Unlike the full TelegramAdapter, this uses a simple blocked response mode
    (wait for full response, then send). No streaming, no scheduler, no cron —
    just a focused agent that answers messages.
    """

    def __init__(
        self,
        agent_def: AgentDefinition,
        config: Config,
        provider: LLMProvider,
        parent_registry: ToolRegistry,
    ):
        self.agent_def = agent_def
        self.config = config
        self.bot = Bot(token=agent_def.bot_token)
        self.dp = Dispatcher()
        self._agent: Agent | None = None
        self._provider = provider
        self._parent_registry = parent_registry
        self._bot_username: str = ""  # Resolved on first message
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        """Register message handlers."""
        @self.dp.message(F.text.startswith("/start"))
        async def handle_start(message: Message) -> None:
            await self._handle_start(message)

        @self.dp.message(F.text.startswith("/reset"))
        async def handle_reset(message: Message) -> None:
            await self._handle_reset(message)

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.photo)
        async def handle_photo(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.voice)
        async def handle_voice(message: Message) -> None:
            await self._handle_message(message)

        @self.dp.message(F.document)
        async def handle_document(message: Message) -> None:
            await self._handle_message(message)

    def _is_allowed(self, user_id: int) -> bool:
        """Check if user is allowed to use this agent bot."""
        if not self.config.allowed_users:
            return True
        return user_id in self.config.allowed_users

    async def _handle_start(self, message: Message) -> None:
        """Handle /start command."""
        if not self._is_allowed(message.from_user.id):
            return
        name = self.agent_def.name or self.agent_def.id
        await message.answer(
            f"Salom! Men <b>{name}</b> agentiman. Menga vazifa bering.\n\n"
            f"/reset — suhbatni tozalash",
            parse_mode=ParseMode.HTML,
        )

    async def _handle_reset(self, message: Message) -> None:
        """Handle /reset — clear conversation."""
        if not self._is_allowed(message.from_user.id):
            return
        if self._agent:
            user_id = str(message.from_user.id)
            self._agent.reset(user_id)
        await message.answer("Suhbat tozalandi.")

    async def _resolve_bot_username(self) -> str:
        """Resolve and cache this bot's username."""
        if not self._bot_username:
            try:
                me = await self.bot.get_me()
                self._bot_username = (me.username or "").lower()
            except Exception:
                pass
        return self._bot_username

    def _is_group(self, message: Message) -> bool:
        """Check if message is from a group/supergroup chat."""
        return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    async def _is_mentioned(self, message: Message) -> bool:
        """Check if this bot is mentioned in the message (for group filtering)."""
        text = message.text or message.caption or ""
        username = await self._resolve_bot_username()

        # Check @username mention in text
        if username and f"@{username}" in text.lower():
            return True

        # Check if message is a reply to this bot's message
        if message.reply_to_message and message.reply_to_message.from_user:
            me = await self.bot.get_me()
            if message.reply_to_message.from_user.id == me.id:
                return True

        return False

    async def _handle_message(self, message: Message) -> None:
        """Process an incoming message through the agent."""
        if not message.from_user:
            return
        if not self._is_allowed(message.from_user.id):
            return

        # In groups: only respond when mentioned or replied to
        if self._is_group(message):
            if not await self._is_mentioned(message):
                return

        user_id = str(message.from_user.id)
        chat_id = message.chat.id
        text = message.text or message.caption or ""

        # Strip @bot_username from the text
        username = await self._resolve_bot_username()
        if username:
            text = text.replace(f"@{username}", "").replace(f"@{username.upper()}", "").strip()

        if not text.strip():
            return

        # Show typing indicator
        typing_task = asyncio.create_task(self._typing_loop(chat_id))

        try:
            agent = self._ensure_agent()
            result = await agent.run_turn(text, user_id=user_id, chat_id=chat_id)
            await self._send_response(chat_id, result)
        except Exception as e:
            logger.error(
                "AgentBot '%s' error for user %s: %s",
                self.agent_def.id, user_id, e, exc_info=True,
            )
            await self._send_response(
                chat_id, "Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.",
            )
        finally:
            typing_task.cancel()

    def _ensure_agent(self) -> Agent:
        """Lazily create the Agent instance on first use."""
        if self._agent is not None:
            return self._agent

        from qanot.agent import Agent, ToolRegistry
        from qanot.context import ContextTracker
        from qanot.session import SessionWriter
        from qanot.tools.builtin import register_builtin_tools
        from qanot.tools.delegate import register_delegate_tools

        # Create agent-specific provider if model/provider differs
        provider = self._create_agent_provider()

        # Build tool registry: start from parent, apply allow/deny
        tool_registry = self._build_tool_registry()

        # Context tracker
        context = ContextTracker(
            max_tokens=self.config.max_context_tokens,
            workspace_dir=self.config.workspace_dir,
        )

        # Session writer (agent-specific prefix)
        session = SessionWriter(self.config.sessions_dir)

        # Build system prompt from agent identity or config
        from qanot.tools.delegate import _load_agent_identity
        identity = _load_agent_identity(self.config.workspace_dir, self.agent_def.id)
        system_prompt = identity or self.agent_def.prompt or (
            f"You are {self.agent_def.name or self.agent_def.id}. "
            f"Complete tasks assigned to you."
        )

        # Create agent with custom system prompt
        agent = Agent(
            config=self._make_agent_config(),
            provider=provider,
            tool_registry=tool_registry,
            session=session,
            context=context,
            prompt_mode="minimal",
            system_prompt_override=system_prompt,
        )

        # Register delegate tools so this agent can talk to others
        register_delegate_tools(
            tool_registry, self.config, self._provider, self._parent_registry,
            get_user_id=lambda: agent.current_user_id,
        )

        self._agent = agent
        logger.info(
            "AgentBot '%s' agent initialized (model=%s, provider=%s)",
            self.agent_def.id,
            self.agent_def.model or self.config.model,
            self.agent_def.provider or self.config.provider,
        )
        return agent

    def _create_agent_provider(self) -> LLMProvider:
        """Create LLM provider for this agent (reuse main if no overrides)."""
        if not self.agent_def.model and not self.agent_def.provider:
            return self._provider

        from qanot.providers.failover import ProviderProfile, _create_single_provider

        profile = ProviderProfile(
            name=f"agent_{self.agent_def.id}",
            provider_type=self.agent_def.provider or self.config.provider,
            api_key=self.agent_def.api_key or self.config.api_key,
            model=self.agent_def.model or self.config.model,
        )
        return _create_single_provider(profile)

    def _build_tool_registry(self) -> ToolRegistry:
        """Build filtered tool registry based on agent's allow/deny lists."""
        from qanot.tools.delegate import _build_delegate_registry

        return _build_delegate_registry(
            self._parent_registry,
            depth=1,  # Agent bots can delegate once more (depth 1 of max 2)
            tools_allow=self.agent_def.tools_allow or None,
            tools_deny=self.agent_def.tools_deny or None,
        )

    def _make_agent_config(self) -> Config:
        """Create a Config copy with agent-specific overrides for prompt building."""
        import dataclasses

        overrides = {}
        if self.agent_def.model:
            overrides["model"] = self.agent_def.model
        if self.agent_def.provider:
            overrides["provider"] = self.agent_def.provider

        # Use agent's prompt as bot_name context for system prompt
        if self.agent_def.name:
            overrides["bot_name"] = self.agent_def.name

        return dataclasses.replace(self.config, **overrides)

    async def _send_response(self, chat_id: int, text: str) -> None:
        """Send response, splitting if too long."""
        if not text:
            return

        # Import the formatting helper from telegram module
        from qanot.telegram import _md_to_html, _sanitize_response

        text = _sanitize_response(text)

        if len(text) <= MAX_MSG_LEN:
            try:
                html = _md_to_html(text)
                await self.bot.send_message(
                    chat_id=chat_id, text=html,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                # Fallback to plain text
                await self.bot.send_message(chat_id=chat_id, text=text)
        else:
            # Split long messages
            chunks = [text[i:i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]
            for chunk in chunks:
                try:
                    html = _md_to_html(chunk)
                    await self.bot.send_message(
                        chat_id=chat_id, text=html,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    await self.bot.send_message(chat_id=chat_id, text=chunk)

    async def _typing_loop(self, chat_id: int) -> None:
        """Send typing indicator until cancelled."""
        try:
            while True:
                await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except (asyncio.CancelledError, Exception):
            pass

    async def start(self) -> None:
        """Start polling for this agent bot."""
        logger.info(
            "AgentBot '%s' (%s) starting polling...",
            self.agent_def.id, self.agent_def.name or self.agent_def.id,
        )
        try:
            await self.dp.start_polling(self.bot, drop_pending_updates=True)
        finally:
            await self.bot.session.close()

    async def stop(self) -> None:
        """Stop this agent bot."""
        logger.info("AgentBot '%s' stopping...", self.agent_def.id)
        await self.dp.stop_polling()
        await self.bot.session.close()


async def start_agent_bots(
    config: Config,
    provider: LLMProvider,
    parent_registry: ToolRegistry,
) -> list[AgentBot]:
    """Start all agent bots that have bot_token configured.

    Returns list of started AgentBot instances for cleanup.
    Each agent bot runs as a background task.
    """
    bots: list[AgentBot] = []

    for agent_def in config.agents:
        if not agent_def.bot_token:
            continue

        agent_bot = AgentBot(
            agent_def=agent_def,
            config=config,
            provider=provider,
            parent_registry=parent_registry,
        )
        bots.append(agent_bot)

        # Start polling in background
        asyncio.create_task(
            agent_bot.start(),
            name=f"agent_bot_{agent_def.id}",
        )
        logger.info(
            "Agent bot launched: %s (%s)",
            agent_def.id, agent_def.name or agent_def.id,
        )

    if bots:
        logger.info("Started %d agent bot(s)", len(bots))

    return bots
