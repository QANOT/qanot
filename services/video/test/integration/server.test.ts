/**
 * Integration tests: real HTTP socket bound to 127.0.0.1 on a free port.
 *
 * Server runs on Node (Hono + @hono/node-server) per the spec runtime choice
 * (Node 22 LTS + better-sqlite3, see ARCHITECTURE.md §3.2).
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resetConfigForTesting } from "../../src/config.js";
import { resetLoggerForTesting } from "../../src/observability/logger.js";
import { startServer, type RunningServer } from "../../src/server.js";

const SECRET = "integration-test-secret-aaaaaaaaaaaaaaaaaaaa";

let workDir: string;
let running: RunningServer | null = null;
let originalEnv: NodeJS.ProcessEnv;

beforeEach(() => {
  originalEnv = { ...process.env };
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-int-"));
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

describe("server lifecycle", () => {
  it("starts on a free port and answers GET /health without auth", async () => {
    running = await startServer({ port: 0 });
    expect(running.host).toBe("127.0.0.1");
    expect(running.port).toBeGreaterThan(0);

    const res = await fetch(`http://127.0.0.1:${running.port}/health`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean };
    expect(body.ok).toBe(true);
  });

  it("returns 401 on /render without auth and 501 with auth (Phase 2 stub)", async () => {
    running = await startServer({ port: 0 });

    const noAuth = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    expect(noAuth.status).toBe(401);
    const noAuthBody = (await noAuth.json()) as { error: { code: string } };
    expect(noAuthBody.error.code).toBe("unauthorized");

    const withAuth = await fetch(`http://127.0.0.1:${running.port}/render`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${SECRET}`,
      },
      body: "{}",
    });
    expect(withAuth.status).toBe(501);
    const withAuthBody = (await withAuth.json()) as { error: { code: string } };
    expect(withAuthBody.error.code).toBe("not_implemented");
  });

  it("propagates X-Request-ID when client supplies one", async () => {
    running = await startServer({ port: 0 });
    const supplied = "01HTESTREQUESTID1234567890";
    const res = await fetch(`http://127.0.0.1:${running.port}/health`, {
      headers: { "x-request-id": supplied },
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("x-request-id")).toBe(supplied);
  });

  it("returns 401 on /metrics without auth (auth-required per spec)", async () => {
    running = await startServer({ port: 0 });
    const res = await fetch(`http://127.0.0.1:${running.port}/metrics`);
    expect(res.status).toBe(401);
  });

  it("serves /metrics in Prometheus text format with auth", async () => {
    running = await startServer({ port: 0 });
    const res = await fetch(`http://127.0.0.1:${running.port}/metrics`, {
      headers: { authorization: `Bearer ${SECRET}` },
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/plain");
    const text = await res.text();
    expect(text).toContain("process_start_time_seconds");
    expect(text).toContain("# TYPE");
  });
});
