"""Tests for owner-identity injection in the system prompt.

Regression: @topkeydevbot was asked "@zukhriddin0212 ni tanysanmi" in
a group, and the bot hallucinated that Zukhriddin was its owner because
MEMORY.md said "Egam menga bu ismni bergan" (without a name) while
config.owner_name="Umurzoq Sirliboyev" was never surfaced to the model.

Fix: ``build_system_prompt`` now auto-injects an "Owner Identity" block
derived from config.owner_name. Every new QanotCloud customer bot picks
this up for free — no hand-edited MEMORY.md required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qanot.prompt import _build_owner_block, build_system_prompt


# ────────── _build_owner_block ──────────


def test_owner_block_empty_when_name_blank():
    assert _build_owner_block("") == ""
    assert _build_owner_block("   ") == ""


def test_owner_block_contains_name():
    block = _build_owner_block("Umurzoq Sirliboyev")
    assert "Umurzoq Sirliboyev" in block
    assert "Owner Identity" in block


def test_owner_block_warns_against_inference():
    """The block must explicitly tell the model not to guess ownership
    from chat context — that's the bug we're fixing."""
    block = _build_owner_block("Umurzoq Sirliboyev")
    assert "Do NOT infer ownership" in block


def test_owner_block_strips_whitespace():
    block = _build_owner_block("  Umurzoq  ")
    assert "**Umurzoq**" in block


# ────────── build_system_prompt integration ──────────


@pytest.fixture
def empty_workspace(tmp_path: Path) -> Path:
    """A minimal workspace with no MEMORY/SOUL/IDENTITY files."""
    (tmp_path / "SOUL.md").write_text("# Bot\nYou are a helpful assistant.")
    return tmp_path


def test_owner_block_injected_when_owner_name_set(empty_workspace):
    prompt = build_system_prompt(
        workspace_dir=str(empty_workspace),
        owner_name="Umurzoq Sirliboyev",
        bot_name="Qanot",
        mode="full",
    )
    assert "Owner Identity" in prompt
    assert "Umurzoq Sirliboyev" in prompt


def test_owner_block_absent_when_owner_name_blank(empty_workspace):
    """Bots without an owner_name configured (e.g. legacy installs) get
    no Owner Identity section — the rest of the prompt is unchanged."""
    prompt = build_system_prompt(
        workspace_dir=str(empty_workspace),
        owner_name="",
        bot_name="Qanot",
        mode="full",
    )
    assert "Owner Identity" not in prompt


def test_owner_block_skipped_in_minimal_mode(empty_workspace):
    """Minimal mode keeps the prompt tiny (SOUL + TOOLS + session only).
    Owner block lives in the full-mode IDENTITY group, so minimal stays
    out of scope."""
    prompt = build_system_prompt(
        workspace_dir=str(empty_workspace),
        owner_name="Umurzoq Sirliboyev",
        bot_name="Qanot",
        mode="minimal",
    )
    assert "Owner Identity" not in prompt


def test_owner_block_skipped_in_none_mode(empty_workspace):
    prompt = build_system_prompt(
        workspace_dir=str(empty_workspace),
        owner_name="Umurzoq Sirliboyev",
        bot_name="Qanot",
        mode="none",
    )
    assert "Owner Identity" not in prompt
