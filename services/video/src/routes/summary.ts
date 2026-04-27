/**
 * GET /summary -- curated dashboard snapshot.
 *
 * Per docs/video-engine/ARCHITECTURE.md §8.3: the Qanot dashboard
 * (`qanot/dashboard.py`) needs a small structured payload covering queue
 * depth, worker status, today's job totals, recent jobs, and disk free.
 *
 * Returning this from a dedicated endpoint (rather than scraping /metrics
 * text) keeps the dashboard simple and avoids re-implementing the
 * Prometheus parser in Python.
 *
 * Shape:
 *   {
 *     "queue_depth": 0,
 *     "worker_busy": false,
 *     "jobs_today": {"succeeded": 14, "failed": 1, "cancelled": 0},
 *     "recent_jobs": [
 *       {"job_id": "...", "bot_id": "...", "status": "succeeded",
 *        "duration_s": 38, "format": "9:16", "queued_at": "..."}
 *     ],
 *     "disk_free_bytes": 8500000000,
 *     "service_healthy": true
 *   }
 *
 * Auth-required per §3.4.
 */

import { Hono } from "hono";
import type { Database as SqliteDb } from "better-sqlite3";
import type { AppEnv } from "../server.js";
import { sampleResourceGauges } from "../observability/sampler.js";
import { queueDepth } from "../queue/jobs.js";
import type { Worker } from "../queue/worker.js";

const RECENT_LIMIT = 20;
const TERMINAL_STATUSES = ["succeeded", "failed", "cancelled"] as const;

export interface SummaryRouteDeps {
  db: SqliteDb;
  worker: Worker;
  outputDir: string;
}

export interface SummaryPayload {
  queue_depth: number;
  worker_busy: boolean;
  jobs_today: {
    succeeded: number;
    failed: number;
    cancelled: number;
  };
  recent_jobs: Array<{
    job_id: string;
    bot_id: string;
    status: string;
    duration_s: number | null;
    format: string;
    queued_at: string;
  }>;
  disk_free_bytes: number | null;
  service_healthy: boolean;
}

export function buildSummaryRoutes(deps: SummaryRouteDeps): Hono<AppEnv> {
  const app = new Hono<AppEnv>();

  app.get("/summary", (c) => {
    const payload = computeSummary(deps);
    return c.json(payload satisfies SummaryPayload);
  });

  return app;
}

/**
 * Compute the summary payload. Exported so tests can call directly without
 * spinning the HTTP server.
 */
export function computeSummary(deps: SummaryRouteDeps): SummaryPayload {
  const depth = safeQueueDepth(deps.db);
  const busy = deps.worker.getCurrentJobId() !== null;
  const jobsToday = countJobsToday(deps.db);
  const recent = listRecentJobs(deps.db);
  const snapshot = sampleResourceGauges(deps.outputDir);
  return {
    queue_depth: depth,
    worker_busy: busy,
    jobs_today: jobsToday,
    recent_jobs: recent,
    disk_free_bytes: snapshot.disk?.free_bytes ?? null,
    service_healthy: true,
  };
}

function safeQueueDepth(db: SqliteDb): number {
  try {
    return queueDepth(db);
  } catch {
    return 0;
  }
}

function countJobsToday(
  db: SqliteDb,
): { succeeded: number; failed: number; cancelled: number } {
  // UTC day boundary: today's "00:00 UTC" in unix seconds.
  const startOfDay = utcStartOfDayEpoch();
  const placeholders = TERMINAL_STATUSES.map(() => "?").join(",");
  const rows = db
    .prepare(
      `SELECT status, COUNT(*) AS n FROM jobs
         WHERE finished_at IS NOT NULL
           AND finished_at >= ?
           AND status IN (${placeholders})
         GROUP BY status`,
    )
    .all(startOfDay, ...TERMINAL_STATUSES) as Array<{ status: string; n: number }>;
  const out = { succeeded: 0, failed: 0, cancelled: 0 };
  for (const r of rows) {
    if (r.status === "succeeded") out.succeeded = r.n;
    else if (r.status === "failed") out.failed = r.n;
    else if (r.status === "cancelled") out.cancelled = r.n;
  }
  return out;
}

function listRecentJobs(
  db: SqliteDb,
): SummaryPayload["recent_jobs"] {
  const rows = db
    .prepare(
      `SELECT id, bot_id, status, format, render_duration_ms, queued_at
         FROM jobs
         ORDER BY queued_at DESC
         LIMIT ?`,
    )
    .all(RECENT_LIMIT) as Array<{
    id: string;
    bot_id: string;
    status: string;
    format: string;
    render_duration_ms: number | null;
    queued_at: number;
  }>;
  return rows.map((r) => ({
    job_id: r.id,
    bot_id: r.bot_id,
    status: r.status,
    duration_s:
      r.render_duration_ms === null ? null : Math.round(r.render_duration_ms / 1000),
    format: r.format,
    queued_at: new Date(r.queued_at * 1000).toISOString(),
  }));
}

function utcStartOfDayEpoch(): number {
  const now = new Date();
  const d = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
    0,
    0,
    0,
    0,
  );
  return Math.floor(d / 1000);
}
