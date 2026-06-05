"""Post-turn self-review (A1) — within-session learning loop.

Every N user turns, reflect on the recent conversation with a cheap aux model
and capture at most a couple of DURABLE lessons. Qanot's existing learning
paths are either deterministic (WAL regex), single-message (memo extractor on
WAL hits), or weekly (dreams consolidation) — none reflects on a whole
conversation mid-session. This closes that gap within ~12 turns.

Safety / cost discipline (this is the file that could re-introduce the bloat
qanot deliberately fixed, so it's deliberately conservative):

* Runs fire-and-forget AFTER the reply — never blocks or slows a turn.
* Uses ``agent.provider.client`` with an explicit ``model=`` per call (like the
  memo extractor) — it does NOT mutate the shared ``provider.model``, so it's
  safe to run concurrently with live turns.
* Cadenced (every ``REVIEW_EVERY_N_TURNS``), capped (``MAX_LESSONS``), and each
  candidate is gated through the existing ``judge_lesson`` meta-judge — only
  lessons scoring ``>= MIN_QUALITY`` are kept (and stored pre-scored). The
  reflection prompt is also told to dedup against existing lessons.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SELF_REVIEW_MODEL = "claude-haiku-4-5-20251001"
REVIEW_EVERY_N_TURNS = 12
MAX_LESSONS = 2
MIN_QUALITY = 60.0          # judge_lesson overall score (0..100) to keep
RECENT_MESSAGES = 24
MAX_CONVO_CHARS = 7000

_REVIEW_SYSTEM = (
    "You are a reflective learning agent for an AI assistant. You read a recent "
    "conversation and extract only DURABLE, reusable lessons — specific, "
    "actionable, non-obvious takeaways that would help the assistant next time. "
    "You are extremely selective: most conversations yield NOTHING worth saving."
)

_REVIEW_PROMPT = (
    "Reflect on this recent conversation and extract AT MOST 2 durable lessons.\n\n"
    "A good lesson is specific, actionable, non-obvious, and NOT already in the "
    "existing list below. Return an EMPTY array [] unless something is genuinely "
    "worth remembering long-term.\n"
    "Do NOT capture: one-off facts, environment/tooling errors, things the user "
    "can simply re-state, restating identity, or anything already known.\n\n"
    "Existing lessons (do NOT duplicate these):\n{existing}\n\n"
    "Recent conversation:\n{convo}\n\n"
    "Return ONLY a JSON array, each item {{\"observation\": \"what happened "
    "(one sentence)\", \"lesson\": \"the actionable takeaway (one sentence)\"}}. "
    "Return [] if nothing is worth keeping."
)


def schedule_self_review(agent: Any, user_id: str | None) -> None:
    """Increment the per-conversation turn counter and, on cadence, fire a
    fire-and-forget review. Cheap no-op on most turns."""
    if not user_id:
        return
    try:
        counts = agent._review_turn_count
        counts[user_id] = counts.get(user_id, 0) + 1
        if counts[user_id] % REVIEW_EVERY_N_TURNS != 0:
            return
        messages = list(agent._get_messages(user_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("self-review scheduling skipped: %s", e)
        return
    try:
        asyncio.get_running_loop().create_task(_run_review(agent, str(user_id), messages))
    except RuntimeError:
        pass  # no running loop (e.g. tests call _run_review directly)


def _recent_text(messages: list[dict], n: int) -> str:
    """Plain-text transcript of the last ``n`` messages (user + assistant text)."""
    parts: list[str] = []
    for m in messages[-n:]:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks = [b.get("text", "") for b in content
                      if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(c for c in chunks if c)
        text = text.strip()
        if not text or text.startswith("[CONVERSATION SUMMARY") or text.startswith("[CONTEXT COMPACTION"):
            continue
        parts.append(f"{role}: {text[:1000]}")
    blob = "\n".join(parts)
    return blob[-MAX_CONVO_CHARS:]


def _parse_candidates(text: str) -> list[dict]:
    """Pull a JSON array of {observation, lesson} out of the model's reply."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("observation") and item.get("lesson"):
            out.append(item)
    return out


def _extract_text(response: Any) -> str:
    try:
        return "".join(
            b.text for b in response.content
            if getattr(b, "type", "") == "text"
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


async def _run_review(agent: Any, user_id: str, messages: list[dict]) -> None:
    """The actual reflection pass. Best-effort — never raises."""
    try:
        client = getattr(getattr(agent, "provider", None), "client", None)
        if client is None or not hasattr(client, "messages"):
            return
        workspace_dir = agent.config.workspace_dir

        convo = _recent_text(messages, RECENT_MESSAGES)
        if len(convo) < 80:
            return

        from qanot.learnings import load_learnings, append_learning, set_quality_score
        existing = [e for e in load_learnings(workspace_dir) if not e.get("revoked")]
        existing_lines = "\n".join(
            f"- {e.get('lesson', '')[:160]}" for e in existing[-15:]
        ) or "(none yet)"

        prompt = _REVIEW_PROMPT.format(existing=existing_lines, convo=convo)
        try:
            resp = await client.messages.create(
                model=SELF_REVIEW_MODEL,
                max_tokens=700,
                system=_REVIEW_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("self-review LLM call failed: %s", e)
            return

        candidates = _parse_candidates(_extract_text(resp))
        if not candidates:
            logger.info("self-review: no durable lessons this round (user=%s)", user_id)
            return

        kept = 0
        for c in candidates:
            if kept >= MAX_LESSONS:
                break
            obs = str(c.get("observation", "")).strip()
            les = str(c.get("lesson", "")).strip()
            if not obs or not les:
                continue

            # Quality gate via the existing meta-judge. Only keep lessons that
            # clear the bar — this is the bloat guard. Best-effort: if the judge
            # is unavailable we conservatively SKIP (don't store unvetted ones).
            score: float | None = None
            summary = ""
            try:
                from evals.judge_lesson import judge_lesson
                verdict = await asyncio.to_thread(
                    judge_lesson,
                    {"observation": obs, "lesson": les, "tags": ["self-review"]},
                    existing_lessons=existing,
                )
                score, summary = float(verdict.overall), verdict.summary
            except Exception as e:  # noqa: BLE001
                logger.debug("self-review judge unavailable, skipping lesson: %s", e)
                continue
            if score < MIN_QUALITY:
                logger.info("self-review: dropped low-quality lesson (%.0f): %s", score, les[:60])
                continue

            try:
                entry = append_learning(
                    workspace_dir, obs, les, tags=["self-review"], user_id=user_id,
                )
                set_quality_score(workspace_dir, entry["ts"], score, judge_summary=summary)
                kept += 1
                logger.info("self-review: captured lesson (%.0f): %s", score, les[:60])
            except ValueError as e:
                # injection-scan / validation rejection — skip silently
                logger.debug("self-review lesson rejected: %s", e)
    except Exception as e:  # noqa: BLE001 — must never surface to the user
        logger.debug("self-review failed: %s", e)
