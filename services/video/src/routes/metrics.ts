/**
 * GET /metrics -- Prometheus exposition.
 *
 * Phase 1 returns a minimal payload (process_start_time + http_requests_total).
 * Phase 4 will populate the full §8.1 catalog.
 *
 * Auth-required per §3.4 (the global auth middleware enforces).
 */

import { Hono } from "hono";
import { renderMetrics } from "../observability/metrics.js";

export function buildMetricsRoutes(): Hono {
  const app = new Hono();

  app.get("/metrics", (c) => {
    const body = renderMetrics();
    return c.text(body, 200, {
      "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
      "Cache-Control": "no-store",
    });
  });

  return app;
}
