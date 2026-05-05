"""Meta-judge: scores the *quality* of a single learning entry.

Different from the response-judge in evals/judge.py (which scores how
the bot talks to users). This scores whether a captured lesson is
worth keeping in the prompt — actionable, specific, non-duplicate,
not contradictory to existing lessons.

Used by `verify_lesson` tool. Result is written back to the lesson
entry as `quality_score` (0-100). Lessons with low scores can be
revoked manually; auto-revocation is not implemented in v1 (operator
keeps control).

Cost: one Sonnet call per lesson. ~$0 on OAuth tier.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import anthropic

from evals.judge import _build_client, _is_oauth_token

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-sonnet-4-6"

CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

LESSON_JUDGE_SYSTEM_PROMPT = """You evaluate the quality of "lessons" an AI agent has captured from past
interactions. Each lesson is one entry in a learnings store; high-quality
lessons get auto-injected into the agent's system prompt at session start
and shape future behavior.

For each lesson, score these dimensions on a 0-100 scale:

  1. ACTIONABLE — does the lesson tell the reader what to DO differently?
     (Bad: "the system is complicated." Good: "Use tool X for Y, never raw SQL.")
  2. SPECIFIC — does it name concrete tools/contexts/edge cases?
     (Bad: "be careful with the database." Good: "tbl_companies.default_customer
      is per-company; never hardcode id=1.")
  3. ONE_IDEA — is it focused on a single takeaway, not a paragraph of stuff?
     (Bad: 3 lessons crammed into one. Good: one rule, one sentence.)
  4. NOT_TRIVIAL — is this worth carrying forward, or is it obvious / temporary?
     (Bad: "user said hello at 14:30." Good: a real workflow rule.)
  5. NOT_DUPLICATE — does it say something materially different from other
     existing lessons? (You'll get the existing lessons as context.)

Compute an OVERALL score as a weighted average:
  - actionable + specific + one_idea: 25% each
  - not_trivial: 15%
  - not_duplicate: 10%

Score interpretation:
  ≥80: high-quality, keep
  60-79: marginal, keep but watch
  <60: low quality, candidate for revocation

Return ONLY valid JSON, no prose, no markdown fences:
{
  "actionable": <0-100>,
  "specific": <0-100>,
  "one_idea": <0-100>,
  "not_trivial": <0-100>,
  "not_duplicate": <0-100>,
  "overall": <weighted-average>,
  "verdict": "keep" | "watch" | "revoke",
  "summary": "<one sentence — what's strong/weak about this lesson>"
}"""


@dataclass
class LessonVerdict:
    overall: float
    verdict: str  # keep | watch | revoke
    actionable: float
    specific: float
    one_idea: float
    not_trivial: float
    not_duplicate: float
    summary: str


def judge_lesson(
    lesson: dict[str, Any],
    *,
    existing_lessons: list[dict[str, Any]] | None = None,
    client: anthropic.Anthropic | None = None,
) -> LessonVerdict:
    """Score one lesson entry. Existing lessons fed in for duplication check."""
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        client = _build_client(api_key)
    is_oauth = getattr(client, "auth_token", None) is not None

    # Build the user prompt: target lesson + a digest of existing lessons.
    existing_block = ""
    if existing_lessons:
        existing_lines = []
        for e in existing_lessons[:10]:  # cap to keep prompt small
            if e.get("ts") == lesson.get("ts"):
                continue
            existing_lines.append(f"- {e.get('lesson', '')[:200]}")
        if existing_lines:
            existing_block = "EXISTING LESSONS (for duplicate detection):\n" + "\n".join(existing_lines)

    user_prompt = (
        f"LESSON TO EVALUATE:\n"
        f"observation: {lesson.get('observation', '')}\n"
        f"lesson: {lesson.get('lesson', '')}\n"
        f"tags: {', '.join(lesson.get('tags', []))}\n\n"
        f"{existing_block}\n\n"
        "Score this lesson and return the JSON verdict."
    )

    if is_oauth:
        system_blocks: Any = [
            {"type": "text", "text": CLAUDE_CODE_IDENTITY},
            {"type": "text", "text": LESSON_JUDGE_SYSTEM_PROMPT},
        ]
    else:
        system_blocks = LESSON_JUDGE_SYSTEM_PROMPT

    msg = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1024,
        system=system_blocks,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Lesson judge returned non-JSON: {e}; raw: {raw[:200]}") from e

    def _score(key: str) -> float:
        v = parsed.get(key, 0)
        try:
            return max(0.0, min(100.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    actionable = _score("actionable")
    specific = _score("specific")
    one_idea = _score("one_idea")
    not_trivial = _score("not_trivial")
    not_duplicate = _score("not_duplicate")
    overall = parsed.get("overall")
    if not isinstance(overall, (int, float)):
        # Compute it from the weights if the judge didn't provide one.
        overall = (
            0.25 * actionable + 0.25 * specific + 0.25 * one_idea
            + 0.15 * not_trivial + 0.10 * not_duplicate
        )
    overall = round(max(0.0, min(100.0, float(overall))), 1)

    verdict_raw = parsed.get("verdict", "")
    if verdict_raw not in ("keep", "watch", "revoke"):
        # Derive from score band if judge fumbled the field.
        verdict_raw = "keep" if overall >= 80 else ("watch" if overall >= 60 else "revoke")

    return LessonVerdict(
        overall=overall,
        verdict=verdict_raw,
        actionable=actionable,
        specific=specific,
        one_idea=one_idea,
        not_trivial=not_trivial,
        not_duplicate=not_duplicate,
        summary=parsed.get("summary", "")[:300],
    )
