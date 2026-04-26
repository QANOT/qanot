/**
 * Worker loop.
 *
 * Phase 1: the loop is real (start/stop/poll), but does no actual rendering.
 * Every tick it asks the DB for a leasable job and gets `null` back; sleeps;
 * loops. Phase 2 will fill in lint + render + state transitions per §3.6.
 */

import type { Database as SqliteDb } from "better-sqlite3";
import type { Logger } from "pino";
import { childLogger } from "../observability/logger.js";
import { leaseNextQueuedJob } from "./jobs.js";

const DEFAULT_POLL_MS = 1000;

export interface WorkerOptions {
  db: SqliteDb;
  /** Override for tests; defaults to 1s per §3.6. */
  pollIntervalMs?: number;
}

export class Worker {
  private readonly db: SqliteDb;
  private readonly pollIntervalMs: number;
  private readonly log: Logger;

  private running = false;
  private loopPromise: Promise<void> | null = null;
  private wakeup: (() => void) | null = null;

  constructor(opts: WorkerOptions) {
    this.db = opts.db;
    this.pollIntervalMs = opts.pollIntervalMs ?? DEFAULT_POLL_MS;
    this.log = childLogger({ component: "worker" });
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.log.info({ poll_interval_ms: this.pollIntervalMs }, "worker starting");
    this.loopPromise = this.loop().catch((err) => {
      this.log.error({ err: serializeError(err) }, "worker loop crashed");
    });
  }

  /** Resolve when the in-flight tick (if any) returns and the loop exits. */
  async stop(): Promise<void> {
    if (!this.running) return;
    this.running = false;
    // Nudge the sleep so we don't wait the full poll interval before exiting.
    this.wakeup?.();
    await this.loopPromise;
    this.loopPromise = null;
    this.log.info("worker stopped");
  }

  isRunning(): boolean {
    return this.running;
  }

  private async loop(): Promise<void> {
    while (this.running) {
      try {
        const job = leaseNextQueuedJob(this.db);
        if (job) {
          // TODO(Phase 2): execute job -- lint, render, transition.
          this.log.warn(
            { job_id: job.id },
            "worker leased a job in Phase 1 -- this should not happen yet",
          );
        }
      } catch (err) {
        this.log.error({ err: serializeError(err) }, "worker tick failed");
      }
      await this.sleep(this.pollIntervalMs);
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

function serializeError(err: unknown): Record<string, unknown> {
  if (err instanceof Error) {
    return { name: err.name, message: err.message, stack: err.stack };
  }
  return { value: String(err) };
}
