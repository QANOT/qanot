"""Shared HTTP client for the qanot-video render service.

Extracted verbatim from qanot/tools/video.py so that more than one caller
can submit renders without duplicating the retry/poll/stream logic:

- ``render_video`` tool (LLM-authored compositions) — qanot/tools/video.py
- ``plugins/reels`` legacy pipeline (asset-driven compositions) routed
  through the service instead of a local ``hyperframes`` CLI

Behaviour is intentionally identical to the original private helpers; the
old names are kept as aliases in qanot/tools/video.py for backward
compatibility (tests and call sites that import the underscored names).

The service binds to 127.0.0.1 and is reached over the Docker network on
the same host — never the public internet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

# HTTP client behavior. Service typically responds in <50ms; the 30s
# connect timeout is the kernel-level retry safety net, not a steady-state
# expectation.
HTTP_CONNECT_TIMEOUT_S = 10.0
HTTP_REQUEST_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 2.0
POLL_PROGRESS_EDIT_EVERY_NTH = 3  # invoke progress callback every Nth poll

# Submit retry policy: exponential backoff on network/5xx errors.
SUBMIT_RETRIES = 3
SUBMIT_BACKOFF_BASE_S = 1.0


# ── Errors ──────────────────────────────────────────────────────────────


class ServiceUnavailable(Exception):
    """Raised when the render service is unreachable after retries."""


# ── HTTP operations ─────────────────────────────────────────────────────


async def submit_render(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    bearer: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST /render with exponential-backoff retries. Returns the job dict."""
    last_exc: Exception | None = None
    for attempt in range(SUBMIT_RETRIES + 1):
        try:
            resp = await client.post(
                f"{base_url}/render",
                headers={"Authorization": f"Bearer {bearer}"},
                json=payload,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
        else:
            # 5xx -> retry; 4xx -> surface as service error (validation, etc.)
            if 500 <= resp.status_code < 600:
                last_exc = httpx.HTTPStatusError(
                    "service 5xx", request=resp.request, response=resp,
                )
            else:
                resp.raise_for_status()
                return resp.json()
        if attempt < SUBMIT_RETRIES:
            await asyncio.sleep(SUBMIT_BACKOFF_BASE_S * (2 ** attempt))
    raise ServiceUnavailable(f"render service unreachable: {last_exc}")


async def poll_job(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    bearer: str,
    job_id: str,
    on_progress: Callable[[dict[str, Any]], asyncio.Future[None] | None] | None = None,
    on_progress_async: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Poll GET /jobs/:id until the job hits a terminal state. Returns the
    final status dict. Calls on_progress every Nth iteration with the latest
    status, so the caller can update Telegram."""
    poll_count = 0
    while True:
        resp = await client.get(
            f"{base_url}/jobs/{job_id}",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        resp.raise_for_status()
        status = resp.json()
        state = status.get("status")
        if state in ("succeeded", "failed", "cancelled", "expired"):
            return status
        poll_count += 1
        if on_progress_async and (poll_count % POLL_PROGRESS_EDIT_EVERY_NTH == 0):
            try:
                await on_progress_async(status)
            except Exception as exc:  # noqa: BLE001 — progress is best-effort
                logger.debug("progress callback failed: %s", exc)
        elif on_progress and (poll_count % POLL_PROGRESS_EDIT_EVERY_NTH == 0):
            try:
                result = on_progress(status)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.debug("progress callback failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_S)


async def download_output(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    bearer: str,
    job_id: str,
    dest_path: Path,
) -> None:
    """Stream MP4 to disk."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    async with client.stream(
        "GET",
        f"{base_url}/jobs/{job_id}/output",
        headers={"Authorization": f"Bearer {bearer}"},
    ) as resp:
        resp.raise_for_status()
        with dest_path.open("wb") as fh:
            async for chunk in resp.aiter_bytes():
                fh.write(chunk)
