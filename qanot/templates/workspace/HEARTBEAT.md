# HEARTBEAT.md

## Self-Healing Checks

Run these checks every heartbeat cycle. Fix issues silently. Report only meaningful findings.

### 1. Pending Tasks
- Read today's and yesterday's daily notes in `memory/`
- Look for uncompleted tasks, promises, or follow-ups
- If found: complete them or write a reminder to `proactive-outbox.md`

### 2. Workspace Integrity
- Verify these files exist and are not empty: SOUL.md, TOOLS.md, IDENTITY.md
- Check `memory/` directory exists
- If SESSION-STATE.md is stale (>7 days old), archive relevant parts to MEMORY.md

### 3. Memory Consolidation
- If daily notes from >3 days ago contain important learnings, distill into MEMORY.md
- Remove redundant or duplicate entries from MEMORY.md
- Keep MEMORY.md under 5000 chars

### 4. TOOLS.md Validation
- Read TOOLS.md — check for incorrect examples, stale references
- If you find errors (wrong column names, outdated API paths, etc.), fix them

### 5. Pattern Detection
- Scan recent daily notes for repeated user requests
- If the same request appears 3+ times, note it in `notes/areas/recurring-patterns.md`

### 6. Proactive Report
- If you found and fixed issues, write a clear summary to `proactive-outbox.md`
- Format: what you found, what you fixed, any recommendations
- If nothing needs attention, respond with HEARTBEAT_OK (do NOT write to outbox)
