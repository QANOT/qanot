"""LLM-as-judge for qanot eval harness.

Scores recorded bot responses against a structured rubric. Uses Claude
Sonnet via the Anthropic SDK so the judge model is independent from the
agent model (which is typically Opus).

Rubric severity tiers:
  - critical:  any fail = case fails (binary gate)
  - important: weighted 0.5 toward the score
  - nice:      weighted 0.1 toward the score

The judge returns structured JSON. Parsing is strict — any malformed
output triggers a retry with a clarifying nudge. After 2 retries the
case is marked as judge_error (separate from agent failure).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 2

Severity = Literal["critical", "important", "nice"]
RubricResult = Literal["pass", "fail", "n/a"]


def _is_oauth_token(key: str) -> bool:
    """OAuth access tokens (sk-ant-oat01-) need Claude Code identity headers
    and a 'You are Claude Code' system prompt block to access Sonnet/Opus.
    Regular API keys (sk-ant-api03-) use plain x-api-key auth."""
    return key.startswith("sk-ant-oat01-")


def _build_client(api_key: str) -> anthropic.Anthropic:
    """Build an Anthropic client that handles both API keys and OAuth tokens."""
    if _is_oauth_token(api_key):
        return anthropic.Anthropic(
            api_key=None,
            auth_token=api_key,
            default_headers={
                "anthropic-dangerous-direct-browser-access": "true",
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                "user-agent": "claude-cli/1.0.0",
                "x-app": "cli",
            },
        )
    return anthropic.Anthropic(api_key=api_key)


CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


@dataclass
class RubricItem:
    criterion: str
    severity: Severity = "important"


@dataclass
class RubricItemResult:
    criterion: str
    severity: Severity
    result: RubricResult
    reason: str


@dataclass
class JudgeVerdict:
    case_id: str
    verdict: Literal["pass", "fail", "judge_error"]
    score: float
    rubric_results: list[RubricItemResult] = field(default_factory=list)
    summary: str = ""
    raw_judge_output: str = ""

    @property
    def has_critical_failure(self) -> bool:
        return any(r.severity == "critical" and r.result == "fail" for r in self.rubric_results)


JUDGE_SYSTEM_PROMPT = """You are a strict, fair evaluator of an AI agent's chat responses.

You will receive:
  1. The user's message (input to the agent).
  2. The agent's recorded response.
  3. A rubric of criteria, each with a severity tier.

Your job: judge each rubric item independently as pass | fail | n/a.

Rules:
  - "pass"  — response clearly satisfies the criterion.
  - "fail"  — response clearly violates the criterion.
  - "n/a"   — criterion doesn't apply to this response (use sparingly; explain).

Be strict but fair:
  - "Doesn't mention X" — if X appears in the response, that's a fail.
  - "Uses Y vocabulary" — must be present and used correctly.
  - "Response is in language Z" — body of the response must be in Z (some quoted technical terms in other languages are OK).

Respond with ONLY a JSON object, no prose before or after. Schema:
{
  "rubric_results": [
    {"criterion": "<exact criterion text>", "result": "pass" | "fail" | "n/a", "reason": "<one sentence>"}
  ],
  "summary": "<one sentence summary of the verdict>"
}

The criterion field MUST exactly match the input criterion text. Do not paraphrase."""


def _build_user_prompt(user_message: str, response: str, rubric: list[RubricItem], case_id: str) -> str:
    # Send criteria WITHOUT the severity tag so the judge echoes them back
    # exactly (we match by string equality on return). Severity stays in our
    # internal rubric for scoring.
    rubric_lines = "\n".join(f"  - {item.criterion}" for item in rubric)
    return (
        f"CASE: {case_id}\n\n"
        f"USER MESSAGE:\n{user_message}\n\n"
        f"AGENT RESPONSE:\n{response}\n\n"
        f"RUBRIC (criteria to evaluate):\n{rubric_lines}\n\n"
        "Evaluate each criterion above and return the JSON verdict. The "
        "`criterion` field in your response MUST be copied verbatim from the "
        "rubric above — no paraphrasing, no severity tags, no truncation."
    )


def _normalize_criterion(s: str) -> str:
    """Strip leading severity tags like '[critical]' that judges sometimes
    add despite the instruction. Returns the bare criterion text."""
    s = s.strip()
    if s.startswith("[") and "]" in s[:20]:
        s = s.split("]", 1)[1].strip()
    return s


def _parse_judge_output(raw: str) -> dict[str, Any]:
    """Strip code fences if present, parse JSON. Raises ValueError on failure."""
    s = raw.strip()
    if s.startswith("```"):
        # Strip ```json or ``` fences
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(s)


def _score_from_results(results: list[RubricItemResult]) -> float:
    """Weighted score 0-100. Critical fail = 0. Otherwise weighted average."""
    if any(r.severity == "critical" and r.result == "fail" for r in results):
        return 0.0
    weights = {"critical": 1.0, "important": 0.5, "nice": 0.1}
    total_weight = 0.0
    earned = 0.0
    for r in results:
        if r.result == "n/a":
            continue
        w = weights[r.severity]
        total_weight += w
        if r.result == "pass":
            earned += w
    if total_weight == 0:
        return 100.0
    return round(100.0 * earned / total_weight, 1)


def judge(
    case_id: str,
    user_message: str,
    response: str,
    rubric: list[RubricItem],
    *,
    client: anthropic.Anthropic | None = None,
) -> JudgeVerdict:
    """Score one recorded response against a rubric. Synchronous (judge calls
    are infrequent and parallelism is handled at the runner level)."""
    if client is None:
        client = _build_client(os.environ["ANTHROPIC_API_KEY"])
    is_oauth = _is_oauth_token(os.environ.get("ANTHROPIC_API_KEY", "")) or \
               getattr(client, "auth_token", None) is not None

    user_prompt = _build_user_prompt(user_message, response, rubric, case_id)
    last_error = ""
    raw_output = ""

    for attempt in range(MAX_RETRIES + 1):
        nudge = (
            "" if attempt == 0
            else f"\n\nYour previous response was not valid JSON: {last_error}. "
                 "Return ONLY the JSON object, no markdown fences, no prose."
        )
        # OAuth tokens require "You are Claude Code" as the first system block
        # to access Sonnet/Opus. Standard API keys use plain system prompt.
        if is_oauth:
            system_blocks = [
                {"type": "text", "text": CLAUDE_CODE_IDENTITY},
                {"type": "text", "text": JUDGE_SYSTEM_PROMPT + nudge},
            ]
        else:
            system_blocks = JUDGE_SYSTEM_PROMPT + nudge
        msg = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=2048,
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_output = "".join(b.text for b in msg.content if hasattr(b, "text"))
        try:
            parsed = _parse_judge_output(raw_output)
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_error = str(e)
            logger.warning("Judge output parse failed (attempt %d): %s", attempt + 1, e)
    else:
        return JudgeVerdict(
            case_id=case_id, verdict="judge_error", score=0.0,
            summary=f"Judge output unparseable after {MAX_RETRIES + 1} attempts: {last_error}",
            raw_judge_output=raw_output,
        )

    rubric_by_criterion = {item.criterion: item for item in rubric}
    rubric_by_normalized = {_normalize_criterion(item.criterion): item for item in rubric}
    results: list[RubricItemResult] = []
    for r in parsed.get("rubric_results", []):
        raw_crit = r.get("criterion", "")
        # Try exact match first, then normalized (strips '[critical]' prefix
        # that judges sometimes prepend despite instructions).
        item = rubric_by_criterion.get(raw_crit) or rubric_by_normalized.get(_normalize_criterion(raw_crit))
        if item is None:
            logger.warning("Judge returned unknown criterion: %r", raw_crit)
            continue
        result_val = r.get("result", "n/a")
        if result_val not in ("pass", "fail", "n/a"):
            result_val = "n/a"
        results.append(RubricItemResult(
            criterion=item.criterion, severity=item.severity,
            result=result_val, reason=r.get("reason", ""),
        ))

    # If the judge returned items but NONE matched our rubric, that's a
    # judge_error — we can't score what we couldn't link back. Empty rubric
    # results when rubric WAS provided also = judge_error (judge skipped all).
    if rubric and not results:
        return JudgeVerdict(
            case_id=case_id, verdict="judge_error", score=0.0,
            summary=f"Judge returned no recognizable rubric results (raw: {len(parsed.get('rubric_results', []))} items)",
            raw_judge_output=raw_output,
        )

    # Add n/a for any rubric items the judge skipped.
    seen = {r.criterion for r in results}
    for item in rubric:
        if item.criterion not in seen:
            results.append(RubricItemResult(
                criterion=item.criterion, severity=item.severity,
                result="n/a", reason="judge omitted this criterion",
            ))

    score = _score_from_results(results)
    has_critical_fail = any(r.severity == "critical" and r.result == "fail" for r in results)
    return JudgeVerdict(
        case_id=case_id,
        verdict="fail" if has_critical_fail else ("pass" if score >= 70 else "fail"),
        score=score,
        rubric_results=results,
        summary=parsed.get("summary", ""),
        raw_judge_output=raw_output,
    )
