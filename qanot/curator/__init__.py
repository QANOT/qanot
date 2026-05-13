"""Skill curator — keeps the skill library bounded and high-quality.

The closed learning loop has a forgetting half. Hermes' #11425 line of bugs
(skills accumulate forever, prompt budget rots, duplicates pile up) traces
back to *creating* skills without ever *retiring* them. The curator does two
things, on different cadences:

  1. Age-based passes (cheap, no LLM):
       - mark `idle > 30d` as stale
       - archive `idle > 90d` to `.archive/`
       - exempt pinned skills + STICKY topics

  2. LLM review (weekly, isolated cron agent):
       - propose semantic duplicates among agent-created skills
       - propose consolidations into umbrella skills
       - never auto-delete — only ARCHIVE (recoverable)
       - results land in `proactive-outbox.md` so the user sees them
"""

from __future__ import annotations

from .loop import CuratorReport, age_pass, run_age_pass, should_run_review
from .prompts import CURATOR_REVIEW_PROMPT

__all__ = [
    "CuratorReport",
    "age_pass",
    "run_age_pass",
    "should_run_review",
    "CURATOR_REVIEW_PROMPT",
]
