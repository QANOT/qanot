"""One-shot migration: MEMORY.md → /memories/ files.

Run BEFORE flipping `inject_legacy_memory=False` so the Anthropic memory
tool's `view /memories/` path finds the durable facts that used to be
baked into the system prompt.

Splits MEMORY.md by top-level sections (`## Identity`, `## User Profile`,
`## Key Learnings`, etc.) and writes each to its own file in
`{workspace_dir}/memories/`. Idempotent — re-running skips files that
already exist unless `overwrite=True`.

The original MEMORY.md is NOT deleted — it stays on disk and remains
indexed by RAG so `rag_search` can still hit it as a fallback.

Can be invoked via:
  python3 -c "from qanot.tools.memory_migrate import migrate; migrate('/data/workspace')"
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Normalise section titles → flat filenames. Sub-sections under
# "Key Learnings" become individual files under memories/learnings/.
_FILENAME_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(name: str) -> str:
    s = name.strip().lower()
    s = s.replace(" ", "_")
    s = _FILENAME_RE.sub("", s)
    return s or "section"


def _split_sections(md: str) -> list[tuple[int, str, str]]:
    """Return list of (level, title, body) tuples.

    Only top-level (##) sections are treated as split points; deeper
    headings (###, ####) stay inside their parent body.
    """
    lines = md.splitlines()
    sections: list[tuple[int, str, list[str]]] = []
    current: list[str] | None = None
    current_title = ""
    for line in lines:
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m and len(m.group(1)) == 2:  # ## only
            if current is not None:
                sections.append((2, current_title, current))
            current_title = m.group(2).strip()
            current = []
        else:
            if current is None:
                # Content before any ## — drop (usually the H1 title).
                continue
            current.append(line)
    if current is not None:
        sections.append((2, current_title, current))
    return [(lvl, title, "\n".join(body).strip()) for lvl, title, body in sections]


def _split_learnings(body: str) -> list[tuple[str, str]]:
    """Split a "Key Learnings" body into (subtopic, content) pairs.

    Each ### heading becomes one pair. Preamble before the first ###
    lands under an "overview" key.
    """
    lines = body.splitlines()
    subsections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    current_title = "overview"
    preamble: list[str] = []
    saw_first = False
    for line in lines:
        m = re.match(r"^###\s+(.*)$", line)
        if m:
            if current is not None:
                subsections.append((current_title, current))
            current_title = m.group(1).strip()
            current = []
            saw_first = True
        else:
            if saw_first and current is not None:
                current.append(line)
            else:
                preamble.append(line)
    if current is not None:
        subsections.append((current_title, current))
    out: list[tuple[str, str]] = []
    pre = "\n".join(preamble).strip()
    if pre:
        out.append(("overview", pre))
    for title, body_lines in subsections:
        text = "\n".join(body_lines).strip()
        if text:
            out.append((title, text))
    return out


def migrate(workspace_dir: str | Path, *, overwrite: bool = False) -> dict:
    """Copy MEMORY.md sections into /memories/ and return a summary.

    Returns: {"written": [...], "skipped": [...], "source_kept": bool}
    """
    ws = Path(workspace_dir)
    src = ws / "MEMORY.md"
    memories = ws / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "learnings").mkdir(parents=True, exist_ok=True)

    summary = {"written": [], "skipped": [], "source_kept": True}

    if not src.exists():
        logger.info("No MEMORY.md to migrate at %s", src)
        return summary

    md = src.read_text(encoding="utf-8")
    sections = _split_sections(md)
    if not sections:
        logger.info("MEMORY.md had no ## sections; writing whole file as legacy_memory.md")
        out = memories / "legacy_memory.md"
        if out.exists() and not overwrite:
            summary["skipped"].append(str(out))
        else:
            out.write_text(md, encoding="utf-8")
            summary["written"].append(str(out))
        return summary

    for _, title, body in sections:
        if title.lower().startswith("key learnings"):
            for subtopic, content in _split_learnings(body):
                out = memories / "learnings" / f"{_slug(subtopic)}.md"
                payload = f"# {subtopic}\n\n{content}\n"
                if out.exists() and not overwrite:
                    summary["skipped"].append(str(out))
                    continue
                out.write_text(payload, encoding="utf-8")
                summary["written"].append(str(out))
        else:
            out = memories / f"{_slug(title)}.md"
            payload = f"# {title}\n\n{body}\n"
            if out.exists() and not overwrite:
                summary["skipped"].append(str(out))
                continue
            out.write_text(payload, encoding="utf-8")
            summary["written"].append(str(out))

    logger.info(
        "Migration complete: %d written, %d skipped (overwrite=%s)",
        len(summary["written"]), len(summary["skipped"]), overwrite,
    )
    return summary


if __name__ == "__main__":
    import sys
    ws = sys.argv[1] if len(sys.argv) > 1 else "/data/workspace"
    overwrite = "--overwrite" in sys.argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = migrate(ws, overwrite=overwrite)
    print(f"Written: {len(result['written'])}")
    for p in result["written"]:
        print(f"  + {p}")
    print(f"Skipped: {len(result['skipped'])}")
    for p in result["skipped"]:
        print(f"  · {p}")
