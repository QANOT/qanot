"""Multi-stage compaction — OpenClaw-style chunked summarization with fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qanot.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# ── Constants ──

# 40% of context per chunk (base), reduced for large messages
BASE_CHUNK_RATIO = 0.40
MIN_CHUNK_RATIO = 0.15
# Safety margin for token estimation inaccuracy (~20%)
SAFETY_MARGIN = 1.2
# Tokens reserved for summarization overhead (prompt + response)
SUMMARIZATION_OVERHEAD = 4096
# Default number of parts to split into
DEFAULT_PARTS = 2
# Minimum messages to trigger multi-stage
MIN_MESSAGES_FOR_SPLIT = 4
# Approximate chars-per-token for estimation
CHARS_PER_TOKEN = 4

SUMMARIZATION_SYSTEM = (
    "You are a concise summarizer. Output only the summary, nothing else. "
    "Preserve all opaque identifiers exactly as written (no shortening or reconstruction), "
    "including UUIDs, hashes, IDs, tokens, hostnames, IPs, ports, URLs, and file names."
)

SUMMARIZATION_PROMPT = (
    "Summarize this conversation segment concisely. Preserve:\n"
    "1. Key decisions and their rationale\n"
    "2. Active tasks and their current status\n"
    "3. Important facts (names, numbers, IDs, URLs, file paths)\n"
    "4. User preferences and corrections\n"
    "5. Current goal — what the user is trying to accomplish\n"
    "6. Any commitments or follow-ups promised\n\n"
    "Be concise but preserve all actionable information.\n\n"
    "---\n\n"
)

MERGE_PROMPT = (
    "Merge these partial summaries into a single cohesive summary.\n\n"
    "MUST PRESERVE:\n"
    "- Active tasks and their current status (in-progress, blocked, pending)\n"
    "- The last thing the user requested and what was being done about it\n"
    "- Decisions made and their rationale\n"
    "- TODOs, open questions, and constraints\n"
    "- Any commitments or follow-ups promised\n"
    "- Names, IDs, URLs, file paths\n\n"
    "PRIORITIZE recent context over older history. The agent needs to know "
    "what it was doing, not just what was discussed.\n\n"
    "---\n\n"
    "Partial summaries to merge:\n\n"
)


# ── Token estimation ──

def estimate_tokens(text: str) -> int:
    """Estimate token count from text length. ~4 chars per token for English/Uzbek."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_message_tokens(msg: dict) -> int:
    """Estimate tokens for a single message."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return estimate_tokens(content) + 4  # role overhead
    if isinstance(content, list):
        total = 4  # role overhead
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    total += estimate_tokens(str(block.get("input", {}))) + 20
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    # Cap tool result estimation (they can be huge)
                    total += min(estimate_tokens(result), 500) + 10
        return total
    return estimate_tokens(str(content)) + 4


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a message list."""
    return sum(estimate_message_tokens(m) for m in messages)


# ── Message utilities ──

def strip_tool_result_details(messages: list[dict]) -> list[dict]:
    """Strip verbose tool result content before summarization.

    Replaces large tool results with truncated versions to prevent
    feeding untrusted/verbose payloads into the summarization LLM.
    """
    result = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            new_content = []
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if len(text) > 300:
                        text = text[:300] + "... [truncated]"
                    new_content.append({**block, "content": text})
                else:
                    new_content.append(block)
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)
    return result


def messages_to_text(messages: list[dict]) -> str:
    """Convert messages to text for summarization."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[tool: {block.get('name', '?')}]")
                    elif block.get("type") == "tool_result":
                        r = block.get("content", "")
                        if len(r) > 200:
                            r = r[:200] + "..."
                        text_parts.append(f"[result: {r}]")
            text = "\n".join(text_parts)
        else:
            text = str(content)

        if text.strip():
            # Sanitize: remove embedded role markers that could act as prompt injection
            sanitized = text[:8000]
            for marker in ("**system**:", "**System**:", "<|system|>", "<|im_start|>system",
                           "[INST]", "<<SYS>>", "</s>", "<|endoftext|>"):
                sanitized = sanitized.replace(marker, f"[blocked:{marker[:8]}]")
            parts.append(f"**{role}**: {sanitized}")

    return "\n\n".join(parts)


# ── Splitting ──

def _chunk_messages(
    messages: list[dict],
    should_flush: "callable",
    flush_oversized: bool = False,
    max_chunks: int | None = None,
) -> list[list[dict]]:
    """Generic chunking helper: split messages at boundaries determined by should_flush.

    Args:
        messages: Messages to chunk.
        should_flush: Callable(current_tokens, msg_tokens, chunks_so_far) -> bool.
        flush_oversized: If True, flush immediately after adding an oversized message.
        max_chunks: If set, stop creating new chunks after this many (last chunk gets the rest).
    """
    if not messages:
        return []

    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0

    for msg in messages:
        msg_tokens = estimate_message_tokens(msg)

        can_split = max_chunks is None or len(chunks) < max_chunks - 1
        if can_split and current and should_flush(current_tokens, msg_tokens, len(chunks)):
            chunks.append(current)
            current = []
            current_tokens = 0

        current.append(msg)
        current_tokens += msg_tokens

        if flush_oversized and should_flush(0, msg_tokens, len(chunks)):
            chunks.append(current)
            current = []
            current_tokens = 0

    if current:
        chunks.append(current)

    return chunks


def split_messages_by_token_share(
    messages: list[dict],
    parts: int = DEFAULT_PARTS,
    _precomputed_total: int | None = None,
) -> list[list[dict]]:
    """Split messages into roughly equal-sized token chunks.

    Keeps message boundaries intact (never splits a single message).

    Args:
        _precomputed_total: If provided, skip re-estimating total tokens.
    """
    if not messages:
        return []

    parts = min(parts, len(messages))
    if parts <= 1:
        return [messages]

    # Precompute per-message token counts to avoid double estimation
    msg_token_cache: dict[int, int] = {}
    if _precomputed_total is not None:
        total_tokens = _precomputed_total
    else:
        total_tokens = 0
        for i, msg in enumerate(messages):
            t = estimate_message_tokens(msg)
            msg_token_cache[id(msg)] = t
            total_tokens += t

    target_tokens = total_tokens / parts

    # Use cached token counts in chunking if available
    if msg_token_cache:
        chunks: list[list[dict]] = []
        current: list[dict] = []
        current_tokens = 0

        for msg in messages:
            msg_tokens = msg_token_cache.get(id(msg), estimate_message_tokens(msg))
            can_split = len(chunks) < parts - 1
            if can_split and current and current_tokens + msg_tokens > target_tokens:
                chunks.append(current)
                current = []
                current_tokens = 0
            current.append(msg)
            current_tokens += msg_tokens

        if current:
            chunks.append(current)
        return chunks

    return _chunk_messages(
        messages,
        should_flush=lambda cur_tokens, msg_tokens, _n: cur_tokens + msg_tokens > target_tokens,
        max_chunks=parts,
    )


def chunk_messages_by_max_tokens(messages: list[dict], max_tokens: int) -> list[list[dict]]:
    """Chunk messages so each chunk fits within max_tokens."""
    if not messages:
        return []

    effective_max = max(1, int(max_tokens / SAFETY_MARGIN))

    return _chunk_messages(
        messages,
        should_flush=lambda cur_tokens, msg_tokens, _n: cur_tokens + msg_tokens > effective_max,
        flush_oversized=True,
    )


def compute_adaptive_chunk_ratio(messages: list[dict], context_window: int) -> float:
    """Reduce chunk ratio when messages are individually large."""
    if not messages or context_window <= 0:
        return BASE_CHUNK_RATIO

    total_tokens = estimate_messages_tokens(messages)
    avg_tokens = total_tokens / len(messages)
    safe_avg = avg_tokens * SAFETY_MARGIN
    avg_ratio = safe_avg / context_window

    if avg_ratio > 0.1:
        reduction = min(avg_ratio * 2, BASE_CHUNK_RATIO - MIN_CHUNK_RATIO)
        return max(MIN_CHUNK_RATIO, BASE_CHUNK_RATIO - reduction)

    return BASE_CHUNK_RATIO


def is_oversized_for_summary(msg: dict, context_window: int) -> bool:
    """Check if a message is too large (>50% context) for summarization."""
    tokens = estimate_message_tokens(msg) * SAFETY_MARGIN
    return tokens > context_window * 0.5


# ── Core summarization ──

async def _generate_summary(
    provider: LLMProvider,
    text: str,
    previous_summary: str | None = None,
) -> str:
    """Generate a summary using the LLM."""
    prompt = SUMMARIZATION_PROMPT
    if previous_summary:
        prompt += f"Previous summary (for context):\n{previous_summary}\n\n---\n\nNew segment:\n"
    prompt += text

    # Cap input to prevent overflow
    if len(prompt) > 48_000:
        prompt = prompt[:48_000] + "\n\n[... truncated for summarization]"

    response = await provider.chat(
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        system=SUMMARIZATION_SYSTEM,
    )
    return response.content.strip()


async def summarize_chunks(
    provider: LLMProvider,
    messages: list[dict],
    max_chunk_tokens: int,
    previous_summary: str | None = None,
) -> str:
    """Summarize messages in chunks, accumulating summaries."""
    if not messages:
        return previous_summary or ""

    safe_messages = strip_tool_result_details(messages)
    chunks = chunk_messages_by_max_tokens(safe_messages, max_chunk_tokens)

    summary = previous_summary
    for chunk in chunks:
        text = messages_to_text(chunk)
        if not text.strip():
            continue

        # Retry up to 3 times
        for attempt in range(3):
            try:
                summary = await _generate_summary(provider, text, summary)
                break
            except Exception as e:
                logger.warning("Summarization attempt %d failed: %s", attempt + 1, e)
                if attempt == 2:
                    raise
                await asyncio.sleep(min(2 ** attempt, 5))

    return summary or ""


async def summarize_with_fallback(
    provider: LLMProvider,
    messages: list[dict],
    max_chunk_tokens: int,
    context_window: int,
    previous_summary: str | None = None,
) -> str:
    """Summarize with graceful fallback for oversized messages.

    Three-tier fallback:
    1. Full summarization
    2. Partial (exclude oversized messages) + notes
    3. Human-readable metadata
    """
    if not messages:
        return previous_summary or ""

    # Tier 1: Try full summarization
    try:
        return await summarize_chunks(provider, messages, max_chunk_tokens, previous_summary)
    except Exception as e:
        logger.warning("Full summarization failed, trying partial: %s", e)

    # Tier 2: Summarize only small messages, note oversized ones
    small_messages: list[dict] = []
    oversized_notes: list[str] = []

    for msg in messages:
        if is_oversized_for_summary(msg, context_window):
            role = msg.get("role", "message")
            tokens = estimate_message_tokens(msg)
            oversized_notes.append(
                f"[Large {role} (~{tokens // 1000}K tokens) omitted from summary]"
            )
        else:
            small_messages.append(msg)

    if small_messages:
        try:
            partial = await summarize_chunks(provider, small_messages, max_chunk_tokens, previous_summary)
            notes = "\n" + "\n".join(oversized_notes) if oversized_notes else ""
            return partial + notes
        except Exception as e:
            logger.warning("Partial summarization also failed: %s", e)

    # Tier 3: Metadata only
    return (
        f"Context contained {len(messages)} messages "
        f"({len(oversized_notes)} oversized). "
        f"Summary unavailable due to size limits."
    )


# ── Multi-stage summarization (the core algorithm) ──

async def summarize_in_stages(
    provider: LLMProvider,
    messages: list[dict],
    context_window: int,
    parts: int = DEFAULT_PARTS,
) -> str:
    """Multi-stage summarization like OpenClaw.

    Algorithm:
    1. Check if worth splitting (skip if small)
    2. Split messages into N parts by token share
    3. Summarize each part independently
    4. Merge partial summaries into one cohesive summary
    """
    if not messages:
        return ""

    chunk_ratio = compute_adaptive_chunk_ratio(messages, context_window)
    max_chunk_tokens = int(context_window * chunk_ratio) - SUMMARIZATION_OVERHEAD
    max_chunk_tokens = max(max_chunk_tokens, 2000)  # Floor

    total_tokens = estimate_messages_tokens(messages)

    # Skip multi-stage if not worth it
    if (
        parts <= 1
        or len(messages) < MIN_MESSAGES_FOR_SPLIT
        or total_tokens <= max_chunk_tokens
    ):
        return await summarize_with_fallback(
            provider, messages, max_chunk_tokens, context_window,
        )

    # Stage 1: Split and summarize each part independently
    splits = [
        s for s in split_messages_by_token_share(messages, parts)
        if s
    ]

    if len(splits) <= 1:
        return await summarize_with_fallback(
            provider, messages, max_chunk_tokens, context_window,
        )

    # Summarize parts concurrently
    partial_tasks = [
        summarize_with_fallback(provider, chunk, max_chunk_tokens, context_window)
        for chunk in splits
    ]
    partial_summaries = await asyncio.gather(*partial_tasks, return_exceptions=True)

    # Filter out failures
    valid_summaries: list[str] = []
    for i, result in enumerate(partial_summaries):
        if isinstance(result, Exception):
            logger.warning("Stage 1 chunk %d failed: %s", i, result)
            # Try to get text from the chunk directly
            text = messages_to_text(strip_tool_result_details(splits[i]))
            if text:
                valid_summaries.append(text[:2000] + "... [summarization failed, raw excerpt]")
        elif result:
            valid_summaries.append(result)

    if not valid_summaries:
        return "Context compaction failed. Check workspace files for context."

    if len(valid_summaries) == 1:
        return valid_summaries[0]

    # Stage 2: Merge partial summaries
    merge_text = MERGE_PROMPT
    for i, summary in enumerate(valid_summaries, 1):
        merge_text += f"### Part {i}\n{summary}\n\n"

    try:
        response = await provider.chat(
            messages=[{"role": "user", "content": merge_text}],
            tools=None,
            system=SUMMARIZATION_SYSTEM,
        )
        merged = response.content.strip()
        if merged and len(merged) > 20:
            logger.info(
                "Multi-stage compaction: %d parts → merged summary (%d chars)",
                len(valid_summaries), len(merged),
            )
            return merged
    except Exception as e:
        logger.warning("Merge stage failed, concatenating summaries: %s", e)

    # Fallback: concatenate partial summaries
    return "\n\n---\n\n".join(valid_summaries)


# ── History pruning ──

def prune_history_for_context(
    messages: list[dict],
    max_context_tokens: int,
    max_history_share: float = 0.5,
    parts: int = DEFAULT_PARTS,
) -> tuple[list[dict], int]:
    """Drop oldest message chunks until history fits within budget.

    Returns (pruned_messages, dropped_count).
    """
    budget = int(max_context_tokens * max_history_share)
    kept = messages
    total_dropped = 0
    kept_tokens = estimate_messages_tokens(kept)

    while kept and kept_tokens > budget:
        chunks = split_messages_by_token_share(kept, parts)
        if len(chunks) <= 1:
            break

        # Drop oldest chunk, keep rest
        dropped = chunks[0]
        if not dropped:
            # Safety: if the oldest chunk is empty, avoid infinite loop
            break
        prev_kept_len = len(kept)
        kept = [msg for chunk in chunks[1:] for msg in chunk]
        total_dropped += len(dropped)

        # Repair orphaned tool results
        kept = _repair_orphaned_tool_results(kept)
        # Always re-estimate: repair may change token counts even without
        # removing whole messages (e.g., stripping tool_result blocks from
        # a message's content list reduces tokens but not message count).
        kept_tokens = estimate_messages_tokens(kept)

        # Guard against no progress (e.g., rounding or repair restoring tokens)
        if len(kept) >= prev_kept_len:
            logger.warning("prune_history_for_context: no progress made, breaking to avoid infinite loop")
            break

    return kept, total_dropped


def _repair_orphaned_tool_results(messages: list[dict]) -> list[dict]:
    """Remove tool_result blocks whose tool_use was dropped during pruning."""
    # Collect all tool_use IDs
    tool_use_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_ids.add(block.get("id", ""))

    # Filter tool_results
    result: list[dict] = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            filtered = []
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("tool_use_id", "") in tool_use_ids:
                        filtered.append(block)
                    # else: drop orphaned tool_result
                else:
                    filtered.append(block)
            if filtered:
                result.append({**msg, "content": filtered})
        else:
            result.append(msg)

    return result
