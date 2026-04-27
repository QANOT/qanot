/**
 * POST /render -- accept a render job.
 *
 * Per docs/video-engine/ARCHITECTURE.md §3.4:
 *   - 256 KB body cap (413).
 *   - Idempotent on `request_id` (returns existing job, status 200).
 *   - 503 in degraded mode (memory or disk pressure).
 *   - 202 + { job_id, status, queue_position, estimated_start_seconds }.
 *
 * Validation is via zod; the schema mirrors §3.4 exactly. The body-limit
 * middleware uses Hono's stock `bodyLimit` so we get a uniform 413 envelope
 * and avoid buffering arbitrarily large requests into memory.
 */

import { Hono } from "hono";
import { bodyLimit } from "hono/body-limit";
import type { Database as SqliteDb } from "better-sqlite3";
import { z } from "zod";
import type { AppEnv } from "../server.js";
import { childLogger } from "../observability/logger.js";
import { incCounter } from "../observability/metrics.js";
import { probeDisk } from "../observability/sampler.js";
import { insertJob, queuePosition } from "../queue/jobs.js";
import {
  RenderQuality,
  VideoFormat,
  type ErrorEnvelope,
  type RenderRequest,
} from "../types.js";

const MAX_BODY_BYTES = 256 * 1024;
const MEMORY_RSS_LIMIT_BYTES = 1.4 * 1024 * 1024 * 1024;
const DISK_FULL_RATIO = 0.95;
const ESTIMATED_SECONDS_PER_QUEUED = 25;

const RenderRequestSchema = z.object({
  request_id: z.string().uuid(),
  bot_id: z.string().min(1).max(128),
  user_id: z.string().min(1).max(128),
  composition_html: z.string().min(1).max(MAX_BODY_BYTES),
  format: z.enum([VideoFormat.Vertical, VideoFormat.Horizontal, VideoFormat.Square]),
  duration_seconds: z.number().int().min(1).max(60),
  fps: z.union([z.literal(24), z.literal(30), z.literal(60)]).optional(),
  quality: z
    .enum([RenderQuality.Draft, RenderQuality.Standard, RenderQuality.High])
    .optional(),
  deadline_seconds: z.number().int().min(1).max(600).optional(),
});

export interface RenderRouteDeps {
  db: SqliteDb;
  /**
   * Output directory; used by the default disk-pressure probe so the 503
   * envelope surfaces when the OUTPUT_DIR filesystem is >95% full.
   */
  outputDir?: string;
  /**
   * Optional probe override. Tests inject this to simulate degraded states.
   * When omitted, the default probe is built from `outputDir`.
   */
  isDegraded?: () => DegradedReason | null;
}

export interface DegradedReason {
  code: "memory_pressure" | "degraded_disk_full";
  message: string;
  retry_after_seconds: number;
}

export function buildRenderRoutes(deps: RenderRouteDeps): Hono<AppEnv> {
  const app = new Hono<AppEnv>();
  const probe =
    deps.isDegraded ??
    (() => defaultDegradedProbe(deps.outputDir));

  app.use(
    "/render",
    bodyLimit({
      maxSize: MAX_BODY_BYTES,
      onError: (c) => {
        const body: ErrorEnvelope = {
          error: {
            code: "payload_too_large",
            message: `Request body exceeds ${MAX_BODY_BYTES.toString()} bytes.`,
          },
        };
        return c.json(body, 413);
      },
    }),
  );

  app.post("/render", async (c) => {
    const log = c.get("logger") ?? childLogger({ component: "render-route" });

    const degraded = probe();
    if (degraded) {
      const body: ErrorEnvelope = {
        error: {
          code: "service_unavailable",
          message: degraded.message,
          details: {
            code: degraded.code,
            retry_after_seconds: degraded.retry_after_seconds,
          },
        },
      };
      log.warn(
        { degraded_code: degraded.code },
        "rejecting render in degraded mode",
      );
      return c.json(body, 503, {
        "Retry-After": String(degraded.retry_after_seconds),
      });
    }

    let raw: unknown;
    try {
      raw = await c.req.json();
    } catch {
      const body: ErrorEnvelope = {
        error: { code: "invalid_json", message: "Request body is not valid JSON." },
      };
      return c.json(body, 400);
    }

    const parsed = RenderRequestSchema.safeParse(raw);
    if (!parsed.success) {
      const issues = parsed.error.issues.map((i) => ({
        path: i.path.join("."),
        code: i.code,
        message: i.message,
      }));
      const body: ErrorEnvelope = {
        error: {
          code: "validation_failed",
          message: "Request body failed validation.",
          details: issues,
        },
      };
      log.info({ issues }, "render request validation failed");
      return c.json(body, 400);
    }

    const req = parsed.data satisfies RenderRequest;

    const result = insertJob(deps.db, req);
    if (result.duplicate) {
      log.info(
        { job_id: result.job.id, request_id: req.request_id },
        "duplicate request_id; returning existing job",
      );
      return c.json(buildAcceptedBody(deps.db, result.job), 200);
    }

    incCounter("video_jobs_submitted_total", { bot_id: req.bot_id });
    log.info(
      {
        job_id: result.job.id,
        request_id: req.request_id,
        format: req.format,
        duration_seconds: req.duration_seconds,
      },
      "job enqueued",
    );
    return c.json(buildAcceptedBody(deps.db, result.job), 202);
  });

  return app;
}

function buildAcceptedBody(
  db: SqliteDb,
  job: { id: string; status: string; queued_at: number },
): {
  job_id: string;
  status: string;
  queue_position: number;
  estimated_start_seconds: number;
} {
  const position = queuePosition(db, job.queued_at);
  return {
    job_id: job.id,
    status: job.status,
    queue_position: position,
    estimated_start_seconds: position * ESTIMATED_SECONDS_PER_QUEUED,
  };
}

/**
 * Default degraded-mode probe.
 *   - RAM: process RSS over MEMORY_RSS_LIMIT_BYTES (90% of the 1.5 GB cap).
 *   - Disk: statvfs on OUTPUT_DIR; reject when used/total > 95% per §9.4.
 *
 * On platforms where statvfs is unavailable (Windows test runners) the
 * disk probe degrades gracefully -- we silently skip it and rely on the
 * cleanup cron to keep capacity under control.
 */
export function defaultDegradedProbe(
  outputDir: string | undefined,
): DegradedReason | null {
  const usage = process.memoryUsage();
  if (usage.rss > MEMORY_RSS_LIMIT_BYTES) {
    return {
      code: "memory_pressure",
      message: "Service is at memory capacity. Retry shortly.",
      retry_after_seconds: 30,
    };
  }
  if (outputDir) {
    const disk = probeDisk(outputDir);
    if (disk && disk.usage_ratio > DISK_FULL_RATIO) {
      return {
        code: "degraded_disk_full",
        message: `Output volume ${disk.mount} is full (${(disk.usage_ratio * 100).toFixed(1)}% used). Retry after cleanup.`,
        retry_after_seconds: 300,
      };
    }
  }
  return null;
}
