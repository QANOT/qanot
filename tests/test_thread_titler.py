"""Tests for qanot/thread_titler.py — auto-rename private threads.

Focus: pure logic of the titler (normalisation, gating, persistence,
single-fire-per-thread) with fakes for the Bot and provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qanot.thread_titler import (
    TITLE_MAX_LEN,
    TITLED_FILENAME,
    ThreadTitler,
    _normalise_title,
)


# ────────── Title normalisation ──────────

def test_normalise_strips_double_quotes():
    assert _normalise_title('"Oltin tahlili"') == "Oltin tahlili"


def test_normalise_strips_single_quotes():
    assert _normalise_title("'Python yordami'") == "Python yordami"


def test_normalise_strips_curly_quotes():
    assert _normalise_title("“Mahalla AI”") == "Mahalla AI"


def test_normalise_strips_trailing_punctuation():
    assert _normalise_title("Tanishuv.") == "Tanishuv"
    assert _normalise_title("Salom!") == "Salom"
    assert _normalise_title("Savol?") == "Savol"
    assert _normalise_title("Mavzu...") == "Mavzu"


def test_normalise_keeps_internal_punctuation():
    # We don't want to mangle titles like "AI/ML qo'shimchasi" — only
    # trailing terminators get trimmed.
    assert _normalise_title("AI/ML yordami") == "AI/ML yordami"


def test_normalise_takes_first_line_only():
    raw = "Oltin tahlili\nA longer description on a second line"
    assert _normalise_title(raw) == "Oltin tahlili"


def test_normalise_truncates_overlong_titles_on_word_boundary():
    raw = "Bu juda uzun thread nomi atrofda yana yana ko'p so'zlar bilan"
    result = _normalise_title(raw)
    assert len(result) <= TITLE_MAX_LEN
    # Word-boundary truncation: must not end mid-word.
    assert not result.endswith(" ")
    # Sanity: starts with the original prefix.
    assert result.startswith("Bu juda")


def test_normalise_empty_input():
    assert _normalise_title("") == ""
    assert _normalise_title("   ") == ""


# ────────── Titler fixtures ──────────


class _FakeProvider:
    """Stand-in for an LLM provider with a writable .model attribute."""

    def __init__(self, *, reply: str = "Tanishuv"):
        self.model = "claude-sonnet-4-6"
        self._reply = reply
        self.calls: list[dict] = []
        self.raise_on_chat: Exception | None = None
        self.observed_models: list[str] = []

    async def chat(self, *, messages, tools, system):
        # Record the model AT CALL TIME — confirms the titler swapped it.
        self.observed_models.append(self.model)
        self.calls.append({"messages": messages, "system": system})
        if self.raise_on_chat is not None:
            raise self.raise_on_chat
        return SimpleNamespace(content=self._reply, stop_reason="end_turn")


class _FakeBot:
    def __init__(self):
        self.edit_calls: list[dict] = []
        self.raise_on_edit: Exception | None = None

    async def __call__(self, method):
        # We accept the EditForumTopic instance and record its fields.
        if self.raise_on_edit is not None:
            raise self.raise_on_edit
        self.edit_calls.append({
            "chat_id": getattr(method, "chat_id", None),
            "message_thread_id": getattr(method, "message_thread_id", None),
            "name": getattr(method, "name", None),
        })
        return True


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


def _make_titler(workspace, *, reply="Tanishuv"):
    return ThreadTitler(
        bot=_FakeBot(),
        provider=_FakeProvider(reply=reply),
        workspace_dir=workspace,
    )


async def _drain():
    """Yield control so the background task can complete."""
    for _ in range(3):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


# ────────── Gating ──────────


def test_should_title_true_for_new_thread(workspace):
    t = _make_titler(workspace)
    assert t.should_title(100, 5) is True


def test_should_title_false_without_thread_id(workspace):
    t = _make_titler(workspace)
    assert t.should_title(100, None) is False
    assert t.should_title(100, 0) is False


def test_should_title_false_after_titled(workspace):
    t = _make_titler(workspace)
    t._titled.add("100:5")
    assert t.should_title(100, 5) is False


def test_should_title_false_while_in_flight(workspace):
    t = _make_titler(workspace)
    t._in_flight.add("100:5")
    assert t.should_title(100, 5) is False


# ────────── End-to-end title flow ──────────


def test_titles_a_fresh_thread(workspace):
    t = _make_titler(workspace, reply="Salomlashish")

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="salom")
        await _drain()

    asyncio.run(go())

    assert t._bot.edit_calls == [{
        "chat_id": 100, "message_thread_id": 5, "name": "Salomlashish",
    }]
    assert "100:5" in t._titled


def test_skips_when_no_thread_id(workspace):
    t = _make_titler(workspace)

    async def go():
        await t.maybe_title(chat_id=100, thread_id=None, user_message="salom")
        await _drain()

    asyncio.run(go())

    assert t._bot.edit_calls == []
    assert t._titled == set()


def test_skips_after_already_titled(workspace):
    t = _make_titler(workspace, reply="First")

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="salom")
        await _drain()
        # Second message must NOT trigger another LLM/API call.
        await t.maybe_title(chat_id=100, thread_id=5, user_message="rahmat")
        await _drain()

    asyncio.run(go())

    assert len(t._bot.edit_calls) == 1
    assert t._provider.calls == t._provider.calls[:1]  # only one LLM call


def test_concurrent_messages_for_same_thread_fire_once(workspace):
    """Two near-simultaneous first messages must result in one title."""
    t = _make_titler(workspace, reply="Once")

    async def go():
        await asyncio.gather(
            t.maybe_title(chat_id=100, thread_id=5, user_message="msg1"),
            t.maybe_title(chat_id=100, thread_id=5, user_message="msg2"),
            t.maybe_title(chat_id=100, thread_id=5, user_message="msg3"),
        )
        await _drain()

    asyncio.run(go())

    assert len(t._bot.edit_calls) == 1
    assert len(t._provider.calls) == 1


# ────────── Persistence ──────────


def test_state_persisted_to_disk(workspace):
    t = _make_titler(workspace, reply="OK")

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="hi")
        await _drain()

    asyncio.run(go())

    state_path = Path(workspace) / TITLED_FILENAME
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert "100:5" in state


def test_state_survives_restart(workspace):
    t1 = _make_titler(workspace, reply="OK")

    async def first_run():
        await t1.maybe_title(chat_id=100, thread_id=5, user_message="hi")
        await _drain()

    asyncio.run(first_run())

    # Fresh titler on the same workspace inherits the titled set.
    t2 = _make_titler(workspace)
    assert t2.should_title(100, 5) is False


def test_corrupt_state_file_ignored(workspace):
    (Path(workspace) / TITLED_FILENAME).write_text("not json")
    t = _make_titler(workspace)
    assert t._titled == set()


def test_legacy_list_state_format_supported(workspace):
    """If an older version wrote a JSON list instead of a dict, load it."""
    (Path(workspace) / TITLED_FILENAME).write_text(json.dumps(["100:5", "100:6"]))
    t = _make_titler(workspace)
    assert t.should_title(100, 5) is False
    assert t.should_title(100, 6) is False
    assert t.should_title(100, 7) is True


# ────────── Model swap ──────────


def test_uses_haiku_model_for_title_generation(workspace):
    t = _make_titler(workspace, reply="Tanishuv")
    t._provider.model = "claude-sonnet-4-6"

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="salom")
        await _drain()

    asyncio.run(go())

    # The model at the moment chat() ran must be the Haiku title model.
    assert t._provider.observed_models == ["claude-haiku-4-5-20251001"]
    # And the original model is restored afterwards.
    assert t._provider.model == "claude-sonnet-4-6"


# ────────── Failure handling ──────────


def test_llm_failure_does_not_mark_titled(workspace):
    """If the LLM call fails, we leave the state empty so a retry can
    fire on the next message — better than permanently giving up."""
    t = _make_titler(workspace)
    t._provider.raise_on_chat = RuntimeError("provider down")

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="salom")
        await _drain()

    asyncio.run(go())

    assert t._bot.edit_calls == []
    assert "100:5" not in t._titled


def test_edit_forum_topic_failure_does_not_mark_titled(workspace):
    """Same retry-friendly behaviour when the Telegram call fails."""
    t = _make_titler(workspace, reply="Tanishuv")
    t._bot.raise_on_edit = RuntimeError("CHAT_NOT_FORUM")

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="salom")
        await _drain()

    asyncio.run(go())

    assert "100:5" not in t._titled


def test_empty_title_recorded_to_avoid_retry_storm(workspace):
    """If the LLM returns nothing usable (e.g. user sent pure emoji),
    record the attempt anyway — otherwise every subsequent message in
    the thread would re-fire the LLM call."""
    t = _make_titler(workspace, reply="")

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="🚀")
        await _drain()

    asyncio.run(go())

    assert t._bot.edit_calls == []  # no rename
    assert "100:5" in t._titled  # but recorded — no retry storm


def test_distinct_threads_get_independent_titles(workspace):
    """Two threads on the same chat must each get their own title."""
    bot = _FakeBot()
    provider = _FakeProvider(reply="Topic")
    t = ThreadTitler(bot=bot, provider=provider, workspace_dir=workspace)

    async def go():
        await t.maybe_title(chat_id=100, thread_id=5, user_message="forex")
        await t.maybe_title(chat_id=100, thread_id=6, user_message="python")
        await _drain()

    asyncio.run(go())

    assert len(bot.edit_calls) == 2
    seen_threads = {c["message_thread_id"] for c in bot.edit_calls}
    assert seen_threads == {5, 6}
