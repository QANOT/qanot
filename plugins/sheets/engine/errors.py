"""Uzbek-friendly error mapping for the Sheets plugin.

Translates aiohttp + Google API errors into structured dicts the agent
can reason about and show to users. Keep messages actionable: every
branch should tell the user the next step.
"""

from __future__ import annotations

from typing import Any

from engine.auth import TokenRefreshError
from engine.client import SheetsAPIError


_FRIENDLY: dict[int, str] = {
    401: (
        "Google tokeni bekor qilingan yoki muddati tugagan. "
        "Menyu → Integratsiyalar → Google Sheets → qayta ulash."
    ),
    403: (
        "Ushbu sheet-ga ruxsat yo'q. drive.file ruxsati faqat siz tanlagan "
        "yoki agent o'zi yaratgan sheet-larga ishlaydi. Kerakli sheet-ni "
        "Google Picker orqali qayta tanlang."
    ),
    404: (
        "Sheet topilmadi — o'chirilgan bo'lishi yoki tanlangan sheet-lar "
        "ro'yxatida yo'qligidan. sheets_list_connected ni chaqirib tekshiring."
    ),
    409: "Konflikt — kimdir ayni damda o'sha jadvalni tahrir qilayotgan bo'lishi mumkin.",
    429: "Google API rate-limit. Bir necha soniya kutib qaytadan urinish kerak.",
    500: "Google tomonida ichki xatolik. Qayta urinib ko'ring.",
    502: "Google gateway xatoligi. Bir daqiqadan so'ng qayta urinish kerak.",
    503: "Google Sheets vaqtincha ishlamayapti.",
}


def map_exception(exc: Exception) -> dict[str, Any]:
    """Return a compact, user-actionable error dict."""
    if isinstance(exc, TokenRefreshError):
        return {
            "error": (
                "Google OAuth tokenini yangilab bo'lmadi. Ehtimol siz Qanot "
                "ruxsatini Google akkaunt sozlamalaridan olib tashlagansiz. "
                "Qayta ulash uchun: Integratsiyalar → Google Sheets."
            ),
            "type": "TokenRefreshError",
            "detail": str(exc)[:200],
        }

    if isinstance(exc, SheetsAPIError):
        friendly = _FRIENDLY.get(exc.status, "")
        err = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
        google_msg = err.get("message") if isinstance(err, dict) else None
        out: dict[str, Any] = {
            "error": friendly or google_msg or str(exc),
            "type": "SheetsAPIError",
            "http_status": exc.status,
        }
        if google_msg and friendly and google_msg != friendly:
            out["google_message"] = google_msg
        return out

    return {
        "error": str(exc) or "Sheets plugin internal error",
        "type": exc.__class__.__name__,
    }
