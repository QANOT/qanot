"""OAuth token refresh for the Sheets plugin.

Holds a Google OAuth refresh_token and trades it for short-lived access
tokens on demand. Access tokens are cached in-memory until shortly before
their stated expiry (the skew avoids 401 races on in-flight requests).

Refresh happens under an asyncio.Lock so N concurrent callers don't each
hit Google's token endpoint when the cached token goes stale.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Refresh this many seconds before the stated expiry so an in-flight
# request can't land with an already-expired token.
REFRESH_SKEW_SECONDS = 60


class TokenRefreshError(RuntimeError):
    """Raised when Google rejects the refresh_token (revoked, expired, etc)."""


class TokenManager:
    """Refresh-token → access-token exchange with in-memory cache."""

    def __init__(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing through Google if needed."""
        if self._is_fresh():
            return self._access_token  # type: ignore[return-value]
        async with self._lock:
            # Double-check: another coroutine may have refreshed while we waited.
            if self._is_fresh():
                return self._access_token  # type: ignore[return-value]
            await self._refresh()
            return self._access_token  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Drop cached access token — next get_access_token() refreshes."""
        self._access_token = None
        self._expires_at = 0.0

    def _is_fresh(self) -> bool:
        return bool(
            self._access_token
            and time.time() < self._expires_at - REFRESH_SKEW_SECONDS
        )

    async def _refresh(self) -> None:
        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_TOKEN_URL,
                data=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"error": "invalid_response", "raw": (await resp.text())[:400]}
                if resp.status != 200 or "access_token" not in data:
                    raise TokenRefreshError(
                        f"Google token refresh failed ({resp.status}): "
                        f"{data.get('error_description') or data.get('error') or data}"
                    )

        self._access_token = data["access_token"]
        # Google returns expires_in in seconds; default to 1h if missing.
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        logger.debug(
            "Google access token refreshed (expires in %ss)",
            data.get("expires_in", 3600),
        )
