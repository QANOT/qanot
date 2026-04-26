"""Userbot plugin tests.

Covers:
  - Opaque recipient-token mint / resolve / expiry
  - Whitelist gating (empty = allow any; usernames + ints)
  - Rate limiting (per-recipient cooldown, hourly global)
  - Audit log shape for send / rate_limit / whitelist_reject / send_error
  - Preview construction (with + without username)
  - tg_find_contact / tg_send_message / tg_list_recent_chats /
    tg_get_chat_history tool paths (all against an AsyncMock pyrogram Client).

We never touch the real Telegram API — the shared userbot client is
monkey-patched to return our AsyncMock.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Import helpers ──────────────────────────────────────────────
#
# The plugin lives outside the importable qanot package, so we load it
# by path — mirroring what qanot.plugins.loader does at runtime.
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "userbot"
# Local imports inside plugin.py (`from ratelimit import RateLimiter`
# etc.) need the plugin dir on sys.path before we import plugin.py.
sys.path.insert(0, str(PLUGIN_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ratelimit_mod = _load_module("userbot_ratelimit_test", PLUGIN_DIR / "ratelimit.py")
audit_mod = _load_module("userbot_audit_test", PLUGIN_DIR / "audit.py")
plugin_mod = _load_module("userbot_plugin_test", PLUGIN_DIR / "plugin.py")

RateLimiter = ratelimit_mod.RateLimiter
RateLimitError = ratelimit_mod.RateLimitError
AuditLog = audit_mod.AuditLog
UserbotPlugin = plugin_mod.UserbotPlugin


# ── Test fixtures ───────────────────────────────────────────────


@dataclass
class FakeConfig:
    """Stand-in for qanot.config.Config."""
    userbot_enabled: bool = True
    userbot_send_per_recipient_seconds: int = 10
    userbot_send_hourly_global: int = 20
    userbot_recipient_whitelist: list = field(default_factory=list)
    bot_token: str = ""
    voicecall_api_id: int = 123
    voicecall_api_hash: str = "hash"
    voicecall_session: str = "sess"


def _make_plugin(
    *,
    config: FakeConfig | None = None,
    workspace_dir: Path | None = None,
) -> UserbotPlugin:
    """Build a UserbotPlugin by hand, skipping the real setup()."""
    p = UserbotPlugin()
    cfg = config or FakeConfig()
    p._config = cfg
    p._workspace_dir = str(workspace_dir or Path("/tmp/qanot-ub-test"))
    Path(p._workspace_dir).mkdir(parents=True, exist_ok=True)
    p._rate_limiter = RateLimiter(
        per_recipient_seconds=cfg.userbot_send_per_recipient_seconds,
        hourly_global=cfg.userbot_send_hourly_global,
    )
    p._audit = AuditLog(p._workspace_dir)
    p._disabled = False
    return p


def _make_fake_chat(*, chat_id: int, username: str | None, first_name: str | None = None, title: str | None = None, chat_type: str = "private") -> MagicMock:
    chat = MagicMock()
    chat.id = chat_id
    chat.username = username
    chat.first_name = first_name
    chat.title = title
    chat.type = MagicMock()
    chat.type.name = chat_type.upper()
    chat.type.value = chat_type
    return chat


def _patch_client(monkeypatch, client: Any) -> None:
    """Make :func:`get_userbot_client` return ``client`` regardless of args."""
    async def _fake_get_client(cfg):
        return client

    monkeypatch.setattr(
        "qanot.userbot_client.get_userbot_client", _fake_get_client,
    )


# ── Rate limiter ────────────────────────────────────────────────


class TestRateLimiter:
    def test_per_recipient_cooldown_blocks_within_window(self):
        rl = RateLimiter(per_recipient_seconds=10, hourly_global=100)
        now = 1000.0
        rl.record("r1", now=now)
        with pytest.raises(RateLimitError) as exc_info:
            rl.check("r1", now=now + 5)
        assert exc_info.value.bucket == "per_recipient"
        assert exc_info.value.retry_after_seconds >= 1

    def test_per_recipient_cooldown_allows_after_window(self):
        rl = RateLimiter(per_recipient_seconds=10, hourly_global=100)
        now = 1000.0
        rl.record("r1", now=now)
        # No raise → passes.
        rl.check("r1", now=now + 11)

    def test_per_recipient_independent_across_recipients(self):
        rl = RateLimiter(per_recipient_seconds=10, hourly_global=100)
        now = 1000.0
        rl.record("r1", now=now)
        # Different recipient should NOT be blocked.
        rl.check("r2", now=now + 1)

    def test_hourly_global_blocks_after_quota(self):
        rl = RateLimiter(per_recipient_seconds=0, hourly_global=3)
        t = 1000.0
        for i in range(3):
            rl.check(f"r{i}", now=t + i)
            rl.record(f"r{i}", now=t + i)
        with pytest.raises(RateLimitError) as exc_info:
            rl.check("r4", now=t + 4)
        assert exc_info.value.bucket == "hourly_global"

    def test_hourly_global_evicts_after_3600s(self):
        rl = RateLimiter(per_recipient_seconds=0, hourly_global=2)
        t = 1000.0
        rl.record("r1", now=t)
        rl.record("r2", now=t + 10)
        # Quota full now.
        with pytest.raises(RateLimitError):
            rl.check("r3", now=t + 11)
        # After 3600s the first two slots fall out of the window.
        rl.check("r3", now=t + 3700)


# ── Audit log ───────────────────────────────────────────────────


class TestAuditLog:
    def test_send_event_shape(self, tmp_path):
        al = AuditLog(tmp_path)
        al.send(
            recipient_id="rcp_abc",
            recipient="@umid",
            text="a" * 300,
            message_id=42,
        )
        lines = al.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "send"
        assert entry["recipient_id"] == "rcp_abc"
        assert entry["recipient"] == "@umid"
        assert entry["message_id"] == 42
        assert entry["text_len"] == 300
        # Preview must NOT be the full body.
        assert len(entry["text_preview"]) <= 200
        assert entry["ts"].endswith("Z")

    def test_rate_limit_event(self, tmp_path):
        al = AuditLog(tmp_path)
        al.rate_limit(recipient="@umid", bucket="per_recipient", retry_after=7)
        entry = json.loads(al.path.read_text().strip())
        assert entry["event"] == "rate_limit"
        assert entry["bucket"] == "per_recipient"
        assert entry["retry_after"] == 7

    def test_whitelist_reject_event(self, tmp_path):
        al = AuditLog(tmp_path)
        al.whitelist_reject(recipient="@stranger")
        entry = json.loads(al.path.read_text().strip())
        assert entry["event"] == "whitelist_reject"
        assert entry["recipient"] == "@stranger"

    def test_send_error_event(self, tmp_path):
        al = AuditLog(tmp_path)
        al.send_error(recipient="@umid", error_class="UserIsBlocked")
        entry = json.loads(al.path.read_text().strip())
        assert entry["event"] == "send_error"
        assert entry["error_class"] == "UserIsBlocked"

    def test_appends_multiple_lines(self, tmp_path):
        al = AuditLog(tmp_path)
        al.send(recipient_id="r1", recipient="@a", text="hi", message_id=1)
        al.whitelist_reject(recipient="@b")
        al.send_error(recipient="@c", error_class="E")
        lines = al.path.read_text().strip().splitlines()
        assert len(lines) == 3
        events = [json.loads(l)["event"] for l in lines]
        assert events == ["send", "whitelist_reject", "send_error"]


# ── Opaque token mint / lookup ──────────────────────────────────


class TestTokens:
    @pytest.mark.asyncio
    async def test_mint_and_resolve(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=123, username="umid", first_name="Umid", peer_id=123, peer_type="user",
        )
        assert token.startswith("rcp_")
        entry = await p._lookup_token(token)
        assert entry is not None
        assert entry["username"] == "umid"
        assert entry["id"] == 123

    @pytest.mark.asyncio
    async def test_unknown_token_is_none(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)
        assert await p._lookup_token("rcp_does_not_exist") is None

    @pytest.mark.asyncio
    async def test_token_expires_after_ttl(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username=None, first_name="X", peer_id=1, peer_type="user",
        )
        # Age the token past TTL.
        p._peers[token]["minted_at"] = time.time() - plugin_mod.TOKEN_TTL_SECONDS - 1
        assert await p._lookup_token(token) is None


# ── Whitelist ───────────────────────────────────────────────────


class TestWhitelist:
    def test_empty_allows_any(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)  # default whitelist is []
        assert p._allowed_by_whitelist({"username": "anyone", "id": 1})

    def test_username_match_case_insensitive(self, tmp_path):
        cfg = FakeConfig(userbot_recipient_whitelist=["@Umid"])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        assert p._allowed_by_whitelist({"username": "umid", "id": 1})
        assert p._allowed_by_whitelist({"username": "UMID", "id": 2})

    def test_username_without_at_prefix(self, tmp_path):
        cfg = FakeConfig(userbot_recipient_whitelist=["umid"])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        assert p._allowed_by_whitelist({"username": "umid", "id": 1})

    def test_int_id_match(self, tmp_path):
        cfg = FakeConfig(userbot_recipient_whitelist=[42])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        assert p._allowed_by_whitelist({"username": None, "id": 42})
        assert not p._allowed_by_whitelist({"username": None, "id": 43})

    def test_numeric_string_id_match(self, tmp_path):
        cfg = FakeConfig(userbot_recipient_whitelist=["42"])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        assert p._allowed_by_whitelist({"username": None, "id": 42})

    def test_nonempty_whitelist_rejects_non_match(self, tmp_path):
        cfg = FakeConfig(userbot_recipient_whitelist=["@umid", 42])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        assert not p._allowed_by_whitelist({"username": "attacker", "id": 99})


# ── Recipient label (used in preview + audit) ───────────────────


class TestRecipientLabel:
    def test_with_username(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)
        assert p._recipient_label({"username": "umid", "first_name": "U", "id": 1}) == "@umid"

    def test_without_username_uses_first_name(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)
        assert p._recipient_label({"username": None, "first_name": "Umid", "id": 1}) == "Umid"

    def test_without_anything_falls_back_to_id(self, tmp_path):
        p = _make_plugin(workspace_dir=tmp_path)
        assert p._recipient_label({"username": None, "first_name": "", "id": 7}) == "7"


# ── Tool: tg_find_contact ───────────────────────────────────────


class TestFindContact:
    @pytest.mark.asyncio
    async def test_returns_token_and_metadata(self, tmp_path, monkeypatch):
        client = AsyncMock()
        client.get_chat = AsyncMock(
            return_value=_make_fake_chat(
                chat_id=123, username="umid", first_name="Umid", chat_type="private",
            ),
        )
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)

        result = json.loads(await p.tg_find_contact({"query": "@umid"}))
        assert result["status"] == "ok"
        assert result["recipient_id"].startswith("rcp_")
        assert result["username"] == "umid"
        assert result["id"] == 123
        assert result["type"] == "user"

        # Token should be stored and resolvable.
        entry = await p._lookup_token(result["recipient_id"])
        assert entry is not None

    @pytest.mark.asyncio
    async def test_rejects_empty_query(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        result = json.loads(await p.tg_find_contact({"query": ""}))
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_unconfigured_when_client_missing(self, tmp_path, monkeypatch):
        async def _none(cfg):
            return None
        monkeypatch.setattr("qanot.userbot_client.get_userbot_client", _none)
        p = _make_plugin(workspace_dir=tmp_path)
        result = json.loads(await p.tg_find_contact({"query": "@x"}))
        assert result["status"] == "unconfigured"


# ── Tool: tg_send_message ───────────────────────────────────────


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_successful_send(self, tmp_path, monkeypatch):
        client = AsyncMock()
        sent_msg = MagicMock()
        sent_msg.id = 9876
        client.send_message = AsyncMock(return_value=sent_msg)
        _patch_client(monkeypatch, client)

        p = _make_plugin(workspace_dir=tmp_path)
        # Skip the preview post (no Agent / bot in test context).
        p._post_preview = AsyncMock(return_value=None)

        token = await p._mint_token(
            peer=123, username="umid", first_name="Umid", peer_id=123, peer_type="user",
        )
        result = json.loads(await p.tg_send_message(
            {"recipient_id": token, "text": "salom"},
        ))
        assert result["status"] == "ok"
        assert result["message_id"] == 9876
        assert result["recipient"] == "@umid"
        client.send_message.assert_awaited_once_with(123, "salom")

        # Audit should contain exactly one "send" entry.
        lines = (Path(tmp_path) / "userbot_audit.log").read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "send"

    @pytest.mark.asyncio
    async def test_rejects_expired_token(self, tmp_path, monkeypatch):
        client = AsyncMock()
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)

        result = json.loads(await p.tg_send_message(
            {"recipient_id": "rcp_nope", "text": "hi"},
        ))
        assert result["status"] == "error"
        client.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitelist_blocks_non_match(self, tmp_path, monkeypatch):
        client = AsyncMock()
        _patch_client(monkeypatch, client)
        cfg = FakeConfig(userbot_recipient_whitelist=["@allowed"])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)

        token = await p._mint_token(
            peer=1, username="attacker", first_name="Bad", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_message(
            {"recipient_id": token, "text": "hi"},
        ))
        assert result["status"] == "error"
        client.send_message.assert_not_awaited()
        # Audit: whitelist_reject written.
        entry = json.loads(
            (Path(tmp_path) / "userbot_audit.log").read_text().strip().splitlines()[-1],
        )
        assert entry["event"] == "whitelist_reject"
        assert entry["recipient"] == "@attacker"

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_second_send(self, tmp_path, monkeypatch):
        client = AsyncMock()
        sent = MagicMock()
        sent.id = 1
        client.send_message = AsyncMock(return_value=sent)
        _patch_client(monkeypatch, client)

        cfg = FakeConfig(userbot_send_per_recipient_seconds=60)
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        p._post_preview = AsyncMock()

        token = await p._mint_token(
            peer=1, username="umid", first_name="U", peer_id=1, peer_type="user",
        )
        ok = json.loads(await p.tg_send_message(
            {"recipient_id": token, "text": "first"},
        ))
        assert ok["status"] == "ok"

        blocked = json.loads(await p.tg_send_message(
            {"recipient_id": token, "text": "second"},
        ))
        assert blocked["status"] == "error"
        assert blocked["bucket"] == "per_recipient"
        assert blocked["retry_after_seconds"] >= 1
        # send_message called once, not twice.
        assert client.send_message.await_count == 1

        # Audit should have send + rate_limit.
        events = [
            json.loads(l)["event"]
            for l in (Path(tmp_path) / "userbot_audit.log").read_text().strip().splitlines()
        ]
        assert events == ["send", "rate_limit"]

    @pytest.mark.asyncio
    async def test_rpc_error_wraps_friendly_message(self, tmp_path, monkeypatch):
        class UserIsBlocked(Exception):
            pass

        client = AsyncMock()
        client.send_message = AsyncMock(side_effect=UserIsBlocked("blocked"))
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)
        p._post_preview = AsyncMock()

        token = await p._mint_token(
            peer=1, username="umid", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_message(
            {"recipient_id": token, "text": "hi"},
        ))
        assert result["status"] == "error"
        assert result["error_class"] == "UserIsBlocked"
        # Rate limiter should NOT have recorded a send (error path).
        # Another send in the same window should succeed (post-mock) → we
        # reset the mock and try again.
        sent = MagicMock()
        sent.id = 7
        client.send_message = AsyncMock(return_value=sent)
        result2 = json.loads(await p.tg_send_message(
            {"recipient_id": token, "text": "hi again"},
        ))
        assert result2["status"] == "ok"

        events = [
            json.loads(l)["event"]
            for l in (Path(tmp_path) / "userbot_audit.log").read_text().strip().splitlines()
        ]
        assert events[0] == "send_error"
        assert events[-1] == "send"


# ── Tool: tg_list_recent_chats ──────────────────────────────────


class TestListDialogs:
    @pytest.mark.asyncio
    async def test_mints_token_per_dialog(self, tmp_path, monkeypatch):
        dialog1 = MagicMock()
        dialog1.chat = _make_fake_chat(
            chat_id=1, username="umid", first_name="Umid", chat_type="private",
        )
        dialog1.unread_messages_count = 2
        top1 = MagicMock()
        top1.text = "salom"
        top1.caption = None
        dialog1.top_message = top1

        dialog2 = MagicMock()
        dialog2.chat = _make_fake_chat(
            chat_id=-100, username=None, title="Work", chat_type="supergroup",
        )
        dialog2.unread_messages_count = 0
        top2 = MagicMock()
        top2.text = None
        top2.caption = None
        dialog2.top_message = top2

        client = AsyncMock()

        async def _gen(limit):
            yield dialog1
            yield dialog2

        client.get_dialogs = MagicMock(return_value=_gen(20))
        _patch_client(monkeypatch, client)

        p = _make_plugin(workspace_dir=tmp_path)
        result = json.loads(await p.tg_list_recent_chats({"limit": 10}))
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["dialogs"][0]["type"] == "user"
        assert result["dialogs"][0]["title"] == "Umid"
        assert result["dialogs"][0]["last_message_preview"] == "salom"
        assert result["dialogs"][1]["type"] == "group"
        # Media-only top message shows the placeholder.
        assert result["dialogs"][1]["last_message_preview"] == "<media>"
        # Every dialog gets its own token, both resolvable.
        for d in result["dialogs"]:
            assert d["recipient_id"].startswith("rcp_")
            assert await p._lookup_token(d["recipient_id"]) is not None


# ── Tool: tg_get_chat_history ───────────────────────────────────


class TestChatHistory:
    @pytest.mark.asyncio
    async def test_reads_messages(self, tmp_path, monkeypatch):
        import datetime as dt

        msg1 = MagicMock()
        sender = MagicMock()
        sender.username = "umid"
        sender.first_name = "Umid"
        sender.id = 1
        msg1.from_user = sender
        msg1.text = "hello"
        msg1.caption = None
        msg1.date = dt.datetime(2026, 4, 1, 12, 0, 0)

        msg2 = MagicMock()
        msg2.from_user = None
        msg2.text = None
        msg2.caption = None
        msg2.date = dt.datetime(2026, 4, 1, 12, 1, 0)

        client = AsyncMock()

        async def _gen(peer, limit):
            yield msg1
            yield msg2

        client.get_chat_history = MagicMock(return_value=_gen(None, 10))
        _patch_client(monkeypatch, client)

        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="umid", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_get_chat_history(
            {"recipient_id": token, "limit": 10},
        ))
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["messages"][0]["from"] == "@umid"
        assert result["messages"][0]["text"] == "hello"
        assert result["messages"][0]["date"].startswith("2026-04-01")
        # Media-only message surfaces the placeholder.
        assert result["messages"][1]["text"] == "<media>"

    @pytest.mark.asyncio
    async def test_rejects_expired_token(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        result = json.loads(await p.tg_get_chat_history(
            {"recipient_id": "rcp_nope", "limit": 5},
        ))
        assert result["status"] == "error"


# ── setup() kill-switch ─────────────────────────────────────────


class TestSetupGate:
    @pytest.mark.asyncio
    async def test_disabled_registers_no_tools(self, tmp_path, monkeypatch):
        """When userbot_enabled=False, get_tools() returns an empty list."""
        cfg = FakeConfig(userbot_enabled=False)

        # Patch load_config so setup() sees our fake disabled config.
        def _fake_load(path):
            return cfg

        monkeypatch.setattr("qanot.config.load_config", _fake_load)
        monkeypatch.setenv("QANOT_CONFIG", str(tmp_path / "config.json"))

        p = UserbotPlugin()
        await p.setup({"workspace_dir": str(tmp_path)})
        assert p.get_tools() == []

    @pytest.mark.asyncio
    async def test_enabled_registers_all_tools(self, tmp_path, monkeypatch):
        cfg = FakeConfig(userbot_enabled=True)

        def _fake_load(path):
            return cfg

        monkeypatch.setattr("qanot.config.load_config", _fake_load)
        monkeypatch.setenv("QANOT_CONFIG", str(tmp_path / "config.json"))

        p = UserbotPlugin()
        await p.setup({"workspace_dir": str(tmp_path)})
        tool_names = {t.name for t in p.get_tools()}
        assert tool_names == {
            "tg_find_contact",
            "tg_send_message",
            "tg_send_checklist",
            "tg_list_recent_chats",
            "tg_get_chat_history",
            "tg_scan_unread",
            "tg_find_mentions",
        }


# ── Tool: tg_send_checklist ─────────────────────────────────────


@pytest.fixture
def fake_raw_pyrogram(monkeypatch):
    """Inject a minimal fake ``pyrogram.raw`` so tg_send_checklist's
    deferred import succeeds without pyrofork installed.

    Each fake constructor stashes its kwargs on ``self`` so assertions can
    inspect what the tool sent. SendMedia is captured by the AsyncMock
    client.invoke() in the test body — we don't need to invoke the real
    one anywhere.
    """
    import types as _types

    class _Capture:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    raw_pkg = _types.ModuleType("pyrogram.raw")
    raw_pkg.__path__ = []  # mark as package
    raw_funcs_pkg = _types.ModuleType("pyrogram.raw.functions")
    raw_funcs_pkg.__path__ = []
    raw_funcs_messages = _types.ModuleType("pyrogram.raw.functions.messages")
    raw_funcs_messages.SendMedia = _Capture
    raw_types = _types.ModuleType("pyrogram.raw.types")
    raw_types.InputMediaTodo = _Capture
    raw_types.TodoList = _Capture
    raw_types.TodoItem = _Capture
    raw_types.TextWithEntities = _Capture

    # Install. We DO NOT install a top-level "pyrogram" because tg_send_message
    # imports nothing from it directly — keeping it absent ensures the rest of
    # the suite stays untouched.
    monkeypatch.setitem(sys.modules, "pyrogram.raw", raw_pkg)
    monkeypatch.setitem(sys.modules, "pyrogram.raw.functions", raw_funcs_pkg)
    monkeypatch.setitem(
        sys.modules,
        "pyrogram.raw.functions.messages",
        raw_funcs_messages,
    )
    monkeypatch.setitem(sys.modules, "pyrogram.raw.types", raw_types)
    # Some import resolvers want the parent name registered too.
    if "pyrogram" not in sys.modules:
        pyrogram_pkg = _types.ModuleType("pyrogram")
        pyrogram_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "pyrogram", pyrogram_pkg)
    return _Capture


def _make_checklist_client() -> AsyncMock:
    """An AsyncMock pyrogram client wired for tg_send_checklist.

    ``resolve_peer`` returns a marker, ``rnd_id`` returns a fixed int,
    ``invoke`` returns a fake Updates with one UpdateNewMessage carrying
    message id 555.
    """
    client = AsyncMock()
    client.resolve_peer = AsyncMock(return_value="peer-resolved")
    client.rnd_id = MagicMock(return_value=42424242)

    fake_msg = MagicMock()
    fake_msg.id = 555

    fake_update = MagicMock()
    fake_update.message = fake_msg
    type(fake_update).__name__ = "UpdateNewMessage"

    fake_updates = MagicMock()
    fake_updates.updates = [fake_update]

    client.invoke = AsyncMock(return_value=fake_updates)
    return client


class TestSendChecklist:
    @pytest.mark.asyncio
    async def test_rejects_missing_recipient_id(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        result = json.loads(await p.tg_send_checklist(
            {"title": "T", "tasks": ["a"]},
        ))
        assert result["status"] == "error"
        assert "recipient_id" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_empty_title(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {"recipient_id": token, "title": "   ", "tasks": ["a"]},
        ))
        assert result["status"] == "error"
        assert "title" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_empty_tasks_array(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {"recipient_id": token, "title": "T", "tasks": []},
        ))
        assert result["status"] == "error"
        assert "tasks" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_non_string_task(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {"recipient_id": token, "title": "T", "tasks": ["ok", 123]},
        ))
        assert result["status"] == "error"
        assert "string" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_all_blank_tasks(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {"recipient_id": token, "title": "T", "tasks": ["  ", ""]},
        ))
        assert result["status"] == "error"
        assert "non-empty" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_title_too_long(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {
                "recipient_id": token,
                "title": "x" * (plugin_mod.TODO_TITLE_MAX + 1),
                "tasks": ["a"],
            },
        ))
        assert result["status"] == "error"
        assert "title" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_too_many_tasks(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {
                "recipient_id": token,
                "title": "T",
                "tasks": [f"task {i}" for i in range(plugin_mod.TODO_ITEMS_MAX + 1)],
            },
        ))
        assert result["status"] == "error"
        assert "tasks" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_task_too_long(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {
                "recipient_id": token,
                "title": "T",
                "tasks": ["ok", "x" * (plugin_mod.TODO_ITEM_MAX + 1)],
            },
        ))
        assert result["status"] == "error"
        assert result["task_index"] == 2

    @pytest.mark.asyncio
    async def test_rejects_expired_token(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        p = _make_plugin(workspace_dir=tmp_path)
        result = json.loads(await p.tg_send_checklist(
            {"recipient_id": "rcp_nope", "title": "T", "tasks": ["a"]},
        ))
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_whitelist_blocks_non_match(self, tmp_path, monkeypatch):
        _patch_client(monkeypatch, AsyncMock())
        cfg = FakeConfig(userbot_recipient_whitelist=["@allowed"])
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="attacker", first_name="A", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist(
            {"recipient_id": token, "title": "T", "tasks": ["a"]},
        ))
        assert result["status"] == "error"
        # Whitelist gate fires BEFORE any client call.
        events = [
            json.loads(l)["event"]
            for l in (Path(tmp_path) / "userbot_audit.log").read_text().strip().splitlines()
        ]
        assert events == ["whitelist_reject"]

    @pytest.mark.asyncio
    async def test_dry_run_validates_without_send(
        self, tmp_path, monkeypatch, fake_raw_pyrogram,
    ):
        client = _make_checklist_client()
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=123, username="umid", first_name="Umid", peer_id=123, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist({
            "recipient_id": token,
            "title": "Sprint 12",
            "tasks": ["alpha", "beta"],
            "dry_run": True,
        }))
        assert result["status"] == "ok"
        assert result["dry_run"] is True
        assert result["task_count"] == 2
        # No invoke / send happened.
        client.invoke.assert_not_awaited()
        # Audit recorded the dry run.
        entry = json.loads(
            (Path(tmp_path) / "userbot_audit.log").read_text().strip(),
        )
        assert entry["event"] == "dry_run_checklist"
        assert entry["task_count"] == 2

    @pytest.mark.asyncio
    async def test_successful_send(
        self, tmp_path, monkeypatch, fake_raw_pyrogram,
    ):
        client = _make_checklist_client()
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)
        p._post_preview_message = AsyncMock(return_value=None)
        token = await p._mint_token(
            peer=123, username="umid", first_name="Umid", peer_id=123, peer_type="user",
        )

        result = json.loads(await p.tg_send_checklist({
            "recipient_id": token,
            "title": "Sprint 12",
            "tasks": [" alpha ", "beta", ""],  # blanks dropped, others trimmed
        }))
        assert result["status"] == "ok"
        assert result["message_id"] == 555
        assert result["task_count"] == 2
        assert result["recipient"] == "@umid"
        assert result["others_can_append"] is True
        assert result["others_can_complete"] is True

        # Verify the raw payload that hit invoke().
        client.invoke.assert_awaited_once()
        sent = client.invoke.await_args.args[0]
        assert type(sent).__name__ == "_Capture"
        assert sent.message == ""
        assert sent.peer == "peer-resolved"
        assert sent.random_id == 42424242
        media = sent.media
        todo = media.todo
        assert todo.others_can_append is True
        assert todo.others_can_complete is True
        # Task ids start at 1; blanks were dropped.
        assert [t.id for t in todo.list] == [1, 2]
        assert [t.title.text for t in todo.list] == ["alpha", "beta"]
        assert todo.title.text == "Sprint 12"

        # Audit row.
        entry = json.loads(
            (Path(tmp_path) / "userbot_audit.log").read_text().strip(),
        )
        assert entry["event"] == "send_checklist"
        assert entry["task_count"] == 2
        assert entry["message_id"] == 555

        # Preview line was posted.
        p._post_preview_message.assert_awaited_once()
        preview_arg = p._post_preview_message.await_args.args[0]
        assert "📋" in preview_arg
        assert "Sprint 12" in preview_arg
        assert "@umid" in preview_arg

    @pytest.mark.asyncio
    async def test_others_can_complete_false_propagates(
        self, tmp_path, monkeypatch, fake_raw_pyrogram,
    ):
        client = _make_checklist_client()
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)
        p._post_preview_message = AsyncMock(return_value=None)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist({
            "recipient_id": token,
            "title": "Read-only",
            "tasks": ["x"],
            "others_can_append": False,
            "others_can_complete": False,
        }))
        assert result["status"] == "ok"
        sent = client.invoke.await_args.args[0]
        assert sent.media.todo.others_can_append is False
        assert sent.media.todo.others_can_complete is False

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_second_send(
        self, tmp_path, monkeypatch, fake_raw_pyrogram,
    ):
        client = _make_checklist_client()
        _patch_client(monkeypatch, client)
        cfg = FakeConfig(userbot_send_per_recipient_seconds=60)
        p = _make_plugin(config=cfg, workspace_dir=tmp_path)
        p._post_preview_message = AsyncMock()
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )

        ok = json.loads(await p.tg_send_checklist({
            "recipient_id": token,
            "title": "T",
            "tasks": ["a"],
        }))
        assert ok["status"] == "ok"

        blocked = json.loads(await p.tg_send_checklist({
            "recipient_id": token,
            "title": "T2",
            "tasks": ["b"],
        }))
        assert blocked["status"] == "error"
        assert blocked["bucket"] == "per_recipient"
        # invoke() called once, not twice.
        assert client.invoke.await_count == 1

    @pytest.mark.asyncio
    async def test_premium_required_error_friendly(
        self, tmp_path, monkeypatch, fake_raw_pyrogram,
    ):
        class PremiumAccountRequired(Exception):
            pass

        client = _make_checklist_client()
        client.invoke = AsyncMock(
            side_effect=PremiumAccountRequired("PREMIUM_ACCOUNT_REQUIRED"),
        )
        _patch_client(monkeypatch, client)
        p = _make_plugin(workspace_dir=tmp_path)
        token = await p._mint_token(
            peer=1, username="u", first_name="U", peer_id=1, peer_type="user",
        )
        result = json.loads(await p.tg_send_checklist({
            "recipient_id": token,
            "title": "T",
            "tasks": ["a"],
        }))
        assert result["status"] == "error"
        assert result["error_class"] == "PremiumAccountRequired"
        assert "Premium" in result["error"]
        # Send error audit row.
        entry = json.loads(
            (Path(tmp_path) / "userbot_audit.log").read_text().strip(),
        )
        assert entry["event"] == "send_error"

    @pytest.mark.asyncio
    async def test_extract_message_id_handles_update_message_id(self):
        """Fallback: if updates carry only UpdateMessageID (no full message)."""
        u = MagicMock()
        u.message = None
        u.id = 777
        type(u).__name__ = "UpdateMessageID"
        envelope = MagicMock()
        envelope.updates = [u]
        assert UserbotPlugin._extract_message_id(envelope) == 777

    @pytest.mark.asyncio
    async def test_extract_message_id_returns_zero_when_unknown(self):
        envelope = MagicMock()
        envelope.updates = []
        assert UserbotPlugin._extract_message_id(envelope) == 0
        assert UserbotPlugin._extract_message_id(None) == 0


# ── Audit log: checklist events ─────────────────────────────────


class TestAuditChecklist:
    def test_send_checklist_event_shape(self, tmp_path):
        al = AuditLog(tmp_path)
        al.send_checklist(
            recipient_id="rcp_x",
            recipient="@umid",
            title="Sprint 12",
            task_count=5,
            message_id=999,
            others_can_append=True,
            others_can_complete=False,
        )
        entry = json.loads(al.path.read_text().strip())
        assert entry["event"] == "send_checklist"
        assert entry["task_count"] == 5
        assert entry["message_id"] == 999
        assert entry["title_preview"] == "Sprint 12"
        assert entry["others_can_append"] is True
        assert entry["others_can_complete"] is False

    def test_dry_run_checklist_event_shape(self, tmp_path):
        al = AuditLog(tmp_path)
        al.dry_run_checklist(
            recipient_id="rcp_x",
            recipient="@umid",
            title="T",
            task_count=2,
            others_can_append=False,
            others_can_complete=True,
        )
        entry = json.loads(al.path.read_text().strip())
        assert entry["event"] == "dry_run_checklist"
        assert entry["task_count"] == 2
