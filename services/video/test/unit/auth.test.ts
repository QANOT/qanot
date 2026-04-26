/**
 * Auth middleware unit tests.
 *
 * Covers: valid bearer passes, missing header rejects, wrong token rejects,
 * /health bypasses, malformed header rejects, empty token rejects, and the
 * constant-time-compare helper handles unequal lengths without throwing.
 */

import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import { constantTimeEqual, serviceKeyAuth } from "../../src/auth/service-key.js";

const SECRET = "test-secret-must-be-long-enough-1234567890";

function buildApp(): Hono {
  const app = new Hono();
  app.use("*", serviceKeyAuth({ secret: SECRET }));
  app.get("/health", (c) => c.json({ ok: true }));
  app.get("/protected", (c) => c.json({ ok: true, you: "are in" }));
  return app;
}

describe("serviceKeyAuth middleware", () => {
  it("passes when Authorization: Bearer <secret> matches", async () => {
    const app = buildApp();
    const res = await app.fetch(
      new Request("http://test.local/protected", {
        headers: { authorization: `Bearer ${SECRET}` },
      }),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean };
    expect(body.ok).toBe(true);
  });

  it("rejects 401 when Authorization header is missing", async () => {
    const app = buildApp();
    const res = await app.fetch(new Request("http://test.local/protected"));
    expect(res.status).toBe(401);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("unauthorized");
  });

  it("rejects 401 when bearer token is wrong", async () => {
    const app = buildApp();
    const res = await app.fetch(
      new Request("http://test.local/protected", {
        headers: { authorization: "Bearer not-the-real-secret" },
      }),
    );
    expect(res.status).toBe(401);
  });

  it("allows /health without any Authorization header", async () => {
    const app = buildApp();
    const res = await app.fetch(new Request("http://test.local/health"));
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean };
    expect(body.ok).toBe(true);
  });

  it("rejects 401 on malformed Authorization header (no 'Bearer ' prefix)", async () => {
    const app = buildApp();
    const res = await app.fetch(
      new Request("http://test.local/protected", {
        headers: { authorization: SECRET },
      }),
    );
    expect(res.status).toBe(401);
  });

  it("rejects 401 on empty bearer token", async () => {
    const app = buildApp();
    const res = await app.fetch(
      new Request("http://test.local/protected", {
        headers: { authorization: "Bearer " },
      }),
    );
    expect(res.status).toBe(401);
  });
});

describe("constantTimeEqual", () => {
  it("returns true for identical strings", () => {
    expect(constantTimeEqual("abc123", "abc123")).toBe(true);
  });

  it("returns false for different equal-length strings", () => {
    expect(constantTimeEqual("abc123", "xyz123")).toBe(false);
  });

  it("returns false (without throwing) for strings of different lengths", () => {
    expect(constantTimeEqual("short", "muchlonger-string-here")).toBe(false);
    expect(constantTimeEqual("", "nonempty")).toBe(false);
  });
});
