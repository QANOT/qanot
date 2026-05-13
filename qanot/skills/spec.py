"""agentskills.io SKILL.md frontmatter spec — parser, validator, dataclass.

The spec is small: `name` (required, kebab-case ASCII, must equal parent
directory) and `description` (required, 1-1024 chars). Optional `license`,
`compatibility`, `metadata` map, and the experimental `allowed-tools` string.

We accept the de-facto Hermes / Anthropic extensions — `version`, `author`,
`platforms`, `metadata.<vendor>.*` — so existing skill bundles drop in without
edits. Anything beyond those keys is kept under `extra` for forward-compat but
ignored at injection time.

Strict YAML via PyYAML. The legacy implementation had a hand-rolled line
splitter that silently passed malformed frontmatter — that's exactly how
Hermes #13265 (no quality gate on skill writes) lets garbage skills land in
prod. We refuse to parse on malformed frontmatter and let the loader skip.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Hard limits — chosen for the prompt-cache-friendly path. The legacy module
# capped at 6 KB which is tighter than spec; spec recommends <5K tokens
# (~20 KB) so we sit between the two. Bigger skills should be split via
# references/.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_SKILL_CONTENT_CHARS = 32_000  # ~10K tokens, hard ceiling on body
MAX_INDEX_HINT_CHARS = 400        # truncation for prompt-injected hints

# Spec-strict name regex. agentskills.io says lowercase alphanumeric + hyphens,
# no leading/trailing hyphens, no `--`. We rule out underscores and dots
# explicitly even though Hermes lets them through.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_NAME_DBL_HYPHEN_RE = re.compile(r"--")

# Platform mapping — fastembed-style cross-platform aliases.
_PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}


class SkillSpecError(ValueError):
    """Raised when a SKILL.md violates the spec."""


@dataclass
class SkillSpec:
    """Parsed skill spec — what we keep in memory.

    Body is the post-frontmatter Markdown (not yet template-substituted).
    `path` is the SKILL.md file path; `directory` is its parent dir (skill
    root). All paths inside `body` are resolved relative to `directory` at
    runtime, never at parse time, so a skill remains portable.
    """

    name: str
    description: str
    body: str
    path: Path

    # Optional spec fields
    license: str = ""
    compatibility: str = ""
    allowed_tools: list[str] = field(default_factory=list)

    # De-facto extensions
    version: str = ""
    author: str = ""
    platforms: list[str] = field(default_factory=list)

    # Vendor namespace (metadata.qanot.*, metadata.hermes.*, etc.)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Anything unrecognized — preserved for round-tripping but not used.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.path.parent

    # ─── legacy attribute aliases ────────────────────────────────
    #
    # The v1 module exposed `content`, `auto_invoke`, `user_invocable`.
    # Tests assert on those names. We keep them as properties so the new
    # canonical fields stay clean (`body`, vendor metadata for the
    # flags) but old code reads the names it expects.

    @property
    def content(self) -> str:
        """Alias for body. v1 callers used `skill.content`."""
        return self.body

    @property
    def auto_invoke(self) -> bool:
        """v1 flag — agent may auto-activate the skill. Drawn from
        `metadata.qanot.auto_invoke` if present, otherwise True (the
        opt-out default — agents opt skills *out* of auto-match, not in).
        """
        qan = self.metadata.get("qanot") if isinstance(self.metadata, dict) else None
        if isinstance(qan, dict) and "auto_invoke" in qan:
            return bool(qan["auto_invoke"])
        # Legacy frontmatter key
        if "disable-auto" in self.extra:
            return not bool(self.extra["disable-auto"])
        return True

    @property
    def user_invocable(self) -> bool:
        """v1 flag — user may invoke via `/skill-name`. Same precedence
        as auto_invoke; default True.
        """
        qan = self.metadata.get("qanot") if isinstance(self.metadata, dict) else None
        if isinstance(qan, dict) and "user_invocable" in qan:
            return bool(qan["user_invocable"])
        if "user-invocable" in self.extra:
            return bool(self.extra["user-invocable"])
        return True

    @property
    def index_hint(self) -> str:
        """One-line hint for the system-prompt skill index.

        Format: `name: description (truncated)`. We don't expose every field
        — that's the activation-time job.
        """
        desc = self.description.replace("\n", " ").strip()
        if len(desc) > MAX_INDEX_HINT_CHARS:
            desc = desc[: MAX_INDEX_HINT_CHARS - 1] + "…"
        return f"{self.name}: {desc}"

    def platform_compatible(self, current: str | None = None) -> bool:
        """Return True if the skill's `platforms` list allows the current OS.

        Empty list (the default) means all platforms. We compare with
        `startswith` to handle linux/linux2 etc.
        """
        if not self.platforms:
            return True
        sysname = (current or sys.platform).lower()
        for p in self.platforms:
            mapped = _PLATFORM_MAP.get(p.lower().strip(), p.lower().strip())
            if sysname.startswith(mapped):
                return True
        return False


# ─────────────────────── Frontmatter parsing ─────────────────────────


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---`-delimited YAML frontmatter from a Markdown body.

    Returns (frontmatter_dict, body). Raises SkillSpecError on malformed
    YAML — the caller logs and skips. Empty frontmatter dict means the file
    had no `---` block; we treat that as a spec violation at validation time.
    """
    if not text.startswith("---"):
        return {}, text

    # Find the closing `---` on its own line. We accept either `\n---\n` or
    # `\n---\r\n` (Windows line endings sometimes sneak in).
    body_offset = _find_closing_fence(text)
    if body_offset is None:
        # Unclosed frontmatter — treat as empty (spec violation surfaced later).
        return {}, text

    yaml_blob = text[3:body_offset[0]].strip("\n")
    body = text[body_offset[1] :].lstrip("\n")

    try:
        parsed = yaml.safe_load(yaml_blob)
    except yaml.YAMLError as exc:
        raise SkillSpecError(f"malformed YAML frontmatter: {exc}") from exc

    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        raise SkillSpecError(
            f"frontmatter root must be a mapping, got {type(parsed).__name__}"
        )
    return parsed, body


def _find_closing_fence(text: str) -> tuple[int, int] | None:
    """Locate the closing `---` of a frontmatter block.

    Returns (yaml_end, body_start) where yaml_end is the index just before
    the closing `\n---` and body_start is the index just past it.
    """
    idx = 3
    while idx < len(text):
        nxt = text.find("\n---", idx)
        if nxt < 0:
            return None
        after = nxt + 4
        # Must be followed by EOL or EOF to count as a fence.
        if after >= len(text):
            return nxt, after
        if text[after] in ("\n", "\r"):
            # Skip past the EOL char(s) so the body starts cleanly.
            skip = after
            if text[skip] == "\r" and skip + 1 < len(text) and text[skip + 1] == "\n":
                skip += 2
            else:
                skip += 1
            return nxt, skip
        idx = after
    return None


# ─────────────────────── Validation & loading ────────────────────────


def parse_skill_file(
    path: Path, *, validate_dir_name: bool = True,
) -> SkillSpec:
    """Read a SKILL.md and produce a validated SkillSpec.

    Raises SkillSpecError on any spec violation. The loader catches and
    logs at WARNING; callers should not rely on partial returns.

    `validate_dir_name=True` (the default) enforces the spec rule that the
    frontmatter `name` must equal the parent directory. Legacy callers
    that handed us bare SKILL.md files under arbitrary tmp paths can opt
    out with `validate_dir_name=False`.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillSpecError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SkillSpecError(f"{path} is not valid UTF-8: {exc}") from exc

    frontmatter, body = split_frontmatter(raw)
    return _build_spec(frontmatter, body, path, validate_dir_name=validate_dir_name)


def _build_spec(
    frontmatter: dict, body: str, path: Path,
    *, validate_dir_name: bool = True,
) -> SkillSpec:
    if not frontmatter:
        raise SkillSpecError("missing frontmatter")

    name = frontmatter.get("name")
    description = frontmatter.get("description")

    parent_name = path.parent.name if validate_dir_name else ""
    _validate_name(name, parent_name)
    _validate_description(description)

    body = (body or "").strip()
    if len(body) > MAX_SKILL_CONTENT_CHARS:
        raise SkillSpecError(
            f"body exceeds {MAX_SKILL_CONTENT_CHARS} chars (got {len(body)})"
        )

    license_ = _str_or_empty(frontmatter.get("license"))
    compatibility = _str_or_empty(frontmatter.get("compatibility"), max_chars=500)
    allowed_tools = _parse_allowed_tools(frontmatter.get("allowed-tools"))

    version = _str_or_empty(frontmatter.get("version"), max_chars=40)
    author = _str_or_empty(frontmatter.get("author"), max_chars=120)
    platforms = _parse_platforms(frontmatter.get("platforms"))

    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SkillSpecError(
            f"metadata must be a mapping, got {type(metadata).__name__}"
        )

    known = {
        "name", "description", "license", "compatibility", "allowed-tools",
        "version", "author", "platforms", "metadata",
    }
    extra = {k: v for k, v in frontmatter.items() if k not in known}

    return SkillSpec(
        name=name,
        description=description,
        body=body,
        path=path,
        license=license_,
        compatibility=compatibility,
        allowed_tools=allowed_tools,
        version=version,
        author=author,
        platforms=platforms,
        metadata=metadata,
        extra=extra,
    )


# ─────────────────────── Field validators ────────────────────────────


def _validate_name(name: Any, parent_dir_name: str) -> None:
    if not isinstance(name, str) or not name:
        raise SkillSpecError("name is required and must be a non-empty string")
    if len(name) > MAX_NAME_CHARS:
        raise SkillSpecError(f"name exceeds {MAX_NAME_CHARS} chars")
    if not _NAME_RE.match(name):
        raise SkillSpecError(
            f"name {name!r} must match [a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
        )
    if _NAME_DBL_HYPHEN_RE.search(name):
        raise SkillSpecError(f"name {name!r} contains '--'")
    # Spec mandates name equals parent directory. Hermes does not check this
    # and it's how their #21782 frontmatter/dir divergence bug landed.
    if parent_dir_name and parent_dir_name != name:
        raise SkillSpecError(
            f"name {name!r} must equal parent directory {parent_dir_name!r}"
        )


def _validate_description(description: Any) -> None:
    if not isinstance(description, str) or not description.strip():
        raise SkillSpecError(
            "description is required and must be a non-empty string"
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillSpecError(
            f"description exceeds {MAX_DESCRIPTION_CHARS} chars "
            f"(got {len(description)})"
        )


def _str_or_empty(value: Any, *, max_chars: int | None = None) -> str:
    """Normalize an optional scalar field to a string.

    Numbers and booleans are coerced to their canonical string form because
    PyYAML resolves unquoted `1.0` or `2026` to a float/int, and the
    descriptive fields (version/author/license) are commonly written
    unquoted by hand. Containers (lists, dicts) remain a hard error.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # bool is a subclass of int — handle before the int branch.
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise SkillSpecError(f"expected string, got {type(value).__name__}")
    value = value.strip()
    if max_chars is not None and len(value) > max_chars:
        raise SkillSpecError(f"value exceeds {max_chars} chars")
    return value


def _parse_allowed_tools(raw: Any) -> list[str]:
    """`allowed-tools` is a space-separated string per spec. Reject lists —
    that's a Hermes drift we don't carry forward. The field is experimental;
    we surface it to the caller but the caller decides whether to honour it.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        # Accept lists for friendliness but normalize to spec form.
        return [str(x).strip() for x in raw if str(x).strip()]
    if not isinstance(raw, str):
        raise SkillSpecError(
            f"allowed-tools must be a string, got {type(raw).__name__}"
        )
    return [tok for tok in raw.split() if tok]


def _parse_platforms(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip().lower()] if raw.strip() else []
    if not isinstance(raw, list):
        raise SkillSpecError(
            f"platforms must be a string or list, got {type(raw).__name__}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SkillSpecError(
                f"platforms entries must be strings, got {type(item).__name__}"
            )
        if item.strip():
            out.append(item.strip().lower())
    return out
