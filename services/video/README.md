# qanot-video

Render service for the Qanot video engine. Wraps HyperFrames (Apache 2.0)
behind an HTTP API so the Python framework can submit composition HTML and
get back rendered MP4 files.

> **Status: Phase 1 -- skeleton, no rendering yet.** This service starts,
> serves `/health`, persists a job queue schema in SQLite, and exposes auth-
> guarded endpoints that all return `501 Not Implemented` until Phase 2.
> Full spec: [`docs/video-engine/ARCHITECTURE.md`](../../docs/video-engine/ARCHITECTURE.md).

## What it does (target end state)

- Accepts `POST /render` with HTML+CSS+GSAP composition + format/duration.
- Lints the composition, queues, renders via Chromium + FFmpeg.
- Exposes `GET /jobs/:id` for progress polling and `GET /jobs/:id/output`
  for MP4 retrieval.
- Single render worker, SQLite-backed durable queue, 24h output retention.
- Service-to-service Bearer auth on every endpoint except `/health`.

## What Phase 1 actually ships

| Endpoint | Phase 1 behavior |
|---|---|
| `GET /health` | 200 `{"ok":true}` (public) |
| `POST /render` | 501 `not_implemented` |
| `GET /jobs/:id` | 501 `not_implemented` |
| `GET /jobs/:id/output` | 501 `not_implemented` |
| `DELETE /jobs/:id` | 501 `not_implemented` |
| `GET /metrics` | Prometheus text (process_start_time + http_requests_total) |

The worker loop polls every second but never finds a job to lease (Phase 2
implements the lease query). The DB schema is fully laid down so Phase 2 can
just write rows.

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
├── Dockerfile             # Bun + Chromium + FFmpeg, ready for Phase 2.
├── package.json           # pinned deps
├── tsconfig.json          # strict TS
├── src/
│   ├── server.ts          # Hono app, @hono/node-server, graceful shutdown
│   ├── config.ts          # zod-validated env
│   ├── types.ts           # Job, JobStatus, RenderRequest, ErrorEnvelope
│   ├── auth/service-key.ts
│   ├── observability/
│   │   ├── logger.ts      # pino + pino-pretty in dev
│   │   └── metrics.ts     # Prometheus text builder (no prom-client dep)
│   ├── queue/
│   │   ├── db.ts          # better-sqlite3 + migration runner
│   │   ├── jobs.ts        # CRUD + transitions + ULID
│   │   └── worker.ts      # poll loop (does no work yet)
│   └── routes/
│       ├── health.ts      # full impl
│       ├── render.ts      # 501 stub
│       ├── jobs.ts        # 501 stubs
│       └── metrics.ts     # minimal
├── test/
│   ├── unit/
│   │   ├── auth.test.ts
│   │   ├── db.test.ts
│   │   ├── jobs.test.ts
│   │   └── health.test.ts
│   └── integration/
│       └── server.test.ts
└── compositions/
    └── _smoke.html        # placeholder for Phase 2 smoke test
```

## Phase 2 onward

See `docs/video-engine/ARCHITECTURE.md` §14.1 for the full plan. Phase 2
fills in `lint` / `render` / state-transition logic in `src/queue/worker.ts`
and turns the 501 routes into real implementations.
