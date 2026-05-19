"""Evaluator-optimizer for high-stakes write tools.

The third layer of the buried-bullet fix. Layer 1 (memos package) gave
us the storage; layer 2 (router + system-reminder) injects relevant
memos into the prompt so the LLM has the rule in front of it. Even with
both in place, the LLM occasionally still ships output that violates a
freshly-stated rule — training-data habits aren't always defeated by a
single recency-positioned reminder.

This module is the safety net: tools that produce structured output
subject to format rules (Notion titles, filenames, daily-note headings)
run their draft through ``validate_text_against_memos`` before
submission. A Haiku call ($0.0002) checks the draft against active
feedback memos and, when a violation is found, rewrites the offending
text *minimally* — preserving everything else.

Production failure mode this closes:
  - User on 2026-05-13: "sarlavha har doim '13-may, 2026' eslab qol"
  - WAL captures, extractor writes feedback-title-format memo
  - Next day, router injects the memo as <system-reminder>
  - Opus drafts "Daily Entry — 2026-yil, 13-may (Chorshanba)" anyway
  - Validator catches the violation, rewrites to "13-may, 2026"
  - Notion gets the rewritten title; the user never sees the regression

Cost model: ~$0.0002 per validated tool call. Apply only to tools that
ship to external systems where rule violations are user-visible. The
filename of a temp file isn't worth validating; a Notion page title is.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .spec import MemoSpec, MemoType

logger = logging.getLogger(__name__)


VALIDATOR_MODEL = "claude-haiku-4-5-20251001"
VALIDATOR_MAX_TOKENS = 800  # rewrite + violations array

# Truncation backup guard. The PRECISE truncation signal is
# ``stop_reason == "max_tokens"`` (checked first). This ratio is only a
# fallback for shims/paths where stop_reason isn't exposed. It is gated
# on input length: a *short* field (a Notion title, a filename) shrinking
# hard is the validator working as designed — "Daily Entry — …13-may…" →
# "13-may, 2026" is a legitimate 70% drop. Only on a *long* input is a
# big shrink implausible for a minimal edit and therefore truncation/
# paraphrase. Below the floor we trust the rewrite; at/above it we
# enforce the ratio. Rule-agnostic — holds no matter which memo fired.
_LOSS_GUARD_MIN_CHARS = 400
_MIN_VERIFIED_LENGTH_RATIO = 0.85


_VALIDATOR_SYSTEM = """You are a strict rule-compliance auditor. You read a DRAFT text that the
agent is about to write (to Notion, to a file, to a tool API) and check whether
it violates any of the ACTIVE RULES the user has previously established.

For each rule, the rule text contains:
  - a positive directive ("ALWAYS X", "use Y format")
  - optionally negative examples ("NEVER write Z", "do NOT use Q")
  - a "How to apply" line describing when this rule should fire

Your job:
  1. Read every active rule.
  2. Check the draft for direct violations of any rule.
  3. If the draft is fully compliant, output the draft VERBATIM as ``verified``.
  4. If the draft violates one or more rules, rewrite ONLY the offending
     parts. Preserve everything else — content, structure, punctuation,
     language, casing where not under rule. Do NOT add new phrasing, do
     NOT make stylistic "improvements", do NOT translate.

A violation must be a CONCRETE breach the rule text addresses — not a
general aesthetic concern. If you're not sure a rule applies, assume the
draft is compliant.

OUTPUT JSON ONLY, no prose:

When compliant:
{"compliant": true, "verified": "<draft verbatim>"}

When non-compliant:
{
  "compliant": false,
  "verified": "<rewritten draft, minimally changed>",
  "violations": ["<rule name>: <what was wrong, one sentence>"]
}"""


@dataclass
class ValidationResult:
    """Outcome of one validation pass."""

    original: str
    verified: str
    was_changed: bool = False
    violations: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.was_changed

    def summary_line(self) -> str:
        if not self.was_changed:
            return "validator: compliant"
        return f"validator: rewrote ({len(self.violations)} violation(s))"


# ─── public API ──────────────────────────────────────────────────


async def validate_text_against_memos(
    text: str,
    *,
    field_context: str,
    active_memos: Iterable[MemoSpec],
    client: Any,
    model: str = VALIDATOR_MODEL,
) -> ValidationResult:
    """Check ``text`` against feedback memos; rewrite minimally on violation.

    ``field_context`` is a short human label ("Notion page title",
    "filename", "DOCX heading") — the validator uses it to judge which
    rules apply (rules carry a "How to apply" hint).

    ``active_memos`` is typically ``MemoStore.list_in_scope(...)`` already
    filtered to the current user/thread. We further filter to
    ``MemoType.FEEDBACK`` here so user/project/reference facts don't
    trigger spurious rewrites.

    Returns a ``ValidationResult``. On any failure (LLM down, unparseable
    output, no rules to check) we pass the original through unchanged.
    Tools call this in their draft-to-submission path; never raises.
    """
    if not text or not text.strip():
        return ValidationResult(original=text, verified=text)

    rules = [m for m in active_memos if m.type == MemoType.FEEDBACK]
    if not rules:
        return ValidationResult(original=text, verified=text)

    rules_block = _render_rules_block(rules)
    user_block = (
        f"Field context: {field_context}\n\n"
        f"DRAFT text:\n```\n{text}\n```\n\n"
        f"ACTIVE RULES:\n\n{rules_block}"
    )

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=VALIDATOR_MAX_TOKENS,
            system=_VALIDATOR_SYSTEM,
            messages=[{"role": "user", "content": user_block}],
        )
    except Exception as exc:  # noqa: BLE001 — validator must never break the tool
        logger.warning("memo validator LLM call failed: %s", exc)
        return ValidationResult(original=text, verified=text)

    # If Haiku ran out of output budget echoing the draft back, anything
    # it returned is a truncated fragment, not a rewrite. Shipping it would
    # send the user a cut-off reply (and the agent, seeing its own mangled
    # output, retries → tool-call loop). Fail safe to the original.
    if _extract_stop_reason(response) == "max_tokens":
        logger.warning(
            "memo validator hit max_tokens (draft too long to round-trip, "
            "%d chars) — passing original through unchanged", len(text),
        )
        return ValidationResult(original=text, verified=text)

    raw = _extract_text(response)
    payload = _parse_json(raw)
    if payload is None:
        logger.debug("memo validator: unparseable response: %s", raw[:160])
        return ValidationResult(original=text, verified=text)

    if payload.get("compliant"):
        return ValidationResult(original=text, verified=text)

    verified = str(payload.get("verified") or "").strip()
    if not verified:
        logger.debug("memo validator: empty verified field; passing through")
        return ValidationResult(original=text, verified=text)

    # Loss-guard (length-gated backup to the stop_reason check above).
    # Only on long inputs is a big shrink implausible for a minimal edit;
    # short fields (titles, filenames) legitimately shrink hard when a
    # banned prefix is stripped, so they're exempt.
    if (
        len(text) >= _LOSS_GUARD_MIN_CHARS
        and len(verified) < len(text) * _MIN_VERIFIED_LENGTH_RATIO
    ):
        logger.warning(
            "memo validator rewrite was lossy (%d → %d chars, ratio %.2f) "
            "— discarding, passing original through. violations=%s",
            len(text), len(verified), len(verified) / max(len(text), 1),
            (payload.get("violations") or [])[:3],
        )
        return ValidationResult(original=text, verified=text)

    raw_violations = payload.get("violations") or []
    if isinstance(raw_violations, str):
        raw_violations = [raw_violations]
    violations = [str(v) for v in raw_violations]

    was_changed = verified != text
    if was_changed:
        logger.info(
            "memo validator rewrote %s: %r → %r (violations: %s)",
            field_context, _truncate(text, 80),
            _truncate(verified, 80), violations[:3],
        )
    return ValidationResult(
        original=text, verified=verified,
        was_changed=was_changed, violations=violations,
    )


# ─── helpers ────────────────────────────────────────────────────


def _render_rules_block(rules: list[MemoSpec]) -> str:
    """Compose the active-rules section of the user prompt.

    We include name + description + body + how_to_apply (when present)
    so the model judges against the *intent* of the rule, not just the
    surface phrasing. Costs ~50-200 tokens per rule.
    """
    chunks: list[str] = []
    for i, rule in enumerate(rules, start=1):
        header = f"### Rule {i}: {rule.name}"
        desc = rule.description
        body = rule.body
        how = (
            f"\n\n**How to apply:** {rule.how_to_apply}"
            if rule.how_to_apply else ""
        )
        chunks.append(f"{header}\n{desc}\n\n{body}{how}")
    return "\n\n---\n\n".join(chunks)


def _extract_text(response: Any) -> str:
    """Pull text out of an anthropic SDK response (dict-form-tolerant)."""
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


def _extract_stop_reason(response: Any) -> str | None:
    """Pull ``stop_reason`` from an anthropic SDK response (dict-tolerant).

    ``"max_tokens"`` means the model was cut off mid-output — for this
    validator that means a truncated echo of the draft, never a valid
    rewrite.
    """
    try:
        sr = getattr(response, "stop_reason", None)
        if sr is None and isinstance(response, dict):
            sr = response.get("stop_reason")
        return str(sr) if sr is not None else None
    except (AttributeError, TypeError):
        return None


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if match is None:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
