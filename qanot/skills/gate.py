"""AAMC-style cost gate for skill creation.

Hermes' canonical skill-bloat bug (issue #13265) traces to a mechanical
`creation_nudge_interval: 15` timer that *nudges* the agent to create a
skill every 15 turns regardless of whether anything novel just happened.
The result: 100s of low-value skills accumulate, the prompt budget rots,
and #22620 ("10-15K tokens/turn on skill index") becomes a real bug.

This module gates `skill_create` on three signals instead:

  1. **Cost threshold** — the trajectory that produced the would-be skill
     must have cost at least N tokens. Cheap workflows (one-shot replies,
     small lookups) are not worth a persistent skill.

  2. **Semantic novelty** — the would-be skill's description must not
     overlap heavily with an existing skill. We use the lightweight
     keyword scorer from `qanot/skills/index.py` here so the gate adds
     zero LLM cost. A semantic embedding upgrade lands later in P1
     once the RAG embedder is plumbed through.

  3. **Hard pre-check** — name validity, body size, scan verdict. These
     duplicate the SkillStore checks, but failing fast at the gate avoids
     the .history snapshot and reload-callback work that a doomed write
     would still trigger.

The gate returns a `GateResult` with a clear reason and (when rejecting)
a suggestion the caller can surface to the agent so the next attempt is
informed — e.g. "this looks like skill X; consider `skill_patch` instead".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from .guard import Verdict, scan_text
from .index import _score as _keyword_score  # reuse the index scorer
from .index import _tokenize
from .loader import LoadedSkill
from .spec import (
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MAX_SKILL_CONTENT_CHARS,
    SkillSpec,
)

logger = logging.getLogger(__name__)


# Gate defaults. Knobs are exposed via Config later if needed.

# Minimum token cost the producing trajectory must have paid for the skill
# to be worth persisting. Below this, the workflow is too cheap to amortize.
# 5_000 tokens at Opus pricing ≈ $0.075 — call it a "non-trivial turn".
DEFAULT_MIN_TRAJECTORY_TOKENS = 5_000

# Maximum tolerable keyword-score against an existing skill before we
# reject as duplicate. The scorer max is roughly `10 + 2 * keyword_count`;
# a score of 6 means at least 3 meaningful tokens overlap, which is
# enough to suggest the new skill collides with an existing one.
DEFAULT_SEMANTIC_REJECT_SCORE = 6

# A milder threshold where we WARN ("looks similar") but still allow,
# so the agent can patch the existing skill if intentional.
DEFAULT_SEMANTIC_WARN_SCORE = 3


@dataclass
class GateResult:
    """Decision plus rationale.

    `suggestion` is a structured hint the caller should pass back to the
    agent: when blocking, it tells the agent what to do instead (e.g.
    use ``skill_patch`` on the existing match); when allowing, it can
    name a closely-related skill the new one should reference.
    """

    allow: bool
    reason: str
    score: int = 0                       # keyword-match score against best existing skill
    matched_skill: str | None = None     # best-match name when score > 0
    suggestion: dict | None = None       # actionable hint for the caller


def pre_create_check(
    name: str,
    description: str,
    body: str,
    *,
    existing_skills: Iterable[LoadedSkill | SkillSpec] = (),
    trajectory_tokens: int = 0,
    min_trajectory_tokens: int = DEFAULT_MIN_TRAJECTORY_TOKENS,
    semantic_reject_score: int = DEFAULT_SEMANTIC_REJECT_SCORE,
    semantic_warn_score: int = DEFAULT_SEMANTIC_WARN_SCORE,
    bypass: bool = False,
) -> GateResult:
    """Decide whether to allow ``skill_create`` to proceed.

    `existing_skills` should be the current loaded skills (LoadedSkill or
    SkillSpec). Passing an empty iterable disables the semantic-novelty
    check — useful for tests and for first-skill creation when the
    library is genuinely empty.

    `trajectory_tokens` is the total token cost (input + output) attributable
    to the trajectory that produced this skill. The caller computes it from
    the context tracker — see `qanot/context.py:ContextTracker.total_tokens`.
    Passing 0 effectively disables the cost gate; the caller should always
    populate this in production.

    `bypass=True` skips the cost and semantic checks (still runs the hard
    pre-check). The curator and explicit user request paths use this.
    """
    # ─── 1. Hard pre-check — duplicate cheap validators ──────────
    err = _hard_precheck(name, description, body)
    if err is not None:
        return GateResult(allow=False, reason=err)

    if bypass:
        return GateResult(
            allow=True,
            reason="bypass=true (curator or explicit user request)",
        )

    # ─── 2. Cost gate ────────────────────────────────────────────
    if trajectory_tokens and trajectory_tokens < min_trajectory_tokens:
        return GateResult(
            allow=False,
            reason=(
                f"trajectory cost too low: {trajectory_tokens} tokens "
                f"< {min_trajectory_tokens}. Cheap workflows are not worth "
                f"a persistent skill — answer directly this turn."
            ),
            suggestion={
                "action": "respond_directly",
                "hint": (
                    "If you expect this same workflow to recur, wait until "
                    "you've actually paid the cost a second time before "
                    "creating the skill."
                ),
            },
        )

    # ─── 3. Semantic-novelty gate ────────────────────────────────
    best_score, best_name = _best_existing_match(
        description, body, existing_skills,
    )

    if best_score >= semantic_reject_score:
        return GateResult(
            allow=False,
            reason=(
                f"too similar to existing skill {best_name!r} "
                f"(score {best_score}). Use `skill_patch` to refine the "
                f"existing skill, or `skill_edit` to rewrite it."
            ),
            score=best_score,
            matched_skill=best_name,
            suggestion={
                "action": "patch_existing",
                "target": best_name,
                "hint": (
                    "Open the existing skill with `skill_view` and decide "
                    "whether to extend its body or split into a more "
                    "specific sibling."
                ),
            },
        )

    if best_score >= semantic_warn_score:
        return GateResult(
            allow=True,
            reason=(
                f"allowed with caution — looks related to {best_name!r} "
                f"(score {best_score}). Make sure the new skill's "
                f"trigger description is sharper than the existing one."
            ),
            score=best_score,
            matched_skill=best_name,
            suggestion={
                "action": "tighten_description",
                "related": best_name,
                "hint": (
                    "Both descriptions should make it obvious to a future "
                    "session which skill to reach for in which situation."
                ),
            },
        )

    return GateResult(
        allow=True,
        reason="passed: cost ok, no semantic collision",
        score=best_score,
        matched_skill=best_name,
    )


# ─── internals ───────────────────────────────────────────────────


def _hard_precheck(name: str, description: str, body: str) -> str | None:
    """Replicate the SkillStore validators so the gate can refuse before
    any disk work happens. Returns the error message or None.
    """
    if not isinstance(name, str) or not name.strip():
        return "name is required"
    if len(name) > MAX_NAME_CHARS:
        return f"name exceeds {MAX_NAME_CHARS} chars"
    # Loose pre-check; the strict regex lives in SkillSpec.
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        return f"name {name!r} must be lowercase a-z 0-9 hyphens only"
    if "--" in name or name.startswith("-") or name.endswith("-"):
        return f"name {name!r} has invalid hyphen placement"

    if not isinstance(description, str) or not description.strip():
        return "description is required"
    if len(description) > MAX_DESCRIPTION_CHARS:
        return (
            f"description exceeds {MAX_DESCRIPTION_CHARS} chars "
            f"(got {len(description)})"
        )

    if not isinstance(body, str) or not body.strip():
        return "body is required"
    if len(body) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"body exceeds {MAX_SKILL_CONTENT_CHARS} chars "
            f"(got {len(body)})"
        )

    # Cheap scan to fail fast — store.create runs this again, but we
    # don't want to allocate the dir + roll back on something we could
    # have caught for free.
    scan = scan_text(body, label="gate")
    if scan.verdict == Verdict.DANGEROUS:
        return f"scan rejected body: {scan.to_summary(limit=2)}"
    return None


def _best_existing_match(
    description: str,
    body: str,
    existing: Iterable[LoadedSkill | SkillSpec],
) -> tuple[int, str | None]:
    """Run the keyword scorer against each existing skill and return the
    best (score, name) tuple. We score on description+body combined to
    catch cases where two skills have different surface descriptions but
    identical bodies (which we want to flag).
    """
    blob = f"{description}\n\n{body}".lower()
    blob_tokens = _tokenize(blob)
    best_score = 0
    best_name: str | None = None
    for item in existing:
        spec = item.spec if isinstance(item, LoadedSkill) else item
        # Score the proposed skill against the existing one's description
        # — the same scorer used at runtime activation. Symmetric scoring
        # would be more accurate but is overkill at this gate; the
        # description+body of the new skill against the existing
        # description captures the duplication case we care about.
        score = _keyword_score(spec, blob, blob_tokens)
        if score > best_score:
            best_score = score
            best_name = spec.name
    return best_score, best_name
