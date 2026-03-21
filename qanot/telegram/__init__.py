"""Telegram adapter package — aiogram 3.x with configurable response modes."""

from __future__ import annotations

from qanot.telegram.adapter import TelegramAdapter
from qanot.telegram.formatting import _md_to_html, _sanitize_response, _split_text, MAX_MSG_LEN

__all__ = [
    "TelegramAdapter",
    "_md_to_html",
    "_sanitize_response",
    "_split_text",
    "MAX_MSG_LEN",
]
