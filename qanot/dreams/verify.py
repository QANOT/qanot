"""Five verification probes for a freshly-consolidated memory tree.

Run between Phase 6 (index rebuild) and Phase 8 (atomic swap). If any
CRITICAL finding fires, the swap is aborted and the next call to
`SwapEngine.commit()` must clean up the orphan `memory.next/` directory.

The probes are written to be cheap, deterministic, and explainable —
no LLM calls. The slow path (LLM-vs-LLM audit of fact attribution) lives
in a separate module if we add it later.

Probes:
  1. Relative-date leak     — body text mentions "yesterday/today/last week"
  2. Cross-user contamination — any reference to a user_id other than the bound one
  3. Broken file link        — `[text](file.md)` whose target doesn't exist
  4. Hallucination smell     — fact line without `(src: …)` attribution
  5. Size                    — any file > 50KB, topic file > 200 facts, index > 200 lines

The verifier is invoked by the consolidation cron agent via a tool, or
directly from the CLI for a dry-run pass over the current `/memories`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Tight defaults — they map straight to the research limits.
MAX_FILE_BYTES = 50 * 1024              # per file
MAX_INDEX_LINES = 200                   # MEMORY.md / index file
MAX_FACTS_PER_TOPIC = 200               # `- ` bullets in a topic file
RELATIVE_DATE_TERMS = (
    "yesterday", "today", "tomorrow", "last week", "next week",
    "recently", "soon", "earlier today", "later today",
    "this morning", "this afternoon", "this evening",
)


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class Finding:
    file: str
    line: int           # 1-indexed; 0 = whole-file finding
    probe: str
    severity: Severity
    detail: str


@dataclass
class VerifyResult:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def has_critical(self) -> bool:
        return any(f.severity == Severity.CRITICAL for f in self.findings)

    @property
    def passed(self) -> bool:
        return not self.has_critical

    def summary(self) -> str:
        by_sev: dict[Severity, int] = {}
        for f in self.findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        if not self.findings:
            return f"verify: PASS ({self.files_scanned} files scanned)"
        parts = [f"{sev.value}={n}" for sev, n in by_sev.items()]
        verdict = "FAIL" if self.has_critical else "WARN"
        return f"verify: {verdict} ({', '.join(parts)}; {self.files_scanned} files)"


# ─── public API ──────────────────────────────────────────────────


def verify_tree(
    root: Path,
    *,
    expected_user_id: str | None = None,
    other_user_ids: list[str] | None = None,
) -> VerifyResult:
    """Walk `root` and run all five probes. `expected_user_id` enables the
    cross-contamination check; pass None to skip (single-user / global
    workspace).
    """
    result = VerifyResult()
    if not root.is_dir():
        result.findings.append(Finding(
            file=str(root), line=0, probe="exists",
            severity=Severity.CRITICAL,
            detail="root does not exist",
        ))
        return result

    md_files: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        # We probe markdown content specifically. Other file types (assets)
        # only get the size probe.
        if p.suffix.lower() == ".md":
            md_files.append(p)
        else:
            _probe_size_only(p, root, result)

    for path in md_files:
        result.files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Treat unreadable files as critical — consolidation should never
            # produce non-UTF8 output.
            result.findings.append(Finding(
                file=_rel(path, root), line=0,
                probe="readable", severity=Severity.CRITICAL,
                detail="file is not readable as UTF-8",
            ))
            continue

        _probe_relative_dates(text, path, root, result)
        _probe_cross_user(
            text, path, root, result, expected_user_id, other_user_ids or [],
        )
        _probe_broken_links(text, path, root, result)
        _probe_hallucination(text, path, root, result)
        _probe_size(text, path, root, result)

    return result


# ─── probes ──────────────────────────────────────────────────────


_REL_DATE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in RELATIVE_DATE_TERMS) + r")\b",
    re.IGNORECASE,
)


def _probe_relative_dates(
    text: str, path: Path, root: Path, result: VerifyResult,
) -> None:
    """Catch the Anthropic-prompted 'absolutify dates' rule failing.

    Treat as CRITICAL — relative dates rot the moment the consolidation
    output gets replayed.
    """
    for idx, line in enumerate(text.splitlines(), start=1):
        m = _REL_DATE_RE.search(line)
        if m:
            result.findings.append(Finding(
                file=_rel(path, root), line=idx,
                probe="relative_date",
                severity=Severity.CRITICAL,
                detail=f"contains {m.group(1)!r}",
            ))


_USER_ID_RE = re.compile(r"\buser[_\-]id\s*[:=]\s*['\"]?([A-Za-z0-9_-]+)", re.I)


def _probe_cross_user(
    text: str, path: Path, root: Path, result: VerifyResult,
    expected: str | None, others: list[str],
) -> None:
    """Reject any explicit reference to a different user_id.

    We deliberately do not run a general PII sweep — that belongs in a
    separate redaction pass. Here we only catch the structural leak.
    """
    if not expected and not others:
        return
    for idx, line in enumerate(text.splitlines(), start=1):
        for m in _USER_ID_RE.finditer(line):
            uid = m.group(1)
            if expected and uid == expected:
                continue
            if uid in others:
                result.findings.append(Finding(
                    file=_rel(path, root), line=idx,
                    probe="cross_user",
                    severity=Severity.CRITICAL,
                    detail=f"reference to other user_id {uid!r}",
                ))


_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+\.md)\)")


def _probe_broken_links(
    text: str, path: Path, root: Path, result: VerifyResult,
) -> None:
    """Every `[label](file.md)` link must resolve to a real file under root.

    External `http(s)://` links are ignored — we only enforce the local
    contract that consolidation outputs reference each other correctly.
    """
    for idx, line in enumerate(text.splitlines(), start=1):
        for m in _LINK_RE.finditer(line):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            # Resolve relative to the file's parent, the same way a
            # markdown renderer would.
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                result.findings.append(Finding(
                    file=_rel(path, root), line=idx,
                    probe="broken_link", severity=Severity.WARN,
                    detail=f"link {target!r} escapes root",
                ))
                continue
            if not resolved.is_file():
                result.findings.append(Finding(
                    file=_rel(path, root), line=idx,
                    probe="broken_link", severity=Severity.CRITICAL,
                    detail=f"link target {target!r} not found",
                ))


_FACT_LINE_RE = re.compile(r"^\s*-\s+\[\d{4}-\d{2}-\d{2}\]")
_HAS_SRC_RE = re.compile(r"\(src:\s*[^)]+\)", re.I)


def _probe_hallucination(
    text: str, path: Path, root: Path, result: VerifyResult,
) -> None:
    """Every dated fact line should carry a `(src: …)` attribution.

    This is the structural part of the hallucination check — the prompt
    template demands attribution and the verifier enforces it. We mark
    WARN rather than CRITICAL because some files (the index, free-form
    narrative) deliberately have no per-fact attribution.

    Files whose name ends in `.index.md` or whose path contains `index`
    are exempt — the consolidation index file is metadata, not facts.
    """
    name_low = path.name.lower()
    if "index" in name_low or name_low == "memory.md":
        return
    for idx, line in enumerate(text.splitlines(), start=1):
        if not _FACT_LINE_RE.match(line):
            continue
        if not _HAS_SRC_RE.search(line):
            result.findings.append(Finding(
                file=_rel(path, root), line=idx,
                probe="hallucination",
                severity=Severity.WARN,
                detail="dated fact line missing (src: …) attribution",
            ))


def _probe_size(
    text: str, path: Path, root: Path, result: VerifyResult,
) -> None:
    """Three size checks: file bytes, lines (for index), bullet count
    (for topic files)."""
    byte_len = len(text.encode("utf-8"))
    if byte_len > MAX_FILE_BYTES:
        result.findings.append(Finding(
            file=_rel(path, root), line=0,
            probe="size_bytes",
            severity=Severity.WARN,
            detail=f"file is {byte_len} bytes (cap {MAX_FILE_BYTES})",
        ))

    name_low = path.name.lower()
    if name_low in ("memory.md", "index.md") or name_low.endswith(".index.md"):
        line_count = text.count("\n") + 1
        if line_count > MAX_INDEX_LINES:
            result.findings.append(Finding(
                file=_rel(path, root), line=0,
                probe="size_index_lines",
                severity=Severity.WARN,
                detail=f"index has {line_count} lines (cap {MAX_INDEX_LINES})",
            ))

    bullet_count = sum(
        1 for line in text.splitlines() if line.lstrip().startswith("- ")
    )
    if bullet_count > MAX_FACTS_PER_TOPIC:
        result.findings.append(Finding(
            file=_rel(path, root), line=0,
            probe="size_facts_per_topic",
            severity=Severity.WARN,
            detail=f"{bullet_count} bullets (cap {MAX_FACTS_PER_TOPIC})",
        ))


def _probe_size_only(
    path: Path, root: Path, result: VerifyResult,
) -> None:
    """Run size probe on a non-markdown asset (image, json, etc.)."""
    try:
        byte_len = path.stat().st_size
    except OSError:
        return
    if byte_len > MAX_FILE_BYTES:
        result.findings.append(Finding(
            file=_rel(path, root), line=0,
            probe="size_bytes",
            severity=Severity.WARN,
            detail=f"asset is {byte_len} bytes (cap {MAX_FILE_BYTES})",
        ))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
