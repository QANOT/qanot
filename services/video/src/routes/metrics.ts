/**
 * GET /metrics -- Prometheus exposition.
 *
 * Phase 4: re-samples the OS-probe gauges (disk used/free, RSS, chromium
 * count) on every scrape so dashboards always see fresh values without
 * needing a separate background sampler. Cheap (<5ms in production).
 *
 * Auth-required per §3.4 (the global auth middleware enforces).
 */

import { Hono } from "hono";
import { renderMetrics } from "../observability/metrics.js";
import { sampleResourceGauges } from "../observability/sampler.js";

export interface MetricsRouteDeps {
  /** OUTPUT_DIR; sampled for the disk gauges. */
  outputDir: string;
}

export function buildMetricsRoutes(deps: MetricsRouteDeps): Hono {
  const app = new Hono();

  app.get("/metrics", (c) => {
    // Refresh disk + RSS + chromium gauges so /metrics emits live values.
    try {
      sampleResourceGauges(deps.outputDir);
    } catch {
      // Probe failures are non-fatal -- the metric simply keeps its prior
      // value (or zero if never set).
    }
    const body = renderMetrics();
    return c.text(body, 200, {
      "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
      "Cache-Control": "no-store",
    });
  });

  return app;
}
