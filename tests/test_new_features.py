"""Tests for new Qanot AI features:
- Sticker handling (telegram.py)
- Telegram commands (/reset, /status, /help)
- Self-healing heartbeat (scheduler.py)
- Plugin manifest (plugins/base.py)
- Plugin loader (plugins/loader.py)
- Proactive delivery (telegram.py)
"""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.config import Config
from qanot.plugins.base import PluginManifest, Plugin, ToolDef, validate_tool_params, tool
from qanot.plugins.loader import PluginManager, _check_required_config
from qanot.scheduler import CronScheduler, _is_heartbeat_ok, HEARTBEAT_OK_TOKEN


# ── Helpers ──────────────────────────────────────────────────


def make_config(tmp_path, **overrides) -> Config:
    kwargs = dict(
        workspace_dir=str(tmp_path / "workspace"),
        sessions_dir=str(tmp_path / "sessions"),
        cron_dir=str(tmp_path / "cron"),
        plugins_dir=str(tmp_path / "plugins"),
        bot_token="123:FAKE",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_sticker(*, is_animated=False, is_video=False, emoji="", thumbnail=None, set_name=""):
    """Create a mock Sticker object."""
    sticker = MagicMock()
    sticker.is_animated = is_animated
    sticker.is_video = is_video
    sticker.emoji = emoji
    sticker.thumbnail = thumbnail
    sticker.set_name = set_name
    sticker.file_id = "sticker_file_id"
    return sticker


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
    msg.message_id = 999
    if from_user:
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
    else:
        msg.from_user = None
    return msg


# ── 1. Sticker Handling ──────────────────────────────────────


class TestDownloadSticker:
    """Tests for TelegramAdapter._download_sticker."""

    @pytest.mark.asyncio
    async def test_static_sticker_downloads_directly(self):
        """Static WEBP stickers should be downloaded via bot.download."""
        from qanot.telegram import TelegramAdapter

        sticker = _make_sticker(is_animated=False, is_video=False, emoji="\U0001f600")
        message = _make_message(sticker=sticker)

        # Create a minimal WEBP-like payload (small enough to skip resize)
        fake_image = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 50

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        async def fake_download(file_obj, destination=None):
            if isinstance(destination, BytesIO):
                destination.write(fake_image)

        adapter.bot.download = fake_download

        with patch.object(TelegramAdapter, '_downscale_image', return_value=(fake_image, "image/webp")):
            result = await adapter._download_sticker(message)

        assert isinstance(result, dict)
        assert result["type"] == "image"
        assert result["source"]["type"] == "base64"
        assert result["source"]["media_type"] == "image/webp"

    @pytest.mark.asyncio
    async def test_animated_sticker_uses_thumbnail(self):
        """Animated TGS stickers should use the thumbnail image."""
        from qanot.telegram import TelegramAdapter

        thumbnail = MagicMock()
        sticker = _make_sticker(is_animated=True, is_video=False, emoji="\U0001f389", thumbnail=thumbnail)
        message = _make_message(sticker=sticker)

        fake_thumb = b'\xff\xd8\xff' + b'\x00' * 100  # JPEG-like

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        async def fake_download(file_obj, destination=None):
            if isinstance(destination, BytesIO):
                destination.write(fake_thumb)

        adapter.bot.download = fake_download

        with patch.object(TelegramAdapter, '_downscale_image', return_value=(fake_thumb, "image/jpeg")):
            result = await adapter._download_sticker(message)

        assert isinstance(result, dict)
        assert result["type"] == "image"
        assert result["source"]["media_type"] == "image/jpeg"
        # Verify thumbnail was passed to download, not the sticker itself
        # (bot.download was called with thumbnail object)

    @pytest.mark.asyncio
    async def test_video_sticker_uses_thumbnail(self):
        """Video WEBM stickers should use the thumbnail image."""
        from qanot.telegram import TelegramAdapter

        thumbnail = MagicMock()
        sticker = _make_sticker(is_animated=False, is_video=True, emoji="\U0001f525", thumbnail=thumbnail)
        message = _make_message(sticker=sticker)

        fake_thumb = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        async def fake_download(file_obj, destination=None):
            if isinstance(destination, BytesIO):
                destination.write(fake_thumb)

        adapter.bot.download = fake_download

        with patch.object(TelegramAdapter, '_downscale_image', return_value=(fake_thumb, "image/png")):
            result = await adapter._download_sticker(message)

        assert isinstance(result, dict)
        assert result["type"] == "image"

    @pytest.mark.asyncio
    async def test_animated_sticker_no_thumbnail_returns_text(self):
        """Animated stickers without thumbnail should return text description."""
        from qanot.telegram import TelegramAdapter

        sticker = _make_sticker(is_animated=True, is_video=False, emoji="\U0001f60e", thumbnail=None)
        message = _make_message(sticker=sticker)

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        result = await adapter._download_sticker(message)

        assert isinstance(result, str)
        assert "Sticker" in result
        assert "\U0001f60e" in result

    @pytest.mark.asyncio
    async def test_no_sticker_returns_none(self):
        """Message without sticker should return None."""
        from qanot.telegram import TelegramAdapter

        message = _make_message(sticker=None)
        message.sticker = None

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()

        result = await adapter._download_sticker(message)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_error_returns_none(self):
        """Download failure should return None gracefully."""
        from qanot.telegram import TelegramAdapter

        sticker = _make_sticker(is_animated=False, is_video=False, emoji="\U0001f4a5")
        message = _make_message(sticker=sticker)

        config = Config(bot_token="123:FAKE")
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.config = config
        adapter.bot = AsyncMock()
        adapter.bot.download = AsyncMock(side_effect=Exception("network error"))

        result = await adapter._download_sticker(message)
        assert result is None


# ── 2. Telegram Commands ─────────────────────────────────────


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


# ── 3. Self-Healing Heartbeat ────────────────────────────────


class TestHeartbeatOkDetection:
    def test_exact_match(self):
        assert _is_heartbeat_ok("HEARTBEAT_OK") is True

    def test_case_insensitive(self):
        assert _is_heartbeat_ok("heartbeat_ok") is True

    def test_with_whitespace(self):
        assert _is_heartbeat_ok("  HEARTBEAT_OK  \n") is True

    def test_with_surrounding_text(self):
        assert _is_heartbeat_ok("Everything fine. HEARTBEAT_OK") is True

    def test_long_text_not_ok(self):
        # Over 300 chars should not be treated as HEARTBEAT_OK
        long_text = "A" * 301 + " HEARTBEAT_OK"
        assert _is_heartbeat_ok(long_text) is False

    def test_no_token_present(self):
        assert _is_heartbeat_ok("All systems nominal") is False

    def test_empty_string(self):
        assert _is_heartbeat_ok("") is False


class TestIdleDetection:
    def test_no_activity_is_idle(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        # No activity recorded yet -> should be idle
        assert sched._is_user_idle() is True

    def test_recent_activity_not_idle(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )

        loop = asyncio.new_event_loop()
        try:
            # Record activity at current time
            sched._last_user_activity = loop.time()
            # Monkey-patch _is_user_idle to use the same loop
            with patch("asyncio.get_event_loop", return_value=loop):
                assert sched._is_user_idle() is False
        finally:
            loop.close()

    def test_old_activity_is_idle(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )

        loop = asyncio.new_event_loop()
        try:
            # Activity was 10 minutes ago (well past 5-minute threshold)
            sched._last_user_activity = loop.time() - 600
            with patch("asyncio.get_event_loop", return_value=loop):
                assert sched._is_user_idle() is True
        finally:
            loop.close()

    def test_record_user_activity(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        assert sched._last_user_activity == 0.0
        loop = asyncio.new_event_loop()
        try:
            with patch("asyncio.get_event_loop", return_value=loop):
                sched.record_user_activity()
                assert sched._last_user_activity > 0
        finally:
            loop.close()


class TestHeartbeatSkipConditions:
    @pytest.mark.asyncio
    async def test_skip_when_user_active(self, tmp_path):
        """Heartbeat should skip if user is currently active."""
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        # Simulate recent activity
        sched._is_user_idle = MagicMock(return_value=False)

        # This should return without calling spawn_isolated_agent
        with patch("qanot.agent.spawn_isolated_agent") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_when_heartbeat_md_empty(self, tmp_path):
        """Heartbeat should skip if HEARTBEAT.md has no actionable content."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        hb_path = ws / "HEARTBEAT.md"
        hb_path.write_text("# Heartbeat Checklist\n\n# Just comments\n")

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_when_heartbeat_md_has_content(self, tmp_path):
        """Heartbeat should run when HEARTBEAT.md has actionable items."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        hb_path = ws / "HEARTBEAT.md"
        hb_path.write_text("# Checklist\n\n- Check disk space\n- Verify backups\n")

        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="HEARTBEAT_OK") as mock_spawn:
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_ok_suppressed(self, tmp_path):
        """HEARTBEAT_OK responses should not be delivered to users."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        hb_path = ws / "HEARTBEAT.md"
        hb_path.write_text("- Check logs\n")

        config = make_config(tmp_path)
        queue = asyncio.Queue()
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
            message_queue=queue,
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="HEARTBEAT_OK"):
            await sched._run_isolated(job_name="heartbeat", prompt="test")

        # Queue should remain empty (HEARTBEAT_OK suppressed)
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_non_heartbeat_job_always_runs(self, tmp_path):
        """Non-heartbeat jobs should not check idle status."""
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=False)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="done") as mock_spawn:
            await sched._run_isolated(job_name="daily_report", prompt="generate report")
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_no_file_runs_normally(self, tmp_path):
        """If HEARTBEAT.md does not exist, heartbeat should run."""
        config = make_config(tmp_path)
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
        )
        sched._is_user_idle = MagicMock(return_value=True)
        # workspace exists but no HEARTBEAT.md
        (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="HEARTBEAT_OK"):
            await sched._run_isolated(job_name="heartbeat", prompt="test")
            # Should have called spawn since file doesn't exist


# ── 4. Plugin Manifest ───────────────────────────────────────


class TestPluginManifest:
    def test_from_file_full(self, tmp_path):
        manifest_data = {
            "name": "mysql_query",
            "version": "1.2.0",
            "description": "MySQL query tool",
            "author": "Sardor",
            "dependencies": ["pymysql"],
            "plugin_deps": ["auth"],
            "required_config": ["db_host", "db_password"],
            "min_qanot_version": "1.0.0",
            "homepage": "https://github.com/example",
            "license": "Apache-2.0",
        }
        manifest_path = tmp_path / "plugin.json"
        manifest_path.write_text(json.dumps(manifest_data))

        result = PluginManifest.from_file(manifest_path)

        assert result.name == "mysql_query"
        assert result.version == "1.2.0"
        assert result.description == "MySQL query tool"
        assert result.author == "Sardor"
        assert result.dependencies == ["pymysql"]
        assert result.plugin_deps == ["auth"]
        assert result.required_config == ["db_host", "db_password"]
        assert result.min_qanot_version == "1.0.0"
        assert result.homepage == "https://github.com/example"
        assert result.license == "Apache-2.0"

    def test_from_file_minimal(self, tmp_path):
        manifest_path = tmp_path / "plugin.json"
        manifest_path.write_text('{"name": "simple"}')

        result = PluginManifest.from_file(manifest_path)

        assert result.name == "simple"
        assert result.version == "0.1.0"
        assert result.dependencies == []
        assert result.plugin_deps == []
        assert result.license == "MIT"

    def test_from_file_missing_name_uses_parent_dir(self, tmp_path):
        plugin_dir = tmp_path / "my_plugin"
        plugin_dir.mkdir()
        manifest_path = plugin_dir / "plugin.json"
        manifest_path.write_text("{}")

        result = PluginManifest.from_file(manifest_path)

        assert result.name == "my_plugin"

    def test_from_file_invalid_json(self, tmp_path):
        manifest_path = tmp_path / "plugin.json"
        manifest_path.write_text("not valid json {{{")

        result = PluginManifest.from_file(manifest_path)

        # Falls back to default with parent dir name
        assert result.name == tmp_path.name

    def test_default_manifest(self):
        result = PluginManifest.default("test_plugin")

        assert result.name == "test_plugin"
        assert result.version == "0.1.0"
        assert result.description == ""
        assert result.dependencies == []
        assert result.plugin_deps == []
        assert result.required_config == []
        assert result.license == "MIT"


class TestValidateToolParams:
    def test_valid_params(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        }
        errors = validate_tool_params({"query": "SELECT 1", "limit": 10}, schema)
        assert errors == []

    def test_missing_required(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        }
        errors = validate_tool_params({}, schema)
        assert len(errors) == 1
        assert "Missing required parameter: query" in errors[0]

    def test_wrong_type(self):
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
        }
        errors = validate_tool_params({"count": "not_a_number"}, schema)
        assert len(errors) == 1
        assert "count" in errors[0]
        assert "integer" in errors[0]

    def test_multiple_errors(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        errors = validate_tool_params({"age": "twenty"}, schema)
        assert len(errors) == 2  # missing 'name' + wrong type 'age'

    def test_extra_params_ignored(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
        }
        errors = validate_tool_params({"name": "test", "extra": 42}, schema)
        assert errors == []

    def test_number_accepts_int_and_float(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
            },
        }
        assert validate_tool_params({"value": 42}, schema) == []
        assert validate_tool_params({"value": 3.14}, schema) == []
        assert len(validate_tool_params({"value": "nope"}, schema)) == 1

    def test_boolean_type(self):
        schema = {
            "type": "object",
            "properties": {
                "flag": {"type": "boolean"},
            },
        }
        assert validate_tool_params({"flag": True}, schema) == []
        assert len(validate_tool_params({"flag": "yes"}, schema)) == 1

    def test_array_type(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array"},
            },
        }
        assert validate_tool_params({"items": [1, 2, 3]}, schema) == []
        assert len(validate_tool_params({"items": "not_array"}, schema)) == 1

    def test_object_type(self):
        schema = {
            "type": "object",
            "properties": {
                "data": {"type": "object"},
            },
        }
        assert validate_tool_params({"data": {"key": "val"}}, schema) == []
        assert len(validate_tool_params({"data": [1]}, schema)) == 1

    def test_unknown_type_passes(self):
        schema = {
            "type": "object",
            "properties": {
                "x": {"type": "custom_type"},
            },
        }
        errors = validate_tool_params({"x": "anything"}, schema)
        assert errors == []

    def test_empty_schema(self):
        errors = validate_tool_params({"key": "value"}, {})
        assert errors == []


# ── 5. Plugin Loader ─────────────────────────────────────────


class TestPluginManager:
    def test_initial_state(self):
        mgr = PluginManager()
        assert mgr.loaded_plugins == {}
        assert mgr.get_plugin("anything") is None
        assert mgr.get_manifest("anything") is None

    @pytest.mark.asyncio
    async def test_shutdown_clears_plugins(self):
        mgr = PluginManager()

        # Manually inject a fake plugin
        plugin = MagicMock(spec=Plugin)
        plugin.teardown = AsyncMock()
        manifest = PluginManifest.default("test")

        mgr._plugins["test"] = plugin
        mgr._manifests["test"] = manifest

        assert mgr.get_plugin("test") is not None

        await mgr.shutdown_all()

        assert mgr.loaded_plugins == {}
        assert mgr.get_plugin("test") is None
        assert mgr.get_manifest("test") is None
        plugin.teardown.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_teardown_error(self):
        mgr = PluginManager()

        plugin = MagicMock(spec=Plugin)
        plugin.teardown = AsyncMock(side_effect=RuntimeError("cleanup failed"))
        mgr._plugins["bad"] = plugin
        mgr._manifests["bad"] = PluginManifest.default("bad")

        # Should not raise
        await mgr.shutdown_all()

        assert mgr.loaded_plugins == {}

    def test_check_plugin_deps_all_present(self):
        mgr = PluginManager()
        mgr._plugins["auth"] = MagicMock()
        mgr._plugins["db"] = MagicMock()

        manifest = PluginManifest(name="test", plugin_deps=["auth", "db"])
        missing = mgr._check_plugin_deps(manifest)
        assert missing == []

    def test_check_plugin_deps_some_missing(self):
        mgr = PluginManager()
        mgr._plugins["auth"] = MagicMock()

        manifest = PluginManifest(name="test", plugin_deps=["auth", "db", "cache"])
        missing = mgr._check_plugin_deps(manifest)
        assert missing == ["db", "cache"]

    def test_check_plugin_deps_none_required(self):
        mgr = PluginManager()
        manifest = PluginManifest(name="test", plugin_deps=[])
        missing = mgr._check_plugin_deps(manifest)
        assert missing == []


class TestCheckRequiredConfig:
    def test_all_present(self):
        manifest = PluginManifest(name="test", required_config=["db_host", "db_port"])
        config = {"db_host": "localhost", "db_port": "5432"}
        missing = _check_required_config(manifest, config)
        assert missing == []

    def test_some_missing(self):
        manifest = PluginManifest(name="test", required_config=["db_host", "db_port", "db_password"])
        config = {"db_host": "localhost"}
        missing = _check_required_config(manifest, config)
        assert "db_port" in missing
        assert "db_password" in missing

    def test_empty_value_counts_as_missing(self):
        manifest = PluginManifest(name="test", required_config=["api_key"])
        config = {"api_key": ""}
        missing = _check_required_config(manifest, config)
        assert "api_key" in missing

    def test_no_required_config(self):
        manifest = PluginManifest(name="test", required_config=[])
        missing = _check_required_config(manifest, {})
        assert missing == []


class TestPluginConflictDetection:
    @pytest.mark.asyncio
    async def test_tool_name_conflict_logged(self, tmp_path):
        """When a plugin registers a tool that already exists, a warning should be logged."""
        from qanot.agent import ToolRegistry

        registry = ToolRegistry()

        async def noop(_):
            return "{}"

        registry.register("read_file", "Read a file", {"type": "object"}, noop)

        mgr = PluginManager()

        # Create a fake plugin with a conflicting tool
        class FakePlugin(Plugin):
            name = "conflict_test"

            def get_tools(self):
                return [ToolDef(
                    name="read_file",
                    description="Conflicting read_file",
                    parameters={"type": "object"},
                    handler=noop,
                )]

        plugin = FakePlugin()
        tools = plugin.get_tools()
        conflicts = [t.name for t in tools if t.name in registry.tool_names]
        assert conflicts == ["read_file"]


# ── 6. Proactive Delivery ───────────────────────────────────


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


# ── 7. Tool Decorator ───────────────────────────────────────


class TestToolDecorator:
    def test_tool_decorator_attaches_metadata(self):
        class MyPlugin(Plugin):
            name = "test"

            @tool("greet", "Say hello", {"type": "object", "properties": {"name": {"type": "string"}}})
            async def greet(self, params):
                return json.dumps({"greeting": f"Hello {params['name']}"})

            def get_tools(self):
                return self._collect_tools()

        plugin = MyPlugin()
        tools = plugin.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "greet"
        assert tools[0].description == "Say hello"
        assert "name" in tools[0].parameters["properties"]

    def test_tool_decorator_default_params(self):
        class MyPlugin(Plugin):
            name = "test"

            @tool("noop", "Does nothing")
            async def noop(self, params):
                return "{}"

            def get_tools(self):
                return self._collect_tools()

        plugin = MyPlugin()
        tools = plugin.get_tools()
        assert tools[0].parameters == {"type": "object", "properties": {}}


# ── 8. Scheduler Load Jobs ──────────────────────────────────


class TestCronSchedulerJobs:
    def test_load_jobs_empty_file(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("[]")

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._load_jobs()
        assert jobs == []

    def test_load_jobs_missing_file(self, tmp_path):
        config = make_config(tmp_path)
        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._load_jobs()
        assert jobs == []

    def test_load_jobs_invalid_json(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("not json")

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._load_jobs()
        assert jobs == []

    def test_ensure_builtin_jobs_adds_if_missing(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text("[]")

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._ensure_builtin_jobs([])
        assert any(j["name"] == "heartbeat" for j in jobs)
        assert any(j["name"] == "briefing" for j in jobs)

    def test_ensure_builtin_jobs_no_duplicate(self, tmp_path):
        config = make_config(tmp_path)
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True)

        existing = [
            {"name": "heartbeat", "schedule": "*/30 * * * *", "mode": "isolated", "prompt": "test", "enabled": True},
            {"name": "briefing", "schedule": "0 8 * * *", "mode": "isolated", "prompt": "test", "enabled": True},
        ]
        (cron_dir / "jobs.json").write_text(json.dumps(existing))

        sched = CronScheduler(config=config, provider=MagicMock(), tool_registry=MagicMock())
        jobs = sched._ensure_builtin_jobs(existing)
        assert len([j for j in jobs if j["name"] == "heartbeat"]) == 1
        assert len([j for j in jobs if j["name"] == "briefing"]) == 1


# ── 9. Proactive Outbox ─────────────────────────────────────


class TestProactiveOutbox:
    @pytest.mark.asyncio
    async def test_outbox_content_queued(self, tmp_path):
        """Proactive outbox content should be put into the message queue."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        outbox = ws / "proactive-outbox.md"
        outbox.write_text("Found disk usage at 95%. Cleaned temp files.")

        # Also create HEARTBEAT.md with content so it doesn't skip
        (ws / "HEARTBEAT.md").write_text("- Check disk space\n")

        config = make_config(tmp_path)
        queue = asyncio.Queue()
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
            message_queue=queue,
        )
        sched._is_user_idle = MagicMock(return_value=True)

        # Agent returns a non-HEARTBEAT_OK result (indicating work was done)
        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="Fixed disk issue"):
            await sched._run_isolated(job_name="heartbeat", prompt="check")

        assert not queue.empty()
        msg = await queue.get()
        assert msg["type"] == "proactive"
        assert "95%" in msg["text"]
        assert msg["source"] == "heartbeat"

        # Outbox should be cleared after reading
        assert outbox.read_text() == ""

    @pytest.mark.asyncio
    async def test_empty_outbox_not_queued(self, tmp_path):
        """Empty proactive outbox should not enqueue anything."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        outbox = ws / "proactive-outbox.md"
        outbox.write_text("")

        (ws / "HEARTBEAT.md").write_text("- Check logs\n")

        config = make_config(tmp_path)
        queue = asyncio.Queue()
        sched = CronScheduler(
            config=config,
            provider=MagicMock(),
            tool_registry=MagicMock(),
            message_queue=queue,
        )
        sched._is_user_idle = MagicMock(return_value=True)

        with patch("qanot.agent.spawn_isolated_agent", new_callable=AsyncMock, return_value="Fixed something"):
            await sched._run_isolated(job_name="heartbeat", prompt="check")

        assert queue.empty()
