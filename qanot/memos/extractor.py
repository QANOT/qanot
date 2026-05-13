"""WAL → structured memo writer.

When the user message trips a high-confidence WAL pattern ("har doim X",
"eslab qol", "always Y"), we want the rule captured as a proper memo
file — not the buried-bullet shape that produced the 2026-05-13
title-format regression.

This module does the LLM half of the capture path. Given a user message,
it asks Haiku to either emit a structured memo (when the message
contains a persistent rule worth saving) or to refuse. The output is a
strict JSON shape that maps 1:1 to ``MemoStore.upsert`` kwargs.

The extractor is conservative by design. The cost of saving a useless
memo is non-zero (the router pulls it into context on similar queries
forever after), so we'd rather miss-capture than over-capture. The
prompt explicitly enumerates skip-conditions, and a low-confidence
fall-through returns ``None``.

Cost model: one Haiku call (~$0.0002) per WAL hit. WAL hits are rare
(estimate: 1-3 per active user per day) so the marginal monthly cost
is well under a cent per user.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .spec import MemoType

logger = logging.getLogger(__name__)


# Haiku 4.5 is the cheapest current-gen Anthropic model — fast and
# accurate enough for short structured-extraction tasks like this.
EXTRACTOR_MODEL = "claude-haiku-4-5-20251001"
EXTRACTOR_MAX_TOKENS = 600  # JSON output cap; rule bodies are short


# The prompt is split into a stable system block (cache-friendly) and a
# per-call user block. The system block enumerates skip-conditions so
# the LLM has clear guidance on when NOT to save — over-capture is the
# expensive failure mode here.
_EXTRACTOR_SYSTEM = """You are a memory-curation agent. You read a single user message and
decide whether it contains a PERSISTENT RULE the bot should remember across
sessions and apply at WRITE time (when generating output, not just when
acknowledging).

SAVE when the user clearly states:
  - a format / style preference ("titles always D-month, YYYY", "respond in B2 academic English")
  - a hard ban / always-do directive ("never use English in DOCX", "always send DOCX, not PDF")
  - durable identity / role facts ("my name is X", "I work as Y")
  - a correction the user wants applied forever ("not blue — navy blue")

SKIP these (output should_save=false):
  - one-off requests ("do this once", "create today's report")
  - chat pleasantries / acknowledgements ("ok", "rahmat", "great")
  - questions or requests ("how do I X?", "create Y for me")
  - venting or opinions without imperative form
  - vague directives lacking an actionable rule ("be better", "improve")
  - things that are obviously already true (the bot's own name, default behavior)

Memo types:
  - user       — who the user is (identity, role, language, color preference, etc.)
  - feedback   — format/style/correction rules the bot must apply (HARD rules)
  - project    — current-initiative facts (decisions, deadlines, project status)
  - reference  — pointers to external systems (URLs, channel IDs, file paths)

Scope rules:
  - user_scope: set to the current user_id when the rule is about THIS user's
    preferences. Leave empty for global rules (e.g. bot-wide identity).
  - thread_scope: set to the current thread when the rule is clearly bound to
    that thread (e.g. daily-notes thread → titles must be date-only). Leave
    empty when the rule applies everywhere.

Name rules:
  - kebab-case ASCII, ≤64 chars
  - prefix the type: "feedback-title-format", "user-language-preference",
    "project-trading-pause", "reference-eskiz-sms"
  - the name + description is what the router sees when picking memos —
    make the description SPECIFIC (no padding words, no marketing)

Output JSON ONLY, no prose. Shape:

When saving:
{
  "should_save": true,
  "name": "feedback-title-format",
  "description": "Daily note titles must use D-month, YYYY format",
  "type": "feedback",
  "body": "ALWAYS use \\"13-may, 2026\\" shape. NEVER write \\"Daily Entry — YYYY-yil, DD-month\\".",
  "user_scope": "1545224574",
  "thread_scope": "kunlik-yozuv",
  "why": "User stated this rule on 2026-05-13",
  "how_to_apply": "When writing daily Notion titles, dates, or any time-stamped headings."
}

When NOT saving:
{"should_save": false, "reason": "<one-line reason>"}

For type=feedback memos, the body MUST contain at least one POSITIVE example
("ALWAYS X" or "Use X") AND, when contradicting habit is likely, at least one
NEGATIVE example ("NEVER write Y"). This is what makes the rule survive
training-data habits at write time."""


class LLMClient(Protocol):
    """Minimal subset of the Anthropic client interface we need."""

    async def messages_create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a dict with the same shape as anthropic SDK's response."""
        ...


@dataclass
class ExtractedMemo:
    """Structured output ready to feed into MemoStore.upsert.

    ``should_save=False`` results are returned as None by extract_memo;
    the dataclass only carries successful extractions.
    """

    name: str
    description: str
    type: MemoType
    body: str
    user_scope: str = ""
    thread_scope: str = ""
    why: str = ""
    how_to_apply: str = ""

    def to_kwargs(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "memo_type": self.type,
            "body": self.body,
            "user_scope": self.user_scope,
            "thread_scope": self.thread_scope,
            "why": self.why,
            "how_to_apply": self.how_to_apply,
        }


# ─── public API ──────────────────────────────────────────────────


async def extract_memo(
    client: Any,
    user_message: str,
    *,
    user_id: str = "",
    thread_id: str = "",
    today_iso: str | None = None,
    model: str = EXTRACTOR_MODEL,
) -> ExtractedMemo | None:
    """Run a Haiku call to decide whether the message holds a saveable rule.

    Returns ``None`` when the message isn't rule-worthy or when the LLM
    output can't be parsed. Never raises — failures are logged so the
    caller can call this in a background task without try-except clutter.

    ``client`` is an ``anthropic.AsyncAnthropic`` instance (or any
    duck-typed equivalent exposing ``messages.create``). We pass the
    client in rather than constructing one here so call-site retries,
    rate-limiting, and OAuth identity headers all stay in the agent's
    provider layer.
    """
    if not user_message or not user_message.strip():
        return None

    today_iso = today_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context_block = (
        f"Today: {today_iso}\n"
        f"Current user_id: {user_id or '(unknown)'}\n"
        f"Current thread: {thread_id or '(unknown)'}\n\n"
        f"User message:\n{user_message}"
    )

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=EXTRACTOR_MAX_TOKENS,
            system=_EXTRACTOR_SYSTEM,
            messages=[{"role": "user", "content": context_block}],
        )
    except Exception as exc:  # noqa: BLE001 — extractor must never break the turn
        logger.warning("memo extractor LLM call failed: %s", exc)
        return None

    text = _extract_text(response)
    if not text:
        return None

    payload = _parse_json(text)
    if payload is None:
        logger.debug("memo extractor: could not parse JSON from response")
        return None

    if not payload.get("should_save"):
        logger.debug(
            "memo extractor: skip — %s",
            payload.get("reason", "unstated"),
        )
        return None

    try:
        return _validate_payload(payload, user_id, thread_id)
    except ValueError as exc:
        logger.warning("memo extractor: invalid payload: %s", exc)
        return None


# ─── internals ───────────────────────────────────────────────────


def _extract_text(response: Any) -> str:
    """Pull the text out of an Anthropic ``messages.create`` response.

    Tolerates both the SDK object form (``response.content[0].text``)
    and a plain dict (used in tests).
    """
    try:
        content = getattr(response, "content", None)
        if content is None and isinstance(response, dict):
            content = response.get("content")
        if not content:
            return ""
        first = content[0]
        if isinstance(first, dict):
            return first.get("text") or ""
        return getattr(first, "text", "") or ""
    except (AttributeError, IndexError, TypeError):
        return ""


# Greedy match of the outermost {...} block — tolerant to extra prose
# the LLM might emit before/after the JSON despite the prompt.
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of the LLM's response.

    The prompt asks for JSON-only, but production LLMs sometimes wrap
    in ```json``` fences or add a trailing comment. Strip and parse
    leniently — fail to None on anything we can't decode.
    """
    text = text.strip()
    if text.startswith("```"):
        # Strip code fences. Look for ```json\n...\n``` or ```\n...\n```.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to extracting the largest brace-delimited block.
        match = _JSON_BLOCK_RE.search(text)
        if match is None:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _validate_payload(
    payload: dict[str, Any], user_id: str, thread_id: str,
) -> ExtractedMemo:
    """Strict-validate the LLM's JSON shape, with sane fallbacks.

    Raises ``ValueError`` on missing-required-field errors; the caller
    logs and returns None.
    """
    for required in ("name", "description", "type", "body"):
        if not payload.get(required):
            raise ValueError(f"missing required field: {required}")

    name = str(payload["name"]).strip()
    description = str(payload["description"]).strip()
    body = str(payload["body"]).strip()
    type_str = str(payload["type"]).strip().lower()

    try:
        memo_type = MemoType(type_str)
    except ValueError as exc:
        raise ValueError(f"invalid type: {type_str!r}") from exc

    # Scope: the LLM may echo back the user/thread context we provided,
    # OR explicitly emit empty strings for "global". We trust its choice
    # but coerce numeric-looking IDs to strings.
    user_scope = str(payload.get("user_scope") or "").strip()
    thread_scope = str(payload.get("thread_scope") or "").strip()

    # Sanity check: if the LLM picked a scope that differs from the
    # caller's context, log a debug note. This isn't fatal — the LLM
    # might legitimately decide a global rule has no scope — but it's
    # the kind of drift we want to see in production logs.
    if user_scope and user_id and user_scope != user_id:
        logger.debug(
            "memo extractor picked user_scope=%s vs caller user_id=%s",
            user_scope, user_id,
        )

    return ExtractedMemo(
        name=name,
        description=description,
        type=memo_type,
        body=body,
        user_scope=user_scope,
        thread_scope=thread_scope,
        why=str(payload.get("why") or "").strip(),
        how_to_apply=str(payload.get("how_to_apply") or "").strip(),
    )
