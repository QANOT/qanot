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

from qanot.learnings import append_learning, search_learnings
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
