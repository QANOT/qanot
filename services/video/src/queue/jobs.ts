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
 * Phase 2 worker hook. Today it does nothing: there is no actual rendering,
 * so we never lease. Implemented as a function so the worker loop is real
 * code (not a TODO comment) and Phase 2 can drop in a real query.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function leaseNextQueuedJob(_db: SqliteDb, _leaseSeconds = 180): Job | null {
  // TODO(Phase 2): atomic SELECT … UPDATE leased_until = now()+lease_seconds
  // returning the row, restricted to status='queued' and ordered by queued_at.
  return null;
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
