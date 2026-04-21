"""Skill system — Markdown-only capability extensions.

Skills are SKILL.md files that provide structured instructions to the agent.
No code needed — just Markdown with YAML frontmatter.

Directory structure:
    workspace/skills/my-skill/SKILL.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Security limits
MAX_SKILL_CHARS = 6000       # Per-skill content limit
MAX_DESCRIPTION_CHARS = 1024
MAX_NAME_CHARS = 64
MAX_ACTIVE_SKILLS = 3        # Max skills in prompt per turn
NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$')

# Injection deny-list patterns
INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?previous\s+instructions', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+', re.IGNORECASE),
    re.compile(r'system\s*:\s*', re.IGNORECASE),
    re.compile(r'<\s*system\s*>', re.IGNORECASE),
    re.compile(r'override\s+(your\s+)?instructions', re.IGNORECASE),
]


@dataclass
class Skill:
    """Parsed skill from SKILL.md."""

    name: str
    description: str
    content: str  # Full SKILL.md body (after frontmatter)
    path: Path
    allowed_tools: list[str] = field(default_factory=list)
    user_invocable: bool = True
    auto_invoke: bool = True  # Can model invoke automatically
    # Claude Code compatibility — optional frontmatter fields.
    when_to_use: str = ""   # progressive-disclosure hint; richer than description
    version: str = ""

    @property
    def index_entry(self) -> str:
        """Compact representation for prompt injection.

        Prefers `when_to_use` when the skill author provided it — that's the
        Claude Code convention for telling the model WHEN to reach for the
        skill, not just what it does. Falls back to `description`.
        """
        hint = self.when_to_use or self.description
        return f"- {self.name}: {hint}"


def discover_skills(workspace_dir: str) -> list[Skill]:
    """Discover all skills in workspace/skills/ directory."""
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.is_dir():
        return []

    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        skill = _parse_skill(skill_md)
        if skill:
            skills.append(skill)

    logger.info("Discovered %d skills in %s", len(skills), skills_dir)
    return skills


def _parse_skill(path: Path) -> Skill | None:
    """Parse a SKILL.md file into a Skill object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to read skill %s: %s", path, e)
        return None

    # Parse YAML frontmatter
    frontmatter, body = _split_frontmatter(raw)
    if not frontmatter:
        logger.warning("Skill %s has no frontmatter", path)
        return None

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    # Validate name
    if not name or not isinstance(name, str):
        logger.warning("Skill %s has invalid name: %s", path, name)
        return None
    if len(name) > MAX_NAME_CHARS or not NAME_PATTERN.match(name):
        logger.warning("Skill %s has invalid name: %s", path, name)
        return None

    # Validate description
    if not description or not isinstance(description, str):
        logger.warning("Skill %s has no description", path)
        return None
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]

    # Replace {skill_dir} placeholder with actual path
    skill_dir = str(path.parent)
    content = body.strip().replace("{skill_dir}", skill_dir)

    # Sanitize content
    if len(content) > MAX_SKILL_CHARS:
        content = content[:MAX_SKILL_CHARS] + "\n\n[Truncated — skill exceeds size limit]"

    # Check for injection patterns
    for pattern in INJECTION_PATTERNS:
        if pattern.search(content):
            logger.warning("Skill %s contains suspicious pattern, skipping", name)
            return None

    # Parse optional fields
    raw_tools = frontmatter.get("allowed-tools", "")
    allowed_tools = raw_tools.split() if isinstance(raw_tools, str) and raw_tools else []
    user_invocable = frontmatter.get("user-invocable", True)
    auto_invoke = not frontmatter.get("disable-auto", False)
    # Claude Code compat fields — `when_to_use` improves auto-matching and
    # is used as the primary index-entry hint when provided. Anthropic's
    # catalogue skills don't use it, but skill-creator etc. emit both keys
    # (with and without underscore). Accept both.
    when_to_use = (
        frontmatter.get("when_to_use")
        or frontmatter.get("when-to-use")
        or ""
    )
    version = str(frontmatter.get("version") or "")

    return Skill(
        name=name,
        description=description,
        content=content,
        path=path,
        allowed_tools=allowed_tools,
        user_invocable=user_invocable if isinstance(user_invocable, bool) else str(user_invocable).lower() != "false",
        auto_invoke=auto_invoke,
        when_to_use=str(when_to_use).strip()[:400],
        version=version.strip()[:40],
    )


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from Markdown body.

    Uses a simple key: value parser to avoid PyYAML dependency.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("---", 3)
    if end == -1:
        return {}, text

    yaml_str = text[3:end].strip()
    body = text[end + 3:].strip()

    # Simple YAML parser (no dependency on PyYAML)
    frontmatter: dict = {}
    for line in yaml_str.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Handle booleans
            if value.lower() in ("true", "yes"):
                value = True  # type: ignore[assignment]
            elif value.lower() in ("false", "no"):
                value = False  # type: ignore[assignment]
            frontmatter[key] = value

    return frontmatter, body


def build_skill_index(skills: list[Skill]) -> str:
    """Build compact skill index for system prompt injection."""
    if not skills:
        return ""

    auto_skills = [s for s in skills if s.auto_invoke]
    if not auto_skills:
        return ""

    lines = ["Available skills (activate when relevant):"]
    for skill in auto_skills:
        lines.append(skill.index_entry)

    return "\n".join(lines)


def match_skills(skills: list[Skill], user_message: str) -> list[Skill]:
    """Find skills relevant to the user message.

    Uses simple keyword matching against skill name and description.
    Returns up to MAX_ACTIVE_SKILLS matches.
    """
    if not skills or not user_message:
        return []

    msg_lower = user_message.lower()
    scored: list[tuple[int, Skill]] = []

    for skill in skills:
        if not skill.auto_invoke:
            continue

        score = 0
        # Name match
        if skill.name in msg_lower:
            score += 10

        # Description keyword match
        desc_words = set(skill.description.lower().split())
        msg_words = set(msg_lower.split())
        overlap = desc_words & msg_words
        # Filter out common stop words
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "va", "bu", "da", "ham", "uchun", "bilan", "ga", "ni", "dan",
        }
        meaningful_overlap = overlap - stop_words
        score += len(meaningful_overlap) * 2

        if score > 0:
            scored.append((score, skill))

    # Sort by score descending, take top N
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:MAX_ACTIVE_SKILLS]]


def format_active_skills(skills: list[Skill]) -> str:
    """Format active skill contents for system prompt injection."""
    if not skills:
        return ""

    parts = []
    for skill in skills:
        parts.append(f"## Active Skill: {skill.name}\n\n{skill.content}")

    return "\n\n---\n\n".join(parts)
