"""Tests for qanot.memos.spec — frontmatter, scope hierarchy, render/parse round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from qanot.memos import (
    MAX_BODY_CHARS,
    MAX_DESCRIPTION_CHARS,
    MemoSpec,
    MemoSpecError,
    MemoType,
    parse_memo_file,
    render_memo,
    split_frontmatter,
)


def _write_memo(parent: Path, name: str, content: str) -> Path:
    p = parent / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ─── frontmatter parsing ─────────────────────────────────────────


class TestSplitFrontmatter:
    def test_basic(self):
        fm, body = split_frontmatter(
            '---\nname: x\ndescription: "y"\nmetadata:\n  type: user\n---\nbody',
        )
        assert fm["name"] == "x"
        assert fm["description"] == "y"
        assert fm["metadata"]["type"] == "user"
        assert body == "body"

    def test_no_frontmatter(self):
        fm, body = split_frontmatter("just markdown")
        assert fm == {}
        assert body == "just markdown"

    def test_malformed_yaml_raises(self):
        with pytest.raises(MemoSpecError):
            split_frontmatter("---\nname: : :\n  bad: indent\n---\n")

    def test_array_root_rejected(self):
        with pytest.raises(MemoSpecError):
            split_frontmatter("---\n- a\n- b\n---\nbody")


# ─── parse_memo_file ─────────────────────────────────────────────


class TestParseMemo:
    def test_minimal_global(self, tmp_path):
        content = render_memo(
            "user-name", "User's name and role", MemoType.USER,
            "User goes by Umurzoq, runs a Telegram bot framework.",
        )
        p = _write_memo(tmp_path, "user-name", content)
        m = parse_memo_file(p)
        assert m.name == "user-name"
        assert m.type == MemoType.USER
        assert m.is_global
        assert m.user_scope == ""
        assert m.thread_scope == ""

    def test_user_scoped(self, tmp_path):
        content = render_memo(
            "user-color", "Favorite color", MemoType.USER,
            "Favorite color: blue.", user_scope="1545224574",
        )
        p = _write_memo(tmp_path, "user-color", content)
        m = parse_memo_file(p)
        assert m.user_scope == "1545224574"
        assert m.thread_scope == ""
        assert not m.is_global

    def test_thread_scoped(self, tmp_path):
        content = render_memo(
            "feedback-title-format",
            "Daily note title must be D-month, YYYY",
            MemoType.FEEDBACK,
            "Title format: '12-may, 2026'.",
            thread_scope="kunlik-yozuv",
        )
        p = _write_memo(tmp_path, "feedback-title-format", content)
        m = parse_memo_file(p)
        assert m.thread_scope == "kunlik-yozuv"
        assert m.user_scope == ""

    def test_user_and_thread_scoped(self, tmp_path):
        content = render_memo(
            "feedback-ielts-style", "Academic English for IELTS prep",
            MemoType.FEEDBACK,
            "Respond in academic register, B2+ vocabulary.",
            user_scope="1545224574", thread_scope="ielts",
        )
        p = _write_memo(tmp_path, "feedback-ielts-style", content)
        m = parse_memo_file(p)
        assert m.user_scope == "1545224574"
        assert m.thread_scope == "ielts"

    def test_why_how_extracted(self, tmp_path):
        content = render_memo(
            "feedback-no-emoji", "Avoid emoji in formal documents",
            MemoType.FEEDBACK,
            "Do not use emoji.",
            why="User finds emoji unprofessional in legal docs.",
            how_to_apply="Strip emoji from any DOCX/PDF output. Chat replies unaffected.",
        )
        p = _write_memo(tmp_path, "feedback-no-emoji", content)
        m = parse_memo_file(p)
        assert "unprofessional" in m.why
        assert "Strip emoji" in m.how_to_apply

    def test_numeric_user_id_coerced(self, tmp_path):
        # YAML parses an unquoted numeric ID as int — we must coerce.
        p = _write_memo(tmp_path, "user-foo", (
            '---\nname: user-foo\ndescription: "test"\nmetadata:\n'
            '  type: user\n  user: 1545224574\n---\nbody'
        ))
        m = parse_memo_file(p)
        assert m.user_scope == "1545224574"
        assert m.matches_scope(user_id="1545224574")


# ─── validation errors ──────────────────────────────────────────


class TestValidation:
    def test_missing_name(self, tmp_path):
        p = _write_memo(tmp_path, "x", (
            '---\ndescription: "d"\nmetadata:\n  type: user\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="name"):
            parse_memo_file(p)

    def test_missing_description(self, tmp_path):
        p = _write_memo(tmp_path, "x", (
            '---\nname: x\nmetadata:\n  type: user\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="description"):
            parse_memo_file(p)

    def test_missing_type(self, tmp_path):
        p = _write_memo(tmp_path, "x", (
            '---\nname: x\ndescription: "d"\nmetadata: {}\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="type"):
            parse_memo_file(p)

    def test_invalid_type(self, tmp_path):
        p = _write_memo(tmp_path, "x", (
            '---\nname: x\ndescription: "d"\nmetadata:\n  type: weird\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="must be one of"):
            parse_memo_file(p)

    def test_name_mismatch_with_filename(self, tmp_path):
        p = _write_memo(tmp_path, "actual", (
            '---\nname: different\ndescription: "d"\nmetadata:\n  type: user\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="filename stem"):
            parse_memo_file(p)

    def test_invalid_name_chars(self, tmp_path):
        p = _write_memo(tmp_path, "BadName", (
            '---\nname: BadName\ndescription: "d"\nmetadata:\n  type: user\n---\nb'
        ))
        with pytest.raises(MemoSpecError):
            parse_memo_file(p)

    def test_oversized_description(self, tmp_path):
        big = "x" * (MAX_DESCRIPTION_CHARS + 10)
        p = _write_memo(tmp_path, "big", (
            f'---\nname: big\ndescription: "{big}"\nmetadata:\n  type: user\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="description exceeds"):
            parse_memo_file(p)

    def test_oversized_body(self, tmp_path):
        body = "x " * (MAX_BODY_CHARS // 2 + 100)
        p = _write_memo(tmp_path, "big", (
            f'---\nname: big\ndescription: "d"\nmetadata:\n  type: user\n---\n{body}'
        ))
        with pytest.raises(MemoSpecError, match="body exceeds"):
            parse_memo_file(p)

    def test_bool_scope_rejected(self, tmp_path):
        p = _write_memo(tmp_path, "x", (
            '---\nname: x\ndescription: "d"\nmetadata:\n  type: user\n'
            '  user: true\n---\nb'
        ))
        with pytest.raises(MemoSpecError, match="bool"):
            parse_memo_file(p)


# ─── matches_scope ──────────────────────────────────────────────


class TestMatchesScope:
    def test_global_matches_everything(self):
        m = MemoSpec(name="g", description="d", type=MemoType.USER,
                    body="b", path=Path("/tmp/g.md"))
        assert m.matches_scope() is True
        assert m.matches_scope(user_id="a") is True
        assert m.matches_scope(thread_id="t1") is True
        assert m.matches_scope(user_id="a", thread_id="t1") is True

    def test_user_scoped_filters(self):
        m = MemoSpec(
            name="u", description="d", type=MemoType.USER, body="b",
            path=Path("/tmp/u.md"), user_scope="alice",
        )
        assert m.matches_scope(user_id="alice") is True
        assert m.matches_scope(user_id="bob") is False
        # No user_id → can't prove match → reject (safe default).
        assert m.matches_scope(thread_id="anything") is False

    def test_thread_scoped_filters(self):
        m = MemoSpec(
            name="t", description="d", type=MemoType.FEEDBACK, body="b",
            path=Path("/tmp/t.md"), thread_scope="ielts",
        )
        assert m.matches_scope(thread_id="ielts") is True
        assert m.matches_scope(thread_id="daily") is False
        assert m.matches_scope() is False  # no thread proof

    def test_both_scopes_require_both(self):
        m = MemoSpec(
            name="ut", description="d", type=MemoType.FEEDBACK, body="b",
            path=Path("/tmp/ut.md"), user_scope="alice", thread_scope="ielts",
        )
        # AND, not OR
        assert m.matches_scope(user_id="alice", thread_id="ielts") is True
        assert m.matches_scope(user_id="alice", thread_id="daily") is False
        assert m.matches_scope(user_id="bob", thread_id="ielts") is False
        assert m.matches_scope(user_id="alice") is False


# ─── render_memo ────────────────────────────────────────────────


class TestRender:
    def test_round_trip_global(self, tmp_path):
        content = render_memo(
            "x", "test desc", MemoType.USER, "body line",
        )
        p = _write_memo(tmp_path, "x", content)
        m = parse_memo_file(p)
        assert m.name == "x"
        assert m.description == "test desc"
        assert m.body == "body line"

    def test_round_trip_full_scope(self, tmp_path):
        content = render_memo(
            "feedback-x", "rule X", MemoType.FEEDBACK,
            "Do X always.",
            user_scope="u1", thread_scope="t1",
            why="user said so", how_to_apply="when writing X-shaped output",
        )
        p = _write_memo(tmp_path, "feedback-x", content)
        m = parse_memo_file(p)
        assert m.user_scope == "u1"
        assert m.thread_scope == "t1"
        assert m.why == "user said so"
        assert m.how_to_apply == "when writing X-shaped output"

    def test_description_with_quotes(self, tmp_path):
        # The renderer must escape embedded quotes so the YAML parses.
        content = render_memo(
            "x", 'desc with "quotes" inside', MemoType.USER, "body",
        )
        p = _write_memo(tmp_path, "x", content)
        m = parse_memo_file(p)
        assert '"quotes"' in m.description

    def test_invalid_type_raises(self):
        with pytest.raises(MemoSpecError):
            render_memo("x", "d", "weird", "body")

    def test_string_type_coerced(self):
        # Accept "user" / "feedback" / etc. as a friendliness affordance.
        out = render_memo("x", "d", "user", "body")
        assert "type: user" in out

    def test_empty_body_rejected(self):
        with pytest.raises(MemoSpecError, match="body"):
            render_memo("x", "d", MemoType.USER, "")
