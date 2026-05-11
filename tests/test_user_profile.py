"""Tests for qanot/user_profile.py — Bot API 10.0 user enrichment.

We fake the aiogram Bot and the RAG indexer; the goal is to lock down
the throttling, persistence, and graceful-degradation behaviour so that
a misconfigured deployment never crashes the agent loop.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qanot.user_profile import (
    PROFILES_FILENAME,
    REENRICH_AFTER_SECONDS,
    UserProfileEnricher,
)


# ────────── Fakes ──────────

class FakeMessage:
    def __init__(self, *, text=None, caption=None):
        self.text = text
        self.caption = caption


class FakeBot:
    """Stand-in for an aiogram 3.28+ Bot.

    ``messages_by_user`` is a dict[int, list[FakeMessage]] — empty / missing
    means "no personal chat" (raises). Tests can also set ``raise_attr``
    to mimic aiogram < 3.28 where the method doesn't exist.
    """

    def __init__(self, *, messages_by_user=None, raise_attr=False):
        self._msgs = messages_by_user or {}
        self._raise_attr = raise_attr
        self.calls: list[tuple[int, int]] = []

    @property
    def get_user_personal_chat_messages(self):
        if self._raise_attr:
            raise AttributeError("simulated aiogram < 3.28")
        return self._call

    async def _call(self, *, user_id: int, limit: int):
        self.calls.append((user_id, limit))
        if user_id not in self._msgs:
            raise RuntimeError("no personal chat")
        return self._msgs[user_id]


class FakeIndexer:
    def __init__(self):
        self.calls: list[dict] = []
        self.should_raise: Exception | None = None

    async def index_text(self, text, *, source, user_id, metadata):
        if self.should_raise is not None:
            raise self.should_raise
        self.calls.append({
            "text": text,
            "source": source,
            "user_id": user_id,
            "metadata": metadata,
        })
        return ["chunk_1"]


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


@pytest.fixture
def indexer():
    return FakeIndexer()


def _make_enricher(workspace, bot, indexer):
    return UserProfileEnricher(
        bot=bot, indexer=indexer, workspace_dir=workspace,
    )


async def _drain_background_tasks():
    """Yield control until any scheduled task has run.

    ``maybe_enrich`` is fire-and-forget — we use a couple of event loop
    turns + a tiny sleep so the background task can complete fully
    (the awaitable chain includes JSON writes).
    """
    for _ in range(3):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


# ────────── Happy path ──────────

def test_enriches_new_user_with_text_posts(workspace, indexer):
    bot = FakeBot(messages_by_user={
        100: [
            FakeMessage(text="I run a stakan business"),
            FakeMessage(text="Wholesale only"),
            FakeMessage(caption="Logo photo caption"),
        ],
    })
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    assert len(indexer.calls) == 1
    call = indexer.calls[0]
    assert "stakan business" in call["text"]
    assert "Wholesale only" in call["text"]
    assert "Logo photo caption" in call["text"]
    assert call["source"] == "user_profile:100"
    assert call["user_id"] == "100"
    assert call["metadata"]["kind"] == "user_personal_channel"
    assert call["metadata"]["count"] == 3


def test_state_persisted_to_disk(workspace, indexer):
    bot = FakeBot(messages_by_user={100: [FakeMessage(text="hi")]})
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    state_path = Path(workspace) / PROFILES_FILENAME
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert "100" in state
    assert isinstance(state["100"], float)
    # Just-written timestamp must be very recent.
    assert (time.time() - state["100"]) < 5


# ────────── Throttling ──────────

def test_does_not_reenrich_within_cadence(workspace, indexer):
    bot = FakeBot(messages_by_user={100: [FakeMessage(text="post")]})
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()
        # Second call within cadence should NOT trigger a fetch.
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    assert len(bot.calls) == 1, "second maybe_enrich should be throttled"
    assert len(indexer.calls) == 1


def test_reenriches_after_cadence(workspace, indexer):
    bot = FakeBot(messages_by_user={100: [FakeMessage(text="post")]})
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()
        # Backdate the state to just past the cadence boundary.
        enricher._state["100"] = time.time() - REENRICH_AFTER_SECONDS - 1
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    assert len(bot.calls) == 2


def test_concurrent_calls_dedupe_per_user(workspace, indexer):
    """Two near-simultaneous messages from the same user must only fire
    one network call."""
    slow_bot = FakeBot(messages_by_user={100: [FakeMessage(text="x")]})
    original = slow_bot._call

    async def slow_call(*, user_id, limit):
        await asyncio.sleep(0.02)
        return await original(user_id=user_id, limit=limit)

    slow_bot._call = slow_call
    enricher = _make_enricher(workspace, slow_bot, indexer)

    async def go():
        await asyncio.gather(
            enricher.maybe_enrich(100),
            enricher.maybe_enrich(100),
            enricher.maybe_enrich(100),
        )
        await asyncio.sleep(0.1)  # let the background task finish

    asyncio.run(go())

    assert len(slow_bot.calls) == 1


# ────────── State loading ──────────

def test_state_survives_restart(workspace, indexer):
    bot = FakeBot(messages_by_user={100: [FakeMessage(text="post")]})

    async def first_run():
        e1 = _make_enricher(workspace, bot, indexer)
        await e1.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(first_run())

    # Fresh enricher on the same workspace — must inherit the timestamp.
    e2 = _make_enricher(workspace, bot, indexer)
    assert "100" in e2._state
    assert not e2.should_enrich("100")


def test_corrupt_state_file_ignored(workspace, indexer):
    state_path = Path(workspace) / PROFILES_FILENAME
    state_path.write_text("{not valid json")
    bot = FakeBot(messages_by_user={100: [FakeMessage(text="post")]})

    # Construction must not raise; state must be empty.
    enricher = _make_enricher(workspace, bot, indexer)
    assert enricher._state == {}


def test_state_file_with_garbage_values_filtered(workspace, indexer):
    state_path = Path(workspace) / PROFILES_FILENAME
    state_path.write_text(json.dumps({
        "100": 1234567.0,
        "200": "not-a-number",
        "300": None,
        "400": 9876543.5,
    }))
    bot = FakeBot()
    enricher = _make_enricher(workspace, bot, indexer)
    assert set(enricher._state.keys()) == {"100", "400"}


# ────────── Edge cases ──────────

def test_empty_personal_chat_records_timestamp(workspace, indexer):
    """User has no pinned channel → API call raises → we still record the
    attempt so we don't retry-storm on every message."""
    bot = FakeBot(messages_by_user={})  # any user_id will miss
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(999)
        await _drain_background_tasks()

    asyncio.run(go())

    assert indexer.calls == []
    # Timestamp recorded → won't retry immediately.
    assert "999" in enricher._state


def test_messages_with_no_text_skipped(workspace, indexer):
    bot = FakeBot(messages_by_user={
        100: [
            FakeMessage(text=None, caption=None),  # media-only, no caption
            FakeMessage(text="  "),                 # whitespace
            FakeMessage(text=""),                   # empty
        ],
    })
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    # No non-empty text → nothing indexed, but attempt is recorded.
    assert indexer.calls == []
    assert "100" in enricher._state


def test_aiogram_too_old_records_timestamp(workspace, indexer):
    bot = FakeBot(raise_attr=True)
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    assert indexer.calls == []
    assert "100" in enricher._state


def test_indexer_failure_records_timestamp(workspace, indexer):
    bot = FakeBot(messages_by_user={100: [FakeMessage(text="post")]})
    indexer.should_raise = RuntimeError("vec store offline")
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(100)
        await _drain_background_tasks()

    asyncio.run(go())

    # We tried, indexer crashed — record the attempt so we don't keep
    # hammering a broken vec store on every message.
    assert "100" in enricher._state


def test_none_user_id_noop(workspace, indexer):
    bot = FakeBot()
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(None)
        await _drain_background_tasks()

    asyncio.run(go())
    assert bot.calls == []
    assert indexer.calls == []


def test_non_integer_user_id_noop(workspace, indexer):
    bot = FakeBot()
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich("not-an-int")
        await _drain_background_tasks()

    asyncio.run(go())
    # Bot is never called because int() conversion fails inside _do_enrich.
    assert bot.calls == []
    assert indexer.calls == []


def test_integer_user_id_accepted(workspace, indexer):
    bot = FakeBot(messages_by_user={42: [FakeMessage(text="x")]})
    enricher = _make_enricher(workspace, bot, indexer)

    async def go():
        await enricher.maybe_enrich(42)  # int, not str
        await _drain_background_tasks()

    asyncio.run(go())

    assert bot.calls == [(42, 20)]
    assert indexer.calls[0]["source"] == "user_profile:42"


def test_should_enrich_returns_false_for_recent(workspace, indexer):
    bot = FakeBot()
    enricher = _make_enricher(workspace, bot, indexer)
    enricher._state["100"] = time.time() - 60
    assert enricher.should_enrich("100") is False


def test_should_enrich_returns_true_for_unknown(workspace, indexer):
    bot = FakeBot()
    enricher = _make_enricher(workspace, bot, indexer)
    assert enricher.should_enrich("999") is True


def test_should_enrich_returns_true_after_cadence(workspace, indexer):
    bot = FakeBot()
    enricher = _make_enricher(workspace, bot, indexer)
    enricher._state["100"] = time.time() - REENRICH_AFTER_SECONDS - 10
    assert enricher.should_enrich("100") is True
