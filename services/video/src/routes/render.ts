/**
 * POST /render -- Phase 1 stub.
 *
 * Returns 501 until Phase 2 wires up validation, idempotent insert, and
 * worker dispatch (see docs/video-engine/ARCHITECTURE.md §3.4 + §14.1).
 */

import { Hono } from "hono";
import type { ErrorEnvelope } from "../types.js";

export function buildRenderRoutes(): Hono {
  const app = new Hono();

  app.post("/render", (c) => {
    const body: ErrorEnvelope = {
      error: {
        code: "not_implemented",
        message:
          "POST /render is not implemented yet. Scheduled for Phase 2 (render integration).",
      },
    };
    return c.json(body, 501);
  });

  return app;
}
