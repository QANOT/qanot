"""Message repair and sanitization for conversation history."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def strip_old_images(messages: list[dict]) -> list[dict]:
    """Strip base64 image blocks from all user messages except the last one.

    Images are huge (~130K+ chars each) and bloat context fast.
    Once the model has seen and responded to an image, the base64 data
    is no longer needed — replace with a lightweight placeholder.
    """
    if not messages:
        return messages

    # Find the index of the last user message that contains images
    last_image_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            if any(
                isinstance(b, dict) and b.get("type") == "image"
                for b in msg["content"]
            ):
                last_image_idx = i
                break

    if last_image_idx < 0:
        return messages

    result = []
    for i, msg in enumerate(messages):
        if (
            i != last_image_idx
            and msg.get("role") == "user"
            and isinstance(msg.get("content"), list)
        ):
            # Replace image blocks with placeholder text
            new_content = []
            image_count = 0
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "image":
                    image_count += 1
                else:
                    new_content.append(block)
            if image_count:
                new_content.append({
                    "type": "text",
                    "text": f"[{image_count} image(s) were analyzed in this turn]",
                })
            result.append({"role": msg["role"], "content": new_content})
        else:
            result.append(msg)

    return result


def strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Strip thinking blocks from assistant messages in conversation history.

    Thinking blocks are internal reasoning from the model (extended thinking).
    They must not be sent back in context — the API rejects them and they
    waste tokens. Like OpenClaw's dropThinkingBlocks().
    """
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if not any(isinstance(block, dict) and block.get("type") == "thinking" for block in content):
            continue
        filtered = [
            block for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        ]
        msg["content"] = filtered if filtered else [{"type": "text", "text": ""}]
    return messages


def repair_messages(messages: list[dict]) -> list[dict]:
    """Repair message history to fix common corruption issues.

    Fixes:
    - Orphaned tool_result blocks (no matching tool_use)
    - Consecutive same-role messages (merge or remove)
    - Base64 image bloat in older messages
    """
    if not messages:
        return messages

    # Strip old images first to prevent context bloat
    messages = strip_old_images(messages)

    repaired = []
    # Track tool_use IDs that exist
    active_tool_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            # Track tool_use IDs from assistant messages
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    active_tool_ids.add(block.get("id", ""))
            repaired.append(msg)

        elif role == "user" and isinstance(content, list):
            # Filter tool_results: only keep those with matching tool_use
            valid_results = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    if tool_use_id in active_tool_ids:
                        valid_results.append(block)
                        active_tool_ids.discard(tool_use_id)
                    else:
                        logger.warning("Removing orphaned tool_result: %s", tool_use_id)
                else:
                    valid_results.append(block)

            if valid_results:
                repaired.append({"role": "user", "content": valid_results})
        else:
            repaired.append(msg)

    return repaired
