"""Tests for group orchestration — visible multi-agent collaboration.

Tests the LoopGuard, GroupOrchestrator, and delegate_to_group tool.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.config import AgentDefinition, Config
from qanot.orchestrator.loop_guard import LoopGuard
from qanot.orchestrator.group import GroupOrchestrator
from qanot.registry import ToolRegistry


# ──────────────────────────── fixtures / helpers ────────────────────────────


def _make_config(tmp_path, **overrides) -> Config:
    defaults = dict(
        bot_token="test-bot-token",
        api_key="test-key",
        workspace_dir=str(tmp_path),
        sessions_dir=str(tmp_path / "sessions"),
        group_orchestration=True,
        orchestration_group_id=-1001234567890,
        bot_to_bot_max_depth=5,
        bot_to_bot_cooldown=2.0,
        bot_to_bot_chain_timeout=300,
        agents=[
            AgentDefinition(
                id="researcher",
                name="Tadqiqotchi",
                bot_token="researcher-bot-token",
            ),
            AgentDefinition(
                id="coder",
                name="Dasturchi",
                bot_token="coder-bot-token",
            ),
            AgentDefinition(
                id="internal-only",
                name="Internal",
                bot_token="",  # No bot token — can't do group delegation
            ),
        ],
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_message(
    *,
    text: str = "Hello",
    from_id: int = 123,
    is_bot: bool = False,
    chat_id: int = -1001234567890,
    message_id: int = 1,
    reply_to_message: SimpleNamespace | None = None,
) -> SimpleNamespace:
    """Build a fake aiogram Message."""
    msg = SimpleNamespace()
    msg.from_user = SimpleNamespace(
        id=from_id,
        is_bot=is_bot,
        full_name="TestBot" if is_bot else "TestUser",
        username="testbot" if is_bot else "testuser",
    )
    msg.chat = SimpleNamespace(id=chat_id, type="supergroup")
    msg.text = text
    msg.caption = None
    msg.message_id = message_id
    msg.reply_to_message = reply_to_message
    msg.reply_to_message_id = (
        reply_to_message.message_id if reply_to_message else None
    )
    return msg


def _make_agent_bot(agent_id: str, username: str, bot_id: int) -> SimpleNamespace:
    """Build a fake AgentBot."""
    ab = SimpleNamespace()
    ab.agent_def = AgentDefinition(
        id=agent_id,
        name=agent_id.title(),
        bot_token=f"{agent_id}-token",
    )
    ab._bot_username = username
    ab._bot_id = bot_id
    ab.bot = MagicMock()
    ab.bot.send_message = AsyncMock()
    ab.group_orchestrator = None

    async def _resolve():
        return username

    ab._resolve_bot_username = _resolve
    return ab


def _make_group_orchestrator(tmp_path) -> tuple[GroupOrchestrator, LoopGuard]:
    """Build a GroupOrchestrator with fake bots."""
    config = _make_config(tmp_path)
    loop_guard = LoopGuard(
        max_depth=5,
        cooldown_seconds=2.0,
        chain_timeout_seconds=300,
    )

    main_bot = MagicMock()
    main_bot.send_message = AsyncMock(
        return_value=SimpleNamespace(
            message_id=100,
            chat=SimpleNamespace(id=-1001234567890),
            text="delegated task",
            from_user=SimpleNamespace(id=999, is_bot=True),
            reply_to_message=None,
            reply_to_message_id=None,
        ),
    )
    main_bot._id = 999

    researcher_bot = _make_agent_bot("researcher", "researcherbot", 1001)
    coder_bot = _make_agent_bot("coder", "coderbot", 1002)

    agent_bots = {
        "researcher": researcher_bot,
        "coder": coder_bot,
    }

    orchestrator = GroupOrchestrator(
        config=config,
        main_bot=main_bot,
        agent_bots=agent_bots,
        loop_guard=loop_guard,
    )
    return orchestrator, loop_guard


# ──────────────────────────── LoopGuard tests ────────────────────────────


class TestLoopGuard:
    """Test the bot-to-bot loop prevention system."""

    def test_allows_first_message(self):
        """Fresh guard allows first bot response."""
        guard = LoopGuard(max_depth=5, cooldown_seconds=2.0)
        msg = _make_message(is_bot=True, from_id=1001)
        allowed, reason = guard.should_respond(msg, my_bot_id=2001)
        assert allowed is True
        assert reason == ""

    def test_blocks_at_max_depth(self):
        """Chain at max depth should be blocked."""
        guard = LoopGuard(max_depth=3, cooldown_seconds=0)

        # Build a chain of 3 bot messages
        guard._record_message(1, 1001, time.time(), None)
        guard._record_message(2, 1002, time.time(), 1)
        guard._record_message(3, 1001, time.time(), 2)

        # Message replying to message 3 (depth = 3)
        reply_to = SimpleNamespace(message_id=3)
        msg = _make_message(
            is_bot=True, from_id=1002, message_id=4,
            reply_to_message=reply_to,
        )

        allowed, reason = guard.should_respond(msg, my_bot_id=2002)
        assert allowed is False
        assert "depth" in reason

    def test_cooldown_enforced(self):
        """Same bot responding within cooldown should be blocked."""
        guard = LoopGuard(max_depth=10, cooldown_seconds=2.0)
        chat_id = -1001234567890
        my_bot_id = 2001

        # Record a recent response
        guard._last_response_time[(chat_id, my_bot_id)] = time.time()

        msg = _make_message(is_bot=True, from_id=1001, chat_id=chat_id)
        allowed, reason = guard.should_respond(msg, my_bot_id=my_bot_id)
        assert allowed is False
        assert "cooldown" in reason

    def test_cooldown_expired_allows(self):
        """Same bot after cooldown period should be allowed."""
        guard = LoopGuard(max_depth=10, cooldown_seconds=2.0)
        chat_id = -1001234567890
        my_bot_id = 2001

        # Record an old response (3s ago, cooldown is 2s)
        guard._last_response_time[(chat_id, my_bot_id)] = time.time() - 3.0

        msg = _make_message(is_bot=True, from_id=1001, chat_id=chat_id)
        allowed, reason = guard.should_respond(msg, my_bot_id=my_bot_id)
        assert allowed is True

    def test_dedup_blocks_identical(self):
        """Same content from same bot should be blocked."""
        guard = LoopGuard(max_depth=10, cooldown_seconds=0)

        # Record a hash for "Hello" from bot 1001
        content_hash = guard._hash_content(1001, "Hello")
        guard._recent_hashes[content_hash] = time.time()

        msg = _make_message(is_bot=True, from_id=1001, text="Hello")
        allowed, reason = guard.should_respond(msg, my_bot_id=2001)
        assert allowed is False
        assert "dedup" in reason

    def test_chain_timeout(self):
        """Chain older than timeout should be blocked."""
        guard = LoopGuard(max_depth=10, cooldown_seconds=0, chain_timeout_seconds=300)

        # Record a root message from 6 minutes ago
        old_time = time.time() - 360
        guard._record_message(1, 1001, old_time, None)

        reply_to = SimpleNamespace(message_id=1)
        msg = _make_message(
            is_bot=True, from_id=1002, message_id=2,
            reply_to_message=reply_to,
        )

        allowed, reason = guard.should_respond(msg, my_bot_id=2002)
        assert allowed is False
        assert "timeout" in reason

    def test_human_messages_always_pass(self):
        """Human messages (is_bot=False) should never be blocked."""
        guard = LoopGuard(max_depth=0, cooldown_seconds=999)

        # Even with absurd settings, human messages pass
        msg = _make_message(is_bot=False, from_id=12345)
        allowed, reason = guard.should_respond(msg, my_bot_id=2001)
        assert allowed is True
        assert reason == ""

    def test_cleanup_removes_expired(self):
        """Cleanup should remove expired dedup hashes."""
        guard = LoopGuard()

        # Add an old hash (6 minutes ago, window is 5 min)
        old_hash = "abc123"
        guard._recent_hashes[old_hash] = time.time() - 400

        # Add a fresh hash
        fresh_hash = "def456"
        guard._recent_hashes[fresh_hash] = time.time()

        guard._cleanup_expired()

        assert old_hash not in guard._recent_hashes
        assert fresh_hash in guard._recent_hashes

    def test_track_incoming(self):
        """track_incoming should record message for chain depth."""
        guard = LoopGuard()
        msg = _make_message(is_bot=True, from_id=1001, message_id=42)
        guard.track_incoming(msg)
        assert 42 in guard._message_chain
        assert guard._message_chain[42].from_bot_id == 1001

    def test_track_response(self):
        """track_response should update cooldown and dedup."""
        guard = LoopGuard()
        sent = SimpleNamespace(
            message_id=99,
            chat=SimpleNamespace(id=-100),
            text="response text",
            from_user=SimpleNamespace(id=2001, is_bot=True),
            reply_to_message=None,
            reply_to_message_id=None,
        )
        guard.track_response(sent, bot_id=2001)
        assert (-100, 2001) in guard._last_response_time
        assert 99 in guard._message_chain


# ──────────────────────────── GroupOrchestrator tests ────────────────────────────


class TestGroupOrchestrator:
    """Test the group orchestration routing and delegation."""

    @pytest.mark.asyncio
    async def test_routes_human_to_main_bot(self, tmp_path):
        """Human message with no @mention should not be handled (fall through)."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        msg = _make_message(is_bot=False, text="What is the weather?")
        handled = await orchestrator.route_message(msg)
        assert handled is False

    @pytest.mark.asyncio
    async def test_routes_human_mention_to_agent_bot(self, tmp_path):
        """Human @mention of an agent bot should not be handled (aiogram routes it)."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        msg = _make_message(
            is_bot=False, text="@researcherbot find me some data",
        )
        handled = await orchestrator.route_message(msg)
        # Falls through to agent bot's own handler
        assert handled is False

    @pytest.mark.asyncio
    async def test_routes_bot_message_with_guard(self, tmp_path):
        """Bot message that passes guard should fall through for routing."""
        orchestrator, guard = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        msg = _make_message(
            is_bot=True, from_id=1001, text="@coderbot please implement this",
        )
        handled = await orchestrator.route_message(msg)
        # Falls through to agent bot handler (not handled by orchestrator itself)
        assert handled is False

    @pytest.mark.asyncio
    async def test_blocks_bot_message_failing_guard(self, tmp_path):
        """Bot message that fails guard should still fall through (guard is checked in AgentBot)."""
        orchestrator, guard = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        # Orchestrator routes messages; loop guard enforcement happens in AgentBot._handle_message
        msg = _make_message(is_bot=True, from_id=1001, text="some message")
        handled = await orchestrator.route_message(msg)
        assert handled is False

    @pytest.mark.asyncio
    async def test_delegate_posts_to_group(self, tmp_path):
        """delegate() should send messages to the orchestration group."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        msg_id = await orchestrator.delegate(
            orchestrator.main_bot,
            "researcher",
            "Find information about quantum computing",
        )

        # main_bot.send_message should have been called (announcement + delegation)
        assert orchestrator.main_bot.send_message.call_count >= 1
        assert msg_id == 100  # from our mock return value

    @pytest.mark.asyncio
    async def test_delegate_wait_resolves_on_response(self, tmp_path):
        """delegate_and_wait should resolve when agent replies."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        # Start delegation in background
        async def _delegate():
            return await orchestrator.delegate_and_wait(
                orchestrator.main_bot, "researcher", "test task", timeout=5,
            )

        task = asyncio.create_task(_delegate())

        # Give it a moment to register the future
        await asyncio.sleep(0.1)

        # Simulate agent response by resolving the pending delegation
        # The delegation message_id is 100 (from mock)
        reply_msg = SimpleNamespace(message_id=100)
        response = _make_message(
            is_bot=True, from_id=1001, text="Here are the results",
            message_id=200, reply_to_message=reply_msg,
        )
        await orchestrator.handle_agent_response(response)

        result = await asyncio.wait_for(task, timeout=2)
        assert "results" in result

    @pytest.mark.asyncio
    async def test_get_group_context(self, tmp_path):
        """get_group_context returns recent messages."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        orchestrator._record_message("Alice", "Hello everyone")
        orchestrator._record_message("ResearcherBot", "Working on it")

        context = await orchestrator.get_group_context(limit=5)
        assert "[Alice]: Hello everyone" in context
        assert "[ResearcherBot]: Working on it" in context

    @pytest.mark.asyncio
    async def test_get_agent_bot_by_username(self, tmp_path):
        """Username lookup should find the correct agent bot."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        bot = orchestrator.get_agent_bot_by_username("researcherbot")
        assert bot is not None
        assert bot.agent_def.id == "researcher"

        bot2 = orchestrator.get_agent_bot_by_username("nonexistent")
        assert bot2 is None


# ──────────────────────────── delegate_to_group tool tests ────────────────────────────


class TestDelegateToGroupTool:
    """Test the delegate_to_group tool registration and handler."""

    @pytest.mark.asyncio
    async def test_rejects_agent_without_bot_token(self, tmp_path):
        """Agents without bot_token can't be group-delegated."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        config = _make_config(tmp_path)
        registry = ToolRegistry()

        from qanot.orchestrator.group_tools import register_group_orchestration_tools
        register_group_orchestration_tools(registry, config, orchestrator)

        handler = registry._handlers.get("delegate_to_group")
        assert handler is not None

        result = json.loads(await handler({"agent_id": "internal-only", "task": "test"}))
        assert "error" in result
        assert "bot_token" in result["error"]

    @pytest.mark.asyncio
    async def test_fire_and_forget_returns_immediately(self, tmp_path):
        """wait=False should return status=delegated immediately."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()
        config = _make_config(tmp_path)
        registry = ToolRegistry()

        from qanot.orchestrator.group_tools import register_group_orchestration_tools
        register_group_orchestration_tools(registry, config, orchestrator)

        handler = registry._handlers.get("delegate_to_group")
        result = json.loads(
            await handler({"agent_id": "researcher", "task": "find data", "wait": False}),
        )
        assert result["status"] == "delegated"
        assert result["agent_id"] == "researcher"

    @pytest.mark.asyncio
    async def test_wait_timeout(self, tmp_path):
        """wait=True with no response should return timeout error."""
        config = _make_config(tmp_path, bot_to_bot_chain_timeout=1)
        loop_guard = LoopGuard(max_depth=5, cooldown_seconds=0, chain_timeout_seconds=1)

        main_bot = MagicMock()
        main_bot.send_message = AsyncMock(
            return_value=SimpleNamespace(
                message_id=100,
                chat=SimpleNamespace(id=-1001234567890),
                text="task",
                from_user=SimpleNamespace(id=999, is_bot=True),
                reply_to_message=None,
                reply_to_message_id=None,
            ),
        )

        researcher_bot = _make_agent_bot("researcher", "researcherbot", 1001)
        agent_bots = {"researcher": researcher_bot}

        orchestrator = GroupOrchestrator(
            config=config,
            main_bot=main_bot,
            agent_bots=agent_bots,
            loop_guard=loop_guard,
        )
        await orchestrator.build_username_cache()

        registry = ToolRegistry()
        from qanot.orchestrator.group_tools import register_group_orchestration_tools
        register_group_orchestration_tools(registry, config, orchestrator)

        handler = registry._handlers.get("delegate_to_group")
        result = json.loads(
            await handler({"agent_id": "researcher", "task": "find data", "wait": True}),
        )
        assert result["status"] == "completed"
        assert "vaqti tugadi" in result["result"].lower() or "timeout" in result["result"].lower()

    @pytest.mark.asyncio
    async def test_rejects_missing_agent(self, tmp_path):
        """Unknown agent_id should return error."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        config = _make_config(tmp_path)
        registry = ToolRegistry()

        from qanot.orchestrator.group_tools import register_group_orchestration_tools
        register_group_orchestration_tools(registry, config, orchestrator)

        handler = registry._handlers.get("delegate_to_group")
        result = json.loads(
            await handler({"agent_id": "nonexistent", "task": "test"}),
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_rejects_empty_task(self, tmp_path):
        """Empty task should return error."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        config = _make_config(tmp_path)
        registry = ToolRegistry()

        from qanot.orchestrator.group_tools import register_group_orchestration_tools
        register_group_orchestration_tools(registry, config, orchestrator)

        handler = registry._handlers.get("delegate_to_group")
        result = json.loads(await handler({"agent_id": "researcher", "task": ""}))
        assert "error" in result


# ──────────────────────────── Integration-style test ────────────────────────────


class TestGroupOrchestrationIntegration:
    """Integration-style tests for the full delegation cycle."""

    @pytest.mark.asyncio
    async def test_full_delegation_cycle(self, tmp_path):
        """Human -> main bot -> delegate_to_group -> agent bot -> response in group."""
        orchestrator, loop_guard = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        # Step 1: Human sends message to main bot (route_message returns False)
        human_msg = _make_message(
            is_bot=False, text="Analyze this code", message_id=10,
        )
        handled = await orchestrator.route_message(human_msg)
        assert handled is False  # Falls through to main bot handler

        # Step 2: Main bot delegates to researcher via delegate_to_group
        config = _make_config(tmp_path)
        registry = ToolRegistry()
        from qanot.orchestrator.group_tools import register_group_orchestration_tools
        register_group_orchestration_tools(registry, config, orchestrator)

        handler = registry._handlers.get("delegate_to_group")

        # Fire-and-forget delegation
        result = json.loads(
            await handler({
                "agent_id": "researcher",
                "task": "Research quantum computing",
                "wait": False,
            }),
        )
        assert result["status"] == "delegated"
        delegation_msg_id = result["message_id"]  # 100 from mock

        # Step 3: Researcher bot processes and sends response in group
        reply_to = SimpleNamespace(message_id=delegation_msg_id)
        agent_response = _make_message(
            is_bot=True,
            from_id=1001,
            text="Quantum computing uses qubits for parallel computation.",
            message_id=201,
            reply_to_message=reply_to,
        )

        # Track the response in loop guard
        loop_guard.track_incoming(agent_response)

        # Route the agent response through orchestrator
        handled = await orchestrator.route_message(agent_response)

        # Step 4: Verify the message was tracked
        assert 201 in loop_guard._message_chain

    @pytest.mark.asyncio
    async def test_delegation_wait_full_cycle(self, tmp_path):
        """Full cycle with wait=True: delegate -> agent responds -> future resolved."""
        orchestrator, _ = _make_group_orchestrator(tmp_path)
        await orchestrator.build_username_cache()

        # Start a wait delegation
        async def _do_delegation():
            return await orchestrator.delegate_and_wait(
                orchestrator.main_bot, "coder", "Write a function", timeout=5,
            )

        task = asyncio.create_task(_do_delegation())
        await asyncio.sleep(0.1)

        # Simulate coder bot responding to the delegation message (id=100)
        reply_to = SimpleNamespace(message_id=100)
        response = _make_message(
            is_bot=True, from_id=1002,
            text="def calculate(): return 42",
            message_id=300,
            reply_to_message=reply_to,
        )
        await orchestrator.handle_agent_response(response)

        result = await asyncio.wait_for(task, timeout=2)
        assert "calculate" in result
