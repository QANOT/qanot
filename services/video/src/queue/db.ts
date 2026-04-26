/**
 * SQLite setup + idempotent schema migration runner.
 *
 * Schema is per docs/video-engine/ARCHITECTURE.md §5.2. Phase 1 lays down the
 * full schema (jobs + quota_ledger + indexes); Phase 2+ populates rows.
 *
 * WAL mode enabled (§9.3). Synchronous=NORMAL is the recommended trade-off
 * for WAL — durable on application crash, may lose last commit on power loss.
 */

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import Database, { type Database as SqliteDb } from "better-sqlite3";

/**
 * Discrete SQL migrations. Each statement is idempotent (CREATE … IF NOT
 * EXISTS) so re-running is a no-op. We do NOT use a migrations table yet
 * because every statement here is self-guarding; once we have schema changes
 * that aren't, we add `schema_migrations`.
 */
export const SCHEMA_STATEMENTS: readonly string[] = [
  `CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    composition_html TEXT NOT NULL,
    format TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    fps INTEGER NOT NULL DEFAULT 30,
    quality TEXT NOT NULL DEFAULT 'standard',
    deadline_seconds INTEGER NOT NULL DEFAULT 120,

    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT,
    progress_percent INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    error_details TEXT,

    output_path TEXT,
    output_size_bytes INTEGER,
    render_duration_ms INTEGER,

    leased_until INTEGER,
    queued_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    expires_at INTEGER NOT NULL
  )`,
  `CREATE INDEX IF NOT EXISTS idx_jobs_status_queued ON jobs(status, queued_at)`,
  `CREATE INDEX IF NOT EXISTS idx_jobs_bot_user_queued ON jobs(bot_id, user_id, queued_at)`,
  `CREATE INDEX IF NOT EXISTS idx_jobs_expires ON jobs(expires_at)`,
  `CREATE TABLE IF NOT EXISTS quota_ledger (
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bucket_day TEXT NOT NULL,
    job_count INTEGER NOT NULL DEFAULT 0,
    cost_usd_micros INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, user_id, bucket_day)
  )`,
] as const;

export interface OpenDbOptions {
  /**
   * If true (default), create parent directories for the DB path. False is
   * useful in tests where the parent is already a tmpdir.
   */
  ensureDir?: boolean;
}

/** Open the database, set pragmas, and run migrations. Returns the handle. */
export function openDatabase(path: string, opts: OpenDbOptions = {}): SqliteDb {
  if (opts.ensureDir !== false && !path.startsWith(":memory:")) {
    mkdirSync(dirname(path), { recursive: true });
  }
  const db = new Database(path);
  // Pragmas first; some require an empty/exclusive db, but our defaults are safe.
  db.pragma("journal_mode = WAL");
  db.pragma("synchronous = NORMAL");
  db.pragma("foreign_keys = ON");
  db.pragma("busy_timeout = 5000");
  runMigrations(db);
  return db;
}

/** Apply schema. Safe to call repeatedly. */
export function runMigrations(db: SqliteDb): void {
  const tx = db.transaction(() => {
    for (const stmt of SCHEMA_STATEMENTS) {
      db.exec(stmt);
    }
  });
  tx();
}

/** Quick liveness probe used by /health. */
export function isHealthy(db: SqliteDb): boolean {
  try {
    const row = db.prepare("SELECT 1 AS ok").get() as { ok?: number } | undefined;
    return row?.ok === 1;
  } catch {
    return false;
  }
}
