/**
 * Disk-full degraded mode tests.
 *
 * Per ARCHITECTURE §9.4: when the OUTPUT_DIR filesystem is >95% used,
 * POST /render returns 503 with retry_after_seconds and the
 * `degraded_disk_full` code in the envelope details.
 *
 * We use the `isDegraded` injection seam to simulate the disk probe rather
 * than actually filling a filesystem. The default probe is exercised in
 * the metrics unit tests where the real statfs result is asserted.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Hono } from "hono";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetConfigForTesting } from "../../src/config.js";
import { resetLoggerForTesting } from "../../src/observability/logger.js";
import { openDatabase } from "../../src/queue/db.js";
import { buildRenderRoutes } from "../../src/routes/render.js";

const VALID_BODY = {
  request_id: "11111111-1111-4111-8111-111111111111",
  bot_id: "topkeydevbot",
  user_id: "u-1",
  composition_html: "<!doctype html><html></html>",
  format: "9:16",
  duration_seconds: 5,
};

let workDir: string;
let originalEnv: NodeJS.ProcessEnv;

beforeEach(() => {
  originalEnv = { ...process.env };
  process.env.SERVICE_SECRET = "degraded-test-secret-aaaaaaaaaaaaaaaaaaaa";
  process.env.LOG_LEVEL = "silent";
  process.env.NODE_ENV = "test";
  resetConfigForTesting();
  resetLoggerForTesting();
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-degraded-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
  process.env = originalEnv;
  resetConfigForTesting();
  resetLoggerForTesting();
});

describe("degraded_disk_full envelope", () => {
  it("returns 503 with retry_after_seconds=300 when disk > 95%", async () => {
    const dbPath = join(workDir, "jobs.db");
    const db = openDatabase(dbPath);
    try {
      const app = new Hono();
      app.route(
        "/",
        buildRenderRoutes({
          db,
          outputDir: workDir,
          isDegraded: () => ({
            code: "degraded_disk_full",
            message: "Output volume /data/video is full (96.4% used).",
            retry_after_seconds: 300,
          }),
        }),
      );

      const res = await app.fetch(
        new Request("http://test.local/render", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            ...VALID_BODY,
            request_id: "11111111-1111-4111-8111-111111111aaa",
          }),
        }),
      );
      expect(res.status).toBe(503);
      expect(res.headers.get("retry-after")).toBe("300");
      const body = (await res.json()) as {
        error: {
          code: string;
          message: string;
          details?: { code: string; retry_after_seconds: number };
        };
      };
      expect(body.error.code).toBe("service_unavailable");
      expect(body.error.details?.code).toBe("degraded_disk_full");
      expect(body.error.details?.retry_after_seconds).toBe(300);
    } finally {
      db.close();
    }
  });

  it("accepts a /render submission when no degraded reason is reported", async () => {
    const dbPath = join(workDir, "jobs.db");
    const db = openDatabase(dbPath);
    try {
      const app = new Hono();
      app.route(
        "/",
        buildRenderRoutes({
          db,
          outputDir: workDir,
          isDegraded: () => null,
        }),
      );

      const res = await app.fetch(
        new Request("http://test.local/render", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            ...VALID_BODY,
            request_id: "22222222-2222-4222-8222-222222222222",
          }),
        }),
      );
      expect(res.status).toBe(202);
      const body = (await res.json()) as { job_id: string; status: string };
      expect(body.status).toBe("queued");
      expect(body.job_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
    } finally {
      db.close();
    }
  });

  it("uses asset_fetch_failed-class code in the closed set on disk-full", async () => {
    // Sanity: the structured details surface a code that the Python tool
    // can match on to drive backoff. The contract is: 503 +
    // body.error.details.code === "degraded_disk_full".
    const dbPath = join(workDir, "jobs.db");
    const db = openDatabase(dbPath);
    try {
      const app = new Hono();
      app.route(
        "/",
        buildRenderRoutes({
          db,
          outputDir: workDir,
          isDegraded: () => ({
            code: "degraded_disk_full",
            message: "no space",
            retry_after_seconds: 300,
          }),
        }),
      );
      const res = await app.fetch(
        new Request("http://test.local/render", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            ...VALID_BODY,
            request_id: "33333333-3333-4333-8333-333333333333",
          }),
        }),
      );
      expect(res.status).toBe(503);
      const body = (await res.json()) as {
        error: { details?: { code: string } };
      };
      expect(body.error.details?.code).toBe("degraded_disk_full");
    } finally {
      db.close();
    }
  });
});
