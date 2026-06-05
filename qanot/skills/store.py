"""Atomic skill create / edit / patch / archive — with .history/ versioning.

The Hermes lifecycle had three structural bugs we close here:
- #20352 *Unversioned hand-edited markdown* — no diff, no rollback. We keep
  every prior `SKILL.md` body under `.history/YYYYMMDD-HHMMSS.md` so any
  edit is reversible.
- #19549 *Locally-patched skills don't update* — they kept the local copy
  as the canonical, breaking upstream sync. We store the agent's patches
  separately at `SKILL.local.md` (overlay) and never touch the upstream
  body, so `qanot dream rollback` and upstream upgrades both work.
- #11425 *No skill metabolism* — usage tracking now records create/patch/
  archive moves so the curator can prune.

Writes are atomic: temp file in same dir, then `os.replace`. The same
ALLOWED_SUBDIRS gate that Hermes uses protects supporting-file writes from
escaping the skill bundle.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .guard import Verdict, scan_text
from .spec import (
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MAX_SKILL_CONTENT_CHARS,
    SkillSpecError,
    parse_skill_file,
)
from .usage import UsageStore

logger = logging.getLogger(__name__)


# Subdirectories a skill may legitimately ship — Hermes' set verbatim.
# Writes outside this set are rejected by `write_supporting_file`.
ALLOWED_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})

HISTORY_DIR_NAME = ".history"
ARCHIVE_DIR_NAME = ".archive"


class StoreError(Exception):
    """Raised on store operation failure — write rejected, path invalid,
    name collides, etc. Distinct from SkillSpecError which is about content."""


@dataclass
class WriteResult:
    """What we tell the agent after a successful skill write."""

    name: str
    path: Path
    action: str  # "created" | "edited" | "patched" | "archived" | "unarchived"
    history_snapshot: Path | None
    scan_verdict: Verdict


# ─── helpers ──────────────────────────────────────────────────────


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _fuzzy_line_patch(body: str, old: str, new: str) -> str | None:
    """Whitespace-tolerant single-span replace.

    Matches ``old``'s lines against ``body``'s lines by *stripped* equality
    (so indentation / trailing-space drift is forgiven) and replaces a UNIQUE
    span with ``new``. Returns the patched body, or None when there is no
    match or more than one (ambiguous — refuse, like the exact path).
    """
    body_lines = body.splitlines()
    s_old = [ln.strip() for ln in old.splitlines()]
    if not s_old:
        return None
    s_body = [ln.strip() for ln in body_lines]
    m = len(s_old)
    spans = [i for i in range(len(s_body) - m + 1) if s_body[i:i + m] == s_old]
    if len(spans) != 1:
        return None
    i = spans[0]
    patched = body_lines[:i] + new.splitlines() + body_lines[i + m:]
    out = "\n".join(patched)
    if body.endswith("\n"):
        out += "\n"
    return out


def _atomic_write_text(path: Path, content: str) -> None:
    """tempfile-in-same-dir + os.replace.

    Same-dir rename is atomic on POSIX and atomic-ish on NTFS. If the
    parent dir doesn't exist we create it first; tempfile creation is
    deliberate (not `path.write_text`) so we never leave a half-written
    SKILL.md behind on crash.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix, dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _snapshot(path: Path) -> Path | None:
    """Copy `path` into `<dir>/.history/<stamp>.md`. Returns the snapshot
    path or None if `path` doesn't exist yet (i.e. first create).
    """
    if not path.is_file():
        return None
    hist_dir = path.parent / HISTORY_DIR_NAME
    hist_dir.mkdir(parents=True, exist_ok=True)
    target = hist_dir / f"{_now_stamp()}-{path.name}"
    shutil.copy2(path, target)
    return target


def _validate_subdir(rel_path: Path) -> None:
    """`rel_path` must be `references/...`, `templates/...`, `scripts/...`
    or `assets/...`. No traversal, no symlink targets.
    """
    parts = rel_path.parts
    if not parts:
        raise StoreError("empty supporting-file path")
    if ".." in parts:
        raise StoreError("path traversal not allowed in supporting-file path")
    top = parts[0]
    if top not in ALLOWED_SUBDIRS:
        raise StoreError(
            f"supporting files must live under one of {sorted(ALLOWED_SUBDIRS)}; "
            f"got {top!r}"
        )


def _frontmatter_text(name: str, description: str, *, extra: dict | None = None) -> str:
    """Render a minimal valid frontmatter block."""
    # YAML quoting policy: we always quote `description` because it commonly
    # contains punctuation, quotes, and colons. Hermes' loose parser splits
    # on the first `:` and loses the rest — we won't.
    description = description.replace('"', '\\"')
    lines = ["---", f"name: {name}", f'description: "{description}"']
    if extra:
        for k, v in extra.items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, list):
                v = "[" + ", ".join(str(x) for x in v) + "]"
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ─── public API ──────────────────────────────────────────────────


class SkillStore:
    """Bundles atomic write + usage tracking + scan-on-write for one skills
    root. Bound to the writable root only (not external_dirs) — see
    `SkillLoader.writable_root()`.
    """

    def __init__(self, skills_root: str | Path):
        self.skills_root = Path(skills_root)
        self.usage = UsageStore(self.skills_root)

    # ─── create ──────────────────────────────────────────────────

    def create(
        self,
        name: str,
        description: str,
        body: str,
        *,
        agent_created: bool = False,
        scan_strict: bool = True,
        extra_frontmatter: dict | None = None,
    ) -> WriteResult:
        """Create a brand-new skill. Idempotent — refuses if the dir exists.

        On `scan_strict=True` (default) any DANGEROUS verdict raises and
        the directory is rolled back. CAUTION verdicts log but allow the
        write — the skill is loadable but flagged in the prompt index.
        """
        self._validate_name(name)
        self._validate_description(description)
        body = (body or "").strip()
        if len(body) > MAX_SKILL_CONTENT_CHARS:
            raise StoreError(
                f"body exceeds {MAX_SKILL_CONTENT_CHARS} chars (got {len(body)})"
            )

        skill_dir = self.skills_root / name
        if skill_dir.exists():
            raise StoreError(f"skill {name!r} already exists at {skill_dir}")

        skill_md_content = _frontmatter_text(
            name, description, extra=extra_frontmatter,
        ) + body

        # Pre-write scan: agent_created skills must pass on creation.
        scan = scan_text(skill_md_content, label=f"{name}/SKILL.md")
        if scan_strict and scan.verdict == Verdict.DANGEROUS:
            raise StoreError(
                f"scan rejected skill {name!r}:\n{scan.to_summary()}"
            )

        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        try:
            _atomic_write_text(skill_md, skill_md_content)
        except Exception:
            # Roll back the empty dir on write failure.
            try:
                skill_dir.rmdir()
            except OSError:
                pass
            raise

        # Validate by re-parsing; reject and roll back on any spec violation.
        try:
            parse_skill_file(skill_md)
        except SkillSpecError as exc:
            shutil.rmtree(skill_dir, ignore_errors=True)
            raise StoreError(f"post-write validation failed: {exc}") from exc

        self.usage.register_new(name, agent_created=agent_created)
        return WriteResult(
            name=name, path=skill_md, action="created",
            history_snapshot=None, scan_verdict=scan.verdict,
        )

    # ─── edit / patch ────────────────────────────────────────────

    def edit(
        self,
        name: str,
        new_body: str,
        *,
        new_description: str | None = None,
        scan_strict: bool = True,
    ) -> WriteResult:
        """Replace the entire body (and optionally description) of an
        existing skill. Snapshots the prior SKILL.md into `.history/`
        before overwriting.
        """
        skill_md = self._must_exist(name)
        existing = parse_skill_file(skill_md)
        desc = new_description if new_description is not None else existing.description
        self._validate_description(desc)
        new_body = (new_body or "").strip()
        if len(new_body) > MAX_SKILL_CONTENT_CHARS:
            raise StoreError(
                f"body exceeds {MAX_SKILL_CONTENT_CHARS} chars "
                f"(got {len(new_body)})"
            )

        # Preserve any optional frontmatter the user had (version/author/etc).
        preserved: dict = {}
        if existing.version:
            preserved["version"] = existing.version
        if existing.author:
            preserved["author"] = existing.author
        if existing.license:
            preserved["license"] = existing.license
        if existing.platforms:
            preserved["platforms"] = existing.platforms

        new_content = _frontmatter_text(name, desc, extra=preserved) + new_body
        scan = scan_text(new_content, label=f"{name}/SKILL.md")
        if scan_strict and scan.verdict == Verdict.DANGEROUS:
            raise StoreError(
                f"scan rejected edit to {name!r}:\n{scan.to_summary()}"
            )

        snap = _snapshot(skill_md)
        _atomic_write_text(skill_md, new_content)

        # Re-parse to guarantee on-disk validity; rollback on failure.
        try:
            parse_skill_file(skill_md)
        except SkillSpecError as exc:
            # Restore prior snapshot if we have one.
            if snap is not None:
                shutil.copy2(snap, skill_md)
            raise StoreError(f"post-edit validation failed: {exc}") from exc

        self.usage.record_patch(name)
        return WriteResult(
            name=name, path=skill_md, action="edited",
            history_snapshot=snap, scan_verdict=scan.verdict,
        )

    def patch_body(
        self, name: str, old_substring: str, new_substring: str,
        *, scan_strict: bool = True,
    ) -> WriteResult:
        """Find-and-replace within the body of an existing SKILL.md.

        Returns an error if `old_substring` is not present or appears more
        than once — patches must be unambiguous so repeated runs are safe.
        """
        skill_md = self._must_exist(name)
        existing = parse_skill_file(skill_md)
        body = existing.body
        count = body.count(old_substring)
        if count > 1:
            raise StoreError(
                f"old_substring appears {count} times in {name} — ambiguous"
            )
        if count == 1:
            new_body = body.replace(old_substring, new_substring, 1)
            return self.edit(name, new_body, scan_strict=scan_strict)

        # Exact match failed — try a whitespace-tolerant line-span match. The
        # model often quotes skill text with indentation/trailing-space drift;
        # an exact-only patch then fails and forces a full rewrite. Match by
        # per-line stripped equality, requiring a UNIQUE span (still safe to
        # re-run). Single-line patches work too.
        new_body = _fuzzy_line_patch(body, old_substring, new_substring)
        if new_body is None:
            raise StoreError(f"old_substring not found in {name}")
        return self.edit(name, new_body, scan_strict=scan_strict)

    # ─── archive / restore ───────────────────────────────────────

    def archive(self, name: str) -> WriteResult:
        """Move the skill dir under `<skills_root>/.archive/<name>/`.

        Always reversible via `unarchive`. The usage row keeps its history
        so re-archiving doesn't reset the metabolism clock.
        """
        skill_dir = self._must_exist(name).parent
        archive_root = self.skills_root / ARCHIVE_DIR_NAME
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / name
        if target.exists():
            raise StoreError(f"archive slot already occupied: {target}")
        shutil.move(str(skill_dir), str(target))
        self.usage.set_status(name, "archived")
        return WriteResult(
            name=name, path=target / "SKILL.md", action="archived",
            history_snapshot=None, scan_verdict=Verdict.SAFE,
        )

    def unarchive(self, name: str) -> WriteResult:
        archived = self.skills_root / ARCHIVE_DIR_NAME / name
        if not archived.is_dir():
            raise StoreError(f"no archived skill named {name!r}")
        target = self.skills_root / name
        if target.exists():
            raise StoreError(f"cannot un-archive — slot occupied: {target}")
        shutil.move(str(archived), str(target))
        self.usage.set_status(name, "active")
        return WriteResult(
            name=name, path=target / "SKILL.md", action="unarchived",
            history_snapshot=None, scan_verdict=Verdict.SAFE,
        )

    # ─── supporting-file writes ──────────────────────────────────

    def write_supporting_file(
        self, name: str, rel_path: str, content: str, *, executable: bool = False,
    ) -> Path:
        """Write a file under `references/`, `templates/`, `scripts/`,
        or `assets/` inside the skill bundle. Used by skill authors who
        want to ship runnable scripts or long reference docs.
        """
        skill_dir = self._must_exist(name).parent
        rel = Path(rel_path)
        _validate_subdir(rel)
        if len(content) > MAX_SKILL_CONTENT_CHARS * 4:  # 128KB soft cap for assets
            raise StoreError("supporting file too large")
        target = skill_dir / rel
        if target.is_symlink():
            raise StoreError("refusing to write to symlink")
        # Resolve and ensure containment.
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            resolved = target.resolve()
        except OSError:
            resolved = target
        if not str(resolved).startswith(str(skill_dir.resolve())):
            raise StoreError("supporting file escapes skill dir")
        _atomic_write_text(target, content)
        if executable:
            try:
                os.chmod(target, 0o755)
            except OSError as exc:
                logger.warning("chmod failed for %s: %s", target, exc)
        return target

    # ─── internals ───────────────────────────────────────────────

    def _must_exist(self, name: str) -> Path:
        self._validate_name(name)
        skill_md = self.skills_root / name / "SKILL.md"
        if not skill_md.is_file():
            raise StoreError(f"skill {name!r} not found at {skill_md}")
        return skill_md

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise StoreError("name is required")
        if len(name) > MAX_NAME_CHARS:
            raise StoreError(f"name exceeds {MAX_NAME_CHARS} chars")
        # The full character set is enforced by parse_skill_file at write
        # time; this is a fast pre-check to fail before touching the FS.
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            raise StoreError(
                f"name {name!r} must be lowercase a-z, 0-9, hyphens only"
            )
        if "--" in name or name.startswith("-") or name.endswith("-"):
            raise StoreError(f"name {name!r} has invalid hyphen placement")

    @staticmethod
    def _validate_description(description: str) -> None:
        if not isinstance(description, str) or not description.strip():
            raise StoreError("description is required")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise StoreError(
                f"description exceeds {MAX_DESCRIPTION_CHARS} chars "
                f"(got {len(description)})"
            )
