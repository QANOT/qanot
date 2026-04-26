/**
 * Service-to-service Bearer auth.
 *
 * Per §7.1: token comes from env (SERVICE_SECRET); client sends
 * `Authorization: Bearer <token>`. Comparison is constant-time to avoid
 * timing oracles. /health bypasses (per spec).
 */

import { timingSafeEqual } from "node:crypto";
import type { Context, MiddlewareHandler } from "hono";
import { loadConfig } from "../config.js";
import type { ErrorEnvelope } from "../types.js";

const PUBLIC_PATHS = new Set(["/health"]);

/**
 * Constant-time string compare. Both inputs must be UTF-8 strings.
 *
 * If lengths differ we still call timingSafeEqual on equal-length buffers to
 * avoid leaking the secret length, then return false.
 */
export function constantTimeEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, "utf8");
  const bBuf = Buffer.from(b, "utf8");

  // Always perform a fixed-size compare so the work done does not depend on
  // whether the lengths matched.
  const len = Math.max(aBuf.length, bBuf.length, 1);
  const aPadded = Buffer.alloc(len);
  const bPadded = Buffer.alloc(len);
  aBuf.copy(aPadded);
  bBuf.copy(bPadded);

  const equal = timingSafeEqual(aPadded, bPadded);
  return equal && aBuf.length === bBuf.length;
}

function unauthorized(c: Context, message: string): Response {
  const body: ErrorEnvelope = {
    error: { code: "unauthorized", message },
  };
  return c.json(body, 401);
}

/**
 * Hono middleware factory. Pass an explicit secret in tests; production uses
 * loadConfig().SERVICE_SECRET.
 */
export function serviceKeyAuth(opts?: { secret?: string }): MiddlewareHandler {
  return async (c, next) => {
    if (PUBLIC_PATHS.has(c.req.path)) {
      return next();
    }

    const expected = opts?.secret ?? loadConfig().SERVICE_SECRET;
    const header = c.req.header("authorization") ?? "";
    const prefix = "Bearer ";
    if (!header.startsWith(prefix)) {
      return unauthorized(c, "Missing or malformed Authorization header.");
    }
    const provided = header.slice(prefix.length).trim();
    if (provided.length === 0) {
      return unauthorized(c, "Empty bearer token.");
    }
    if (!constantTimeEqual(provided, expected)) {
      return unauthorized(c, "Invalid service key.");
    }
    return next();
  };
}
