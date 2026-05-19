"""Memory consolidation — Auto Dream-style weekly cron job.

Prevents unbounded growth of daily notes + /memories/ once we stop
actively condensing into MEMORY.md. Run as an isolated cron agent.

What the agent does (per the prompt below):
  1. Reads the recent session-transcript digest as the PRIMARY signal
     (raw user phrasing, corrections, recurring asks) — built
     deterministically by `qanot.dreams.digest` before this job runs.
  2. Reads recent daily notes as the SECONDARY signal (corroborate).
  3. Extracts NEW durable facts (preferences, decisions, identity,
     recurring workflows) and updates/creates /memories/topic.md.
  4. Archives daily notes older than 30 days to memory/archive/.
  5. Reports a one-line summary per action to proactive-outbox.md.

The transcript digest is the highest-leverage borrow from Anthropic's
managed Dreams (which mines full session transcripts, not note
summaries). We keep it self-hosted + deterministic — see
`qanot/dreams/digest.py`.

Schedule: weekly Sunday 04:00 local (default). Can be changed via
`cron_update`. Mode: isolated (no user context, no streaming).
"""

from __future__ import annotations

from pathlib import Path


CONSOLIDATION_PROMPT = """MEMORY CONSOLIDATION JOB — you are running as an autonomous agent.

Your job is to keep long-term memory clean, organised, and bounded.

STEPS:

1. View /memories/ to see the current topic layout:
   Use: memory tool, command=view, path=/memories

2. Read the PRIMARY signal — the recent session transcript digest:
   Use: read_file, path="/data/workspace/memory/recent-sessions-digest.md"
   This is a bounded digest of what the user ACTUALLY said and did across
   the last week of conversations — raw phrasing, corrections ("no, not
   that", "always do X"), and asks that recur across sessions. It is the
   richest source of durable facts. If the file does not exist, skip this
   step (no recent sessions) and rely on daily notes alone.
   NOTE: this file is regenerated every run — it is NOT a daily note.
   Do NOT archive, delete, or rewrite it.

3. Read the SECONDARY signal — recent daily notes (corroborate / fill
   gaps the digest's per-message truncation may have cut):
   Use: run_command, command="ls -1t /data/workspace/memory/*.md | grep -v recent-sessions-digest | head -8"
   Read each with read_file.

   Across BOTH sources look for:
   - NEW user preferences not yet in /memories/ (weight corrections and
     repeated asks heavily — a thing said twice is a durable fact)
   - NEW decisions worth keeping
   - NEW identity/profile facts
   - Recurring patterns that suggest a habit or workflow
   SKIP one-off events, chat pleasantries, temporary state.

4. For each NEW durable fact:
   a. Check if a relevant /memories/<topic>.md or /memories/learnings/<topic>.md
      already exists (use memory tool, command=view on plausible paths).
   b. If yes → append the fact using memory tool command=str_replace or insert.
   c. If no → create a new file with memory tool command=create. Filenames
      are short slugs: preferences.md, identity.md, workflows.md,
      learnings/video.md, learnings/documents.md, etc.

5. Archive daily notes older than 30 days:
   Use: run_command, command="mkdir -p /data/workspace/memory/archive &&
     find /data/workspace/memory -maxdepth 1 -name '*.md' -type f -mtime +30
     -exec mv {} /data/workspace/memory/archive/ \\\\;"

6. Append a concise summary to /data/workspace/memory/proactive-outbox.md
   using write_file (append-style — read first, add a new section at the
   bottom). Format:

     ## Memory consolidation — YYYY-MM-DD
     - Merged N facts into /memories/<file>
     - Created /memories/<new>
     - Archived X old daily notes

7. If nothing needed attention, respond with exactly: CONSOLIDATION_OK

RULES:
- DO NOT invent facts. Only record things the user actually said or did.
- DO NOT mention internal file names to the user.
- DO NOT send messages to the user — this is a background job.
- Keep files concise. Prefer str_replace (edit) over create (new file).
- Organise, don't clutter. Five tight files > twenty scattered ones.
- Your tool surface is memory, read_file, write_file, run_command only.
"""


def write_consolidation_prompt(path: str | Path) -> None:
    """Write the prompt to a workspace file so cron jobs can load it."""
    Path(path).write_text(CONSOLIDATION_PROMPT, encoding="utf-8")
