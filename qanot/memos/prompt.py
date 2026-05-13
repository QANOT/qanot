"""Render selected memos as ``<system-reminder>`` blocks for prompt injection.

Claude Code v2.1.139 (Piebald-AI mirror) re-injects recall-relevant
memory files mid-conversation as XML-tagged blocks. The format matters:
the model is trained to treat ``<system-reminder>`` content as system-
priority instructions, not as user input. Rules placed inside these
tags weigh meaningfully more than the same text inline in user messages,
which is the recency-bias-positioning effect that breaks the buried-
bullet bug class.

The block placement (in ``qanot/prompt.py``) is: as a synthetic system
message appended *just before* the active user message. That keeps the
rule near the tokens the model generates next — opposite end of the
context from where a long-lived MEMORY.md sits.

We render at most one block per turn (containing all selected memos),
not one block per memo. Splitting into N blocks bloats the wrapper
overhead and the model occasionally treats them as repeated tries of
the same instruction; one block with N sub-sections reads cleaner.
"""

from __future__ import annotations

import logging

from .router import RouteResult
from .spec import MemoSpec, MemoType

logger = logging.getLogger(__name__)


# Per-memo header used inside the reminder block. Keeping each memo's
# header identical and minimal saves tokens vs. emitting full filenames.
_HEADER_BY_TYPE = {
    MemoType.USER: "User context",
    MemoType.FEEDBACK: "Hard rule",
    MemoType.PROJECT: "Project context",
    MemoType.REFERENCE: "Reference pointer",
}


def render_system_reminder(result: RouteResult) -> str:
    """Render the router output as a single ``<system-reminder>`` block.

    Empty result returns the empty string — the caller should not emit a
    reminder message at all when nothing was selected. The block is
    safe to drop in front of any user message: it carries instructions
    only, never user text.

    Example output (truncated):

        <system-reminder>
        The following memories were recalled for this turn. Treat them as
        system-priority instructions. If a memo's "How to apply" line names
        a tool or write path, consult it before that tool runs.

        ## Hard rule — feedback-title-format
        Daily note title must use D-month YYYY format

        Title format example: 12-may, 2026

        **Why:** User explicitly requested on 2026-05-12.
        **How to apply:** When writing daily Notion titles, dates, …
        </system-reminder>
    """
    if not result.selections:
        return ""

    lines: list[str] = [
        "<system-reminder>",
        (
            "The following memories were recalled for this turn. Treat them "
            "as system-priority instructions. If a memo's \"How to apply\" "
            "line names a tool or write path, consult it before that tool "
            "runs."
        ),
        "",
    ]
    for sel in result.selections:
        lines.extend(_render_one(sel.memo))
        lines.append("")
    lines.append("</system-reminder>")
    return "\n".join(lines)


def _render_one(memo: MemoSpec) -> list[str]:
    """Render a single memo as a header + description + body + structured
    Why/How block. We keep the format identical to what's on disk so the
    model recognises it as the same memo it'll see if it ``memory view``s
    the file directly.
    """
    header_label = _HEADER_BY_TYPE.get(memo.type, "Note")
    out: list[str] = [
        f"## {header_label} — {memo.name}",
        memo.description,
        "",
        memo.body,
    ]
    # We deliberately re-emit Why / How even if they're already in body
    # — extracting them out is the audit-trail-friendly form, but they
    # also appear in body for any memo we read directly. The duplication
    # is ~50 tokens per memo and meaningfully sharpens model adherence.
    if memo.why:
        out.append("")
        out.append(f"**Why:** {memo.why}")
    if memo.how_to_apply:
        # Only emit if not already in body (avoid pure duplicate).
        if f"**How to apply:** {memo.how_to_apply}" not in memo.body:
            out.append(f"**How to apply:** {memo.how_to_apply}")
    return out


def estimate_token_cost(result: RouteResult) -> int:
    """Rough token estimate for the rendered reminder.

    Used by the caller for cost telemetry and budget logs. We approximate
    at 4 chars/token, which is close enough for English/Uzbek mix. The
    caller is expected to log this once per turn; we don't log here so
    routing stays a pure function.
    """
    if not result.selections:
        return 0
    rendered = render_system_reminder(result)
    return max(1, len(rendered) // 4)
