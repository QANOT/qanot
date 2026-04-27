/**
 * GET /summary tests.
 *
 * The summary endpoint backs the Qanot dashboard's /api/video proxy. It
 * must return a stable shape, count today's jobs by UTC day boundary, and
 * order recent_jobs newest-first.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Database as SqliteDb } from "better-sqlite3";
import { resetConfigForTesting } from "../../src/config.js";
import { resetLoggerForTesting } from "../../src/observability/logger.js";
import { openDatabase } from "../../src/queue/db.js";
import { insertJob, transitionStatus } from "../../src/queue/jobs.js";
import { computeSummary } from "../../src/routes/summary.js";
import { Worker } from "../../src/queue/worker.js";
import {
  JobStatus,
  RenderQuality,
  VideoFormat,
  type RenderRequest,
} from "../../src/types.js";

let workDir: string;
let db: SqliteDb;
let worker: Worker;
let originalEnv: NodeJS.ProcessEnv;

const baseRequest: RenderRequest = {
  request_id: "00000000-0000-0000-0000-000000000000",
  bot_id: "topkeydevbot",
  user_id: "u-1",
  composition_html: "<!doctype html><html></html>",
  format: VideoFormat.Vertical,
  duration_seconds: 5,
  fps: 30,
  quality: RenderQuality.Standard,
  deadline_seconds: 30,
};

beforeEach(() => {
  originalEnv = { ...process.env };
  process.env.SERVICE_SECRET = "summary-test-secret-aaaaaaaaaaaaaaaaaaaa";
  process.env.LOG_LEVEL = "silent";
  process.env.NODE_ENV = "test";
  resetConfigForTesting();
  resetLoggerForTesting();

  workDir = mkdtempSync(join(tmpdir(), "qanot-video-summary-"));
  db = openDatabase(join(workDir, "jobs.db"));
  worker = new Worker({
    db,
    outputDir: join(workDir, "renders"),
    pollIntervalMs: 1000,
    lintFn: async () => ({ ok: true, warnings: [], duration_ms: 1 }),
    renderFn: async () => ({
      ok: true,
      output_path: "",
      output_size_bytes: 0,
      render_duration_ms: 1,
    }),
  });
  // We do NOT start the worker -- we only need the getCurrentJobId hook.
});

afterEach(async () => {
  await worker.stop();
  db.close();
  rmSync(workDir, { recursive: true, force: true });
  process.env = originalEnv;
  resetConfigForTesting();
  resetLoggerForTesting();
});

function utcStartOfDay(): number {
  const now = new Date();
  return Math.floor(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) / 1000,
  );
}

function landJob(reqId: string, finishedAt: number, status: JobStatus): string {
  const { job } = insertJob(db, { ...baseRequest, request_id: reqId });
  transitionStatus(db, job.id, JobStatus.Queued, JobStatus.Linting);
  transitionStatus(db, job.id, JobStatus.Linting, JobStatus.Rendering);
  transitionStatus(db, job.id, JobStatus.Rendering, status, {
    finished_at: finishedAt,
    render_duration_ms: 12_345,
  });
  return job.id;
}

describe("computeSummary shape", () => {
  it("returns the documented top-level keys", () => {
    const out = computeSummary({
      db,
      worker,
      outputDir: workDir,
    });
    expect(Object.keys(out).toSorted()).toEqual(
      [
        "disk_free_bytes",
        "jobs_today",
        "queue_depth",
        "recent_jobs",
        "service_healthy",
        "worker_busy",
      ].toSorted(),
    );
    expect(out.service_healthy).toBe(true);
    expect(out.worker_busy).toBe(false);
    expect(out.queue_depth).toBe(0);
    expect(out.jobs_today).toEqual({ succeeded: 0, failed: 0, cancelled: 0 });
    expect(out.recent_jobs).toEqual([]);
  });
});

describe("jobs_today UTC filtering", () => {
  it("counts only jobs whose finished_at lies in today's UTC window", () => {
    const startOfToday = utcStartOfDay();
    // 2 succeeded today, 1 yesterday, 1 failed today, 1 cancelled today.
    landJob("00000000-0000-0000-0000-000000000a01", startOfToday + 100, JobStatus.Succeeded);
    landJob("00000000-0000-0000-0000-000000000a02", startOfToday + 200, JobStatus.Succeeded);
    landJob("00000000-0000-0000-0000-000000000a03", startOfToday - 86_400, JobStatus.Succeeded);
    landJob("00000000-0000-0000-0000-000000000a04", startOfToday + 300, JobStatus.Failed);
    landJob("00000000-0000-0000-0000-000000000a05", startOfToday + 400, JobStatus.Cancelled);

    const out = computeSummary({ db, worker, outputDir: workDir });
    expect(out.jobs_today.succeeded).toBe(2);
    expect(out.jobs_today.failed).toBe(1);
    expect(out.jobs_today.cancelled).toBe(1);
  });
});

describe("recent_jobs ordering", () => {
  it("returns rows ordered descending by queued_at", () => {
    const startOfToday = utcStartOfDay();
    const ids = [
      landJob("00000000-0000-0000-0000-000000000b01", startOfToday + 10, JobStatus.Succeeded),
      landJob("00000000-0000-0000-0000-000000000b02", startOfToday + 20, JobStatus.Succeeded),
      landJob("00000000-0000-0000-0000-000000000b03", startOfToday + 30, JobStatus.Succeeded),
    ];
    const out = computeSummary({ db, worker, outputDir: workDir });
    expect(out.recent_jobs.length).toBe(3);
    // queued_at is descending across the array.
    const queued = out.recent_jobs.map((j) => j.queued_at);
    const sorted = [...queued].toSorted((a, b) => (a < b ? 1 : a > b ? -1 : 0));
    expect(queued).toEqual(sorted);
    // Each row carries the documented fields.
    for (const r of out.recent_jobs) {
      expect(typeof r.job_id).toBe("string");
      expect(r.bot_id).toBe("topkeydevbot");
      expect(r.format).toBe("9:16");
      expect(r.duration_s).toBe(12);
      expect(typeof r.queued_at).toBe("string");
    }
    // sanity check: at least one of our inserted ids appears
    expect(ids.some((id) => out.recent_jobs.some((r) => r.job_id === id))).toBe(true);
  });
});
