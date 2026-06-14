"""Output Formatting prompt section — teaches the model to use Bot API 10.1
Rich Messages (GFM tables/LaTeX/lists) for STRUCTURED content, while keeping
ordinary replies short (brevity-first, per Uzbek copy guidance).

Spec: docs/superpowers/specs/2026-06-15-telegram-rich-messages-design.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qanot.prompt import build_system_prompt


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "SOUL.md").write_text("# Bot\nYou are a helpful assistant.")
    return tmp_path


def test_output_formatting_section_present(ws):
    prompt = build_system_prompt(
        workspace_dir=str(ws), bot_name="Qanot", mode="full",
    )
    assert "## Output Formatting" in prompt


def test_formatting_advertises_rich_affordances(ws):
    """The model must know tables and LaTeX render natively now."""
    prompt = build_system_prompt(
        workspace_dir=str(ws), bot_name="Qanot", mode="full",
    )
    assert "table" in prompt.lower()
    assert "LaTeX" in prompt


def test_formatting_guards_brevity(ws):
    """Must NOT encourage walls of text — structure only when it earns its place."""
    prompt = build_system_prompt(
        workspace_dir=str(ws), bot_name="Qanot", mode="full",
    )
    assert "Brevity first" in prompt
