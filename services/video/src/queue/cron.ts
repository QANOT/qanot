/**
 * Cleanup + backup cron jobs.
 *
 * Per docs/video-engine/ARCHITECTURE.md §9.3 + §9.4:
 *
 *   Cleanup (hourly):
 *     - Delete output_path files for jobs in terminal status (succeeded |
 *       failed | cancelled) whose finished_at is older than 24h.
 *     - Mark those rows status='expired' in one transaction.
 *     - Log count cleaned + bytes freed.
 *
 *   Backup (daily):
 *     - SQLite VACUUM INTO '/data/video/backups/jobs-YYYYMMDD.db'.
 *     - Keep the 7 most-recent backups; older ones are deleted.
 *     - Log file path + size.
 *
 * Both methods are exposed for tests so they can be triggered synchronously
 * without waiting for the actual setInterval. The CronManager class wires
 * them into a single start/stop lifecycle that the worker controls.
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  unlinkSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import type { Database as SqliteDb } from "better-sqlite3";
import type { Logger } from "pino";
import { childLogger } from "../observability/logger.js";
import { JobStatus } from "../types.js";

const DEFAULT_RETENTION_SECONDS = 24 * 60 * 60;
const DEFAULT_CLEANUP_INTERVAL_MS = 60 * 60 * 1000; // 1 hour
const DEFAULT_BACKUP_INTERVAL_MS = 24 * 60 * 60 * 1000; // 24 hours
const DEFAULT_BACKUPS_TO_KEEP = 7;
const TERMINAL_STATUSES: ReadonlyArray<string> = [
  JobStatus.Succeeded,
  JobStatus.Failed,
  JobStatus.Cancelled,
];

export interface CleanupResult {
  jobs_cleaned: number;
  bytes_freed: number;
  /** Files we tried to delete but couldn't (kept count for logging). */
  delete_errors: number;
}

export interface BackupResult {
  /** Absolute path of the freshly-written backup, or null on failure. */
  path: string | null;
  size_bytes: number;
  /** SHA-256 of the on-disk backup. Empty when path is null. */
  sha256: string;
  /** How many older backups were pruned to maintain the keep window. */
  pruned: number;
}

export interface CronManagerOptions {
  db: SqliteDb;
  /** Output directory; cleanup walks job rows + unlinks output_path files. */
  outputDir: string;
  /** Path to the SQLite db file (used to derive the backup directory). */
  dbPath: string;
  /** Override the backup directory. Default: <dbDir>/backups. */
  backupDir?: string;
  /** Override retention window. Default: 24h. */
  retentionSeconds?: number;
  /** Override cleanup interval. Default: 1h. */
  cleanupIntervalMs?: number;
  /** Override backup interval. Default: 24h. */
  backupIntervalMs?: number;
  /** Override how many backups to keep. Default: 7. */
  backupsToKeep?: number;
  /** Time provider (used by tests). Returns Unix epoch seconds. */
  nowSeconds?: () => number;
  /** Logger override. */
  logger?: Logger;
}

/**
 * One owner for both cron jobs. Lifecycle:
 *
 *   const cron = new CronManager({...});
 *   cron.start();   // begins the setIntervals
 *   ...
 *   await cron.stop(); // clears intervals, awaits any in-flight tick
 *
 * Tests bypass the timers entirely and call runCleanupOnce / runBackupOnce.
 */
export class CronManager {
  private readonly db: SqliteDb;
  private readonly backupDir: string;
  private readonly retentionSeconds: number;
  private readonly cleanupIntervalMs: number;
  private readonly backupIntervalMs: number;
  private readonly backupsToKeep: number;
  private readonly now: () => number;
  private readonly log: Logger;

  private cleanupTimer: NodeJS.Timeout | null = null;
  private backupTimer: NodeJS.Timeout | null = null;
  private inflight: Promise<void> | null = null;

  constructor(opts: CronManagerOptions) {
    this.db = opts.db;
    this.backupDir = opts.backupDir ?? join(dirname(opts.dbPath), "backups");
    this.retentionSeconds = opts.retentionSeconds ?? DEFAULT_RETENTION_SECONDS;
    this.cleanupIntervalMs = opts.cleanupIntervalMs ?? DEFAULT_CLEANUP_INTERVAL_MS;
    this.backupIntervalMs = opts.backupIntervalMs ?? DEFAULT_BACKUP_INTERVAL_MS;
    this.backupsToKeep = opts.backupsToKeep ?? DEFAULT_BACKUPS_TO_KEEP;
    this.now = opts.nowSeconds ?? (() => Math.floor(Date.now() / 1000));
    this.log = opts.logger ?? childLogger({ component: "cron" });
  }

  /** Start the periodic timers. Both ticks run a small amount on start. */
  start(): void {
    if (this.cleanupTimer || this.backupTimer) return;
    this.cleanupTimer = setInterval(() => {
      this.tickCleanup();
    }, this.cleanupIntervalMs);
    this.cleanupTimer.unref?.();
    this.backupTimer = setInterval(() => {
      this.tickBackup();
    }, this.backupIntervalMs);
    this.backupTimer.unref?.();
    this.log.info(
      {
        cleanup_interval_ms: this.cleanupIntervalMs,
        backup_interval_ms: this.backupIntervalMs,
      },
      "cron started",
    );
  }

  /** Stop both timers and wait for any in-flight tick to finish. */
  async stop(): Promise<void> {
    if (this.cleanupTimer) {
      clearInterval(this.cleanupTimer);
      this.cleanupTimer = null;
    }
    if (this.backupTimer) {
      clearInterval(this.backupTimer);
      this.backupTimer = null;
    }
    if (this.inflight) {
      await this.inflight;
    }
    this.log.info("cron stopped");
  }

  /** Test seam: run one cleanup pass synchronously and return its result. */
  runCleanupOnce(): CleanupResult {
    return this.cleanupExpired();
  }

  /** Test seam: run one backup pass synchronously and return its result. */
  runBackupOnce(): BackupResult {
    return this.backupDatabase();
  }

  /** Test/Phase-4 dashboard hook: list backups newest-first. */
  listBackups(): Array<{ path: string; size_bytes: number; mtime: number }> {
    if (!existsSync(this.backupDir)) return [];
    const entries = readdirSync(this.backupDir);
    const out: Array<{ path: string; size_bytes: number; mtime: number }> = [];
    for (const e of entries) {
      if (!e.startsWith("jobs-") || !e.endsWith(".db")) continue;
      const full = join(this.backupDir, e);
      try {
        const st = statSync(full);
        out.push({
          path: full,
          size_bytes: st.size,
          mtime: Math.floor(st.mtimeMs / 1000),
        });
      } catch {
        // ignore -- file vanished mid-list
      }
    }
    out.sort((a, b) => b.mtime - a.mtime);
    return out;
  }

  // ---- internal --------------------------------------------------------

  private tickCleanup(): void {
    const promise = (async () => {
      try {
        const r = this.cleanupExpired();
        this.log.info(
          {
            jobs_cleaned: r.jobs_cleaned,
            bytes_freed: r.bytes_freed,
            delete_errors: r.delete_errors,
          },
          "cleanup tick complete",
        );
      } catch (err) {
        this.log.error(
          { err: err instanceof Error ? err.message : String(err) },
          "cleanup tick failed",
        );
      }
    })();
    this.inflight = promise;
    promise.finally(() => {
      if (this.inflight === promise) this.inflight = null;
    });
  }

  private tickBackup(): void {
    const promise = (async () => {
      try {
        const r = this.backupDatabase();
        if (r.path) {
          this.log.info(
            { path: r.path, size_bytes: r.size_bytes, sha256: r.sha256, pruned: r.pruned },
            "backup tick complete",
          );
        }
      } catch (err) {
        this.log.error(
          { err: err instanceof Error ? err.message : String(err) },
          "backup tick failed",
        );
      }
    })();
    this.inflight = promise;
    promise.finally(() => {
      if (this.inflight === promise) this.inflight = null;
    });
  }

  private cleanupExpired(): CleanupResult {
    const cutoff = this.now() - this.retentionSeconds;
    const placeholders = TERMINAL_STATUSES.map(() => "?").join(",");
    const rows = this.db
      .prepare(
        `SELECT id, output_path FROM jobs
           WHERE status IN (${placeholders})
             AND finished_at IS NOT NULL
             AND finished_at < ?`,
      )
      .all(...TERMINAL_STATUSES, cutoff) as Array<{
      id: string;
      output_path: string | null;
    }>;

    let bytesFreed = 0;
    let deleteErrors = 0;
    const idsToExpire: string[] = [];

    for (const r of rows) {
      idsToExpire.push(r.id);
      const p = r.output_path;
      if (!p) continue;
      try {
        const st = statSync(p);
        bytesFreed += st.size;
      } catch {
        // File already gone: nothing to add to the freed total.
      }
      try {
        unlinkSync(p);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        // ENOENT is fine -- file may have been cleaned already.
        if (!/ENOENT/i.test(msg)) {
          deleteErrors += 1;
        }
      }
    }

    if (idsToExpire.length > 0) {
      const tx = this.db.transaction((ids: string[]) => {
        const stmt = this.db.prepare(
          `UPDATE jobs SET status='expired', stage='expired',
                          output_path = NULL
             WHERE id = ? AND status IN (${placeholders})`,
        );
        for (const id of ids) {
          stmt.run(id, ...TERMINAL_STATUSES);
        }
      });
      tx(idsToExpire);
    }

    return {
      jobs_cleaned: idsToExpire.length,
      bytes_freed: bytesFreed,
      delete_errors: deleteErrors,
    };
  }

  private backupDatabase(): BackupResult {
    mkdirSync(this.backupDir, { recursive: true });
    const stamp = formatDateUtc(new Date(this.now() * 1000));
    const dest = join(this.backupDir, `jobs-${stamp}.db`);

    // VACUUM INTO needs the file to NOT exist (SQLite refuses an existing
    // path). If we already ran today, overwrite by removing the prior file
    // first -- callers want the latest snapshot of the day.
    if (existsSync(dest)) {
      try {
        unlinkSync(dest);
      } catch {
        // Best-effort; if we can't remove, VACUUM INTO will fail and we
        // surface the failure below.
      }
    }

    try {
      // VACUUM INTO is a single atomic statement on its own connection;
      // better-sqlite3 prepares it like any other DDL.
      this.db.exec(`VACUUM INTO '${dest.replace(/'/g, "''")}'`);
    } catch (err) {
      this.log.warn(
        { err: err instanceof Error ? err.message : String(err) },
        "backup VACUUM INTO failed",
      );
      return { path: null, size_bytes: 0, sha256: "", pruned: 0 };
    }

    let sizeBytes = 0;
    try {
      sizeBytes = statSync(dest).size;
    } catch {
      // Race: backup file vanished between VACUUM and stat.
      return { path: null, size_bytes: 0, sha256: "", pruned: 0 };
    }

    const sha256 = sha256Sync(dest);
    const pruned = this.pruneOldBackups();
    return { path: dest, size_bytes: sizeBytes, sha256, pruned };
  }

  private pruneOldBackups(): number {
    const all = this.listBackups();
    if (all.length <= this.backupsToKeep) return 0;
    const toRemove = all.slice(this.backupsToKeep);
    let pruned = 0;
    for (const b of toRemove) {
      try {
        rmSync(b.path, { force: true });
        pruned += 1;
      } catch {
        // Skip; surface as a metric later if it becomes a hot path.
      }
    }
    return pruned;
  }
}

function formatDateUtc(d: Date): string {
  const y = d.getUTCFullYear().toString().padStart(4, "0");
  const m = (d.getUTCMonth() + 1).toString().padStart(2, "0");
  const day = d.getUTCDate().toString().padStart(2, "0");
  return `${y}${m}${day}`;
}

function sha256Sync(path: string): string {
  // readFileSync is acceptable here -- backup files are SQLite snapshots
  // (sub-megabyte) and the cron runs once a day in the background.
  const buf = readFileSync(path);
  return createHash("sha256").update(buf).digest("hex");
}
