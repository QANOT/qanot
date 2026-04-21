"""Tests for TelegramClient — stubs aiohttp, covers routing + error mapping."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from tg_engine.client import (  # noqa: E402
    TelegramClient,
    make_inline_keyboard,
)
from tg_engine.errors import TelegramAPIError, map_exception  # noqa: E402


class _FakeResponse:
    def __init__(self, status: int, body: dict | str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def json(self, content_type=None):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")

    async def text(self):
        return str(self._body)


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, *, json=None, data=None):
        self.calls.append({"url": url, "json": json, "data": data})
        if not self._responses:
            raise AssertionError("no scripted response")
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


def _client(responses: list[_FakeResponse]) -> tuple[TelegramClient, _FakeSession]:
    c = TelegramClient("TEST_TOKEN")
    session = _FakeSession(responses)
    c._session = session  # type: ignore[attr-defined]
    return c, session


# ── _media_kind classification ──────────────────────────────────


def test_media_kind_url():
    c = TelegramClient("x")
    assert c._media_kind("https://example.com/a.jpg") == "url"
    assert c._media_kind("http://example.com/a.jpg") == "url"


def test_media_kind_file_id():
    c = TelegramClient("x")
    # Typical Telegram file_id format — arbitrary string
    assert c._media_kind("AgACAgIAAxkBAAIBcA") == "file_id"


def test_media_kind_file_path(tmp_path):
    c = TelegramClient("x")
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"fake")
    assert c._media_kind(str(f)) == "file_path"


# ── _call JSON path ─────────────────────────────────────────────


def test_call_raises_on_not_ok():
    c, session = _client([
        _FakeResponse(200, {"ok": False, "error_code": 403, "description": "Forbidden"}),
    ])
    with pytest.raises(TelegramAPIError) as exc:
        asyncio.run(c.get_chat("@foo"))
    assert exc.value.status == 403
    assert "Forbidden" in exc.value.description


def test_call_unwraps_result():
    c, session = _client([
        _FakeResponse(200, {"ok": True, "result": {"id": 123, "title": "X"}}),
    ])
    result = asyncio.run(c.get_chat("@foo"))
    assert result["id"] == 123


def test_send_message_payload_shape():
    c, session = _client([
        _FakeResponse(200, {"ok": True, "result": {"message_id": 42}}),
    ])
    asyncio.run(c.send_message(-1001, "Hello <b>world</b>"))
    call = session.calls[0]
    assert call["url"].endswith("/sendMessage")
    assert call["json"]["chat_id"] == -1001
    assert call["json"]["text"] == "Hello <b>world</b>"
    assert call["json"]["parse_mode"] == "HTML"


def test_send_photo_url_uses_json_path():
    c, session = _client([
        _FakeResponse(200, {"ok": True, "result": {"message_id": 99}}),
    ])
    result = asyncio.run(c.send_photo(
        -1001, "https://example.com/x.jpg", caption="hi", has_spoiler=True,
    ))
    assert result["message_id"] == 99
    call = session.calls[0]
    assert call["url"].endswith("/sendPhoto")
    # URL path → JSON body, no multipart
    assert call["json"] is not None
    assert call["json"]["photo"] == "https://example.com/x.jpg"
    assert call["json"]["has_spoiler"] is True


def test_send_poll_rejects_bad_options():
    c, session = _client([])
    with pytest.raises(TelegramAPIError):
        asyncio.run(c.send_poll(-1001, "Q?", ["only-one"]))


def test_send_poll_quiz_requires_correct_option():
    c, session = _client([])
    with pytest.raises(TelegramAPIError):
        asyncio.run(c.send_poll(
            -1001, "Q?", ["A", "B"], poll_type="quiz",
        ))


def test_send_poll_quiz_happy_path():
    c, session = _client([
        _FakeResponse(200, {"ok": True, "result": {"message_id": 7, "poll": {"id": "p1"}}}),
    ])
    result = asyncio.run(c.send_poll(
        -1001, "2+2?", ["3", "4", "5"],
        poll_type="quiz", correct_option_id=1,
        explanation="basic math",
    ))
    assert result["message_id"] == 7
    call = session.calls[0]["json"]
    assert call["type"] == "quiz"
    assert call["correct_option_id"] == 1
    assert call["explanation"] == "basic math"


def test_send_media_group_rejects_wrong_size():
    c, session = _client([])
    with pytest.raises(TelegramAPIError):
        asyncio.run(c.send_media_group(-1001, [
            {"type": "photo", "media": "https://x/1.jpg"},
        ]))


def test_send_media_group_rejects_local_files():
    c, session = _client([])
    with pytest.raises(TelegramAPIError) as exc:
        # Will fail because the path exists check returns file_path
        asyncio.run(c.send_media_group(-1001, [
            {"type": "photo", "media": "/tmp"},  # /tmp exists
            {"type": "photo", "media": "https://x/2.jpg"},
        ]))
    assert "local file" in str(exc.value).lower()


def test_send_media_group_happy_path():
    c, session = _client([
        _FakeResponse(200, {"ok": True, "result": [
            {"message_id": 10}, {"message_id": 11}, {"message_id": 12},
        ]}),
    ])
    result = asyncio.run(c.send_media_group(-1001, [
        {"type": "photo", "media": "https://x/1.jpg", "caption": "album"},
        {"type": "photo", "media": "https://x/2.jpg"},
        {"type": "video", "media": "https://x/3.mp4"},
    ]))
    assert len(result) == 3
    assert result[0]["message_id"] == 10


def test_stop_poll():
    c, session = _client([
        _FakeResponse(200, {"ok": True, "result": {"id": "p1", "is_closed": True}}),
    ])
    result = asyncio.run(c.stop_poll(-1001, 42))
    assert result["is_closed"] is True


# ── make_inline_keyboard ─────────────────────────────────────────


def test_inline_keyboard_url_button():
    kb = make_inline_keyboard([
        [{"text": "Buy", "url": "https://shop.uz/123"}],
    ])
    assert kb == {
        "inline_keyboard": [
            [{"text": "Buy", "url": "https://shop.uz/123"}],
        ],
    }


def test_inline_keyboard_callback_button():
    kb = make_inline_keyboard([
        [{"text": "Vote", "callback_data": "yes"}],
    ])
    assert kb["inline_keyboard"][0][0]["callback_data"] == "yes"


def test_inline_keyboard_missing_target_defaults_to_noop():
    kb = make_inline_keyboard([
        [{"text": "Click"}],  # no url, no callback_data
    ])
    assert kb["inline_keyboard"][0][0]["callback_data"] == "noop"


def test_inline_keyboard_drops_empty_text():
    kb = make_inline_keyboard([
        [{"text": "", "url": "x"}, {"text": "Ok", "url": "y"}],
    ])
    assert len(kb["inline_keyboard"][0]) == 1
    assert kb["inline_keyboard"][0][0]["text"] == "Ok"


# ── map_exception ────────────────────────────────────────────────


def test_map_exception_friendly_bot_not_admin():
    exc = TelegramAPIError(400, "Bad Request: Bot is not a member of the channel chat")
    mapped = map_exception(exc)
    assert "admin" in mapped["error"].lower()


def test_map_exception_friendly_chat_not_found():
    exc = TelegramAPIError(400, "Bad Request: chat not found")
    mapped = map_exception(exc)
    assert "Kanal topilmadi" in mapped["error"]


def test_map_exception_unknown_falls_back_to_description():
    exc = TelegramAPIError(500, "Internal Server Error")
    mapped = map_exception(exc)
    assert "Internal Server Error" in mapped["error"] or "500" in mapped["error"]


def test_map_exception_non_telegram_error():
    mapped = map_exception(ValueError("something else"))
    assert mapped["type"] == "ValueError"
    assert "something else" in mapped["error"]
