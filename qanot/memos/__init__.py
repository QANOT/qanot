"""Qanot memo subsystem — file-per-fact memory with scoped recall.

Implements the Claude Code v2.1.139 memory recipe (Piebald-AI mirror):
one fact per file, ``description:`` frontmatter for router selection,
``metadata.type`` for rules-vs-facts distinction. Extends the recipe
with ``metadata.user`` + ``metadata.thread`` for Global / User / Thread
/ User+Thread scope hierarchy — required for our multi-user Telegram
deployment where a thread-scoped style rule must not leak into other
threads.

Package layout (under construction):
    .spec     — MemoSpec dataclass + parser + render (this is the schema)
    .store    — atomic file-per-memo writes + list/load by scope
    .router   — FastEmbed-based per-turn relevance selector
    .prompt   — render selected memos as ``<system-reminder>`` blocks
"""

from __future__ import annotations

from .spec import (
    MAX_BODY_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MemoSpec,
    MemoSpecError,
    MemoType,
    parse_memo_file,
    render_memo,
    split_frontmatter,
)
from .store import (
    ARCHIVE_DIR_NAME,
    MEMOS_DIR_NAME,
    MemoStore,
    StoreError,
    WriteResult,
)
from .router import (
    DEFAULT_BUDGET_CHARS,
    DEFAULT_THRESHOLD,
    DEFAULT_TOP_K,
    EmbedFn,
    MemoRouter,
    RouteResult,
    Selection,
)
from .prompt import (
    estimate_token_cost,
    render_system_reminder,
)
from .extractor import (
    EXTRACTOR_MODEL,
    ExtractedMemo,
    extract_memo,
)

__all__ = [
    # Spec
    "MemoSpec",
    "MemoSpecError",
    "MemoType",
    "parse_memo_file",
    "render_memo",
    "split_frontmatter",
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_NAME_CHARS",
    # Store
    "MemoStore",
    "StoreError",
    "WriteResult",
    "MEMOS_DIR_NAME",
    "ARCHIVE_DIR_NAME",
    # Router
    "MemoRouter",
    "RouteResult",
    "Selection",
    "EmbedFn",
    "DEFAULT_TOP_K",
    "DEFAULT_THRESHOLD",
    "DEFAULT_BUDGET_CHARS",
    # Prompt
    "render_system_reminder",
    "estimate_token_cost",
    # Extractor
    "extract_memo",
    "ExtractedMemo",
    "EXTRACTOR_MODEL",
]
