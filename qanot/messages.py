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


def _collect_tool_use_ids(content) -> list[str]:
    """Extract tool_use ids from an assistant message's content list."""
    if not isinstance(content, list):
        return []
    ids: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tid = block.get("id", "")
            if tid:
                ids.append(tid)
    return ids


def _next_is_matching_tool_result(
    messages: list[dict], index: int, pending_ids: list[str],
) -> bool:
    """Return True if messages[index] is a user message with tool_results
    covering all pending_ids."""
    if index >= len(messages):
        return False
    nxt = messages[index]
    if nxt.get("role") != "user":
        return False
    content = nxt.get("content")
    if not isinstance(content, list):
        return False
    found: set[str] = set()
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tid = block.get("tool_use_id", "")
            if tid:
                found.add(tid)
    return all(pid in found for pid in pending_ids)


def _synthesize_placeholder_results(ids: list[str]) -> dict:
    """Build a user message with placeholder tool_results for orphan tool_uses."""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": "[Tool execution interrupted — no result recorded]",
                "is_error": True,
            }
            for tid in ids
        ],
    }


def repair_messages(messages: list[dict]) -> list[dict]:
    """Repair message history to fix common corruption issues.

    Anthropic API requires: every assistant `tool_use` block must be
    followed in the VERY NEXT message by a user `tool_result` block with
    a matching tool_use_id. This is position-critical — placeholders at
    the end of history do NOT satisfy the requirement.

    Fixes:
    - Orphan tool_use (assistant has tool_use but next msg isn't matching
      tool_result) → insert synthetic placeholder tool_result message RIGHT
      AFTER the orphan
    - Orphan tool_result (user has tool_result with no matching tool_use
      earlier) → drop the orphan tool_result block
    - Base64 image bloat in older messages
    """
    if not messages:
        return messages

    # Strip old images first to prevent context bloat
    messages = strip_old_images(messages)

    # First pass: walk through messages, inserting placeholder tool_results
    # immediately after any assistant message with orphan tool_uses.
    repaired: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant":
            tool_use_ids = _collect_tool_use_ids(content)
            repaired.append(msg)
            if tool_use_ids:
                # Does the next message satisfy all tool_use_ids?
                if _next_is_matching_tool_result(messages, i + 1, tool_use_ids):
                    # Good — the next user message has the tool_results we need.
                    # We'll process it on the next loop iteration.
                    pass
                else:
                    # Orphan: insert synthetic placeholder RIGHT after this assistant msg
                    logger.warning(
                        "Synthesizing placeholder tool_results for orphan tool_uses at msg %d: %s",
                        i, tool_use_ids,
                    )
                    repaired.append(_synthesize_placeholder_results(tool_use_ids))
            i += 1
        else:
            # Pass user messages through, but filter orphan tool_results
            # (tool_results pointing to tool_use ids that never existed).
            # We detect these by checking all assistant tool_use ids seen so far.
            if isinstance(content, list):
                all_prior_tool_use_ids: set[str] = set()
                for prev in repaired:
                    if prev.get("role") == "assistant":
                        all_prior_tool_use_ids.update(
                            _collect_tool_use_ids(prev.get("content"))
                        )
                valid: list = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if tid in all_prior_tool_use_ids:
                            valid.append(block)
                        else:
                            logger.warning("Removing orphaned tool_result: %s", tid)
                    else:
                        valid.append(block)
                if valid:
                    repaired.append({"role": "user", "content": valid})
            else:
                repaired.append(msg)
            i += 1

    return repaired
