"""Core agent loop — the heart of Qanot AI."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from qanot.config import Config
from qanot.context import ContextTracker
from qanot.memory import wal_scan, wal_write, write_daily_note
from qanot.prompt import build_system_prompt
from qanot.providers.base import LLMProvider, ProviderResponse, ToolCall
from qanot.session import SessionWriter

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 25


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable[[dict], Awaitable[str]]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict], Awaitable[str]],
    ) -> None:
        """Register a tool with its handler."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "input_schema": parameters,
        }
        self._handlers[name] = handler

    def get_definitions(self) -> list[dict]:
        """Get tool definitions for the LLM."""
        return list(self._tools.values())

    async def execute(self, name: str, input_data: dict) -> str:
        """Execute a tool by name."""
        handler = self._handlers.get(name)
        if not handler:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            result = await handler(input_data)
            return result
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e)
            return json.dumps({"error": str(e)})

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


class Agent:
    """Core agent that runs the tool_use loop."""

    def __init__(
        self,
        config: Config,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        session: SessionWriter | None = None,
        context: ContextTracker | None = None,
    ):
        self.config = config
        self.provider = provider
        self.tools = tool_registry
        self.session = session or SessionWriter(config.sessions_dir)
        self.context = context or ContextTracker(
            max_tokens=config.max_context_tokens,
            workspace_dir=config.workspace_dir,
        )
        self.messages: list[dict] = []
        self._last_user_msg_id = ""

    def _build_system_prompt(self) -> str:
        """Build the system prompt from workspace files."""
        return build_system_prompt(
            workspace_dir=self.config.workspace_dir,
            owner_name=self.config.owner_name,
            bot_name=self.config.bot_name,
            timezone_str=self.config.timezone,
            context_percent=self.context.get_context_percent(),
            total_tokens=self.context.total_tokens,
        )

    async def run_turn(self, user_message: str) -> str:
        """Process a user message through the agent loop.

        Returns the final text response.
        """
        # WAL Protocol: scan user message BEFORE generating response
        wal_entries = wal_scan(user_message)
        if wal_entries:
            wal_write(wal_entries, self.config.workspace_dir)
            logger.debug("WAL: wrote %d entries before responding", len(wal_entries))

        # Check for compaction recovery
        if self.context.detect_compaction(self.messages):
            recovery = self.context.recover_from_compaction()
            if recovery:
                user_message = f"{user_message}\n\n---\n\n[COMPACTION RECOVERY]\n{recovery}"
                logger.info("Compaction recovery injected")

        # Add user message to conversation
        self.messages.append({
            "role": "user",
            "content": user_message,
        })

        # Log to session
        self._last_user_msg_id = self.session.log_user_message(user_message)

        # Agent loop
        final_text = ""
        for iteration in range(MAX_ITERATIONS):
            system = self._build_system_prompt()
            tool_defs = self.tools.get_definitions()

            # Call LLM
            response = await self.provider.chat(
                messages=self.messages,
                tools=tool_defs if tool_defs else None,
                system=system,
            )

            # Track usage
            self.context.add_usage(
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            # Check 60% threshold
            if self.context.check_threshold():
                logger.warning("Context at %.1f%% — Working Buffer activated",
                             self.context.get_context_percent())

            if response.stop_reason == "tool_use" and response.tool_calls:
                # Build assistant message with text + tool_use blocks
                assistant_content: list[dict] = []
                if response.content:
                    assistant_content.append({"type": "text", "text": response.content})
                for tc in response.tool_calls:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })

                self.messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                })

                # Log tool uses
                tool_use_dicts = [{"name": tc.name, "input": tc.input} for tc in response.tool_calls]
                self.session.log_assistant_message(
                    text=response.content,
                    tool_uses=tool_use_dicts,
                    usage=response.usage,
                    parent_id=self._last_user_msg_id,
                    model=self.provider.model,
                )

                # Execute tools and collect results
                tool_results: list[dict] = []
                for tc in response.tool_calls:
                    logger.info("Executing tool: %s", tc.name)
                    result = await self.tools.execute(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })

                # Add tool results as user message
                self.messages.append({
                    "role": "user",
                    "content": tool_results,
                })

            elif response.stop_reason == "end_turn":
                final_text = response.content

                # Add assistant response to messages
                self.messages.append({
                    "role": "assistant",
                    "content": response.content,
                })

                # Log final response
                self.session.log_assistant_message(
                    text=response.content,
                    usage=response.usage,
                    parent_id=self._last_user_msg_id,
                    model=self.provider.model,
                )

                # Write to working buffer if active
                if self.context.buffer_active:
                    summary = final_text[:200] + "..." if len(final_text) > 200 else final_text
                    self.context.append_to_buffer(user_message, summary)

                # Write daily note
                write_daily_note(
                    f"**User:** {user_message[:100]}...\n**Agent:** {final_text[:200]}...",
                    self.config.workspace_dir,
                )

                break
            else:
                # Unknown stop reason
                final_text = response.content or "(No response)"
                self.messages.append({
                    "role": "assistant",
                    "content": final_text,
                })
                break
        else:
            final_text = "(Agent reached maximum iterations)"
            logger.warning("Agent hit max iterations (%d)", MAX_ITERATIONS)

        return final_text

    def reset(self) -> None:
        """Reset conversation state."""
        self.messages.clear()


async def spawn_isolated_agent(
    config: Config,
    provider: LLMProvider,
    tool_registry: ToolRegistry,
    prompt: str,
    session_id: str | None = None,
) -> str:
    """Spawn an isolated agent that runs independently.

    Used for cron jobs and background tasks.
    Returns the agent's final response.
    """
    session = SessionWriter(config.sessions_dir)
    if session_id:
        session.new_session(session_id)

    context = ContextTracker(
        max_tokens=config.max_context_tokens,
        workspace_dir=config.workspace_dir,
    )

    agent = Agent(
        config=config,
        provider=provider,
        tool_registry=tool_registry,
        session=session,
        context=context,
    )

    result = await agent.run_turn(prompt)
    return result
