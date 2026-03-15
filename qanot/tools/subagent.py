"""Sub-agent tool — spawn isolated agents for complex background tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from qanot.agent import ToolRegistry

if TYPE_CHECKING:
    from qanot.agent import Agent
    from qanot.config import Config
    from qanot.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Limits
MAX_CONCURRENT_PER_USER = 3
SUB_AGENT_TIMEOUT = 300  # 5 minutes
MAX_RESULT_CHARS = 6000  # Max chars to deliver back

# Track active sub-agents per user
_active_tasks: dict[str, dict[str, asyncio.Task]] = {}  # user_id → {task_id → Task}
MAX_TASK_CHARS = 10000  # Max chars for task description


def _get_active_count(user_id: str) -> int:
    """Get number of active sub-agent tasks for a user."""
    tasks = _active_tasks.get(user_id, {})
    # Clean up completed tasks
    done = [tid for tid, t in tasks.items() if t.done()]
    for tid in done:
        tasks.pop(tid, None)
    return len(tasks)


def _format_result(task_id: str, task_desc: str, result: str, elapsed: float) -> str:
    """Format sub-agent result for delivery to user."""
    # Truncate result if too long
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + "\n\n[... truncated]"

    return (
        f"**Sub-agent completed** ({elapsed:.0f}s)\n"
        f"Task: {task_desc}\n\n"
        f"---\n\n"
        f"{result}"
    )


async def _run_sub_agent(
    config: "Config",
    provider: "LLMProvider",
    tool_registry: ToolRegistry,
    task: str,
    task_id: str,
    user_id: str,
    chat_id: int,
    send_callback,
) -> None:
    """Run a sub-agent in background and deliver results via callback."""
    from qanot.agent import spawn_isolated_agent

    start = time.monotonic()
    task_preview = task[:200] + ("..." if len(task) > 200 else "")
    try:
        # Build a focused prompt for the sub-agent
        prompt = (
            f"You are a research sub-agent. Complete this task thoroughly and return "
            f"a clear, well-structured result.\n\n"
            f"TASK: {task}\n\n"
            f"INSTRUCTIONS:\n"
            f"- Use available tools (web_search, web_fetch, read_file, memory_search) as needed\n"
            f"- Be thorough but concise in your final answer\n"
            f"- Structure your findings clearly with headers and bullet points\n"
            f"- If the task involves research, cite sources\n"
            f"- Your response will be sent directly to the user"
        )

        result = await asyncio.wait_for(
            spawn_isolated_agent(
                config=config,
                provider=provider,
                tool_registry=tool_registry,
                prompt=prompt,
                session_id=f"subagent_{task_id}",
            ),
            timeout=SUB_AGENT_TIMEOUT,
        )

        elapsed = time.monotonic() - start
        formatted = _format_result(task_id[:8], task, result, elapsed)
        await send_callback(chat_id, formatted)
        logger.info(
            "Sub-agent %s completed in %.1fs for user %s",
            task_id[:8], elapsed, user_id,
        )

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        try:
            await send_callback(
                chat_id,
                f"**Sub-agent timed out** ({elapsed:.0f}s)\n"
                f"Task: {task_preview}\n\n"
                f"The task took too long (>{SUB_AGENT_TIMEOUT}s limit). "
                f"Try breaking it into smaller parts.",
            )
        except Exception as cb_err:
            logger.error("Failed to send timeout notification for sub-agent %s: %s", task_id[:8], cb_err)
        logger.warning("Sub-agent %s timed out for user %s", task_id[:8], user_id)

    except Exception as e:
        elapsed = time.monotonic() - start
        error_msg = str(e)[:500] if str(e) else "Unknown error"
        try:
            await send_callback(
                chat_id,
                f"**Sub-agent failed** ({elapsed:.0f}s)\n"
                f"Task: {task_preview}\n\n"
                f"Error: {error_msg}",
            )
        except Exception as cb_err:
            logger.error("Failed to send error notification for sub-agent %s: %s", task_id[:8], cb_err)
        logger.error("Sub-agent %s failed for user %s: %s", task_id[:8], user_id, e)

    finally:
        # Remove from active tasks
        tasks = _active_tasks.get(user_id, {})
        tasks.pop(task_id, None)
        if not tasks:
            _active_tasks.pop(user_id, None)


def register_sub_agent_tools(
    registry: ToolRegistry,
    config: "Config",
    provider: "LLMProvider",
    tool_registry: ToolRegistry,
    *,
    get_user_id: callable,
    get_chat_id: callable,
    send_callback,
) -> None:
    """Register sub-agent spawning tools.

    Args:
        registry: Tool registry to register into.
        config: Bot config.
        provider: LLM provider (shared with main agent).
        tool_registry: Tool registry for sub-agents (same tools available).
        get_user_id: Callable returning current user_id.
        get_chat_id: Callable returning current chat_id.
        send_callback: async callable(chat_id, text) to deliver results.
    """

    async def spawn_sub_agent(params: dict) -> str:
        """Spawn a background sub-agent for a complex task."""
        task = params.get("task", "").strip()
        if not task:
            return json.dumps({"error": "task is required — describe what to research or do"})

        if len(task) > MAX_TASK_CHARS:
            return json.dumps({
                "error": f"Task too long ({len(task)} chars). Max {MAX_TASK_CHARS} chars.",
            })

        try:
            user_id = get_user_id()
            chat_id = get_chat_id()
        except Exception as e:
            logger.error("Failed to resolve user/chat context for sub-agent: %s", e)
            return json.dumps({"error": "Cannot resolve user conversation context"})

        if not user_id or not chat_id:
            return json.dumps({"error": "Cannot spawn sub-agent outside user conversation"})

        # Check concurrency limit
        active = _get_active_count(user_id)
        if active >= MAX_CONCURRENT_PER_USER:
            return json.dumps({
                "error": f"Too many active sub-agents ({active}/{MAX_CONCURRENT_PER_USER}). Wait for one to finish.",
                "active_count": active,
            })

        # Spawn the sub-agent
        task_id = uuid.uuid4().hex[:16]

        bg_task = asyncio.create_task(
            _run_sub_agent(
                config=config,
                provider=provider,
                tool_registry=tool_registry,
                task=task,
                task_id=task_id,
                user_id=user_id,
                chat_id=chat_id,
                send_callback=send_callback,
            )
        )

        # Track it
        _active_tasks.setdefault(user_id, {})[task_id] = bg_task

        logger.info(
            "Sub-agent %s spawned for user %s: %s",
            task_id[:8], user_id, task[:80],
        )

        return json.dumps({
            "status": "spawned",
            "task_id": task_id[:8],
            "task": task,
            "message": "Sub-agent is working in the background. Results will be sent when ready.",
            "active_count": active + 1,
            "timeout_seconds": SUB_AGENT_TIMEOUT,
        })

    registry.register(
        name="spawn_sub_agent",
        description=(
            "Murakkab yoki ko'p vaqt talab qiladigan vazifa uchun fon sub-agentni ishga tushirish. "
            "Sub-agent mustaqil ishlaydi va natijalarni foydalanuvchiga yuboradi. "
            "Misol: chuqur tadqiqot, ko'p manbali tahlil, murakkab hisob-kitob. "
            "Oddiy savollar uchun ISHLATMANG — faqat murakkab, ko'p bosqichli vazifalar uchun."
        ),
        parameters={
            "type": "object",
            "required": ["task"],
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Vazifa tavsifi — aniq va batafsil. Sub-agent buni mustaqil bajaradi. "
                        "Masalan: 'Python va Rust tezligini solishtirish — benchmark ma'lumotlarni web_search orqali toping'"
                    ),
                },
            },
        },
        handler=spawn_sub_agent,
        category="agent",
    )

    # ── list_sub_agents ──

    async def list_sub_agents(params: dict) -> str:
        """List active sub-agents for current user."""
        user_id = get_user_id()
        _get_active_count(user_id)  # cleans up completed tasks as a side effect
        tasks = _active_tasks.get(user_id, {})

        if not tasks:
            return json.dumps({"active": 0, "message": "No active sub-agents."})

        agents = [
            {"task_id": tid[:8], "status": "running"}
            for tid in tasks
        ]

        return json.dumps({
            "active": len(agents),
            "agents": agents,
        })

    registry.register(
        name="list_sub_agents",
        description="Faol sub-agentlar ro'yxatini ko'rsatish.",
        parameters={"type": "object", "properties": {}},
        handler=list_sub_agents,
        category="agent",
    )
