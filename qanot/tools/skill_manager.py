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
    SkillLoader,
    SkillStore,
    StoreError,
    UsageStore,
    parse_skill_file,
)
from qanot.skills.gate import GateResult, pre_create_check

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
    registry,
    workspace_dir: str,
    reload_callback=None,
    *,
    trajectory_tokens_callback=None,
) -> None:
    """Register the skill manager tool surface.

    `reload_callback` is called after any state-changing tool succeeds so
    the agent's in-memory skill cache (qanot/agent/agent.py:_skills) gets
    invalidated. Pass None to skip hot reload (curator subagent path —
    it runs in a fresh process and re-loads anyway).

    `trajectory_tokens_callback` returns the current trajectory token cost
    (input + output across the active turn/session). Used by the AAMC cost
    gate at `skill_create` time — skills produced by trivially cheap
    trajectories are rejected. If None, the cost gate is effectively
    disabled (callback returns 0).
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

    def _trajectory_tokens() -> int:
        if trajectory_tokens_callback is None:
            return 0
        try:
            return int(trajectory_tokens_callback() or 0)
        except Exception as exc:  # noqa: BLE001 — callback failures must not break tools
            logger.warning("trajectory_tokens callback failed: %s", exc)
            return 0

    def _load_existing_skills() -> list:
        # Lazy-instantiate a SkillLoader for the gate. We don't keep a
        # long-lived loader because the cache invalidation contract here
        # is "every tool call sees fresh disk state" — important for
        # the curator subagent path which writes via the same tools.
        try:
            loaded, _ = SkillLoader(workspace_dir).load()
        except Exception as exc:  # noqa: BLE001 — gate must not break tools
            logger.warning("skill loader failed in gate: %s", exc)
            return []
        return loaded

    # ─── skill_create ────────────────────────────────────────────

    async def skill_create(params: dict) -> str:
        name = (params.get("name") or "").strip()
        description = (params.get("description") or "").strip()
        body = (params.get("body") or params.get("instructions") or "").strip()
        agent_created = bool(params.get("agent_created", True))
        bypass_gate = bool(params.get("bypass_gate", False))

        if not name:
            return _err("name is required")
        if not description:
            return _err("description is required")
        if not body:
            return _err("body is required")

        # AAMC cost gate — runs only for agent-created skills, since
        # user-authored skills are presumed deliberate. Bypass via
        # `bypass_gate=true` for the curator subagent path or when the
        # user has explicitly asked for a skill to be created.
        if agent_created and not bypass_gate:
            gate = pre_create_check(
                name, description, body,
                existing_skills=_load_existing_skills(),
                trajectory_tokens=_trajectory_tokens(),
            )
            if not gate.allow:
                logger.info(
                    "skill_create blocked by gate: %s (score=%d, matched=%s)",
                    gate.reason, gate.score, gate.matched_skill,
                )
                return _err(
                    f"gate rejected: {gate.reason}",
                    name=name,
                    matched_skill=gate.matched_skill,
                    suggestion=gate.suggestion,
                )
            gate_summary = {
                "score": gate.score,
                "matched_skill": gate.matched_skill,
                "reason": gate.reason,
            }
        else:
            gate_summary = {"reason": "bypassed" if bypass_gate else "user-authored"}

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
            "gate": gate_summary,
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
                "bypass_gate": {
                    "type": "boolean",
                    "description": (
                        "Skip the AAMC cost / semantic-novelty gate. Use ONLY "
                        "when the user has explicitly asked you to save this "
                        "as a skill, or when the curator subagent is restoring "
                        "a known-good skill. Default false."
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

    # ─── skill_search (registry — read only) ─────────────────────

    async def skill_search(params: dict) -> str:
        query = (params.get("query") or "").strip()
        if not query:
            return _err("query is required")
        try:
            from qanot.skills.registry import search_skill_registry
            matches = search_skill_registry(query)
        except Exception as exc:  # noqa: BLE001 — registry fetch must not break the turn
            return _err(f"registry search failed: {exc}")
        rows = [
            {
                "name": e.name,
                "description": e.description,
                "version": e.version,
                "tier": e.tier,
                "tags": e.tags,
            }
            for e in matches[:10]
        ]
        return _ok({"query": query, "count": len(rows), "results": rows})

    registry.register(
        name="skill_search",
        description=(
            "Search the Qanot skill registry (the public marketplace) for "
            "shareable SKILL.md workflows by keyword. READ-ONLY — this only "
            "lists what's available; it does NOT install anything. When the "
            "user needs a capability you don't have a local skill for, "
            "search here first, then SHOW the user the matches and ASK "
            "before calling skill_install."
        ),
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keyword to match against skill name, description, "
                        "category, and tags (e.g. 'vat', 'sotuv hisoboti')."
                    ),
                },
            },
        },
        handler=skill_search,
        category="core",
        timeout=_SKILL_TOOL_TIMEOUT,
    )

    # ─── skill_install (registry — confirmation-gated) ───────────

    async def skill_install(params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            return _err("name is required")
        confirmed = bool(params.get("user_confirmed", False))
        if not confirmed:
            return _err(
                "user_confirmed is required. Installing a skill pulls "
                "external content into the workspace — you MUST show the "
                "user the skill name + description + tier and get an "
                "explicit yes BEFORE calling this with user_confirmed=true.",
                name=name,
            )
        try:
            from qanot.skills.registry import (
                install_skill, skill_registry_info,
            )
        except Exception as exc:  # noqa: BLE001
            return _err(f"registry unavailable: {exc}")

        # Hard tier gate. The agent may install official/community after
        # user confirmation; unverified is operator-only via the CLI
        # (`qanot skill install <name> --allow-unverified`). This mirrors
        # the boundary every mature framework keeps — Hermes/OpenClaw do
        # NOT expose hub-install to the model at all; we allow it but
        # only for reviewed tiers and only with explicit user consent.
        entry = skill_registry_info(name)
        if entry is None:
            return _err(
                f"'{name}' not found in the registry. Use skill_search "
                f"first to find the exact name.",
                name=name,
            )
        if entry.tier == "unverified":
            return _err(
                f"'{name}' is UNVERIFIED — the agent cannot install "
                f"unverified skills. Tell the user to run "
                f"`qanot skill install {name} --allow-unverified` "
                f"themselves if they trust the author.",
                name=name, tier=entry.tier,
            )

        ok, msg = install_skill(
            name, skills_root,
            with_scripts=False,        # never pull executables autonomously
            allow_unverified=False,    # belt-and-suspenders with the gate above
            force=bool(params.get("force", False)),
        )
        if not ok:
            return _err(f"install failed: {msg}", name=name)
        _reload()
        return _ok({
            "name": name,
            "tier": entry.tier,
            "message": msg,
            "note": "Skill loaded — usable from your next turn.",
        })

    registry.register(
        name="skill_install",
        description=(
            "Install a skill from the Qanot registry into the workspace. "
            "GUARDRAILS: (1) you MUST call skill_search first and SHOW the "
            "user the candidate(s); (2) you MUST get an explicit user "
            "'yes' and only then call this with user_confirmed=true; "
            "(3) 'unverified' tier skills are refused — operator-only via "
            "CLI. The bundle is security-scanned and integrity-checked "
            "(sha256) before it lands; scripts/ are stripped. Never "
            "auto-install without the user asking for the capability."
        ),
        parameters={
            "type": "object",
            "required": ["name", "user_confirmed"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact skill name from skill_search results.",
                },
                "user_confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true ONLY after you showed the user the skill "
                        "details and they explicitly approved the install."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": "Overwrite an existing skill of the same name.",
                },
            },
        },
        handler=skill_install,
        category="core",
        timeout=30.0,  # network: git clone + scan
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
