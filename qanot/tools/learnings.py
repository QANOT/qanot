"""Self-improvement tools: evolve_soul + recall_lessons.

The agent uses these to capture lessons from past interactions and
recall them when facing similar problems. Recent learnings auto-inject
into the system prompt at session start (qanot/prompt.py); these tools
let the agent write new ones and search older ones.

evolve_soul appends to <workspace>/memory/learnings.jsonl (NOT SOUL.md;
identity is sacred). recall_lessons reads from the same store.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from qanot.learnings import (
    append_learning,
    load_learnings,
    revoke_learning,
    search_learnings,
    set_quality_score,
)
from qanot.registry import ToolRegistry


def register_learning_tools(
    registry: ToolRegistry,
    workspace_dir: str,
    get_user_id: Callable[[], str | None] | None = None,
) -> None:
    """Register evolve_soul and recall_lessons. Called from main.py."""

    async def evolve_soul(params: dict) -> str:
        observation = (params.get("observation") or "").strip()
        lesson = (params.get("lesson") or "").strip()
        tags = params.get("tags") or []
        if not isinstance(tags, list):
            return json.dumps({"error": "tags must be an array of strings"})
        try:
            uid = get_user_id() if get_user_id else None
            entry = append_learning(
                workspace_dir,
                observation,
                lesson,
                tags=tags,
                user_id=uid or "",
            )
            return json.dumps({
                "success": True,
                "ts": entry["ts"],
                "lesson": entry["lesson"],
                "tags": entry["tags"],
                "note": (
                    "Lesson recorded. It will auto-appear in your prompt "
                    "at the next session start; recall older ones via "
                    "recall_lessons."
                ),
            }, ensure_ascii=False)
        except ValueError as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="evolve_soul",
        description=(
            "Capture a lesson from a clear takeaway — a mistake the user corrected, "
            "a non-obvious pattern in the data, a workflow that should be your default. "
            "Use SPARINGLY: real lessons only, not chatter or trivia. "
            "Lessons should be ONE actionable sentence (e.g. 'For cashier daily reports, "
            "always call absmarket_get_cashier_daily_report — never derive via raw SQL'). "
            "DON'T use for: things already in SOUL.md / USER.md / MEMORY.md, temporary "
            "observations, or things specific to one user (use the WAL for those — happens "
            "automatically). Lessons go to a shared learnings ledger; ~5 most recent "
            "auto-inject into your prompt every session."
        ),
        parameters={
            "type": "object",
            "required": ["observation", "lesson"],
            "properties": {
                "observation": {
                    "type": "string",
                    "description": "What happened — one sentence, max 500 chars.",
                },
                "lesson": {
                    "type": "string",
                    "description": "What to do differently next time — one actionable sentence, max 400 chars.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Up to 10 short keywords for retrieval (e.g. ['absmarket', 'cashier_report']).",
                },
            },
        },
        handler=evolve_soul,
    )

    async def recall_lessons(params: dict) -> str:
        topic = (params.get("topic") or "").strip()
        try:
            limit = int(params.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 20))
        matches = search_learnings(workspace_dir, topic, limit=limit)
        return json.dumps({
            "query": topic,
            "count": len(matches),
            "lessons": [
                {
                    "date": str(e.get("ts", ""))[:10],
                    "lesson": e.get("lesson", ""),
                    "observation": e.get("observation", ""),
                    "tags": e.get("tags", []),
                }
                for e in matches
            ],
        }, ensure_ascii=False)

    registry.register(
        name="recall_lessons",
        description=(
            "Search your past lessons. Call BEFORE attempting a problem you might have "
            "solved before — even if you don't remember the specific lesson, semantically "
            "similar past situations may have lessons attached. "
            "`topic` (optional): substring filter on observation/lesson/tags; empty = newest. "
            "`limit`: max results (default 5, max 20). "
            "Note: your ~5 most recent lessons already auto-inject into the prompt at "
            "session start — use this tool for older or topic-targeted recall."
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Substring filter (optional). Empty = most recent.",
                },
                "limit": {
                    "type": "number",
                    "description": "Max results (default 5, max 20).",
                },
            },
        },
        handler=recall_lessons,
    )

    async def verify_lesson(params: dict) -> str:
        """Run the meta-judge on a lesson and write the score back."""
        ts = (params.get("ts") or "").strip()
        if not ts:
            return json.dumps({"error": "ts is required (lesson's timestamp from evolve_soul response)"})
        try:
            from evals.judge_lesson import judge_lesson
        except ImportError as e:
            return json.dumps({"error": f"meta-judge unavailable: {e}"})

        # Find the target lesson (include revoked so verify still works on them).
        all_lessons = load_learnings(workspace_dir, include_revoked=True)
        target = next((e for e in all_lessons if e.get("ts") == ts), None)
        if target is None:
            return json.dumps({"error": f"lesson with ts={ts} not found"})

        # Existing lessons (excluding the target) for duplicate detection.
        existing = [e for e in all_lessons if e.get("ts") != ts and not e.get("revoked")]
        try:
            verdict = judge_lesson(target, existing_lessons=existing)
        except Exception as e:
            return json.dumps({"error": f"meta-judge failed: {e}"})

        set_quality_score(workspace_dir, ts, verdict.overall, judge_summary=verdict.summary)

        return json.dumps({
            "success": True,
            "ts": ts,
            "overall": verdict.overall,
            "verdict": verdict.verdict,
            "scores": {
                "actionable": verdict.actionable,
                "specific": verdict.specific,
                "one_idea": verdict.one_idea,
                "not_trivial": verdict.not_trivial,
                "not_duplicate": verdict.not_duplicate,
            },
            "summary": verdict.summary,
            "note": (
                "Score has been written back to the lesson entry. "
                "Verdict 'revoke' = consider calling revoke_lesson; "
                "'watch' = marginal; 'keep' = high quality."
            ),
        }, ensure_ascii=False)

    registry.register(
        name="verify_lesson",
        description=(
            "Score a captured lesson on quality dimensions (actionable, specific, one-idea, "
            "non-trivial, non-duplicate) using a meta-judge. The overall score is written "
            "back to the lesson entry. Use this AFTER calling evolve_soul to verify the "
            "lesson is worth keeping in the prompt — low scores (<60) suggest revoke. "
            "Costs ~one Sonnet call per lesson. `ts` = the timestamp from evolve_soul's "
            "success response."
        ),
        parameters={
            "type": "object",
            "required": ["ts"],
            "properties": {
                "ts": {
                    "type": "string",
                    "description": "Lesson's timestamp (ISO8601, from evolve_soul response).",
                },
            },
        },
        handler=verify_lesson,
    )

    async def revoke_lesson_handler(params: dict) -> str:
        ts = (params.get("ts") or "").strip()
        reason = (params.get("reason") or "").strip()
        if not ts:
            return json.dumps({"error": "ts is required (lesson's timestamp)"})
        if not reason:
            return json.dumps({"error": "reason is required (one sentence — why this lesson is bad)"})
        uid = get_user_id() if get_user_id else None
        entry = revoke_learning(workspace_dir, ts, reason, revoked_by=uid or "")
        if entry is None:
            return json.dumps({"error": f"lesson with ts={ts} not found or already revoked"})
        return json.dumps({
            "success": True,
            "ts": ts,
            "revoked_at": entry["revoked"]["ts"],
            "reason": entry["revoked"]["reason"],
            "note": "Lesson is now hidden from prompt injection and recall_lessons. The entry remains in the JSONL as audit trail.",
        }, ensure_ascii=False)

    registry.register(
        name="revoke_lesson",
        description=(
            "Revoke a captured lesson — it stops auto-injecting into the prompt and "
            "won't show in recall_lessons. The entry stays in the learnings file as audit "
            "trail (not deleted). Use when verify_lesson returns 'revoke' verdict OR when "
            "a user/operator points out that a stored lesson is actively wrong."
        ),
        parameters={
            "type": "object",
            "required": ["ts", "reason"],
            "properties": {
                "ts": {
                    "type": "string",
                    "description": "Lesson's timestamp (ISO8601, from evolve_soul response).",
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence explaining why this lesson is bad (kept as audit trail).",
                },
            },
        },
        handler=revoke_lesson_handler,
    )
