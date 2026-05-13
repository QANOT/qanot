"""Prompt-injection logic — index, match, activate.

Progressive disclosure (`agentskills.io/specification`): the system prompt
carries only `name + description` for each skill at discovery time; the full
SKILL.md body lands in context only when the skill activates. Hermes
issues #2045, #17649, #22620 are all instances of the same bug — broadcasting
full skill bodies on every turn — and we sidestep them by keeping injection
to the index until activation actually fires.

We give the caller three primitives:
- `build_skill_index(...)`     — produce the index block for the system prompt
- `match_skills(...)`          — pick candidates for auto-activation
- `format_active_skills(...)`  — render the full body for an activated skill

The match function is intentionally simple — token + name overlap, plus a
small Uzbek/English stop-word filter. Semantic matching via the RAG embedder
is a later upgrade (Phase 1: curator semantic dedup); putting it on the hot
path of every user message would double the latency floor.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .guard import Verdict
from .loader import LoadedSkill
from .spec import SkillSpec

logger = logging.getLogger(__name__)


# Hermes broadcasts up to ~243 skills per turn (issue #22620). We cap at 5
# auto-active skills per message because: (a) Telegram chats are short, the
# user isn't asking the bot to juggle 5+ workflows at once; (b) prompt budget
# math — 5 skills × ~6K chars ≈ 30K chars body, fits comfortably under the
# system prompt budget defined in qanot/prompt.py.
MAX_ACTIVE_SKILLS = 5

# Same number of suggestions to keep in the index. We expose the *names* of
# more skills via the index than we activate per turn — the index is cheap
# (~50 tokens / skill) but activation injects the full body.
MAX_INDEX_ENTRIES = 50


# Stop-word filter for the keyword matcher. Carries English + the most common
# Uzbek function words; we err on the side of dropping rather than including
# so the match score reflects actual topic words.
_STOP_WORDS = frozenset({
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "and", "or",
    "not", "no", "yes", "do", "does", "did", "have", "has", "had", "this",
    "that", "these", "those", "i", "you", "he", "she", "it", "we", "they",
    # Uzbek (Latin + the most common bot-prompt words)
    "va", "bu", "u", "da", "ga", "ni", "dan", "ham", "uchun", "bilan",
    "lekin", "ammo", "yoki", "shu", "men", "sen", "biz", "siz", "ular",
    "qil", "qildi", "kerak", "mumkin", "bo'lsa", "agar", "shunday",
})


def build_skill_index(skills: Iterable[LoadedSkill | SkillSpec]) -> str:
    """Render the compact skill index for the system prompt.

    Accepts either LoadedSkill or bare SkillSpec to keep call sites simple.
    Skips DANGEROUS scanner verdicts entirely — they should not have been
    loaded in the first place under default settings, but a paranoid second
    layer is cheap.
    """
    entries: list[str] = []
    for item in skills:
        spec, verdict = _unpack(item)
        if verdict == Verdict.DANGEROUS:
            continue
        if len(entries) >= MAX_INDEX_ENTRIES:
            break
        entries.append(f"- {spec.index_hint}")
    if not entries:
        return ""
    header = (
        "Available skills — reach for one when the user's request matches its "
        "description. To activate a skill, mention its name in your response "
        "or ask `which skill should I use for ...`."
    )
    return header + "\n" + "\n".join(entries)


def match_skills(
    skills: Iterable[LoadedSkill | SkillSpec],
    user_message: str,
    *,
    limit: int = MAX_ACTIVE_SKILLS,
) -> list[LoadedSkill | SkillSpec]:
    """Pick up to `limit` skills whose name or description overlaps with
    the user message.

    Scoring:
    - Exact name substring          → +10
    - Each meaningful token overlap → +2
    - Description-trigger phrases   → +5 (we look for "use this skill when"
      or "this skill is for" then snip the next clause)

    Items with score == 0 are excluded. Ties broken by name.
    """
    msg = (user_message or "").lower()
    if not msg:
        return []

    msg_tokens = _tokenize(msg)
    scored: list[tuple[int, str, LoadedSkill | SkillSpec]] = []
    for item in skills:
        spec, verdict = _unpack(item)
        if verdict == Verdict.DANGEROUS:
            continue
        score = _score(spec, msg, msg_tokens)
        if score > 0:
            scored.append((score, spec.name, item))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [item for _, _, item in scored[:limit]]


def format_active_skills(
    skills: Iterable[LoadedSkill | SkillSpec],
) -> str:
    """Render the bodies of activated skills as a system-prompt block.

    Each skill is delimited by a heading so the model can tell them apart.
    Bodies are surfaced verbatim — template substitution (e.g., `${SKILL_DIR}`)
    is applied here so a skill author can refer to bundle assets by abstract
    name.
    """
    blocks: list[str] = []
    for item in skills:
        spec, verdict = _unpack(item)
        if verdict == Verdict.DANGEROUS:
            continue
        body = _substitute_templates(spec)
        warn = ""
        if verdict == Verdict.CAUTION:
            warn = (
                "\n[scanner: this skill flagged caution-level signals at load "
                "time; review the body before relying on its instructions]\n"
            )
        blocks.append(f"## Active Skill: {spec.name}\n{warn}{body}")
    return "\n\n---\n\n".join(blocks)


# ─── internals ────────────────────────────────────────────────────


def _unpack(item: LoadedSkill | SkillSpec) -> tuple[SkillSpec, Verdict]:
    if isinstance(item, LoadedSkill):
        return item.spec, item.verdict
    return item, Verdict.SAFE


def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in text.split():
        # Strip surrounding punctuation; keep internal hyphens (skill names).
        word = raw.strip(".,!?;:()[]{}\"'`").lower()
        if not word or word in _STOP_WORDS:
            continue
        if len(word) < 2:
            continue
        tokens.add(word)
    return tokens


def _score(spec: SkillSpec, msg: str, msg_tokens: set[str]) -> int:
    score = 0
    if spec.name in msg:
        score += 10
    desc_tokens = _tokenize(spec.description)
    overlap = desc_tokens & msg_tokens
    score += len(overlap) * 2
    # `description` often contains "Use this skill when ..." style cues —
    # we boost when the user's message echoes that clause directly.
    for trigger in ("use this skill when", "use when", "this skill is for"):
        idx = spec.description.lower().find(trigger)
        if idx == -1:
            continue
        clause = spec.description[idx + len(trigger): idx + len(trigger) + 80].lower()
        clause_tokens = _tokenize(clause)
        if clause_tokens & msg_tokens:
            score += 5
            break
    return score


def _substitute_templates(spec: SkillSpec) -> str:
    """Replace `${QANOT_SKILL_DIR}` and `${SKILL_DIR}` (legacy alias) with
    the absolute skill directory path. We deliberately do NOT support
    inline shell `!`cmd`` substitution — Hermes' guard treats it as a
    `caution` finding because it executes at prompt-build time, which
    destroys the audit trail.
    """
    body = spec.body
    skill_dir = str(spec.directory)
    # Two aliases — `SKILL_DIR` keeps legacy SKILL.md files working without
    # edits, `QANOT_SKILL_DIR` is the going-forward name.
    body = body.replace("${QANOT_SKILL_DIR}", skill_dir)
    body = body.replace("${SKILL_DIR}", skill_dir)
    body = body.replace("{skill_dir}", skill_dir)  # legacy curly-brace form
    return body
