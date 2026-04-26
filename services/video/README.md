# qanot-video

Render service for the Qanot video engine. Wraps HyperFrames (Apache 2.0)
behind an HTTP API so the Python framework can submit composition HTML and
get back rendered MP4 files.

> **Status: Phase 2 -- render integration.** The service accepts jobs,
> lints + renders compositions via `npx hyperframes`, and serves the
> resulting MP4. Phase 3 wires the Python framework into the HTTP API.
> Full spec: [`docs/video-engine/ARCHITECTURE.md`](../../docs/video-engine/ARCHITECTURE.md).

## What it does (target end state)

- Accepts `POST /render` with HTML+CSS+GSAP composition + format/duration.
- Lints the composition, queues, renders via Chromium + FFmpeg.
- Exposes `GET /jobs/:id` for progress polling and `GET /jobs/:id/output`
  for MP4 retrieval.
- Single render worker, SQLite-backed durable queue, 24h output retention.
- Service-to-service Bearer auth on every endpoint except `/health`.

## What Phase 2 ships

| Endpoint | Behavior |
|---|---|
| `GET /health` | 200 `{"ok":true}` (public) |
| `POST /render` | 202 + `{job_id, status, queue_position, estimated_start_seconds}`; 200 on idempotent retry; 400 on validation; 413 if body > 256 KB; 503 in degraded mode |
| `GET /jobs/:id` | 200 with full status payload; 404 if unknown |
| `GET /jobs/:id/output` | 200 streaming MP4 with `Content-Disposition: attachment`; 404 if not yet succeeded; 410 if expired or output missing |
| `DELETE /jobs/:id` | 200 transitions queued -> cancelled atomically; 200 signals worker for in-flight; 409 on terminal state |
| `GET /metrics` | Prometheus text: jobs submitted/succeeded/failed/cancelled, lint failures, render/lint duration histograms, queue depth, worker busy gauge |

The worker leases queued jobs, runs `npx hyperframes lint --json` then
`npx hyperframes render`, atomically renames `<job_id>.tmp.mp4` ->
`<job_id>.mp4` on success. Lease is bumped every 30s during long renders.
Crash recovery on startup re-queues jobs whose lease expired without
completion.

## Local development

```bash
cd services/video
cp .env.example .env
# edit .env -- generate SERVICE_SECRET via `openssl rand -hex 32`
bun install            # bun is the package manager + lockfile authority
bun run test           # vitest on Node
bun run dev            # tsx watch src/server.ts
curl http://127.0.0.1:8770/health
# {"ok":true}
```

### Why Node, not Bun, at runtime

The architecture spec (§3.2) names Node.js 22 LTS as the runtime ("Match
HyperFrames requirements"). better-sqlite3 -- the spec's chosen queue
backend -- ships a native binding that does not load under the Bun runtime
yet (`oven-sh/bun#4290`). Bun is therefore used as the package manager and
lint runner; the server process itself is Node, executed via `tsx` so we
keep the no-build TypeScript flow.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Loopback only (per §7.1). |
| `PORT` | `8770` | HTTP port. |
| `SERVICE_SECRET` | _required_ | Bearer token. `openssl rand -hex 32`. Min 16 chars. |
| `DB_PATH` | `./data/jobs.db` | SQLite file. WAL mode. |
| `OUTPUT_DIR` | `./data/renders` | Where Phase 2 will write MP4s. |
| `LOG_LEVEL` | `info` | pino level. `silent` in tests. |
| `NODE_ENV` | `development` | `development` enables pino-pretty. |

## Tests

```bash
bun run test             # vitest run
bun run lint             # oxlint
bun run typecheck        # tsc --noEmit
```

Unit tests use Hono's `app.fetch(request)` directly -- no socket.
The integration suite starts the @hono/node-server listener on
`127.0.0.1:0` (free port) and exercises the routes via `fetch`.

## Layout

```
services/video/
├── Dockerfile             # Bun + Chromium + FFmpeg
├── package.json           # pinned deps
├── tsconfig.json          # strict TS
├── src/
│   ├── server.ts          # Hono app, @hono/node-server, graceful shutdown
│   ├── config.ts          # zod-validated env
│   ├── types.ts           # Job, JobStatus, RenderRequest, LintResult, RenderResult
│   ├── auth/service-key.ts
│   ├── observability/
│   │   ├── logger.ts      # pino + pino-pretty in dev
│   │   └── metrics.ts     # Prometheus text builder (counters/gauges/histograms)
│   ├── queue/
│   │   ├── db.ts          # better-sqlite3 + migration runner
│   │   ├── jobs.ts        # CRUD + transitions + lease + recovery
│   │   └── worker.ts      # state machine: queued -> linting -> rendering -> succeeded/failed
│   ├── render/
│   │   ├── timeout.ts     # spawn-with-timeout + SIGTERM-then-SIGKILL escalation
│   │   ├── lint.ts        # npx hyperframes lint --json wrapper
│   │   └── render.ts      # npx hyperframes render wrapper (progress, atomic write)
│   └── routes/
│       ├── health.ts      # /health
│       ├── render.ts      # POST /render (zod validation, idempotency, 503)
│       ├── jobs.ts        # GET /jobs/:id, GET .../output, DELETE
│       └── metrics.ts     # GET /metrics
├── test/
│   ├── unit/
│   │   ├── auth.test.ts
│   │   ├── db.test.ts
│   │   ├── health.test.ts
│   │   ├── jobs.test.ts
│   │   ├── render-lint.test.ts
│   │   ├── render-render.test.ts
│   │   └── worker.test.ts
│   └── integration/
│       ├── server.test.ts          # boot + auth + health + metrics
│       ├── render-flow.test.ts     # POST /render + GET /jobs/:id + DELETE
│       └── real-render.test.ts     # gated by RUN_INTEGRATION=1
└── compositions/
    └── _smoke.html        # 2-second 1080x1920 composition for real-render
```

## Phase 3 onward

See `docs/video-engine/ARCHITECTURE.md` §14.1 for the full plan. Phase 3
adds `qanot/tools/video.py` and the composition sub-agent.
