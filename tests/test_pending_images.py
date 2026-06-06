"""Tests for send_pending_images album batching (qanot/telegram/media.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qanot.telegram.media import send_pending_images


def _bot():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_media_group = AsyncMock()
    return bot


def _agent(paths):
    agent = MagicMock()
    agent.pop_pending_images = MagicMock(return_value=list(paths))
    return agent


@pytest.mark.asyncio
async def test_no_images_is_noop():
    bot = _bot()
    await send_pending_images(bot, 1, "u", _agent([]))
    bot.send_photo.assert_not_awaited()
    bot.send_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_image_sends_photo():
    bot = _bot()
    await send_pending_images(bot, 1, "u", _agent(["/a.png"]))
    bot.send_photo.assert_awaited_once()
    bot.send_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_images_send_one_album():
    bot = _bot()
    await send_pending_images(bot, 1, "u", _agent(["/a.png", "/b.png", "/c.png"]))
    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 3
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_than_ten_chunks_into_groups_of_ten():
    bot = _bot()
    await send_pending_images(bot, 1, "u", _agent([f"/{i}.png" for i in range(12)]))
    assert bot.send_media_group.await_count == 2  # 10 + 2
    sizes = [len(c.kwargs["media"]) for c in bot.send_media_group.await_args_list]
    assert sizes == [10, 2]


@pytest.mark.asyncio
async def test_thread_id_propagated_to_album():
    bot = _bot()
    await send_pending_images(bot, 1, "u", _agent(["/a.png", "/b.png"]), thread_id=42)
    assert bot.send_media_group.await_args.kwargs["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_album_failure_falls_back_to_singles():
    bot = _bot()
    bot.send_media_group = AsyncMock(side_effect=RuntimeError("album rejected"))
    await send_pending_images(bot, 1, "u", _agent(["/a.png", "/b.png"]))
    assert bot.send_photo.await_count == 2  # fell back to individual sends
