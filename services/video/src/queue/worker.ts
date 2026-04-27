/**
 * Render worker loop.
 *
 * Phase 2: implements the state machine in §3.6.
 *
 *   queued -> linting -> rendering -> succeeded
 *                    \-> failed (lint_failed)
 *                              \-> failed (render_timeout|chrome_crash|asset_fetch_failed|internal)
 *
 * Concurrency:
 *   - The HTTP server and the worker live in the same Node.js process. The
 *     worker holds an in-memory `cancelRequests: Set<jobId>` that the
 *     DELETE /jobs/:id route writes into; before/during a render we check
 *     the set and abort the subprocess via AbortSignal.
 *   - The lease (`leased_until`) is bumped every 30s while a render runs so
 *     a long render is not mistaken for a crashed worker. On worker startup
 *     we call recoverOrphanedJobs() to re-queue anything actually stuck.
 *
 * Output safety:
 *   - The render wrapper writes `<job_id>.tmp.mp4` and only renames to
 *     `<job_id>.mp4` on a successful exit, so a crashed worker never
 *     publishes a partial file.
 */

import { mkdirSync } from "node:fs";
import type { Database as SqliteDb } from "better-sqlite3";
import type { Logger } from "pino";
import { childLogger } from "../observability/logger.js";
import {
  incCounter,
  observeHistogram,
  setGauge,
} from "../observability/metrics.js";
import {
  checkCompositionAssets,
  type AssetGuardOptions,
  type AssetGuardResult,
} from "../render/asset_guard.js";
import { lintComposition } from "../render/lint.js";
import { renderComposition } from "../render/render.js";
import {
  type Job,
  JobErrorCode,
  JobStage,
  JobStatus,
  type LintError,
  type LintResult,
  type RenderResult,
} from "../types.js";
import {
  extendLease,
  leaseNextQueuedJob,
  recoverOrphanedJobs,
  transitionStatus,
  updateProgress,
  queueDepth,
} from "./jobs.js";

const DEFAULT_POLL_MS = 1000;
const DEFAULT_LEASE_SECONDS = 180;
const DEFAULT_HEARTBEAT_MS = 30_000;

export interface WorkerOptions {
  db: SqliteDb;
  /** Where rendered outputs are atomically published. */
  outputDir: string;
  /** Override for tests; defaults to 1s per §3.6. */
  pollIntervalMs?: number;
  /** Override for tests; defaults to 180s. */
  leaseSeconds?: number;
  /** Override for tests; defaults to 30s. */
  heartbeatMs?: number;
  /** Allow tests to inject fake lint/render. */
  lintFn?: typeof lintComposition;
  renderFn?: typeof renderComposition;
  /**
   * Override the asset-URL allowlist check. Tests inject a stub that
   * always passes, or one that returns a deterministic failure. Production
   * uses the default `checkCompositionAssets`.
   */
  assetGuardFn?: (
    html: string,
    opts?: AssetGuardOptions,
  ) => Promise<AssetGuardResult>;
  /** Forwarded into the asset-guard call (e.g. resolver, file roots). */
  assetGuardOptions?: AssetGuardOptions;
}

export class Worker {
  private readonly db: SqliteDb;
  private readonly outputDir: string;
  private readonly pollIntervalMs: number;
  private readonly leaseSeconds: number;
  private readonly heartbeatMs: number;
  private readonly lintFn: typeof lintComposition;
  private readonly renderFn: typeof renderComposition;
  private readonly assetGuardFn: NonNullable<WorkerOptions["assetGuardFn"]>;
  private readonly assetGuardOptions: AssetGuardOptions | undefined;
  private readonly log: Logger;

  private running = false;
  private loopPromise: Promise<void> | null = null;
  private wakeup: (() => void) | null = null;

  /** job_id -> AbortController used to terminate an in-flight render. */
  private readonly cancelControllers = new Map<string, AbortController>();
  /** job_id of the job currently being processed, if any. */
  private currentJobId: string | null = null;

  constructor(opts: WorkerOptions) {
    this.db = opts.db;
    this.outputDir = opts.outputDir;
    this.pollIntervalMs = opts.pollIntervalMs ?? DEFAULT_POLL_MS;
    this.leaseSeconds = opts.leaseSeconds ?? DEFAULT_LEASE_SECONDS;
    this.heartbeatMs = opts.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
    this.lintFn = opts.lintFn ?? lintComposition;
    this.renderFn = opts.renderFn ?? renderComposition;
    this.assetGuardFn = opts.assetGuardFn ?? checkCompositionAssets;
    this.assetGuardOptions = opts.assetGuardOptions;
    this.log = childLogger({ component: "worker" });
    mkdirSync(this.outputDir, { recursive: true });
  }

  start(): void {
    if (this.running) return;
    this.running = true;

    // Crash recovery on startup -- requeue any jobs stuck in linting/rendering.
    try {
      const recovered = recoverOrphanedJobs(this.db);
      if (recovered > 0) {
        this.log.warn({ recovered_count: recovered }, "requeued orphaned jobs");
      }
    } catch (err) {
      this.log.error(
        { err: serializeError(err) },
        "crash recovery sweep failed",
      );
    }

    setGauge("video_worker_busy", 0);
    setGauge("video_queue_depth", safeQueueDepth(this.db));
    this.log.info({ poll_interval_ms: this.pollIntervalMs }, "worker starting");
    this.loopPromise = this.loop().catch((err) => {
      this.log.error({ err: serializeError(err) }, "worker loop crashed");
    });
  }

  /** Resolve when the in-flight tick (if any) returns and the loop exits. */
  async stop(): Promise<void> {
    if (!this.running) return;
    this.running = false;
    // Abort any in-flight render so the subprocess wraps up promptly.
    for (const controller of this.cancelControllers.values()) {
      controller.abort();
    }
    this.wakeup?.();
    await this.loopPromise;
    this.loopPromise = null;
    this.log.info("worker stopped");
  }

  isRunning(): boolean {
    return this.running;
  }

  /** Public hook used by DELETE /jobs/:id to cancel an in-flight render. */
  requestCancel(jobId: string): boolean {
    const controller = this.cancelControllers.get(jobId);
    if (!controller) return false;
    controller.abort();
    return true;
  }

  /** Public hook to peek at the currently processing job (used by tests). */
  getCurrentJobId(): string | null {
    return this.currentJobId;
  }

  private async loop(): Promise<void> {
    while (this.running) {
      let processed = false;
      try {
        const job = leaseNextQueuedJob(this.db, this.leaseSeconds);
        if (job) {
          processed = true;
          await this.processJob(job);
        }
      } catch (err) {
        this.log.error({ err: serializeError(err) }, "worker tick failed");
      }
      try {
        setGauge("video_queue_depth", safeQueueDepth(this.db));
      } catch {
        /* metrics are best-effort */
      }
      if (!processed) {
        await this.sleep(this.pollIntervalMs);
      }
    }
  }

  private async processJob(job: Job): Promise<void> {
    const log = this.log.child({
      job_id: job.id,
      bot_id: job.bot_id,
      user_id: job.user_id,
      request_id: job.request_id,
    });
    log.info({ status: "linting" }, "job picked");
    setGauge("video_worker_busy", 1);
    this.currentJobId = job.id;
    const cancelController = new AbortController();
    this.cancelControllers.set(job.id, cancelController);

    let heartbeat: NodeJS.Timeout | null = null;
    const startHeartbeat = (): void => {
      if (heartbeat) return;
      heartbeat = setInterval(() => {
        try {
          extendLease(this.db, job.id, this.leaseSeconds);
        } catch (err) {
          log.warn({ err: serializeError(err) }, "lease extension failed");
        }
      }, this.heartbeatMs);
      heartbeat.unref?.();
    };
    const stopHeartbeat = (): void => {
      if (heartbeat) {
        clearInterval(heartbeat);
        heartbeat = null;
      }
    };

    try {
      startHeartbeat();

      // -------- Asset URL allowlist (Phase 4 §6.3 + §7.4) -------- //
      // Runs BEFORE the hyperframes lint so a malicious composition cannot
      // escape into Chromium even if lint somehow let it through.
      let assetVerdict: AssetGuardResult;
      try {
        assetVerdict = await this.assetGuardFn(
          job.composition_html,
          this.assetGuardOptions,
        );
      } catch (err) {
        log.error({ err: serializeError(err) }, "asset_guard threw");
        this.failJob(job, JobStatus.Linting, JobErrorCode.AssetFetchFailed, {
          message: "Asset URL allowlist check failed unexpectedly",
          details: err instanceof Error ? err.message : String(err),
        });
        return;
      }
      if (!assetVerdict.ok) {
        log.warn(
          {
            reason: assetVerdict.reason,
            offending_url: assetVerdict.offending_url,
          },
          "asset_guard rejected composition",
        );
        this.failJob(
          job,
          JobStatus.Linting,
          JobErrorCode.AssetFetchFailed,
          {
            message: assetVerdict.message,
            details: {
              reason: assetVerdict.reason,
              offending_url: assetVerdict.offending_url ?? null,
            },
          },
        );
        return;
      }
      if (cancelController.signal.aborted) {
        this.cancelJob(job, JobStatus.Linting);
        return;
      }

      // -------- Lint -------- //
      let lintResult: LintResult;
      try {
        lintResult = await this.lintFn({
          composition_html: job.composition_html,
        });
      } catch (err) {
        log.error({ err: serializeError(err) }, "lint subprocess threw");
        this.failJob(job, JobStatus.Linting, JobErrorCode.LintFailed, {
          message: "Lint subprocess threw an exception",
          details: err instanceof Error ? err.message : String(err),
        });
        return;
      }
      observeHistogram(
        "video_lint_duration_seconds",
        lintResult.duration_ms / 1000,
      );

      if (cancelController.signal.aborted) {
        this.cancelJob(job, JobStatus.Linting);
        return;
      }

      if (!lintResult.ok) {
        const errors = lintResult.errors;
        log.warn(
          { error_count: errors.length, reason: lintResult.reason },
          "lint failed",
        );
        incCounter("video_lint_failures_total", { bot_id: job.bot_id });
        this.failJob(job, JobStatus.Linting, JobErrorCode.LintFailed, {
          message: lintResult.message ?? buildLintMessage(errors),
          details: errors,
        });
        return;
      }

      // -------- Rendering -------- //
      const advanced = transitionStatus(
        this.db,
        job.id,
        JobStatus.Linting,
        JobStatus.Rendering,
        {
          stage: JobStage.RenderingFrames,
          progress_percent: 0,
        },
      );
      if (!advanced) {
        // Probably cancelled or torn down between lease and now.
        log.warn("could not transition linting -> rendering");
        return;
      }

      let renderResult: RenderResult;
      try {
        renderResult = await this.renderFn({
          job_id: job.id,
          composition_html: job.composition_html,
          output_dir: this.outputDir,
          fps: job.fps,
          quality: job.quality,
          deadline_seconds: job.deadline_seconds,
          cancel_signal: cancelController.signal,
          on_progress: ({ percent, stage }) => {
            try {
              updateProgress(this.db, job.id, percent, stage);
            } catch {
              /* metrics best-effort */
            }
          },
        });
      } catch (err) {
        log.error({ err: serializeError(err) }, "render subprocess threw");
        this.failJob(job, JobStatus.Rendering, JobErrorCode.Internal, {
          message: "Render subprocess threw an exception",
          details: err instanceof Error ? err.message : String(err),
        });
        return;
      }

      if (renderResult.ok) {
        observeHistogram(
          "video_render_duration_seconds",
          renderResult.render_duration_ms / 1000,
          { format: job.format, quality: job.quality },
        );
        const finishedAt = Math.floor(Date.now() / 1000);
        const transitioned = transitionStatus(
          this.db,
          job.id,
          JobStatus.Rendering,
          JobStatus.Succeeded,
          {
            stage: JobStage.Succeeded,
            progress_percent: 100,
            output_path: renderResult.output_path,
            output_size_bytes: renderResult.output_size_bytes,
            render_duration_ms: renderResult.render_duration_ms,
            finished_at: finishedAt,
            leased_until: null,
          },
        );
        if (transitioned) {
          incCounter("video_jobs_succeeded_total", { bot_id: job.bot_id });
          observeHistogram(
            "video_total_lifecycle_seconds",
            Math.max(0, finishedAt - job.queued_at),
            { bot_id: job.bot_id },
          );
          log.info(
            {
              output_size_bytes: renderResult.output_size_bytes,
              render_duration_ms: renderResult.render_duration_ms,
            },
            "render succeeded",
          );
        } else {
          log.warn(
            "render succeeded but transition to succeeded was rejected (probably cancelled mid-flight)",
          );
        }
        return;
      }

      // Render failed.
      if (cancelController.signal.aborted) {
        this.cancelJob(job, JobStatus.Rendering);
        return;
      }
      log.warn(
        {
          error_code: renderResult.code,
          stderr_tail: renderResult.stderr_tail.slice(-500),
        },
        "render failed",
      );
      this.failJob(job, JobStatus.Rendering, renderResult.code, {
        message: renderResult.message,
        details: renderResult.stderr_tail || null,
      });
    } finally {
      stopHeartbeat();
      this.cancelControllers.delete(job.id);
      this.currentJobId = null;
      setGauge("video_worker_busy", 0);
    }
  }

  private failJob(
    job: Job,
    fromStatus: JobStatus,
    code: JobErrorCode,
    err: { message: string; details: unknown },
  ): void {
    const finishedAt = Math.floor(Date.now() / 1000);
    const ok = transitionStatus(this.db, job.id, fromStatus, JobStatus.Failed, {
      stage: JobStage.Failed,
      error_code: code,
      error_message: err.message,
      error_details:
        err.details === null || err.details === undefined
          ? null
          : typeof err.details === "string"
            ? err.details
            : JSON.stringify(err.details),
      finished_at: finishedAt,
      leased_until: null,
    });
    if (ok) {
      incCounter("video_jobs_failed_total", {
        bot_id: job.bot_id,
        error_code: code,
      });
      observeHistogram(
        "video_total_lifecycle_seconds",
        Math.max(0, finishedAt - job.queued_at),
        { bot_id: job.bot_id },
      );
    }
  }

  private cancelJob(job: Job, fromStatus: JobStatus): void {
    const finishedAt = Math.floor(Date.now() / 1000);
    const ok = transitionStatus(
      this.db,
      job.id,
      fromStatus,
      JobStatus.Cancelled,
      {
        stage: JobStage.Cancelled,
        finished_at: finishedAt,
        leased_until: null,
      },
    );
    if (ok) {
      incCounter("video_jobs_cancelled_total", { bot_id: job.bot_id });
      observeHistogram(
        "video_total_lifecycle_seconds",
        Math.max(0, finishedAt - job.queued_at),
        { bot_id: job.bot_id },
      );
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.wakeup = null;
        resolve();
      }, ms);
      this.wakeup = () => {
        clearTimeout(timer);
        this.wakeup = null;
        resolve();
      };
    });
  }
}

function safeQueueDepth(db: SqliteDb): number {
  try {
    return queueDepth(db);
  } catch {
    return 0;
  }
}

function buildLintMessage(errors: ReadonlyArray<LintError>): string {
  if (errors.length === 0) return "Composition lint failed";
  const first = errors[0];
  if (!first) return "Composition lint failed";
  const more = errors.length > 1 ? ` (+${errors.length - 1} more)` : "";
  return `Composition lint failed: ${first.message}${more}`;
}

function serializeError(err: unknown): Record<string, unknown> {
  if (err instanceof Error) {
    return { name: err.name, message: err.message, stack: err.stack };
  }
  return { value: String(err) };
}
