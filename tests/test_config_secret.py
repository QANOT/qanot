"""Tests for agent-initiated config secret set flow.

Flow: agent calls config_set_secret → user message scrubbed → approval card →
user clicks button → secrets.env written (0600) + config.json SecretRef → restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qanot.config import Config
from qanot.registry import ToolRegistry
from qanot.tools.config_manage import (
    ALLOWED_FIELDS,
    PROPOSAL_TTL_SECONDS,
    _REGISTERED_REGISTRIES,
    _mask_value,
    _value_hash,
    handle_config_approve_callback,
    handle_config_deny_callback,
    register_config_tools,
)


# ──────────────────────────── fixtures / helpers ────────────────────────────


def _make_config(tmpdir: Path, **overrides) -> Config:
    cfg = Config(
        bot_token="test-bot-token",
        api_key="test-key",
        workspace_dir=str(tmpdir),
        sessions_dir=str(tmpdir / "sessions"),
        secrets_env_path=str(tmpdir / "secrets.env"),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_adapter() -> SimpleNamespace:
    adapter = SimpleNamespace()
    adapter._pending_config_proposals = {}
    adapter.bot = MagicMock()
    adapter.bot.send_message = AsyncMock()
    adapter.bot.delete_message = AsyncMock()
    return adapter


def _make_callback(user_id: int, data: str, message_text: str = "card") -> SimpleNamespace:
    msg = SimpleNamespace()
    msg.text = message_text
    msg.edit_text = AsyncMock()
    cb = SimpleNamespace()
    cb.from_user = SimpleNamespace(id=user_id)
    cb.data = data
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


def _write_config_file(tmpdir: Path, **fields) -> Path:
    cfg_path = tmpdir / "config.json"
    data = {
        "bot_token": "test-bot-token",
        "api_key": "test-key",
        "brave_api_key": "",
    }
    data.update(fields)
    cfg_path.write_text(json.dumps(data, indent=2))
    return cfg_path


@pytest.fixture(autouse=True)
def _reset_state():
    _REGISTERED_REGISTRIES.clear()
    # Clean any QANOT_* env vars that tests may leave behind.
    to_clean = [k for k in os.environ if k.startswith("QANOT_BRAVE_API_KEY")
                or k.startswith("QANOT_VOICE_API_KEY")
                or k.startswith("QANOT_IMAGE_API_KEY")]
    for k in to_clean:
        os.environ.pop(k, None)
    yield
    _REGISTERED_REGISTRIES.clear()
    for k in list(os.environ):
        if k.startswith("QANOT_BRAVE_API_KEY") or k.startswith("QANOT_VOICE_API_KEY") or k.startswith("QANOT_IMAGE_API_KEY"):
            os.environ.pop(k, None)


@pytest.fixture(autouse=True)
def _no_restart():
    """Silence the SIGTERM-based restart in all tests."""
    with patch("qanot.tools.config_manage._trigger_restart") as m:
        yield m


def _register(registry, config, adapter, *, user_id="42", chat_id=1000, message_id=7):
    register_config_tools(
        registry,
        config,
        adapter,
        get_user_id=lambda: user_id,
        get_chat_id=lambda: chat_id,
        get_message_id=lambda: message_id,
        get_bot=lambda: adapter.bot,
    )


async def _call_set_secret(registry, **params):
    handler = registry.get_handler("config_set_secret")
    return json.loads(await handler(params))


async def _call_delete_message(registry, **params):
    handler = registry.get_handler("delete_message")
    return json.loads(await handler(params))


# ──────────────────────────── tests ────────────────────────────


class TestAllowlist:
    @pytest.mark.asyncio
    async def test_bot_token_rejected(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        result = await _call_set_secret(
            registry,
            field="bot_token",
            value="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            source="user pasted",
            reason="bot setup",
        )
        assert result["success"] is False
        assert "system-critical" in result["error"] or "not settable" in result["error"]
        assert not adapter._pending_config_proposals

    @pytest.mark.asyncio
    async def test_api_key_rejected(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        result = await _call_set_secret(
            registry,
            field="api_key",
            value="sk-ant-abcdefghijklmnop",
            source="user",
            reason="setup",
        )
        assert result["success"] is False
        assert "system-critical" in result["error"]
        assert not adapter._pending_config_proposals

    @pytest.mark.asyncio
    async def test_random_field_rejected(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        result = await _call_set_secret(
            registry,
            field="random_made_up_field",
            value="somevalue123456",
            source="user",
            reason="test",
        )
        assert result["success"] is False
        assert "not settable" in result["error"] or "Allowlisted" in result["error"]
        assert not adapter._pending_config_proposals


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_propose_stores_pending(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"
        result = await _call_set_secret(
            registry,
            field="brave_api_key",
            value=value,
            source="user pasted in chat",
            reason="enable web search",
        )
        assert result["success"] is True
        assert result["status"] == "awaiting_approval"
        assert result["field"] == "brave_api_key"
        assert result["message_scrubbed"] is True
        assert "***" in result["masked_value"]
        pid = result["proposal_id"]
        assert pid in adapter._pending_config_proposals
        pending = adapter._pending_config_proposals[pid]
        assert pending["value"] == value
        assert pending["value_hash"] == _value_hash(value)
        adapter.bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approval_writes_secrets_env(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry,
                field="brave_api_key",
                value=value,
                source="user",
                reason="web search",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        secrets_path = Path(cfg.secrets_env_path)
        assert secrets_path.exists()
        content = secrets_path.read_text()
        assert f"QANOT_BRAVE_API_KEY={value}" in content
        mode = secrets_path.stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.asyncio
    async def test_approval_writes_secretref_to_config(self, tmp_path):
        cfg_path = _write_config_file(tmp_path, timezone="Asia/Tashkent")
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        raw = json.loads(cfg_path.read_text())
        assert raw["brave_api_key"] == {"env": "QANOT_BRAVE_API_KEY"}
        assert raw["bot_token"] == "test-bot-token"  # untouched
        assert raw["timezone"] == "Asia/Tashkent"  # untouched

    @pytest.mark.asyncio
    async def test_approval_sets_os_environ(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)
            assert os.environ.get("QANOT_BRAVE_API_KEY") == value
        # In-memory Config mirrored
        assert cfg.brave_api_key == value


class TestAuthorization:
    @pytest.mark.asyncio
    async def test_wrong_user_rejected(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            # Different user clicks
            cb = _make_callback(999, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        raw = json.loads(cfg_path.read_text())
        assert raw["brave_api_key"] == ""  # unchanged
        assert not Path(cfg.secrets_env_path).exists()
        cb.answer.assert_awaited()
        # Proposal still there — intruder doesn't consume it.
        assert pid in adapter._pending_config_proposals


class TestTTL:
    @pytest.mark.asyncio
    async def test_ttl_expiry_treated_as_deny(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            # Force expiry
            adapter._pending_config_proposals[pid]["expires_at"] = 0
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        # Expired proposal is consumed + config unchanged
        assert pid not in adapter._pending_config_proposals
        raw = json.loads(cfg_path.read_text())
        assert raw["brave_api_key"] == ""


class TestMasking:
    def test_long_value_masking(self):
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"
        masked = _mask_value(value)
        assert masked.startswith("BSAR")
        assert masked.endswith(f"jsh1 (len {len(value)})")
        assert "***" in masked

    def test_short_value_masking(self):
        value = "abcdef"
        masked = _mask_value(value)
        assert masked == "*** (len 6)"
        assert "abc" not in masked

    def test_boundary_12_chars(self):
        value = "abcd12345678"  # exactly 12
        masked = _mask_value(value)
        assert masked == "abcd***5678 (len 12)"


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_delete_message_tool_uses_defaults(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter, chat_id=1000, message_id=7)
        result = await _call_delete_message(registry)
        assert result["success"] is True
        adapter.bot.delete_message.assert_awaited_once_with(chat_id=1000, message_id=7)

    @pytest.mark.asyncio
    async def test_delete_message_tool_explicit(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        result = await _call_delete_message(registry, chat_id=555, message_id=99)
        assert result["success"] is True
        adapter.bot.delete_message.assert_awaited_once_with(chat_id=555, message_id=99)

    @pytest.mark.asyncio
    async def test_config_set_secret_scrubs_on_entry(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter, chat_id=1000, message_id=7)
        result = await _call_set_secret(
            registry,
            field="brave_api_key",
            value="BSARabcdefghijklmnopqrstuvwxyzjsh1",
            source="user",
            reason="web",
        )
        adapter.bot.delete_message.assert_awaited_once_with(chat_id=1000, message_id=7)
        assert result["message_scrubbed"] is True

    @pytest.mark.asyncio
    async def test_scrub_failure_does_not_abort(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        adapter.bot.delete_message.side_effect = RuntimeError("too old")
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        result = await _call_set_secret(
            registry, field="brave_api_key",
            value="BSARabcdefghijklmnopqrstuvwxyzjsh1",
            source="user", reason="web",
        )
        assert result["success"] is True
        assert result["message_scrubbed"] is False
        assert result["scrub_error"] is not None


class TestRollback:
    @pytest.mark.asyncio
    async def test_secrets_env_write_failure_rolls_back(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            with patch(
                "qanot.tools.config_manage._update_secrets_env",
                side_effect=RuntimeError("disk full"),
            ):
                await handle_config_approve_callback(adapter, cfg, cb, pid)

        # config.json untouched
        raw = json.loads(cfg_path.read_text())
        assert raw["brave_api_key"] == ""
        # No env var leaked
        assert os.environ.get("QANOT_BRAVE_API_KEY") is None
        # Pending entry consumed (not leaked)
        assert pid not in adapter._pending_config_proposals


class TestIdempotentRegistration:
    def test_double_register_is_noop(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        names_before = set(registry.tool_names)
        _register(registry, cfg, adapter)
        names_after = set(registry.tool_names)
        assert names_before == names_after
        assert "config_set_secret" in names_after
        assert "delete_message" in names_after


class TestValueValidation:
    @pytest.mark.asyncio
    async def test_empty_value_rejected(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        result = await _call_set_secret(
            registry, field="brave_api_key", value="",
            source="user", reason="web",
        )
        assert result["success"] is False
        assert "empty" in result["error"] or "short" in result["error"]

    @pytest.mark.asyncio
    async def test_placeholder_value_rejected(self, tmp_path):
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        for junk in ("TODO", "your-key-here", "placeholder"):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=junk,
                source="user", reason="web",
            )
            assert result["success"] is False, f"{junk} should be rejected"


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_log_does_not_contain_raw_value(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARSUPERSECRETabcdefghijklmnopqrstjsh1"

        captured: list[str] = []

        def fake_write_daily_note(content, workspace_dir="", user_id=""):
            captured.append(content)

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}), \
             patch("qanot.memory.write_daily_note", side_effect=fake_write_daily_note):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        assert captured, "no audit lines written"
        for line in captured:
            assert value not in line, f"raw value leaked into audit line: {line}"
            assert "SUPERSECRET" not in line


class TestSecretsEnvMerge:
    @pytest.mark.asyncio
    async def test_existing_lines_preserved(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        secrets_path = Path(cfg.secrets_env_path)
        secrets_path.write_text("OTHER_VAR=keepme\n# a comment\nFOO=bar\n")

        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        content = secrets_path.read_text()
        assert "OTHER_VAR=keepme" in content
        assert "# a comment" in content
        assert "FOO=bar" in content
        assert f"QANOT_BRAVE_API_KEY={value}" in content

    @pytest.mark.asyncio
    async def test_same_var_replaced_not_duplicated(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        secrets_path = Path(cfg.secrets_env_path)
        secrets_path.write_text("QANOT_BRAVE_API_KEY=oldvalue_aaaaaaaaaaaaaaaa\n")

        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARnewvalue1234567890abcdefghijklmn"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="rotate",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_approve:{pid}")
            await handle_config_approve_callback(adapter, cfg, cb, pid)

        content = secrets_path.read_text()
        occurrences = content.count("QANOT_BRAVE_API_KEY=")
        assert occurrences == 1
        assert "oldvalue" not in content
        assert value in content


class TestDeny:
    @pytest.mark.asyncio
    async def test_deny_discards_proposal(self, tmp_path):
        cfg_path = _write_config_file(tmp_path)
        cfg = _make_config(tmp_path)
        adapter = _make_adapter()
        registry = ToolRegistry()
        _register(registry, cfg, adapter)
        value = "BSARabcdefghijklmnopqrstuvwxyzjsh1"

        with patch.dict(os.environ, {"QANOT_CONFIG": str(cfg_path)}):
            result = await _call_set_secret(
                registry, field="brave_api_key", value=value,
                source="user", reason="web",
            )
            pid = result["proposal_id"]
            cb = _make_callback(42, f"cfg_deny:{pid}")
            await handle_config_deny_callback(adapter, cfg, cb, pid)

        assert pid not in adapter._pending_config_proposals
        raw = json.loads(cfg_path.read_text())
        assert raw["brave_api_key"] == ""
        assert not Path(cfg.secrets_env_path).exists()
