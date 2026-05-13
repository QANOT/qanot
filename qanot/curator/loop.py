"""Skill curator — age-based passes and the LLM-review gate.

Two surfaces live here:

- `run_age_pass(skills_root)` is pure-Python, cheap, deterministic. It
  flips usage-store rows to `stale` after 30 idle days and archives them
  after 90. Safe to call from the scheduler on every tick.

- `should_run_review(skills_root)` is the gate for the LLM review pass.
  It returns True only when (a) the bot has been idle for ≥2 hours,
  (b) the last review was ≥7 days ago, and (c) there are at least 3
  agent-created skills to review. The actual review runs as an isolated
  cron agent via `CURATOR_REVIEW_PROMPT`, so this function only decides
  whether to dispatch it.

The 30/90 day defaults match Hermes' curator constants. Skills that are
`pinned=True` or were not `agent_created=True` are exempt — we don't
silently rot the workspace ships and never touch user-authored skills.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from qanot.skills.store import SkillStore, StoreError
from qanot.skills.usage import SkillUsage, UsageStore, days_since

logger = logging.getLogger(__name__)


# Defaults. Make these configurable via Config later if needed.
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90
REVIEW_INTERVAL_DAYS = 7
REVIEW_MIN_IDLE_HOURS = 2
REVIEW_MIN_AGENT_SKILLS = 3

# Tombstone file recording the last review run.
CURATOR_STATE_FILE = ".curator_state.json"


@dataclass
class CuratorReport:
    """Summary of one age-pass run."""

    marked_stale: list[str] = field(default_factory=list)
    unmarked_active: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    skipped_pinned: list[str] = field(default_factory=list)
    skipped_user_authored: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        bits = []
        if self.archived:
            bits.append(f"archived={len(self.archived)}")
        if self.marked_stale:
            bits.append(f"marked_stale={len(self.marked_stale)}")
        if self.unmarked_active:
            bits.append(f"reactivated={len(self.unmarked_active)}")
        if not bits:
            return "no-op"
        return " ".join(bits)


# ─── age-based pass (no LLM) ──────────────────────────────────────


def run_age_pass(skills_root: str | Path) -> CuratorReport:
    """Apply stale/archive rules to the usage store. No LLM cost.

    - rows idle ≥ ARCHIVE_AFTER_DAYS, not pinned, agent_created
        → archive the skill directory (recoverable via `skill_unarchive`)
    - rows idle ≥ STALE_AFTER_DAYS, not pinned
        → flip status to "stale" (warning only; skill stays loaded)
    - rows recently used but still marked stale
        → flip back to "active"
    """
    report = CuratorReport()
    skills_root = Path(skills_root)
    if not skills_root.is_dir():
        return report

    store = SkillStore(skills_root)
    usage = store.usage
    records = usage.load()

    for name, rec in records.items():
        if rec.pinned:
            report.skipped_pinned.append(name)
            continue
        if not rec.agent_created:
            # User-authored skills are immutable to the curator — they
            # represent ground truth, not the agent's experimental
            # autobiography.
            report.skipped_user_authored.append(name)
            continue
        if rec.status == "archived":
            # Already archived; nothing to do.
            continue

        idle_days = days_since(rec.last_used_at or rec.created_at)

        if idle_days >= ARCHIVE_AFTER_DAYS:
            try:
                store.archive(name)
                report.archived.append(name)
            except StoreError as exc:
                logger.warning("curator: archive %s failed: %s", name, exc)
                report.errors.append(f"{name}: archive failed ({exc})")
            continue

        if idle_days >= STALE_AFTER_DAYS and rec.status != "stale":
            try:
                usage.set_status(name, "stale")
                report.marked_stale.append(name)
            except Exception as exc:  # noqa: BLE001 — usage I/O can throw any OSError
                logger.warning("curator: stale %s failed: %s", name, exc)
                report.errors.append(f"{name}: set_status failed ({exc})")
            continue

        # Reactivate: row was stale but recent use brought it back.
        if rec.status == "stale" and idle_days < STALE_AFTER_DAYS:
            try:
                usage.set_status(name, "active")
                report.unmarked_active.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("curator: reactivate %s failed: %s", name, exc)

    return report


# Alias retained for the very first cron tick in old code that imported the
# shorter name.
age_pass = run_age_pass


# ─── LLM review gate ──────────────────────────────────────────────


def should_run_review(
    skills_root: str | Path,
    *,
    last_user_activity_iso: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether to dispatch the LLM-review cron job.

    Returns ``(should_run, reason)``. Caller logs the reason.

    The three gates, in order:
    1. Bot has been idle for ≥ REVIEW_MIN_IDLE_HOURS — running a multi-LLM
       review during an active turn would inflate latency budgets.
    2. At least REVIEW_INTERVAL_DAYS have passed since the last review.
    3. At least REVIEW_MIN_AGENT_SKILLS agent-created skills exist — running
       a review against a 2-skill library is wasted tokens.
    """
    skills_root = Path(skills_root)
    now = now or datetime.now(timezone.utc)

    # Gate 1: idle
    if last_user_activity_iso:
        idle_days = days_since(last_user_activity_iso)
        if idle_days * 24 < REVIEW_MIN_IDLE_HOURS:
            return False, "user not idle"

    # Gate 2: interval
    state = _load_state(skills_root)
    last_iso = state.get("last_review_at", "")
    if last_iso and days_since(last_iso) < REVIEW_INTERVAL_DAYS:
        return False, f"last review {days_since(last_iso):.1f}d ago"

    # Gate 3: enough agent-created skills
    usage = UsageStore(skills_root).load()
    agent_skills = sum(
        1 for r in usage.values()
        if r.agent_created and r.status != "archived"
    )
    if agent_skills < REVIEW_MIN_AGENT_SKILLS:
        return False, f"only {agent_skills} agent-created skills"

    return True, "ready"


def record_review_run(
    skills_root: str | Path, *, now: datetime | None = None,
) -> None:
    """Record that a review just ran, so should_run_review() backs off."""
    state = _load_state(skills_root)
    state["last_review_at"] = (now or datetime.now(timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _save_state(skills_root, state)


# ─── state file ───────────────────────────────────────────────────


def _state_path(skills_root: Path) -> Path:
    return skills_root / CURATOR_STATE_FILE


def _load_state(skills_root: Path) -> dict:
    p = _state_path(skills_root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(skills_root: Path, state: dict) -> None:
    skills_root.mkdir(parents=True, exist_ok=True)
    p = _state_path(skills_root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)
