"""Tests for Telegram commands (/reset, /status, /help) and proactive delivery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qanot.config import Config


# ── Helpers ──────────────────────────────────────────────────


def _make_message(*, user_id=12345, chat_id=67890, text=None, sticker=None, from_user=True):
    """Create a mock Message object."""
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.sticker = sticker
    msg.photo = None
    msg.document = None
    msg.voice = None
    msg.video_note = None
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.message_id = 999
    # Default to base-view (no thread). _conv_key now treats truthy
    # message_thread_id as a thread-scoped private chat (Bot API 10.0),
    # so a MagicMock default would silently change the conv key.
    msg.message_thread_id = None
    if from_user:
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
    else:
        msg.from_user = None
    return msg


# ── /reset Command ──────────────────────────────────────────


class TestHandleReset:
    @pytest.mark.asyncio
    async def test_reset_clears_conversation(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[12345])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.agent = MagicMock()
        adapter.agent.reset = MagicMock()
        adapter.bot = AsyncMock()
        adapter._send_final = AsyncMock()

        message = _make_message(user_id=12345)

        await adapter._handle_reset(message)

        adapter.agent.reset.assert_called_once_with("12345")
        adapter._send_final.assert_called_once()
        call_args = adapter._send_final.call_args[0]
        assert call_args[0] == 67890  # chat_id
        assert "tozalandi" in call_args[1].lower()

    @pytest.mark.asyncio
    async def test_reset_blocked_user(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[99999])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.agent = MagicMock()
        adapter._send_final = AsyncMock()

        message = _make_message(user_id=12345)

        await adapter._handle_reset(message)

        adapter.agent.reset.assert_not_called()
        adapter._send_final.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_no_from_user(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.agent = MagicMock()
        adapter._send_final = AsyncMock()

        message = _make_message(from_user=False)

        await adapter._handle_reset(message)

        adapter.agent.reset.assert_not_called()


# ── /status Command ─────────────────────────────────────────


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_status_shows_session_info(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[], provider="anthropic", model="claude-sonnet-4-6")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.agent = MagicMock()
        adapter.agent.context = MagicMock()
        adapter.agent.context.session_status.return_value = {
            "context_percent": 42.5,
            "total_tokens": 15000,
            "turn_count": 7,
            "buffer_active": False,
        }
        adapter.agent._conversations = {"12345": [{"role": "user"}, {"role": "assistant"}]}
        # Mock provider with status() for failover info
        mock_provider = MagicMock()
        mock_provider.status.return_value = [
            {"name": "claude-main", "model": "claude-sonnet-4-6", "available": True, "active": True, "last_error": ""},
            {"name": "gemini-backup", "model": "gemini-2.5-flash", "available": True, "active": False, "last_error": ""},
        ]
        adapter.agent.provider = mock_provider
        adapter._send_final = AsyncMock()

        message = _make_message(user_id=12345)

        await adapter._handle_status(message)

        adapter._send_final.assert_called_once()
        status_text = adapter._send_final.call_args[0][1]
        assert "42.5%" in status_text
        assert "15,000" in status_text
        assert "7" in status_text
        assert "claude-main" in status_text
        assert "gemini-backup" in status_text

    @pytest.mark.asyncio
    async def test_status_blocked_user(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[99999])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        message = _make_message(user_id=12345)

        await adapter._handle_status(message)

        adapter._send_final.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_no_from_user(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        message = _make_message(from_user=False)

        await adapter._handle_status(message)

        adapter._send_final.assert_not_called()


# ── /help Command ────────────────────────────────────────────


class TestHandleHelp:
    @pytest.mark.asyncio
    async def test_help_shows_commands(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        message = _make_message(user_id=12345)

        await adapter._handle_help(message)

        adapter._send_final.assert_called_once()
        help_text = adapter._send_final.call_args[0][1]
        assert "/reset" in help_text
        assert "/status" in help_text
        assert "/help" in help_text

    @pytest.mark.asyncio
    async def test_help_blocked_user(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[99999])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        message = _make_message(user_id=12345)

        await adapter._handle_help(message)

        adapter._send_final.assert_not_called()


# ── Proactive Delivery ───────────────────────────────────────


class TestDeliverProactive:
    @pytest.mark.asyncio
    async def test_deliver_to_owner(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[12345])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        await adapter._deliver_proactive("Disk space low", source="heartbeat")

        adapter._send_final.assert_called_once()
        call_args = adapter._send_final.call_args[0]
        assert call_args[0] == 12345  # owner_id
        assert "#agent" in call_args[1]
        assert "#heartbeat" in call_args[1]
        assert "Disk space low" in call_args[1]

    @pytest.mark.asyncio
    async def test_deliver_without_source(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[12345])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        await adapter._deliver_proactive("General update")

        call_args = adapter._send_final.call_args[0]
        assert call_args[1] == "#agent\nGeneral update"

    @pytest.mark.asyncio
    async def test_deliver_no_allowed_users_drops_message(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock()

        await adapter._deliver_proactive("This should be dropped")

        adapter._send_final.assert_not_called()

    @pytest.mark.asyncio
    async def test_deliver_send_failure_handled(self):
        from qanot.telegram import TelegramAdapter

        config = Config(bot_token="123:FAKE", allowed_users=[12345])
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter._send_final = AsyncMock(side_effect=Exception("send failed"))

        # Should not raise
        await adapter._deliver_proactive("Important alert", source="cron")


class TestStopCancelsTurn:
    """/stop cancels the in-flight agent turn (#3)."""

    def _adapter(self):
        from qanot.telegram import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._active_turns = {}
        adapter._pending_approvals = {}
        adapter._send_final = AsyncMock()
        adapter._check_command_access = lambda m: True
        adapter._conv_key = lambda m: str(m.from_user.id)
        return adapter

    @pytest.mark.asyncio
    async def test_stop_cancels_active_turn(self):
        import asyncio

        adapter = self._adapter()
        started = asyncio.Event()

        async def long_turn():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(long_turn())
        adapter._active_turns["12345"] = task
        await started.wait()

        await adapter._handle_stop(_make_message(user_id=12345))

        # The turn task was cancelled and the confirmation was sent.
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        sent = adapter._send_final.call_args[0][1]
        assert "To'xtatildi" in sent

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running(self):
        adapter = self._adapter()
        await adapter._handle_stop(_make_message(user_id=12345))
        sent = adapter._send_final.call_args[0][1]
        assert "Hech narsa" in sent


class TestAskUserClarify:
    """ask_user tool + request_clarification button flow (#6)."""

    @pytest.mark.asyncio
    async def test_ask_user_tool_returns_choice(self):
        import json
        from qanot.registry import ToolRegistry
        from qanot.tools.builtin import register_builtin_tools
        from qanot.context import ContextTracker

        async def clar(uid, q, opts):
            assert uid == "1" and q == "Qaysi?" and opts == ["A", "B"]
            return opts[1]

        r = ToolRegistry()
        register_builtin_tools(
            r, "/tmp", ContextTracker(max_tokens=100000),
            get_user_id=lambda: "1", clarify_callback=clar,
        )
        out = await r.execute("ask_user", {"question": "Qaysi?", "options": ["A", "B"]})
        assert json.loads(out)["answer"] == "B"

    @pytest.mark.asyncio
    async def test_ask_user_rejects_too_few_options(self):
        import json
        from qanot.registry import ToolRegistry
        from qanot.tools.builtin import register_builtin_tools
        from qanot.context import ContextTracker

        r = ToolRegistry()
        register_builtin_tools(
            r, "/tmp", ContextTracker(max_tokens=100000),
            clarify_callback=lambda *a: None,
        )
        out = await r.execute("ask_user", {"question": "x", "options": ["only one"]})
        assert "error" in json.loads(out)

    @pytest.mark.asyncio
    async def test_request_clarification_resolves_on_button(self):
        import asyncio
        from qanot.telegram import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._pending_clarifications = {}
        adapter.agent = type("A", (), {"current_chat_id": 555, "current_thread_id": None})()

        class _Bot:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kw):
                self.sent.append(kw)
                return type("M", (), {"message_id": 1})()

        adapter.bot = _Bot()

        task = asyncio.create_task(
            adapter.request_clarification("123", "Qaysi fayl?", ["a.txt", "b.txt"])
        )
        # let the coroutine send the buttons + register the pending future
        for _ in range(5):
            await asyncio.sleep(0)
            if adapter._pending_clarifications:
                break
        assert len(adapter._pending_clarifications) == 1
        assert any("Qaysi fayl?" in str(k.get("text", "")) for k in adapter.bot.sent)
        clar_id = next(iter(adapter._pending_clarifications))

        cb = MagicMock()
        cb.from_user.id = 123
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        await adapter._cb_clarify(cb, f"clarify:{clar_id}:1", 123)

        result = await task
        assert result == "b.txt"
        assert adapter._pending_clarifications == {}  # cleaned up

    @pytest.mark.asyncio
    async def test_clarify_wrong_user_cannot_answer(self):
        import asyncio
        from qanot.telegram import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter._pending_clarifications = {
            "abc": {"user_id": 111, "question": "q", "options": ["x", "y"],
                    "future": asyncio.get_event_loop().create_future()},
        }
        cb = MagicMock()
        cb.answer = AsyncMock()
        await adapter._cb_clarify(cb, "clarify:abc:0", 999)  # different user
        # future not resolved, pending kept
        assert not adapter._pending_clarifications["abc"]["future"].done()
        cb.answer.assert_awaited()
