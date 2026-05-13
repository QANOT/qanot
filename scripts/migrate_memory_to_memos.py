"""One-shot migration: monolithic MEMORY.md → per-fact memo files.

Splits a workspace's existing ``MEMORY.md`` into the new
``memories/<slug>.md`` files defined by ``qanot/memos/spec.py``. The
migration is deliberately conservative:

  - We do not touch files the existing ``memory_tool.py`` writes
    (``identity.md``, ``user_profile_*.md``, ``recurring_patterns.md``,
    ``learnings/``, ``daily-notes/``). They keep their own lifecycle.
  - We do not delete ``MEMORY.md`` after migrating — the agent's prompt
    builder still injects it as a fallback. After a successful deploy
    we'll add a follow-up that switches the prompt to ``memos``-only
    and then ``MEMORY.md`` can be archived by hand.
  - We never call out to an LLM. Memo classification is rule-based off
    the section headers and ``- **<category>**:`` prefixes we already
    write in ``qanot/memory.py``. This keeps the migration deterministic
    and offline-runnable.

Usage (inside the container, after a backup):
    docker exec qanot-bot-1545224574 python3 -m scripts.migrate_memory_to_memos \\
        --workspace /data/workspace --dry-run
    # review the proposed plan, then run for real:
    docker exec qanot-bot-1545224574 python3 -m scripts.migrate_memory_to_memos \\
        --workspace /data/workspace

The script is idempotent — re-running over an already-migrated workspace
is a no-op (existing memo files are detected and skipped). Failed
parses log at WARNING; the script never raises so a single weird
bullet doesn't block the rest of the migration.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from qanot.memos import MemoStore, MemoType

logger = logging.getLogger("migrate")


# Regex catalogue for parsing the existing MEMORY.md structure.

# Bullet prefix pattern: `- **<category>**:[ user:N] <detail>`. The
# user tag is optional; we use it to populate metadata.user.
# Category is restricted to known WAL categories so we don't capture
# ad-hoc bold key prefixes (e.g. `- **web_search**: enabled` was
# previously mis-parsed as a memo with category=web_search).
_KNOWN_CATEGORIES = (
    "remember|preference|proper_noun|decision|correction|specific_value|style_rule"
)
_BULLET_RE = re.compile(
    rf"^-\s+\*\*(?P<category>{_KNOWN_CATEGORIES})\*\*:?"
    r"(?:\s*\[user:(?P<user>[^\]]+)\])?\s*(?P<detail>.+)$",
    re.MULTILINE,
)

# Section header: `## SECTION NAME` — used to pick a sensible memo type
# for bullets that aren't structurally tagged.
_SECTION_RE = re.compile(r"^##\s+(?P<title>.+)$", re.MULTILINE)


# Map MEMORY.md section titles → memo type. Lowercase substring match.
_SECTION_TO_TYPE: list[tuple[str, MemoType]] = [
    ("topic isolation", MemoType.FEEDBACK),     # HARD rule
    ("title & format rules", MemoType.FEEDBACK),
    ("style rules", MemoType.FEEDBACK),
    ("user profile", MemoType.USER),
    ("identity", MemoType.USER),
    ("recurring patterns", MemoType.PROJECT),
    ("financial context", MemoType.PROJECT),
    ("ielts preparation", MemoType.PROJECT),
    ("key learnings", MemoType.PROJECT),
    ("auto-captured", MemoType.USER),  # safer default than feedback
]

# Map WAL category → memo type (when we have a structured bullet).
_CATEGORY_TO_TYPE = {
    "remember": MemoType.FEEDBACK,
    "preference": MemoType.USER,
    "proper_noun": MemoType.USER,
    "decision": MemoType.PROJECT,
    "correction": MemoType.FEEDBACK,
    "specific_value": MemoType.PROJECT,
}


@dataclass
class ProposedMemo:
    """One memo we're about to write — kept structured so --dry-run can
    show the user what would land on disk without actually writing it.
    """

    name: str
    description: str
    memo_type: MemoType
    body: str
    user_scope: str = ""
    thread_scope: str = ""
    why: str = ""

    def render_summary(self) -> str:
        scope = "global"
        if self.user_scope or self.thread_scope:
            bits = []
            if self.user_scope:
                bits.append(f"user:{self.user_scope}")
            if self.thread_scope:
                bits.append(f"thread:{self.thread_scope}")
            scope = "+".join(bits)
        return f"  [{self.memo_type.value}/{scope}] {self.name} — {self.description}"


# ─── parsing helpers ─────────────────────────────────────────────


def _slugify(text: str, max_len: int = 60) -> str:
    """Compress ``text`` into a kebab-case slug suitable for a memo name.

    We strip non-alphanumeric chars, replace runs with hyphens, lowercase,
    truncate, and trim trailing hyphens. The result must satisfy
    ``MemoSpec._NAME_RE``.
    """
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if not s:
        return "memo"
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    # Collapse repeated hyphens (regex above generally handles this, but
    # truncation can leave a trailing run).
    s = re.sub(r"-+", "-", s)
    return s


def _section_type(title: str) -> MemoType | None:
    title_low = title.lower()
    for needle, memo_type in _SECTION_TO_TYPE:
        if needle in title_low:
            return memo_type
    return None


def _bullet_type(category: str) -> MemoType:
    return _CATEGORY_TO_TYPE.get(category, MemoType.USER)


def _description_from_detail(detail: str, fallback_len: int = 80) -> str:
    """Synthesize a one-line description from a longer detail string.

    The first sentence — or a hard cut at 80 chars — is good enough.
    The description is what the router embeds, so trimming aggressively
    actually helps cosine-similarity precision.
    """
    detail = re.sub(r"\s+", " ", detail).strip()
    if not detail:
        return "(no description)"
    # Break at the first sentence boundary if it's short enough.
    sentence_end = re.search(r"[.!?]\s", detail)
    if sentence_end and sentence_end.start() <= fallback_len:
        return detail[: sentence_end.start() + 1]
    return detail[:fallback_len] + ("…" if len(detail) > fallback_len else "")


# ─── core migration ─────────────────────────────────────────────


def parse_memory_md(text: str) -> list[ProposedMemo]:
    """Walk a ``MEMORY.md`` text and emit proposed memos.

    Strategy:
      - Iterate sections. For each section header we hold a default
        memo type derived from ``_SECTION_TO_TYPE``.
      - Within a section, structured bullets ``- **<category>**:`` get
        memo type from ``_CATEGORY_TO_TYPE``.
      - Unstructured bullets within a known section get the section's
        default type.
      - Skill / heading bodies (e.g., the full ``## TITLE & FORMAT RULES
        (HARD)`` block) collapse into a single memo if no bullets exist.

    Names are slugified from the first 60 chars of the detail. Same name
    twice → second one gets a numeric suffix. We don't worry about
    semantic deduplication at this layer — that's the curator's job.
    """
    out: list[ProposedMemo] = []
    seen_names: dict[str, int] = {}

    # Build a list of (header_start, header_end, title) so we can map
    # each bullet back to its section.
    headers = [
        (m.start(), m.end(), m.group("title").strip())
        for m in _SECTION_RE.finditer(text)
    ]
    # Add a sentinel so the loop terminates cleanly.
    headers.append((len(text), len(text), "_end_"))

    for i in range(len(headers) - 1):
        section_start = headers[i][1]
        section_end = headers[i + 1][0]
        section_title = headers[i][2]
        section_body = text[section_start:section_end]

        section_type = _section_type(section_title) or MemoType.USER

        bullets = list(_BULLET_RE.finditer(section_body))
        if bullets:
            for m in bullets:
                category = m.group("category") or ""
                user_scope = (m.group("user") or "").strip()
                detail = m.group("detail").strip()
                memo_type = _bullet_type(category) if category else section_type
                description = _description_from_detail(detail)
                base_name = _slugify(
                    f"{memo_type.value}-{description}", max_len=60,
                )
                name = _dedupe_name(base_name, seen_names)
                out.append(ProposedMemo(
                    name=name,
                    description=description,
                    memo_type=memo_type,
                    body=detail,
                    user_scope=user_scope,
                    why=f"Captured from MEMORY.md '{section_title}' section.",
                ))
        else:
            # No bullets — the whole section body becomes one memo. Skip
            # very short or empty sections.
            body = section_body.strip()
            if len(body) < 30:
                continue
            description = _description_from_detail(section_title, 80)
            base_name = _slugify(f"{section_type.value}-{section_title}", 60)
            name = _dedupe_name(base_name, seen_names)
            out.append(ProposedMemo(
                name=name,
                description=description,
                memo_type=section_type,
                body=body,
                why=f"Captured from MEMORY.md '{section_title}' section.",
            ))

    return out


def _dedupe_name(base: str, seen: dict[str, int]) -> str:
    """Return ``base`` (or ``base-2``, ``base-3``, …) for the first
    unused slot. Mutates ``seen`` so subsequent calls increment.
    """
    if base not in seen:
        seen[base] = 1
        return base
    seen[base] += 1
    return f"{base}-{seen[base]}"


# ─── apply ──────────────────────────────────────────────────────


def apply_proposed(
    store: MemoStore, proposals: list[ProposedMemo], *, dry_run: bool,
) -> tuple[int, int, int]:
    """Write each proposed memo via the store. Returns ``(created,
    skipped, errors)``.

    Skipped = memo with the same name already exists on disk (idempotent
    re-run). Errors = SpecError or StoreError raised; logged but not
    re-raised.
    """
    created = skipped = errors = 0
    for proposed in proposals:
        if store.load(proposed.name) is not None:
            logger.debug("skip %s — already exists", proposed.name)
            skipped += 1
            continue
        if dry_run:
            logger.info("would write %s", proposed.name)
            created += 1
            continue
        try:
            store.upsert(
                name=proposed.name,
                description=proposed.description,
                memo_type=proposed.memo_type,
                body=proposed.body,
                user_scope=proposed.user_scope,
                thread_scope=proposed.thread_scope,
                why=proposed.why,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 — migration must not raise
            logger.warning("failed to write %s: %s", proposed.name, exc)
            errors += 1
    return created, skipped, errors


# ─── CLI entrypoint ─────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", default="/data/workspace",
        help="Workspace directory (where MEMORY.md lives)",
    )
    parser.add_argument(
        "--memory-file", default="MEMORY.md",
        help="Filename within the workspace (default: MEMORY.md)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show the proposed memos without writing any files",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    workspace = Path(args.workspace)
    memory_path = workspace / args.memory_file
    if not memory_path.is_file():
        logger.error("MEMORY.md not found at %s", memory_path)
        return 2

    text = memory_path.read_text(encoding="utf-8")
    proposals = parse_memory_md(text)
    logger.info("parsed %d candidate memos from %s", len(proposals), memory_path)

    if args.verbose or args.dry_run:
        for p in proposals:
            print(p.render_summary())

    store = MemoStore(workspace)
    created, skipped, errors = apply_proposed(
        store, proposals, dry_run=args.dry_run,
    )

    verb = "would write" if args.dry_run else "wrote"
    logger.info(
        "migration done: %s %d memo(s); skipped %d (already exist); %d error(s)",
        verb, created, skipped, errors,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
