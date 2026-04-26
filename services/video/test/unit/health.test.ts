/**
 * Health route unit test (no auth, no socket).
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { openDatabase } from "../../src/queue/db.js";
import { buildHealthRoutes } from "../../src/routes/health.js";
import type { Database as SqliteDb } from "better-sqlite3";

let workDir: string;
let db: SqliteDb;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-health-"));
  db = openDatabase(join(workDir, "jobs.db"));
});

afterEach(() => {
  db.close();
  rmSync(workDir, { recursive: true, force: true });
});

describe("GET /health", () => {
  it("returns 200 and {ok:true} when the DB responds", async () => {
    const app = buildHealthRoutes({ db });
    const res = await app.fetch(new Request("http://test.local/health"));
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean };
    expect(body.ok).toBe(true);
  });

  it("returns 503 with error envelope when the DB is closed", async () => {
    db.close();
    const app = buildHealthRoutes({ db });
    const res = await app.fetch(new Request("http://test.local/health"));
    expect(res.status).toBe(503);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("unhealthy");

    // Reopen so the afterEach close() doesn't double-close (better-sqlite3
    // throws on a second close, but we swallowed via the closed flag).
    db = openDatabase(join(workDir, "jobs.db"));
  });
});
