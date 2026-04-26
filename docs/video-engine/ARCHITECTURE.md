# Qanot Video Engine — Architecture

**Status**: DRAFT — pending sign-off
**Author**: Initial draft generated 2026-04-26
**Target ship**: ~5 weeks from sign-off
**Replaces**: `plugins/reels/plugin.py` (deprecated after rollout)

---

## 0. Executive Summary

This document specifies the production replacement for the current `reels` plugin
(`plugins/reels/plugin.py`, 499-line prototype). The new system is built around
HeyGen's open-source **HyperFrames** rendering engine (Apache 2.0, Node.js +
Puppeteer + FFmpeg) wrapped behind a small async render service, with Qanot's
Python framework consuming it through a typed tool.

The defining shift: agents stop pushing "topic strings" at a stock-footage
pipeline and start authoring HTML+GSAP compositions directly. The render service
is a thin, well-tested envelope around `npx hyperframes render` — not a
homegrown video engine.

Right-sized for current scale: single-server deployment on the existing topkey
host (Hetzner CX22 / 4 GB RAM), one render worker, SQLite-backed job queue,
local-disk output with 24h retention. No S3, no Redis, no separate render
server. The service is structured so we can scale each of those independently
later without rewrites.

---

## 1. Goals & Non-Goals

### Goals

1. **Agent visual authority** — agents author HTML+CSS+GSAP compositions; the
   service renders them deterministically. No more `topic → black-box pipeline`.
2. **Brand consistency** — per-bot `DESIGN.md` (palette, typography, motion
   defaults) injected into the agent's system prompt; agents respect it across
   every render.
3. **Production-grade reliability** — `subprocess.returncode` actually checked,
   timeouts enforced, OOM handled, errors surfaced with actionable messages.
4. **Observability from day one** — render duration, success rate, queue depth,
   per-user/per-bot cost are visible on the dashboard.
5. **Safe by default** — per-user rate limits, per-bot cost ceilings,
   asset-URL allowlist (no SSRF via composition), Chromium sandbox.
6. **Tested** — current reels plugin has 0 tests. Target: ≥80% coverage on the
   render service and the Python bridge.
7. **Migrateable** — existing `create_reel` users continue working during a
   30-day deprecation window; per-bot opt-in toggle controls which engine is
   active.

### Non-Goals

1. **Distributed rendering** — single host is enough for projected scale.
   Re-evaluate at >50 renders/day sustained.
2. **Object storage (S3)** — local disk + Telegram `file_id` is sufficient.
   Re-evaluate when retention requirements exceed 24h.
3. **Multi-tenant API key system** — one Qanot deployment, one render service.
   Service-to-service auth uses a single shared secret, not a key tree.
4. **Distributed tracing** — structured JSON logs are the observability floor.
   Add OpenTelemetry only when log correlation across hosts becomes needed.
5. **Webhook callbacks** — clients poll `GET /jobs/:id`. Webhook adds nothing
   for in-process Python clients.
6. **Render result caching** — every composition is unique; cache hit rate
   would be near zero.
7. **Custom video editor UI** — HyperFrames Studio exists if anyone wants it
   later. Telegram-driven generation is the primary UX.
8. **In-house TTS** — keep ElevenLabs/Muxlisa/KotibAI for narration (already
   wired in `qanot/voice.py`). HyperFrames' Kokoro TTS evaluated separately.

---

## 2. System Overview

### 2.1 Component diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│                          topkey host (Hetzner CX22)                    │
│                                                                        │
│  ┌────────────────────────┐         ┌──────────────────────────────┐  │
│  │  qanot-bot-* (existing)│         │  qanot-video (NEW)           │  │
│  │  Python framework      │  HTTP   │  Node.js render service       │  │
│  │                        │ ──────▶ │  - Express + zod              │  │
│  │  qanot/tools/video.py  │ <────── │  - SQLite job queue           │  │
│  │  render_video tool     │  (poll) │  - 1 worker × Puppeteer       │  │
│  │                        │         │  - HyperFrames CLI            │  │
│  │  Telegram adapter      │         │  - FFmpeg                     │  │
│  └────────────────────────┘         └──────────────────────────────┘  │
│                                                ▲                       │
│                                                │ 127.0.0.1:8770       │
│                                                │ (loopback only)      │
│                                                                        │
│  ┌────────────────────────┐         ┌──────────────────────────────┐  │
│  │  Shared volumes        │         │  Output volume                │  │
│  │  /data/video_renders/  │ ◀──────▶│  /data/video_renders/         │  │
│  │  (24h retention)       │         │  job-uuid.mp4                 │  │
│  └────────────────────────┘         └──────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                      ┌────────────────────┐
                      │  Telegram          │
                      │  send_video        │
                      │  → file_id (1y+)   │
                      └────────────────────┘
```

### 2.2 Request lifecycle

```
User (Telegram)                Qanot Agent              Render Service
    │                              │                          │
    │ "30s video about X, 9:16"   │                          │
    │ ────────────────────────▶    │                          │
    │                              │                          │
    │                              │ rate-limit check        │
    │                              │ cost-cap check          │
    │                              │                          │
    │                              │ author HTML composition  │
    │                              │ (LLM, ~5-15s)           │
    │                              │                          │
    │                              │ POST /render            │
    │                              │ ────────────────────▶   │
    │                              │                          │ enqueue
    │                              │ ◀────── { job_id }      │ SQLite
    │ ◀── "Tayyorlanmoqda…"       │                          │
    │   (draft message)            │                          │
    │                              │                          │ worker picks
    │                              │                          │ npx hyperframes
    │                              │                          │ lint + render
    │                              │ poll GET /jobs/:id      │
    │                              │ ────────────────────▶   │ (every 2s)
    │                              │ ◀── { progress: 35% }   │
    │ ◀── "35%…"                  │                          │
    │   (edit draft)               │                          │
    │                              │                          │ done
    │                              │ ◀── { status: succeeded }│
    │                              │      output: /data/...   │
    │                              │                          │
    │ ◀── send_video(MP4)         │                          │
    │     [video preview]          │                          │
    │                              │                          │
```

### 2.3 Deployment topology

Single host (`topkey`, currently 4 GB RAM):

| Container | Memory limit | Notes |
|---|---:|---|
| `qanot-bot-*` (existing 1+ active) | 256 MB each | Telegram bot processes |
| `qanot-video` (NEW) | 1.5 GB | Single render worker |
| Host services (sshd, etc.) | ~200 MB | OS overhead |
| Headroom | ~1 GB | Burst, page cache |

If a second concurrent render is needed in the future, options in order of cost:
(a) bump container concurrency to 2 (needs RAM upgrade), (b) split to a second
host. The architecture supports both without code changes — only ops config.

---

## 3. Render Service (`qanot-video`)

Lives in this repo at `services/video/` (NOT a separate repo — keeps deploy
simple, single PR for cross-cutting changes; if it ever grows enough to deserve
its own lifecycle, extracting is mechanical).

### 3.1 Responsibilities

The render service owns:

- HTTP API for job submission, status, output retrieval
- Job queue (SQLite-backed, durable across restarts)
- Composition validation (`hyperframes lint` before render)
- Render worker (`hyperframes render` subprocess, supervised)
- Output file lifecycle (write, serve, cleanup at 24h)
- Resource monitoring (RAM, disk) and graceful job rejection when constrained
- Per-bot rate limit enforcement (defense in depth — the Python tool also
  enforces, but the service is the authoritative point)
- Health and metrics endpoints

It does NOT own:

- LLM composition authoring (that is the agent's job, in Python)
- User authentication (the Python tool authenticates with a service secret;
  there are no end-user accounts at this layer)
- Telegram delivery (Python framework's existing `send_file` does that)

### 3.2 Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Runtime | Node.js 22 LTS | Match HyperFrames requirements |
| Package manager | bun | HyperFrames uses bun; minimal toolchain divergence |
| HTTP framework | Hono | Lightweight, TypeScript-native, used by HyperFrames itself |
| Validation | zod | Type-safe, agents will misshape requests, hard limits matter |
| Database | better-sqlite3 (synchronous) | Tiny dep, durable, no separate process |
| Logging | pino | JSON-structured, ms-overhead, ecosystem-standard |
| Test | vitest | Fast, ESM-native, integration-test-friendly |
| Lint | oxlint | What HyperFrames uses; no config drift |

No Express, no Fastify (Hono is enough), no TypeORM/Prisma (raw SQL on
better-sqlite3 is 50 lines), no Pino transports (`stdout` → Docker → file).

### 3.3 Directory structure

```
services/video/
├── Dockerfile                    # node:22 + chromium + ffmpeg + hyperframes
├── package.json                  # pinned versions
├── bun.lock
├── tsconfig.json
├── README.md
├── src/
│   ├── server.ts                 # Hono app, routes, error handling
│   ├── routes/
│   │   ├── render.ts             # POST /render
│   │   ├── jobs.ts               # GET /jobs/:id, GET /jobs/:id/output
│   │   ├── health.ts             # GET /health
│   │   └── metrics.ts            # GET /metrics (Prometheus format)
│   ├── queue/
│   │   ├── db.ts                 # better-sqlite3 setup, schema migration
│   │   ├── jobs.ts               # job CRUD, state transitions
│   │   └── worker.ts             # picks queued, runs, transitions state
│   ├── render/
│   │   ├── lint.ts               # `npx hyperframes lint` wrapper
│   │   ├── render.ts             # `npx hyperframes render` wrapper
│   │   └── timeout.ts            # subprocess + timeout + kill
│   ├── auth/
│   │   └── service-key.ts        # Bearer middleware
│   ├── observability/
│   │   ├── logger.ts             # pino setup
│   │   └── metrics.ts            # counters, histograms
│   ├── config.ts                 # env var validation (zod)
│   └── types.ts                  # shared types
├── test/
│   ├── unit/
│   │   ├── lint.test.ts
│   │   ├── render.test.ts
│   │   ├── jobs.test.ts
│   │   └── auth.test.ts
│   └── integration/
│       ├── full-render.test.ts   # end-to-end with real chromium
│       └── failure-modes.test.ts # OOM simulation, timeout, malformed comp
└── compositions/
    └── _smoke.html               # tiny composition used in CI smoke test
```

### 3.4 Public API

All endpoints require `Authorization: Bearer <SERVICE_SECRET>` except `/health`.
The service binds to `127.0.0.1` only — exposed to other containers on the same
host via Docker network, never to the public internet.

#### `POST /render`

Submit a render job.

**Request**:

```json
{
  "request_id": "uuid-from-caller",
  "bot_id": "topkeydevbot",
  "user_id": "12345",
  "composition_html": "<!doctype html>...",
  "format": "9:16",
  "duration_seconds": 30,
  "fps": 30,
  "quality": "standard",
  "deadline_seconds": 120
}
```

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `request_id` | UUID | yes | Idempotency key. Re-submitting same id returns existing job. |
| `bot_id` | string | yes | Used for per-bot rate limit + observability. |
| `user_id` | string | yes | Used for per-user rate limit. |
| `composition_html` | string | yes | Full HTML document. Max 256 KB. |
| `format` | enum | yes | `9:16`, `16:9`, `1:1`. Maps to canvas dimensions. |
| `duration_seconds` | int | yes | 1–60. Hard cap at 60 for cost control. |
| `fps` | int | no | 24, 30, 60. Default 30. |
| `quality` | enum | no | `draft`, `standard`, `high`. Default `standard`. |
| `deadline_seconds` | int | no | Job killed if not finished by deadline. Default 120. |

**Response 202 Accepted**:

```json
{
  "job_id": "01HXYZ...ulid",
  "status": "queued",
  "queue_position": 2,
  "estimated_start_seconds": 18
}
```

**Errors**:

- `400` — validation failure (zod error in body)
- `401` — missing or invalid service key
- `409` — duplicate `request_id` (returns existing `job_id`)
- `413` — `composition_html` exceeds 256 KB
- `429` — per-bot quota exceeded
- `503` — service in degraded mode (RAM > 90%, disk > 95%)

#### `GET /jobs/:id`

Poll job status. Caller polls every 2-5 seconds.

**Response 200**:

```json
{
  "job_id": "01HXYZ...",
  "status": "running",
  "progress_percent": 45,
  "stage": "rendering_frames",
  "queued_at": "2026-04-26T08:00:00Z",
  "started_at": "2026-04-26T08:00:18Z",
  "error": null
}
```

`stage` values: `queued`, `linting`, `rendering_frames`, `encoding_video`,
`succeeded`, `failed`, `expired`, `cancelled`.

When `status` is `succeeded`, response also contains:

```json
{
  ...
  "output_path": "/data/video_renders/01HXYZ.mp4",
  "output_size_bytes": 8421234,
  "render_duration_seconds": 38,
  "expires_at": "2026-04-27T08:00:38Z"
}
```

When `status` is `failed`:

```json
{
  ...
  "error": {
    "code": "lint_failed",
    "message": "Composition lint failed: missing data-duration on element #title",
    "details": "..."
  }
}
```

Error codes (closed set): `lint_failed`, `render_timeout`, `chrome_crash`,
`asset_fetch_failed`, `disk_full`, `oom_killed`, `internal`.

#### `GET /jobs/:id/output`

Stream the rendered MP4. 200 with `Content-Type: video/mp4` and
`Content-Disposition: attachment`. Caller saves bytes to disk locally; the
file is also accessible directly at `output_path` since both containers share
the volume — this endpoint exists for the future where they may not.

`404` if job not succeeded yet, expired, or unknown.

#### `DELETE /jobs/:id`

Cancel a queued job. Running jobs are killed (SIGTERM, then SIGKILL after 5s).
Idempotent.

#### `GET /health`

Public (no auth). Returns 200 with `{"ok": true}` if the worker loop is alive
and SQLite is responsive. Used by Docker `HEALTHCHECK`.

#### `GET /metrics`

Auth-required. Prometheus-format text. Counters and histograms enumerated in
§8.1.

### 3.5 Composition validation

Before rendering, every job is linted via `npx hyperframes lint --strict`. Lint
failure means the job fails fast (no render attempted, no Chrome spawned, no
quota burned). The lint output (parsed JSON) is returned in `error.details`
so the agent can read it and retry with a corrected composition.

The Python bridge implements automatic retry-with-feedback: on `lint_failed`,
it re-prompts the agent with the lint error and the previous composition,
asking for a fix. Maximum 1 retry. After that, surface to the user.

### 3.6 Worker model

The service runs a single worker loop in the same Node.js process as the HTTP
server. (Justification: at 1 worker concurrency, separating into a worker
process adds operational complexity for zero gain. When concurrency >1, split.)

Worker loop, simplified:

```
while running:
    job = db.lease_next_queued_job(lease_seconds=180)
    if not job:
        sleep 1s; continue
    try:
        with monitor(job):
            lint_result = lint(job.composition_html)
            if not lint_result.ok:
                db.fail(job.id, code='lint_failed', details=lint_result.errors)
                continue
            render_result = render(job, deadline=job.deadline_seconds)
            db.succeed(job.id, output=render_result.path, ...)
    except DeadlineExceeded:
        db.fail(job.id, code='render_timeout')
    except ChromeRendererCrash:
        db.fail(job.id, code='chrome_crash')
    except OOMKilled:
        db.fail(job.id, code='oom_killed')
        # also: refuse new jobs for 30s while RAM recovers
    except Exception as e:
        db.fail(job.id, code='internal', details=str(e))
        log.error(...)
```

Lease semantics: a job that is leased for >180s without a heartbeat is
considered orphaned and re-queued. Used by crash recovery — if the worker
process dies mid-render, the next worker pick gets the same job. (Idempotent
because the worker writes output to a temp file and `mv`'s on success.)

---

## 4. Python Bridge

Lives in `qanot/tools/video.py`. The render service is invoked exactly here,
nowhere else.

### 4.1 Tool: `render_video`

```python
async def render_video(params: dict) -> str:
    """
    Render a video composition via the qanot-video service.

    Input:
      brief: str           # what the video should be (free-form description)
      duration: int = 30   # 1-60 seconds
      format: str = "9:16" # "9:16" | "16:9" | "1:1"
      style: str = ""      # optional brand override (matches a DESIGN.md preset)

    Returns JSON:
      { "success": true, "video_path": "...", "render_seconds": 38 }
      OR
      { "error": "rate_limited" | "lint_failed" | "render_timeout" | ..., "message": "..." }
    """
```

The tool is registered in `qanot/main.py` only when `config.video_engine ==
"hyperframes"`. Other values: `"legacy_reels"` (registers the old plugin) or
`"off"` (no video tool registered).

### 4.2 Composition authoring

The tool internally:

1. Reads `templates/workspace/skills/hyperframes/SKILL.md` (cached).
2. Reads the per-bot `DESIGN.md` if it exists in the workspace.
3. Uses a **sub-agent** call (Sonnet, not Opus — composition writing is
   well-bounded so cheaper) with a focused system prompt:
   *"You are a video composition author. Output ONLY valid HTML conforming to
   the HyperFrames composition spec below. No commentary, no markdown fences."*
   Then injects the skill + the brief + duration/format constraints.
4. Validates the returned HTML is non-empty and starts with `<!doctype`.
5. Submits to the render service.

This separation matters: the main agent (which the user is talking to) does
not have to know HTML+GSAP. It just calls `render_video(brief="...")`. The
composition sub-agent specializes.

### 4.3 Telegram UX

The current `create_reel` returns a bare `{"success": true, "video_path": "..."}`
and the agent has to decide to `send_file`. That's brittle — the agent
sometimes forgets, sometimes describes the video instead of sending it.

New design: `render_video` returns a result the agent should pass to a paired
`send_video` tool, OR (simpler) `render_video` itself sends the file at the end
via the existing Telegram bot reference. To be decided in detail during
implementation; either works. Lean toward "tool sends, returns confirmation"
for atomic UX.

While rendering, the Python tool edits a Telegram draft message every 3-5
seconds with progress: *"Tayyorlanmoqda… 35% (rendering frames)"*. Bot API 9.5
`editMessageText` is already wired in `qanot/telegram/streaming.py`.

### 4.4 Configuration (Config dataclass additions)

```python
# qanot/config.py
video_engine: str = "off"             # "off" | "legacy_reels" | "hyperframes"
video_render_url: str = "http://qanot-video:8770"
video_service_secret: SecretRef = ... # Bearer token, env var
video_per_user_daily_limit: int = 5
video_per_bot_daily_limit: int = 50
video_per_user_daily_cost_usd: float = 0.50  # composition LLM only; render is free compute
video_composition_model: str = "claude-sonnet-4-6"  # Sonnet for cost
video_default_duration_seconds: int = 30
video_max_duration_seconds: int = 60
```

`SecretRef` is an existing pattern in `qanot/secrets.py` — env var or file path.

---

## 5. Job Lifecycle

### 5.1 State machine

```
   ┌───────┐   submit   ┌────────┐  worker picks  ┌─────────┐  lint ok  ┌──────────┐
   │ <new> │──────────▶ │ queued │──────────────▶ │ linting │─────────▶ │ rendering│
   └───────┘            └────────┘                └─────────┘           └────┬─────┘
                            │                          │                     │
                       cancel│                  lint fail│              render ok
                            ▼                          ▼                     ▼
                     ┌────────────┐             ┌────────┐            ┌──────────┐
                     │ cancelled  │             │ failed │            │succeeded │
                     └────────────┘             └────────┘            └─────┬────┘
                                                                            │
                                                                       24h elapsed
                                                                            ▼
                                                                       ┌─────────┐
                                                                       │ expired │
                                                                       └─────────┘
```

State transitions are atomic in SQLite (UPDATE … WHERE status = 'expected').
No race conditions between worker and the cleanup cron.

### 5.2 SQLite schema

```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,                     -- ULID
    request_id TEXT NOT NULL UNIQUE,         -- idempotency
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    composition_html TEXT NOT NULL,
    format TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    fps INTEGER NOT NULL DEFAULT 30,
    quality TEXT NOT NULL DEFAULT 'standard',
    deadline_seconds INTEGER NOT NULL DEFAULT 120,

    status TEXT NOT NULL DEFAULT 'queued',   -- queued|linting|rendering|succeeded|failed|cancelled|expired
    stage TEXT,                              -- finer-grained progress
    progress_percent INTEGER DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    error_details TEXT,                      -- JSON

    output_path TEXT,
    output_size_bytes INTEGER,
    render_duration_ms INTEGER,

    leased_until INTEGER,                    -- unix epoch; for crash recovery
    queued_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    expires_at INTEGER                       -- queued_at + 24h
);

CREATE INDEX idx_jobs_status_queued ON jobs(status, queued_at);
CREATE INDEX idx_jobs_bot_user_queued ON jobs(bot_id, user_id, queued_at);
CREATE INDEX idx_jobs_expires ON jobs(expires_at);

CREATE TABLE quota_ledger (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bucket_day TEXT NOT NULL,                -- YYYY-MM-DD UTC
    job_count INTEGER NOT NULL DEFAULT 0,
    cost_usd_micros INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, user_id, bucket_day)
);
```

### 5.3 Idempotency

`request_id` is supplied by the Python caller (UUID4). If a request comes in
with a `request_id` that already has a job, return the existing job's
`{job_id, status}` instead of creating a new row. This makes the Python tool
safe to retry on network glitch — no double-render, no double-charge.

---

## 6. Composition Format & Skill

### 6.1 Authoring layer

Compositions are **plain HTML5 + CSS + GSAP** (no JSX, no build step) per
HyperFrames spec. Example minimum-viable composition:

```html
<!doctype html>
<html><head><meta charset="UTF-8">
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style>
    html,body{margin:0;width:1080px;height:1920px;background:#0a0a0a;color:#fff;
              font-family:system-ui;overflow:hidden}
    .stage{display:flex;flex-direction:column;justify-content:center;
           align-items:center;height:100%;padding:0 80px;text-align:center}
    h1{font-size:96px;font-weight:800;margin:0 0 32px}
    p{font-size:48px;color:#a0a0a0;margin:0;line-height:1.4}
  </style>
</head>
<body>
  <div id="root" data-composition-id="main" data-start="0" data-duration="6"
       data-width="1080" data-height="1920">
    <div class="stage">
      <h1 id="title">Mahsulotim</h1>
      <p id="sub">Endi onlayn buyurtma qabul qilinadi</p>
    </div>
  </div>
  <script>
    const tl = gsap.timeline({paused:true});
    tl.from("#title",{y:80,opacity:0,duration:0.7,ease:"power3.out"},0)
      .from("#sub",{y:40,opacity:0,duration:0.6,ease:"power3.out"},0.3)
      .to(["#title","#sub"],{opacity:0,y:-30,duration:0.5,ease:"power2.in"},5.4);
    window.__timelines={main:tl};
  </script>
</body>
</html>
```

### 6.2 Per-bot DESIGN.md

A new optional file in the workspace: `{workspace_dir}/DESIGN.md`. If present,
the composition sub-agent reads it and respects:

- Color palette (CSS custom properties or specific hexes)
- Typography (font family, weights, scales)
- Motion defaults (easing curves, durations)
- Imagery rules (logo placement, background patterns)

Without `DESIGN.md`, the agent uses HyperFrames' defaults. With it, every
video matches brand. Bot owners can author this manually, or the agent can
help them: a separate one-off tool `bootstrap_design(answers)` (out of scope
for v1).

### 6.3 Asset URL allowlist

Compositions can reference external assets (fonts, images, videos, music) via
URL. To prevent SSRF inside the headless browser, the lint pass extracts all
`src=`, `href=`, `url(...)` references and rejects:

- Any URL whose hostname resolves to a private IP range (reuse
  `qanot/tools/web.py:_is_ip_blocked`)
- Non-HTTPS for non-localhost
- Schemes other than `http`, `https`, `data:`, `file:` (file: only resolves
  to a whitelisted asset directory inside the container)

This is the same defense as `web_fetch` SSRF (we already shipped that fix).
The render service shares the validation module.

### 6.4 Audio

Audio elements use HyperFrames' `<audio data-start data-duration data-volume>`
syntax. Audio sources can be:

- TTS narration generated externally (existing `qanot/voice.py` providers —
  ElevenLabs, Muxlisa, KotibAI). The Python tool generates the voiceover
  before composition, hosts it on a local URL inside the container's asset
  dir, and the composition references it.
- HyperFrames built-in Kokoro TTS via `npx hyperframes tts` — evaluated for
  free local TTS. Out of scope for v1.
- User-supplied audio (future).

---

## 7. Security

### 7.1 Service-to-service auth

The Python tool authenticates with the render service using a Bearer token.
Token is generated at deploy (`openssl rand -hex 32`), stored in environment
on both containers, never logged.

```typescript
// Render service middleware
app.use('*', async (c, next) => {
  if (c.req.path === '/health') return next();
  const auth = c.req.header('authorization');
  if (auth !== `Bearer ${env.SERVICE_SECRET}`) {
    return c.json({error: 'unauthorized'}, 401);
  }
  return next();
});
```

The service binds to `127.0.0.1:8770`, exposed only via Docker's internal
network. No public ingress. Even if the bearer leaked, an attacker on the
host LAN would need network access to reach it.

### 7.2 Per-user rate limit

Enforced at TWO layers (defense in depth):

- **Python bridge**: existing `qanot/ratelimit.py` sliding window. 5
  videos/user/day default. Blocks before the LLM composition call (saves
  cost).
- **Render service**: SQLite `quota_ledger` check before enqueueing. Same
  default. Authoritative when multiple bots share the service (later).

### 7.3 Per-bot cost cap

The cost is dominated by composition LLM tokens (Sonnet, ~10K-30K tokens per
composition = ~$0.05). Render compute itself is free (already paid-for server
time).

Per-bot daily cap default: $1/day for video composition. Configurable.
Tracked in `quota_ledger.cost_usd_micros`. Exceeded → `429`.

### 7.4 Asset URL allowlist

Covered in §6.3. Lint-time rejection of:

- Private IPs (post-DNS check, same as `web_fetch`)
- Cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`)
- Non-HTTPS for non-localhost
- File:// outside the asset whitelist

### 7.5 Chromium sandbox

Chromium runs with:

- `--no-sandbox` is **disabled** (default sandbox enabled). Container runs as
  non-root user with `--cap-add=SYS_ADMIN` to allow user namespace sandbox.
  (HeyGen's Dockerfile has the right invocation; we copy it.)
- `--disable-dev-shm-usage` (Docker `/dev/shm` is too small)
- `--disable-gpu` (no GPU on topkey; software render)
- Network access enabled — needed for asset fetch — but only to URLs the lint
  step approved. (The browser itself trusts HTML; we trust the lint.)

### 7.6 No code execution from user input

User input (the natural-language brief) is **never** passed directly to a
shell command, never `eval`'d, never used as part of a JSON merge into the
composition. The agent authors the composition; user words become content,
not code.

The composition itself is HTML+JS that runs in a sandboxed headless browser.
A malicious composition cannot escape the browser to the host (Chromium's
sandbox is the same one billions of users rely on).

---

## 8. Observability

### 8.1 Metrics (Prometheus format on `/metrics`)

```
# Counters
video_jobs_submitted_total{bot_id,user_id}
video_jobs_succeeded_total{bot_id,user_id}
video_jobs_failed_total{bot_id,error_code}
video_jobs_cancelled_total{bot_id}
video_lint_failures_total{bot_id}

# Histograms (in seconds)
video_render_duration_seconds{format,quality,duration_seconds_bucket}
video_lint_duration_seconds
video_total_lifecycle_seconds{bot_id}

# Gauges
video_queue_depth
video_worker_busy
video_disk_used_bytes
video_disk_free_bytes
video_chromium_processes
video_memory_rss_bytes
```

### 8.2 Logs

JSON-structured (pino), every line:

```json
{
  "level": "info",
  "ts": "2026-04-26T08:00:18.234Z",
  "service": "qanot-video",
  "job_id": "01HXYZ...",
  "bot_id": "topkeydevbot",
  "user_id": "12345",
  "request_id": "uuid",
  "stage": "rendering_frames",
  "progress": 35,
  "msg": "frames captured"
}
```

`request_id` is the join key with the Python framework's logs (the Python
bridge logs the same `request_id` on submit). One grep across both
containers reconstructs the full lifecycle.

### 8.3 Dashboard integration

The existing Qanot dashboard (`qanot/dashboard.py`) gets a new
`/api/video` endpoint that proxies a curated subset of metrics + recent jobs:

```json
{
  "queue_depth": 0,
  "worker_busy": false,
  "jobs_today": {"succeeded": 14, "failed": 1, "cancelled": 0},
  "recent_jobs": [
    {"job_id": "...", "bot_id": "...", "status": "succeeded",
     "duration_s": 38, "format": "9:16"}
  ],
  "disk_free_bytes": 8500000000
}
```

Future: a dedicated Video tab on the dashboard HTML (out of scope for v1
implementation; metrics endpoint is enough).

---

## 9. Failure Modes & Recovery

### 9.1 Render service crash

- Docker restart policy: `unless-stopped`. Process restart: <1s.
- In-flight jobs: marked `leased_until=<future>` in SQLite. After 180s with
  no heartbeat, next worker picks them up. The render is restarted from
  scratch (no checkpointing — rendering is short enough that retry is
  cheaper than checkpoint complexity).
- Queued jobs: durable in SQLite, picked on restart.
- HTTP clients: retry safe (idempotent on `request_id`).

### 9.2 Chromium hang or OOM

- Per-job timeout (`deadline_seconds`, default 120). Exceeded → SIGTERM, 5s
  grace, SIGKILL. Job marked `render_timeout` or `chrome_crash`.
- Container memory limit (1.5 GB). Worker monitors RSS every 5s; if >90%,
  refuses new jobs and waits for current to finish.
- OOM killer at host level: container restarts; queued jobs survive.

### 9.3 SQLite corruption

- Daily backup: SQLite `VACUUM INTO '/data/backups/jobs-YYYYMMDD.db'` cron.
- If corruption detected on startup (`PRAGMA integrity_check` fails), restore
  from latest backup; in-flight jobs lost (ack'd via SQLite WAL but worst
  case is one day of jobs, all of which are recoverable on re-submit).
- WAL mode enabled (`PRAGMA journal_mode=WAL`) for crash safety on every
  commit.

### 9.4 Disk full

- Cleanup cron runs every hour: deletes `output_path` files older than 24h
  AND marks rows `expired`.
- If disk usage exceeds 95% even after cleanup, service goes into degraded
  mode: `POST /render` returns 503, existing jobs continue. Alert fires
  (just a log line at ERROR level; later wires into a webhook).
- Output storage is on the same volume as backups, which is a lifecycle risk.
  Mitigation: backup volume is sized 10× a typical day's output (8 GB worth
  of MP4s ~ 800 jobs/day, way over capacity).

### 9.5 Telegram upload failure

- Python framework retries with exponential backoff (existing logic).
- If still failing after 3 retries, user gets an apologetic message with the
  job_id; output stays on disk for 24h, support can re-upload manually.

### 9.6 Worker process leak (Chromium zombies)

- HyperFrames itself reuses Chrome instances within a worker. Single-worker
  service has at most 1 Chrome process at a time.
- A zombie check on startup: kill any orphan `chromium` processes on boot
  (defensive, in case of unclean shutdown).

---

## 10. Capacity & Performance

### 10.1 Per-render resource model

Empirical, from HyperFrames docs and benchmarks:

| Metric | 30s @ 1080×1920 @ 30fps | 60s @ same |
|---|---:|---:|
| Wall time | 8-12 s | 18-25 s |
| Peak RAM | ~700 MB | ~900 MB |
| Peak CPU | 2 cores @ 100% | 2 cores @ 100% |
| Output size | 5-15 MB | 10-30 MB |

Standard quality. Draft is ~40% faster, high is ~60% slower.

### 10.2 Throughput target

Single worker, 30s videos at standard quality: ~6 renders/minute = **360/hour
sustained**, **~8000/day theoretical**. In practice with composition LLM
authoring (~5-15s per video) the bottleneck is split:
- ~10s LLM author + ~10s render = 20s end-to-end.
- 180/hour, ~4000/day theoretical.

For perspective: current `create_reel` throughput is ~12-20 reels/hour
(blocking, 3-5 min each). New system is **10× faster end-to-end**.

### 10.3 Scaling strategy

When sustained load >500/day:

1. Bump worker concurrency to 2. Requires RAM upgrade (CCX22 / 8 GB, €27/mo).
2. When 2 workers saturate, split render service to a second host. The
   Python bridge already speaks HTTP; only the URL changes.
3. When 2 hosts saturate, introduce Redis-backed queue (replacing SQLite)
   and Lambda-style horizontal workers.

Each step is independent. No premature work for steps 2 and 3.

---

## 11. Cost Model

### 11.1 LLM cost (composition writing)

- Sonnet 4.6: $3/Mtok input, $15/Mtok output
- Composition prompt: ~5 KB skill + ~2 KB DESIGN.md + ~500 tok user brief =
  ~8K input
- Composition output: ~3K tokens (HTML+CSS+GSAP for 30s video)
- **Per render**: ~$0.024 + $0.045 = ~$0.07

### 11.2 Compute cost

Already-paid topkey time. Marginal: zero.

### 11.3 Per-bot cap

Default $1/day → ~14 videos/day. Configurable per bot:

```python
config.video_per_user_daily_cost_usd  # default 0.50
config.video_per_bot_daily_cost_usd   # default 5.00
```

### 11.4 LLM model choice

Sonnet, not Opus, for composition authoring. Justification:

- Composition is well-bounded (HTML+CSS+GSAP within HyperFrames spec). Sonnet
  scores within 5% of Opus on structured output tasks.
- 5× cheaper.
- Skill injection narrows the model's degrees of freedom further; the cost
  of model intelligence drops as task structure rises.

If quality regression appears in production, escalate to Opus per-bot
override. Revisit after 100 jobs.

---

## 12. Versioning & Dependencies

### 12.1 HyperFrames pin

Pin to exact version: `hyperframes@0.4.30` (current latest as of 2026-04-26).
Renovate disabled for this dep. Manual upgrades, gated by:

1. Read changelog for breaking changes.
2. Run full test suite against new version.
3. Render 5 representative compositions; diff outputs (pixel diff via
   `imagemagick compare`).
4. If clean, bump.

### 12.2 Chromium pin

Pinned via Dockerfile (`chromium=<exact version>`). Re-build image only on
deliberate Chromium upgrade. Determinism matters — same composition →
identical bytes.

### 12.3 Update strategy

- Node.js: stay on 22 LTS until 2027.
- HyperFrames: per §12.1.
- Other deps: Renovate enabled, weekly batched PR, auto-merge on patch+test
  green.

---

## 13. Migration from Legacy Reels

### 13.1 Coexistence period

Both engines registered. Per-bot config flag:

```json
{ "video_engine": "hyperframes" }   // or "legacy_reels" or "off"
```

Default for **new** bots: `hyperframes`.
Default for **existing** bots: `legacy_reels` until ops flips them.

### 13.2 Deprecation timeline

| Date (relative to v1 ship) | Event |
|---|---|
| T+0 | v1 ships. Both engines run. |
| T+14d | All Telegram bots flipped to `hyperframes`. |
| T+30d | `legacy_reels` deregistered. `plugins/reels/` moved to `plugins/reels-legacy/`. |
| T+90d | `plugins/reels-legacy/` deleted. |

### 13.3 What we keep

- ElevenLabs/Muxlisa/KotibAI voice infrastructure (`qanot/voice.py`) —
  used by the new engine for narration.
- Pexels API key — useful for stock B-roll references (the agent may pick
  Pexels URLs from the asset registry).
- Brand fonts (Montserrat etc.) downloaded by the legacy plugin —
  preserved in the asset cache.

---

## 14. Implementation Plan

### 14.1 Phases

#### Phase 1 — Render service skeleton (Week 1)

- Repo bootstrap: `services/video/` with package.json, tsconfig, Dockerfile,
  CI workflow, lint, test infra.
- HTTP server with `/health`, auth middleware, error envelope.
- SQLite schema + migration runner.
- Empty worker loop.
- Deploy to topkey alongside existing bots; smoke test `/health`.

**Deliverable**: container running, no actual rendering yet.

#### Phase 2 — Render integration (Week 2)

- `npx hyperframes lint` wrapper with timeout + parsed output.
- `npx hyperframes render` wrapper with progress streaming and timeout.
- Job state machine in worker.
- `POST /render`, `GET /jobs/:id`, `GET /jobs/:id/output` endpoints.
- Integration test: submit a fixed composition, get a real MP4.

**Deliverable**: service can render videos via HTTP. Not yet wired to Qanot.

#### Phase 3 — Python bridge (Week 3)

- `qanot/tools/video.py` with `render_video` tool.
- Composition sub-agent flow.
- Skill loading + DESIGN.md handling.
- Telegram progress UX.
- Unit tests with mocked render service.
- Per-user/per-bot rate limits in Python.

**Deliverable**: agent in a test bot can render a video end-to-end.

#### Phase 4 — Hardening + observability (Week 4)

- Metrics endpoint with all counters/histograms in §8.1.
- Dashboard `/api/video`.
- Failure-mode tests (timeout, OOM simulation, lint-fail-retry, disk-full).
- Cost cap enforcement + tests.
- Asset URL allowlist + tests.
- Alerting (log threshold rules).
- Backup cron for SQLite.
- Cleanup cron for outputs.

**Deliverable**: production-ready service, all failure modes covered.

#### Phase 5 — Migration + docs (Week 5)

- Per-bot toggle wired into config.
- `templates/workspace/skills/hyperframes/SKILL.md` localized + trimmed.
- Operator runbook (how to flip a bot, debug a stuck job, restore from
  backup).
- User-facing documentation.
- Roll out to first real bot.
- Monitor 1 week; flip remaining bots on T+14d schedule.

**Deliverable**: real customers using hyperframes engine. Legacy still
available as fallback.

### 14.2 Test strategy

Each phase has its own test budget:

- **Phase 1**: ≥10 tests, covering auth, route mounting, SQLite migration,
  health endpoint.
- **Phase 2**: ≥20 tests, covering lint pass/fail, render success, render
  timeout, Chrome crash simulation, output file integrity.
- **Phase 3**: ≥15 tests, covering composition sub-agent prompt construction,
  rate limit, cost cap, retry-with-feedback, Telegram progress edits.
- **Phase 4**: ≥20 tests, covering all error codes, metrics correctness,
  cleanup cron, disk-full degraded mode.

Total: ~65 new tests for the engine + bridge + service.

CI: every PR runs full unit suite + a real-render integration test on a
~3-second smoke composition. Integration tests gated by `RUN_INTEGRATION=1`
env to avoid 30s CI overhead on doc-only PRs.

---

## 15. Open Questions / Future Work

These are deliberately deferred and not blocking v1:

1. **HyperFrames Studio integration** — the visual editor exists; could
   embed in the dashboard for ops to author/debug compositions. Out of scope
   for v1.
2. **Composition templates** — pre-authored DESIGN.md presets for common
   verticals (mahalla, e-commerce, e'lon). Out of scope; v1 ships with
   "default neutral" only.
3. **Kokoro TTS evaluation** — local TTS via HyperFrames could replace
   ElevenLabs for cost savings. Benchmark Uzbek phoneme quality before
   adopting.
4. **Video editing** — "shorten this video by 5s", "add subtitle to existing
   render". Different problem; v1 is generation only.
5. **Long-form** — current cap 60s. Bumping to 5min is RAM and time
   budget — defer until demand.
6. **Webhook callbacks** — for clients that don't poll. Nothing in qanot
   needs this.
7. **Render result CDN** — Telegram `file_id` is the de facto CDN. Direct
   browser playback (qanot dashboard) needs to be designed; out of scope.
8. **A/B testing of compositions** — render two variants, compare engagement.
   Product question, not engineering.

---

## Appendix A — Composition skill content (excerpt)

The full skill is `templates/workspace/skills/hyperframes/SKILL.md` (copied
from upstream `/skills/hyperframes/SKILL.md`, ~1100 lines). Brief excerpt
showing what the agent receives in its system prompt:

```markdown
You are authoring HyperFrames compositions. Output strictly valid HTML5.

Hard rules:
- Output ONLY the HTML. No commentary, no markdown fences, no preamble.
- The root element must have data-composition-id="main", data-start="0",
  data-duration matching the requested seconds, data-width and data-height
  matching the requested format (1080×1920 for 9:16, etc.).
- All animations use GSAP timelines registered to window.__timelines.
- Audio must be in <audio> tags (video elements muted).
- Asset URLs must be HTTPS (or HTTP only for localhost / data: only for
  small images).

Composition structure:
1. <!doctype html> + html + head with meta + GSAP CDN script
2. <style> with reset + brand-aware colors and typography
3. <body> with #root scaffold per spec
4. <script> with GSAP timeline ending exactly at data-duration
```

## Appendix B — Sample DESIGN.md

```markdown
# Bot brand: TopKey Dev

## Palette
- Background: #0a0a0a (near-black)
- Primary text: #ffffff
- Accent: #00ff88 (emerald)
- Muted: #a0a0a0

## Typography
- Headings: 'Inter', system-ui (700-800 weight)
- Body: 'Inter', system-ui (400-500 weight)
- Heading scale: 96px / 64px / 48px

## Motion
- Easing default: power3.out for entrances, power2.in for exits
- Duration default: 0.7s entrances, 0.5s exits

## Imagery
- Logo: top-left, 80px wide, 40px from edge, opacity 0.6
- Background: solid #0a0a0a or video B-roll dimmed to 40% with overlay
```

---

## Sign-off

Reviewers:

- [ ] Architecture approved
- [ ] Security approved (§7)
- [ ] Operational concerns approved (§9, §10)
- [ ] Ready to begin Phase 1

Once signed off, Phase 1 work begins on a feature branch
`feature/video-engine-phase-1`.
