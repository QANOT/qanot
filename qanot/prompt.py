"""Prompt builder — assembles system prompt from workspace files."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from qanot.utils import truncate_with_marker

logger = logging.getLogger(__name__)

MAX_FILE_CHARS = 20_000

# ── Plugin registries ──
# These are module-level singletons shared across all Agent instances.
# Registrations must happen during startup (before the event loop processes messages).
# Call freeze_plugin_registries() after startup to prevent further mutations.
_plugin_prompt_sections: list[dict] = []  # [{"name": str, "content": str}]
_plugin_template_vars: dict[str, str] = {}
_plugin_registries_frozen = False


def register_prompt_section(name: str, content: str) -> None:
    """Register a plugin prompt section. Appended after core sections.

    Must be called during startup before freeze_plugin_registries().
    """
    if _plugin_registries_frozen:
        logger.warning("Ignoring register_prompt_section('%s') — registries frozen after startup", name)
        return
    _plugin_prompt_sections.append({"name": name, "content": content})


def register_template_var(key: str, value: str) -> None:
    """Register a custom template variable for prompt injection.

    Must be called during startup before freeze_plugin_registries().
    """
    if _plugin_registries_frozen:
        logger.warning("Ignoring register_template_var('%s') — registries frozen after startup", key)
        return
    _plugin_template_vars[key] = value


def freeze_plugin_registries() -> None:
    """Freeze plugin registries after startup. Prevents runtime mutations."""
    global _plugin_registries_frozen
    _plugin_registries_frozen = True


def _truncate_content(content: str, max_chars: int) -> str:
    return truncate_with_marker(content, max_chars)
MAX_TOTAL_CHARS = 150_000

_IDENTITY_LINE = "You are Qanot AI, a personal assistant."


def _build_owner_block(owner_name: str) -> str:
    """Inject the bot's operator identity into the system prompt.

    Sourced from ``config.owner_name`` — populated automatically by
    QanotCloud during bot creation, or hand-set in the bot's config.json.
    The block is intentionally small and unambiguous: it answers the
    "kim sizning egangiz?" question deterministically so the model
    doesn't have to guess from MEMORY.md context.
    """
    name = owner_name.strip()
    if not name:
        return ""
    return (
        "## Owner Identity\n\n"
        f"Your owner / operator is **{name}**. You were created by them and "
        "you serve them as a personal assistant. When asked 'who is your "
        "owner / egang kim?', answer with this name. Do NOT infer ownership "
        "from chat participants, mentioned usernames, or partial context — "
        f"only **{name}** is your owner."
    )

# Marker to split system prompt into cacheable (above) and dynamic (below) parts.
# Anthropic provider uses this to set cache_control on the stable prefix.
_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"

# Cap on the dynamic (post-boundary) suffix. Hermes-borrow #4: keep the
# uncached portion small so we pay the full input price for as few tokens
# as possible per turn. SESSION-STATE.md / MEMORY.md / learnings / error
# notes / skill_index / active_skills / session info all share this budget.
# Sized for ~5 lessons + ~5 error notes + ~2KB MEMORY + ~1KB SESSION-STATE
# + skill content + 200 chars session info. Comfortable headroom.
MAX_DYNAMIC_SUFFIX_CHARS = 12_000


def build_system_prompt(
    workspace_dir: str = "/data/workspace",
    owner_name: str = "",
    bot_name: str = "",
    timezone_str: str = "Asia/Tashkent",
    context_percent: float = 0.0,
    total_tokens: int = 0,
    skill_path: str | None = None,
    mode: str = "full",
    user_id: str = "",
    skill_index: str = "",
    active_skills_content: str = "",
    inject_legacy_memory: bool | str = True,
) -> str:
    """Build the full system prompt from workspace files.

    Args:
        mode: Prompt assembly mode.
            ``"full"``    -- all sections (default).
            ``"minimal"`` -- SOUL.md + TOOLS.md + session info only.
            ``"none"``    -- identity line only.

    Concatenation order (full mode):
    1. SOUL.md (identity, principles)
    2. IDENTITY.md (agent name, vibe, emoji)
    3. SKILL.md (proactive agent behaviors)
    4. TOOLS.md (tool configurations)
    5. AGENTS.md (operating rules)
    6. SESSION-STATE.md (active context)
    7. USER.md excerpt (human context)
    8. BOOTSTRAP.md (first-run ritual, if exists)
    9. SOUL_APPEND.md sections (plugin personality additions)
    """
    if mode == "none":
        return _IDENTITY_LINE

    if mode not in ("full", "minimal"):
        logger.warning("Unknown prompt mode %r, falling back to 'minimal'", mode)
        mode = "minimal"

    ws = Path(workspace_dir)
    parts: list[str] = []
    total_chars = 0

    def _add(content: str) -> None:
        """Append content to parts while tracking total char budget."""
        nonlocal total_chars
        if not content:
            return
        remaining = MAX_TOTAL_CHARS - total_chars
        if remaining <= 0:
            return
        content = _truncate_content(content, min(MAX_FILE_CHARS, remaining))
        parts.append(content)
        total_chars += len(content)

    # 1. SOUL.md
    _add(_read_file(ws / "SOUL.md"))

    if mode == "full":
        # 2. IDENTITY.md (agent name, vibe, emoji)
        _add(_read_file(ws / "IDENTITY.md"))

        # 2b. Owner identity — derived from config.owner_name. Injected
        # automatically so every customer bot in QanotCloud knows who
        # its operator is, without relying on hand-edited MEMORY.md.
        # Without this, the bot would hallucinate ownership when asked
        # "who is your owner?" — we saw this with @topkeydevbot picking
        # the first plausible-looking username from group context.
        if owner_name:
            _add(_build_owner_block(owner_name))

        # 3. SKILL.md (proactive agent skill)
        if skill_path:
            _add(_read_file(Path(skill_path)))
        else:
            _add(_read_file(ws / "SKILL.md"))

    # 4. TOOLS.md (included in both full and minimal)
    _add(_read_file(ws / "TOOLS.md"))

    if mode == "full":
        # Check for plugin TOOLS files
        for p in sorted(ws.glob("*_TOOLS.md")):
            _add(_read_file(p))

        # 5. AGENTS.md
        _add(_read_file(ws / "AGENTS.md"))

        # 6. USER.md (per-user if exists, fallback to shared) — rarely changes
        user_md = _read_file(ws / "users" / str(user_id) / "USER.md") if user_id else ""
        if not user_md:
            user_md = _read_file(ws / "USER.md")
        _add(user_md)

        # 7. BOOTSTRAP.md — first-run ritual (only if it exists) — static
        if bootstrap := _read_file(ws / "BOOTSTRAP.md"):
            _add(bootstrap)

    # Hardcoded behavioral rules — static
    parts.append(
        "## Tool Call Style\n"
        "Default: do not narrate routine tool calls (just call the tool silently).\n"
        "Narrate only when it helps: multi-step work, complex problems, or when the user explicitly asks.\n"
        "Do not proactively mention internal file names (USER.md, IDENTITY.md, SOUL.md, MEMORY.md, SESSION-STATE.md, etc.).\n"
        "Do not say things like 'I am updating USER.md' or 'Let me save to IDENTITY.md' during routine operations.\n"
        "However, if the user explicitly asks to see or edit these files, comply — they are the owner.\n"
        "When a tool call fails, retry silently or work around it. Never show error details about file operations to the user.\n"
        "All internal bookkeeping is invisible unless the user specifically asks about it.\n"
        "\n"
        "## Presentation Hygiene\n"
        "Tool internals stay in your reasoning — never in the user-facing answer. This includes:\n"
        "- Numeric IDs (user_id 854, task_id 27, project_id, board_column_id)\n"
        "- Schema/database terms (column names, status slugs like 'completed' / 'incomplete', SQL fragments)\n"
        "- Tool names (`topkey_list_employees`, `web_search`, etc.) — describe the action in human language instead\n"
        "- Pagination, retries, candidate searches ('I tried IDs 753, 854...', 'searching page 2 of 5')\n"
        "- Endpoint paths, JSON keys, API shapes\n"
        "Speak the user's domain language: names, dates, counts, statuses translated to natural words.\n"
        "Bad: 'Yangi user IDlar topildi: 753, 854. Ozodbek — ID 854 ekan.'\n"
        "Good: 'Ozodbek topildi, hisoboti tayyorlanmoqda...'\n"
        "If a search needs multiple attempts, show one calm progress line ('🔍 izlanmoqda...'), not the iteration.\n"
        "\n"
        "## Learning from Experience\n"
        "You have two tools for self-improvement: `evolve_soul` and `recall_lessons`.\n"
        "Recent lessons auto-inject into your prompt at session start (see 'Recent Learnings' "
        "section if any exist) — read them and apply them.\n"
        "When to call `evolve_soul`:\n"
        "- The user corrected you on something non-obvious that's likely to recur\n"
        "- You discovered a non-obvious data pattern (e.g. 'default_customer is per-company, not always id=1')\n"
        "- You realized a workflow that should be your DEFAULT next time, not a one-off\n"
        "Don't call `evolve_soul` for: trivia, things already in MEMORY.md/SOUL.md/USER.md, "
        "user-specific preferences (the WAL handles those automatically), or temporary observations.\n"
        "Lessons should be ONE actionable sentence — what to DO differently, not what happened.\n"
        "Call `recall_lessons` BEFORE attempting a problem similar to past ones, when the auto-injected "
        "5 most recent lessons aren't enough.\n"
        "\n"
        "## Programmatic Tool Calling (`execute_code`)\n"
        "For multi-step orchestration where intermediate tool results would clutter your context "
        "(e.g. 'for each task in this list, fetch its history, count completions, summarize'), "
        "use `execute_code` instead of N round-trip tool calls. Inside the script, tools are "
        "available as `await qanot_tools.tool_name(**kwargs)`; only `print()` output returns to "
        "you. Token savings on >3-step turns are typically 50-90%.\n"
        "Use it when:\n"
        "- You'd otherwise call the same tool many times (loops over a list)\n"
        "- You need to filter/aggregate tool results before deciding the answer\n"
        "- You're chaining 3+ dependent tool calls\n"
        "Don't use it for:\n"
        "- A single tool call (just call the tool directly)\n"
        "- When you need the raw structured response shown to the user (execute_code returns only print() output)\n"
        "Plan your final `print()` to be the summary you want to see — anything not printed is gone.\n"
        "\n"
        "## Cache-Stable Prefix\n"
        "Your system prompt has a stable prefix (this section, plus SOUL/IDENTITY/TOOLS/AGENTS/USER/plugins) "
        "and a dynamic suffix (MEMORY/SESSION-STATE/lessons/skills/session-info). The stable prefix is cached "
        "by Anthropic — keeping it identical across turns is ~10× cheaper than re-billing it every request.\n"
        "Don't mutate stable-prefix files DURING a conversation: SOUL.md, IDENTITY.md, TOOLS.md, AGENTS.md, "
        "USER.md, BOOTSTRAP.md. If you must edit them (rare — at user request only), expect cache invalidation "
        "for the rest of that user's session. The dynamic-suffix files (MEMORY.md, SESSION-STATE.md, lessons "
        "ledger, daily notes) ARE safe to update mid-conversation — they live below the cache boundary."
    )

    # Plugin prompt sections — static (changes only when plugins added/removed)
    for section in _plugin_prompt_sections:
        _add(f"# {section['name']}\n\n{section['content']}")

    # ── CACHE BOUNDARY MARKER ──
    # Everything ABOVE = stable (SOUL, IDENTITY, TOOLS, AGENTS, USER, plugins)
    # Everything BELOW = changes frequently (memory, session state, skills, session info)
    parts.append(_CACHE_BOUNDARY)

    # Hermes-borrow #4: track post-boundary char usage separately and
    # enforce MAX_DYNAMIC_SUFFIX_CHARS. Anthropic prompt cache reads are
    # ~10× cheaper than uncached input; the post-boundary content is the
    # ONLY part billed at full input price every turn. Keeping it small
    # is the highest-leverage cost lever after caching itself.
    dynamic_chars = 0

    def _add_dynamic(content: str, label: str = "") -> None:
        """Append to post-boundary section, respecting dynamic budget."""
        nonlocal dynamic_chars, total_chars
        if not content:
            return
        remaining_dynamic = MAX_DYNAMIC_SUFFIX_CHARS - dynamic_chars
        remaining_total = MAX_TOTAL_CHARS - total_chars
        budget = min(remaining_dynamic, remaining_total, MAX_FILE_CHARS)
        if budget <= 0:
            logger.debug(
                "Dynamic suffix budget exhausted before %s (used %d/%d)",
                label or "section", dynamic_chars, MAX_DYNAMIC_SUFFIX_CHARS,
            )
            return
        content = _truncate_content(content, budget)
        parts.append(content)
        dynamic_chars += len(content)
        total_chars += len(content)

    if mode == "full":
        # Legacy memory injection: SESSION-STATE.md + MEMORY.md in the prompt.
        # When disabled (the production setting once the memory tool is
        # adopted), the agent must `memory view /memories/` or `rag_search`
        # to retrieve facts. Saves ~1400 tokens/turn.
        #
        # The "auto" mode (default in 2026-05-14+) inspects the memos/
        # directory and turns legacy injection OFF once enough feedback-
        # typed memos exist. This makes the cutover free of explicit
        # config changes — adoption is observable from the filesystem.
        legacy_inject = _resolve_legacy_injection(
            inject_legacy_memory, workspace_dir,
        )
        if legacy_inject:
            # SESSION-STATE.md — changes on every WAL write (DYNAMIC)
            if state := _read_file(ws / "SESSION-STATE.md"):
                _add_dynamic(f"# Current Session State\n\n{state}", "SESSION-STATE")

            # MEMORY.md — changes when durable facts saved (DYNAMIC)
            if memory := _read_file(ws / "MEMORY.md"):
                _add_dynamic(f"# Your Long-Term Memory\n\n{memory}", "MEMORY")

        # Self-improvement: most recent lessons + last-7-days error notes.
        # Closes the bidirectional learning gap (audit found daily notes
        # were write-only; agent never read its own past errors). ~150-300
        # tokens total — small enough to stay in every prompt.
        try:
            from qanot.learnings import (
                format_recent_learnings_block,
                format_error_lessons_block,
            )
            if learnings_block := format_recent_learnings_block(workspace_dir, limit=5):
                _add_dynamic(learnings_block, "learnings")
            if errors_block := format_error_lessons_block(workspace_dir, days=7):
                _add_dynamic(errors_block, "error-notes")
        except Exception as e:
            logger.warning("Learnings injection failed: %s", e)

        # Skill index — may change per turn
        if skill_index:
            _add_dynamic(skill_index, "skill_index")

        # Active skill content — changes per turn
        if active_skills_content:
            _add_dynamic(active_skills_content, "active_skills")

    # Session info — changes every request.
    # IMPORTANT: agent has repeatedly miscomputed dates when only given UTC,
    # so we show BOTH local (the user's civil date/time) AND UTC with offset.
    # Never make the model do timezone arithmetic.
    now_utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo(timezone_str)
        now_local = now_utc.astimezone(local_tz)
    except Exception:
        now_local = now_utc
    date_str = now_local.strftime("%Y-%m-%d")
    time_str_local = now_local.strftime("%H:%M")
    offset_str = now_local.strftime("%z")
    offset_fmt = f"{offset_str[:3]}:{offset_str[3:]}" if offset_str else ""
    utc_date_str = now_utc.strftime("%Y-%m-%d")
    utc_time_str = now_utc.strftime("%H:%M")
    weekday = now_local.strftime("%A")

    parts.append(
        f"# Session Info\n"
        f"- **Local date & time:** {date_str} {time_str_local} ({weekday}, {timezone_str}, UTC{offset_fmt})\n"
        f"- **UTC reference:** {utc_date_str}T{utc_time_str}:00Z\n"
        f"- **Context Usage:** {context_percent:.1f}%\n"
        f"- **Total Tokens:** {total_tokens:,}\n"
        f"- **Reminder:** when scheduling cron/reminders, pass local time with the offset "
        f"(e.g. `2026-03-12T17:00:00{offset_fmt or '+05:00'}`) — do NOT do timezone math yourself.\n"
        + ("- **WARNING:** Context above 50% — Working Buffer Protocol ACTIVE\n" if context_percent >= 50 else "")
    )

    # Variable injection
    full = "\n\n---\n\n".join(parts)
    full = full.replace("{date}", date_str)
    full = full.replace("{bot_name}", bot_name)
    full = full.replace("{owner_name}", owner_name)
    full = full.replace("{timezone}", timezone_str)

    # Plugin template vars
    for key, value in _plugin_template_vars.items():
        full = full.replace(key, value)

    return full


def _resolve_legacy_injection(setting: bool | str, workspace_dir: str) -> bool:
    """Resolve the ``inject_legacy_memory`` config to a concrete boolean.

    Modes:
      - True   → always inject
      - False  → never inject
      - "auto" → inject only when the memos/ directory has fewer than
        the configured threshold of feedback-typed memos. The threshold
        is treated as 3 here; the canonical knob lives in Config but
        we don't import Config to avoid a circular dep — the value is
        small and stable enough to inline.
    """
    if isinstance(setting, bool):
        return setting
    if not isinstance(setting, str):
        return True  # safety: unknown type → keep legacy behavior
    if setting.lower() != "auto":
        return True
    # Auto: count feedback memos. We do this lazily and cheaply — a
    # directory glob, no parsing.
    try:
        from qanot.memos import MemoStore, MemoType
        store = MemoStore(workspace_dir)
        feedback = sum(
            1 for m in store.list_all() if m.type == MemoType.FEEDBACK
        )
    except Exception as exc:  # noqa: BLE001 — never let auto-check break prompt
        logger.warning("legacy injection auto check failed: %s", exc)
        return True
    return feedback < 3


def _read_file(path: Path) -> str:
    """Read a file if it exists, return empty string otherwise.

    Content is returned as-is; callers (e.g. ``_add``) are responsible
    for applying truncation within the overall character budget.
    """
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
    return ""
