"""Qanot skill subsystem — agentskills.io-compliant SKILL.md handling.

Replaces the flat `qanot/skills.py` module from earlier versions. The public
surface is preserved so existing call sites in `qanot/agent/preprocessing.py`,
`qanot/agent/agent.py`, and the test suite continue to import the same names.

The split:
    .spec       — SKILL.md frontmatter parser + Skill dataclass + validators
    .guard      — prompt-injection / structural scanner
    .loader     — filesystem discovery walk with mtime-stable caching
    .usage      — per-skill usage tracking (.usage.json)
    .index      — system-prompt build_skill_index / match / format

The legacy hand-rolled YAML parser and keyword-only Skill class live on at
`._legacy`. We keep the module around so historical imports do not error,
but new code should depend on the strict spec.
"""

from __future__ import annotations

# Public symbols — the names callers used to import from `qanot.skills`.
from .spec import (
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MAX_SKILL_CONTENT_CHARS,
    SkillSpec,
    SkillSpecError,
    parse_skill_file,
    split_frontmatter,
)
from .guard import (
    ScanResult,
    Verdict,
    scan_skill_bundle,
    scan_text,
)
from .loader import (
    EXCLUDED_DIR_NAMES,
    LoadedSkill,
    LoadStats,
    SkillLoader,
    discover_skills,
)
from .usage import (
    USAGE_FILE_NAME,
    SkillUsage,
    UsageStore,
    days_since,
)
from .store import (
    ALLOWED_SUBDIRS,
    ARCHIVE_DIR_NAME,
    HISTORY_DIR_NAME,
    SkillStore,
    StoreError,
    WriteResult,
)
from .gate import (
    DEFAULT_MIN_TRAJECTORY_TOKENS,
    DEFAULT_SEMANTIC_REJECT_SCORE,
    DEFAULT_SEMANTIC_WARN_SCORE,
    GateResult,
    pre_create_check,
)
from .index import (
    MAX_ACTIVE_SKILLS,
    MAX_INDEX_ENTRIES,
    build_skill_index,
    format_active_skills,
    match_skills,
)


# ─── Back-compat aliases ─────────────────────────────────────────
#
# The pre-package module exposed `Skill`, `MAX_SKILL_CHARS`, `_parse_skill`,
# `_split_frontmatter`. We keep them pointing at the new symbols so old
# imports (`from qanot.skills import Skill, MAX_SKILL_CHARS`) keep working.
# Tests in tests/test_skills.py specifically import the leading-underscore
# helpers; we expose those too.

Skill = SkillSpec  # type: ignore[assignment]  # historical name
MAX_SKILL_CHARS = MAX_SKILL_CONTENT_CHARS

_split_frontmatter = split_frontmatter


def _parse_skill(path):
    """Legacy parser shim — returns None on spec violations (the v1 contract).

    The new `parse_skill_file` raises `SkillSpecError`. Old callers expect
    a None-return on bad input, so we translate here. The conversion lets
    `tests/test_skills.py` keep passing without rewrites. Legacy callers
    also don't put SKILL.md under a name-matching parent dir (the tmp_path
    pattern in pytest), so we relax the directory-name check too.
    """
    try:
        return parse_skill_file(path, validate_dir_name=False)
    except SkillSpecError:
        return None


__all__ = [
    # Spec
    "SkillSpec", "SkillSpecError", "parse_skill_file", "split_frontmatter",
    "MAX_NAME_CHARS", "MAX_DESCRIPTION_CHARS", "MAX_SKILL_CONTENT_CHARS",
    # Guard
    "ScanResult", "Verdict", "scan_skill_bundle", "scan_text",
    # Loader
    "SkillLoader", "LoadedSkill", "LoadStats", "discover_skills",
    "EXCLUDED_DIR_NAMES",
    # Usage
    "SkillUsage", "UsageStore", "USAGE_FILE_NAME", "days_since",
    # Store
    "SkillStore", "StoreError", "WriteResult",
    "ALLOWED_SUBDIRS", "HISTORY_DIR_NAME", "ARCHIVE_DIR_NAME",
    # Gate (AAMC cost-gated improvement loop)
    "GateResult", "pre_create_check",
    "DEFAULT_MIN_TRAJECTORY_TOKENS", "DEFAULT_SEMANTIC_REJECT_SCORE",
    "DEFAULT_SEMANTIC_WARN_SCORE",
    # Index
    "build_skill_index", "match_skills", "format_active_skills",
    "MAX_ACTIVE_SKILLS", "MAX_INDEX_ENTRIES",
    # Back-compat
    "Skill", "MAX_SKILL_CHARS", "_parse_skill", "_split_frontmatter",
]
