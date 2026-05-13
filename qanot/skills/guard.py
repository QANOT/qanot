"""Skill content security scanner.

Detects prompt-injection patterns, invisible-unicode tricks, and structural
red flags in SKILL.md bundles. The Hermes scanner missed two real attacks
(#7072 — `tools/skills_guard.py` dynamic-import bypass, CVSS 7.7; #8884 —
DESCRIPTION.md never scanned) because it whitelisted by file extension. We
scan *every* text-decodable file inside the bundle.

Verdicts:
    SAFE      — no findings, skill can be loaded freely
    CAUTION   — soft signals (suspicious phrasing); load with a warning
    DANGEROUS — hard match on a known attack class; refuse to load

The scanner is regex-based and intentionally false-positive-leaning. Skills
are static text we control via the bot operator, so refusing on suspicion
is cheaper than a successful jailbreak.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# Structural caps. A skill bundle larger than these is almost always either
# a mistake (someone dumped a repo into the skill dir) or hostile. The number
# of files is deliberately small — agentskills.io recommends 1-3 references,
# not a directory of 50.
MAX_BUNDLE_FILES = 50
MAX_BUNDLE_SIZE_BYTES = 1024 * 1024  # 1 MiB
MAX_SINGLE_FILE_BYTES = 256 * 1024    # 256 KiB

# Files we always scan, regardless of extension.
_ALWAYS_SCAN_NAMES = {
    "SKILL.md",
    "skill.md",
    "DESCRIPTION.md",
    "description.md",
    "INSTRUCTIONS.md",
    "README.md",
    "USAGE.md",
}


class Verdict(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


@dataclass
class Finding:
    """A single security signal."""

    file: str          # relative path within the skill bundle
    line: int          # 1-indexed, 0 for whole-file findings
    severity: Verdict  # CAUTION or DANGEROUS — SAFE is the absence of findings
    category: str      # one of the keys in PATTERNS
    snippet: str       # short matched span, truncated to 120 chars


@dataclass
class ScanResult:
    """Aggregate scan output. `verdict` is the max severity of any finding."""

    verdict: Verdict = Verdict.SAFE
    findings: list[Finding] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return self.verdict == Verdict.SAFE

    def to_summary(self, *, limit: int = 5) -> str:
        if self.safe:
            return "scan: SAFE"
        head = self.findings[:limit]
        more = max(0, len(self.findings) - len(head))
        lines = [f"scan: {self.verdict.value.upper()} ({len(self.findings)} findings)"]
        for f in head:
            lines.append(
                f"  [{f.severity.value}/{f.category}] {f.file}:{f.line} {f.snippet}"
            )
        if more:
            lines.append(f"  …and {more} more")
        return "\n".join(lines)


# ─────────────────────── Pattern catalogue ───────────────────────────


# (category, severity, compiled_regex, label_for_snippet)
_INJECTION = [
    # Prompt-injection openers — classic phrasings.
    ("injection", Verdict.DANGEROUS, re.compile(
        r"ignore\s+(?:all\s+|the\s+|your\s+|previous\s+)+instructions", re.I)),
    ("injection", Verdict.DANGEROUS, re.compile(
        r"disregard\s+(?:all\s+|the\s+|your\s+|previous\s+)+(?:instructions|rules|prompt)", re.I)),
    ("injection", Verdict.DANGEROUS, re.compile(
        r"override\s+(?:your\s+)?(?:instructions|system\s+prompt|rules)", re.I)),
    ("injection", Verdict.DANGEROUS, re.compile(
        r"forget\s+(?:everything|all\s+previous|your\s+training)", re.I)),
    ("injection", Verdict.CAUTION, re.compile(
        r"you\s+are\s+now\s+(?!a\s+helpful)", re.I)),
    ("injection", Verdict.CAUTION, re.compile(
        r"pretend\s+(?:you\s+are|to\s+be)\s+", re.I)),
    ("injection", Verdict.CAUTION, re.compile(
        r"act\s+as\s+(?:if\s+you\s+are\s+)?(?:a|an)\s+different", re.I)),
    # Tag-spoofing: skill content fakes a system message.
    ("tag_spoof", Verdict.DANGEROUS, re.compile(
        r"<\s*system\s*>", re.I)),
    ("tag_spoof", Verdict.DANGEROUS, re.compile(
        r"<\s*\|im_start\|\s*>\s*system", re.I)),
    ("tag_spoof", Verdict.DANGEROUS, re.compile(
        r"^\s*system\s*:", re.I | re.M)),
    ("tag_spoof", Verdict.DANGEROUS, re.compile(
        r"<\s*/?\s*assistant\s*>", re.I)),
    ("tag_spoof", Verdict.CAUTION, re.compile(
        r"\[INST\]|\[/INST\]", re.I)),
]

_EXFIL = [
    # Trying to read or send credentials.
    ("exfil", Verdict.DANGEROUS, re.compile(
        r"\b(?:env|environ|ENV)\b[^a-zA-Z]{0,4}\b(?:get|read|dump|all)\b", re.I)),
    ("exfil", Verdict.DANGEROUS, re.compile(
        r"\bos\.environ\b", re.I)),
    ("exfil", Verdict.DANGEROUS, re.compile(
        r"~/\.ssh/|/etc/passwd|/etc/shadow|~/\.aws/credentials", re.I)),
    ("exfil", Verdict.DANGEROUS, re.compile(
        r"(?:curl|wget|fetch).{0,40}(?:--data|POST|@\$|@~)", re.I)),
]

_DESTRUCTIVE = [
    ("destructive", Verdict.DANGEROUS, re.compile(
        r"\brm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*\b", re.I)),
    ("destructive", Verdict.DANGEROUS, re.compile(
        r"\bsudo\s+rm\b", re.I)),
    ("destructive", Verdict.DANGEROUS, re.compile(
        r":\(\)\s*\{\s*:\|:&\s*\}\s*;", re.I)),  # fork bomb
    ("destructive", Verdict.DANGEROUS, re.compile(
        r"\bdd\s+if=[^\s]+\s+of=/dev/(?:sd[a-z]|nvme|disk)", re.I)),
    ("destructive", Verdict.DANGEROUS, re.compile(
        r"\bmkfs\.\w+\s+/dev/", re.I)),
    ("destructive", Verdict.CAUTION, re.compile(
        r"\b(?:DROP|TRUNCATE)\s+(?:TABLE|DATABASE)\b", re.I)),
]

_OBFUSCATION = [
    # Dynamic import + string-built attribute access (Hermes #7072 pattern).
    ("obfuscation", Verdict.DANGEROUS, re.compile(
        r"importlib\.import_module|__import__\s*\(", re.I)),
    ("obfuscation", Verdict.DANGEROUS, re.compile(
        r"\beval\s*\(|\bexec\s*\(|\bcompile\s*\(", re.I)),
    ("obfuscation", Verdict.DANGEROUS, re.compile(
        r"base64\.b64decode\s*\(.{0,40}\)\.decode\(\)\s*\)?", re.I)),
    ("obfuscation", Verdict.CAUTION, re.compile(
        r"chr\s*\(\s*\d+\s*\)\s*\+", re.I)),
]

_NETWORK = [
    ("network", Verdict.CAUTION, re.compile(
        r"https?://(?:[\w-]+\.)*(?:bit\.ly|tinyurl\.com|ngrok\.io|requestbin)", re.I)),
    ("network", Verdict.CAUTION, re.compile(
        r"\bnc\s+-l|\bnetcat\s+-l|reverse\s+shell", re.I)),
]

# Path-traversal trick attempting to escape skill dir.
_TRAVERSAL = [
    ("traversal", Verdict.DANGEROUS, re.compile(
        r"\.\./\.\.|\.\.\\\.\.|~/\.\.|/etc/|/root/", re.I)),
]

# Suspicious frontmatter overrides — `allowed-tools` is experimental and a
# textbook approval-bypass when honoured naively.
_FRONTMATTER_OVERRIDE = [
    ("frontmatter", Verdict.CAUTION, re.compile(
        r"^\s*allowed-tools\s*:", re.M | re.I)),
]


_ALL_PATTERNS = (
    _INJECTION + _EXFIL + _DESTRUCTIVE + _OBFUSCATION
    + _NETWORK + _TRAVERSAL + _FRONTMATTER_OVERRIDE
)


# Invisible-unicode catalogue. Hermes' scanner short-circuits at the first
# match per line — we count *every* occurrence so attackers can't hide payloads
# behind a single decoy character on the same line.
_INVISIBLE_CHARS = {
    "​": "ZERO WIDTH SPACE",
    "‌": "ZERO WIDTH NON-JOINER",
    "‍": "ZERO WIDTH JOINER",
    "‎": "LEFT-TO-RIGHT MARK",
    "‏": "RIGHT-TO-LEFT MARK",
    "‪": "LEFT-TO-RIGHT EMBEDDING",
    "‫": "RIGHT-TO-LEFT EMBEDDING",
    "‬": "POP DIRECTIONAL FORMATTING",
    "‭": "LEFT-TO-RIGHT OVERRIDE",
    "‮": "RIGHT-TO-LEFT OVERRIDE",
    "⁦": "LEFT-TO-RIGHT ISOLATE",
    "⁧": "RIGHT-TO-LEFT ISOLATE",
    "⁨": "FIRST STRONG ISOLATE",
    "⁩": "POP DIRECTIONAL ISOLATE",
    "﻿": "ZERO WIDTH NO-BREAK SPACE",
}


# ─────────────────────── Scanner ─────────────────────────────────────


def scan_skill_bundle(skill_dir: Path) -> ScanResult:
    """Scan every text-decodable file in `skill_dir` recursively.

    Returns a ScanResult whose `verdict` is the max severity found. The
    caller decides whether to load the skill: most call sites refuse on
    DANGEROUS, warn-and-load on CAUTION, load silently on SAFE.
    """
    result = ScanResult()

    if not skill_dir.is_dir():
        result.verdict = Verdict.DANGEROUS
        result.findings.append(Finding(
            file=str(skill_dir), line=0, severity=Verdict.DANGEROUS,
            category="structural", snippet="not a directory",
        ))
        return result

    # Structural checks first — file count, total size, single-file cap.
    files: list[Path] = []
    total_size = 0
    for entry in skill_dir.rglob("*"):
        if entry.is_symlink():
            target = entry.resolve()
            if not _is_within(target, skill_dir.resolve()):
                _add_finding(result, entry, 0, Verdict.DANGEROUS,
                             "symlink_escape", f"symlink to {target}")
                continue
        if not entry.is_file():
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        if size > MAX_SINGLE_FILE_BYTES:
            _add_finding(result, entry, 0, Verdict.DANGEROUS,
                         "size_per_file", f"{size} bytes")
            continue
        files.append(entry)
        total_size += size

    if len(files) > MAX_BUNDLE_FILES:
        _add_finding(result, skill_dir, 0, Verdict.DANGEROUS,
                     "size_bundle_count", f"{len(files)} files")
    if total_size > MAX_BUNDLE_SIZE_BYTES:
        _add_finding(result, skill_dir, 0, Verdict.DANGEROUS,
                     "size_bundle_bytes", f"{total_size} bytes")

    for entry in files:
        _scan_file(entry, skill_dir, result)

    return result


def scan_text(text: str, label: str = "<inline>") -> ScanResult:
    """Scan a single in-memory string. Used for newly-created skill bodies
    before they touch disk.
    """
    result = ScanResult()
    _scan_lines(text.splitlines(), label, result)
    _scan_invisibles(text, label, result)
    return result


def _scan_file(path: Path, root: Path, result: ScanResult) -> None:
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    name = path.name

    # Decide whether to read as text. We don't trust extensions alone —
    # any UTF-8-decodable file under the per-file size cap is scanned.
    try:
        raw = path.read_bytes()
    except OSError:
        return

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary asset (image, font, etc.) — structural checks already passed.
        # We deliberately do not scan binary content; if you ship a malicious
        # PNG, you're using the wrong attack surface.
        return

    _scan_lines(text.splitlines(), rel, result)
    _scan_invisibles(text, rel, result)

    if name in _ALWAYS_SCAN_NAMES:
        # Mark presence so we can audit later that key files were scanned.
        logger.debug("scanned %s as always-scan file", rel)


def _scan_lines(lines: list[str], label: str, result: ScanResult) -> None:
    for idx, line in enumerate(lines, start=1):
        for category, severity, pattern in _ALL_PATTERNS:
            m = pattern.search(line)
            if m:
                snippet = m.group(0)
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                _add_finding(result, label, idx, severity, category, snippet)


def _scan_invisibles(text: str, label: str, result: ScanResult) -> None:
    """Count every invisible-char occurrence. The point isn't accuracy on
    location — it's denying the attacker the trick of hiding the second
    payload behind a single decoy character on the same line.
    """
    for ch, descr in _INVISIBLE_CHARS.items():
        count = text.count(ch)
        if count == 0:
            continue
        # Find the first line offset for the report; tally goes in snippet.
        first_idx = text.index(ch)
        line_no = text.count("\n", 0, first_idx) + 1
        _add_finding(
            result, label, line_no, Verdict.CAUTION, "invisible",
            f"{descr} (x{count})",
        )


def _add_finding(
    result: ScanResult,
    file: str | Path,
    line: int,
    severity: Verdict,
    category: str,
    snippet: str,
) -> None:
    if isinstance(file, Path):
        file = str(file)
    result.findings.append(Finding(
        file=file, line=line, severity=severity,
        category=category, snippet=snippet,
    ))
    # Severity escalation: SAFE < CAUTION < DANGEROUS.
    order = {Verdict.SAFE: 0, Verdict.CAUTION: 1, Verdict.DANGEROUS: 2}
    if order[severity] > order[result.verdict]:
        result.verdict = severity


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False
