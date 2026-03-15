"""Agent-to-agent delegation with ping-pong conversations and shared project board.

Five modes of agent interaction:
1. delegate_to_agent — one-shot task handoff, returns result
2. converse_with_agent — multi-turn ping-pong conversation (up to 5 turns)
3. project_board — shared results store, agents see each other's completed work
4. agent_session_history — read another agent's conversation transcript
5. agent_sessions_list — discover active agent sessions with metadata

Supports config-driven agents (config.agents[]) and built-in roles as fallback.
Agent-to-agent access control via delegate_allow config field.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from qanot.agent import ToolRegistry

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Limits
MAX_DELEGATION_DEPTH = 2
DELEGATION_TIMEOUT = 120
MAX_CONTEXT_CHARS = 4000
MAX_RESULT_CHARS = 8000
MAX_PING_PONG_TURNS = 5
MAX_BOARD_ENTRIES = 20  # Max entries on shared project board
MAX_SESSION_HISTORY = 50  # Max messages to keep per agent session
MAX_SESSION_HISTORY_RETURN = 20  # Max messages to return in history query
MAX_ACTIVITY_LOG = 100  # Max entries in the activity log
LOOP_DETECTION_WINDOW = 5  # Check last N delegations for loops
LOOP_SIMILARITY_THRESHOLD = 0.8  # Task similarity threshold for loop detection

# Shared project board — agents post results here, other agents can read them
# Keyed per user_id so different users don't see each other's boards
_project_boards: dict[str, list[dict]] = {}  # user_id → [{agent_id, task, result, timestamp}]

# Agent session transcripts — store conversation history for cross-agent reading
# Keyed: user_id → agent_id → [{role, content, timestamp, has_tools}]
_agent_sessions: dict[str, dict[str, list[dict]]] = {}

# Activity log — real-time feed of ALL agent interactions for monitoring
# Keyed per user_id → [{event, from_agent, to_agent, task, status, timestamp, ...}]
_activity_log: dict[str, list[dict]] = {}

# Notification callback — set by main.py to push live updates to Telegram
_notify_callback: dict[str, object] = {}  # user_id → async callable(text)

# Last access time per user — for TTL eviction of stale data
_user_last_access: dict[str, float] = {}
_EVICTION_TTL = 3600 * 6  # 6 hours — evict user data not accessed in this time


def _touch_user(user_id: str) -> None:
    """Record user access time for TTL eviction."""
    _user_last_access[user_id] = time.time()


def _evict_stale_users() -> None:
    """Remove data for users who haven't been active in _EVICTION_TTL seconds."""
    now = time.time()
    stale = [uid for uid, ts in _user_last_access.items() if now - ts > _EVICTION_TTL]
    for uid in stale:
        _project_boards.pop(uid, None)
        _agent_sessions.pop(uid, None)
        _activity_log.pop(uid, None)
        _notify_callback.pop(uid, None)
        _user_last_access.pop(uid, None)
    if stale:
        logger.info("Evicted stale delegation data for %d users", len(stale))


def set_notify_callback(user_id: str, callback) -> None:
    """Set notification callback for live agent monitoring."""
    _notify_callback[user_id] = callback


def _log_activity(
    user_id: str,
    event: str,
    *,
    from_agent: str = "",
    to_agent: str = "",
    task: str = "",
    status: str = "",
    detail: str = "",
    depth: int = 0,
) -> None:
    """Log an agent activity event."""
    _touch_user(user_id)
    # Periodic eviction (every 100th call, lightweight)
    if len(_user_last_access) > 10 and hash(user_id) % 100 == 0:
        _evict_stale_users()
    log = _activity_log.setdefault(user_id, [])
    entry = {
        "event": event,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "task": task[:200],
        "status": status,
        "detail": detail[:500],
        "depth": depth,
        "timestamp": time.time(),
    }
    log.append(entry)
    while len(log) > MAX_ACTIVITY_LOG:
        log.pop(0)

    # Send live notification to user
    callback = _notify_callback.get(user_id)
    if callback:
        _send_notification(callback, event, entry)


def _send_notification(callback, event: str, entry: dict) -> None:
    """Fire-and-forget notification to user."""
    icons = {
        "delegate_start": "🔄",
        "delegate_done": "✅",
        "delegate_error": "❌",
        "delegate_timeout": "⏰",
        "converse_start": "💬",
        "converse_turn": "🔁",
        "converse_done": "✅",
        "loop_detected": "🚫",
    }
    icon = icons.get(event, "📋")

    if event == "delegate_start":
        text = f"{icon} **Agent delegation**\n{entry['from_agent'] or 'Main'} → {entry['to_agent']}\nTask: {entry['task'][:100]}"
    elif event == "delegate_done":
        text = f"{icon} **Delegation complete**\n{entry['to_agent']} finished ({entry['detail']})"
    elif event in ("delegate_error", "delegate_timeout"):
        text = f"{icon} **Delegation failed**\n{entry['to_agent']}: {entry['detail'][:100]}"
    elif event == "converse_start":
        text = f"{icon} **Agent conversation**\n{entry['from_agent'] or 'Main'} ↔ {entry['to_agent']}"
    elif event == "converse_turn":
        text = f"{icon} Turn {entry['detail']} — {entry['to_agent']}"
    elif event == "loop_detected":
        text = f"{icon} **Loop detected!** {entry['detail']}"
    else:
        return  # Don't notify for unknown events

    try:
        if inspect.iscoroutinefunction(callback):
            asyncio.create_task(callback(text))
        elif callable(callback):
            # Sync callback — wrap in coroutine
            result = callback(text)
            if inspect.isawaitable(result):
                asyncio.create_task(result)
    except Exception as e:
        logger.debug("Notification send failed (non-fatal): %s", e)


def _check_for_loop(user_id: str, agent_id: str, task: str) -> str | None:
    """Check if this delegation looks like a loop.

    Returns error message if loop detected, None if ok.
    """
    log = _activity_log.get(user_id, [])
    if not log:
        return None

    # Check ping-pong first: A→B→A→B pattern (needs fewer entries)
    recent_all = [
        e for e in log[-10:]
        if e["event"] == "delegate_start"
    ]
    if len(recent_all) >= 4:
        agents = [e["to_agent"] for e in recent_all[-4:]]
        if agents[0] == agents[2] and agents[1] == agents[3] and agents[0] != agents[1]:
            msg = f"Ping-pong loop detected: {agents[0]} ↔ {agents[1]}. Breaking loop."
            _log_activity(user_id, "loop_detected", to_agent=agent_id, detail=msg)
            return msg

    # Check repeated similar tasks to the same agent
    recent = [
        e for e in log[-LOOP_DETECTION_WINDOW * 2:]
        if e["event"] == "delegate_start" and e["to_agent"] == agent_id
    ]

    if len(recent) < LOOP_DETECTION_WINDOW:
        return None

    task_lower = task.lower().strip()
    similar_count = 0
    for entry in recent[-LOOP_DETECTION_WINDOW:]:
        entry_task = entry["task"].lower().strip()
        task_words = set(task_lower.split())
        entry_words = set(entry_task.split())
        if not task_words or not entry_words:
            continue
        overlap = len(task_words & entry_words) / max(len(task_words), len(entry_words))
        if overlap >= LOOP_SIMILARITY_THRESHOLD:
            similar_count += 1

    if similar_count >= LOOP_DETECTION_WINDOW - 1:
        msg = (
            f"Loop detected: agent '{agent_id}' received {similar_count} similar tasks "
            f"in last {LOOP_DETECTION_WINDOW} delegations. Breaking loop."
        )
        _log_activity(user_id, "loop_detected", to_agent=agent_id, detail=msg)
        return msg

    return None


def get_activity_log(user_id: str, limit: int = 20) -> list[dict]:
    """Get recent activity log entries for a user."""
    log = _activity_log.get(user_id, [])
    return log[-limit:]


# ── Group monitoring ─────────────────────────────────────────
# Mirror agent conversations to a Telegram group so the user can watch live

_group_bot_cache: dict[str, object] = {}  # bot_token → aiogram.Bot


def _get_agent_bot_token(config: "Config", agent_id: str) -> str:
    """Get the bot token for an agent. Falls back to main bot token."""
    for ad in config.agents:
        if ad.id == agent_id and ad.bot_token:
            return ad.bot_token
    return config.bot_token


def _get_agent_name(config: "Config", agent_id: str) -> str:
    """Get human-readable name for an agent."""
    if not agent_id or agent_id == "main":
        return config.bot_name or "Main Bot"
    for ad in config.agents:
        if ad.id == agent_id:
            return ad.name or ad.id
    return agent_id


async def _get_group_bot(config: "Config", agent_id: str):
    """Get or create an aiogram Bot for posting to group as a specific agent."""
    bot_token = _get_agent_bot_token(config, agent_id)
    if bot_token not in _group_bot_cache:
        from aiogram import Bot
        _group_bot_cache[bot_token] = Bot(token=bot_token)
    return _group_bot_cache[bot_token]


async def _send_typing_to_group(config: "Config", agent_id: str) -> None:
    """Send typing indicator in the monitoring group as the agent's bot."""
    monitor_group = getattr(config, "monitor_group_id", 0)
    if not monitor_group:
        return
    try:
        bot = await _get_group_bot(config, agent_id)
        await bot.send_chat_action(chat_id=monitor_group, action="typing")
    except Exception:
        pass  # Non-fatal


async def _mirror_to_group(
    config: "Config",
    from_agent: str,
    to_agent: str,
    text: str,
    *,
    direction: str = "message",
) -> None:
    """Mirror an agent interaction message to the monitoring group.

    Posts as the from_agent's own bot (if it has a token), making it look
    like a real chat between bots in the group.
    """
    monitor_group = getattr(config, "monitor_group_id", 0)
    if not monitor_group:
        return

    try:
        bot = await _get_group_bot(config, from_agent)

        # Natural chat format — no ugly prefixes, just the message
        # Only add a light header for delegate/converse start events
        if direction == "delegate":
            msg = f"📋 <i>{_get_agent_name(config, to_agent)}</i> ga vazifa:\n\n{text[:3000]}"
        elif direction == "converse":
            msg = f"💬 <i>{_get_agent_name(config, to_agent)}</i> bilan suhbat boshladim:\n\n{text[:3000]}"
        else:
            msg = text[:3500]

        await bot.send_message(
            chat_id=monitor_group,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.debug("Failed to mirror to monitoring group: %s", e)


# Built-in roles
BUILTIN_ROLES: dict[str, dict] = {
    "researcher": {
        "name": "Tadqiqotchi",
        "prompt": (
            "You are a research specialist. Your job is to thoroughly investigate "
            "a topic using available tools (web_search, web_fetch, memory_search, read_file). "
            "Return well-structured findings with sources cited. "
            "Be thorough but concise — focus on facts, not opinions."
        ),
    },
    "analyst": {
        "name": "Tahlilchi",
        "prompt": (
            "You are an analysis specialist. Your job is to analyze data, code, or "
            "information and provide clear insights. Break down complex topics into "
            "understandable parts. Use structured formats (tables, bullet points, comparisons). "
            "Focus on actionable conclusions."
        ),
    },
    "coder": {
        "name": "Dasturchi",
        "prompt": (
            "You are a coding specialist. Your job is to write, review, or debug code. "
            "Use read_file and write_file tools as needed. Follow existing project conventions. "
            "Write clean, tested, production-ready code. Explain key decisions briefly."
        ),
    },
    "reviewer": {
        "name": "Tekshiruvchi",
        "prompt": (
            "You are a code review specialist. Your job is to review code for bugs, "
            "security issues, performance problems, and style violations. "
            "Use read_file to examine code. Be specific about issues found — include "
            "file paths and line numbers. Suggest concrete fixes."
        ),
    },
    "writer": {
        "name": "Yozuvchi",
        "prompt": (
            "You are a writing specialist. Your job is to draft, edit, or improve text — "
            "documentation, messages, summaries, reports. Write clearly and professionally. "
            "Match the requested tone and audience."
        ),
    },
}

_ALWAYS_DENIED = frozenset({
    "spawn_sub_agent",
    "list_sub_agents",
})


# ── Helpers ──────────────────────────────────────────────────


def _record_session_message(
    user_id: str,
    agent_id: str,
    role: str,
    content: str,
    has_tools: bool = False,
) -> None:
    """Record a message in an agent's session transcript."""
    sessions = _agent_sessions.setdefault(user_id, {})
    history = sessions.setdefault(agent_id, [])
    history.append({
        "role": role,
        "content": content[:2000],  # Keep compact
        "timestamp": time.time(),
        "has_tools": has_tools,
    })
    # Evict oldest if over limit
    while len(history) > MAX_SESSION_HISTORY:
        history.pop(0)


def _get_session_history(
    user_id: str,
    agent_id: str,
    limit: int = MAX_SESSION_HISTORY_RETURN,
    include_tools: bool = False,
) -> list[dict]:
    """Get conversation transcript for an agent."""
    sessions = _agent_sessions.get(user_id, {})
    history = sessions.get(agent_id, [])
    if not include_tools:
        history = [m for m in history if not m.get("has_tools")]
    return history[-limit:]


def _get_active_sessions(user_id: str) -> list[dict]:
    """List all agent sessions with metadata for a user."""
    sessions = _agent_sessions.get(user_id, {})
    result = [
        {
            "agent_id": agent_id,
            "message_count": len(history),
            "last_active": history[-1]["timestamp"],
            "last_message_preview": history[-1]["content"][:100],
        }
        for agent_id, history in sessions.items()
        if history
    ]
    # Sort by last_active descending
    result.sort(key=lambda x: x["last_active"], reverse=True)
    return result


def _check_delegate_allow(
    caller_agent_id: str,
    target_agent_id: str,
    config: "Config",
) -> bool:
    """Check if caller agent is allowed to delegate to target agent.

    Rules:
    - Main agent (caller_agent_id="") can always delegate to anyone
    - If caller has delegate_allow=[] (empty), it can delegate to anyone
    - If caller has delegate_allow=["x","y"], it can only delegate to x and y
    """
    if not caller_agent_id:
        return True  # Main agent can always delegate

    for agent_def in config.agents:
        if agent_def.id == caller_agent_id:
            if not agent_def.delegate_allow:
                return True  # Empty = allow all
            return target_agent_id in agent_def.delegate_allow

    return True  # Builtin roles can delegate to anyone


def _build_delegate_registry(
    parent_registry: ToolRegistry,
    depth: int,
    *,
    tools_allow: list[str] | None = None,
    tools_deny: list[str] | None = None,
) -> ToolRegistry:
    """Build a restricted tool registry for a delegated agent."""
    child = ToolRegistry()
    denied = set(_ALWAYS_DENIED)
    if depth >= MAX_DELEGATION_DEPTH:
        denied.add("delegate_to_agent")
        denied.add("converse_with_agent")
    if tools_deny:
        denied.update(tools_deny)

    for tool_def in parent_registry.get_definitions():
        name = tool_def["name"]
        if tools_allow and name not in tools_allow:
            continue
        if name in denied:
            continue
        handler = parent_registry.get_handler(name)
        if handler:
            child.register(
                name=name,
                description=tool_def.get("description", ""),
                parameters=tool_def.get("input_schema", {}),
                handler=handler,
            )
    return child


def _truncate_context(context: str) -> str:
    """Truncate context to prevent excessive token usage."""
    if len(context) > MAX_CONTEXT_CHARS:
        return context[:MAX_CONTEXT_CHARS] + "\n\n[... context truncated]"
    return context


def _get_available_agents(config: "Config") -> dict[str, dict]:
    """Build merged agent map: config agents + built-in roles."""
    agents: dict[str, dict] = {}

    for role_id, role_info in BUILTIN_ROLES.items():
        agents[role_id] = {
            "name": role_info["name"],
            "prompt": role_info["prompt"],
            "model": "",
            "provider": "",
            "api_key": "",
            "tools_allow": [],
            "tools_deny": [],
            "timeout": DELEGATION_TIMEOUT,
            "source": "builtin",
        }

    for agent_def in config.agents:
        agents[agent_def.id] = {
            "name": agent_def.name or agent_def.id,
            "prompt": agent_def.prompt or f"You are the {agent_def.id} agent. Complete tasks assigned to you.",
            "model": agent_def.model,
            "provider": agent_def.provider,
            "api_key": agent_def.api_key,
            "tools_allow": agent_def.tools_allow,
            "tools_deny": agent_def.tools_deny,
            "timeout": agent_def.timeout or DELEGATION_TIMEOUT,
            "source": "config",
        }

    return agents


def _create_provider_for_agent(
    agent_info: dict,
    default_provider: "LLMProvider",
    config: "Config",
) -> "LLMProvider":
    """Create a provider for a delegated agent (reuses main if no overrides)."""
    agent_model = agent_info.get("model", "")
    agent_provider_type = agent_info.get("provider", "")
    agent_api_key = agent_info.get("api_key", "")

    if not agent_model and not agent_provider_type:
        return default_provider

    from qanot.providers.failover import ProviderProfile, _create_single_provider

    profile = ProviderProfile(
        name=f"delegate_{agent_info.get('name', 'agent')}",
        provider_type=agent_provider_type or config.provider,
        api_key=agent_api_key or config.api_key,
        model=agent_model or config.model,
    )
    return _create_single_provider(profile)


def _load_agent_identity(workspace_dir: str, agent_id: str) -> str:
    """Load per-agent identity file if it exists.

    Looks for: {workspace_dir}/agents/{agent_id}/SOUL.md
    Falls back to empty string if not found.
    """
    soul_path = Path(workspace_dir) / "agents" / agent_id / "SOUL.md"
    try:
        if soul_path.exists():
            content = soul_path.read_text(encoding="utf-8").strip()
            if content:
                logger.debug("Loaded identity for agent '%s' from %s", agent_id, soul_path)
                return content
    except Exception as e:
        logger.warning("Failed to read agent identity %s: %s", soul_path, e)
    return ""


def _build_agent_prompt(
    agent_info: dict,
    agent_id: str,
    workspace_dir: str,
    task: str,
    context: str = "",
    board_summary: str = "",
) -> str:
    """Build the full prompt for a delegated agent."""
    parts: list[str] = []

    # Per-agent identity file (SOUL.md) takes priority
    identity = _load_agent_identity(workspace_dir, agent_id)
    if identity:
        parts.append(identity)
    else:
        parts.append(agent_info["prompt"])

    parts.append("")
    parts.append(f"TASK: {task}")

    if context:
        parts.extend(["", "CONTEXT:", _truncate_context(context)])

    if board_summary:
        parts.extend(["", "PROJECT BOARD (other agents' completed work):", board_summary])

    # Let agent know about its identity file
    identity_path = f"agents/{agent_id}/SOUL.md"
    identity_note = (
        f"- Your identity file is at: {identity_path}\n"
        f"  You can read_file and write_file to update your own personality/skills."
    )

    parts.extend([
        "",
        "INSTRUCTIONS:",
        "- Complete the task thoroughly and return a clear result.",
        "- Use available tools as needed.",
        "- Be concise but complete.",
        "- Your response will be returned to the requesting agent.",
        identity_note,
    ])

    return "\n".join(parts)


def _get_board_summary(user_id: str, exclude_agent: str = "") -> str:
    """Get a summary of the project board for context injection."""
    board = _project_boards.get(user_id, [])
    if not board:
        return ""

    lines: list[str] = []
    for entry in board[-10:]:  # Last 10 entries
        if entry["agent_id"] == exclude_agent:
            continue
        # Truncate result for board summary
        result_preview = entry["result"][:500] + ("..." if len(entry["result"]) > 500 else "")
        lines.append(
            f"- **{entry['agent_name']}** ({entry['agent_id']}): {entry['task'][:100]}\n"
            f"  Result: {result_preview}"
        )

    return "\n".join(lines) if lines else ""


def _post_to_board(user_id: str, agent_id: str, agent_name: str, task: str, result: str) -> None:
    """Post an agent's completed work to the shared project board."""
    board = _project_boards.setdefault(user_id, [])
    board.append({
        "agent_id": agent_id,
        "agent_name": agent_name,
        "task": task[:200],
        "result": result[:2000],  # Keep board entries compact
        "timestamp": time.time(),
    })
    # Evict oldest if over limit
    while len(board) > MAX_BOARD_ENTRIES:
        board.pop(0)


async def _create_delegate_agent(
    config: "Config",
    provider: "LLMProvider",
    child_registry: ToolRegistry,
    prompt: str,
    agent_id: str,
    timeout: int,
) -> str:
    """Create and run an isolated delegate agent. Returns the result text."""
    from qanot.agent import Agent
    from qanot.context import ContextTracker
    from qanot.session import SessionWriter

    session = SessionWriter(config.sessions_dir)
    session.new_session(f"delegate_{agent_id}_{int(time.time())}")

    ctx = ContextTracker(
        max_tokens=config.max_context_tokens,
        workspace_dir=config.workspace_dir,
    )

    agent = Agent(
        config=config,
        provider=provider,
        tool_registry=child_registry,
        session=session,
        context=ctx,
        prompt_mode="minimal",
    )

    result = await asyncio.wait_for(agent.run_turn(prompt), timeout=timeout)
    return result


# ── Registration ─────────────────────────────────────────────


def register_delegate_tools(
    registry: ToolRegistry,
    config: "Config",
    provider: "LLMProvider",
    parent_registry: ToolRegistry,
    *,
    get_user_id: callable,
    current_depth: int = 0,
    caller_agent_id: str = "",
) -> None:
    """Register agent-to-agent delegation, conversation, and project board tools."""
    available_agents = _get_available_agents(config)

    # ── 1. delegate_to_agent (one-shot) ──

    async def delegate_to_agent(params: dict) -> str:
        """Delegate a task to a specialist agent and wait for the result."""
        task = params.get("task", "")
        if not isinstance(task, str):
            return json.dumps({"error": "task must be a string"})
        task = task.strip()
        if not task:
            return json.dumps({"error": "task is required"})
        if len(task) > 10000:
            return json.dumps({"error": f"task too long ({len(task)} chars, max 10000)"})

        agent_id = params.get("agent_id", "researcher")
        if not isinstance(agent_id, str):
            return json.dumps({"error": "agent_id must be a string"})
        agent_id = agent_id.lower()
        if agent_id not in available_agents:
            return json.dumps({
                "error": f"Unknown agent: {agent_id}",
                "available_agents": list(available_agents.keys()),
            })

        # Check delegate_allow access control
        if not _check_delegate_allow(caller_agent_id, agent_id, config):
            return json.dumps({
                "error": f"Agent '{caller_agent_id}' is not allowed to delegate to '{agent_id}'.",
                "hint": "Check delegate_allow in agent config.",
            })

        context = params.get("context", "").strip()
        depth = current_depth + 1

        if depth > MAX_DELEGATION_DEPTH:
            return json.dumps({
                "error": f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached.",
            })

        agent_info = available_agents[agent_id]
        timeout = agent_info.get("timeout", DELEGATION_TIMEOUT)
        user_id = get_user_id() or "delegate"

        # Loop detection
        loop_msg = _check_for_loop(user_id, agent_id, task)
        if loop_msg:
            return json.dumps({"error": loop_msg, "status": "loop_detected"})

        # Log start
        _log_activity(
            user_id, "delegate_start",
            from_agent=caller_agent_id, to_agent=agent_id,
            task=task, depth=depth,
        )

        # Get project board for context
        board_summary = _get_board_summary(user_id, exclude_agent=agent_id)

        prompt = _build_agent_prompt(
            agent_info, agent_id, config.workspace_dir,
            task, context, board_summary,
        )

        child_registry = _build_delegate_registry(
            parent_registry, depth,
            tools_allow=agent_info.get("tools_allow") or None,
            tools_deny=agent_info.get("tools_deny") or None,
        )
        delegate_provider = _create_provider_for_agent(agent_info, provider, config)

        start = time.monotonic()
        logger.info("Delegating to '%s' (depth=%d) for user %s: %s", agent_id, depth, user_id, task[:80])

        # Mirror task to monitoring group — posted by requester's bot
        await _mirror_to_group(
            config, caller_agent_id or "main", agent_id,
            task[:3000], direction="delegate",
        )

        try:
            # Send typing indicator in monitoring group
            await _send_typing_to_group(config, agent_id)

            result = await _create_delegate_agent(
                config, delegate_provider, child_registry, prompt, agent_id, timeout,
            )
            elapsed = time.monotonic() - start

            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "\n\n[... result truncated]"

            # Post to shared project board
            _post_to_board(user_id, agent_id, agent_info["name"], task, result)

            # Record in session history
            _record_session_message(user_id, agent_id, "user", f"[delegation] {task}")
            _record_session_message(user_id, agent_id, "assistant", result)

            # Log completion
            _log_activity(
                user_id, "delegate_done",
                from_agent=caller_agent_id, to_agent=agent_id,
                task=task, status="completed",
                detail=f"{elapsed:.1f}s, {len(result)} chars",
            )

            # Mirror result to monitoring group — posted by the AGENT's bot
            await _mirror_to_group(
                config, agent_id, caller_agent_id or "main",
                result[:3000], direction="result",
            )

            logger.info("Delegation to '%s' completed in %.1fs", agent_id, elapsed)
            return json.dumps({
                "status": "completed",
                "agent_id": agent_id,
                "agent_name": agent_info["name"],
                "result": result,
                "elapsed_seconds": round(elapsed, 1),
            })

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            logger.warning("Delegation to '%s' timed out after %.0fs", agent_id, elapsed)
            _log_activity(
                user_id, "delegate_timeout",
                from_agent=caller_agent_id, to_agent=agent_id,
                task=task, status="timeout",
                detail=f"Timed out after {timeout}s",
            )
            return json.dumps({
                "status": "timeout", "agent_id": agent_id,
                "error": f"Agent timed out after {timeout}s.",
                "elapsed_seconds": round(elapsed, 1),
            })
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Delegation to '%s' failed: %s", agent_id, e)
            _log_activity(
                user_id, "delegate_error",
                from_agent=caller_agent_id, to_agent=agent_id,
                task=task, status="error",
                detail=str(e)[:200],
            )
            return json.dumps({
                "status": "error", "agent_id": agent_id,
                "error": str(e), "elapsed_seconds": round(elapsed, 1),
            })

    # ── 2. converse_with_agent (ping-pong) ──

    async def converse_with_agent(params: dict) -> str:
        """Start a multi-turn conversation with an agent (ping-pong)."""
        message = params.get("message", "")
        if not isinstance(message, str):
            return json.dumps({"error": "message must be a string"})
        message = message.strip()
        if not message:
            return json.dumps({"error": "message is required"})
        if len(message) > 10000:
            return json.dumps({"error": f"message too long ({len(message)} chars, max 10000)"})

        agent_id = params.get("agent_id", "researcher").lower()
        if agent_id not in available_agents:
            return json.dumps({
                "error": f"Unknown agent: {agent_id}",
                "available_agents": list(available_agents.keys()),
            })

        # Check delegate_allow access control
        if not _check_delegate_allow(caller_agent_id, agent_id, config):
            return json.dumps({
                "error": f"Agent '{caller_agent_id}' is not allowed to converse with '{agent_id}'.",
                "hint": "Check delegate_allow in agent config.",
            })

        max_turns = min(params.get("max_turns", 3), MAX_PING_PONG_TURNS)
        depth = current_depth + 1

        if depth > MAX_DELEGATION_DEPTH:
            return json.dumps({
                "error": f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached.",
            })

        agent_info = available_agents[agent_id]
        timeout = agent_info.get("timeout", DELEGATION_TIMEOUT)
        user_id = get_user_id() or "delegate"

        # Loop detection
        loop_msg = _check_for_loop(user_id, agent_id, message)
        if loop_msg:
            return json.dumps({"error": loop_msg, "status": "loop_detected"})

        # Log start
        _log_activity(
            user_id, "converse_start",
            from_agent=caller_agent_id, to_agent=agent_id,
            task=message, depth=depth,
        )

        child_registry = _build_delegate_registry(
            parent_registry, depth,
            tools_allow=agent_info.get("tools_allow") or None,
            tools_deny=agent_info.get("tools_deny") or None,
        )
        delegate_provider = _create_provider_for_agent(agent_info, provider, config)
        board_summary = _get_board_summary(user_id, exclude_agent=agent_id)

        # Load agent identity
        identity = _load_agent_identity(config.workspace_dir, agent_id)
        agent_prompt = identity if identity else agent_info["prompt"]

        start = time.monotonic()
        conversation_log: list[dict] = []

        logger.info(
            "Starting ping-pong with '%s' (max %d turns) for user %s",
            agent_id, max_turns, user_id,
        )

        # Mirror first message to group — posted by the REQUESTER bot
        await _mirror_to_group(
            config, caller_agent_id or "main", agent_id,
            message[:3000], direction="converse",
        )

        # Turn 1: send initial message to agent
        current_message = message

        try:
            for turn in range(1, max_turns + 1):
                # Send typing indicator in monitoring group
                await _send_typing_to_group(config, agent_id)

                # Build prompt with conversation history
                turn_prompt_parts = [
                    agent_prompt,
                    "",
                    "You are in a multi-turn conversation with another agent.",
                    f"This is turn {turn} of {max_turns}.",
                    "",
                ]

                if board_summary:
                    turn_prompt_parts.extend([
                        "PROJECT BOARD (other agents' completed work):",
                        board_summary,
                        "",
                    ])

                if conversation_log:
                    turn_prompt_parts.append("CONVERSATION SO FAR:")
                    for entry in conversation_log:
                        role_label = "Requesting agent" if entry["role"] == "requester" else f"{agent_info['name']}"
                        turn_prompt_parts.append(f"[{role_label}]: {entry['message']}")
                    turn_prompt_parts.append("")

                turn_prompt_parts.extend([
                    f"[Requesting agent]: {current_message}",
                    "",
                    "INSTRUCTIONS:",
                    "- Respond to the message above.",
                    "- Use tools if needed to research or verify.",
                    f"- If you have completed your work, say DONE at the start of your response.",
                    f"- You have {max_turns - turn} turns remaining after this one.",
                ])

                turn_prompt = "\n".join(turn_prompt_parts)

                result = await _create_delegate_agent(
                    config, delegate_provider, child_registry,
                    turn_prompt, agent_id, timeout,
                )

                conversation_log.append({"role": "requester", "message": current_message})
                conversation_log.append({"role": "agent", "message": result})

                # Log turn
                _log_activity(
                    user_id, "converse_turn",
                    from_agent=caller_agent_id, to_agent=agent_id,
                    task=message, detail=f"{turn}/{max_turns}",
                )

                # Mirror agent's response to group — posted by the AGENT's bot
                # This makes it look like a real chat: Bot A writes, Bot B responds
                await asyncio.sleep(0.5)  # Small delay for natural feel
                await _mirror_to_group(
                    config, agent_id, caller_agent_id or "main",
                    result[:3000], direction="turn",
                )

                logger.debug("Ping-pong turn %d/%d with '%s'", turn, max_turns, agent_id)

                # Check if agent signaled completion
                if result.strip().upper().startswith("DONE"):
                    break

                # For next turn, the result becomes the context
                if turn < max_turns:
                    current_message = result
                    # Mirror the requester's next message (which is the agent's
                    # previous response being relayed back) — skip this to avoid
                    # duplicate messages in group since the agent already posted

            elapsed = time.monotonic() - start

            # Compile final result
            final_result = conversation_log[-1]["message"] if conversation_log else ""
            if len(final_result) > MAX_RESULT_CHARS:
                final_result = final_result[:MAX_RESULT_CHARS] + "\n\n[... truncated]"

            # Post final result to board
            _post_to_board(user_id, agent_id, agent_info["name"], message[:200], final_result)

            # Record conversation in session history
            for entry in conversation_log:
                role = "user" if entry["role"] == "requester" else "assistant"
                _record_session_message(user_id, agent_id, role, entry["message"])

            # Log completion
            turns_done = len(conversation_log) // 2
            _log_activity(
                user_id, "converse_done",
                from_agent=caller_agent_id, to_agent=agent_id,
                task=message, status="completed",
                detail=f"{turns_done} turns, {elapsed:.1f}s",
            )

            logger.info(
                "Ping-pong with '%s' completed: %d turns in %.1fs",
                agent_id, turns_done, elapsed,
            )

            return json.dumps({
                "status": "completed",
                "agent_id": agent_id,
                "agent_name": agent_info["name"],
                "turns": turns_done,
                "conversation": [
                    {"role": e["role"], "message": e["message"][:1000]}
                    for e in conversation_log
                ],
                "final_result": final_result,
                "elapsed_seconds": round(elapsed, 1),
            })

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            logger.warning("Ping-pong with '%s' timed out", agent_id)
            _log_activity(
                user_id, "delegate_timeout",
                from_agent=caller_agent_id, to_agent=agent_id,
                task=message, status="timeout",
                detail=f"Timed out after {timeout}s",
            )
            return json.dumps({
                "status": "timeout", "agent_id": agent_id,
                "turns_completed": len(conversation_log) // 2,
                "error": f"Conversation timed out after {timeout}s.",
                "elapsed_seconds": round(elapsed, 1),
            })
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Ping-pong with '%s' failed: %s", agent_id, e)
            _log_activity(
                user_id, "delegate_error",
                from_agent=caller_agent_id, to_agent=agent_id,
                task=message, status="error",
                detail=str(e)[:200],
            )
            return json.dumps({
                "status": "error", "agent_id": agent_id,
                "error": str(e), "elapsed_seconds": round(elapsed, 1),
            })

    # ── 3. project_board (shared results) ──

    async def view_project_board(params: dict) -> str:
        """View the shared project board — see what other agents have done."""
        user_id = get_user_id() or "default"
        board = _project_boards.get(user_id, [])

        if not board:
            return json.dumps({"entries": [], "message": "Project board is empty."})

        agent_filter = params.get("agent_id", "").strip()
        entries = [
            {
                "agent_id": entry["agent_id"],
                "agent_name": entry["agent_name"],
                "task": entry["task"],
                "result": entry["result"][:1000],
                "timestamp": entry["timestamp"],
            }
            for entry in board
            if not agent_filter or entry["agent_id"] == agent_filter
        ]

        return json.dumps({"entries": entries, "total": len(entries)})

    async def clear_project_board(params: dict) -> str:
        """Clear the shared project board."""
        user_id = get_user_id() or "default"
        count = len(_project_boards.get(user_id, []))
        _project_boards.pop(user_id, None)
        return json.dumps({"cleared": count})

    # ── 4. list_agents ──

    async def list_agents(params: dict) -> str:
        """List all available agents and their capabilities."""
        agents_list = []
        for aid, info in available_agents.items():
            agent_entry = {
                "agent_id": aid,
                "name": info["name"],
                "description": info["prompt"][:120] + ("..." if len(info["prompt"]) > 120 else ""),
                "source": info.get("source", "builtin"),
                "has_identity_file": Path(config.workspace_dir, "agents", aid, "SOUL.md").exists(),
            }
            if info.get("model"):
                agent_entry["model"] = info["model"]
            agents_list.append(agent_entry)

        return json.dumps({
            "agents": agents_list,
            "total": len(agents_list),
            "max_depth": MAX_DELEGATION_DEPTH,
            "current_depth": current_depth,
            "can_delegate": current_depth < MAX_DELEGATION_DEPTH,
        })

    # ── 5. agent_session_history ──

    async def agent_session_history(params: dict) -> str:
        """Read another agent's conversation transcript."""
        agent_id = params.get("agent_id", "").strip()
        if not agent_id:
            return json.dumps({"error": "agent_id is required"})

        if agent_id not in available_agents:
            return json.dumps({
                "error": f"Unknown agent: {agent_id}",
                "available_agents": list(available_agents.keys()),
            })

        user_id = get_user_id() or "default"
        limit = min(params.get("limit", MAX_SESSION_HISTORY_RETURN), MAX_SESSION_HISTORY_RETURN)
        include_tools = params.get("include_tools", False)

        history = _get_session_history(user_id, agent_id, limit, include_tools)

        if not history:
            return json.dumps({
                "agent_id": agent_id,
                "messages": [],
                "message": f"No session history for agent '{agent_id}'.",
            })

        return json.dumps({
            "agent_id": agent_id,
            "agent_name": available_agents[agent_id]["name"],
            "messages": history,
            "total": len(history),
        })

    # ── 6. agent_sessions_list ──

    async def agent_sessions_list(params: dict) -> str:
        """List all active agent sessions with metadata."""
        user_id = get_user_id() or "default"
        sessions = _get_active_sessions(user_id)

        if not sessions:
            return json.dumps({
                "sessions": [],
                "message": "No active agent sessions.",
            })

        return json.dumps({
            "sessions": sessions,
            "total": len(sessions),
        })

    # ── 7. view_agent_activity ──

    async def view_agent_activity(params: dict) -> str:
        """View real-time agent activity log — see what agents are doing."""
        user_id = get_user_id() or "default"
        limit = min(params.get("limit", 20), 50)
        agent_filter = params.get("agent_id", "").strip()

        log = get_activity_log(user_id, limit=limit)

        if agent_filter:
            log = [
                e for e in log
                if e["from_agent"] == agent_filter or e["to_agent"] == agent_filter
            ]

        if not log:
            return json.dumps({
                "entries": [],
                "message": "No agent activity yet.",
            })

        return json.dumps({
            "entries": log,
            "total": len(log),
        })

    # ── 8. set_monitor_group ──

    async def set_monitor_group(params: dict) -> str:
        """Set a Telegram group for live agent monitoring.

        Add both agent bots and the main bot to the group,
        then use this tool to start mirroring conversations there.
        """
        group_id = params.get("group_id", 0)
        if not group_id:
            return json.dumps({"error": "group_id is required (negative number for groups)"})

        try:
            group_id_int = int(group_id)
        except (ValueError, TypeError):
            return json.dumps({"error": f"group_id must be an integer, got: {type(group_id).__name__}"})

        if group_id_int == 0:
            return json.dumps({"error": "group_id cannot be zero"})

        if group_id_int > 0:
            logger.warning("set_monitor_group called with positive group_id=%d; Telegram group IDs are typically negative", group_id_int)

        config.monitor_group_id = group_id_int

        # Persist to config.json
        config_path = os.environ.get("QANOT_CONFIG", "/data/config.json")
        p = Path(config_path)
        try:
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                raw["monitor_group_id"] = config.monitor_group_id
                # Write to temp file first, then rename for atomicity
                tmp_path = p.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp_path.replace(p)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to persist monitor_group_id to config: %s", e)
            return json.dumps({
                "status": "partial",
                "monitor_group_id": config.monitor_group_id,
                "warning": f"Group set in memory but failed to persist to config file: {e}",
            })

        return json.dumps({
            "status": "configured",
            "monitor_group_id": config.monitor_group_id,
            "message": (
                f"Monitoring group set to {group_id_int}. "
                "All agent-to-agent interactions will be mirrored there. "
                "Make sure all agent bots are added to this group."
            ),
        })

    # ── Register all tools ──

    if current_depth < MAX_DELEGATION_DEPTH:
        agents_desc = ", ".join(f"{aid} ({info['name']})" for aid, info in available_agents.items())

        registry.register(
            name="delegate_to_agent",
            description=(
                "Vazifani agentga topshirish va natijani kutish (bir martalik). "
                f"Mavjud agentlar: {agents_desc}."
            ),
            parameters={
                "type": "object",
                "required": ["task", "agent_id"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Vazifa tavsifi — aniq va batafsil.",
                    },
                    "agent_id": {
                        "type": "string",
                        "enum": list(available_agents.keys()),
                        "description": "Agent identifikatori.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Ixtiyoriy kontekst — faqat vazifaga tegishli ma'lumot.",
                    },
                },
            },
            handler=delegate_to_agent,
            category="agent",
        )

        registry.register(
            name="converse_with_agent",
            description=(
                "Agent bilan ko'p bosqichli suhbat boshlash (ping-pong). "
                "Agent javob beradi, siz javob berasiz, agent yana javob beradi. "
                f"Maksimum {MAX_PING_PONG_TURNS} tur. "
                "Murakkab muzokaralar va hamkorlik uchun."
            ),
            parameters={
                "type": "object",
                "required": ["message", "agent_id"],
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Agentga yuboriladigan xabar.",
                    },
                    "agent_id": {
                        "type": "string",
                        "enum": list(available_agents.keys()),
                        "description": "Suhbatlashadigan agent.",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": f"Maksimum turlar soni (1-{MAX_PING_PONG_TURNS}, default: 3).",
                    },
                },
            },
            handler=converse_with_agent,
            category="agent",
        )

    registry.register(
        name="view_project_board",
        description=(
            "Loyiha doskasini ko'rish — boshqa agentlar qilgan ishlarni ko'ring. "
            "Agent natijalarini boshqa agentlarga kontekst sifatida berish uchun."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Ixtiyoriy — faqat ma'lum agent natijalarini filtrlash.",
                },
            },
        },
        handler=view_project_board,
        category="agent",
    )

    registry.register(
        name="clear_project_board",
        description="Loyiha doskasini tozalash.",
        parameters={"type": "object", "properties": {}},
        handler=clear_project_board,
        category="agent",
    )

    registry.register(
        name="list_agents",
        description="Mavjud agentlar ro'yxatini ko'rsatish — har birining modeli, roli va imkoniyatlari.",
        parameters={"type": "object", "properties": {}},
        handler=list_agents,
        category="agent",
    )

    registry.register(
        name="agent_session_history",
        description=(
            "Boshqa agentning suhbat tarixini o'qish. "
            "Agent nima qilganini, qanday natijalar olganini ko'rish uchun. "
            "OpenClaw sessions_history ga o'xshash."
        ),
        parameters={
            "type": "object",
            "required": ["agent_id"],
            "properties": {
                "agent_id": {
                    "type": "string",
                    "enum": list(available_agents.keys()),
                    "description": "Tarixini ko'rmoqchi bo'lgan agent.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maksimum xabarlar soni (default: {MAX_SESSION_HISTORY_RETURN}).",
                },
                "include_tools": {
                    "type": "boolean",
                    "description": "Tool natijalarini ham ko'rsatish (default: false).",
                },
            },
        },
        handler=agent_session_history,
        category="agent",
    )

    registry.register(
        name="agent_sessions_list",
        description=(
            "Barcha faol agent sessiyalarini ko'rish — qaysi agentlar ishlagan, "
            "oxirgi faollik vaqti va xabarlar soni. "
            "OpenClaw sessions_list ga o'xshash."
        ),
        parameters={"type": "object", "properties": {}},
        handler=agent_sessions_list,
        category="agent",
    )

    registry.register(
        name="view_agent_activity",
        description=(
            "Agent faoliyat jurnalini ko'rish — qaysi agent kimga vazifa berdi, "
            "natijalar, xatolar, vaqtlar. Real-time monitoring."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maksimum yozuvlar soni (default: 20, max: 50).",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Ixtiyoriy — faqat ma'lum agent faoliyatini ko'rish.",
                },
            },
        },
        handler=view_agent_activity,
        category="agent",
    )

    registry.register(
        name="set_monitor_group",
        description=(
            "Telegram guruhni monitoring uchun sozlash. "
            "Agentlar o'zaro gaplashganda xabarlar shu guruhga yuboriladii — "
            "siz real-time ko'rasiz. Har bir agent bot guruhga qo'shilgan bo'lishi kerak."
        ),
        parameters={
            "type": "object",
            "required": ["group_id"],
            "properties": {
                "group_id": {
                    "type": "integer",
                    "description": "Telegram guruh ID (manfiy raqam, masalan: -1001234567890).",
                },
            },
        },
        handler=set_monitor_group,
        category="agent",
    )
