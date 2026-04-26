/**
 * Job CRUD + state transitions.
 *
 * Phase 1 implements the rows-and-reads needed for tests and the future
 * worker: insert, getById, getByRequestId, listByStatus, transition.
 * leaseNextQueuedJob is provided as a thin stub that the Phase 2 worker will
 * actually use; today it returns null because Phase 1 leaves jobs queued.
 */

import type { Database as SqliteDb } from "better-sqlite3";
import { ulid } from "ulid";
import {
  type Job,
  type JobStatus,
  JobStatus as JobStatusValues,
  type JobErrorCode,
  type JobStage,
  type RenderRequest,
  type VideoFormat,
  type RenderQuality,
  RenderQuality as RenderQualityValues,
} from "../types.js";

const RETENTION_SECONDS = 24 * 60 * 60;

export interface InsertJobParams extends RenderRequest {}

export interface InsertResult {
  job: Job;
  /** True if request_id matched an existing job (idempotent return). */
  duplicate: boolean;
}

/**
 * Insert a job, or return the existing one when request_id is already taken.
 *
 * UNIQUE(request_id) makes idempotency atomic: the INSERT either succeeds (new
 * job) or fails with a constraint violation, and we then SELECT the prior row.
 */
export function insertJob(db: SqliteDb, params: InsertJobParams): InsertResult {
  const existing = getByRequestId(db, params.request_id);
  if (existing) return { job: existing, duplicate: true };

  const id = ulid();
  const nowSeconds = Math.floor(Date.now() / 1000);
  const expiresAt = nowSeconds + RETENTION_SECONDS;
  const fps = params.fps ?? 30;
  const quality: RenderQuality = params.quality ?? RenderQualityValues.Standard;
  const deadline = params.deadline_seconds ?? 120;

  const stmt = db.prepare(`
    INSERT INTO jobs (
      id, request_id, bot_id, user_id, composition_html,
      format, duration_seconds, fps, quality, deadline_seconds,
      status, progress_percent, queued_at, expires_at
    ) VALUES (
      @id, @request_id, @bot_id, @user_id, @composition_html,
      @format, @duration_seconds, @fps, @quality, @deadline_seconds,
      @status, 0, @queued_at, @expires_at
    )
  `);

  try {
    stmt.run({
      id,
      request_id: params.request_id,
      bot_id: params.bot_id,
      user_id: params.user_id,
      composition_html: params.composition_html,
      format: params.format,
      duration_seconds: params.duration_seconds,
      fps,
      quality,
      deadline_seconds: deadline,
      status: JobStatusValues.Queued,
      queued_at: nowSeconds,
      expires_at: expiresAt,
    });
  } catch (err) {
    // Race: another caller inserted the same request_id between the SELECT
    // above and this INSERT. Return their row.
    const racy = getByRequestId(db, params.request_id);
    if (racy) return { job: racy, duplicate: true };
    throw err;
  }

  const inserted = getById(db, id);
  if (!inserted) {
    throw new Error(`insertJob: row vanished after insert (id=${id})`);
  }
  return { job: inserted, duplicate: false };
}

export function getById(db: SqliteDb, id: string): Job | null {
  const row = db.prepare("SELECT * FROM jobs WHERE id = ?").get(id);
  return row ? rowToJob(row) : null;
}

export function getByRequestId(db: SqliteDb, requestId: string): Job | null {
  const row = db.prepare("SELECT * FROM jobs WHERE request_id = ?").get(requestId);
  return row ? rowToJob(row) : null;
}

export function listByStatus(db: SqliteDb, status: JobStatus, limit = 100): Job[] {
  const rows = db
    .prepare("SELECT * FROM jobs WHERE status = ? ORDER BY queued_at ASC LIMIT ?")
    .all(status, limit) as unknown[];
  return rows.map(rowToJob);
}

/**
 * Atomic status transition. Returns true if the row was in `from` and is now
 * in `to`; false if the precondition was not met (no row, or already in
 * another status).
 */
export function transitionStatus(
  db: SqliteDb,
  id: string,
  from: JobStatus,
  to: JobStatus,
  patch: Partial<{
    stage: JobStage | null;
    progress_percent: number;
    error_code: JobErrorCode | null;
    error_message: string | null;
    error_details: string | null;
    output_path: string | null;
    output_size_bytes: number | null;
    render_duration_ms: number | null;
    started_at: number | null;
    finished_at: number | null;
    leased_until: number | null;
  }> = {},
): boolean {
  const setFragments: string[] = ["status = @to"];
  const bind: Record<string, unknown> = { id, from, to };

  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    setFragments.push(`${key} = @${key}`);
    bind[key] = value;
  }

  const sql = `UPDATE jobs SET ${setFragments.join(", ")} WHERE id = @id AND status = @from`;
  const info = db.prepare(sql).run(bind);
  return info.changes === 1;
}

/**
 * Atomically claim the next queued job, transitioning it to `linting` and
 * setting a lease so a crashed worker's job is reclaimable. Returns the
 * leased job, or null if the queue is empty.
 *
 * Per docs/video-engine/ARCHITECTURE.md §3.6 + §9.1: the lease is a
 * timestamp (`leased_until`) so crash recovery is just "any row whose lease
 * has elapsed AND is in a non-terminal in-flight state".
 */
export function leaseNextQueuedJob(
  db: SqliteDb,
  leaseSeconds = 180,
): Job | null {
  const tx = db.transaction((): Job | null => {
    const row = db
      .prepare(
        `SELECT id FROM jobs
           WHERE status = 'queued'
           ORDER BY queued_at ASC
           LIMIT 1`,
      )
      .get() as { id: string } | undefined;
    if (!row) return null;

    const nowSeconds = Math.floor(Date.now() / 1000);
    const leaseUntil = nowSeconds + leaseSeconds;
    const updated = db
      .prepare(
        `UPDATE jobs
           SET status = 'linting',
               stage = 'linting',
               started_at = COALESCE(started_at, @now),
               leased_until = @lease
         WHERE id = @id AND status = 'queued'`,
      )
      .run({ id: row.id, now: nowSeconds, lease: leaseUntil });
    if (updated.changes !== 1) return null;
    return getById(db, row.id);
  });
  return tx();
}

/** Update progress + optional stage in one statement. Best-effort, no error. */
export function updateProgress(
  db: SqliteDb,
  jobId: string,
  percent: number,
  stage?: JobStage,
): void {
  const clamped = Math.min(100, Math.max(0, Math.floor(percent)));
  const setStage = stage ? ", stage = @stage" : "";
  db.prepare(
    `UPDATE jobs
       SET progress_percent = @percent ${setStage}
     WHERE id = @id AND status IN ('linting', 'rendering')`,
  ).run({
    id: jobId,
    percent: clamped,
    ...(stage ? { stage } : {}),
  });
}

/** Push the lease forward; called from the worker heartbeat. */
export function extendLease(
  db: SqliteDb,
  jobId: string,
  leaseSeconds: number,
): void {
  const newDeadline = Math.floor(Date.now() / 1000) + leaseSeconds;
  db.prepare(
    `UPDATE jobs
       SET leased_until = @lease
     WHERE id = @id AND status IN ('linting', 'rendering')`,
  ).run({ id: jobId, lease: newDeadline });
}

/**
 * Crash recovery: any row stuck in linting/rendering past its lease is
 * re-queued so the next worker can pick it up. Idempotent because the
 * renderer writes via tmp+rename -- no partial output ever served.
 */
export function recoverOrphanedJobs(db: SqliteDb): number {
  const nowSeconds = Math.floor(Date.now() / 1000);
  const info = db
    .prepare(
      `UPDATE jobs
         SET status = 'queued',
             stage = NULL,
             progress_percent = 0,
             leased_until = NULL,
             started_at = NULL
       WHERE status IN ('linting', 'rendering')
         AND (leased_until IS NULL OR leased_until < @now)`,
    )
    .run({ now: nowSeconds });
  return info.changes;
}

/** COUNT(*) of queued jobs older-or-equal-than the supplied queued_at. */
export function queuePosition(db: SqliteDb, queuedAt: number): number {
  const row = db
    .prepare(
      `SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND queued_at <= ?`,
    )
    .get(queuedAt) as { n: number };
  return row.n;
}

/** Total queued depth (used by metrics). */
export function queueDepth(db: SqliteDb): number {
  const row = db
    .prepare(`SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued'`)
    .get() as { n: number };
  return row.n;
}

function rowToJob(row: unknown): Job {
  // SQLite returns plain objects; cast and trust the schema.
  const r = row as Record<string, unknown>;
  return {
    id: r.id as string,
    request_id: r.request_id as string,
    bot_id: r.bot_id as string,
    user_id: r.user_id as string,
    composition_html: r.composition_html as string,
    format: r.format as VideoFormat,
    duration_seconds: r.duration_seconds as number,
    fps: r.fps as number,
    quality: r.quality as RenderQuality,
    deadline_seconds: r.deadline_seconds as number,

    status: r.status as JobStatus,
    stage: (r.stage as JobStage | null) ?? null,
    progress_percent: r.progress_percent as number,
    error_code: (r.error_code as JobErrorCode | null) ?? null,
    error_message: (r.error_message as string | null) ?? null,
    error_details: (r.error_details as string | null) ?? null,

    output_path: (r.output_path as string | null) ?? null,
    output_size_bytes: (r.output_size_bytes as number | null) ?? null,
    render_duration_ms: (r.render_duration_ms as number | null) ?? null,

    leased_until: (r.leased_until as number | null) ?? null,
    queued_at: r.queued_at as number,
    started_at: (r.started_at as number | null) ?? null,
    finished_at: (r.finished_at as number | null) ?? null,
    expires_at: r.expires_at as number,
  };
}
