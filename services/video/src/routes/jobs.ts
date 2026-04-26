/**
 * /jobs/* -- Phase 1 stubs.
 *
 * Phase 2 fills these in:
 *   GET    /jobs/:id          -> status payload
 *   GET    /jobs/:id/output   -> stream MP4
 *   DELETE /jobs/:id          -> cancel
 *
 * For now every endpoint returns 501.
 */

import { Hono } from "hono";
import type { ErrorEnvelope } from "../types.js";

function notImplemented(verb: string, path: string): ErrorEnvelope {
  return {
    error: {
      code: "not_implemented",
      message: `${verb} ${path} is not implemented yet. Scheduled for Phase 2.`,
    },
  };
}

export function buildJobsRoutes(): Hono {
  const app = new Hono();

  app.get("/jobs/:id", (c) => {
    return c.json(notImplemented("GET", "/jobs/:id"), 501);
  });

  app.get("/jobs/:id/output", (c) => {
    return c.json(notImplemented("GET", "/jobs/:id/output"), 501);
  });

  app.delete("/jobs/:id", (c) => {
    return c.json(notImplemented("DELETE", "/jobs/:id"), 501);
  });

  return app;
}
