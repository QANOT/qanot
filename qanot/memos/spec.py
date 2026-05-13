"""Memo schema — one-fact-per-file with frontmatter, mirrors Claude Code v2.1.139.

Claude Code's reverse-engineered system prompt for memory (Piebald-AI mirror,
``system-prompt-memory-instructions.md``) defines this exact shape:

    ---
    name: <short-kebab-case-slug>
    description: <one-line summary — used to decide relevance during recall>
    metadata:
      type: user | feedback | project | reference
    ---
    <the fact; feedback/project also follow with **Why:** and **How to apply:** lines>

We adopt it verbatim because:

  1. The ``description`` field is what the router reads to pick relevant memos
     per turn. Burying the description inside a long bullet, the way the old
     ``MEMORY.md`` did, was the root cause of the 2026-05-13 title-format
     regression — the router can't select what it can't see.

  2. ``metadata.type`` is the rules-vs-facts distinction the project has been
     asking for. Anthropic's docs explicitly leave typing to the developer; we
     use Claude Code's recipe so a future migration to the official
     ``memory_20250818`` tool is a no-op rename.

  3. ``feedback`` and ``project`` types carry ``**Why:**`` + ``**How to apply:**``
     lines so the model judges edge cases against the original intent instead
     of pattern-matching the fact alone. This is the lesson from CAI
     self-critique — give the model the reason, not just the rule.

Storage policy: one memo = one file at ``<workspace>/memories/<slug>.md``.
File name and ``name`` field must match. The ``/memories/`` directory is the
same one ``qanot/tools/memory_tool.py`` exposes via the Anthropic
``memory_20250818`` protocol — no new directory, no conflicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class MemoType(str, Enum):
    """The four canonical memo types from Claude Code v2.1.139.

    The router uses type as a soft prior — e.g., a ``user`` fact (identity,
    role, preferences) is more often relevant than a ``project`` snapshot
    (which decays fast). The model also uses type when deciding whether to
    apply the memo verbatim or as background context.
    """

    USER = "user"            # who the user is — role, accessibility, language
    FEEDBACK = "feedback"    # corrections / preferences / style rules (HARD)
    PROJECT = "project"      # current initiative facts, decisions, deadlines
    REFERENCE = "reference"  # pointers to external systems (Linear, Grafana…)


# Hard limits chosen to keep ``<system-reminder>`` blocks within the recency
# window the router targets. A memo larger than these is almost always
# trying to be a topic file instead — split before saving.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 200    # one line; longer descriptions waste router tokens
MAX_BODY_CHARS = 4_000         # ~1K tokens — fits 5 memos in <5K reminder budget

# Strict kebab-case for slugs, identical to the agentskills.io name regex so
# memos and skills share validation primitives.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_NAME_DBL_HYPHEN_RE = re.compile(r"--")


class MemoSpecError(ValueError):
    """Raised when a memo file violates the schema."""


@dataclass
class MemoSpec:
    """In-memory representation of a single memo file.

    ``path`` is the on-disk location (e.g.
    ``workspace/memories/feedback-title-date-format.md``). ``directory`` is
    always ``path.parent`` and exists only to keep the spec API symmetric
    with ``SkillSpec``. ``body`` is post-frontmatter Markdown.

    Scope hierarchy (Global / User / Thread / User+Thread) is encoded in
    two optional frontmatter fields:

    - ``metadata.user``    — the memo applies only when ``current_user_id``
                             matches. Missing/empty = applies to all users.
    - ``metadata.thread``  — the memo applies only when ``current_thread``
                             matches. Missing/empty = applies in all threads.

    Both absent → **Global** memo (applies always). Both present → memo
    fires only for that specific user in that specific thread. The router
    pre-filters memos via ``matches_scope`` before calling the LLM
    selector, so out-of-scope memos cost zero router tokens.
    """

    name: str
    description: str
    type: MemoType
    body: str
    path: Path

    # Scope fields. Empty string = "no scope restriction at this level".
    # We use empty-string-as-sentinel rather than None so the YAML round-trip
    # is symmetric: a frontmatter that lacks ``metadata.user`` parses to
    # ``user_scope == ""``, and that's also what the renderer emits when
    # the caller omits the kwarg.
    user_scope: str = ""
    thread_scope: str = ""

    # Multimodal fields. Populated when the memo was derived from a
    # non-text source (voice note, image, video). Empty strings = pure
    # text memo. The original media file lives at
    # ``<workspace>/<media_path>``; the body holds the transcript or
    # description that the router actually embeds. Both are kept so
    # the agent can replay the original via tg_send_voice/photo when
    # the user asks "play it back". ``duration_sec`` is 0 for images.
    media_type: str = ""        # voice | image | video | "" (text)
    media_path: str = ""        # workspace-relative path
    duration_sec: int = 0       # 0 for images, runtime for audio/video

    # Optional structured fields parsed out of the body — populated by
    # ``parse_memo_file`` for ``feedback``/``project`` types so the router
    # can surface "Why" / "How to apply" without re-parsing the markdown.
    why: str = ""
    how_to_apply: str = ""

    # Anything in frontmatter that isn't part of the canonical schema —
    # kept for round-tripping when we re-write the file.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def filename(self) -> str:
        return f"{self.name}.md"

    @property
    def is_global(self) -> bool:
        return not self.user_scope and not self.thread_scope

    @property
    def scope_label(self) -> str:
        """Compact human-readable scope tag for logs and index hints.

        ``global`` | ``user:1545`` | ``thread:kunlik`` | ``user:1545+thread:kunlik``.
        Truncates long IDs so log lines stay scannable.
        """
        if self.is_global:
            return "global"
        parts: list[str] = []
        if self.user_scope:
            uid = self.user_scope[:10] + ("…" if len(self.user_scope) > 10 else "")
            parts.append(f"user:{uid}")
        if self.thread_scope:
            tid = self.thread_scope[:14] + ("…" if len(self.thread_scope) > 14 else "")
            parts.append(f"thread:{tid}")
        return "+".join(parts)

    def matches_scope(
        self, user_id: str | None = None, thread_id: str | None = None,
    ) -> bool:
        """Return True if this memo is in-scope for the given context.

        Rules:
          - No memo scope → in-scope for any caller.
          - ``user_scope`` set → only fires when ``user_id`` matches.
          - ``thread_scope`` set → only fires when ``thread_id`` matches.
          - Both set → both must match (AND, not OR).

        Empty strings on the caller side are treated as "no value" — i.e.,
        a scope-bearing memo is filtered out when the caller can't prove
        the match. This is the safe default: a thread-scoped style rule
        must NOT leak into a different thread just because the caller
        forgot to plumb the thread id through.
        """
        if self.user_scope:
            if not user_id or user_id != self.user_scope:
                return False
        if self.thread_scope:
            if not thread_id or thread_id != self.thread_scope:
                return False
        return True

    @property
    def index_hint(self) -> str:
        """One-line summary the router sees during selection.

        Format: ``<name> [<type>] (<scope>): <description>``. The scope
        tag is informational for the LLM router — when the caller filters
        out-of-scope memos before the router runs (the production path),
        the tag is redundant but cheap; when we surface the memo to a
        human (CLI, audit log), the tag explains why the memo exists.
        """
        return (
            f"{self.name} [{self.type.value}] ({self.scope_label}): "
            f"{self.description}"
        )


# ─── Frontmatter parsing ──────────────────────────────────────────


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``---``-fenced YAML frontmatter from a Markdown body.

    Raises ``MemoSpecError`` on malformed YAML — the loader catches and
    logs. Returning ``({}, text)`` would let a typo-ridden file slip into
    production with silently empty metadata; we'd rather fail loud.
    """
    if not text.startswith("---"):
        return {}, text

    bounds = _find_closing_fence(text)
    if bounds is None:
        return {}, text
    yaml_end, body_start = bounds

    yaml_blob = text[3:yaml_end].strip("\n")
    body = text[body_start:].lstrip("\n")
    try:
        parsed = yaml.safe_load(yaml_blob)
    except yaml.YAMLError as exc:
        raise MemoSpecError(f"malformed YAML frontmatter: {exc}") from exc
    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        raise MemoSpecError(
            f"frontmatter must be a mapping, got {type(parsed).__name__}"
        )
    return parsed, body


def _find_closing_fence(text: str) -> tuple[int, int] | None:
    """Locate the closing ``\\n---`` of a frontmatter block.

    Returns (yaml_end_index, body_start_index) or None on missing fence.
    """
    idx = 3
    while idx < len(text):
        nxt = text.find("\n---", idx)
        if nxt < 0:
            return None
        after = nxt + 4
        if after >= len(text):
            return nxt, after
        if text[after] in ("\n", "\r"):
            skip = after
            if text[skip] == "\r" and skip + 1 < len(text) and text[skip + 1] == "\n":
                skip += 2
            else:
                skip += 1
            return nxt, skip
        idx = after
    return None


# ─── File parser / writer ─────────────────────────────────────────


def parse_memo_file(path: Path) -> MemoSpec:
    """Read a memo file and produce a validated ``MemoSpec``.

    Raises ``MemoSpecError`` on any schema violation.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MemoSpecError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise MemoSpecError(f"{path} is not UTF-8: {exc}") from exc

    fm, body = split_frontmatter(raw)
    return _build_spec(fm, body, path)


def _build_spec(fm: dict, body: str, path: Path) -> MemoSpec:
    if not fm:
        raise MemoSpecError("missing frontmatter")

    name = fm.get("name")
    description = fm.get("description")
    metadata = fm.get("metadata") or {}

    _validate_name(name, path.stem)
    _validate_description(description)
    memo_type = _validate_type(metadata)
    user_scope = _validate_scope(metadata.get("user"), "user")
    thread_scope = _validate_scope(metadata.get("thread"), "thread")

    # Multimodal fields — same shape as scope fields (string with "" default).
    # Validate but don't enforce values here; the multimodal module checks
    # media_type against a known set when CREATING memos.
    media_type = _validate_scope(metadata.get("media_type"), "media_type")
    media_path = _validate_scope(metadata.get("media_path"), "media_path")
    raw_duration = metadata.get("duration_sec") or 0
    try:
        duration_sec = int(raw_duration)
    except (TypeError, ValueError):
        raise MemoSpecError(
            f"metadata.duration_sec must be an integer, got {raw_duration!r}"
        )

    body = (body or "").strip()
    if len(body) > MAX_BODY_CHARS:
        raise MemoSpecError(
            f"body exceeds {MAX_BODY_CHARS} chars (got {len(body)})"
        )

    why, how_to_apply = _extract_why_how(body)

    known = {"name", "description", "metadata"}
    extra = {k: v for k, v in fm.items() if k not in known}

    return MemoSpec(
        name=name,
        description=description,
        type=memo_type,
        body=body,
        path=path,
        user_scope=user_scope,
        thread_scope=thread_scope,
        media_type=media_type,
        media_path=media_path,
        duration_sec=duration_sec,
        why=why,
        how_to_apply=how_to_apply,
        extra=extra,
    )


def _validate_scope(value: Any, field_name: str) -> str:
    """Normalize an optional scope field to a string.

    Numeric scope values (Telegram user IDs like ``1545224574``) parse as
    int from YAML if unquoted — we coerce them to strings so downstream
    comparisons are always string-to-string. Empty / None / whitespace
    becomes the empty string sentinel ("no scope at this level").
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # Bool subclasses int — reject loud rather than coerce to "True"/"False".
        raise MemoSpecError(
            f"metadata.{field_name} must be a string or number, got bool"
        )
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise MemoSpecError(
            f"metadata.{field_name} must be a string, got "
            f"{type(value).__name__}"
        )
    return value.strip()


# ─── Validators ───────────────────────────────────────────────────


def _validate_name(name: Any, filename_stem: str) -> None:
    if not isinstance(name, str) or not name:
        raise MemoSpecError("name is required and must be a non-empty string")
    if len(name) > MAX_NAME_CHARS:
        raise MemoSpecError(f"name exceeds {MAX_NAME_CHARS} chars")
    if not _NAME_RE.match(name):
        raise MemoSpecError(
            f"name {name!r} must match [a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
        )
    if _NAME_DBL_HYPHEN_RE.search(name):
        raise MemoSpecError(f"name {name!r} contains '--'")
    # ``name`` must equal the file stem so the router can resolve a memo
    # by its name without re-reading frontmatter. Same rule we enforce
    # on skills (see ``qanot/skills/spec.py``).
    if filename_stem and filename_stem != name:
        raise MemoSpecError(
            f"name {name!r} must equal filename stem {filename_stem!r}"
        )


def _validate_description(description: Any) -> None:
    if not isinstance(description, str) or not description.strip():
        raise MemoSpecError(
            "description is required and must be a non-empty string"
        )
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise MemoSpecError(
            f"description exceeds {MAX_DESCRIPTION_CHARS} chars "
            f"(the router reads this field on every turn — keep it tight)"
        )


def _validate_type(metadata: Any) -> MemoType:
    if not isinstance(metadata, dict):
        raise MemoSpecError(
            f"metadata must be a mapping, got {type(metadata).__name__}"
        )
    type_str = metadata.get("type")
    if not type_str:
        raise MemoSpecError("metadata.type is required")
    if not isinstance(type_str, str):
        raise MemoSpecError(
            f"metadata.type must be a string, got {type(type_str).__name__}"
        )
    try:
        return MemoType(type_str.lower().strip())
    except ValueError as exc:
        valid = ", ".join(t.value for t in MemoType)
        raise MemoSpecError(
            f"metadata.type must be one of {valid}; got {type_str!r}"
        ) from exc


# ─── Body convention helpers ──────────────────────────────────────


_WHY_RE = re.compile(r"^\s*\*\*Why:\*\*\s*(.+)$", re.MULTILINE)
_HOW_RE = re.compile(r"^\s*\*\*How to apply:\*\*\s*(.+)$", re.MULTILINE)


def _extract_why_how(body: str) -> tuple[str, str]:
    """Pull ``**Why:**`` and ``**How to apply:**`` lines out of the body.

    The convention is Claude Code's: feedback/project memos lead with the
    rule/fact, then a blank line, then these two lines. We extract them
    so the router can surface them without re-parsing markdown.
    """
    why_match = _WHY_RE.search(body)
    how_match = _HOW_RE.search(body)
    why = why_match.group(1).strip() if why_match else ""
    how = how_match.group(1).strip() if how_match else ""
    return why, how


# ─── Rendering — writes go through here for consistency ──────────


def render_memo(
    name: str,
    description: str,
    memo_type: MemoType | str,
    body: str,
    *,
    user_scope: str = "",
    thread_scope: str = "",
    media_type: str = "",
    media_path: str = "",
    duration_sec: int = 0,
    why: str = "",
    how_to_apply: str = "",
) -> str:
    """Render a memo file (frontmatter + body) as a single string.

    Used by the WAL writer and the migration script. The frontmatter is
    YAML, hand-rendered (not via ``yaml.dump``) so the output is
    deterministic and diff-friendly.

    ``user_scope`` and ``thread_scope`` map to ``metadata.user`` and
    ``metadata.thread`` respectively. Empty string = field omitted (memo
    is global at that scope level).
    """
    if isinstance(memo_type, str):
        try:
            memo_type = MemoType(memo_type.lower())
        except ValueError as exc:
            raise MemoSpecError(f"invalid memo type {memo_type!r}") from exc

    # Validate the inputs before rendering so a bad call doesn't write a
    # subtly broken file the loader will then reject.
    _validate_name(name, filename_stem=name)
    _validate_description(description)
    body = (body or "").strip()
    if not body:
        raise MemoSpecError("body is required")
    if len(body) > MAX_BODY_CHARS:
        raise MemoSpecError(f"body exceeds {MAX_BODY_CHARS} chars")

    # Description quoting: always wrap in double quotes; escape embedded
    # quotes. We never let unquoted YAML guess at the value type — that's
    # how ``version: 2.0`` becomes a float and breaks downstream parsers.
    desc_escaped = description.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        "---",
        f"name: {name}",
        f'description: "{desc_escaped}"',
        "metadata:",
        f"  type: {memo_type.value}",
    ]
    if user_scope:
        # Quote the scope value: user IDs are often numeric strings, and
        # unquoted ``user: 1545224574`` would parse to an int.
        lines.append(f'  user: "{user_scope}"')
    if thread_scope:
        lines.append(f'  thread: "{thread_scope}"')
    if media_type:
        lines.append(f'  media_type: "{media_type}"')
    if media_path:
        lines.append(f'  media_path: "{media_path}"')
    if duration_sec:
        lines.append(f"  duration_sec: {int(duration_sec)}")
    lines.extend(["---", "", body])
    if why or how_to_apply:
        lines.append("")
        if why:
            lines.append(f"**Why:** {why}")
        if how_to_apply:
            lines.append(f"**How to apply:** {how_to_apply}")
    lines.append("")
    return "\n".join(lines)
