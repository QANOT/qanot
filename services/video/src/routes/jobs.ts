/**
 * /jobs/:id, /jobs/:id/output, DELETE /jobs/:id.
 *
 * Per docs/video-engine/ARCHITECTURE.md §3.4 + §5.1.
 *
 *   GET    /jobs/:id          -> status payload (200 / 404)
 *   GET    /jobs/:id/output   -> stream MP4 (200 / 404 / 410)
 *   DELETE /jobs/:id          -> cancel (200 / 404 / 409)
 */

import { createReadStream, statSync } from "node:fs";
import { Hono } from "hono";
import { stream } from "hono/streaming";
import type { Database as SqliteDb } from "better-sqlite3";
import type { AppEnv } from "../server.js";
import { childLogger } from "../observability/logger.js";
import { incCounter } from "../observability/metrics.js";
import { getById, transitionStatus } from "../queue/jobs.js";
import type { Worker } from "../queue/worker.js";
import {
  type Job,
  type JobErrorCode,
  type JobStage,
  JobStatus,
  type ErrorEnvelope,
} from "../types.js";

export interface JobsRouteDeps {
  db: SqliteDb;
  worker: Worker;
  /** Output dir; final files live at <outputDir>/<job_id>.mp4. */
  outputDir: string;
}

export function buildJobsRoutes(deps: JobsRouteDeps): Hono<AppEnv> {
  const app = new Hono<AppEnv>();

  app.get("/jobs/:id", (c) => {
    const id = c.req.param("id");
    const job = getById(deps.db, id);
    if (!job) {
      return c.json(notFound(id), 404);
    }
    return c.json(buildStatusPayload(job));
  });

  app.get("/jobs/:id/output", (c) => {
    const id = c.req.param("id");
    const job = getById(deps.db, id);
    if (!job) {
      return c.json(notFound(id), 404);
    }
    const nowSeconds = Math.floor(Date.now() / 1000);
    if (
      job.status === JobStatus.Expired ||
      (job.expires_at && job.expires_at < nowSeconds)
    ) {
      const body: ErrorEnvelope = {
        error: {
          code: "expired",
          message: `Job ${id} output expired at ${new Date(job.expires_at * 1000).toISOString()}.`,
        },
      };
      return c.json(body, 410);
    }
    if (job.status !== JobStatus.Succeeded || !job.output_path) {
      const body: ErrorEnvelope = {
        error: {
          code: "not_ready",
          message: `Job ${id} is in status '${job.status}' (no output available).`,
        },
      };
      return c.json(body, 404);
    }

    let size: number;
    try {
      size = statSync(job.output_path).size;
    } catch {
      const body: ErrorEnvelope = {
        error: {
          code: "output_missing",
          message: `Job ${id} output file is no longer present on disk.`,
        },
      };
      return c.json(body, 410);
    }

    c.header("Content-Type", "video/mp4");
    c.header("Content-Length", String(size));
    c.header("Content-Disposition", `attachment; filename="${id}.mp4"`);
    c.header("Cache-Control", "private, max-age=0, must-revalidate");

    return stream(c, async (s) => {
      const fileStream = createReadStream(job.output_path as string);
      s.onAbort(() => {
        fileStream.destroy();
      });
      // Bridge Node Readable -> WHATWG WritableStream chunk by chunk.
      for await (const chunk of fileStream) {
        await s.write(chunk as Uint8Array);
      }
    });
  });

  app.delete("/jobs/:id", (c) => {
    const id = c.req.param("id");
    const log = c.get("logger") ?? childLogger({ component: "jobs-route" });
    const job = getById(deps.db, id);
    if (!job) {
      return c.json(notFound(id), 404);
    }

    if (
      job.status === JobStatus.Succeeded ||
      job.status === JobStatus.Failed ||
      job.status === JobStatus.Expired
    ) {
      const body: ErrorEnvelope = {
        error: {
          code: "terminal_state",
          message: `Job ${id} is in terminal status '${job.status}' and cannot be cancelled.`,
        },
      };
      return c.json(body, 409);
    }

    if (job.status === JobStatus.Cancelled) {
      // Idempotent.
      return c.json({ ok: true, status: job.status, note: "already cancelled" });
    }

    if (job.status === JobStatus.Queued) {
      const finishedAt = Math.floor(Date.now() / 1000);
      const ok = transitionStatus(
        deps.db,
        job.id,
        JobStatus.Queued,
        JobStatus.Cancelled,
        {
          stage: "cancelled",
          finished_at: finishedAt,
          leased_until: null,
        },
      );
      if (ok) {
        incCounter("video_jobs_cancelled_total", { bot_id: job.bot_id });
      }
      log.info({ job_id: id }, "job cancelled (was queued)");
      return c.json({ ok: true, status: JobStatus.Cancelled });
    }

    // status is linting or rendering: signal the worker to abort.
    const signalled = deps.worker.requestCancel(job.id);
    log.info(
      { job_id: id, signalled, status: job.status },
      "cancellation requested for in-flight job",
    );
    return c.json({
      ok: true,
      status: job.status,
      note: "cancellation requested; worker will terminate the subprocess",
    });
  });

  return app;
}

function notFound(id: string): ErrorEnvelope {
  return {
    error: {
      code: "not_found",
      message: `Job ${id} not found.`,
    },
  };
}

interface StatusPayload {
  job_id: string;
  status: JobStatus;
  stage: JobStage | null;
  progress_percent: number;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string;
  output_path?: string;
  output_size_bytes?: number;
  render_duration_seconds?: number;
  error?: { code: JobErrorCode; message: string; details: unknown };
}

function buildStatusPayload(job: Job): StatusPayload {
  const payload: StatusPayload = {
    job_id: job.id,
    status: job.status,
    stage: job.stage,
    progress_percent: job.progress_percent,
    queued_at: toIso(job.queued_at),
    started_at: job.started_at ? toIso(job.started_at) : null,
    finished_at: job.finished_at ? toIso(job.finished_at) : null,
    expires_at: toIso(job.expires_at),
  };

  if (job.status === JobStatus.Succeeded) {
    if (job.output_path !== null) payload.output_path = job.output_path;
    if (job.output_size_bytes !== null) payload.output_size_bytes = job.output_size_bytes;
    if (job.render_duration_ms !== null) {
      payload.render_duration_seconds = Math.round(job.render_duration_ms / 1000);
    }
  }

  if (job.status === JobStatus.Failed && job.error_code) {
    let details: unknown = job.error_details;
    if (typeof details === "string" && details.length > 0) {
      try {
        details = JSON.parse(details);
      } catch {
        /* leave as string */
      }
    }
    payload.error = {
      code: job.error_code,
      message: job.error_message ?? `Job failed with ${job.error_code}.`,
      details,
    };
  }
  return payload;
}

function toIso(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString();
}
