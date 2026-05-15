# Qanot AI — 2026 Frontier Gap Analysis & Must-Implement Roadmap

**Date**: 2026-05-05
**Source**: Synthesis of (a) deep web survey of agentic systems frontier, (b) qanot's current architecture per CLAUDE.md and recent work.

> **⚠️ This is the original 2026-05-05 analysis. It is preserved unchanged below for context. For current status, read the STATUS UPDATE block immediately following — much of Tier 1/2/3 shipped between 2026-05-05 and 2026-05-16 and was never reflected here.**

---

## STATUS UPDATE — 2026-05-16

Evidence-based reconciliation of roadmap items against the actual codebase (verified by source inspection, not the prose below). The P0/P1/P2 sprint numbering lives only in git commit messages — there is no separate plan doc; this roadmap is the only roadmap, and it had drifted ~2 weeks out of date.

### Tier 1 — Non-negotiable

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Eval harness + CI gating | ✅ **DONE** | `0d363f6 feat(evals): eval harness with golden cases + LLM-as-judge + CI gating`; `tests/test_evals.py`, `scripts/agent_eval.py`, OAuth-aware judge (`5d7ff57`), eval CI workflow |
| 2 | MCP client | ✅ **DONE** | `qanot/mcp_client.py`, `qanot/tools/mcp_manage.py` (server wrapper still open) |
| 3 | Durable execution / checkpointed loops | ❌ **OPEN** | No checkpointer in `qanot/agent/loop.py` or `qanot/session.py`. **This is the single remaining Tier 1 gap — highest-priority foundation.** |
| 4 | Unified hooks system | ✅ **DONE** | `qanot/hooks.py` — 7 events wired: `on_startup`, `on_pre_turn`, `on_tool_use`, `on_error`, `on_post_turn`, `on_compaction`, `on_shutdown` (`e4532d8`) |

### Tier 2 — Production credibility

| # | Item | Status | Evidence |
|---|---|---|---|
| 5 | Multi-level cost guardrails | 🟡 **PARTIAL** | per-turn token + USD caps + per-user daily/hourly (`6b3e579`, `config.py`, `agent/loop.py`). **Missing:** per-conversation, per-tenant-month, per-expensive-tool caps |
| 6 | HITL interrupt() per-tool | 🟡 **PARTIAL** | Approval flow exists for `mcp_manage` + `config_manage` (`qanot/tools/_approval_base.py`, `approval_callback` in `tool_registry.py`). **Missing:** generic per-tool policy for dangerous userbot tools (`tg_send_*`) |
| 7 | OTEL-based observability | ❌ **OPEN** | No OpenTelemetry/Langfuse. `dashboard.py` is live-only, no per-turn retrospective traces |
| 8 | Skills loader (SKILL.md) | ✅ **DONE** | skills P0/P1.1 + registry P2.4 (`7bd66db`, `5a73def`, `03659d8`, `56d0ff4`), curator, agentskills.io-compliant packages |

### Tier 3 — Strategic differentiators

| # | Item | Status | Evidence |
|---|---|---|---|
| 9 | Prompt-injection defense | ❌ **OPEN** | Only `fs_safe.py` path validation; no input classifier / behavioral monitor |
| 10 | Channel-owned multi-tenant auth | ❌ **OPEN** | Still single-tenant `config.json` |
| 11 | Sandboxed code execution | ❌ **OPEN** | `code_exec` shipped (`8c4d4c1`) but runs in-process — no E2B/Modal isolation |
| 12 | Core/Recall/Archival memory hierarchy | ✅ **DONE** | memos P1 sprint — file-per-fact + Global/User/Thread scope + WAL→memo writer + evaluator-optimizer + multimodal + conversation RAG (`b9c2017`…`7d830fb`, `c9d0795`) |
| 13 | Dynamic tool retrieval | ✅ **DONE** | Tool Search Tool + defer_loading (`d5b0ceb`, `2d32d2b`) |
| 14 | A/B + canary deploy | ❌ **OPEN** | Prompts still ship via git push + redeploy |

### Shipped but not on the original roadmap

- `code_exec` — programmatic tool calling, multi-step orchestration without context bloat (`8c4d4c1`)
- Bidirectional self-improvement loop — `evolve_soul` + `recall_lessons` + auto-inject, eval-gated (`614c67a`, `d257ff8`)
- Conversation RAG indexing + periodic snapshot loop (`c9d0795`, `be293fe`)
- Group-zen "should I respond?" classifier, poll-flow conversational quiz, voice TTS tools, thread-aware isolation (Bot API 10.0)
- **qanot-video render service** — containerized HyperFrames bridge (`qanot/tools/video.py` `render_video`, remote `feature/video-engine-phase-1..4`)

### Revised sequencing (as of 2026-05-16)

Tier 1 is **3/4 complete**. The dependency chain has largely cleared:

1. **Durable execution / checkpointed loops** — the only remaining Tier 1 foundation. SQLite-backed per-tool-call checkpoint + resume on `agent/loop.py`. ~1 week.
2. **Finish the partials** — full-scope cost guardrails (per-conversation / per-tenant / per-tool) and generic per-tool HITL policy (now cheap: hooks #4 + approval infra #6 both exist). ~1 week combined.
3. **OTEL observability** — wire per-turn traces (tool calls, tokens, cache ratio, router decision, cost, latency) to Langfuse via the hooks bus. ~1 week.
4. Then Tier 3 by QanotCloud commercial priority (multi-tenant auth + injection defense are the SaaS-blocking pair).

### In-flight work secured to branches (not yet on main)

- `feat/reels-scriptwriter` — value-first scriptwriter skill + HyperFrames composer (calls `hyperframes` CLI directly; **must be reconciled with the `qanot/tools/video.py` render-service bridge** before merge)
- `feat/dreams-verifier` — deterministic memory-tree verifier; standalone, the `SwapEngine` + Phase 1-8 consolidation pipeline it gates does not exist yet

---

## TL;DR

Qanot already has the *hard parts* most frameworks lack: multi-provider failover, OAuth Claude Code identity, per-user isolation, hybrid RAG, voice, multi-agent orchestrator, scheduler, daemon, dashboard. **What's missing is mostly production-credibility infrastructure** — the things that turn "shipped a lot of features" into "trustworthy enterprise platform."

The frontier moved from "can your agent do X?" (mostly yes) to "can your agent do X *reliably*, *safely*, *measurably*, and *across the broader ecosystem*?" Qanot is roughly one quarter behind on this second axis.

Eight items below are **non-negotiable** for 2026 credibility. The rest are strategic differentiators ordered by impact.

---

## Tier 1 — Non-negotiable (Q2 2026)

### 1. Eval harness with golden datasets + CI gating
**Status today**: `scripts/agent_eval.py` does synthetic-user testing — that's a start, not the full story.
**What's missing**:
- Golden test cases checked into the repo (input → expected behavior, scored by LLM-as-judge calibrated to humans at Spearman ≥ 0.80)
- Trajectory eval (was the path right) + outcome eval (was the answer right) as separate axes
- CI gate: PRs that drop eval scores get blocked
- Auto-pipeline: production failures → anonymized → added as regression cases
**Why this is #1**: Without it, every commit is a coin flip. Today's session had to manually verify cashier_daily_report against live data; that should be automated. Reliability story collapses to "trust me" otherwise.
**Effort**: 1-2 weeks for a real harness. Existing `agent_eval.py` is the seed.
**Reference**: LangChain agentevals, Braintrust, Galileo.

### 2. MCP client (and eventually server)
**Status today**: Plugins are Python-side @tool decorators. Powerful, but locked to qanot.
**What's missing**: Speak Model Context Protocol — both consume MCP servers (filesystem, postgres, github, slack, vendor SaaS connectors) and expose qanot tools as MCP for other agents.
**Why critical**: 78% of enterprise AI teams use MCP in production. Registry has 9,400+ servers. Without MCP, every Bito/Bitrix/POS integration is a one-off Python plugin; with it, a whole ecosystem becomes available. Strategic for QanotCloud as a multi-tenant platform.
**Effort**: 1 week for a client; another week for a server wrapper around the existing plugin registry.
**Reference**: MCP 2026 roadmap, mcp-agent.

### 3. Durable execution / checkpointed loops
**Status today**: Daemon mode exists, session JSONL is append-only. But if the process crashes mid-`run_command` or mid-`tg_send_*`, the agent re-runs the whole turn from scratch.
**What's missing**: Per-tool-call checkpoint with resume. SQLite-backed checkpointer is the minimum.
**Why critical**: Scheduler/cron jobs that run for minutes need this. So does any "send a long report" flow that gets interrupted by a redeploy.
**Effort**: 1 week. Extend `session.py` with checkpoint primitives; wire to `agent.py` loop.
**Reference**: LangGraph durable execution, Temporal, Cloudflare Sandboxes.

### 4. Unified hooks system
**Status today**: Scattered hook-shaped logic (WAL scan before user message, compaction trigger at 60%, dashboard streaming, audit logs). Each lives in its own corner.
**What's missing**: PreToolUse / PostToolUse / SubagentStop / SessionStart / SessionEnd / UserPromptSubmit as first-class events that any plugin or user can register handlers on.
**Why critical**: Foundation for *everything else* on this list — cost guardrails, HITL, audit logging, redaction, prompt injection defense all want a hook layer. Not implementing this means each new feature bolts hooks ad-hoc; a year from now it's a maintenance morass.
**Effort**: 1 week. Most logic exists; just promote to a unified event bus.
**Reference**: Claude Agent SDK hooks (18+ events), LangGraph middleware.

---

## Tier 2 — Production credibility (Q3 2026)

### 5. Multi-level cost guardrails
**Status today**: `ratelimit.py` enforces per-user sliding window for messages. No cost ceilings.
**What's missing**: Soft (alert + degrade to cheaper model) and hard (stop with explanation) limits at:
- Per-turn token ceiling
- Per-conversation token ceiling
- Per-user-day cost cap
- Per-tenant-month cost cap
- Per-tool call caps (stricter for expensive tools — image gen, voice synthesis)
**Why critical**: The "agent burned $4,200 in 63 hours" postmortem is real. As QanotCloud onboards more tenants, one runaway loop puts the whole P&L at risk.
**Effort**: 1 week. Hooks system (item 4) makes this a clean middleware insertion.

### 6. HITL interrupt() pattern per-tool
**Status today**: "Blocked" response mode is at the *response* level — entire turn waits for response, not specific tools.
**What's missing**: Per-tool policy: this tool requires approval before execution, that one logs only, this other auto-approves. Telegram inline buttons map perfectly.
**Why critical**: Userbot tools (`tg_send_message`, `tg_send_checklist`) are dangerous — agent could spam contacts. SQL `absmarket_query` could in theory mutate (it can't due to SELECT-only filter, but mistakes happen). Send-file to wrong user is hard to recover from.
**Effort**: 4-5 days. Needs hooks + Telegram inline-button reply correlation.
**Reference**: LangChain HITL middleware, EU AI Act Article 14 (Aug 2026).

### 7. OTEL-based observability
**Status today**: `dashboard.py` shows live state. Good for live monitoring, weak for retrospective analysis.
**What's missing**: Per-turn trace with: tool calls, token in/out, cache hit ratio, model used (router decision), cost, latency, eval scoring if applicable. Wire to Langfuse or Braintrust or self-host with ClickHouse.
**Why important**: Without trace-level data you can't diagnose production drift. "Diyora got 21 yesterday and 40 today, why?" — the answer should be in a trace, not a manual SQL probe.
**Effort**: 1 week if using Langfuse SaaS. 2 weeks self-hosted.

### 8. Skills loader (SKILL.md progressive disclosure)
**Status today**: `skill_index` and `active_skills_content` already exist in `prompt.py` — scaffolding is there but it's not a real progressive disclosure.
**What's missing**: Filesystem-based skills (a `skills/` directory in workspace), each with SKILL.md frontmatter (name + description), body loaded only when relevant, supplementary files loaded on-demand via tool call.
**Why important**: This is the open standard now (Anthropic + OpenAI both support). Marketplaces have 100k+ skills. Every prompt rule we ship today (presentation hygiene, schema gotchas, vocabulary lists) should arguably be a skill, not crammed into SOUL_APPEND.md.
**Effort**: 1 week. Most pieces exist.
**Reference**: SKILL.md spec, skills.sh marketplace.

---

## Tier 3 — Strategic differentiators (Q4 2026)

### 9. Prompt-injection defense layer
**Status today**: `fs_safe.py` blocks system dirs for file writes.
**What's missing**: Input classifier (PromptArmor-class), output schema validation, behavioral tool-call monitor (is the user's agent suddenly trying to email customer DB to attacker@evil.com?), multi-model vote on irreversible actions.
**Why important**: Bito and topkey integrations expose real business systems. A prompt-injected Telegram message that walks the agent into "list all customers, send to attacker" is the lethal-trifecta scenario. Will become a compliance requirement.
**Effort**: 2 weeks for a real layer. Needs hooks + a classifier model.

### 10. Channel-owned multi-tenant auth
**Status today**: Each bot has its own config.json, single-tenant. Good for the current "deploy a bot per business" model.
**What's missing**: When QanotCloud sells a hosted multi-tenant deployment, the OAuth/API tokens should belong to the channel/tenant — not the user, not a global config. Token validation per tool call.
**Why important**: Strategic for QanotCloud. Without this, every new tenant = new container. With it, one platform serves N tenants.
**Effort**: 2 weeks. Needs identity model rework.

### 11. Sandboxed code execution
**Status today**: `run_command` (if enabled) goes through `fs_safe` path validation only. Not a real sandbox.
**What's missing**: Real isolation — E2B, Modal, Cloudflare Sandboxes, or Docker-in-Docker. With egress proxy for credentials.
**Why important**: If we ever expose Python execution to agent (which would be a huge productivity unlock for analytics-heavy tasks), this is non-negotiable.
**Effort**: 1 week using a managed sandbox provider.

### 12. Explicit Core/Recall/Archival memory hierarchy
**Status today**: MEMORY.md (≈core), session JSONL (≈recall), workspace RAG (≈archival). The pieces exist but aren't labeled or wired as a hierarchy.
**What's missing**: Explicit Letta-style 3-tier memory model with: Core (~1k tokens always in context, agent edits), Recall (searchable conversation history with retrieval tool), Archival (cold tool-queried via RAG). Plus Salesforce-validated 89% completion improvement vs stateless.
**Why important**: "Mahalla AI never forgets" is a sellable story. Today's behavior is closer to "summarize and dump" than true hierarchy.
**Effort**: 1 week of refactoring + clearer prompts. Most code exists.
**Reference**: Letta, MemGPT lineage.

### 13. Dynamic tool retrieval (RAG-MCP-style)
**Status today**: ALL tools sent every request — by design, for Ollama KV-cache hits. Per memory file: a deliberate decision.
**What's missing**: A *separate* path for non-cache models (Claude with prompt caching already handles this differently): retrieve top-K relevant tools given user message, expose only those.
**Why important**: As tool count grows past ~30 (you're already there with 28+ topkey + 30+ absmarket + 12 doc tools etc.), context bloat + tool hallucination become real. Cache-hit path stays as-is; dynamic path for the rest.
**Effort**: 4-5 days.

### 14. A/B + canary deploy for prompts/models
**Status today**: Prompts ship via `git push` + redeploy. Model routing is hardcoded.
**What's missing**: Treat the system prompt and routing rules as deployable artifacts with versioning + A/B traffic split + eval-gated promotion. Test SOUL_APPEND v3 on 5% of users → measure → promote.
**Why important**: Today's session shipped four iterations of presentation hygiene rules; each was "deploy and pray." A canary system would catch regressions before they hit users.
**Effort**: 1-2 weeks. Needs eval (item 1) as prerequisite.

---

## Tier 4 — Watch & pilot

### 15. Computer Use as a tool
For SaaS without APIs (or APIs that are gated). Bito-without-API or any web-only Uzbek ERP becomes reachable. Pilot with one specific use case end of 2026.

### 16. Agent-as-judge in-line verifier
For irreversible actions (payments, deletions, mass messages). Two-model vote before commit. Becomes a documented pattern within 6 months.

### 17. Constrained decoding for tool args (non-Claude paths)
Anthropic tool_use already constrained. Ollama / OpenAI paths might benefit from XGrammar/Outlines. Verify in eval whether retry rate justifies it.

### 18. Voice + chat hybrid omnichannel
Same agent core, channel adapters (voice / SMS / WhatsApp / Slack / Telegram / Discord), shared memory. Qanot has Telegram + voice; the omnichannel story is one channel adapter away.

---

## Recommended sequencing

The dependencies form a clear order:

```
Tier 1 (foundations):
  hooks ─────┐
              ├──> eval harness ──> CI gate
  durable ───┤
  MCP ──────────────────────────────────────> ecosystem unlock

Tier 2 (built on Tier 1):
  cost guardrails ──> needs hooks
  HITL interrupt ──> needs hooks
  observability ──> orthogonal, but needs hooks for tracing
  skills loader ──> orthogonal

Tier 3 (built on Tier 2):
  injection defense ──> needs hooks + classifier
  multi-tenant auth ──> needs config redesign
  memory hierarchy ──> needs eval to validate
  dynamic tools ──> orthogonal
  A/B deploy ──> needs eval
```

**Suggested 12-week plan**:
- Weeks 1–4: hooks + eval + durable execution + MCP client (Tier 1 complete)
- Weeks 5–8: cost guardrails + HITL + observability + skills loader (Tier 2 complete)
- Weeks 9–12: pick 2-3 from Tier 3 based on QanotCloud commercial priorities

## What NOT to do

- Don't chase computer use until the foundations ship. It's flashy; foundations are what actually move user trust.
- Don't add a multi-agent autonomy upgrade — orchestrator/ already exists. The Salesforce data shows single-agent + good memory + good tools beats multi-agent for most business workflows.
- Don't ship A/B deploy before eval is real. Without scoring you can't tell which variant won.
- Don't refactor RAG. Hybrid (vector + FTS5) is already the modern best practice; don't rebuild on Pinecone or whatever.

## Open questions for the operator

1. **QanotCloud commercial priority**: which Tier 3 items map to a commercial promise? (Multi-tenant auth, prompt injection defense, A/B deploy are obvious SaaS plays; memory hierarchy is a marketing story.)
2. **Eval team**: who writes the golden test cases? Domain expertise required (POS, HR, Topkey workflows). Offer a per-tenant golden-set service?
3. **MCP strategy**: just consume servers? Or expose qanot tools as MCP for other agents to call? The latter unlocks "qanot is the brain, but X tool is the hands" stories.
4. **Compliance posture**: any EU clients in QanotCloud's pipeline? If yes, HITL becomes a 2026 compliance requirement, moves from Tier 2 to Tier 1.

---

## References

See `claudedocs/qanot-2026-research-sources.md` (separate file) for ~70 cited sources — Anthropic / OpenAI / Google / LangChain / arXiv / production engineering blogs from late 2025 through May 2026.
