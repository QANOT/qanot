"""Tests for scripts/migrate_memory_to_memos.py — MEMORY.md → memos/ parser.

We test the parser pure-function (``parse_memory_md``) and the apply
step (``apply_proposed``) against synthetic and prod-shaped inputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ isn't a package; we add it to sys.path explicitly so the
# tests can import the module under test.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from migrate_memory_to_memos import (  # noqa: E402
    ProposedMemo,
    _slugify,
    apply_proposed,
    parse_memory_md,
)

from qanot.memos import MemoStore, MemoType  # noqa: E402


# ─── slugify ─────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self):
        assert _slugify("Hello World") == "hello-world"

    def test_strips_punctuation(self):
        assert _slugify("TITLE & FORMAT RULES (HARD)") == "title-format-rules-hard"

    def test_collapses_runs(self):
        assert _slugify("a---b____c") == "a-b-c"

    def test_truncates(self):
        out = _slugify("x" * 100, max_len=20)
        assert len(out) <= 20

    def test_handles_unicode(self):
        # We expect non-ASCII to be stripped; Uzbek section titles often
        # include Cyrillic or accented chars from copy-paste.
        assert _slugify("Финансовый Context") == "context"

    def test_empty_input_safe(self):
        assert _slugify("") == "memo"
        assert _slugify("!!!") == "memo"


# ─── section detection ─────────────────────────────────────────


class TestSectionDetection:
    def test_topic_isolation_classified_as_feedback(self):
        md = (
            "# MEMORY.md\n\n"
            "## TOPIC ISOLATION (CRITICAL RULE)\n\n"
            "Rule body content here, long enough to be picked up by the parser.\n"
        )
        proposals = parse_memory_md(md)
        assert len(proposals) == 1
        assert proposals[0].memo_type == MemoType.FEEDBACK
        assert proposals[0].name.startswith("feedback-")

    def test_title_format_classified_as_feedback(self):
        md = (
            "## TITLE & FORMAT RULES (HARD)\n\n"
            "Daily note title format ALWAYS: 13-may, 2026 — never Daily Entry — YYYY-yil format.\n"
        )
        proposals = parse_memory_md(md)
        assert any(p.memo_type == MemoType.FEEDBACK for p in proposals)

    def test_user_profile_classified_as_user(self):
        md = (
            "## User Profile [user:123]\n\n"
            "Language: Uzbek. Timezone: Asia/Tashkent. Long enough section body.\n"
        )
        proposals = parse_memory_md(md)
        assert any(p.memo_type == MemoType.USER for p in proposals)


# ─── bullet parsing ────────────────────────────────────────────


class TestBulletParsing:
    def test_structured_bullet_captured(self):
        md = (
            "## Auto-captured\n\n"
            "- **remember** [user:1545224574] Bot must always greet warmly.\n"
        )
        proposals = parse_memory_md(md)
        assert len(proposals) == 1
        # `remember` category maps to FEEDBACK type.
        assert proposals[0].memo_type == MemoType.FEEDBACK
        assert proposals[0].user_scope == "1545224574"
        assert "greet warmly" in proposals[0].body

    def test_unknown_bold_key_not_treated_as_category(self):
        # `**web_search**` is not a WAL category — must NOT be a bullet match.
        # The whole section becomes one memo instead, and "web_search"
        # appears in the body, not as a memo name fragment.
        md = (
            "## Key Learnings\n\n"
            "Mixed integrations content:\n"
            "- **web_search**: enabled and indexed daily.\n"
            "- **anthropic**: claude opus integration active.\n"
        )
        proposals = parse_memory_md(md)
        assert len(proposals) == 1
        assert "user-enabled" not in proposals[0].name
        assert "web-search" not in proposals[0].name
        assert "web_search" in proposals[0].body

    def test_preference_bullet_user_type(self):
        md = (
            "## Auto-captured\n\n"
            "- **preference** [user:abc] favorite color blue.\n"
        )
        proposals = parse_memory_md(md)
        assert proposals[0].memo_type == MemoType.USER
        assert proposals[0].user_scope == "abc"

    def test_decision_bullet_project_type(self):
        md = (
            "## Auto-captured\n\n"
            "- **decision** Trading pause for 6 months — agreed by user.\n"
        )
        proposals = parse_memory_md(md)
        assert proposals[0].memo_type == MemoType.PROJECT


# ─── dedupe ────────────────────────────────────────────────────


class TestDedupe:
    def test_same_section_name_collides_then_increments(self):
        # Two sections with the same slug → second gets a -2 suffix.
        md = (
            "## TOPIC X\n\nFirst section content long enough to keep.\n\n"
            "## TOPIC X\n\nSecond section content also long enough.\n"
        )
        proposals = parse_memory_md(md)
        names = [p.name for p in proposals]
        assert len(names) == 2
        assert len(set(names)) == 2  # unique


# ─── short sections skipped ────────────────────────────────────


class TestShortSections:
    def test_short_section_dropped(self):
        md = (
            "## Header\n\nOK\n\n"  # < 30 chars body
            "## Real\n\n" + ("x" * 50) + "\n"
        )
        proposals = parse_memory_md(md)
        names = [p.name for p in proposals]
        assert any("real" in n for n in names)
        assert not any(n.endswith("-header") and "real" not in n
                       for n in names if "header" == n.split("-")[-1])


# ─── apply ──────────────────────────────────────────────────────


class TestApply:
    def test_dry_run_writes_nothing(self, tmp_path):
        store = MemoStore(tmp_path)
        proposed = [ProposedMemo(
            name="x", description="d", memo_type=MemoType.USER, body="b",
        )]
        created, _, _ = apply_proposed(store, proposed, dry_run=True)
        assert created == 1
        assert store.load("x") is None

    def test_real_write(self, tmp_path):
        store = MemoStore(tmp_path)
        proposed = [ProposedMemo(
            name="x", description="d", memo_type=MemoType.USER, body="b",
        )]
        created, _, _ = apply_proposed(store, proposed, dry_run=False)
        assert created == 1
        m = store.load("x")
        assert m is not None
        assert m.body == "b"

    def test_idempotent_skip(self, tmp_path):
        store = MemoStore(tmp_path)
        proposed = [ProposedMemo(
            name="x", description="d", memo_type=MemoType.USER, body="b",
        )]
        apply_proposed(store, proposed, dry_run=False)
        created, skipped, _ = apply_proposed(store, proposed, dry_run=False)
        assert created == 0
        assert skipped == 1


# ─── prod-shaped integration ───────────────────────────────────


PROD_LIKE_MEMORY = """# MEMORY.md - Long-Term Memory

## Identity
- My name: Qanot
- Owner: Umurzoq

## TOPIC ISOLATION (CRITICAL RULE)

When the user asks question X, answer ONLY about X. Do not pull
unrelated topics into the reply.

## TITLE & FORMAT RULES (HARD)

Daily note title ALWAYS uses this shape: 13-may, 2026 — never
Daily Entry — YYYY-yil, DD-month format. Strip prefixes before saving.

## User Profile [user:1545224574]
- Language: Uzbek
- Timezone: Asia/Tashkent
- Favorite color blue
- Developer

## Auto-captured

- **remember** [user:1545224574] Always strip "Daily Entry —" prefix from Notion titles.
- **preference** [user:1545224574] prefers DOCX over PDF.
"""


def test_prod_like_migration(tmp_path):
    """Black-box: feed a prod-shaped MEMORY.md and confirm key memos appear."""
    proposals = parse_memory_md(PROD_LIKE_MEMORY)
    names = {p.name for p in proposals}
    # The title-format rule must surface as a feedback-typed memo.
    assert any("title-format" in n or "title" in n for n in names)
    assert any("topic-isolation" in n for n in names)
    # No junk memos from bullet parsing.
    assert "user-enabled" not in names
