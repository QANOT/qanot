"""Skill registry — install / search / update / verify SKILL.md bundles.

This is the markdown-bundle sibling of ``qanot/plugins/registry.py``.
Same proven static-registry shape (GitHub-raw ``index.json``, git-clone
install, SHA256 lock file, 3-tier resolution) with the executable-code
primitives swapped for the markdown primitives already in
``qanot/skills/`` (``spec.parse_skill_file`` for validation,
``guard.scan_skill_bundle`` for the security scan).

Design follows the May-2026 production research:

- **Distribution:** single generated ``index.json`` in the
  ``QANOT/qanot-skills`` repo (asdf-style: per-skill ``skill.json`` is
  the contributor source of truth; CI regenerates the index so PRs
  never hand-edit it and never conflict).
- **Integrity:** git ``commit`` pin + ``sha256`` content hash, verified
  on install with hard-fail on mismatch (npm / Claude-Code model). No
  PKI / signing — the commit pin is the tamper-evidence.
- **Trust tiers:** ``official`` (we authored, in-repo), ``community``
  (PR-reviewed, merged), ``unverified`` (requires ``--allow-unverified``).
- **Scripts stripped by default.** Markdown skills should not ship
  executables; dropping ``scripts/`` kills the ``curl|bash`` /
  ``npx -y`` vectors at the door. Opt back in with ``with_scripts=True``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Generated index.json served via GitHub raw — mirrors the plugin registry.
DEFAULT_SKILL_REGISTRY_URL = (
    "https://raw.githubusercontent.com/QANOT/qanot-skills/main/index.json"
)

USER_SKILLS_DIR = Path.home() / ".qanot" / "skills"
SKILL_LOCK_FILE = "skills.lock.json"

# Trust tiers. `unverified` cannot install without an explicit override.
VALID_TIERS = ("official", "community", "unverified")

# Subdirectory dropped on install unless the caller opts in. Markdown
# skills don't need executables; the absence of scripts/ neutralises
# the curl|bash and npx-y supply-chain vectors before they can run.
STRIPPED_SUBDIR = "scripts"


# ─── registry index entry ────────────────────────────────────────


@dataclass
class SkillEntry:
    """One entry in the registry ``index.json``."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    author: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    tier: str = "community"
    min_qanot_version: str = ""
    license: str = ""
    git_url: str = ""
    subdir: str = ""        # path within the repo to the skill bundle
    commit: str = ""        # 40-hex git commit pin (integrity)
    sha256: str = ""        # bundle content hash (integrity)

    @classmethod
    def from_dict(cls, data: dict) -> "SkillEntry":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            version=data.get("version", "0.1.0"),
            author=data.get("author", ""),
            category=data.get("category", ""),
            tags=data.get("tags", []) or [],
            tier=data.get("tier", "community"),
            min_qanot_version=data.get("min_qanot_version", ""),
            license=data.get("license", ""),
            git_url=data.get("git_url", ""),
            subdir=data.get("subdir", ""),
            commit=data.get("commit", ""),
            sha256=data.get("sha256", ""),
        )


@dataclass
class SkillLockEntry:
    """A locked skill installation record (skills.lock.json)."""

    name: str
    version: str = ""
    source: str = "registry"     # registry | git | local
    source_url: str = ""
    commit: str = ""
    sha256: str = ""
    installed_at: str = ""
    install_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "source_url": self.source_url,
            "commit": self.commit,
            "sha256": self.sha256,
            "installed_at": self.installed_at,
            "install_dir": self.install_dir,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillLockEntry":
        return cls(**{
            k: data.get(k, "") for k in cls.__dataclass_fields__
        })


# ─── lock file ───────────────────────────────────────────────────


def _lock_path(skills_dir: Path) -> Path:
    return skills_dir / SKILL_LOCK_FILE


def read_skill_lock(skills_dir: Path) -> dict[str, SkillLockEntry]:
    path = _lock_path(skills_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {
            name: SkillLockEntry.from_dict(entry)
            for name, entry in raw.get("skills", {}).items()
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to read skill lock file: %s", e)
        return {}


def write_skill_lock(
    skills_dir: Path, entries: dict[str, SkillLockEntry],
) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "skills": {n: e.to_dict() for n, e in entries.items()},
    }
    _lock_path(skills_dir).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ─── content hash (matches plugins/security.compute_plugin_hash) ──


def compute_skill_hash(skill_dir: Path) -> str:
    """Deterministic SHA256 over all bundle files (sorted by relpath).

    Identical algorithm to ``compute_plugin_hash`` so the integrity
    semantics are consistent across the two registries. ``.git`` and
    the lock file are excluded so the hash is stable across installs.
    """
    hasher = hashlib.sha256()
    files = sorted(
        f for f in skill_dir.rglob("*")
        if f.is_file()
        and ".git" not in f.parts
        and f.name != SKILL_LOCK_FILE
    )
    for fpath in files:
        rel = str(fpath.relative_to(skill_dir))
        hasher.update(rel.encode("utf-8"))
        hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


# ─── registry fetch / search ─────────────────────────────────────


def fetch_skill_registry(
    registry_url: str = DEFAULT_SKILL_REGISTRY_URL,
) -> list[SkillEntry]:
    try:
        req = urllib.request.Request(
            registry_url, headers={"User-Agent": "qanot"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [SkillEntry.from_dict(s) for s in data.get("skills", [])]
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to fetch skill registry: %s", e)
        return []


def search_skill_registry(
    query: str, registry_url: str = DEFAULT_SKILL_REGISTRY_URL,
) -> list[SkillEntry]:
    entries = fetch_skill_registry(registry_url)
    q = query.lower()
    out = []
    for e in entries:
        hay = f"{e.name} {e.description} {e.category} {' '.join(e.tags)}".lower()
        if q in hay:
            out.append(e)
    return out


# ─── url helpers (mirror plugins/registry) ───────────────────────


def _is_git_url(source: str) -> bool:
    return (
        source.startswith(("https://", "http://", "git@"))
        or source.endswith(".git")
    )


def _is_plain_http(source: str) -> bool:
    return source.startswith("http://") and not source.startswith("https://")


# ─── install ─────────────────────────────────────────────────────


def install_skill(
    source: str,
    skills_dir: Path,
    *,
    registry_url: str = DEFAULT_SKILL_REGISTRY_URL,
    user_level: bool = False,
    with_scripts: bool = False,
    allow_unverified: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """Install a skill by registry name or git URL.

    Returns ``(success, message)``. Never raises — the CLI prints the
    message verbatim.
    """
    target_dir = USER_SKILLS_DIR if user_level else skills_dir

    if _is_plain_http(source):
        return False, "Refusing plain HTTP — use HTTPS (MITM protection)."

    entry: SkillEntry | None = None
    if _is_git_url(source):
        git_url, subdir, commit = source, "", ""
    else:
        entries = fetch_skill_registry(registry_url)
        entry = next((e for e in entries if e.name == source), None)
        if entry is None:
            return False, (
                f"Skill '{source}' not found. Try "
                f"'qanot skill search {source}' or pass a git URL."
            )
        if entry.tier not in VALID_TIERS:
            return False, f"Skill '{source}' has invalid tier {entry.tier!r}."
        if entry.tier == "unverified" and not allow_unverified:
            return False, (
                f"Skill '{source}' is UNVERIFIED. Re-run with "
                f"--allow-unverified if you trust the author."
            )
        if not entry.git_url:
            return False, f"Registry entry '{source}' has no git_url."
        if _is_plain_http(entry.git_url):
            return False, f"Registry entry '{source}' uses insecure HTTP."
        git_url, subdir, commit = entry.git_url, entry.subdir, entry.commit

    return _install_from_git(
        git_url=git_url,
        subdir=subdir,
        commit=commit,
        registry_entry=entry,
        target_dir=target_dir,
        with_scripts=with_scripts,
        force=force,
    )


def _install_from_git(
    *,
    git_url: str,
    subdir: str,
    commit: str,
    registry_entry: SkillEntry | None,
    target_dir: Path,
    with_scripts: bool,
    force: bool,
) -> tuple[bool, str]:
    from qanot.skills.spec import SkillSpecError, parse_skill_file
    from qanot.skills.guard import Verdict, scan_skill_bundle

    # Skill name comes from the registry entry, else the repo/subdir tail.
    raw_name = (
        registry_entry.name if registry_entry
        else (subdir or git_url).rstrip("/").rsplit("/", 1)[-1]
    )
    name, safe = _sanitize_name(raw_name)
    if not safe:
        return False, f"Unsafe skill name {raw_name!r}."

    dest = target_dir / name
    if dest.exists() and not force:
        return False, (
            f"Skill '{name}' already exists at {dest}. "
            f"Use --force to overwrite."
        )

    tmp_root = Path(tempfile.mkdtemp(prefix=f"qanot-skill-{name}-"))
    clone_dir = tmp_root / "repo"
    try:
        # depth=1, no submodules — same hardening as the plugin path.
        cmd = [
            "git", "clone", "--depth=1", "--no-recurse-submodules",
            git_url, str(clone_dir),
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            return False, "git is not installed."
        except subprocess.TimeoutExpired:
            return False, "git clone timed out (60s)."
        if res.returncode != 0:
            return False, f"git clone failed: {res.stderr.strip()[:200]}"

        # Commit pin is BEST-EFFORT, not the integrity gate. A
        # self-regenerating registry (CI rewrites index.json on every
        # push) can't pin to a commit that contains its own index —
        # chicken-and-egg. The real tamper-evidence is the sha256
        # content hash verified below (npm `integrity` model). So:
        # try to fetch+checkout the pinned commit for reproducibility;
        # if the shallow clone doesn't have it, fall back to the cloned
        # HEAD and let the sha256 check be the hard gate.
        if commit:
            chk = subprocess.run(
                ["git", "-C", str(clone_dir), "checkout", "--quiet", commit],
                capture_output=True, text=True, timeout=30,
            )
            if chk.returncode != 0:
                fetched = subprocess.run(
                    ["git", "-C", str(clone_dir), "fetch", "--depth=1",
                     "origin", commit],
                    capture_output=True, text=True, timeout=30,
                )
                if fetched.returncode == 0:
                    subprocess.run(
                        ["git", "-C", str(clone_dir), "checkout",
                         "--quiet", commit],
                        capture_output=True, text=True, timeout=30,
                    )
                else:
                    logger.info(
                        "skill install: pinned commit %s unreachable in "
                        "shallow clone — using HEAD, sha256 is the gate",
                        commit[:12],
                    )

        bundle_src = clone_dir / subdir if subdir else clone_dir
        if not (bundle_src / "SKILL.md").is_file():
            return False, (
                "No SKILL.md in the cloned bundle — not a valid skill."
            )

        # Strip .git, skill.json (registry metadata — NOT part of the
        # runtime bundle; the CI hash excludes it too, so the integrity
        # check only matches if we drop it here), and (by default)
        # scripts/. Done BEFORE validation/scan/hash so all three reflect
        # exactly what lands on disk.
        shutil.rmtree(bundle_src / ".git", ignore_errors=True)
        (bundle_src / "skill.json").unlink(missing_ok=True)
        if not with_scripts:
            shutil.rmtree(bundle_src / STRIPPED_SUBDIR, ignore_errors=True)

        # Validate frontmatter strictly (spec.py) — refuse on violation.
        try:
            parse_skill_file(bundle_src / "SKILL.md")
        except SkillSpecError as exc:
            return False, f"SKILL.md failed spec validation: {exc}"

        # Security scan every text file (guard.py). Refuse on DANGEROUS.
        scan = scan_skill_bundle(bundle_src)
        if scan.verdict == Verdict.DANGEROUS:
            return False, (
                "Security scan REFUSED this skill (DANGEROUS):\n"
                + scan.to_summary(limit=5)
            )

        # Integrity: recompute hash, hard-fail if the registry pinned one
        # and it doesn't match (npm EINTEGRITY equivalent).
        #
        # The registry sha256 is computed over the CANONICAL artifact —
        # the default no-scripts bundle. A `--with-scripts` install is a
        # deliberately different artifact, so we skip the hard match in
        # that mode (the user explicitly opted into the riskier path;
        # the guard scan above still ran on the real on-disk content).
        actual_hash = compute_skill_hash(bundle_src)
        if (
            not with_scripts
            and registry_entry
            and registry_entry.sha256
            and registry_entry.sha256 != actual_hash
        ):
            return False, (
                "Integrity check FAILED: bundle sha256 mismatch.\n"
                f"  expected {registry_entry.sha256[:16]}…\n"
                f"  actual   {actual_hash[:16]}…"
            )

        # Commit into place atomically: rmtree existing on --force, then
        # move the validated bundle.
        target_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(bundle_src), str(dest))

        # Lock file.
        lock = read_skill_lock(target_dir)
        lock[name] = SkillLockEntry(
            name=name,
            version=registry_entry.version if registry_entry else "",
            source="registry" if registry_entry else "git",
            source_url=git_url,
            commit=commit,
            sha256=actual_hash,
            installed_at=datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
            install_dir=str(dest),
        )
        write_skill_lock(target_dir, lock)

        warn = (
            "" if scan.verdict == Verdict.SAFE
            else f"  (scan: {scan.verdict.value} — review before relying on it)"
        )
        return True, f"OK installed '{name}' → {dest}{warn}"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# ─── remove / verify / list / info ───────────────────────────────


def remove_skill(name: str, skills_dir: Path) -> tuple[bool, str]:
    safe_name, ok = _sanitize_name(name)
    if not ok:
        return False, f"Unsafe skill name {name!r}."
    dest = skills_dir / safe_name
    if not dest.is_dir():
        return False, f"Skill '{safe_name}' is not installed in {skills_dir}."
    shutil.rmtree(dest)
    lock = read_skill_lock(skills_dir)
    lock.pop(safe_name, None)
    write_skill_lock(skills_dir, lock)
    return True, f"Removed '{safe_name}'."


def verify_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """Recompute hashes vs the lock file. Returns drift report rows."""
    lock = read_skill_lock(skills_dir)
    rows: list[dict[str, Any]] = []
    for name, entry in lock.items():
        path = skills_dir / name
        if not path.is_dir():
            rows.append({"name": name, "status": "missing"})
            continue
        actual = compute_skill_hash(path)
        if not entry.sha256:
            rows.append({"name": name, "status": "no-hash-on-record"})
        elif actual == entry.sha256:
            rows.append({"name": name, "status": "ok"})
        else:
            rows.append({
                "name": name, "status": "drift",
                "expected": entry.sha256[:16],
                "actual": actual[:16],
            })
    return rows


def skill_registry_info(
    name: str, registry_url: str = DEFAULT_SKILL_REGISTRY_URL,
) -> SkillEntry | None:
    entries = fetch_skill_registry(registry_url)
    return next((e for e in entries if e.name == name), None)


# ─── internals ───────────────────────────────────────────────────


def _sanitize_name(raw: str) -> tuple[str, bool]:
    """Reject directory traversal / unsafe chars. Mirror of
    ``plugins.security.sanitize_plugin_name`` semantics: lowercase
    alnum + hyphen only, no separators, no dots.
    """
    name = (raw or "").strip().lower()
    if not name or name in (".", ".."):
        return name, False
    if "/" in name or "\\" in name or ".." in name:
        return name, False
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
        return name, False
    return name, True
