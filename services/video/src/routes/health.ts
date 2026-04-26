/**
 * GET /health -- public, no auth.
 *
 * Per §3.4: returns 200 with {ok: true} when SQLite responds. Used by Docker
 * HEALTHCHECK and any external liveness probe.
 *
 * If the DB query fails we return 503 with the error envelope so Docker can
 * restart the container.
 */

import { Hono } from "hono";
import type { Database as SqliteDb } from "better-sqlite3";
import { isHealthy } from "../queue/db.js";
import type { ErrorEnvelope } from "../types.js";

export interface HealthDeps {
  db: SqliteDb;
}

export function buildHealthRoutes(deps: HealthDeps): Hono {
  const app = new Hono();

  app.get("/health", (c) => {
    if (!isHealthy(deps.db)) {
      const body: ErrorEnvelope = {
        error: { code: "unhealthy", message: "Database not responsive." },
      };
      return c.json(body, 503);
    }
    return c.json({ ok: true });
  });

  return app;
}
