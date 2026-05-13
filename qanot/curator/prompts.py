"""LLM prompts for the curator's review pass.

The review runs as an isolated cron agent (same shape as
HEARTBEAT_PROMPT / CONSOLIDATION_PROMPT) so it never blocks live turns and
its decisions are auditable in the session log. Outputs go to
`proactive-outbox.md` and are surfaced to the user on next interaction.
"""

CURATOR_REVIEW_PROMPT = """SKILL CURATOR REVIEW — you are running as an autonomous agent.

Your job is to keep the skill library clean. Skills accumulate over time;
without curation, the prompt budget rots and duplicate skills crowd out
the good ones.

CONTEXT:
- Skills live at `/data/workspace/skills/<name>/SKILL.md`.
- Usage data lives at `/data/workspace/skills/.usage.json` — created_at,
  last_used_at, use_count, patch_count, pinned, agent_created, status.
- Archive lives at `/data/workspace/skills/.archive/<name>/` (recoverable).

STEPS:

1. Read `/data/workspace/skills/.usage.json`:
   Use: read_file, path="/data/workspace/skills/.usage.json"

2. List the skill directories:
   Use: run_command, command="ls -1 /data/workspace/skills/"

3. For each non-pinned, agent-created skill, judge ONE of:
   - KEEP    — actively used or pinned
   - STALE   — idle > 30 days; warn but keep
   - ARCHIVE — idle > 90 days OR clear duplicate of another skill
   - PATCH   — could be merged/clarified; propose a concrete edit

4. For potential duplicates (two skills with overlapping descriptions or
   bodies), pick ONE to keep and propose archiving the other. Prefer:
   - higher use_count
   - more recent last_used_at
   - tighter, more specific description

5. Apply your decisions:
   - ARCHIVE: use the `skill_archive` tool with name="<skill>". Never use
     `run_command rm` — archived skills must remain recoverable.
   - PATCH: use the `skill_patch` tool with name, old_substring, new_substring.

6. Append a summary to `/data/workspace/memory/proactive-outbox.md`:

     ## Skill curator review — YYYY-MM-DD
     - Archived: <skill> (idle 95d, superseded by <other>)
     - Patched: <skill> — clarified trigger phrasing
     - Stale (warned): <skill> (idle 45d)
     - Kept: N skills active

7. If nothing needed action, respond with exactly: CURATOR_OK

RULES:
- DO NOT delete files. ARCHIVE only.
- DO NOT touch user-created skills (agent_created=false in .usage.json).
- DO NOT touch pinned skills (pinned=true) under any condition.
- DO NOT touch the example/template skills shipped with the framework.
- Prefer fewer, sharper skills over many overlapping ones.
- If you are uncertain, do nothing and report STALE — humans decide
  whether to keep marginal skills.
- Your tool surface: read_file, write_file, run_command, skill_archive,
  skill_patch, memory tool. No network, no destructive shell commands.
"""
