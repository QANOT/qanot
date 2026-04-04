"""Shared helpers for document tools — path traversal protection."""

from __future__ import annotations

import json
from pathlib import Path


def resolve_doc_path(params: dict, workspace_dir: str, key: str = "file") -> tuple[Path | None, str | None]:
    """Resolve document file path safely within workspace.

    Returns (path, None) on success or (None, error_json) on failure.
    """
    filepath = params.get(key, "")
    if not filepath:
        return None, json.dumps({"error": f"{key} parametri kerak"})

    resolved = (Path(workspace_dir) / filepath).resolve()
    ws_resolved = Path(workspace_dir).resolve()
    try:
        resolved.relative_to(ws_resolved)
    except ValueError:
        return None, json.dumps({"error": f"Path outside workspace: {filepath}"})

    return resolved, None


def resolve_doc_path_existing(
    params: dict, workspace_dir: str, key: str = "file",
) -> tuple[Path | None, str | None]:
    """Same as resolve_doc_path but also checks file exists."""
    path, error = resolve_doc_path(params, workspace_dir, key=key)
    if error:
        return None, error
    if not path.exists():
        return None, json.dumps({"error": f"Fayl topilmadi: {params.get(key, '')}"})
    return path, None
