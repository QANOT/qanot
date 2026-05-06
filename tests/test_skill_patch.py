"""Tests for the skill patch helper + update_skill tool.

Validates Item 2 from the Hermes-borrow list: incremental skill edits
without rewrite-the-whole-file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qanot.tools.skill_tools import (
    _apply_patch,
    _find_section,
    _split_frontmatter,
    register_skill_tools,
)
from qanot.registry import ToolRegistry


# ── pure-function tests ────────────────────────────────────────


def test_split_frontmatter_with_yaml():
    text = "---\nname: foo\ndescription: bar\n---\nbody line\n"
    fm, body = _split_frontmatter(text)
    assert fm.startswith("---")
    assert "name: foo" in fm
    assert body == "body line\n"


def test_split_frontmatter_no_yaml():
    fm, body = _split_frontmatter("just body content\n")
    assert fm == ""
    assert body == "just body content\n"


def test_split_frontmatter_unterminated():
    fm, body = _split_frontmatter("---\nname: foo\nstill in fm\n")
    assert fm == ""
    assert body == "---\nname: foo\nstill in fm\n"


def test_find_section_h2():
    body = "# Title\n\n## A\nbody A\n\n## B\nbody B\n\n## C\nbody C\n"
    span = _find_section(body, "## B")
    assert span is not None
    start, end = span
    assert body[start:end] == "## B\nbody B\n\n"


def test_find_section_until_eof():
    body = "## Only\nlast line"
    span = _find_section(body, "## Only")
    assert span is not None
    start, end = span
    assert body[start:end] == "## Only\nlast line"


def test_find_section_h3_under_h2():
    body = "## A\n### A.1\nsub\n### A.2\nsub2\n## B\nb body\n"
    span = _find_section(body, "### A.1")
    assert span is not None
    start, end = span
    assert body[start:end] == "### A.1\nsub\n"


def test_find_section_missing():
    assert _find_section("## Only\nbody\n", "## Other") is None


def test_apply_append_to_section_with_existing():
    original = "---\nname: x\n---\n## Examples\nexisting\n## Other\nfoo\n"
    new, desc = _apply_patch(
        original=original, mode="append_to_section",
        content="new line\n", section="## Examples",
    )
    assert new is not None
    assert "## Examples\nexisting\nnew line\n" in new
    assert "appended to '## Examples'" in desc


def test_apply_replace_section():
    original = "## Examples\nold body\nmore old\n## Next\n"
    new, desc = _apply_patch(
        original=original, mode="replace_section",
        content="brand new\n", section="## Examples",
    )
    assert new is not None
    assert "## Examples\nbrand new\n## Next\n" == new
    assert "replaced" in desc


def test_apply_replace_section_preserves_frontmatter():
    original = "---\nname: x\n---\n## A\nold\n## B\nb\n"
    new, _ = _apply_patch(
        original=original, mode="replace_section",
        content="updated", section="## A",
    )
    assert new is not None
    assert new.startswith("---\nname: x\n---\n")
    assert "## A\nupdated\n## B\nb\n" in new


def test_apply_append_at_end():
    original = "## Existing\nfoo\n"
    new, _ = _apply_patch(
        original=original, mode="append",
        content="## New Section\nbar\n", section="",
    )
    assert new is not None
    assert new.endswith("## New Section\nbar\n")
    assert "## Existing\nfoo\n" in new


def test_apply_prepend_after_frontmatter():
    original = "---\nname: x\n---\n## Existing\nfoo\n"
    new, _ = _apply_patch(
        original=original, mode="prepend",
        content="## Top\nbar\n", section="",
    )
    assert new is not None
    assert new.startswith("---\nname: x\n---\n## Top\nbar\n")


def test_apply_replace_missing_section_returns_none():
    new, _ = _apply_patch(
        original="## A\nfoo\n", mode="replace_section",
        content="x", section="## Z",
    )
    assert new is None


# ── update_skill tool integration ──────────────────────────────


def test_update_skill_tool_replace_section(tmp_path: Path):
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: x\n---\n## Steps\nold step\n## Notes\nnotes here\n",
        encoding="utf-8",
    )

    registry = ToolRegistry()
    register_skill_tools(registry, str(tmp_path))

    handler = registry.get_handler("update_skill")
    assert handler is not None
    result = asyncio.run(handler({
        "name": "demo",
        "mode": "replace_section",
        "section": "## Steps",
        "content": "1. new step one\n2. new step two\n",
    }))
    parsed = json.loads(result)
    assert parsed["success"] is True
    new_text = (skills_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "old step" not in new_text
    assert "new step one" in new_text
    assert "## Notes\nnotes here\n" in new_text


def test_update_skill_tool_append_to_section(tmp_path: Path):
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: x\n---\n## Examples\nexample 1\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    register_skill_tools(registry, str(tmp_path))
    handler = registry.get_handler("update_skill")
    result = asyncio.run(handler({
        "name": "demo",
        "mode": "append_to_section",
        "section": "## Examples",
        "content": "example 2\nexample 3\n",
    }))
    assert json.loads(result)["success"] is True
    body = (skills_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "example 1" in body
    assert "example 2" in body
    assert "example 3" in body


def test_update_skill_tool_section_not_found_returns_error(tmp_path: Path):
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\n## A\na\n", encoding="utf-8")
    registry = ToolRegistry()
    register_skill_tools(registry, str(tmp_path))
    handler = registry.get_handler("update_skill")
    result = asyncio.run(handler({
        "name": "demo", "mode": "replace_section",
        "section": "## Missing", "content": "x",
    }))
    parsed = json.loads(result)
    assert "error" in parsed
    assert "Missing" in parsed["error"]


def test_update_skill_tool_unknown_skill(tmp_path: Path):
    registry = ToolRegistry()
    register_skill_tools(registry, str(tmp_path))
    handler = registry.get_handler("update_skill")
    result = asyncio.run(handler({
        "name": "ghost", "mode": "append",
        "content": "x",
    }))
    parsed = json.loads(result)
    assert "error" in parsed
    assert "ghost" in parsed["error"]


def test_update_skill_tool_invalid_mode(tmp_path: Path):
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\n---\nbody\n", encoding="utf-8")
    registry = ToolRegistry()
    register_skill_tools(registry, str(tmp_path))
    handler = registry.get_handler("update_skill")
    result = asyncio.run(handler({
        "name": "demo", "mode": "smash_everything",
        "content": "x",
    }))
    assert "error" in json.loads(result)


def test_update_skill_tool_oversize_blocked(tmp_path: Path):
    skills_dir = tmp_path / "skills" / "demo"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\n---\n## A\na\n", encoding="utf-8")
    registry = ToolRegistry()
    register_skill_tools(registry, str(tmp_path))
    handler = registry.get_handler("update_skill")
    huge = "x" * 100_000
    result = asyncio.run(handler({
        "name": "demo", "mode": "append",
        "content": huge,
    }))
    parsed = json.loads(result)
    assert "error" in parsed
    assert "64KB" in parsed["error"]
