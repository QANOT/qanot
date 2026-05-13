"""Agent-facing skill management tools — routed through SkillStore.

These tools let the agent (and the curator subagent) manage its own skill
library. They sit alongside the legacy ``qanot/tools/skill_tools.py``
which keeps the older, looser surface intact — new code should prefer
the names registered here.

Tool surface:
    skill_create        — new skill from a successful workflow
    skill_edit          — replace body / description
    skill_patch         — find-and-replace within body
    skill_archive       — move to .archive/ (recoverable)
    skill_unarchive     — restore from .archive/
    skill_view          — read SKILL.md + usage record
    skill_list          — directory listing with usage stats
    skill_pin           — exempt from curator metabolism
    skill_unpin         — re-enable curator metabolism

Every write goes through SkillStore so atomic writes, .history/ snapshots,
scan-on-write, and .usage.json bookkeeping are guaranteed regardless of
which path the caller used.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from qanot.skills import (
    ARCHIVE_DIR_NAME,
    SkillStore,
    StoreError,
    UsageStore,
    parse_skill_file,
)

logger = logging.getLogger(__name__)


# Tool timeouts (seconds). Skill writes are short-running; the longest
# operation is the scan_text scan on body, which is a synchronous regex
# pass over a ≤32KB string — well under a second.
_SKILL_TOOL_TIMEOUT = 10.0


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


def register_skill_manager_tools(
    registry, workspace_dir: str, reload_callback=None,
) -> None:
    """Register the skill manager tool surface.

    `reload_callback` is called after any state-changing tool succeeds so
    the agent's in-memory skill cache (qanot/agent/agent.py:_skills) gets
    invalidated. Pass None to skip hot reload (curator subagent path —
    it runs in a fresh process and re-loads anyway).
    """
    skills_root = Path(workspace_dir) / "skills"

    def _store() -> SkillStore:
        return SkillStore(skills_root)

    def _reload() -> None:
        if reload_callback is None:
            return
        try:
            reload_callback()
        except Exception as exc:  # noqa: BLE001 — never let reload failures break the tool
            logger.warning("skill reload callback failed: %s", exc)

    # ─── skill_create ────────────────────────────────────────────

    async def skill_create(params: dict) -> str:
        name = (params.get("name") or "").strip()
        description = (params.get("description") or "").strip()
        body = (params.get("body") or params.get("instructions") or "").strip()
        agent_created = bool(params.get("agent_created", True))

        if not name:
            return _err("name is required")
        if not description:
            return _err("description is required")
        if not body:
            return _err("body is required")

        try:
            result = _store().create(
                name, description, body, agent_created=agent_created,
            )
        except StoreError as exc:
            return _err(str(exc), name=name)

        _reload()
        return _ok({
            "name": result.name,
            "path": str(result.path),
            "action": result.action,
            "scan_verdict": result.scan_verdict.value,
        })

    registry.register(
        name="skill_create",
        description=(
            "Create a new skill — a reusable, agentskills.io-compliant SKILL.md "
            "bundle the agent (or a future session) can invoke. Use this when "
            "a successful workflow is likely to recur. The body should be "
            "step-by-step instructions in Markdown; reference any bundled "
            "scripts via relative paths (resolved at activation time)."
        ),
        parameters={
            "type": "object",
            "required": ["name", "description", "body"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Kebab-case skill name (e.g. 'gmail-summary'). "
                        "Must match the directory name we'll create."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "One-paragraph description — this is the trigger the "
                        "agent uses to decide when to activate. Include both "
                        "WHAT the skill does and WHEN to reach for it."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Markdown body — the skill's actual instructions. "
                        "Keep under 5000 tokens; longer references go under "
                        "references/."
                    ),
                },
                "agent_created": {
                    "type": "boolean",
                    "description": (
                        "True (default) marks this as agent-authored — the "
                        "curator may archive it if unused. Set False for "
                        "user-supplied skills the curator must leave alone."
                    ),
                },
            },
        },
        handler=skill_create,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_edit ──────────────────────────────────────────────

    async def skill_edit(params: dict) -> str:
        name = (params.get("name") or "").strip()
        new_body = params.get("new_body")
        new_description = params.get("new_description")
        if not name:
            return _err("name is required")
        if new_body is None:
            return _err("new_body is required")

        try:
            result = _store().edit(
                name, new_body, new_description=new_description,
            )
        except StoreError as exc:
            return _err(str(exc), name=name)

        _reload()
        return _ok({
            "name": result.name,
            "action": result.action,
            "history_snapshot": (
                str(result.history_snapshot) if result.history_snapshot else None
            ),
            "scan_verdict": result.scan_verdict.value,
        })

    registry.register(
        name="skill_edit",
        description=(
            "Replace the body (and optionally description) of an existing skill. "
            "The prior SKILL.md is snapshot to .history/ so the edit is "
            "reversible. Use for rewrites; use skill_patch for targeted edits."
        ),
        parameters={
            "type": "object",
            "required": ["name", "new_body"],
            "properties": {
                "name": {"type": "string"},
                "new_body": {
                    "type": "string",
                    "description": "Full replacement body — overwrites the old one.",
                },
                "new_description": {
                    "type": "string",
                    "description": (
                        "Optional new description. Omit to keep the existing one."
                    ),
                },
            },
        },
        handler=skill_edit,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_patch ─────────────────────────────────────────────

    async def skill_patch(params: dict) -> str:
        name = (params.get("name") or "").strip()
        old_substring = params.get("old_substring") or ""
        new_substring = params.get("new_substring")
        if not name:
            return _err("name is required")
        if not old_substring:
            return _err("old_substring is required")
        if new_substring is None:
            return _err("new_substring is required (use '' to delete)")

        try:
            result = _store().patch_body(name, old_substring, new_substring)
        except StoreError as exc:
            return _err(str(exc), name=name)

        _reload()
        return _ok({
            "name": result.name,
            "action": "patched",
            "history_snapshot": (
                str(result.history_snapshot) if result.history_snapshot else None
            ),
        })

    registry.register(
        name="skill_patch",
        description=(
            "Find-and-replace within an existing skill's body. The "
            "old_substring must appear EXACTLY ONCE — patches must be "
            "unambiguous so repeated runs are safe. Use to clarify an "
            "instruction, fix a typo, or tighten a trigger phrase."
        ),
        parameters={
            "type": "object",
            "required": ["name", "old_substring", "new_substring"],
            "properties": {
                "name": {"type": "string"},
                "old_substring": {
                    "type": "string",
                    "description": (
                        "Substring to find — must appear EXACTLY ONCE in the "
                        "current body. Quote enough surrounding context to be "
                        "unique."
                    ),
                },
                "new_substring": {
                    "type": "string",
                    "description": "Replacement text. Empty string deletes.",
                },
            },
        },
        handler=skill_patch,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_archive / skill_unarchive ─────────────────────────

    async def skill_archive(params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            return _err("name is required")
        try:
            result = _store().archive(name)
        except StoreError as exc:
            return _err(str(exc), name=name)
        _reload()
        return _ok({"name": result.name, "action": "archived",
                    "path": str(result.path)})

    registry.register(
        name="skill_archive",
        description=(
            "Move a skill to .archive/ — recoverable via skill_unarchive. "
            "Use this instead of deleting. The curator uses this for skills "
            "idle >90 days; the agent should use it when a skill is clearly "
            "superseded by a better one."
        ),
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        handler=skill_archive,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    async def skill_unarchive(params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            return _err("name is required")
        try:
            result = _store().unarchive(name)
        except StoreError as exc:
            return _err(str(exc), name=name)
        _reload()
        return _ok({"name": result.name, "action": "unarchived",
                    "path": str(result.path)})

    registry.register(
        name="skill_unarchive",
        description="Restore a previously archived skill back to the active library.",
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        handler=skill_unarchive,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_view ──────────────────────────────────────────────

    async def skill_view(params: dict) -> str:
        name = (params.get("name") or "").strip()
        archived = bool(params.get("archived", False))
        if not name:
            return _err("name is required")
        root = skills_root / (ARCHIVE_DIR_NAME if archived else "")
        skill_md = root / name / "SKILL.md"
        if not skill_md.is_file():
            return _err(
                f"skill not found at {skill_md}",
                hint="set archived=true if it's in .archive/",
            )
        try:
            spec = parse_skill_file(skill_md)
        except Exception as exc:  # noqa: BLE001 — view should not raise to caller
            return _err(f"failed to parse: {exc}")
        usage_row = UsageStore(skills_root).get(name)
        return _ok({
            "name": spec.name,
            "description": spec.description,
            "body": spec.body,
            "version": spec.version,
            "author": spec.author,
            "platforms": spec.platforms,
            "path": str(skill_md),
            "usage": usage_row.to_dict() if usage_row else None,
        })

    registry.register(
        name="skill_view",
        description=(
            "Read a skill's SKILL.md plus its usage stats (use_count, "
            "last_used_at, pinned, agent_created, status). Pass archived=true "
            "to look in .archive/."
        ),
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "archived": {
                    "type": "boolean",
                    "description": "True to look in .archive/. Default false.",
                },
            },
        },
        handler=skill_view,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_list ──────────────────────────────────────────────

    async def skill_list(params: dict) -> str:
        include_archived = bool(params.get("include_archived", False))
        usage = UsageStore(skills_root).load()
        live: list[dict[str, Any]] = []
        if skills_root.is_dir():
            for entry in sorted(skills_root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.is_file():
                    continue
                rec = usage.get(entry.name)
                live.append({
                    "name": entry.name,
                    "use_count": rec.use_count if rec else 0,
                    "last_used_at": rec.last_used_at if rec else "",
                    "pinned": rec.pinned if rec else False,
                    "agent_created": rec.agent_created if rec else False,
                    "status": rec.status if rec else "active",
                })
        result: dict[str, Any] = {"active": live, "count": len(live)}
        if include_archived:
            archived_dir = skills_root / ARCHIVE_DIR_NAME
            archived: list[dict[str, Any]] = []
            if archived_dir.is_dir():
                for entry in sorted(archived_dir.iterdir()):
                    if not entry.is_dir():
                        continue
                    archived.append({"name": entry.name})
            result["archived"] = archived
            result["archived_count"] = len(archived)
        return _ok(result)

    registry.register(
        name="skill_list",
        description=(
            "List all skills with usage stats. Pass include_archived=true to "
            "also show .archive/ contents."
        ),
        parameters={
            "type": "object",
            "properties": {
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived skills in the response.",
                },
            },
        },
        handler=skill_list,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_pin / skill_unpin ─────────────────────────────────

    async def skill_pin(params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            return _err("name is required")
        skill_md = skills_root / name / "SKILL.md"
        if not skill_md.is_file():
            return _err(f"skill not found: {name}")
        UsageStore(skills_root).set_pinned(name, True)
        return _ok({"name": name, "pinned": True})

    registry.register(
        name="skill_pin",
        description=(
            "Pin a skill so the curator never archives or marks it stale, "
            "regardless of idle time. Use for skills the user has expressed "
            "intent to keep, or for foundational skills the agent depends on."
        ),
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        handler=skill_pin,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    async def skill_unpin(params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            return _err("name is required")
        UsageStore(skills_root).set_pinned(name, False)
        return _ok({"name": name, "pinned": False})

    registry.register(
        name="skill_unpin",
        description="Remove a skill's pin, returning it to normal curator metabolism.",
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        handler=skill_unpin,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )
