/**
 * Cleanup + backup cron tests.
 *
 * Each test drives the CronManager via the runCleanupOnce / runBackupOnce
 * test seams so we never need to wait for the real setInterval. The clock
 * is injected via nowSeconds so retention math is deterministic.
 */

import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Database as SqliteDb } from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetConfigForTesting } from "../../src/config.js";
import { resetLoggerForTesting } from "../../src/observability/logger.js";
import { CronManager } from "../../src/queue/cron.js";
import { openDatabase } from "../../src/queue/db.js";
import { insertJob, transitionStatus } from "../../src/queue/jobs.js";
import {
  JobStatus,
  RenderQuality,
  VideoFormat,
  type RenderRequest,
} from "../../src/types.js";

let workDir: string;
let outputDir: string;
let dbPath: string;
let backupDir: string;
let db: SqliteDb;
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
  process.env.SERVICE_SECRET = "cron-test-secret-aaaaaaaaaaaaaaaaaaaa";
  process.env.LOG_LEVEL = "silent";
  process.env.NODE_ENV = "test";
  resetConfigForTesting();
  resetLoggerForTesting();

  workDir = mkdtempSync(join(tmpdir(), "qanot-video-cron-"));
  outputDir = join(workDir, "renders");
  dbPath = join(workDir, "jobs.db");
  backupDir = join(workDir, "backups");
  db = openDatabase(dbPath);
});

afterEach(() => {
  db.close();
  rmSync(workDir, { recursive: true, force: true });
  process.env = originalEnv;
  resetConfigForTesting();
  resetLoggerForTesting();
});

function makeCron(opts: { now?: () => number } = {}): CronManager {
  return new CronManager({
    db,
    outputDir,
    dbPath,
    backupDir,
    retentionSeconds: 24 * 60 * 60,
    backupsToKeep: 7,
    nowSeconds: opts.now,
  });
}

function insertSucceededJob(
  reqId: string,
  finishedAt: number,
  outputName?: string,
): { id: string; outputPath: string | null } {
  const { job } = insertJob(db, { ...baseRequest, request_id: reqId });
  // Fast-forward through linting -> rendering -> succeeded.
  transitionStatus(db, job.id, JobStatus.Queued, JobStatus.Linting);
  transitionStatus(db, job.id, JobStatus.Linting, JobStatus.Rendering);
  // Materialize an output file so cleanup has bytes to free.
  let outputPath: string | null = null;
  if (outputName) {
    mkdirSync(outputDir, { recursive: true });
    outputPath = join(outputDir, outputName);
    writeFileSync(outputPath, "x".repeat(2048));
  }
  transitionStatus(db, job.id, JobStatus.Rendering, JobStatus.Succeeded, {
    output_path: outputPath,
    output_size_bytes: 2048,
    finished_at: finishedAt,
  });
  return { id: job.id, outputPath };
}

describe("cleanup", () => {
  it("deletes output files + marks rows expired when finished_at < now-24h", () => {
    const now = 2_000_000_000;
    const oldFinished = now - 25 * 60 * 60; // 25h ago
    const recentFinished = now - 1 * 60 * 60; // 1h ago

    const old = insertSucceededJob("00000000-0000-0000-0000-000000000a01", oldFinished, "old.mp4");
    const recent = insertSucceededJob("00000000-0000-0000-0000-000000000a02", recentFinished, "recent.mp4");

    expect(existsSync(old.outputPath as string)).toBe(true);
    expect(existsSync(recent.outputPath as string)).toBe(true);

    const cron = makeCron({ now: () => now });
    const r = cron.runCleanupOnce();
    expect(r.jobs_cleaned).toBe(1);
    expect(r.bytes_freed).toBe(2048);
    expect(r.delete_errors).toBe(0);

    // Old file gone, row expired.
    expect(existsSync(old.outputPath as string)).toBe(false);
    const oldRow = db.prepare("SELECT status, output_path FROM jobs WHERE id = ?")
      .get(old.id) as { status: string; output_path: string | null };
    expect(oldRow.status).toBe("expired");
    expect(oldRow.output_path).toBeNull();

    // Recent file untouched.
    expect(existsSync(recent.outputPath as string)).toBe(true);
    const recentRow = db.prepare("SELECT status FROM jobs WHERE id = ?")
      .get(recent.id) as { status: string };
    expect(recentRow.status).toBe("succeeded");
  });

  it("is idempotent on already-expired rows (second pass cleans nothing)", () => {
    const now = 2_000_000_000;
    insertSucceededJob("00000000-0000-0000-0000-000000000a03", now - 25 * 60 * 60, "a.mp4");
    const cron = makeCron({ now: () => now });

    const first = cron.runCleanupOnce();
    expect(first.jobs_cleaned).toBe(1);

    const second = cron.runCleanupOnce();
    expect(second.jobs_cleaned).toBe(0);
    expect(second.bytes_freed).toBe(0);
  });

  it("tolerates missing output files (ENOENT)", () => {
    const now = 2_000_000_000;
    // Record path to a file we never create.
    const { id } = insertSucceededJob(
      "00000000-0000-0000-0000-000000000a04",
      now - 25 * 60 * 60,
      "missing.mp4",
    );
    rmSync(join(outputDir, "missing.mp4"), { force: true });
    const cron = makeCron({ now: () => now });
    const r = cron.runCleanupOnce();
    expect(r.jobs_cleaned).toBe(1);
    expect(r.delete_errors).toBe(0);
    const row = db.prepare("SELECT status FROM jobs WHERE id = ?").get(id) as
      | { status: string }
      | undefined;
    expect(row?.status).toBe("expired");
  });
});

describe("backup", () => {
  it("creates a snapshot file with the expected naming + non-empty SHA", () => {
    const cron = makeCron({ now: () => 1_700_000_000 });
    const r = cron.runBackupOnce();
    expect(r.path).not.toBeNull();
    if (r.path) {
      expect(existsSync(r.path)).toBe(true);
      expect(r.path).toMatch(/jobs-\d{8}\.db$/);
      expect(r.size_bytes).toBeGreaterThan(0);
      expect(r.sha256).toMatch(/^[0-9a-f]{64}$/);
    }
  });

  it("keeps the 7 most-recent backups and prunes older ones", () => {
    // Create 9 sequential backups one day apart, all in the same backup dir.
    let pruned = 0;
    for (let i = 0; i < 9; i++) {
      const cron = makeCron({ now: () => 1_700_000_000 + i * 86_400 });
      const r = cron.runBackupOnce();
      pruned += r.pruned;
    }
    const remaining = readdirSync(backupDir).filter((f) => f.endsWith(".db"));
    expect(remaining.length).toBe(7);
    // pruned counts exactly the deletes that happened on days 8 and 9.
    expect(pruned).toBe(2);
  });

  it("listBackups returns newest first", () => {
    for (let i = 0; i < 3; i++) {
      const cron = makeCron({ now: () => 1_700_000_000 + i * 86_400 });
      cron.runBackupOnce();
    }
    const cron = makeCron({ now: () => 1_700_000_000 + 3 * 86_400 });
    const list = cron.listBackups();
    expect(list.length).toBe(3);
    // Newest mtime first.
    for (let i = 1; i < list.length; i++) {
      const prev = list[i - 1]?.mtime ?? 0;
      const cur = list[i]?.mtime ?? 0;
      expect(prev).toBeGreaterThanOrEqual(cur);
    }
  });
});
