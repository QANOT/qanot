"""Error mapping for the Notion plugin.

Converts `notion-client` exceptions into structured JSON payloads the agent
can reason about, with user-actionable messages in Uzbek + English.
"""

from __future__ import annotations

from typing import Any

# `notion-client` exposes APIResponseError / APIErrorCode / HTTPResponseError.
# We import lazily inside the mapping function so the plugin still imports
# cleanly when the dependency isn't installed yet (e.g. during initial
# container bootstrap).


_FRIENDLY: dict[str, str] = {
    "unauthorized": (
        "Notion token noto‘g‘ri yoki muddati o‘tgan. "
        "Token ni notion.so/my-integrations dan qaytadan oling."
    ),
    "restricted_resource": (
        "Bu sahifa/database hozircha integratsiyaga ulashilmagan. "
        "Notion da sahifani oching → «…» → Add connections → Qanot integratsiyasini tanlang."
    ),
    "object_not_found": (
        "So‘ralgan sahifa yoki database topilmadi (yoki integratsiyaga ko‘rinmaydi)."
    ),
    "validation_error": (
        "So‘rov noto‘g‘ri formatlangan — filter yoki property nomi database sxemasiga mos kelmasligi mumkin."
    ),
    "rate_limited": (
        "Notion API rate-limit (3 req/s). Biroz kutib qayta urinamiz."
    ),
    "conflict_error": (
        "Conflict — ehtimol bir vaqtning o‘zida boshqa tahrir yoki allaqachon yaratilgan."
    ),
    "internal_server_error": "Notion tomonidan ichki xatolik. Biroz kutib qayta urinamiz.",
    "service_unavailable": "Notion xizmati vaqtincha mavjud emas. Biroz kutib qayta urinamiz.",
}


def map_exception(exc: Exception) -> dict[str, Any]:
    """Return a structured error dict for the tool's JSON response."""
    # Try to pull code + body off notion-client exceptions without importing
    # at module level (dep may not be installed during dev).
    try:
        from notion_client.errors import APIResponseError, HTTPResponseError
    except Exception:
        APIResponseError = None  # type: ignore
        HTTPResponseError = None  # type: ignore

    code: str | None = None
    status: int | None = None
    body: str | None = None

    if APIResponseError is not None and isinstance(exc, APIResponseError):
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None)
        body = getattr(exc, "body", None)
    elif HTTPResponseError is not None and isinstance(exc, HTTPResponseError):
        status = getattr(exc, "status", None)
        body = getattr(exc, "body", None)

    friendly = _FRIENDLY.get(str(code), "")
    message = friendly or str(exc) or "Unknown Notion API error"

    out: dict[str, Any] = {
        "error": message,
        "type": exc.__class__.__name__,
    }
    if code:
        out["code"] = str(code)
    # Use http_status to avoid clashing with the plugin's own "status" field
    # (which callers use as "ok|error|unconfigured").
    if status is not None:
        out["http_status"] = int(status)
    # body can be large; truncate to keep tool responses compact.
    if body:
        out["body_preview"] = str(body)[:400]
    return out
