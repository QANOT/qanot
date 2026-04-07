"""Shared approval flow infrastructure for mcp_manage and config_manage."""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

PROPOSAL_TTL_SECONDS = 600  # 10 minutes


def now() -> float:
    """Current time in seconds (monotonic-compatible wrapper)."""
    return time.time()


def append_audit(
    workspace_dir: str,
    user_id: str,
    event: str,
    details: dict,
    *,
    tag: str = "audit",
) -> None:
    """Append an audit event to the daily note.

    Args:
        tag: Prefix for the audit entry (e.g. "mcp", "secret").
        details: Must NEVER contain raw secret values.
    """
    try:
        from qanot.memory import write_daily_note
        payload = json.dumps(details, ensure_ascii=False, sort_keys=True)
        write_daily_note(
            content=f"**[{tag}:{event}]** {payload}",
            workspace_dir=workspace_dir,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Failed to write %s audit entry: %s", tag, e)


def validate_proposal(
    pending_dict: dict,
    proposal_id: str,
    user_id: int,
) -> tuple[dict | None, str | None]:
    """Validate a pending proposal: exists, not expired, owned by user.

    Returns:
        (pending_data, None) on success.
        (None, error_message) on failure. Also removes expired proposals.
    """
    pending = pending_dict.get(proposal_id)
    if not pending:
        return None, "Bu so'rov muddati tugagan yoki topilmadi."

    if now() > pending.get("expires_at", 0):
        pending_dict.pop(proposal_id, None)
        return None, "Muddati tugagan."

    if pending["user_id"] != user_id:
        return None, "Faqat so'rov egasi ruxsat berishi mumkin."

    return pending, None
