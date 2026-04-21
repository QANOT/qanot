"""Uzbek-friendly error mapping for Telegram Bot API calls.

Maps the common failure modes the agent will hit when managing channels:
  - Bot not added as admin / missing required permission
  - Channel not found (typo, deleted, private, bot not in it)
  - Rate limits (429)
  - Telegram-side service issues (5xx)
"""

from __future__ import annotations

from typing import Any


class TelegramAPIError(Exception):
    """Raised on non-ok Telegram Bot API response."""

    def __init__(self, status: int, description: str, parameters: dict | None = None) -> None:
        self.status = status
        self.description = description or ""
        self.parameters = parameters or {}
        super().__init__(f"[{status}] {description}")


# Friendly Uzbek messages keyed by common Telegram error descriptions.
# Telegram errors are plain-text strings (not codes), so we match by
# substring. Keep keys lowercased; we case-fold on lookup.
_FRIENDLY: list[tuple[str, str]] = [
    (
        "chat not found",
        (
            "Kanal topilmadi. Username to'g'ri yozilganini tekshiring "
            "(@kanal_username) yoki bot shu kanalga qo'shilgan bo'lishi kerak."
        ),
    ),
    (
        "bot is not a member of the channel chat",
        (
            "Bot bu kanalda yo'q. Avval kanalni oching → Administratorlar → "
            "ushbu bot-ni admin qilib qo'shing (post yuborish ruxsati bilan)."
        ),
    ),
    (
        "not enough rights",
        (
            "Bot-ning ruxsati yetarli emas. Kanal sozlamalarida bot-ga "
            "\"Xabar yuborish\", \"Tahrirlash\", \"O'chirish\", \"Qadalgan xabar\" "
            "ruxsatlarini bering."
        ),
    ),
    (
        "have no rights to send a message",
        (
            "Bot xabar yuborish ruxsatiga ega emas. Kanal Administratorlarida "
            "bot-ga \"Post messages\" ruxsatini bering."
        ),
    ),
    (
        "message to edit not found",
        "Tahrirlanadigan xabar topilmadi (o'chirib yuborilgan yoki message_id noto'g'ri).",
    ),
    (
        "message to delete not found",
        "O'chiriladigan xabar topilmadi (allaqachon o'chirilgan yoki message_id noto'g'ri).",
    ),
    (
        "message is not modified",
        "Yangi matn avvalgi matn bilan bir xil — hech narsa o'zgarmadi.",
    ),
    (
        "message is too long",
        "Xabar juda uzun (Telegram limiti 4096 belgi). Qisqartiring yoki bo'lib yuboring.",
    ),
    (
        "too many requests",
        "Telegram rate-limit. Bir necha soniya kutib qayta urinamiz.",
    ),
    (
        "forbidden",
        (
            "Taqiqlangan amal. Ehtimol bot kanaldan chiqarib yuborilgan yoki "
            "ruxsatlari olib tashlangan."
        ),
    ),
    (
        "bad request",
        "So'rov noto'g'ri formatlangan. Parametrlarni tekshiring.",
    ),
]


def map_exception(exc: Exception) -> dict[str, Any]:
    """Return a compact, user-actionable error dict for tool JSON responses."""
    if isinstance(exc, TelegramAPIError):
        desc = exc.description.lower()
        friendly = ""
        for needle, message in _FRIENDLY:
            if needle in desc:
                friendly = message
                break
        out: dict[str, Any] = {
            "error": friendly or exc.description or f"Telegram API error ({exc.status})",
            "type": "TelegramAPIError",
            "http_status": exc.status,
        }
        if exc.description and friendly and exc.description.lower() not in friendly.lower():
            out["telegram_description"] = exc.description
        if exc.parameters:
            out["parameters"] = exc.parameters
        return out

    return {
        "error": str(exc) or "Telegram plugin internal error",
        "type": exc.__class__.__name__,
    }
