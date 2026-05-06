"""Skill management tools — agent can create, list, and run skill scripts.

Enables self-improving behavior: agent creates reusable SKILL.md files
with optional scripts that automate repetitive tasks.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


_HEADING_RE = None  # lazy compile


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_with_delimiters, body). Empty frontmatter on miss."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end < 0:
        return "", text
    boundary = end + 4  # past the closing '\n---'
    if boundary < len(text) and text[boundary] == "\n":
        boundary += 1
    return text[:boundary], text[boundary:]


def _find_section(body: str, heading: str) -> tuple[int, int] | None:
    """Locate a markdown section by exact heading match.

    Returns (start_of_heading_line, end_of_section_exclusive) or None.
    The section ends at the start of the next heading at the same OR
    higher level (fewer #), or end of file.
    """
    import re
    target_level = len(heading) - len(heading.lstrip("#"))
    if target_level == 0:
        return None
    target_text = heading.strip()

    lines = body.splitlines(keepends=True)
    start_line = None
    for i, line in enumerate(lines):
        if line.rstrip("\n") == target_text:
            start_line = i
            break
    if start_line is None:
        return None

    end_line = len(lines)
    same_or_higher = re.compile(r"^#{1," + str(target_level) + r"}\s+\S")
    for j in range(start_line + 1, len(lines)):
        if same_or_higher.match(lines[j]):
            end_line = j
            break

    start_offset = sum(len(lines[k]) for k in range(start_line))
    end_offset = sum(len(lines[k]) for k in range(end_line))
    return start_offset, end_offset


def _apply_patch(
    *,
    original: str,
    mode: str,
    content: str,
    section: str,
) -> tuple[str | None, str]:
    """Apply a patch to a SKILL.md body. Returns (new_text_or_None, description).

    Returns (None, ...) when the requested section can't be found
    (replace_section / append_to_section modes).
    """
    fm, body = _split_frontmatter(original)

    if mode == "append":
        sep = "\n" if body and not body.endswith("\n") else ""
        new_body = body + sep + content
        return fm + new_body, f"appended {len(content)} bytes"

    if mode == "prepend":
        new_body = content + ("\n" if not content.endswith("\n") else "") + body
        return fm + new_body, f"prepended {len(content)} bytes"

    span = _find_section(body, section)
    if span is None:
        return None, f"section {section!r} not found"

    start, end = span
    section_block = body[start:end]
    # Capture the heading line so we can preserve it across replace.
    heading_end = section_block.find("\n")
    if heading_end < 0:
        heading_end = len(section_block)
    heading_line = section_block[: heading_end + 1]

    if mode == "replace_section":
        new_block = heading_line + (content if content.endswith("\n") else content + "\n")
        new_body = body[:start] + new_block + body[end:]
        return fm + new_body, f"replaced body of {section!r}"

    if mode == "append_to_section":
        # Strip trailing whitespace from existing block, append, restore newline.
        existing = section_block.rstrip("\n")
        addition = content if content.endswith("\n") else content + "\n"
        new_block = existing + "\n" + addition
        new_body = body[:start] + new_block + body[end:]
        return fm + new_body, f"appended to {section!r}"

    return None, f"unknown mode {mode!r}"


def register_skill_tools(registry, workspace_dir: str, reload_callback=None) -> None:
    """Register skill management tools.

    Args:
        reload_callback: Called after skill creation to hot-reload skills.
    """

    skills_dir = Path(workspace_dir) / "skills"

    async def create_skill(params: dict) -> str:
        """Create a new skill with SKILL.md and optional scripts."""
        name = params.get("name", "").strip()
        description = params.get("description", "").strip()
        instructions = params.get("instructions", "").strip()
        script_name = params.get("script_name", "")
        script_content = params.get("script_content", "")

        if not name:
            return json.dumps({"error": "name is required"})
        if not description:
            return json.dumps({"error": "description is required"})
        if not instructions:
            return json.dumps({"error": "instructions is required"})

        # Validate name format
        import re
        if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', name):
            return json.dumps({"error": "name must be lowercase alphanumeric with hyphens (e.g., 'gmail-checker')"})

        # Create skill directory
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Build SKILL.md
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n\n"
        skill_md = frontmatter + instructions

        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_md, encoding="utf-8")

        result = {
            "success": True,
            "skill": name,
            "path": str(skill_path),
            "files": ["SKILL.md"],
        }

        # Create script if provided
        if script_name and script_content:
            scripts_dir_path = skill_dir / "scripts"
            scripts_dir_path.mkdir(exist_ok=True)

            script_path = scripts_dir_path / script_name
            script_path.write_text(script_content, encoding="utf-8")

            # Make executable if shell script
            if script_name.endswith(".sh") or script_name.endswith(".bash"):
                script_path.chmod(0o755)

            result["files"].append(f"scripts/{script_name}")

        # Hot-reload skills
        if reload_callback:
            try:
                reload_callback()
                result["reloaded"] = True
            except Exception as e:
                logger.warning("Skill reload failed: %s", e)
                result["reloaded"] = False

        logger.info("Skill created: %s (%d files)", name, len(result["files"]))
        return json.dumps(result)

    registry.register(
        name="create_skill",
        description=(
            "Create a new skill — a reusable set of instructions for repetitive tasks. "
            "Creates SKILL.md and optional scripts. The agent will automatically use "
            "this skill for matching tasks in the future."
        ),
        parameters={
            "type": "object",
            "required": ["name", "description", "instructions"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill nomi (lowercase, defis bilan, masalan: 'gmail-checker', 'daily-report')",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Skill tavsifi — MUHIM: bu trigger sifatida ishlaydi. "
                        "Agent shu tavsif bo'yicha skillni tanlaydi. "
                        "Masalan: 'Check Gmail for important emails and summarize them'"
                    ),
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "Batafsil ko'rsatmalar (Markdown). Agent shu yo'riqnomaga amal qiladi. "
                        "Tool chaqiruvlari, qadam-baqadam yo'riqnoma, natija formati kiradi."
                    ),
                },
                "script_name": {
                    "type": "string",
                    "description": "Ixtiyoriy script fayl nomi (masalan: 'check.py', 'fetch.sh')",
                },
                "script_content": {
                    "type": "string",
                    "description": "Script tarkibi (Python yoki Bash kod)",
                },
            },
        },
        handler=create_skill,
        category="core",
    )

    async def list_skills(params: dict) -> str:
        """List all installed skills."""
        if not skills_dir.is_dir():
            return json.dumps({"skills": [], "count": 0})

        skills = []
        for skill_dir_path in sorted(skills_dir.iterdir()):
            if not skill_dir_path.is_dir():
                continue
            skill_md = skill_dir_path / "SKILL.md"
            if not skill_md.exists():
                continue

            # Parse frontmatter for name/description
            raw = skill_md.read_text(encoding="utf-8")
            name = skill_dir_path.name
            description = ""

            if raw.startswith("---"):
                end = raw.find("---", 3)
                if end > 0:
                    for line in raw[3:end].split("\n"):
                        if line.strip().startswith("description:"):
                            description = line.split(":", 1)[1].strip().strip('"').strip("'")
                            break

            # Check for scripts
            scripts = []
            scripts_path = skill_dir_path / "scripts"
            if scripts_path.is_dir():
                scripts = [f.name for f in scripts_path.iterdir() if f.is_file()]

            skills.append({
                "name": name,
                "description": description,
                "has_scripts": bool(scripts),
                "scripts": scripts,
            })

        return json.dumps({"skills": skills, "count": len(skills)}, ensure_ascii=False)

    registry.register(
        name="list_skills",
        description="List all installed skills with their names, descriptions, and scripts.",
        parameters={"type": "object", "properties": {}},
        handler=list_skills,
        category="core",
    )

    async def run_skill_script(params: dict) -> str:
        """Run a script from a skill's scripts/ directory."""
        skill_name = params.get("skill", "").strip()
        script_name = params.get("script", "").strip()
        args = params.get("args", [])

        if not skill_name or not script_name:
            return json.dumps({"error": "skill and script are required"})

        script_path = skills_dir / skill_name / "scripts" / script_name
        if not script_path.exists():
            return json.dumps({"error": f"Script not found: {skill_name}/scripts/{script_name}"})

        # Security: resolve and check path is within skills dir
        from qanot.fs_safe import resolve_workspace_path
        resolved_str, error = resolve_workspace_path(str(script_path), str(skills_dir))
        if error:
            return json.dumps({"error": "Path traversal blocked"})
        resolved = Path(resolved_str)

        # Determine interpreter
        if script_name.endswith(".py"):
            cmd = ["python3", str(resolved)] + [str(a) for a in args]
        elif script_name.endswith(".sh") or script_name.endswith(".bash"):
            cmd = ["bash", str(resolved)] + [str(a) for a in args]
        else:
            cmd = [str(resolved)] + [str(a) for a in args]

        try:
            import asyncio
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(skills_dir / skill_name),
            )

            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]: {result.stderr}"

            if len(output) > 10000:
                output = output[:10000] + "\n... [truncated]"

            return json.dumps({
                "exit_code": result.returncode,
                "output": output,
            })

        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Script timed out after 60s"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    registry.register(
        name="run_skill_script",
        description="Run a script from a skill's scripts/ directory. Supports Python and Bash.",
        parameters={
            "type": "object",
            "required": ["skill", "script"],
            "properties": {
                "skill": {"type": "string", "description": "Skill nomi"},
                "script": {"type": "string", "description": "Script fayl nomi (masalan: 'check.py')"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scriptga argument sifatida beriladigan qiymatlar",
                },
            },
        },
        handler=run_skill_script,
        category="core",
    )

    async def delete_skill(params: dict) -> str:
        """Delete a skill and all its files."""
        name = params.get("name", "").strip()
        if not name:
            return json.dumps({"error": "name is required"})

        skill_path = skills_dir / name
        if not skill_path.is_dir():
            return json.dumps({"error": f"Skill not found: {name}"})

        # Security check
        from qanot.fs_safe import resolve_workspace_path
        _, error = resolve_workspace_path(str(skill_path), str(skills_dir))
        if error:
            return json.dumps({"error": "Path traversal blocked"})

        import shutil
        shutil.rmtree(skill_path)

        # Hot-reload
        if reload_callback:
            try:
                reload_callback()
            except Exception:
                pass

        logger.info("Skill deleted: %s", name)
        return json.dumps({"success": True, "deleted": name})

    registry.register(
        name="delete_skill",
        description="Delete a skill and all its files (SKILL.md, scripts/).",
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "O'chiriladigan skill nomi"},
            },
        },
        handler=delete_skill,
        category="core",
    )

    # ── install_skill_from_github ─────────────────────────────────

    async def install_skill_from_github(params: dict) -> str:
        """Download a skill folder from GitHub into workspace/skills/.

        Built for Anthropic's official catalogue (anthropics/skills) and any
        compatible repo that uses `{subpath}/{name}/SKILL.md` layout.

        Uses the git trees API to get the full listing in one call, then
        downloads each file from raw.githubusercontent.com. No auth needed
        for public repos; 60-req/hr rate limit is plenty for hand-driven use.
        """
        import aiohttp

        repo = (params.get("repo") or "anthropics/skills").strip().strip("/")
        name = (params.get("name") or "").strip().strip("/")
        branch = (params.get("branch") or "main").strip()
        subpath = (params.get("subpath") or "skills").strip().strip("/")
        overwrite = bool(params.get("overwrite", False))

        if not name:
            return json.dumps({"error": "name is required (e.g. 'pdf', 'docx', 'xlsx')"})
        if not all(c.isalnum() or c in "-_" for c in name):
            return json.dumps({"error": f"invalid skill name {name!r} — use [a-z0-9-_]"})

        target = skills_dir / name
        if target.exists() and not overwrite:
            return json.dumps({
                "error": f"skill '{name}' already exists. Pass overwrite=true to replace.",
                "existing_path": str(target),
            })

        # Fetch tree once, filter to the skill's directory prefix.
        tree_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
        prefix = f"{subpath}/{name}/" if subpath else f"{name}/"

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            try:
                async with session.get(tree_url) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        return json.dumps({
                            "error": f"GitHub tree fetch failed ({resp.status}): {body}",
                        })
                    tree_data = await resp.json()
            except Exception as e:
                return json.dumps({"error": f"GitHub tree fetch error: {e}"})

            if tree_data.get("truncated"):
                return json.dumps({
                    "error": "GitHub tree was truncated — repo too large for this tool; "
                             "clone it manually and copy the skill dir.",
                })

            files = [
                e for e in tree_data.get("tree", [])
                if e.get("type") == "blob" and e.get("path", "").startswith(prefix)
            ]
            if not files:
                return json.dumps({
                    "error": f"no files found at {prefix!r} in {repo}@{branch}. "
                             f"Check 'name' and 'subpath' (try list_github_skills first).",
                })

            has_skill_md = any(e["path"].endswith("/SKILL.md") for e in files)
            if not has_skill_md:
                return json.dumps({
                    "error": f"{prefix}SKILL.md not found — not a valid skill directory.",
                })

            # Stage download into a tmp dir so a partial failure doesn't leave
            # a half-installed skill on disk.
            import shutil
            import tempfile
            staged = Path(tempfile.mkdtemp(prefix=f"qanot-skill-{name}-"))
            try:
                for entry in files:
                    rel = entry["path"][len(prefix):]  # drop `subpath/name/` prefix
                    if not rel:
                        continue
                    # Basic path-traversal defence on the rel path
                    if ".." in rel.split("/") or rel.startswith("/"):
                        return json.dumps({"error": f"unsafe path in repo: {rel!r}"})
                    raw_url = (
                        f"https://raw.githubusercontent.com/"
                        f"{repo}/{branch}/{entry['path']}"
                    )
                    try:
                        async with session.get(raw_url) as rresp:
                            if rresp.status != 200:
                                return json.dumps({
                                    "error": f"download failed for {rel}: HTTP {rresp.status}",
                                })
                            content = await rresp.read()
                    except Exception as e:
                        return json.dumps({"error": f"download failed for {rel}: {e}"})

                    out = staged / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(content)

                # Atomic-ish swap
                if target.exists():
                    backup = target.with_suffix(".bak")
                    if backup.exists():
                        shutil.rmtree(backup)
                    target.rename(backup)
                skills_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(target))
            finally:
                if staged.exists():
                    shutil.rmtree(staged, ignore_errors=True)

        installed_files = sorted(
            str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()
        )
        if reload_callback:
            try:
                reload_callback()
            except Exception as e:
                logger.warning("Skill reload failed after install: %s", e)

        logger.info("Installed skill '%s' from %s@%s (%d files)",
                    name, repo, branch, len(installed_files))
        return json.dumps({
            "success": True,
            "skill": name,
            "source": f"{repo}@{branch}:{subpath}/{name}",
            "path": str(target),
            "files": installed_files,
            "file_count": len(installed_files),
        }, ensure_ascii=False)

    registry.register(
        name="install_skill_from_github",
        description=(
            "Install a skill folder from a GitHub repo into workspace/skills/. "
            "Defaults to the official Anthropic catalogue (anthropics/skills). "
            "Usage examples: name='pdf' installs anthropics/skills/skills/pdf; "
            "name='xlsx' installs anthropics/skills/skills/xlsx. "
            "Available skills in the default catalogue: algorithmic-art, "
            "brand-guidelines, canvas-design, claude-api, doc-coauthoring, "
            "docx, frontend-design, internal-comms, mcp-builder, pdf, pptx, "
            "skill-creator, slack-gif-creator, theme-factory, "
            "web-artifacts-builder, webapp-testing, xlsx."
        ),
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill folder name to install (e.g. 'pdf', 'xlsx').",
                },
                "repo": {
                    "type": "string",
                    "description": "GitHub repo as 'owner/name'. Default: anthropics/skills.",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch or tag. Default: main.",
                },
                "subpath": {
                    "type": "string",
                    "description": "Directory inside the repo holding skill folders. Default: 'skills'.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Overwrite an existing skill of the same name. Default: false.",
                },
            },
        },
        handler=install_skill_from_github,
        category="core",
    )

    # ── update_skill ────────────────────────────────────────────
    async def update_skill(params: dict) -> str:
        """Patch an existing SKILL.md without rewriting the whole file.

        The Hermes-borrow item #2 from the 2026 roadmap: cheap
        incremental edits via `patch` mode. Encourages iterative skill
        evolution instead of throwing away and recreating, which
        preserves edit history (via git) and minimizes surface area
        for accidental regression.
        """
        name = (params.get("name") or "").strip()
        mode = (params.get("mode") or "").strip()
        content = params.get("content") or ""
        section = (params.get("section") or "").strip()

        if not name:
            return json.dumps({"error": "name is required"})
        if mode not in ("replace_section", "append_to_section", "append", "prepend"):
            return json.dumps({
                "error": (
                    "mode must be one of: replace_section, append_to_section, "
                    "append, prepend"
                ),
            })
        if not isinstance(content, str) or not content.strip():
            return json.dumps({"error": "content (string) is required"})
        if mode in ("replace_section", "append_to_section") and not section:
            return json.dumps({
                "error": f"section header is required for mode={mode!r} (e.g. '## Examples')",
            })

        skill_md = skills_dir / name / "SKILL.md"
        if not skill_md.exists():
            return json.dumps({
                "error": f"skill '{name}' has no SKILL.md (use create_skill first)",
            })

        # Path-traversal defence (skill name validated up to here).
        from qanot.fs_safe import resolve_workspace_path
        _, err = resolve_workspace_path(str(skill_md), str(skills_dir))
        if err:
            return json.dumps({"error": "path traversal blocked"})

        try:
            original = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return json.dumps({"error": f"could not read SKILL.md: {e}"})

        new_text, change_description = _apply_patch(
            original=original,
            mode=mode,
            content=content.strip("\n") + "\n",
            section=section,
        )
        if new_text is None:
            return json.dumps({
                "error": (
                    f"section {section!r} not found in SKILL.md — "
                    "use append mode to add a new section, or list_skills to inspect"
                ),
            })

        # Cap total file size (skills should stay tight — large means
        # this should probably be split into multiple skills).
        if len(new_text.encode("utf-8")) > 64_000:
            return json.dumps({
                "error": (
                    "SKILL.md would exceed 64KB after patch — split into "
                    "multiple skills or trim before patching"
                ),
            })

        skill_md.write_text(new_text, encoding="utf-8")
        if reload_callback:
            try:
                reload_callback()
            except Exception as e:
                logger.warning("Skill reload after patch failed: %s", e)

        logger.info("Skill patched: %s (%s)", name, change_description)
        return json.dumps({
            "success": True,
            "skill": name,
            "mode": mode,
            "section": section or None,
            "bytes_added": len(new_text.encode("utf-8")) - len(original.encode("utf-8")),
            "change": change_description,
        }, ensure_ascii=False)

    registry.register(
        name="update_skill",
        description=(
            "Incrementally patch an existing skill's SKILL.md without rewriting it. "
            "Prefer this over delete + create_skill — it preserves edit history "
            "and minimizes surface area for regression. Modes:\n"
            "  - replace_section: overwrite the body of a markdown section "
            "(keeps the heading, replaces lines until the next heading)\n"
            "  - append_to_section: add lines at the end of an existing section\n"
            "  - append: add a new section/lines at the end of the file\n"
            "  - prepend: add lines after the YAML frontmatter (top of body)\n"
            "Section headers must include the leading hashes (e.g. '## Examples')."
        ),
        parameters={
            "type": "object",
            "required": ["name", "mode", "content"],
            "properties": {
                "name": {"type": "string", "description": "Skill name (matches the directory)"},
                "mode": {
                    "type": "string",
                    "description": "replace_section | append_to_section | append | prepend",
                },
                "content": {"type": "string", "description": "Markdown content to insert"},
                "section": {
                    "type": "string",
                    "description": (
                        "Required for replace_section / append_to_section. "
                        "The heading line including hashes, e.g. '## Examples' or '### Edge cases'."
                    ),
                },
            },
        },
        handler=update_skill,
        category="core",
    )

    logger.info(
        "Skill tools registered: create_skill, list_skills, run_skill_script, "
        "delete_skill, install_skill_from_github, update_skill"
    )
