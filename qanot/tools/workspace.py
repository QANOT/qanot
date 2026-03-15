"""Workspace file management — initialization and structured updates."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Templates bundled inside qanot package (pip install) or repo root (Docker)
_pkg_root = Path(__file__).resolve().parent.parent
_pkg_templates = _pkg_root / "templates"
_repo_templates = _pkg_root.parent / "templates"
TEMPLATE_DIR = _pkg_templates if _pkg_templates.exists() else _repo_templates


def init_workspace(workspace_dir: str) -> None:
    """Initialize workspace on first run by copying template files.

    Only copies files that don't already exist (preserves user modifications).
    """
    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (ws / "memory").mkdir(exist_ok=True)
    (ws / "notes" / "areas").mkdir(parents=True, exist_ok=True)

    # Copy workspace template files
    template_ws = TEMPLATE_DIR / "workspace"
    if template_ws.exists():
        for src in template_ws.rglob("*"):
            if src.is_file():
                rel = src.relative_to(template_ws)
                dst = ws / rel
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    logger.info("Copied template: %s", rel)

    for src, dst in [
        (TEMPLATE_DIR / "souls" / "universal.md", ws / "SOUL.md"),
        (TEMPLATE_DIR / "skills" / "proactive-agent" / "SKILL.md", ws / "SKILL.md"),
    ]:
        if not dst.exists() and src.exists():
            shutil.copy2(src, dst)
            logger.info("Copied %s template", dst.name)

    logger.info("Workspace initialized at %s", workspace_dir)


def update_session_state(key: str, value: str, workspace_dir: str = "/data/workspace") -> None:
    """Write a structured key-value entry to SESSION-STATE.md."""
    state_path = Path(workspace_dir) / "SESSION-STATE.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if not state_path.exists():
        state_path.write_text("# SESSION-STATE.md — Active Working Memory\n\n", encoding="utf-8")

    # Read existing content
    content = state_path.read_text(encoding="utf-8")

    # Check if key already exists and update
    lines = content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"- **{key}:**"):
            lines[i] = f"- **{key}:** {value}"
            updated = True
            break

    if updated:
        state_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        with state_path.open("a", encoding="utf-8") as f:
            f.write(f"- **{key}:** {value}\n")
