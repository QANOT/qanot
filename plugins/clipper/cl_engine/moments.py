"""Viral-moment detection — LLM analyzes transcript and returns ranked clip candidates.

Uses Qanot's provider layer (Claude by default) with strict Pydantic schema for
structured output. Follows OpusClip's virality-score model (0-99 on hook,
emotional flow, perceived value, trend alignment).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from cl_engine.models import Moment, Transcript

logger = logging.getLogger(__name__)


class _MomentCandidate(BaseModel):
    """LLM output schema for a single moment."""
    start_s: float = Field(..., description="Start time in seconds")
    end_s: float = Field(..., description="End time in seconds")
    hook: str = Field(..., min_length=3, max_length=120, description="Opening hook text shown as overlay")
    title: str = Field(..., min_length=3, max_length=80, description="Short title for the clip")
    virality_score: int = Field(..., ge=0, le=99, description="0-99 virality prediction")
    rationale: str = Field("", max_length=500, description="Why this moment is viral")
    hashtags: list[str] = Field(default_factory=list, max_length=8, description="Suggested hashtags (no #)")

    @field_validator("end_s")
    @classmethod
    def end_after_start(cls, v: float, info):
        if info.data.get("start_s") is not None and v <= info.data["start_s"]:
            raise ValueError("end_s must be greater than start_s")
        return v

    @field_validator("hashtags")
    @classmethod
    def strip_hashes(cls, v: list[str]) -> list[str]:
        return [tag.lstrip("#").strip() for tag in v if tag.strip()]


class _MomentsResponse(BaseModel):
    """LLM output wrapper."""
    moments: list[_MomentCandidate]


_SYSTEM_PROMPT_UZ = """You are a viral short-form video editor for Uzbek-speaking audiences on Instagram Reels, TikTok, and YouTube Shorts.

Your job: analyze a long-form video transcript and extract the most viral short-clip candidates.

## Virality criteria (score 0-99)
1. **Hook strength** (0-25): does the first 1-3 seconds stop the scroll? Question, shocking claim, bold statement.
2. **Emotional flow** (0-25): does it build tension, deliver payoff, or evoke surprise/humor/insight?
3. **Perceived value** (0-25): does viewer walk away with a fact, tip, framework, or takeaway?
4. **Completeness** (0-25): is it a self-contained thought? No mid-sentence cuts, no missing setup.

## Uzbek market notes
- Business, entrepreneurship, finance topics viral for @tadbirkor.ai audience.
- Prefer clips with concrete numbers ("3 million so'm ishladim"), personal stories, contrarian takes.
- Avoid: generic advice, overlong setups, off-topic tangents.

## Clip constraints
- Duration: {min_duration}s to {max_duration}s (strict)
- Must start at a sentence boundary — NEVER mid-sentence
- Must end at a sentence boundary or clear pause
- Prefer moments where the speaker delivers a complete insight/story

## Output format
Return valid JSON matching this schema:
{{
  "moments": [
    {{
      "start_s": 123.4,
      "end_s": 156.8,
      "hook": "3 soat ishlaganimda 10 mln topdim",
      "title": "Qisqa sarlavha (8 so'zgacha)",
      "virality_score": 85,
      "rationale": "Raqamli da'vo + shaxsiy tajriba + aniq natija",
      "hashtags": ["biznes", "tadbirkor", "uzbekistan"]
    }}
  ]
}}

Return {count} moments sorted by virality_score descending. Be strict — if only 3 moments are truly viral, return 3, not {count} mediocre ones."""

_SYSTEM_PROMPT_EN = """You are a viral short-form video editor for YouTube Shorts, TikTok, and Instagram Reels.

Extract the most viral short-clip candidates from a long-form video transcript.

## Virality criteria (score 0-99)
1. Hook strength (0-25) — first 1-3 seconds must stop the scroll
2. Emotional flow (0-25) — tension + payoff, surprise, humor, insight
3. Perceived value (0-25) — viewer walks away with a concrete takeaway
4. Completeness (0-25) — self-contained thought, no mid-sentence cuts

## Clip constraints
- Duration: {min_duration}s to {max_duration}s (strict)
- Must start/end at sentence boundaries

## Output format (valid JSON)
{{
  "moments": [
    {{
      "start_s": 123.4,
      "end_s": 156.8,
      "hook": "Stop-scroll opening line",
      "title": "Short title (max 8 words)",
      "virality_score": 85,
      "rationale": "Why this is viral",
      "hashtags": ["tag1", "tag2"]
    }}
  ]
}}

Return {count} moments sorted by virality_score descending. Be strict on quality."""


def _format_transcript_for_prompt(transcript: Transcript, max_chars: int = 60_000) -> str:
    """Format transcript with [MM:SS] timestamps for LLM input.

    Truncates if too long (protects against context overflow for multi-hour podcasts).
    """
    lines: list[str] = []
    for seg in transcript.segments:
        mm = int(seg.start_s // 60)
        ss = int(seg.start_s % 60)
        lines.append(f"[{mm:02d}:{ss:02d}] ({seg.start_s:.1f}s-{seg.end_s:.1f}s) {seg.text}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # Preserve start + end; drop middle
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n\n[... middle truncated ...]\n\n{tail}"


def _extract_json_block(text: str) -> str:
    """Extract JSON from LLM output that may be wrapped in markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` fences
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline > 0:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    # Find the outermost { ... } if LLM added preamble
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


async def detect_moments(
    transcript: Transcript,
    provider: Any,  # LLMProvider from qanot.providers.base
    *,
    count: int = 5,
    min_duration_s: float = 30.0,
    max_duration_s: float = 90.0,
    language: str = "uz",
    virality_threshold: int = 60,
) -> list[Moment]:
    """Detect viral moments in a transcript using an LLM.

    Args:
        provider: A Qanot LLMProvider instance (Claude, etc.) with `chat()` method.
        count: Target number of moments to return (LLM may return fewer).
        virality_threshold: Drop moments scoring below this (0-99).
    """
    if not transcript.segments:
        logger.warning("Empty transcript — no moments to detect")
        return []

    system_template = _SYSTEM_PROMPT_UZ if language == "uz" else _SYSTEM_PROMPT_EN
    system = system_template.format(
        count=count,
        min_duration=int(min_duration_s),
        max_duration=int(max_duration_s),
    )

    transcript_text = _format_transcript_for_prompt(transcript)
    user_message = (
        f"Transcript (duration: {transcript.duration_s:.0f}s, language: {transcript.language}):\n\n"
        f"{transcript_text}\n\n"
        f"Return exactly {count} moments in valid JSON (no markdown, no prose). "
        f"Each moment must be {int(min_duration_s)}-{int(max_duration_s)}s long."
    )

    logger.info("Calling LLM for moment detection (transcript: %d segments, %d chars)",
                len(transcript.segments), len(transcript_text))

    # Retry LLM call on empty/invalid response (happens with thinking-mode OAuth tokens
    # or transient rate limits). Each retry uses a shorter transcript slice.
    import asyncio as _asyncio

    max_attempts = 3
    raw_text = ""
    last_stop_reason = "?"

    # Temporarily disable server-side tool injection (code_execution, memory)
    # on the underlying Anthropic provider. Otherwise Claude uses code_execution
    # instead of emitting text, and response.content is empty. We only want a
    # plain JSON text response for moment detection.
    import contextlib

    @contextlib.contextmanager
    def _no_server_tools(p):
        inner = getattr(p, "_provider", p)  # unwrap RoutingProvider/FailoverProvider
        saved_ce = getattr(inner, "_code_execution", None)
        saved_mt = getattr(inner, "_memory_tool", None)
        try:
            if hasattr(inner, "_code_execution"):
                inner._code_execution = False
            if hasattr(inner, "_memory_tool"):
                inner._memory_tool = False
            yield
        finally:
            if saved_ce is not None:
                inner._code_execution = saved_ce
            if saved_mt is not None:
                inner._memory_tool = saved_mt

    for attempt in range(max_attempts):
        try:
            with _no_server_tools(provider):
                response = await provider.chat(
                    messages=[{"role": "user", "content": user_message}],
                    tools=None,
                    system=system,
                )
            raw_text = (response.content or "").strip()
            last_stop_reason = getattr(response, "stop_reason", "?")
        except Exception as e:
            logger.warning("LLM call attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
            if attempt < max_attempts - 1:
                await _asyncio.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"LLM moment detection failed after {max_attempts} attempts: {e}") from e

        if raw_text:
            logger.info(
                "LLM moment detection succeeded on attempt %d: stop_reason=%s, chars=%d",
                attempt + 1, last_stop_reason, len(raw_text),
            )
            break
        # Empty response — log + retry
        logger.warning(
            "LLM returned empty content (attempt %d/%d). stop_reason=%s, usage=%s",
            attempt + 1, max_attempts, last_stop_reason,
            getattr(response, "usage", None),
        )
        if attempt < max_attempts - 1:
            await _asyncio.sleep(2 * (attempt + 1))

    if not raw_text:
        # Diagnose based on stop_reason per Anthropic docs
        if last_stop_reason == "refusal":
            hint = (
                "Claude's API safety filter refused. Sonnet 4.5/4.6 has tightened "
                "filters — try running with Haiku 4.5 via /model haiku, or check "
                "if the transcript contains content that triggers refusal."
            )
        elif last_stop_reason == "max_tokens":
            hint = "Response hit max_tokens (8192) before any text was generated. Rare but possible."
        else:
            hint = f"Unexpected empty response (stop_reason={last_stop_reason}). Possibly transient API issue."
        raise RuntimeError(f"LLM returned empty content 3 times. {hint}")

    json_text = _extract_json_block(raw_text)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error("LLM returned invalid JSON: %s\nRaw (first 1000 chars): %s", e, raw_text[:1000])
        raise RuntimeError(
            f"LLM returned unparseable JSON. First 200 chars: {raw_text[:200]!r}"
        ) from e

    try:
        parsed = _MomentsResponse.model_validate(data)
    except Exception as e:
        logger.error("LLM output failed schema validation: %s\nData: %s", e, str(data)[:500])
        # Try to recover partial moments
        moments_raw = data.get("moments", []) if isinstance(data, dict) else []
        parsed_moments: list[_MomentCandidate] = []
        for m in moments_raw:
            try:
                parsed_moments.append(_MomentCandidate.model_validate(m))
            except Exception:
                continue
        if not parsed_moments:
            return []
        parsed = _MomentsResponse(moments=parsed_moments)

    # Convert to our Moment model + apply filters
    moments: list[Moment] = []
    for c in parsed.moments:
        if c.virality_score < virality_threshold:
            logger.debug("Dropping low-virality moment (%d < %d): %s",
                         c.virality_score, virality_threshold, c.hook)
            continue
        duration = c.end_s - c.start_s
        if duration < min_duration_s * 0.8:  # allow 20% flex
            logger.debug("Dropping too-short moment (%.1fs): %s", duration, c.hook)
            continue
        if duration > max_duration_s * 1.2:
            # Trim instead of dropping
            c.end_s = c.start_s + max_duration_s
        if c.end_s > transcript.duration_s:
            c.end_s = transcript.duration_s
        moments.append(Moment(
            start_s=c.start_s,
            end_s=c.end_s,
            hook=c.hook,
            virality_score=c.virality_score,
            rationale=c.rationale,
            title=c.title,
            hashtags=list(c.hashtags),
        ))

    # Sort by virality desc, return up to count
    moments.sort(key=lambda m: m.virality_score, reverse=True)
    return moments[:count]


def snap_to_sentence_boundary(
    moment: Moment, transcript: Transcript, *, max_shift_s: float = 3.0,
) -> Moment:
    """Adjust moment start/end to nearest sentence boundaries in transcript.

    LLM timestamps can be imprecise. We snap to actual transcript segment boundaries
    within `max_shift_s` to avoid mid-sentence cuts.
    """
    segments = transcript.segments
    if not segments:
        return moment

    # Find closest segment start to moment.start_s
    best_start = moment.start_s
    best_start_delta = float("inf")
    for seg in segments:
        delta = abs(seg.start_s - moment.start_s)
        if delta < best_start_delta and delta <= max_shift_s:
            best_start = seg.start_s
            best_start_delta = delta

    # Find closest segment end to moment.end_s
    best_end = moment.end_s
    best_end_delta = float("inf")
    for seg in segments:
        delta = abs(seg.end_s - moment.end_s)
        if delta < best_end_delta and delta <= max_shift_s:
            best_end = seg.end_s
            best_end_delta = delta

    if best_end <= best_start:
        return moment  # snap would invert — keep original

    return Moment(
        start_s=best_start,
        end_s=best_end,
        hook=moment.hook,
        virality_score=moment.virality_score,
        rationale=moment.rationale,
        title=moment.title,
        hashtags=list(moment.hashtags),
    )
