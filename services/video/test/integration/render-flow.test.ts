/**
 * End-to-end HTTP integration for the Phase 2 routes.
 *
 * Boots the real server (Hono + @hono/node-server) on a free port and
 * exercises POST /render, GET /jobs/:id, DELETE /jobs/:id via fetch().
 *
 * The worker IS running, so submitted jobs would normally try to spawn
 * `npx hyperframes`. To avoid that without disabling the worker, we use a
 * very short poll interval and rely on either:
 *   (a) for queued -> cancelled, beating the worker via DELETE before lease
 *   (b) for the rest, asserting the API surface alone (no need for a real
 *       render to assert idempotency / 413 / 503).
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetConfigForTesting } from "../../src/config.js";
import { resetLoggerForTesting } from "../../src/observability/logger.js";
import { startServer, type RunningServer } from "../../src/server.js";

const SECRET = "render-flow-test-secret-aaaaaaaaaaaaaaaaaa";
const VALID_HTML = `<!doctype html>
<html><head><meta charset="UTF-8"></head>
<body><div id="root" data-composition-id="main" data-start="0" data-duration="2"
  data-width="1080" data-height="1920">hi</div>
<script>window.__timelines={main:null};</script></body></html>`;

let workDir: string;
let running: RunningServer | null = null;
let originalEnv: NodeJS.ProcessEnv;

function uuid(): string {
  // RFC 4122 v4 via crypto.randomUUID; available since Node 14.17.
  return crypto.randomUUID();
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return {
    authorization: `Bearer ${SECRET}`,
    "content-type": "application/json",
    ...extra,
  };
}

beforeEach(() => {
  originalEnv = { ...process.env };
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-flow-"));
  process.env.HOST = "127.0.0.1";
  process.env.PORT = "0";
  process.env.SERVICE_SECRET = SECRET;
  process.env.DB_PATH = join(workDir, "jobs.db");
  process.env.OUTPUT_DIR = join(workDir, "renders");
  process.env.LOG_LEVEL = "silent";
  process.env.NODE_ENV = "test";
  resetConfigForTesting();
  resetLoggerForTesting();
});

afterEach(async () => {
  if (running) {
    await running.close();
    running = null;
  }
  rmSync(workDir, { recursive: true, force: true });
  process.env = originalEnv;
  resetConfigForTesting();
  resetLoggerForTesting();
});

describe("POST /render", () => {
  it("returns 202 with job_id, status=queued, queue_position on first submission", async () => {
    running = await startServer({ port: 0 });
    const res = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: VALID_HTML,
        format: "9:16",
        duration_seconds: 2,
      }),
    });
    expect(res.status).toBe(202);
    const body = (await res.json()) as {
      job_id: string;
      status: string;
      queue_position: number;
      estimated_start_seconds: number;
    };
    expect(body.job_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
    expect(body.status).toBe("queued");
    expect(body.queue_position).toBeGreaterThanOrEqual(1);
    expect(body.estimated_start_seconds).toBeGreaterThanOrEqual(25);
  });

  it("is idempotent on request_id (returns same job_id with status 200)", async () => {
    running = await startServer({ port: 0 });
    const reqId = uuid();
    const submit = async (): Promise<Response> =>
      fetch(`http://127.0.0.1:${running?.port ?? 0}/render`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          request_id: reqId,
          bot_id: "topkeydevbot",
          user_id: "u-1",
          composition_html: VALID_HTML,
          format: "9:16",
          duration_seconds: 2,
        }),
      });
    const first = await submit();
    expect(first.status).toBe(202);
    const firstBody = (await first.json()) as { job_id: string };

    const second = await submit();
    expect(second.status).toBe(200);
    const secondBody = (await second.json()) as { job_id: string };
    expect(secondBody.job_id).toBe(firstBody.job_id);
  });

  it("returns 413 when composition_html exceeds 256 KB", async () => {
    running = await startServer({ port: 0 });
    const big = "a".repeat(300_000);
    const res = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: big,
        format: "9:16",
        duration_seconds: 5,
      }),
    });
    expect(res.status).toBe(413);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("payload_too_large");
  });

  it("returns 400 with validation_failed for an out-of-range duration", async () => {
    running = await startServer({ port: 0 });
    const res = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: VALID_HTML,
        format: "9:16",
        duration_seconds: 9999,
      }),
    });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("validation_failed");
  });
});

describe("GET /jobs/:id", () => {
  it("returns 404 for an unknown job id", async () => {
    running = await startServer({ port: 0 });
    const res = await fetch(`http://127.0.0.1:${running.port}/jobs/no-such-job`, {
      headers: { authorization: `Bearer ${SECRET}` },
    });
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("not_found");
  });

  it("returns the full status payload after a render is enqueued", async () => {
    running = await startServer({ port: 0 });
    const submit = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: VALID_HTML,
        format: "9:16",
        duration_seconds: 2,
      }),
    });
    const { job_id } = (await submit.json()) as { job_id: string };

    const res = await fetch(`http://127.0.0.1:${running.port}/jobs/${job_id}`, {
      headers: { authorization: `Bearer ${SECRET}` },
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      job_id: string;
      status: string;
      progress_percent: number;
      queued_at: string;
      expires_at: string;
    };
    expect(body.job_id).toBe(job_id);
    expect(["queued", "linting", "rendering", "succeeded", "failed"]).toContain(
      body.status,
    );
    expect(body.queued_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(body.expires_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});

describe("DELETE /jobs/:id", () => {
  it("transitions a queued job to cancelled and returns 200", async () => {
    running = await startServer({ port: 0 });
    const submit = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: VALID_HTML,
        format: "9:16",
        duration_seconds: 2,
      }),
    });
    const { job_id } = (await submit.json()) as { job_id: string };

    // Race condition note: the worker polls every 1s; DELETE should land
    // before the worker leases. If the worker did already lease, our route
    // returns 200 with a "cancellation requested" note, which is also valid.
    const del = await fetch(`http://127.0.0.1:${running.port}/jobs/${job_id}`, {
      method: "DELETE",
      headers: { authorization: `Bearer ${SECRET}` },
    });
    expect(del.status).toBe(200);
    const body = (await del.json()) as {
      ok: boolean;
      status: string;
      note?: string;
    };
    expect(body.ok).toBe(true);
    expect(["queued", "linting", "rendering", "cancelled"]).toContain(body.status);
  });

  it("is idempotent: cancelling an already-cancelled job returns 200", async () => {
    running = await startServer({ port: 0 });
    const submit = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: VALID_HTML,
        format: "9:16",
        duration_seconds: 2,
      }),
    });
    const { job_id } = (await submit.json()) as { job_id: string };

    const first = await fetch(`http://127.0.0.1:${running.port}/jobs/${job_id}`, {
      method: "DELETE",
      headers: { authorization: `Bearer ${SECRET}` },
    });
    expect(first.status).toBe(200);

    // Wait a moment in case the first cancel was on a queued job (quick
    // transition) so we have a chance to hit the "already cancelled" path.
    await new Promise((r) => setTimeout(r, 30));
    const second = await fetch(`http://127.0.0.1:${running.port}/jobs/${job_id}`, {
      method: "DELETE",
      headers: { authorization: `Bearer ${SECRET}` },
    });
    // 200 in either case (already-cancelled idempotency or signal to a still-
    // running worker).
    expect(second.status).toBe(200);
  });
});

describe("POST /render degraded mode", () => {
  it("returns 503 with retry-after header when isDegraded probe trips", async () => {
    // For this we drive the route directly (without the full server stack)
    // because the degraded probe is a route-level concern.
    const { Hono } = await import("hono");
    const { buildRenderRoutes } = await import("../../src/routes/render.js");
    const { openDatabase } = await import("../../src/queue/db.js");
    const dbPath = join(workDir, "degraded.db");
    const db = openDatabase(dbPath);
    try {
      const app = new Hono();
      app.route(
        "/",
        buildRenderRoutes({
          db,
          isDegraded: () => ({
            code: "degraded_disk_full",
            message: "Simulated disk-full degraded mode.",
            retry_after_seconds: 60,
          }),
        }),
      );
      const res = await app.fetch(
        new Request("http://test.local/render", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            request_id: uuid(),
            bot_id: "b",
            user_id: "u",
            composition_html: VALID_HTML,
            format: "9:16",
            duration_seconds: 2,
          }),
        }),
      );
      expect(res.status).toBe(503);
      expect(res.headers.get("retry-after")).toBe("60");
      const body = (await res.json()) as {
        error: { code: string; details?: { code: string } };
      };
      expect(body.error.code).toBe("service_unavailable");
      expect(body.error.details?.code).toBe("degraded_disk_full");
    } finally {
      db.close();
    }
  });
});

describe("GET /jobs/:id/output", () => {
  it("returns 404 when the job is not yet succeeded", async () => {
    running = await startServer({ port: 0 });
    const submit = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        request_id: uuid(),
        bot_id: "topkeydevbot",
        user_id: "u-1",
        composition_html: VALID_HTML,
        format: "9:16",
        duration_seconds: 2,
      }),
    });
    const { job_id } = (await submit.json()) as { job_id: string };
    const res = await fetch(
      `http://127.0.0.1:${running.port}/jobs/${job_id}/output`,
      { headers: { authorization: `Bearer ${SECRET}` } },
    );
    expect([404, 410]).toContain(res.status);
  });
});
