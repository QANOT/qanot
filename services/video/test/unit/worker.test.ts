/**
 * Worker state machine tests.
 *
 * The worker takes lint and render functions as constructor injections, so we
 * pass deterministic stubs rather than spawning real subprocesses. This keeps
 * tests fast and lets us assert exact state transitions.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Database as SqliteDb } from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetConfigForTesting } from "../../src/config.js";
import { resetLoggerForTesting } from "../../src/observability/logger.js";
import { openDatabase } from "../../src/queue/db.js";
import {
  getById,
  insertJob,
  recoverOrphanedJobs,
  transitionStatus,
} from "../../src/queue/jobs.js";
import { Worker } from "../../src/queue/worker.js";
import {
  JobErrorCode,
  JobStatus,
  RenderQuality,
  VideoFormat,
  type LintResult,
  type RenderResult,
  type RenderRequest,
} from "../../src/types.js";

let workDir: string;
let outputDir: string;
let db: SqliteDb;

const baseRequest: RenderRequest = {
  request_id: "11111111-1111-4111-8111-111111111111",
  bot_id: "topkeydevbot",
  user_id: "u-1",
  composition_html: "<!doctype html><html></html>",
  format: VideoFormat.Vertical,
  duration_seconds: 5,
  fps: 30,
  quality: RenderQuality.Standard,
  deadline_seconds: 30,
};

let originalEnv: NodeJS.ProcessEnv;

beforeEach(() => {
  originalEnv = { ...process.env };
  process.env.SERVICE_SECRET = "worker-test-secret-aaaaaaaaaaaaaaaaaaaa";
  process.env.LOG_LEVEL = "silent";
  process.env.NODE_ENV = "test";
  resetConfigForTesting();
  resetLoggerForTesting();

  workDir = mkdtempSync(join(tmpdir(), "qanot-video-worker-"));
  outputDir = join(workDir, "renders");
  db = openDatabase(join(workDir, "jobs.db"));
});

afterEach(async () => {
  db.close();
  rmSync(workDir, { recursive: true, force: true });
  process.env = originalEnv;
  resetConfigForTesting();
  resetLoggerForTesting();
});

/** Build a worker with deterministic lint/render stubs. */
function buildWorker(stubs: {
  lint?: LintResult;
  render?: RenderResult;
  /** If set, render waits this long before resolving (used for cancel test). */
  renderDelayMs?: number;
  /** If set, render checks this signal and resolves with code=Internal. */
  renderHonorsCancel?: boolean;
}): Worker {
  return new Worker({
    db,
    outputDir,
    pollIntervalMs: 25,
    leaseSeconds: 60,
    heartbeatMs: 60_000,
    lintFn: async () =>
      stubs.lint ?? { ok: true, warnings: [], duration_ms: 1 },
    renderFn: async (opts) => {
      if (stubs.renderDelayMs) {
        await new Promise((res, rej) => {
          const t = setTimeout(res, stubs.renderDelayMs);
          if (stubs.renderHonorsCancel && opts.cancel_signal) {
            opts.cancel_signal.addEventListener("abort", () => {
              clearTimeout(t);
              rej(new Error("aborted"));
            });
          }
        }).catch(() => undefined);
      }
      if (stubs.renderHonorsCancel && opts.cancel_signal?.aborted) {
        return {
          ok: false,
          code: JobErrorCode.Internal,
          message: "render cancelled",
          stderr_tail: "",
          render_duration_ms: 1,
        };
      }
      return (
        stubs.render ?? {
          ok: true,
          output_path: join(outputDir, `${opts.job_id}.mp4`),
          output_size_bytes: 1024,
          render_duration_ms: 50,
        }
      );
    },
  });
}

async function waitFor<T>(
  predicate: () => T | null | undefined,
  timeoutMs = 2_000,
): Promise<T> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = predicate();
    if (v !== null && v !== undefined && v !== false) return v as T;
    await new Promise((r) => setTimeout(r, 15));
  }
  throw new Error(`waitFor timed out after ${String(timeoutMs)}ms`);
}

describe("Worker state machine", () => {
  it("transitions queued -> linting -> rendering -> succeeded on the happy path", async () => {
    const { job } = insertJob(db, baseRequest);
    const worker = buildWorker({});
    worker.start();
    try {
      const finished = await waitFor(() => {
        const j = getById(db, job.id);
        return j && (j.status === JobStatus.Succeeded || j.status === JobStatus.Failed)
          ? j
          : null;
      });
      expect(finished.status).toBe(JobStatus.Succeeded);
      expect(finished.progress_percent).toBe(100);
      expect(finished.output_size_bytes).toBe(1024);
      expect(finished.finished_at).not.toBeNull();
    } finally {
      await worker.stop();
    }
  });

  it("marks the job failed with lint_failed when lint reports errors", async () => {
    const { job } = insertJob(db, baseRequest);
    const worker = buildWorker({
      lint: {
        ok: false,
        errors: [
          {
            rule: "root_missing_dimensions",
            severity: "error",
            message: "Root missing data-width",
          },
        ],
        duration_ms: 5,
      },
    });
    worker.start();
    try {
      const finished = await waitFor(() => {
        const j = getById(db, job.id);
        return j && j.status === JobStatus.Failed ? j : null;
      });
      expect(finished.error_code).toBe(JobErrorCode.LintFailed);
      const details = JSON.parse(finished.error_details ?? "[]");
      expect(Array.isArray(details)).toBe(true);
      expect(details).toHaveLength(1);
      expect(details[0].rule).toBe("root_missing_dimensions");
    } finally {
      await worker.stop();
    }
  });

  it("marks the job failed with render_timeout when render returns RenderTimeout", async () => {
    const { job } = insertJob(db, baseRequest);
    const worker = buildWorker({
      render: {
        ok: false,
        code: JobErrorCode.RenderTimeout,
        message: "deadline exceeded",
        stderr_tail: "...",
        render_duration_ms: 1000,
      },
    });
    worker.start();
    try {
      const finished = await waitFor(() => {
        const j = getById(db, job.id);
        return j && j.status === JobStatus.Failed ? j : null;
      });
      expect(finished.error_code).toBe(JobErrorCode.RenderTimeout);
      expect(finished.error_message).toMatch(/deadline/i);
    } finally {
      await worker.stop();
    }
  });

  it("requeues orphaned linting jobs (leased_until in the past) on startup", async () => {
    const { job } = insertJob(db, baseRequest);
    // Manually move the job to linting with an EXPIRED lease, simulating a
    // previous worker that crashed before completing.
    const past = Math.floor(Date.now() / 1000) - 600;
    transitionStatus(db, job.id, JobStatus.Queued, JobStatus.Linting, {
      stage: "linting",
      started_at: past,
      leased_until: past + 60,
    });
    expect(getById(db, job.id)?.status).toBe(JobStatus.Linting);

    // Recovery should re-queue it.
    const recovered = recoverOrphanedJobs(db);
    expect(recovered).toBe(1);
    const after = getById(db, job.id);
    expect(after?.status).toBe(JobStatus.Queued);
    expect(after?.leased_until).toBeNull();

    // And a worker that starts now should pick it up and finish it.
    const worker = buildWorker({});
    worker.start();
    try {
      const finished = await waitFor(() => {
        const j = getById(db, job.id);
        return j && j.status === JobStatus.Succeeded ? j : null;
      });
      expect(finished.status).toBe(JobStatus.Succeeded);
    } finally {
      await worker.stop();
    }
  });

  it("transitions to cancelled when requestCancel fires during render", async () => {
    const { job } = insertJob(db, baseRequest);
    const worker = buildWorker({
      renderDelayMs: 500,
      renderHonorsCancel: true,
    });
    worker.start();
    try {
      // Wait for the worker to actually start rendering (status goes to rendering).
      await waitFor(() => {
        const j = getById(db, job.id);
        return j?.status === JobStatus.Rendering ? j : null;
      });
      // Now signal cancel.
      const cancelled = worker.requestCancel(job.id);
      expect(cancelled).toBe(true);

      const finished = await waitFor(() => {
        const j = getById(db, job.id);
        return j &&
          (j.status === JobStatus.Cancelled || j.status === JobStatus.Failed)
          ? j
          : null;
      });
      expect(finished.status).toBe(JobStatus.Cancelled);
    } finally {
      await worker.stop();
    }
  });

  it("treats unknown jobs as a no-op for requestCancel", async () => {
    const worker = buildWorker({});
    worker.start();
    try {
      expect(worker.requestCancel("does-not-exist")).toBe(false);
    } finally {
      await worker.stop();
    }
  });
});

describe("worker recovery + outputs", () => {
  it("creates the output_dir on construction so renders can publish atomically", () => {
    const target = join(workDir, "fresh-output");
    const w = new Worker({
      db,
      outputDir: target,
      pollIntervalMs: 25,
      lintFn: async () => ({ ok: true, warnings: [], duration_ms: 1 }),
      renderFn: async () => ({
        ok: true,
        output_path: "",
        output_size_bytes: 0,
        render_duration_ms: 1,
      }),
    });
    expect(w.isRunning()).toBe(false);
    // Sanity: createReadStream would otherwise need this to exist.
    writeFileSync(join(target, "probe.txt"), "ok");
  });
});
