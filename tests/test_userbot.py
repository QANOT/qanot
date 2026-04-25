"""Unit tests for the userbot plugin — pure-Python logic only.

Pyrofork (the real MTProto client) is NOT installed for tests; we fake it
with simple async-iterator and async-method mocks. The goal here is to
cover the *logic* the plugin owns: tokens, whitelist, rate limit, dry_run,
the scan_unread aggregator, and mention detection.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).parent.parent / "plugins" / "userbot"


# ────────── Module loading helpers ──────────

def _load_module(file_name: str, module_name: str):
    """Import plugins/userbot/<file_name>.py under a unique module name.

    We register the module in ``sys.modules`` before exec so dataclasses
    (Py 3.14) can resolve forward-ref annotations via ``cls.__module__``."""
    spec = importlib.util.spec_from_file_location(
        module_name, str(PLUGIN_DIR / f"{file_name}.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_module("audit", "userbot_audit_under_test")


@pytest.fixture(scope="module")
def ratelimit_mod():
    return _load_module("ratelimit", "userbot_ratelimit_under_test")


@pytest.fixture(scope="module")
def plugin_mod():
    """Import the plugin module. The plugin's own sys.path trick is fine
    here because at module-import time it only adds PLUGIN_DIR — the
    `from ratelimit import …` etc. happen later inside ``setup()`` which
    we never call in these tests."""
    spec = importlib.util.spec_from_file_location(
        "userbot_plugin_under_test", str(PLUGIN_DIR / "plugin.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["userbot_plugin_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ────────── Fake pyrogram primitives ──────────

class FakeChat:
    def __init__(self, *, chat_id, title=None, first_name=None,
                 username=None, type_name="private"):
        self.id = chat_id
        self.title = title
        self.first_name = first_name
        self.username = username
        # pyrogram exposes type as an enum-ish; we mimic with a name attr.
        self.type = SimpleNamespace(name=type_name)


class FakeUser:
    def __init__(self, *, user_id, username=None, first_name=None):
        self.id = user_id
        self.username = username
        self.first_name = first_name


class FakeMessage:
    def __init__(self, *, message_id, text=None, caption=None,
                 from_user=None, date=None, mentioned=False,
                 reply_to_message=None):
        self.id = message_id
        self.text = text
        self.caption = caption
        self.from_user = from_user
        self.date = date
        self.mentioned = mentioned
        self.reply_to_message = reply_to_message


class FakeDialog:
    def __init__(self, *, chat, unread=0, top_message=None):
        self.chat = chat
        self.unread_messages_count = unread
        self.top_message = top_message


class FakeClient:
    """Minimal pyrofork stand-in. Each test wires up exactly the
    behaviour it needs — we don't try to model the whole API."""

    def __init__(self):
        self.dialogs: list[FakeDialog] = []
        self.history: dict[int, list[FakeMessage]] = {}
        self.me: FakeUser = FakeUser(user_id=1, username="me", first_name="Me")
        self.sent: list[dict] = []
        self.send_should_raise: Exception | None = None
        self.contacts: dict[object, FakeChat] = {}  # query → FakeChat

    async def get_me(self):
        return self.me

    async def get_dialogs(self, *, limit=20):
        for d in self.dialogs[:limit]:
            yield d

    async def get_chat_history(self, peer_id, *, limit=10):
        msgs = self.history.get(int(peer_id), [])
        for m in msgs[:limit]:
            yield m

    async def get_chat(self, query):
        if query in self.contacts:
            return self.contacts[query]
        # Fall back to the default — most tests just verify the resolution
        # path returned *something*.
        raise KeyError(f"unknown contact: {query!r}")

    async def send_message(self, peer, text, **kwargs):
        if self.send_should_raise is not None:
            raise self.send_should_raise
        record = {"peer": peer, "text": text, **kwargs}
        self.sent.append(record)
        return SimpleNamespace(id=1000 + len(self.sent))


# ────────── Plugin fixture ──────────

@pytest.fixture
def plugin(plugin_mod, ratelimit_mod, audit_mod, tmp_path):
    """Construct the plugin with manually-injected dependencies, skipping
    setup() (which requires the framework Config + pyrofork)."""
    p = plugin_mod.UserbotPlugin()
    p._workspace_dir = str(tmp_path)
    p._config = SimpleNamespace(
        userbot_enabled=True,
        userbot_recipient_whitelist=[],
        userbot_send_per_recipient_seconds=10,
        userbot_send_hourly_global=20,
        bot_token="",  # disables preview post
    )
    p._rate_limiter = ratelimit_mod.RateLimiter(
        per_recipient_seconds=10, hourly_global=20,
    )
    p._audit = audit_mod.AuditLog(str(tmp_path))
    p._disabled = False
    return p


@pytest.fixture
def fake_client(plugin):
    """Inject a fake client and bypass the real lookup."""
    fc = FakeClient()

    async def _get_client():
        return fc

    plugin._get_client = _get_client  # type: ignore[method-assign]

    # Disable preview post — it tries to find the agent instance.
    async def _no_preview(text, label):
        return None

    plugin._post_preview = _no_preview  # type: ignore[method-assign]
    return fc


# ────────── Audit log ──────────

def test_audit_send_writes_jsonl(audit_mod, tmp_path):
    log = audit_mod.AuditLog(str(tmp_path))
    log.send(
        recipient_id="rcp_abc",
        recipient="@umid",
        text="salom",
        message_id=42,
    )
    lines = (tmp_path / "userbot_audit.log").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "send"
    assert entry["recipient"] == "@umid"
    assert entry["text_preview"] == "salom"
    assert entry["text_len"] == 5
    assert entry["message_id"] == 42
    assert "ts" in entry
    assert "reply_to_message_id" not in entry


def test_audit_send_with_reply_to(audit_mod, tmp_path):
    log = audit_mod.AuditLog(str(tmp_path))
    log.send(
        recipient_id="rcp_abc",
        recipient="@umid",
        text="ok",
        message_id=42,
        reply_to_message_id=7,
    )
    entry = json.loads((tmp_path / "userbot_audit.log").read_text().splitlines()[0])
    assert entry["reply_to_message_id"] == 7


def test_audit_dry_run_separate_event(audit_mod, tmp_path):
    log = audit_mod.AuditLog(str(tmp_path))
    log.dry_run(
        recipient_id="rcp_abc",
        recipient="@umid",
        text="draft",
        reply_to_message_id=3,
    )
    entry = json.loads((tmp_path / "userbot_audit.log").read_text().splitlines()[0])
    assert entry["event"] == "dry_run"
    assert entry["recipient"] == "@umid"
    assert entry["text_preview"] == "draft"
    assert entry["text_len"] == 5
    assert entry["reply_to_message_id"] == 3


def test_audit_preview_truncated(audit_mod, tmp_path):
    log = audit_mod.AuditLog(str(tmp_path))
    long = "x" * 500
    log.send(recipient_id="r", recipient="@u", text=long, message_id=1)
    entry = json.loads((tmp_path / "userbot_audit.log").read_text().splitlines()[0])
    assert len(entry["text_preview"]) == 200  # PREVIEW_MAX
    assert entry["text_len"] == 500


# ────────── Rate limiter ──────────

def test_rate_limiter_per_recipient_cooldown(ratelimit_mod):
    rl = ratelimit_mod.RateLimiter(per_recipient_seconds=10, hourly_global=100)
    rl.check("rcp_a", now=1000.0)  # ok
    rl.record("rcp_a", now=1000.0)
    with pytest.raises(ratelimit_mod.RateLimitError) as exc:
        rl.check("rcp_a", now=1005.0)  # 5s elapsed < 10s
    assert exc.value.bucket == "per_recipient"
    # Different recipient is still fine.
    rl.check("rcp_b", now=1005.0)


def test_rate_limiter_hourly_cap(ratelimit_mod):
    rl = ratelimit_mod.RateLimiter(per_recipient_seconds=0, hourly_global=2)
    rl.record("a", now=1000.0)
    rl.record("b", now=1001.0)
    with pytest.raises(ratelimit_mod.RateLimitError) as exc:
        rl.check("c", now=1002.0)
    assert exc.value.bucket == "hourly_global"
    # After window expires the cap recovers.
    rl.check("c", now=1000.0 + 3601.0)


# ────────── Tokens ──────────

def test_token_mint_and_lookup(plugin):
    async def go():
        token = await plugin._mint_token(
            peer=42, username="umid", first_name="Umid",
            peer_id=42, peer_type="user",
        )
        assert token.startswith("rcp_")
        entry = await plugin._lookup_token(token)
        assert entry is not None
        assert entry["id"] == 42
        assert entry["username"] == "umid"
    asyncio.run(go())


def test_token_expires_after_ttl(plugin, plugin_mod, monkeypatch):
    async def go():
        token = await plugin._mint_token(
            peer=1, username="x", first_name="X",
            peer_id=1, peer_type="user",
        )
        # Force the stored timestamp into the past, beyond TTL.
        plugin._peers[token]["minted_at"] = (
            time.time() - plugin_mod.TOKEN_TTL_SECONDS - 1
        )
        entry = await plugin._lookup_token(token)
        assert entry is None
    asyncio.run(go())


# ────────── Whitelist ──────────

def test_whitelist_empty_allows_all(plugin):
    plugin._config.userbot_recipient_whitelist = []
    assert plugin._allowed_by_whitelist({"username": "stranger", "id": 99}) is True


def test_whitelist_username_match_caseless(plugin):
    plugin._config.userbot_recipient_whitelist = ["@Umid"]
    assert plugin._allowed_by_whitelist({"username": "umid", "id": 1}) is True
    assert plugin._allowed_by_whitelist({"username": "umid", "id": 1}) is True
    assert plugin._allowed_by_whitelist({"username": "other", "id": 2}) is False


def test_whitelist_numeric_id(plugin):
    plugin._config.userbot_recipient_whitelist = [42, "100"]
    assert plugin._allowed_by_whitelist({"username": None, "id": 42}) is True
    assert plugin._allowed_by_whitelist({"username": None, "id": 100}) is True
    assert plugin._allowed_by_whitelist({"username": None, "id": 7}) is False


# ────────── tg_send_message ──────────

def test_send_message_happy_path(plugin, fake_client):
    async def go():
        token = await plugin._mint_token(
            peer=42, username="umid", first_name="Umid",
            peer_id=42, peer_type="user",
        )
        out = await plugin.tg_send_message(
            {"recipient_id": token, "text": "salom"},
        )
        result = json.loads(out)
        assert result["status"] == "ok"
        assert len(fake_client.sent) == 1
        assert fake_client.sent[0]["text"] == "salom"
        assert "reply_to_message_id" not in fake_client.sent[0]
    asyncio.run(go())


def test_send_message_with_reply_to(plugin, fake_client):
    async def go():
        token = await plugin._mint_token(
            peer=42, username="umid", first_name="Umid",
            peer_id=42, peer_type="user",
        )
        out = await plugin.tg_send_message({
            "recipient_id": token,
            "text": "thread",
            "reply_to_message_id": 555,
        })
        result = json.loads(out)
        assert result["status"] == "ok"
        assert result["reply_to_message_id"] == 555
        assert fake_client.sent[0]["reply_to_message_id"] == 555
    asyncio.run(go())


def test_send_message_dry_run_does_not_send(plugin, fake_client):
    async def go():
        token = await plugin._mint_token(
            peer=42, username="umid", first_name="Umid",
            peer_id=42, peer_type="user",
        )
        out = await plugin.tg_send_message({
            "recipient_id": token,
            "text": "draft",
            "dry_run": True,
        })
        result = json.loads(out)
        assert result["status"] == "ok"
        assert result["dry_run"] is True
        assert result["would_send"] is True
        assert result["text"] == "draft"
        # No real send happened.
        assert fake_client.sent == []
    asyncio.run(go())


def test_send_message_dry_run_audited(plugin, fake_client, tmp_path):
    async def go():
        token = await plugin._mint_token(
            peer=42, username="umid", first_name="Umid",
            peer_id=42, peer_type="user",
        )
        await plugin.tg_send_message({
            "recipient_id": token,
            "text": "draft",
            "dry_run": True,
            "reply_to_message_id": 9,
        })
        log_path = Path(plugin._audit.path)
        line = log_path.read_text().splitlines()[-1]
        entry = json.loads(line)
        assert entry["event"] == "dry_run"
        assert entry["recipient"] == "@umid"
        assert entry["reply_to_message_id"] == 9
    asyncio.run(go())


def test_send_message_dry_run_bypasses_rate_limit(plugin, fake_client):
    async def go():
        token = await plugin._mint_token(
            peer=42, username="umid", first_name="Umid",
            peer_id=42, peer_type="user",
        )
        # Burn the per-recipient bucket with one real send.
        await plugin.tg_send_message({"recipient_id": token, "text": "first"})
        # Real second send within cooldown → rejected.
        out_real = await plugin.tg_send_message(
            {"recipient_id": token, "text": "second"},
        )
        assert json.loads(out_real)["status"] == "error"
        # Dry run within the same cooldown → allowed (no record).
        out_dry = await plugin.tg_send_message({
            "recipient_id": token, "text": "draft", "dry_run": True,
        })
        assert json.loads(out_dry)["status"] == "ok"
    asyncio.run(go())


def test_send_message_whitelist_blocks_dry_run_too(plugin, fake_client):
    async def go():
        plugin._config.userbot_recipient_whitelist = ["@allowed_only"]
        token = await plugin._mint_token(
            peer=42, username="stranger", first_name="Stranger",
            peer_id=42, peer_type="user",
        )
        out = await plugin.tg_send_message({
            "recipient_id": token, "text": "x", "dry_run": True,
        })
        result = json.loads(out)
        assert result["status"] == "error"
        assert "whitelist" in result["error"].lower() or "ro'yxat" in result["error"]
    asyncio.run(go())


# ────────── tg_scan_unread ──────────

def test_scan_unread_aggregates_in_one_call(plugin, fake_client):
    """Verifies the aggregator returns dialogs+messages in one structure
    and skips channels by default."""
    async def go():
        chat_dm = FakeChat(
            chat_id=10, first_name="Umid", username="umid", type_name="PRIVATE",
        )
        chat_group = FakeChat(
            chat_id=20, title="Team", username="team", type_name="SUPERGROUP",
        )
        chat_channel = FakeChat(
            chat_id=30, title="Spam Channel", username="spam", type_name="CHANNEL",
        )
        chat_silent_dm = FakeChat(
            chat_id=40, first_name="Quiet", username="quiet", type_name="PRIVATE",
        )

        fake_client.dialogs = [
            FakeDialog(chat=chat_dm, unread=2),
            FakeDialog(chat=chat_group, unread=5),
            FakeDialog(chat=chat_channel, unread=99),
            FakeDialog(chat=chat_silent_dm, unread=0),
        ]
        fake_client.history = {
            10: [FakeMessage(message_id=1, text="hi", from_user=FakeUser(
                user_id=10, username="umid", first_name="Umid",
            ))],
            20: [FakeMessage(message_id=2, text="meeting?", from_user=FakeUser(
                user_id=99, first_name="Bob",
            ))],
        }

        out = await plugin.tg_scan_unread({})
        result = json.loads(out)
        assert result["status"] == "ok"
        # Channel filtered out, silent DM filtered out (only_unread default).
        assert result["count"] == 2
        types_seen = {d["type"] for d in result["dialogs"]}
        assert "channel" not in types_seen
        # Each dialog has a fresh recipient_id token usable on send.
        for d in result["dialogs"]:
            assert d["recipient_id"].startswith("rcp_")
            entry = await plugin._lookup_token(d["recipient_id"])
            assert entry is not None

        # Messages came through.
        umid_dialog = next(d for d in result["dialogs"] if d["title"] == "Umid")
        assert umid_dialog["messages"][0]["text"] == "hi"
        assert umid_dialog["messages"][0]["from"] == "@umid"
        assert umid_dialog["messages"][0]["message_id"] == 1
    asyncio.run(go())


def test_scan_unread_include_channels_flag(plugin, fake_client):
    async def go():
        ch = FakeChat(
            chat_id=30, title="Spam", username="s", type_name="CHANNEL",
        )
        fake_client.dialogs = [FakeDialog(chat=ch, unread=1)]
        fake_client.history = {30: [FakeMessage(message_id=1, text="ad")]}
        out = await plugin.tg_scan_unread({"include_channels": True})
        result = json.loads(out)
        assert result["count"] == 1
        assert result["dialogs"][0]["type"] == "channel"
    asyncio.run(go())


def test_scan_unread_only_unread_false_returns_silent(plugin, fake_client):
    async def go():
        ch = FakeChat(
            chat_id=10, first_name="Quiet", username="q", type_name="PRIVATE",
        )
        fake_client.dialogs = [FakeDialog(chat=ch, unread=0)]
        fake_client.history = {10: [FakeMessage(message_id=1, text="old")]}
        out = await plugin.tg_scan_unread({"only_unread": False})
        result = json.loads(out)
        assert result["count"] == 1
    asyncio.run(go())


# ────────── tg_find_mentions ──────────

def test_find_mentions_picks_up_flag_and_substring(plugin, fake_client):
    async def go():
        fake_client.me = FakeUser(user_id=777, username="me", first_name="Me")
        chat = FakeChat(chat_id=20, title="Team", username="team",
                        type_name="SUPERGROUP")
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=2)
        old = now - timedelta(hours=200)  # outside default 24h window

        fake_client.dialogs = [FakeDialog(chat=chat, unread=3)]
        fake_client.history = {
            20: [
                # Telegram-flag mention
                FakeMessage(message_id=1, text="hey @me check this",
                            from_user=FakeUser(user_id=1, username="boss"),
                            date=recent, mentioned=True),
                # Substring fallback (no flag set)
                FakeMessage(message_id=2, text="cc @me again",
                            from_user=FakeUser(user_id=2, username="al"),
                            date=recent, mentioned=False),
                # Reply to *my* message
                FakeMessage(
                    message_id=3, text="agreed",
                    from_user=FakeUser(user_id=3, username="x"),
                    date=recent, mentioned=False,
                    reply_to_message=FakeMessage(
                        message_id=99, text="my prior msg",
                        from_user=FakeUser(user_id=777, username="me"),
                        date=recent,
                    ),
                ),
                # Older than window — must be skipped.
                FakeMessage(message_id=4, text="@me ancient",
                            from_user=FakeUser(user_id=4, username="ghost"),
                            date=old, mentioned=True),
                # Unrelated chatter — must NOT be returned.
                FakeMessage(message_id=5, text="random talk",
                            from_user=FakeUser(user_id=5, username="bob"),
                            date=recent, mentioned=False),
            ],
        }

        out = await plugin.tg_find_mentions({"hours": 24})
        result = json.loads(out)
        assert result["status"] == "ok"
        ids = sorted(m["message_id"] for m in result["mentions"])
        assert ids == [1, 2, 3]
        reasons = {m["message_id"]: m["reason"] for m in result["mentions"]}
        assert reasons[1] == "mention"
        assert reasons[2] == "substring"
        assert reasons[3] == "reply"
    asyncio.run(go())


def test_find_mentions_skips_channels(plugin, fake_client):
    async def go():
        ch = FakeChat(chat_id=30, title="Ads", username="ads",
                      type_name="CHANNEL")
        now = datetime.now(timezone.utc)
        fake_client.dialogs = [FakeDialog(chat=ch, unread=10)]
        fake_client.history = {30: [
            FakeMessage(message_id=1, text="@me promo!",
                        from_user=FakeUser(user_id=1, username="ad"),
                        date=now, mentioned=True),
        ]}
        out = await plugin.tg_find_mentions({})
        result = json.loads(out)
        assert result["count"] == 0
    asyncio.run(go())
