"""Skill registry commands: install, remove, search, info, list, verify, new.

Sibling of ``plugin_cmd.py`` — same UX, but skills are markdown bundles
not Python code. Backed by ``qanot.skills.registry`` (static index.json
in QANOT/qanot-skills, git-clone install, sha256 lock, trust tiers).
"""

from __future__ import annotations

import json
from pathlib import Path

from qanot.cli.utils import (
    LOGO,
    _bold,
    _cyan,
    _dim,
    _find_config,
    _green,
    _red,
    _yellow,
)


def cmd_skill(args: list[str]) -> None:
    if not args:
        _skill_help()
        return
    subcmd = args[0]
    dispatch = {
        "install": _skill_install,
        "remove": _skill_remove,
        "uninstall": _skill_remove,
        "search": _skill_search,
        "info": _skill_info,
        "list": _skill_list,
        "ls": _skill_list,
        "verify": _skill_verify,
        "new": _skill_new,
    }
    handler = dispatch.get(subcmd)
    if handler:
        handler(args[1:])
    else:
        print(_red(f"Unknown skill command: {subcmd}"))
        _skill_help()


def _skill_help() -> None:
    print(LOGO)
    print("Usage: qanot skill <command>")
    print()
    print("Commands:")
    print("  install <name|url>   Install a skill from the registry or git URL")
    print("  remove <name>        Remove an installed skill")
    print("  search <keyword>     Search the skill registry")
    print("  info <name>          Show registry details for a skill")
    print("  list                 List installed skills (workspace + user)")
    print("  verify               Check installed skill integrity hashes")
    print("  new <name>           Scaffold a new SKILL.md bundle")
    print()
    print("Flags:")
    print("  --user               Install to ~/.qanot/skills/ (user-level)")
    print("  --with-scripts       Keep scripts/ (stripped by default — safer)")
    print("  --allow-unverified   Permit installing an 'unverified' tier skill")
    print("  --force              Overwrite an existing skill of the same name")
    print("  --registry=<url>     Use a custom registry index URL")
    print()
    print("Examples:")
    print("  qanot skill search vat")
    print("  qanot skill install vat-calculation-uz")
    print("  qanot skill install https://github.com/user/qanot-skill-foo")
    print("  qanot skill verify")
    print()


# ── helpers ────────────────────────────────────────────────


def _get_skills_dir(args: list[str]) -> Path:
    """Resolve the workspace skills directory from config or default."""
    remaining = [a for a in args if not a.startswith("--")]
    config_path = _find_config(remaining)
    if config_path:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            ws = raw.get("workspace_dir")
            if ws:
                return Path(ws) / "skills"
            return config_path.parent / "workspace" / "skills"
        except Exception:
            pass
    return Path.cwd() / "workspace" / "skills"


def _extract_flag(args: list[str], prefix: str) -> str | None:
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


# ── install ────────────────────────────────────────────────


def _skill_install(args: list[str]) -> None:
    if not args or args[0].startswith("--"):
        print(_red("Usage: qanot skill install <name|git-url> [--user]"))
        return

    source = args[0]
    user_level = "--user" in args
    with_scripts = "--with-scripts" in args
    allow_unverified = "--allow-unverified" in args
    force = "--force" in args
    registry_url = _extract_flag(args, "--registry=")
    skills_dir = _get_skills_dir(args)

    print(LOGO)
    print(_bold("Skill Install"))
    print()
    print(f"  Source: {_cyan(source)}")
    target_label = "~/.qanot/skills/" if user_level else str(skills_dir)
    print(f"  Target: {_dim(target_label)}")
    if with_scripts:
        print(f"  {_yellow('--with-scripts: scripts/ will be kept (review it!)')}")
    print()

    from qanot.skills.registry import install_skill, DEFAULT_SKILL_REGISTRY_URL

    kwargs: dict = {}
    if registry_url:
        kwargs["registry_url"] = registry_url

    ok, msg = install_skill(
        source, skills_dir,
        user_level=user_level,
        with_scripts=with_scripts,
        allow_unverified=allow_unverified,
        force=force,
        **kwargs,
    )

    if ok:
        print(f"  {_green('OK')} {msg}")
        print()
        print("  The skill is now discoverable by the agent on next load.")
        print(f"  {_dim('No config change needed — skills auto-load from skills/.')}")
    else:
        print(f"  {_red('FAILED')} {msg}")
    print()


# ── remove ─────────────────────────────────────────────────


def _skill_remove(args: list[str]) -> None:
    if not args:
        print(_red("Usage: qanot skill remove <name>"))
        return
    name = args[0]
    skills_dir = _get_skills_dir(args)
    print(LOGO)
    print(_bold("Skill Remove"))
    print()
    from qanot.skills.registry import remove_skill
    ok, msg = remove_skill(name, skills_dir)
    print(f"  {_green('OK') if ok else _red('FAILED')} {msg}")
    print()


# ── search ─────────────────────────────────────────────────


def _skill_search(args: list[str]) -> None:
    if not args:
        print(_red("Usage: qanot skill search <keyword>"))
        return
    query = " ".join(a for a in args if not a.startswith("--"))
    registry_url = _extract_flag(args, "--registry=")
    print(LOGO)
    print(f"  Searching skills for: {_cyan(query)}")
    print()
    from qanot.skills.registry import (
        search_skill_registry, DEFAULT_SKILL_REGISTRY_URL,
    )
    url = registry_url or DEFAULT_SKILL_REGISTRY_URL
    results = search_skill_registry(query, url)
    if not results:
        print(f"  {_dim('No skills found matching')} '{query}'")
        print()
        return
    for e in results:
        tier_c = (
            _green(e.tier) if e.tier == "official"
            else _yellow(e.tier) if e.tier == "unverified"
            else _cyan(e.tier)
        )
        print(f"  {_bold(e.name)} {_dim('v' + e.version)} [{tier_c}]")
        print(f"    {e.description}")
        if e.tags:
            print(f"    {_dim('tags: ' + ', '.join(e.tags))}")
        print()


# ── info ───────────────────────────────────────────────────


def _skill_info(args: list[str]) -> None:
    if not args:
        print(_red("Usage: qanot skill info <name>"))
        return
    name = args[0]
    registry_url = _extract_flag(args, "--registry=")
    from qanot.skills.registry import (
        skill_registry_info, DEFAULT_SKILL_REGISTRY_URL,
    )
    url = registry_url or DEFAULT_SKILL_REGISTRY_URL
    e = skill_registry_info(name, url)
    print(LOGO)
    if e is None:
        print(f"  {_red('Not found in registry')}: {name}")
        print()
        return
    print(_bold(e.name) + _dim(f"  v{e.version}"))
    print(f"  {e.description}")
    print()
    print(f"  Author:   {e.author or _dim('—')}")
    print(f"  Category: {e.category or _dim('—')}")
    print(f"  Tier:     {e.tier}")
    print(f"  Tags:     {', '.join(e.tags) or _dim('—')}")
    print(f"  License:  {e.license or _dim('—')}")
    print(f"  Repo:     {_dim(e.git_url)}")
    if e.commit:
        print(f"  Pinned:   {_dim(e.commit[:12])}")
    print()


# ── list ───────────────────────────────────────────────────


def _skill_list(args: list[str]) -> None:
    skills_dir = _get_skills_dir(args)
    print(LOGO)
    print(_bold("Installed Skills"))
    print()
    from qanot.skills.loader import SkillLoader
    from qanot.skills.registry import USER_SKILLS_DIR, read_skill_lock

    shown = 0
    for label, root in (
        ("workspace", skills_dir),
        ("user", USER_SKILLS_DIR),
    ):
        if not root.is_dir():
            continue
        try:
            loaded, _ = SkillLoader(root.parent).load()
        except Exception:
            loaded = []
        lock = read_skill_lock(root)
        if not loaded and not lock:
            continue
        print(f"  {_cyan(label)}  {_dim(str(root))}")
        for ls in loaded:
            spec = ls.spec
            le = lock.get(spec.name)
            ver = f" v{le.version}" if le and le.version else ""
            print(f"    {_bold(spec.name)}{_dim(ver)} — {spec.description[:60]}")
            shown += 1
        print()
    if shown == 0:
        print(f"  {_dim('No skills installed. Try: qanot skill search <kw>')}")
        print()


# ── verify ─────────────────────────────────────────────────


def _skill_verify(args: list[str]) -> None:
    skills_dir = _get_skills_dir(args)
    print(LOGO)
    print(_bold("Skill Integrity Verify"))
    print()
    from qanot.skills.registry import verify_skills
    rows = verify_skills(skills_dir)
    if not rows:
        print(f"  {_dim('No locked skills to verify.')}")
        print()
        return
    for r in rows:
        st = r["status"]
        if st == "ok":
            print(f"  {_green('OK')}    {r['name']}")
        elif st == "drift":
            exp = r["expected"]
            act = r["actual"]
            detail = _dim(f"expected {exp}… got {act}…")
            print(f"  {_red('DRIFT')} {r['name']} {detail}")
        elif st == "missing":
            print(f"  {_red('GONE')}  {r['name']} {_dim('(in lock, not on disk)')}")
        else:
            print(f"  {_yellow('WARN')}  {r['name']} {_dim(st)}")
    print()


# ── new (scaffold) ─────────────────────────────────────────


_SKILL_TEMPLATE = """---
name: {name}
description: "TODO: one line — WHAT this skill does and WHEN to use it."
metadata:
  version: 0.1.0
  author: ""
---

# {name}

TODO: step-by-step instructions for the agent. Pure markdown — no code.

## When to use

TODO: describe the trigger conditions.

## Steps

1. TODO
2. TODO
"""


def _skill_new(args: list[str]) -> None:
    if not args:
        print(_red("Usage: qanot skill new <name>"))
        return
    raw = args[0].strip().lower()
    from qanot.skills.registry import _sanitize_name
    name, ok = _sanitize_name(raw)
    if not ok:
        print(_red(f"Invalid skill name: {raw!r} (use a-z 0-9 hyphen)"))
        return
    skills_dir = _get_skills_dir(args)
    dest = skills_dir / name
    if dest.exists():
        print(_red(f"Skill '{name}' already exists at {dest}"))
        return
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        _SKILL_TEMPLATE.format(name=name), encoding="utf-8",
    )
    print(LOGO)
    print(f"  {_green('OK')} scaffolded {_bold(name)}")
    print(f"  {_dim(str(dest / 'SKILL.md'))}")
    print()
    print("  Edit SKILL.md, then test by mentioning the skill to the agent.")
    print()
