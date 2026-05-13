"""Lightweight quiz answer evaluator — direct Haiku call, no agent loop.

The conversational-poll flow originally fed every poll_answer through a
full ``agent.run_turn`` cycle. Each cycle re-builds the system prompt
(~50-100K tokens with workspace + tools + memory) and calls the main
provider model. With 10 polls answered in quick succession the OAuth
TPM ceiling hits — Anthropic returns 429 and the user sees
"Limitga yetdik".

Quiz feedback doesn't need that much context. The question, the user's
pick, and the correct answer are enough to produce a 3-4 sentence
reply. This module does exactly that via a direct ``messages.create``
call to Haiku — ~500 input tokens per evaluation, 200x cheaper, no
rate-limit pressure.

Same pattern as ``qanot/thread_titler.py`` — bypass ``provider.chat``
to avoid extended thinking, server tools, and context editing.
"""

from __future__ import annotations

import logging
from typing import Any

from qanot.poll_state import PollRecord

logger = logging.getLogger(__name__)

EVALUATOR_MODEL = "claude-haiku-4-5-20251001"
EVALUATOR_MAX_TOKENS = 400

_EVALUATOR_SYSTEM_PROMPT = (
    "You are a friendly Uzbek-speaking quiz tutor. The user just answered "
    "a quiz question. Reply in 3-4 short Uzbek sentences:\n"
    "1. Start with ✅ TO'G'RI or ❌ NOTO'G'RI (no other prefix).\n"
    "2. If wrong, name the correct option and give the rule/reason "
    "briefly. If right, reinforce the rule in one sentence.\n"
    "3. No filler ('Yaxshi savol', 'Davom etaylik') — go straight to the "
    "evaluation.\n"
    "4. Don't ask follow-up questions. Don't propose the next problem. "
    "Don't list other options. Don't reveal the rule book.\n"
    "Length: 3-4 sentences max. Uzbek (Latin script).\n"
    "If the explanation field already gives the rule, lean on it."
)


def _letter(i: int) -> str:
    if 0 <= i < 26:
        return chr(ord("A") + i)
    return str(i)


def _format_options(options: list[str]) -> str:
    return "\n".join(f"{_letter(i)}) {opt}" for i, opt in enumerate(options))


def _format_picks(record: PollRecord, option_ids: list[int]) -> str:
    parts: list[str] = []
    for i in option_ids:
        if 0 <= i < len(record.options):
            parts.append(f"{_letter(i)}) {record.options[i]}")
    return ", ".join(parts) or "(no option)"


def _build_prompt(record: PollRecord, option_ids: list[int]) -> str:
    correct_picks = _format_picks(record, record.correct_option_ids)
    user_picks = _format_picks(record, option_ids)
    is_correct = sorted(option_ids) == sorted(record.correct_option_ids)
    natija = "TO'G'RI" if is_correct else "NOTO'G'RI"

    lines = [
        f"Savol: {record.question}",
        "",
        "Variantlar:",
        _format_options(record.options),
        "",
        f"To'g'ri javob: {correct_picks}",
        f"Foydalanuvchi tanladi: {user_picks}",
        f"Natija: {natija}",
    ]
    if record.explanation:
        lines += ["", f"Izoh: {record.explanation}"]
    return "\n".join(lines)


class PollEvaluator:
    """Direct Haiku call for quiz answer evaluation.

    Stateless — the ``provider`` is reused across requests so the
    underlying httpx client connection pool is shared. Constructor only
    needs a provider with a ``.client`` attribute (the raw Anthropic
    async client).
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def evaluate(
        self, record: PollRecord, option_ids: list[int],
    ) -> str:
        """Return a 3-4 sentence Uzbek evaluation. Falls back to a
        deterministic local string when the model call fails — the user
        still gets immediate feedback, just without the LLM polish."""
        client = getattr(self._provider, "client", None)
        if client is None:
            return self._fallback(record, option_ids)

        prompt = _build_prompt(record, option_ids)

        try:
            response = await client.messages.create(
                model=EVALUATOR_MODEL,
                max_tokens=EVALUATOR_MAX_TOKENS,
                system=_EVALUATOR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            logger.warning("poll evaluator API call failed: %s", e)
            return self._fallback(record, option_ids)

        text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts).strip()
        if not text:
            return self._fallback(record, option_ids)
        return text

    @staticmethod
    def _fallback(record: PollRecord, option_ids: list[int]) -> str:
        """Deterministic minimal feedback when the LLM is unavailable.
        The user still sees ✅ / ❌ + the correct letter — better than
        the agent saying nothing."""
        if not record.correct_option_ids:
            picks = _format_picks(record, option_ids)
            return f"Javobingiz qabul qilindi: {picks}"
        is_correct = sorted(option_ids) == sorted(record.correct_option_ids)
        correct = _format_picks(record, record.correct_option_ids)
        if is_correct:
            base = f"✅ TO'G'RI. Javob: {correct}."
        else:
            picked = _format_picks(record, option_ids)
            base = (
                f"❌ NOTO'G'RI. Siz tanladingiz: {picked}. "
                f"To'g'ri javob: {correct}."
            )
        if record.explanation:
            base += f" {record.explanation}"
        return base
