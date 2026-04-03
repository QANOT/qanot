"""Shared utilities for Qanot AI."""

from __future__ import annotations

import re
import unicodedata


import os
import tempfile
from pathlib import Path


_TRUNCATION_MARKER = "\n\n... [truncated {} chars] ...\n\n"


# ── Atomic file writes ──────────────────────────────────────────────

def atomic_write(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """Write file atomically via tmp+rename to prevent corruption on crash.

    Creates a temp file in the same directory, writes content, then
    renames to the target path. Rename is atomic on POSIX systems.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# ── Secret scanning (credential leak prevention) ─────────────────────
# High-confidence regex rules based on Claude Code's gitleaks-derived patterns.
# Only patterns with distinctive prefixes — zero false positives.

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sk-ant-[a-zA-Z0-9_\-]{20,})"), "anthropic_api_key"),
    (re.compile(r"\b(sk-(?:proj|svcacct|admin)-[a-zA-Z0-9_\-]{20,})"), "openai_api_key"),
    (re.compile(r"\b(sk-[a-zA-Z0-9]{32,})"), "openai_api_key_legacy"),
    (re.compile(r"\b(AIza[\w\-]{35})\b"), "gcp_api_key"),
    (re.compile(r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b"), "aws_access_key"),
    (re.compile(r"\b(ghp_[0-9a-zA-Z]{36})\b"), "github_pat"),
    (re.compile(r"\b(gho_[0-9a-zA-Z]{36})\b"), "github_oauth"),
    (re.compile(r"\b(glpat-[\w\-]{20,})\b"), "gitlab_pat"),
    (re.compile(r"\b(xoxb-[0-9]{10,}-[0-9a-zA-Z]{20,})\b"), "slack_bot_token"),
    (re.compile(r"\b(xoxp-[0-9]{10,}-[0-9a-zA-Z]{20,})\b"), "slack_user_token"),
    (re.compile(r"\b(sk_live_[0-9a-zA-Z]{24,})\b"), "stripe_secret_key"),
    (re.compile(r"\b(sq0atp-[\w\-]{22,})\b"), "square_access_token"),
    (re.compile(r"\b(shpat_[0-9a-fA-F]{32,})\b"), "shopify_token"),
]


def scan_secrets(text: str) -> list[tuple[str, str]]:
    """Scan text for high-confidence credential patterns.

    Returns list of (secret_type, masked_value) tuples.
    Only fires on distinctive prefixes — zero false positives.
    """
    found: list[tuple[str, str]] = []
    for pattern, secret_type in _SECRET_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(1)
            masked = value[:8] + "..." + value[-4:] if len(value) > 16 else value[:4] + "..."
            found.append((secret_type, masked))
    return found


def redact_secrets(text: str) -> str:
    """Replace detected credentials with [REDACTED] placeholders."""
    for pattern, secret_type in _SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{secret_type}]", text)
    return text

# ── Unicode sanitization (prompt injection defense) ──────────────────
# Based on Claude Code's partiallySanitizeUnicode() and HackerOne #3086545.
# Strips invisible/dangerous Unicode that can be used for prompt injection.

# Zero-width and formatting characters
_DANGEROUS_RANGES = re.compile(
    "["
    "\u200b-\u200f"  # Zero-width space, joiners, LTR/RTL marks
    "\u202a-\u202e"  # LTR/RTL embedding/override
    "\u2066-\u2069"  # LTR/RTL isolates
    "\ufeff"          # Byte order mark
    "\u00ad"          # Soft hyphen (invisible)
    "\u061c"          # Arabic letter mark
    "\u180e"          # Mongolian vowel separator
    "\u2060-\u2064"  # Word joiner, invisible operators
    "\ufff9-\ufffb"  # Interlinear annotation
    "]",
    re.UNICODE,
)

# Private use area (can carry hidden instructions)
_PRIVATE_USE = re.compile("[\ue000-\uf8ff]", re.UNICODE)

# Unicode tag characters (U+E0001-U+E007F, used in HackerOne attack)
_TAG_CHARS = re.compile("[\U000e0001-\U000e007f]", re.UNICODE)


def sanitize_unicode(text: str) -> str:
    """Strip dangerous invisible Unicode from user input.

    Applies NFKC normalization then removes:
    - Zero-width spaces and joiners
    - Directional overrides/embeddings/isolates
    - Private use area characters
    - Unicode tag characters (prompt injection vector)
    - Byte order marks
    """
    # NFKC normalization (collapses composed sequences)
    text = unicodedata.normalize("NFKC", text)
    # Strip dangerous characters
    text = _DANGEROUS_RANGES.sub("", text)
    text = _PRIVATE_USE.sub("", text)
    text = _TAG_CHARS.sub("", text)
    return text


def truncate_with_marker(
    text: str,
    max_chars: int,
    head_ratio: float = 0.70,
    tail_ratio: float = 0.20,
) -> str:
    """Truncate text keeping head and tail with a marker in the middle.

    Default: keeps first 70% and last 20%, with a gap marker.
    """
    if head_ratio < 0 or tail_ratio < 0 or head_ratio + tail_ratio >= 1.0:
        raise ValueError(
            f"head_ratio and tail_ratio must be non-negative and sum to less than 1.0, "
            f"got head_ratio={head_ratio}, tail_ratio={tail_ratio}"
        )
    text_len = len(text)
    if text_len <= max_chars:
        return text
    # Upper-bound marker length: removed <= text_len so digit count never exceeds this.
    marker_overhead = len(_TRUNCATION_MARKER.format(text_len))
    budget = max(max_chars - marker_overhead, 0)
    head = int(budget * head_ratio)
    tail = int(budget * tail_ratio)
    removed = text_len - head - tail
    if removed <= 0:
        # Ratios sum to >= 1.0 for this max_chars; just hard-truncate
        return text[:max_chars]
    tail_text = text[-tail:] if tail else ""
    return text[:head] + _TRUNCATION_MARKER.format(removed) + tail_text
