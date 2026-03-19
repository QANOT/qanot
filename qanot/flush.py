"""Pre-compaction memory flush and compaction orchestration."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qanot.config import Config
    from qanot.context import ContextTracker
    from qanot.conversation import ConversationManager
    from qanot.providers.base import LLMProvider
    from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)

COMPACTION_SUMMARY_PROMPT = (
    "You are summarizing a conversation for context compaction. "
    "Create a concise summary that preserves:\n"
    "1. **Key decisions** made during the conversation\n"
    "2. **Open tasks/TODOs** that are still pending\n"
    "3. **Important facts** (names, numbers, IDs, URLs, file paths)\n"
    "4. **User preferences** expressed during the conversation\n"
    "5. **Current goal** — what the user is trying to accomplish\n\n"
    "Be concise but preserve all actionable information. "
    "Do NOT add commentary — just summarize the facts.\n\n"
    "---\n\n"
    "Conversation to summarize:\n\n"
)

MEMORY_FLUSH_PROMPT = (
    "Pre-compaction memory flush. Context is about to be compacted and older messages will be lost.\n\n"
    "Save any durable memories to files using write_file tool:\n"
    "- Save to `memory/{date}.md` (append, don't overwrite) for daily logs\n"
    "- Save to `MEMORY.md` for long-term curated facts\n\n"
    "What to save:\n"
    "- User's name, preferences, important personal info\n"
    "- Decisions made during this conversation\n"
    "- Project context, URLs, IDs, paths that might be needed later\n"
    "- Lessons learned, mistakes to avoid\n"
    "- Things the user explicitly asked to remember\n\n"
    "What NOT to save:\n"
    "- Routine greetings or small talk\n"
    "- Information already in MEMORY.md or USER.md\n"
    "- Temporary debugging details\n\n"
    "If nothing worth saving, reply with just: NO_SAVE\n"
    "Be concise. Append to existing files, never overwrite."
)

# Only allow read/write tools during memory flush (no shell, no web)
MEMORY_FLUSH_TOOL_NAMES = {"read_file", "write_file", "list_files", "memory_search"}


FLUSH_MODEL = "claude-haiku-4-5-20251001"
FLUSH_MAX_ITERS = 2
FLUSH_RECENT_MESSAGES = 20  # Only send last N messages (not entire conversation)


async def memory_flush(
    messages: list[dict],
    provider: LLMProvider,
    tools: ToolRegistry,
    build_system_prompt: callable,
    build_assistant_tool_message: callable,
) -> None:
    """Run a hidden LLM turn to save durable memories before compaction.

    Like OpenClaw's pre-compaction flush: gives the agent a chance to
    write important facts to memory files before context is lost.
    Only read/write tools are available during flush.

    Cost optimization: uses Haiku model with only recent messages
    instead of the full conversation with the primary model.
    """
    if len(messages) < 4:
        return  # Too little context to flush

    # Build a condensed view of the conversation for the flush prompt
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    flush_prompt = MEMORY_FLUSH_PROMPT.replace("{date}", today)

    # Filter tools to only safe ones (read/write)
    flush_tools = [
        t for t in tools.get_definitions()
        if t.get("name") in MEMORY_FLUSH_TOOL_NAMES
    ]

    if not flush_tools:
        logger.warning("No flush tools available, skipping memory flush")
        return

    # Use cheap model for flush (Haiku instead of Sonnet/Opus)
    original_model = provider.model
    try:
        provider.model = FLUSH_MODEL
    except (AttributeError, TypeError):
        pass  # Provider doesn't support model override — use original

    try:
        # Only send recent messages to save tokens (older context is less relevant)
        recent = messages[-FLUSH_RECENT_MESSAGES:] if len(messages) > FLUSH_RECENT_MESSAGES else list(messages)
        # Ensure first message is from user (API requirement)
        if recent and recent[0].get("role") != "user":
            # Trim leading non-user messages so the API receives user-first turn ordering
            start = next((i for i, m in enumerate(recent) if m.get("role") == "user"), len(recent))
            recent = recent[start:]
        flush_messages = recent
        flush_messages.append({"role": "user", "content": flush_prompt})

        for _ in range(FLUSH_MAX_ITERS):
            response = await provider.chat(
                messages=flush_messages,
                tools=flush_tools,
                system=build_system_prompt(),
            )

            if response.stop_reason == "tool_use" and response.tool_calls:
                # Execute only allowed tools
                flush_messages.append(
                    build_assistant_tool_message(response.content, response.tool_calls)
                )
                tool_results: list[dict] = []
                for tc in response.tool_calls:
                    if tc.name in MEMORY_FLUSH_TOOL_NAMES:
                        result = await tools.execute(tc.name, tc.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": result,
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": "Tool not available during memory flush.",
                        })
                flush_messages.append({"role": "user", "content": tool_results})
            else:
                # end_turn or NO_SAVE -- done
                if response.content and "NO_SAVE" not in response.content:
                    logger.info("Memory flush completed with text response")
                break

        logger.info("Pre-compaction memory flush completed (model=%s, iters=%d)",
                     provider.model, FLUSH_MAX_ITERS)

    except Exception as e:
        logger.warning("Memory flush failed (non-fatal): %s", e)
    finally:
        # Restore original model
        try:
            provider.model = original_model
        except (AttributeError, TypeError):
            pass


async def summarize_for_compaction(
    messages: list[dict],
    provider: LLMProvider,
    config: Config,
    context: ContextTracker,
) -> str | None:
    """Use multi-stage LLM summarization for compaction (OpenClaw-style).

    Returns summary text, or None if summarization fails (falls back to truncation).
    """
    from pathlib import Path
    from qanot.compaction import summarize_in_stages, estimate_messages_tokens

    if config.compaction_mode == "truncate":
        return None

    # Pre-compaction backup: save full context before it's dropped
    try:
        text_to_summarize = context.extract_compaction_text(messages)
        if text_to_summarize and len(text_to_summarize) > 100:
            backup_dir = Path(config.workspace_dir) / "memory"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"pre-compact-{int(time.time())}.md"
            backup_path.write_text(
                f"# Pre-Compaction Backup\n\n{text_to_summarize}",
                encoding="utf-8",
            )
            logger.info("Pre-compaction backup saved: %s", backup_path.name)
    except Exception as e:
        logger.warning("Failed to save pre-compaction backup: %s", e)

    # Extract messages to summarize (middle section)
    if len(messages) <= 6:
        return None

    keep_recent = min(4, len(messages) // 2)
    middle = messages[2:-keep_recent]
    if not middle:
        return None

    total_tokens = estimate_messages_tokens(middle)
    logger.info(
        "Multi-stage compaction: %d messages (~%d tokens)",
        len(middle), total_tokens,
    )

    # Determine number of parts based on size
    parts = 2 if total_tokens < 50_000 else 3

    try:
        summary = await summarize_in_stages(
            provider=provider,
            messages=middle,
            context_window=config.max_context_tokens,
            parts=parts,
        )
        if summary and len(summary) > 20:
            logger.info("Multi-stage compaction summary: %d chars", len(summary))
            return summary
    except Exception as e:
        logger.warning("Multi-stage compaction failed, falling back to truncation: %s", e)

    return None


async def handle_overflow(
    messages: list[dict],
    provider: LLMProvider,
    tools: ToolRegistry,
    config: Config,
    context: ContextTracker,
    conv_manager: ConversationManager,
    user_id: str | None,
    build_system_prompt: callable,
    build_assistant_tool_message: callable,
) -> list[dict]:
    """Handle context overflow by force-compacting the conversation.

    Called reactively when the API returns a context_overflow error.
    """
    logger.warning("Context overflow detected — forcing compaction")
    await memory_flush(messages, provider, tools, build_system_prompt, build_assistant_tool_message)
    summary = await summarize_for_compaction(messages, provider, config, context)
    compacted = context.compact_messages(messages, summary_text=summary)
    conv_manager.set_messages(user_id, compacted)
    return compacted
