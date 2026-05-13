"""Per-skill usage tracking — drives the curator's archive policy.

Each skills directory grows a hidden `.usage.json` that records when each
skill was created, when it was last invoked, and how often. Hermes #11425
(no skill metabolism) is the canonical complaint about a closed learning
loop without a forgetting half: the model creates skills indefinitely and
the prompt budget rots until someone runs `hermes curator archive` by hand.

The format is a flat dict keyed by skill name, written atomically. Records
are added on activation, never on bare discovery — discovery is cheap and
many skills are loaded but never invoked, so we don't reset their idle
clock just because the bot started.

Curator policy (read elsewhere; this module only records facts):
- 30 days idle           ⇒ mark stale (warning, still loaded)
- 90 days idle           ⇒ archive (moved to .archive/, recoverable)
- `pinned=True`          ⇒ exempt
- `agent_created=True`   ⇒ stricter scoring (Hermes #19324 lesson)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


USAGE_FILE_NAME = ".usage.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SkillUsage:
    """One row in `.usage.json`."""

    created_at: str = field(default_factory=_now_iso)
    last_used_at: str = ""
    use_count: int = 0
    patch_count: int = 0
    pinned: bool = False
    agent_created: bool = False
    # status is informational — actual archive lives in the filesystem
    # (.archive/ subdir). `stale` is set by the curator when idle > 30d.
    status: str = "active"  # active | stale | archived

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillUsage":
        # Drop unknown keys forward-compat; missing keys default.
        known = {f for f in cls.__dataclass_fields__}
        cleaned = {k: v for k, v in data.items() if k in known}
        return cls(**cleaned)


class UsageStore:
    """Read/write the usage JSON for a single skills root.

    Atomic on every write via tempfile + os.replace. Concurrent writes from
    multiple workers are still racy in principle (last-writer-wins), but
    the only writers are (a) the agent loop on tool invocation, (b) the
    curator job (single-instance), (c) the skill_manager tool. We accept
    occasional drift over the cost of a portalocker per call.
    """

    def __init__(self, skills_root: str | Path):
        self.skills_root = Path(skills_root)
        self.path = self.skills_root / USAGE_FILE_NAME

    # ─── persistence ──────────────────────────────────────────────

    def load(self) -> dict[str, SkillUsage]:
        if not self.path.is_file():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("usage store unreadable at %s: %s", self.path, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("usage store at %s is not a JSON object", self.path)
            return {}
        out: dict[str, SkillUsage] = {}
        for name, row in data.items():
            if not isinstance(row, dict):
                continue
            try:
                out[name] = SkillUsage.from_dict(row)
            except TypeError as exc:
                logger.warning(
                    "usage row %r in %s is malformed: %s", name, self.path, exc,
                )
        return out

    def save(self, records: dict[str, SkillUsage]) -> None:
        self.skills_root.mkdir(parents=True, exist_ok=True)
        payload = {name: rec.to_dict() for name, rec in records.items()}
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # Atomic replace: write to .tmp in same dir, then rename. Same-dir
        # rename is atomic on POSIX, atomic-ish on NTFS.
        fd, tmp = tempfile.mkstemp(
            prefix=".usage.", suffix=".tmp", dir=str(self.skills_root),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except Exception:
            # Best-effort cleanup; let caller see the original error.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ─── high-level helpers ──────────────────────────────────────

    def record_use(self, name: str) -> None:
        """Increment use_count + bump last_used_at. Creates a row if absent."""
        records = self.load()
        rec = records.get(name) or SkillUsage()
        rec.use_count += 1
        rec.last_used_at = _now_iso()
        if rec.status == "stale":
            # An invocation un-stales a skill — the user clearly still wants it.
            rec.status = "active"
        records[name] = rec
        self.save(records)

    def record_patch(self, name: str) -> None:
        records = self.load()
        rec = records.get(name) or SkillUsage()
        rec.patch_count += 1
        records[name] = rec
        self.save(records)

    def register_new(
        self, name: str, *, agent_created: bool = False, pinned: bool = False,
    ) -> None:
        """Called on skill creation. Idempotent — re-registering preserves
        history so repeated creates don't clobber the use_count.
        """
        records = self.load()
        rec = records.get(name)
        if rec is None:
            rec = SkillUsage(
                created_at=_now_iso(),
                agent_created=agent_created,
                pinned=pinned,
            )
        else:
            # Refresh flags that the caller may have flipped; keep counters.
            rec.agent_created = agent_created or rec.agent_created
            rec.pinned = pinned or rec.pinned
            if rec.status == "archived":
                rec.status = "active"  # un-archive on re-create
        records[name] = rec
        self.save(records)

    def set_status(self, name: str, status: str) -> None:
        if status not in {"active", "stale", "archived"}:
            raise ValueError(f"invalid status: {status}")
        records = self.load()
        rec = records.get(name) or SkillUsage()
        rec.status = status
        records[name] = rec
        self.save(records)

    def set_pinned(self, name: str, pinned: bool) -> None:
        records = self.load()
        rec = records.get(name) or SkillUsage()
        rec.pinned = pinned
        records[name] = rec
        self.save(records)

    def get(self, name: str) -> SkillUsage | None:
        return self.load().get(name)

    def remove(self, name: str) -> None:
        records = self.load()
        records.pop(name, None)
        self.save(records)


def days_since(iso: str) -> float:
    """Return age in days for an ISO timestamp. Returns +inf on empty/unparsable
    input — callers treat that as "never used" which is the conservative
    archive-eligibility answer.
    """
    if not iso:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    delta = datetime.now(timezone.utc) - ts
    return delta.total_seconds() / 86400.0
