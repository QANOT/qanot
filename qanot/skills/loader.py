"""Skill discovery walk with mtime-stable caching.

`SkillLoader.load()` walks the workspace skill tree, parses every SKILL.md,
runs the security scanner, applies platform filtering, and returns a list
of validated SkillSpec objects. It caches results keyed by a manifest of
`{path: (mtime_ns, size)}` so a second load with no filesystem changes
returns the cached list without reparsing — that's what keeps Anthropic
prompt-cache hits alive across turns (Hermes' #25083-class regression).

The directory layout is `<workspace>/skills/<category-or-name>/[<name>/]SKILL.md`.
We accept both single-level (`skills/foo/SKILL.md`) and nested category
trees (`skills/category/foo/SKILL.md`) and infer the parent dir from where
SKILL.md actually lives.

Excluded dirs match Hermes' set (`.git`, `.github`, `.hub`, `.archive`)
plus `.history` (our own snapshot directory) and `.qanot` (reserved
sub-namespace). External read-only dirs can be supplied at construction
time; local-first wins on name collisions, matching Hermes' precedence so
overlay skills work as expected.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .guard import ScanResult, Verdict, scan_skill_bundle
from .spec import SkillSpec, SkillSpecError, parse_skill_file

logger = logging.getLogger(__name__)


EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".github", ".hub", ".archive", ".history",
    ".qanot", "__pycache__", "node_modules",
})


@dataclass
class LoadedSkill:
    """A spec plus the scan verdict and the dir it came from.

    Keeping the verdict alongside the spec lets the prompt builder choose
    whether to surface a warning ("scan: caution") without re-running the
    scanner on every prompt build.
    """

    spec: SkillSpec
    verdict: Verdict
    source_root: Path
    # Manifest entries that produced this skill, for cache validation.
    manifest_entries: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class LoadStats:
    """Summary returned alongside each load() call — useful for telemetry."""

    scanned_dirs: int = 0
    parsed_ok: int = 0
    spec_errors: int = 0
    rejected_dangerous: int = 0
    platform_filtered: int = 0
    duplicate_names: list[str] = field(default_factory=list)


class SkillLoader:
    """Stateful discovery + cache for one workspace.

    Usage:
        loader = SkillLoader(workspace_dir="/data/workspace",
                             external_dirs=["/usr/share/qanot/skills"])
        skills, stats = loader.load()
        # ...
        skills, stats = loader.load()   # cached path if no fs changes
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        external_dirs: list[str | Path] | None = None,
        reject_on_dangerous: bool = True,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.external_dirs: list[Path] = [Path(p) for p in (external_dirs or [])]
        self.reject_on_dangerous = reject_on_dangerous

        # Cache state.
        self._cached: list[LoadedSkill] = []
        self._cached_manifest: dict[str, tuple[int, int]] = {}
        self._cached_stats = LoadStats()

    # ─── public API ───────────────────────────────────────────────

    @property
    def local_skills_dir(self) -> Path:
        return self.workspace_dir / "skills"

    def load(self) -> tuple[list[LoadedSkill], LoadStats]:
        """Return discovered skills, using cache when manifest unchanged."""
        manifest = self._build_manifest()
        if manifest == self._cached_manifest and self._cached:
            return list(self._cached), self._cached_stats

        loaded, stats = self._do_load(manifest)
        self._cached = loaded
        self._cached_manifest = manifest
        self._cached_stats = stats
        return list(loaded), stats

    def invalidate(self) -> None:
        """Force the next `load()` to reparse — call after on-disk edits
        the loader cannot otherwise see (e.g., a fresh-clone of an
        external_dir that swaps inode without bumping mtime).
        """
        self._cached = []
        self._cached_manifest = {}
        self._cached_stats = LoadStats()

    def writable_root(self) -> Path:
        """Where new skills land. Always the local workspace dir, never
        external read-only ones — matches Hermes' rule and avoids the
        #19549 forked-skill bug.
        """
        return self.local_skills_dir

    # ─── manifest + walk ──────────────────────────────────────────

    def _build_manifest(self) -> dict[str, tuple[int, int]]:
        """Snapshot every SKILL.md (and parent dir mtime) we'd visit so a
        repeat call can detect changes cheaply. Keys are absolute paths.
        """
        manifest: dict[str, tuple[int, int]] = {}
        for root in self._all_roots():
            if not root.is_dir():
                continue
            for skill_md in self._iter_skill_files(root):
                try:
                    st = skill_md.stat()
                except OSError:
                    continue
                manifest[str(skill_md.resolve())] = (st.st_mtime_ns, st.st_size)
                # Parent dir mtime catches new sibling files (e.g., a new
                # references/REFERENCE.md) that the SKILL.md mtime alone
                # would miss.
                try:
                    pst = skill_md.parent.stat()
                    manifest[str(skill_md.parent.resolve()) + "/"] = (
                        pst.st_mtime_ns, pst.st_size,
                    )
                except OSError:
                    pass
        return manifest

    def _all_roots(self) -> list[Path]:
        roots = [self.local_skills_dir]
        roots.extend(self.external_dirs)
        return roots

    def _iter_skill_files(self, root: Path):
        """Yield each `SKILL.md` under `root`, skipping excluded subtrees."""
        try:
            walk_iter = os.walk(root, followlinks=False)
        except OSError as exc:
            logger.debug("cannot walk %s: %s", root, exc)
            return
        for dirpath, dirnames, filenames in walk_iter:
            # Mutate dirnames in place so os.walk doesn't descend into
            # excluded subtrees.
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
            if "SKILL.md" in filenames:
                yield Path(dirpath) / "SKILL.md"

    # ─── core load loop ───────────────────────────────────────────

    def _do_load(
        self, manifest: dict[str, tuple[int, int]],
    ) -> tuple[list[LoadedSkill], LoadStats]:
        stats = LoadStats()
        seen_names: dict[str, Path] = {}
        loaded: list[LoadedSkill] = []

        for root in self._all_roots():
            if not root.is_dir():
                continue
            for skill_md in self._iter_skill_files(root):
                stats.scanned_dirs += 1
                outcome = self._load_one(skill_md, root, manifest)
                if outcome is None:
                    stats.spec_errors += 1
                    continue
                loaded_skill, kind = outcome
                if kind == "dangerous":
                    stats.rejected_dangerous += 1
                    if self.reject_on_dangerous:
                        continue
                if kind == "platform":
                    stats.platform_filtered += 1
                    continue
                # Local skills win on collision — process them first by
                # virtue of root ordering (local is _all_roots[0]).
                prior = seen_names.get(loaded_skill.spec.name)
                if prior is not None and prior != skill_md:
                    stats.duplicate_names.append(loaded_skill.spec.name)
                    logger.info(
                        "skill name %r already loaded from %s; ignoring %s",
                        loaded_skill.spec.name, prior, skill_md,
                    )
                    continue
                seen_names[loaded_skill.spec.name] = skill_md
                loaded.append(loaded_skill)
                stats.parsed_ok += 1

        loaded.sort(key=lambda s: s.spec.name)
        return loaded, stats

    def _load_one(
        self, skill_md: Path, root: Path, manifest: dict[str, tuple[int, int]],
    ) -> tuple[LoadedSkill, str] | None:
        try:
            spec = parse_skill_file(skill_md)
        except SkillSpecError as exc:
            logger.warning("skill %s rejected: %s", skill_md, exc)
            return None

        if not spec.platform_compatible():
            return LoadedSkill(spec=spec, verdict=Verdict.SAFE,
                               source_root=root), "platform"

        scan = scan_skill_bundle(skill_md.parent)
        if scan.verdict == Verdict.DANGEROUS:
            logger.warning(
                "skill %s flagged DANGEROUS by scanner:\n%s",
                skill_md, scan.to_summary(),
            )
            return LoadedSkill(spec=spec, verdict=scan.verdict,
                               source_root=root), "dangerous"
        if scan.verdict == Verdict.CAUTION:
            logger.info(
                "skill %s flagged CAUTION:\n%s",
                spec.name, scan.to_summary(),
            )

        entries = {
            str(skill_md.resolve()): manifest.get(
                str(skill_md.resolve()), (0, 0),
            )
        }
        return LoadedSkill(
            spec=spec, verdict=scan.verdict, source_root=root,
            manifest_entries=entries,
        ), "ok"


# ─── back-compat shim ────────────────────────────────────────────


def discover_skills(workspace_dir: str) -> list[LoadedSkill]:
    """Stateless discovery entry point — used by callers that don't keep
    a SkillLoader instance. Equivalent to `SkillLoader(workspace_dir).load()[0]`.
    """
    return SkillLoader(workspace_dir).load()[0]
