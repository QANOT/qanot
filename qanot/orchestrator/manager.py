"""Unified sub-agent lifecycle manager.

Replaces tools/delegate.py + tools/subagent.py with a single manager
that handles spawning, tracking, result delivery, and cleanup.

Three execution modes:
- sync: parent blocks until child completes (tool result)
- async: fire-and-forget, result announced via Telegram
- conversation: multi-turn with SAME agent instance (stateful)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Awaitable
from pathlib import Path
from typing import Any, TYPE_CHECKING

from qanot.orchestrator.types import (
    SubagentRun,
    SpawnParams,
    make_run_id,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
    MODE_SYNC,
    MODE_ASYNC,
    MODE_CONVERSATION,
)
from qanot.orchestrator.registry import SubagentRegistry
from qanot.orchestrator.announce import (
    build_announce_payload,
    deliver_async_result,
    post_to_board,
)
from qanot.orchestrator.context_scope import (
    load_agent_identity,
    build_scoped_prompt,
    get_board_summary,
)
from qanot.orchestrator.tool_policy import (
    MAX_SPAWN_DEPTH,
    resolve_role,
    build_child_registry,
)
from qanot.orchestrator.monitor import mirror_to_group, send_typing_to_group

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.providers.base import LLMProvider
    from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Limits
MAX_CONCURRENT_PER_USER = 5
MAX_RESULT_CHARS = 8000
MAX_CONTEXT_CHARS = 4000
MAX_TASK_CHARS = 10000
DEFAULT_TIMEOUT = 120
LONG_TIMEOUT = 300

# Loop detection
LOOP_DETECTION_WINDOW = 5
LOOP_SIMILARITY_THRESHOLD = 0.7

# Built-in agent roles (fallback when no config agents defined)
BUILTIN_ROLES: dict[str, dict[str, str]] = {
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
            "Adapt your tone to the audience."
        ),
    },
}


def _word_set(text: str) -> set[str]:
    """Extract word set for similarity comparison."""
    return set(text.lower().split())


def _task_similarity(a: str, b: str) -> float:
    """Word overlap similarity between two tasks."""
    sa, sb = _word_set(a), _word_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


class SubagentManager:
    """Unified sub-agent lifecycle manager.

    Handles spawning, tracking, result delivery, and cleanup.
    Inspired by OpenClaw's subagent-spawn + subagent-registry + subagent-announce.
    """

    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        parent_registry: ToolRegistry,
        announce_callback: Callable[[int, str], Awaitable[None]] | None = None,
        persist_dir: Path | str | None = None,
    ):
        self.config = config
        self.provider = provider
        self.parent_registry = parent_registry
        self.announce_callback = announce_callback

        persist_path = None
        if persist_dir:
            persist_path = Path(persist_dir) / ".qanot" / "subagent_registry.json"
        self.registry = SubagentRegistry(persist_path)
        self.registry.restore()

        self._project_board: dict[str, list[dict]] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # run_id -> asyncio.Task

    # ── Public API ──────────────────────────────────────────

    async def spawn(
        self,
        params: SpawnParams,
        user_id: str,
        chat_id: int | None = None,
        depth: int = 0,
        caller_agent_id: str = "main",
    ) -> SubagentRun | str:
        """Spawn a sub-agent. Returns SubagentRun on success, error string on failure."""

        # Validate task
        task = params.task.strip()
        if not task:
            return "Error: task is required"
        if len(task) > MAX_TASK_CHARS:
            return f"Error: task too long ({len(task)} chars, max {MAX_TASK_CHARS})"

        # Validate depth
        child_depth = depth + 1
        if child_depth > MAX_SPAWN_DEPTH:
            return f"Error: maximum spawn depth ({MAX_SPAWN_DEPTH}) reached"

        # Validate concurrency
        active = self.registry.count_active_for_user(user_id)
        if active >= MAX_CONCURRENT_PER_USER:
            return f"Error: maximum concurrent agents ({MAX_CONCURRENT_PER_USER}) reached for this user"

        # Resolve agent
        agent_id = params.agent_id or "researcher"
        agent_info = self._resolve_agent(agent_id)
        if agent_info is None:
            available = list(self.get_available_agents().keys())
            return f"Error: unknown agent '{agent_id}'. Available: {available}"

        # Check delegate_allow
        if not self._check_access(caller_agent_id, agent_id):
            return f"Error: agent '{caller_agent_id}' not allowed to delegate to '{agent_id}'"

        # Loop detection: check recent runs for same user + agent + similar task
        loop_msg = self._check_loop(user_id, agent_id, task)
        if loop_msg:
            return loop_msg

        # Resolve role
        role = resolve_role(child_depth)
        can_spawn = child_depth < MAX_SPAWN_DEPTH

        # Create run record
        run = SubagentRun(
            run_id=make_run_id(),
            parent_user_id=user_id,
            parent_chat_id=chat_id,
            task=task,
            agent_id=agent_id,
            agent_name=agent_info["name"],
            role=role,
            depth=child_depth,
            status=STATUS_PENDING,
            mode=params.mode,
            model=params.model or agent_info.get("model", ""),
            timeout=params.timeout or agent_info.get("timeout", DEFAULT_TIMEOUT),
            max_iterations=params.max_iterations,
        )
        await self.registry.register(run)

        # Build child agent
        child_registry = build_child_registry(
            self.parent_registry,
            depth=child_depth,
            tools_allow=params.tools_allow or agent_info.get("tools_allow"),
            tools_deny=params.tools_deny or agent_info.get("tools_deny"),
        )

        # Register orchestrator tools on child if it can spawn
        if can_spawn:
            from qanot.orchestrator.tools import register_orchestrator_tools
            register_orchestrator_tools(
                child_registry, self, self.config, depth=child_depth,
            )

        # Build prompt
        identity = load_agent_identity(self.config.workspace_dir, agent_id)
        agent_prompt = identity or agent_info["prompt"]
        board_summary = get_board_summary(
            self._project_board.get(user_id, []), exclude_agent=agent_id,
        )
        prompt = build_scoped_prompt(
            task=task,
            agent_identity=agent_prompt,
            parent_context=params.context[:MAX_CONTEXT_CHARS] if params.context else "",
            board_summary=board_summary,
            can_spawn=can_spawn,
            agent_id=agent_id,
        )

        # Create provider
        child_provider = self._create_provider(agent_info)

        # Create agent
        agent = self._create_agent(
            child_registry, child_provider, run,
        )

        # Execute based on mode
        if params.mode == MODE_SYNC:
            await self._run_sync(run, agent, prompt)
        elif params.mode == MODE_ASYNC:
            self._run_async_task(run, agent, prompt)
        elif params.mode == MODE_CONVERSATION:
            await self._run_conversation(run, agent, prompt, params.max_turns)
        else:
            return f"Error: unknown mode '{params.mode}'"

        return run

    async def cancel(self, run_id: str) -> bool:
        """Cancel a running sub-agent."""
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await self.registry.update(
                run_id,
                status=STATUS_CANCELLED,
                ended_at=time.time(),
                error="Cancelled by user",
            )
            self._tasks.pop(run_id, None)
            logger.info("Cancelled sub-agent %s", run_id)
            return True

        # Even if no task, mark as cancelled if still active
        run = self.registry.get(run_id)
        if run and not run.is_terminal:
            await self.registry.update(
                run_id,
                status=STATUS_CANCELLED,
                ended_at=time.time(),
                error="Cancelled by user",
            )
            return True
        return False

    async def cancel_all_for_user(self, user_id: str) -> int:
        """Cancel all active sub-agents for a user. Returns count cancelled."""
        active = self.registry.get_active_for_user(user_id)
        count = 0
        for run in active:
            if await self.cancel(run.run_id):
                count += 1
        if count:
            logger.info("Cancelled %d sub-agents for user %s", count, user_id)
        return count

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel all running tasks and wait."""
        all_tasks = list(self._tasks.values())
        for t in all_tasks:
            if not t.done():
                t.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._tasks.clear()
        await self.registry.persist()
        logger.info("SubagentManager shut down (%d tasks cancelled)", len(all_tasks))

    def get_board(self, user_id: str) -> list[dict]:
        return self._project_board.get(user_id, [])

    def clear_board(self, user_id: str) -> None:
        self._project_board.pop(user_id, None)

    def get_available_agents(self) -> dict[str, dict]:
        """Get all available agents (builtin + config)."""
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
                "timeout": DEFAULT_TIMEOUT,
                "source": "builtin",
            }

        for agent_def in self.config.agents:
            agents[agent_def.id] = {
                "name": agent_def.name or agent_def.id,
                "prompt": agent_def.prompt or f"You are the {agent_def.id} agent.",
                "model": agent_def.model,
                "provider": agent_def.provider,
                "api_key": agent_def.api_key,
                "tools_allow": agent_def.tools_allow,
                "tools_deny": agent_def.tools_deny,
                "timeout": agent_def.timeout or DEFAULT_TIMEOUT,
                "source": "config",
            }

        return agents

    # ── Execution modes ─────────────────────────────────────

    async def _run_sync(self, run: SubagentRun, agent: Any, prompt: str) -> None:
        """Synchronous: parent blocks until child completes."""
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        await self.registry.update(run.run_id, status=run.status, started_at=run.started_at)

        # Mirror to monitoring group
        await mirror_to_group(
            self.config, "main", run.agent_id,
            run.task[:3000], direction="delegate",
        )
        await send_typing_to_group(self.config, run.agent_id)

        try:
            result = await asyncio.wait_for(
                agent.run_turn(prompt, user_id=run.parent_user_id),
                timeout=run.timeout,
            )
            run.status = STATUS_COMPLETED
            run.result_text = result
        except asyncio.TimeoutError:
            run.status = STATUS_TIMEOUT
            run.result_text = f"(Agent timed out after {run.timeout}s)"
            run.error = f"Timeout after {run.timeout}s"
        except asyncio.CancelledError:
            run.status = STATUS_CANCELLED
            run.result_text = "(Cancelled)"
            run.error = "Cancelled"
            # Don't re-raise — let the tool handler return a result
        except Exception as e:
            run.status = STATUS_FAILED
            run.result_text = f"(Agent failed: {e})"
            run.error = str(e)[:500]
            logger.error("Sync agent '%s' failed: %s", run.agent_id, e)
        finally:
            run.ended_at = time.time()
            self._collect_stats(run, agent)
            await self._finalize_run(run)
            # Mirror result to monitoring group
            await mirror_to_group(
                self.config, run.agent_id, "main",
                (run.result_text or "")[:3000], direction="result",
            )

    def _run_async_task(self, run: SubagentRun, agent: Any, prompt: str) -> None:
        """Async: fire and announce on completion."""
        async def _run():
            run.status = STATUS_RUNNING
            run.started_at = time.time()
            await self.registry.update(run.run_id, status=run.status, started_at=run.started_at)

            try:
                result = await asyncio.wait_for(
                    agent.run_turn(prompt, user_id=run.parent_user_id),
                    timeout=run.timeout,
                )
                run.status = STATUS_COMPLETED
                run.result_text = result
            except asyncio.TimeoutError:
                run.status = STATUS_TIMEOUT
                run.result_text = f"(Agent timed out after {run.timeout}s)"
                run.error = f"Timeout after {run.timeout}s"
            except asyncio.CancelledError:
                run.status = STATUS_CANCELLED
                run.error = "Cancelled"
            except Exception as e:
                run.status = STATUS_FAILED
                run.result_text = f"(Agent failed: {e})"
                run.error = str(e)[:500]
                logger.error("Async agent '%s' failed: %s", run.agent_id, e)
            finally:
                run.ended_at = time.time()
                self._collect_stats(run, agent)
                await self._finalize_run(run)
                self._tasks.pop(run.run_id, None)

            # Announce result (including cancellation/failure)
            try:
                await deliver_async_result(run, self.announce_callback, self._project_board)
            except Exception as e:
                logger.error("Failed to deliver async result: %s", e)

            logger.info(
                "Async agent '%s' %s in %.1fs",
                run.agent_id, run.status, run.elapsed_seconds,
            )

        task = asyncio.create_task(_run(), name=f"subagent_{run.run_id}")
        # Ensure exceptions are logged even if task is never awaited
        task.add_done_callback(self._task_done_callback)
        self._tasks[run.run_id] = task

    async def _run_conversation(
        self,
        run: SubagentRun,
        agent: Any,
        initial_prompt: str,
        max_turns: int,
    ) -> None:
        """Conversation: multi-turn with SAME agent instance (stateful).

        Unlike the old ping-pong which created N separate Agents,
        this uses one Agent and calls run_turn() multiple times.
        The agent retains conversation history across turns.

        Timeout applies to the ENTIRE conversation, not per-turn.
        """
        run.status = STATUS_RUNNING
        run.started_at = time.time()
        await self.registry.update(run.run_id, status=run.status, started_at=run.started_at)

        conversation_log: list[dict] = []
        final_result = ""

        async def _conversation_loop() -> str:
            nonlocal final_result
            # First turn: send the full scoped prompt
            result = await agent.run_turn(initial_prompt, user_id=run.parent_user_id)
            conversation_log.append({"role": "user", "message": run.task})
            conversation_log.append({"role": "agent", "message": result})
            final_result = result

            # Subsequent turns
            for turn in range(2, max_turns + 1):
                if self._is_conversation_done(result):
                    break

                followup = (
                    f"Continue your work. This is turn {turn}/{max_turns}. "
                    f"If you have completed the task, start your response with RESULT: "
                    f"followed by your final answer."
                )
                result = await agent.run_turn(followup, user_id=run.parent_user_id)
                conversation_log.append({"role": "user", "message": followup})
                conversation_log.append({"role": "agent", "message": result})
                final_result = result

            return final_result

        try:
            # Total timeout wraps the entire conversation
            total_timeout = run.timeout * max_turns
            final_result = await asyncio.wait_for(_conversation_loop(), timeout=total_timeout)
            run.status = STATUS_COMPLETED
            run.result_text = final_result
        except asyncio.TimeoutError:
            run.status = STATUS_TIMEOUT
            run.result_text = final_result or f"(Conversation timed out)"
            run.error = f"Timeout after {run.timeout * max_turns}s"
        except asyncio.CancelledError:
            run.status = STATUS_CANCELLED
            run.result_text = final_result or "(Cancelled)"
            run.error = "Cancelled"
        except Exception as e:
            run.status = STATUS_FAILED
            run.result_text = final_result or f"(Conversation failed: {e})"
            run.error = str(e)[:500]
            logger.error("Conversation agent '%s' failed: %s", run.agent_id, e)
        finally:
            run.ended_at = time.time()
            self._collect_stats(run, agent)
            await self._finalize_run(run)

        logger.info(
            "Conversation agent '%s' %s in %.1fs (%d turns)",
            run.agent_id, run.status, run.elapsed_seconds, len(conversation_log) // 2,
        )

    # ── Internal helpers ────────────────────────────────────

    async def _finalize_run(self, run: SubagentRun) -> None:
        """Update registry and post to board after run completion."""
        await self.registry.update(
            run.run_id,
            status=run.status,
            ended_at=run.ended_at,
            result_text=run.result_text,
            error=run.error,
            token_input=run.token_input,
            token_output=run.token_output,
            cost=run.cost,
        )
        payload = build_announce_payload(run)
        # Set task on the board entry (announce payload doesn't carry it)
        post_to_board(self._project_board, run.parent_user_id, payload, task=run.task)

    def _check_loop(self, user_id: str, agent_id: str, task: str) -> str | None:
        """Detect delegation loops: same agent + similar task recently."""
        recent = self.registry.get_recent_for_user(user_id, limit=LOOP_DETECTION_WINDOW * 2)
        same_agent_recent = [
            r for r in recent
            if r.agent_id == agent_id and not r.is_terminal
        ]
        if len(same_agent_recent) >= 3:
            return (
                f"Error: loop detected — agent '{agent_id}' already has "
                f"{len(same_agent_recent)} active runs for this user"
            )

        # Check for similar completed tasks (ping-pong detection)
        completed = [r for r in recent if r.agent_id == agent_id and r.status == STATUS_COMPLETED]
        for prev in completed[-LOOP_DETECTION_WINDOW:]:
            sim = _task_similarity(task, prev.task)
            if sim >= LOOP_SIMILARITY_THRESHOLD:
                return (
                    f"Error: loop detected — very similar task recently completed by '{agent_id}' "
                    f"(similarity: {sim:.0%}). Rephrase or use a different approach."
                )

        return None

    def _resolve_agent(self, agent_id: str) -> dict | None:
        """Resolve agent config by ID (config agents take precedence)."""
        agents = self.get_available_agents()
        return agents.get(agent_id)

    def _check_access(self, caller_id: str, target_id: str) -> bool:
        """Check delegate_allow access control."""
        if caller_id == "main":
            return True

        for agent_def in self.config.agents:
            if agent_def.id == caller_id:
                if not agent_def.delegate_allow:
                    return True
                return target_id in agent_def.delegate_allow
        return True

    def _create_provider(self, agent_info: dict) -> LLMProvider:
        """Create provider for child agent (reuse main if no overrides)."""
        agent_model = agent_info.get("model", "")
        agent_provider = agent_info.get("provider", "")
        agent_api_key = agent_info.get("api_key", "")

        if not agent_model and not agent_provider:
            return self.provider

        from qanot.providers.failover import ProviderProfile, _create_single_provider

        profile = ProviderProfile(
            name=f"subagent_{agent_info.get('name', 'agent')}",
            provider_type=agent_provider or self.config.provider,
            api_key=agent_api_key or self.config.api_key,
            model=agent_model or self.config.model,
        )
        return _create_single_provider(profile)

    def _create_agent(
        self,
        child_registry: ToolRegistry,
        child_provider: LLMProvider,
        run: SubagentRun,
    ) -> Any:
        """Create an isolated Agent instance for a sub-agent run."""
        from qanot.agent import Agent
        from qanot.context import ContextTracker
        from qanot.session import SessionWriter

        session = SessionWriter(self.config.sessions_dir)
        session.new_session(f"subagent_{run.agent_id}_{run.run_id}")

        context = ContextTracker(
            max_tokens=self.config.max_context_tokens,
            workspace_dir=self.config.workspace_dir,
        )

        agent = Agent(
            config=self.config,
            provider=child_provider,
            tool_registry=child_registry,
            session=session,
            context=context,
            prompt_mode="minimal",
            _is_child=True,
            max_iterations=run.max_iterations,
        )
        return agent

    def _collect_stats(self, run: SubagentRun, agent: Any) -> None:
        """Collect token usage and cost from agent's CostTracker."""
        try:
            ct = getattr(agent, 'cost_tracker', None)
            if ct is None:
                return
            # CostTracker stores per-user stats; child agents serve one user
            stats = ct.get_user_stats(run.parent_user_id)
            run.token_input = stats.get("input_tokens", 0)
            run.token_output = stats.get("output_tokens", 0)
            run.cost = stats.get("total_cost", 0.0)
        except Exception as e:
            logger.debug("Failed to collect sub-agent cost stats: %s", e)

    @staticmethod
    def _task_done_callback(task: asyncio.Task) -> None:
        """Log unhandled exceptions from fire-and-forget tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Unhandled sub-agent task exception: %s", exc, exc_info=exc)

    @staticmethod
    def _is_conversation_done(response: str) -> bool:
        """Check if agent signaled conversation completion."""
        stripped = response.strip()
        if stripped.upper().startswith("RESULT:"):
            return True
        if stripped.upper().startswith("DONE"):
            return True
        return False
