"""Video generation via the qanot-video render service.

Per docs/video-engine/ARCHITECTURE.md §4 (Python Bridge).

User says "30s video about X" -> render_video tool:

  1. Rate-limit + cost-cap checks (per-user + per-bot, sliding daily window)
  2. Composition sub-agent (Sonnet, single-turn) writes HTML+GSAP from brief,
     using the skill at templates/workspace/skills/hyperframes/SKILL.md and
     the optional per-bot DESIGN.md from workspace_dir/DESIGN.md
  3. POST composition to the render service (services/video on port 8770)
  4. Poll until succeeded/failed, editing a Telegram progress draft
  5. On lint_failed, retry composition once with the lint errors as feedback
  6. On success, download the MP4, push to Agent._pending_videos so the
     Telegram adapter delivers it after the turn

The service binds to 127.0.0.1, so this module ONLY runs inside the same
host (Docker network or localhost).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

# Sonnet input/output token cost (USD per 1M tokens) — keep in sync with
# Anthropic pricing. Used for cost-cap accounting on composition writes.
_SONNET_INPUT_USD_PER_MTOK = 3.0
_SONNET_OUTPUT_USD_PER_MTOK = 15.0

# Composition system prompt is ~5-10 KB skill + 1-2 KB design + brief.
# Output is ~2-5 KB HTML. Estimate generously per call.
_COMPOSITION_INPUT_TOKEN_BUDGET = 12_000
_COMPOSITION_OUTPUT_TOKEN_BUDGET = 4_096

# HTTP client behavior. Service typically responds in <50ms; the 30s
# connect timeout is the kernel-level retry safety net, not a steady-state
# expectation.
_HTTP_CONNECT_TIMEOUT_S = 10.0
_HTTP_REQUEST_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 2.0
_POLL_PROGRESS_EDIT_EVERY_NTH = 3  # edit Telegram message every Nth poll

# Submit retry policy: exponential backoff on network/5xx errors.
_SUBMIT_RETRIES = 3
_SUBMIT_BACKOFF_BASE_S = 1.0

# Composition retry: at most one retry-with-feedback after lint_failed.
_COMPOSITION_RETRY_BUDGET = 1

# Output cache (24h matches the service's retention).
_OUTPUT_CACHE_DIRNAME = "video_renders"

# Composition validation
_DOCTYPE_PREFIXES = ("<!doctype html", "<!doctype  html")

# Stage labels (English from service -> Uzbek for end users).
_STAGE_LABELS_UZ: dict[str, str] = {
    "queued": "navbatda",
    "linting": "tekshirilmoqda",
    "rendering_frames": "kadrlar yaratilmoqda",
    "encoding_video": "video kodlanmoqda",
    "succeeded": "tayyor",
    "failed": "xato",
    "cancelled": "bekor qilindi",
    "expired": "muddati tugadi",
}

# Error code (from service) -> human Uzbek message.
_ERROR_MESSAGES_UZ: dict[str, str] = {
    "lint_failed": "Video kompozitsiyasi noto'g'ri yaratildi. Iltimos, qaytadan urinib ko'ring.",
    "render_timeout": "Video render qilish vaqti oshib ketdi (timeout). Qisqaroq video yoki oddiyroq dizayn bilan urinib ko'ring.",
    "chrome_crash": "Render dvigateli ishdan chiqdi. Iltimos, qaytadan urinib ko'ring.",
    "asset_fetch_failed": "Video uchun zarur manba (rasm/shrift) yuklab olinmadi.",
    "oom_killed": "Render server xotirasi tugadi. Bir daqiqadan keyin qayta urinib ko'ring.",
    "internal": "Render xizmatida ichki xato. Yana bir bor urinib ko'ring.",
    "service_unavailable": "Video xizmati hozir ishlamayapti. Bir daqiqadan keyin urinib ko'ring.",
    "rate_limited": "Bugungi video limitiga yetdingiz.",
    "cost_capped": "Bugungi xarajat chegarasiga yetdingiz.",
    "composition_invalid": "Avtomatik kompozitsiya yaratish chegaradan ko'p marta muvaffaqiyatsiz bo'ldi.",
}


# ── Skill + DESIGN.md loading ──────────────────────────────────────────


def _find_skill_path() -> Path | None:
    """Locate the HyperFrames skill file shipped with the framework.

    Tries three locations in order:
      1. {workspace_dir}/skills/hyperframes/SKILL.md (operator-installed copy)
      2. <repo_root>/templates/workspace/skills/hyperframes/SKILL.md (pipx
         installs copy this into site-packages on install)
      3. None — disable composition skill injection (still works, lower quality)

    workspace lookup is left to the per-call function since it varies per bot.
    """
    # The packaged template lives next to the Python source tree. We resolve
    # relative to this file so it works in both editable installs and wheel
    # installs.
    here = Path(__file__).resolve()
    # qanot/tools/video.py -> qanot/tools -> qanot -> repo root
    repo_candidate = here.parent.parent.parent / "templates" / "workspace" / "skills" / "hyperframes" / "SKILL.md"
    if repo_candidate.is_file():
        return repo_candidate
    return None


_skill_text_cache: str | None = None


def _load_skill_text(workspace_dir: str | None) -> str:
    """Load the skill prompt text. Cached after first read.

    Workspace-installed copies override the packaged one, so an operator can
    customize the skill per bot by dropping a file at
    ``{workspace_dir}/skills/hyperframes/SKILL.md``.
    """
    global _skill_text_cache  # noqa: PLW0603 — module-level cache is intentional

    if workspace_dir:
        ws_path = Path(workspace_dir) / "skills" / "hyperframes" / "SKILL.md"
        if ws_path.is_file():
            try:
                return ws_path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("Failed reading workspace skill %s, falling back", ws_path)

    if _skill_text_cache is not None:
        return _skill_text_cache

    pkg_path = _find_skill_path()
    if pkg_path is None:
        logger.warning("No HyperFrames SKILL.md found; composition quality will be reduced")
        _skill_text_cache = ""
        return ""

    try:
        _skill_text_cache = pkg_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read skill at %s: %s", pkg_path, exc)
        _skill_text_cache = ""
    return _skill_text_cache or ""


def _load_design_text(workspace_dir: str | None) -> str:
    """Load per-bot DESIGN.md if present. Empty string otherwise."""
    if not workspace_dir:
        return ""
    path = Path(workspace_dir) / "DESIGN.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed reading DESIGN.md at %s: %s", path, exc)
        return ""


# ── Composition prompt + validation ─────────────────────────────────────


def _build_composition_system_prompt(
    *,
    skill: str,
    design: str,
    duration_seconds: int,
    aspect_format: str,
) -> str:
    """Assemble the system prompt for the composition sub-agent."""
    width, height = _format_to_dimensions(aspect_format)
    parts: list[str] = [
        "You are a video composition author for the HyperFrames rendering engine.",
        "",
        "Output STRICTLY a single valid HTML5 document. No markdown, no commentary,",
        "no preamble, no code fences. The first non-whitespace characters must be",
        "`<!doctype html>` (case-insensitive).",
        "",
    ]
    if skill:
        parts.append("# HyperFrames composition guide")
        parts.append("")
        parts.append(skill.strip())
        parts.append("")
    if design:
        parts.append("# Brand for this bot")
        parts.append("")
        parts.append(design.strip())
        parts.append("")
    parts.extend([
        "# This render's constraints",
        "",
        f"- Duration: exactly {duration_seconds} seconds. The GSAP timeline",
        f"  for the root composition MUST end at t={duration_seconds}.",
        f"- Aspect ratio: {aspect_format}",
        f"- Canvas: data-width=\"{width}\" data-height=\"{height}\"",
        "- Frame rate: 30 fps",
        "- Asset URLs must be HTTPS or local data: URIs. No file:// outside",
        "  the asset directory.",
    ])
    return "\n".join(parts)


def _format_to_dimensions(fmt: str) -> tuple[int, int]:
    """Map aspect format to canvas dimensions."""
    if fmt == "9:16":
        return 1080, 1920
    if fmt == "16:9":
        return 1920, 1080
    if fmt == "1:1":
        return 1080, 1080
    raise ValueError(f"Unsupported format {fmt!r}; expected 9:16, 16:9 or 1:1")


def _validate_composition_html(html: str) -> tuple[bool, str]:
    """Quick structural validation. Returns (ok, reason_if_not_ok)."""
    if not html or not html.strip():
        return False, "empty"
    head = html.lstrip()[:64].lower()
    if not any(head.startswith(p) for p in _DOCTYPE_PREFIXES):
        return False, "missing <!doctype html>"
    return True, ""


# ── Per-day counters (rate limit + cost cap) ────────────────────────────


def _today_utc_bucket() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class _DailyCounter:
    """Sliding-by-day counter. In-memory; resets on process restart, which
    is acceptable: the render service has its own quota_ledger that survives
    restarts (Phase 4 wires it up). This is the first defensive layer."""

    limit: int  # 0 disables
    counts: dict[tuple[str, str], int]  # (key, day) -> count

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.counts = {}

    def check(self, key: str) -> bool:
        """Return True if this key is under its daily limit."""
        if self.limit <= 0:
            return True
        bucket = _today_utc_bucket()
        return self.counts.get((key, bucket), 0) < self.limit

    def record(self, key: str) -> None:
        if self.limit <= 0:
            return
        bucket = _today_utc_bucket()
        self.counts[(key, bucket)] = self.counts.get((key, bucket), 0) + 1


@dataclass
class _CostLedger:
    """Per-key accumulated cost in USD micros (1 USD = 1_000_000 micros)."""

    cap_usd: float  # 0 disables
    totals: dict[tuple[str, str], int]

    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.totals = {}

    def remaining_micros(self, key: str) -> int:
        if self.cap_usd <= 0:
            return 10**12  # effectively infinite
        bucket = _today_utc_bucket()
        spent = self.totals.get((key, bucket), 0)
        cap_micros = int(round(self.cap_usd * 1_000_000))
        return max(0, cap_micros - spent)

    def add(self, key: str, micros: int) -> None:
        if self.cap_usd <= 0:
            return
        bucket = _today_utc_bucket()
        self.totals[(key, bucket)] = self.totals.get((key, bucket), 0) + micros


def _estimate_composition_cost_micros() -> int:
    """Return the estimated micro-USD cost of one composition write.

    Sonnet pricing baked in. Tracked at submit time, before the actual call,
    to enforce a hard cap. Real cost lands in the cost_tracker at call time.
    """
    in_usd = (_COMPOSITION_INPUT_TOKEN_BUDGET / 1_000_000) * _SONNET_INPUT_USD_PER_MTOK
    out_usd = (_COMPOSITION_OUTPUT_TOKEN_BUDGET / 1_000_000) * _SONNET_OUTPUT_USD_PER_MTOK
    return int(round((in_usd + out_usd) * 1_000_000))


# ── Composition sub-agent ───────────────────────────────────────────────


async def _author_composition(
    *,
    provider: Any,
    model: str,
    system_prompt: str,
    brief: str,
    feedback: str | None = None,
) -> str:
    """Single-shot composition write. Returns raw HTML or raises ValueError.

    Provider must implement the LLMProvider.chat contract from
    qanot/providers/base.py.
    """
    user_msg = brief
    if feedback:
        user_msg = (
            f"{brief}\n\n"
            "Previous attempt failed lint with these errors:\n"
            f"{feedback}\n\n"
            "Output a corrected HTML document only."
        )

    messages = [{"role": "user", "content": user_msg}]
    # No tools, no thinking — composition is pure text generation.
    response = await provider.chat(
        messages=messages,
        system=system_prompt,
        tools=[],
        model=model,
        max_tokens=_COMPOSITION_OUTPUT_TOKEN_BUDGET,
    )

    text = (response.content or "").strip()
    # Some providers wrap in ```html ... ``` even when told not to. Strip.
    if text.startswith("```"):
        # Drop leading fence line and trailing fence line.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    ok, reason = _validate_composition_html(text)
    if not ok:
        raise ValueError(f"composition invalid: {reason}")
    return text


# ── Render service HTTP client ──────────────────────────────────────────


class _ServiceUnavailable(Exception):
    """Raised when the render service is unreachable after retries."""


async def _submit_render(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    bearer: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST /render with exponential-backoff retries. Returns the job dict."""
    last_exc: Exception | None = None
    for attempt in range(_SUBMIT_RETRIES + 1):
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
        if attempt < _SUBMIT_RETRIES:
            await asyncio.sleep(_SUBMIT_BACKOFF_BASE_S * (2 ** attempt))
    raise _ServiceUnavailable(f"render service unreachable: {last_exc}")


async def _poll_job(
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
        if on_progress_async and (poll_count % _POLL_PROGRESS_EDIT_EVERY_NTH == 0):
            try:
                await on_progress_async(status)
            except Exception as exc:  # noqa: BLE001 — progress is best-effort
                logger.debug("progress callback failed: %s", exc)
        elif on_progress and (poll_count % _POLL_PROGRESS_EDIT_EVERY_NTH == 0):
            try:
                result = on_progress(status)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.debug("progress callback failed: %s", exc)
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _download_output(
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


# ── Tool registration ───────────────────────────────────────────────────


def register_video_tools(
    registry: ToolRegistry,
    *,
    config: Any,  # qanot.config.Config — typed loosely to avoid import cycle
    workspace_dir: str,
    get_user_id: Callable[[], str | None] | None = None,
    get_chat_id: Callable[[], int | None] | None = None,
    get_bot: Callable[[], Any] | None = None,
) -> None:
    """Register render_video tool when config.video_engine == 'hyperframes'.

    Reads config to bind defaults; resolves the service secret via SecretRef
    semantics (already handled by load_config(); here it's a plain string).
    """
    if getattr(config, "video_engine", "off") != "hyperframes":
        logger.debug("render_video tool skipped: video_engine=%r", getattr(config, "video_engine", None))
        return

    base_url = (getattr(config, "video_render_url", "") or "").rstrip("/")
    bearer = getattr(config, "video_service_secret", "") or ""
    if not base_url:
        logger.error("video_engine=hyperframes but video_render_url is empty; tool disabled")
        return
    if not bearer:
        logger.error("video_engine=hyperframes but video_service_secret is empty; tool disabled")
        return

    bot_id = getattr(config, "bot_name", "") or "unknown-bot"
    user_limit = int(getattr(config, "video_per_user_daily_limit", 5))
    bot_limit = int(getattr(config, "video_per_bot_daily_limit", 50))
    user_cost_cap = float(getattr(config, "video_per_user_daily_cost_usd", 0.50))
    bot_cost_cap = float(getattr(config, "video_per_bot_daily_cost_usd", 5.00))
    composition_model = getattr(config, "video_composition_model", "claude-sonnet-4-6")
    default_duration = int(getattr(config, "video_default_duration_seconds", 30))
    max_duration = int(getattr(config, "video_max_duration_seconds", 60))

    user_counter = _DailyCounter(user_limit)
    bot_counter = _DailyCounter(bot_limit)
    user_cost = _CostLedger(user_cost_cap)
    bot_cost = _CostLedger(bot_cost_cap)

    output_cache_dir = Path(workspace_dir) / _OUTPUT_CACHE_DIRNAME

    async def render_video(params: dict) -> str:
        brief = (params.get("brief") or "").strip()
        if not brief:
            return json.dumps({
                "error": "missing_brief",
                "message": "Iltimos, video haqida qisqa tasvir bering (masalan: 'Mahsulot 30 soniya hook')."
            }, ensure_ascii=False)

        try:
            duration = int(params.get("duration") or default_duration)
        except (TypeError, ValueError):
            duration = default_duration
        duration = max(1, min(duration, max_duration))

        aspect_format = params.get("format") or "9:16"
        if aspect_format not in ("9:16", "16:9", "1:1"):
            aspect_format = "9:16"

        # Resolve user/bot identity for quota tracking.
        user_id_raw = get_user_id() if get_user_id else None
        user_id = str(user_id_raw) if user_id_raw is not None else "system"

        # Per-user + per-bot rate limit (daily).
        if not user_counter.check(user_id):
            return json.dumps({
                "error": "rate_limited",
                "message": f"{_ERROR_MESSAGES_UZ['rate_limited']} (limit: {user_limit}/kun)",
            }, ensure_ascii=False)
        if not bot_counter.check(bot_id):
            return json.dumps({
                "error": "rate_limited",
                "message": f"{_ERROR_MESSAGES_UZ['rate_limited']} (bot limit: {bot_limit}/kun)",
            }, ensure_ascii=False)

        # Per-user + per-bot cost cap (composition LLM tokens only).
        cost_estimate = _estimate_composition_cost_micros()
        if user_cost.remaining_micros(user_id) < cost_estimate:
            return json.dumps({
                "error": "cost_capped",
                "message": _ERROR_MESSAGES_UZ["cost_capped"],
            }, ensure_ascii=False)
        if bot_cost.remaining_micros(bot_id) < cost_estimate:
            return json.dumps({
                "error": "cost_capped",
                "message": _ERROR_MESSAGES_UZ["cost_capped"],
            }, ensure_ascii=False)

        # Resolve provider from the singleton agent.
        from qanot.agent import Agent  # local import to avoid cycle
        if Agent._instance is None:
            return json.dumps({
                "error": "no_agent",
                "message": "Agent ishga tushmagan; render_video chaqirib bo'lmaydi.",
            }, ensure_ascii=False)
        provider = Agent._instance.provider

        # Skill + DESIGN.md
        skill = _load_skill_text(workspace_dir)
        design = _load_design_text(workspace_dir)
        system_prompt = _build_composition_system_prompt(
            skill=skill,
            design=design,
            duration_seconds=duration,
            aspect_format=aspect_format,
        )

        # Telegram progress draft (best-effort).
        progress_state: dict[str, Any] = {"message_id": None}
        bot = get_bot() if get_bot else None
        chat_id = get_chat_id() if get_chat_id else None

        async def _start_progress(initial_text: str) -> None:
            if bot is None or chat_id is None:
                return
            try:
                msg = await bot.send_message(chat_id=chat_id, text=initial_text)
                progress_state["message_id"] = msg.message_id
            except Exception as exc:  # noqa: BLE001 — progress is best-effort
                logger.debug("send progress message failed: %s", exc)

        async def _update_progress(text: str) -> None:
            if bot is None or chat_id is None or progress_state["message_id"] is None:
                return
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_state["message_id"],
                    text=text,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("edit progress message failed: %s", exc)

        async def _on_progress(status: dict[str, Any]) -> None:
            stage = status.get("stage") or status.get("status") or ""
            label = _STAGE_LABELS_UZ.get(stage, stage)
            percent = status.get("progress_percent") or 0
            await _update_progress(f"Video tayyorlanmoqda… {percent}% ({label})")

        await _start_progress("Video tayyorlanmoqda…")

        request_id = str(uuid.uuid4())
        composition: str | None = None
        last_error: dict[str, Any] = {"error": "internal", "message": _ERROR_MESSAGES_UZ["internal"]}

        timeout = httpx.Timeout(_HTTP_REQUEST_TIMEOUT_S, connect=_HTTP_CONNECT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(_COMPOSITION_RETRY_BUDGET + 1):
                # Author composition (track cost upfront — actual usage is
                # measured by cost_tracker on the provider side; we use the
                # estimate as the cap-side accounting).
                try:
                    feedback = None
                    if attempt > 0 and last_error.get("lint_details"):
                        feedback = json.dumps(last_error["lint_details"], ensure_ascii=False)
                    composition = await _author_composition(
                        provider=provider,
                        model=composition_model,
                        system_prompt=system_prompt,
                        brief=brief,
                        feedback=feedback,
                    )
                    user_cost.add(user_id, cost_estimate)
                    bot_cost.add(bot_id, cost_estimate)
                except ValueError as exc:
                    logger.warning("composition validation failed: %s", exc)
                    await _update_progress("Video kompozitsiyasi yaratilmadi.")
                    return json.dumps({
                        "error": "composition_invalid",
                        "message": _ERROR_MESSAGES_UZ["composition_invalid"],
                    }, ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001
                    logger.error("composition LLM call failed: %s", exc)
                    await _update_progress("Video xizmati ishlamayapti.")
                    return json.dumps({
                        "error": "composition_invalid",
                        "message": _ERROR_MESSAGES_UZ["composition_invalid"],
                    }, ensure_ascii=False)

                # Submit to render service.
                try:
                    submitted = await _submit_render(
                        client=client,
                        base_url=base_url,
                        bearer=bearer,
                        payload={
                            "request_id": request_id,
                            "bot_id": bot_id,
                            "user_id": user_id,
                            "composition_html": composition,
                            "format": aspect_format,
                            "duration_seconds": duration,
                            "deadline_seconds": 120,
                        },
                    )
                except _ServiceUnavailable as exc:
                    logger.error("submit_render failed: %s", exc)
                    await _update_progress("Video xizmati javob bermadi.")
                    return json.dumps({
                        "error": "service_unavailable",
                        "message": _ERROR_MESSAGES_UZ["service_unavailable"],
                    }, ensure_ascii=False)
                except httpx.HTTPStatusError as exc:
                    body: Any = ""
                    try:
                        body = exc.response.json()
                    except Exception:  # noqa: BLE001
                        body = exc.response.text[:200] if exc.response is not None else ""
                    logger.warning("submit_render 4xx: %s %s", exc.response.status_code, body)
                    return json.dumps({
                        "error": "service_rejected",
                        "message": "Render xizmati so'rovni rad etdi.",
                        "details": body,
                    }, ensure_ascii=False)

                job_id = submitted.get("job_id") or ""
                if not job_id:
                    return json.dumps({
                        "error": "internal",
                        "message": _ERROR_MESSAGES_UZ["internal"],
                    }, ensure_ascii=False)

                # Poll until terminal.
                final_status = await _poll_job(
                    client=client,
                    base_url=base_url,
                    bearer=bearer,
                    job_id=job_id,
                    on_progress_async=_on_progress,
                )

                if final_status.get("status") == "succeeded":
                    # Download MP4 to local cache.
                    output_cache_dir.mkdir(parents=True, exist_ok=True)
                    local_path = output_cache_dir / f"{job_id}.mp4"
                    try:
                        await _download_output(
                            client=client,
                            base_url=base_url,
                            bearer=bearer,
                            job_id=job_id,
                            dest_path=local_path,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("download_output failed: %s", exc)
                        return json.dumps({
                            "error": "internal",
                            "message": _ERROR_MESSAGES_UZ["internal"],
                        }, ensure_ascii=False)

                    # Push to Agent's pending video queue for Telegram delivery.
                    Agent._push_pending_video(user_id, str(local_path))

                    user_counter.record(user_id)
                    bot_counter.record(bot_id)

                    await _update_progress("Video tayyor!")
                    render_seconds = int(final_status.get("render_duration_seconds") or 0)
                    return json.dumps({
                        "success": True,
                        "video_path": str(local_path),
                        "duration_seconds": duration,
                        "render_seconds": render_seconds,
                        "format": aspect_format,
                    }, ensure_ascii=False)

                # Failed (or terminal non-success).
                err = final_status.get("error") or {}
                code = err.get("code") or "internal"

                if code == "lint_failed" and attempt < _COMPOSITION_RETRY_BUDGET:
                    last_error = {
                        "error": code,
                        "message": _ERROR_MESSAGES_UZ.get(code, _ERROR_MESSAGES_UZ["internal"]),
                        "lint_details": err.get("details") or err.get("message") or "",
                    }
                    request_id = str(uuid.uuid4())  # fresh idempotency key for retry
                    continue

                last_error = {
                    "error": code,
                    "message": _ERROR_MESSAGES_UZ.get(code, _ERROR_MESSAGES_UZ["internal"]),
                }
                break

        await _update_progress(last_error.get("message") or _ERROR_MESSAGES_UZ["internal"])
        return json.dumps(last_error, ensure_ascii=False)

    registry.register(
        name="render_video",
        description=(
            "Qisqa video (Reel/TikTok/Short) yaratish. Brief — video haqida "
            "qisqa tavsif. Default 30 soniya, 9:16 portrait. Format: '9:16', "
            "'16:9' yoki '1:1'."
        ),
        parameters={
            "type": "object",
            "required": ["brief"],
            "properties": {
                "brief": {
                    "type": "string",
                    "description": "Video haqida qisqa tavsif (mavzu, asosiy xabar, ohang).",
                },
                "duration": {
                    "type": "integer",
                    "description": f"Davomiyligi soniyalarda (1-{max_duration}). Default {default_duration}.",
                },
                "format": {
                    "type": "string",
                    "enum": ["9:16", "16:9", "1:1"],
                    "description": "Video aspect ratio. Default 9:16 (portrait).",
                },
            },
        },
        handler=render_video,
    )
    logger.info(
        "render_video registered (model=%s, user_limit=%d/day, bot_limit=%d/day)",
        composition_model, user_limit, bot_limit,
    )
