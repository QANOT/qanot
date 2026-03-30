"""Push-based result delivery for sub-agents.

Handles formatting and delivering results back to the parent agent
or directly to Telegram for async mode.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Awaitable
from typing import Any

from qanot.orchestrator.types import SubagentRun, AnnouncePayload
from qanot.orchestrator.context_scope import fence_result

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 8000
MAX_BOARD_ENTRIES = 20


def build_announce_payload(run: SubagentRun) -> AnnouncePayload:
    """Build structured announce payload from a completed run."""
    result = run.result_text or "(no output)"
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + "\n\n[... result truncated]"

    return AnnouncePayload(
        run_id=run.run_id,
        agent_id=run.agent_id,
        agent_name=run.agent_name,
        status=run.status,
        result=result,
        elapsed_seconds=run.elapsed_seconds,
        token_input=run.token_input,
        token_output=run.token_output,
        cost=run.cost,
    )


def format_telegram_announce(payload: AnnouncePayload) -> str:
    """Format announce for Telegram delivery (async mode)."""
    status_icon = {
        "completed": "\u2705",
        "failed": "\u274c",
        "timeout": "\u23f0",
        "cancelled": "\u26d4",
    }.get(payload.status, "\u2753")

    header = f"{status_icon} <b>{payload.agent_name}</b> ({payload.agent_id})"
    stats = payload.format_stats_line()
    result = payload.result

    # Truncate for Telegram (4096 char limit minus overhead)
    max_result = 3500
    if len(result) > max_result:
        result = result[:max_result] + "\n\n[... truncated]"

    return f"{header}\n{stats}\n\n{result}"


def format_sync_result(payload: AnnouncePayload) -> dict[str, Any]:
    """Format announce as JSON dict for sync tool result."""
    return {
        "status": payload.status,
        "agent_id": payload.agent_id,
        "agent_name": payload.agent_name,
        "result": fence_result(payload.result),
        "elapsed_seconds": round(payload.elapsed_seconds, 1),
        "tokens": {
            "input": payload.token_input,
            "output": payload.token_output,
            "total": payload.token_input + payload.token_output,
        },
        "cost": round(payload.cost, 4) if payload.cost > 0 else None,
    }


async def deliver_async_result(
    run: SubagentRun,
    send_callback: Callable[[int, str], Awaitable[None]] | None,
    board: dict[str, list[dict]],
) -> None:
    """Deliver async result: post to board + send to Telegram."""
    payload = build_announce_payload(run)

    # Always post to project board
    post_to_board(board, run.parent_user_id, payload)

    # Deliver via Telegram
    if send_callback and run.parent_chat_id:
        try:
            msg = format_telegram_announce(payload)
            await send_callback(run.parent_chat_id, msg)
        except Exception as e:
            logger.error("Failed to deliver async result to chat %s: %s", run.parent_chat_id, e)


def post_to_board(
    board: dict[str, list[dict]],
    user_id: str,
    payload: AnnouncePayload,
    task: str = "",
) -> None:
    """Post announce payload to the shared project board."""
    entries = board.setdefault(user_id, [])
    entries.append({
        "run_id": payload.run_id,
        "agent_id": payload.agent_id,
        "agent_name": payload.agent_name,
        "task": task[:200],
        "result": payload.result[:2000],
        "status": payload.status,
        "elapsed_seconds": round(payload.elapsed_seconds, 1),
        "tokens": payload.token_input + payload.token_output,
        "timestamp": __import__("time").time(),
    })
    # Evict oldest
    while len(entries) > MAX_BOARD_ENTRIES:
        entries.pop(0)
