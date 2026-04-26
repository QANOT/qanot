/**
 * Database / migration tests.
 *
 * Covers: schema creation, idempotent re-run, indexes exist, WAL mode set,
 * isHealthy returns true for an open db.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { isHealthy, openDatabase, runMigrations } from "../../src/queue/db.js";

interface SqliteMaster {
  name: string;
  type: string;
}

let workDir: string;
let dbPath: string;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-db-"));
  dbPath = join(workDir, "jobs.db");
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
});

describe("openDatabase + runMigrations", () => {
  it("creates the jobs and quota_ledger tables", () => {
    const db = openDatabase(dbPath);
    try {
      const tables = db
        .prepare("SELECT name, type FROM sqlite_master WHERE type = 'table'")
        .all() as SqliteMaster[];
      const tableNames = tables.map((t) => t.name);
      expect(tableNames).toContain("jobs");
      expect(tableNames).toContain("quota_ledger");
    } finally {
      db.close();
    }
  });

  it("creates the expected indexes", () => {
    const db = openDatabase(dbPath);
    try {
      const indexes = db
        .prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'jobs'")
        .all() as { name: string }[];
      const names = indexes.map((i) => i.name);
      expect(names).toContain("idx_jobs_status_queued");
      expect(names).toContain("idx_jobs_bot_user_queued");
      expect(names).toContain("idx_jobs_expires");
    } finally {
      db.close();
    }
  });

  it("is idempotent when the migration runs twice on the same DB", () => {
    const db = openDatabase(dbPath);
    try {
      // First open already migrated. Run again -- must not throw.
      runMigrations(db);
      runMigrations(db);
      const count = db
        .prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='jobs'")
        .get() as { n: number };
      expect(count.n).toBe(1);
    } finally {
      db.close();
    }
  });

  it("is idempotent across multiple openDatabase calls on the same path", () => {
    const db1 = openDatabase(dbPath);
    db1.close();
    // Reopen -- the file exists, schema present, no errors.
    const db2 = openDatabase(dbPath);
    try {
      const row = db2.prepare("SELECT COUNT(*) AS n FROM jobs").get() as { n: number };
      expect(row.n).toBe(0);
    } finally {
      db2.close();
    }
  });

  it("enables WAL journal mode", () => {
    const db = openDatabase(dbPath);
    try {
      const mode = db.pragma("journal_mode", { simple: true }) as string;
      expect(mode.toLowerCase()).toBe("wal");
    } finally {
      db.close();
    }
  });
});

describe("isHealthy", () => {
  it("returns true for an open, migrated DB", () => {
    const db = openDatabase(dbPath);
    try {
      expect(isHealthy(db)).toBe(true);
    } finally {
      db.close();
    }
  });
});
