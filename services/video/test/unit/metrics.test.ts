/**
 * Phase 4 metrics catalog tests.
 *
 * Asserts the §8.1 catalog is exposed in /metrics format and that the
 * helper APIs (incCounter / observeHistogram / setGauge) populate series
 * the way the worker expects them to.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  incCounter,
  observeHistogram,
  readCounterSum,
  readGauge,
  renderMetrics,
  resetMetricsForTesting,
  setGauge,
} from "../../src/observability/metrics.js";
import { sampleResourceGauges, probeDisk } from "../../src/observability/sampler.js";

let workDir: string;

beforeEach(() => {
  resetMetricsForTesting();
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-metrics-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
  resetMetricsForTesting();
});

describe("counter increments", () => {
  it("records labelled bumps and renders one line per label set", () => {
    incCounter("video_jobs_submitted_total", { bot_id: "a" });
    incCounter("video_jobs_submitted_total", { bot_id: "a" });
    incCounter("video_jobs_submitted_total", { bot_id: "b" });
    incCounter("video_jobs_failed_total", { bot_id: "a", error_code: "lint_failed" });
    incCounter("video_jobs_failed_total", { bot_id: "a", error_code: "internal" });

    expect(readCounterSum("video_jobs_submitted_total")).toBe(3);

    const text = renderMetrics();
    expect(text).toMatch(
      /^video_jobs_submitted_total\{bot_id="a"\} 2$/m,
    );
    expect(text).toMatch(
      /^video_jobs_submitted_total\{bot_id="b"\} 1$/m,
    );
    expect(text).toMatch(
      /^video_jobs_failed_total\{bot_id="a",error_code="lint_failed"\} 1$/m,
    );
  });
});

describe("histogram observations", () => {
  it("records buckets, sum, and count for video_render_duration_seconds", () => {
    observeHistogram("video_render_duration_seconds", 1.5, {
      format: "9:16",
      quality: "standard",
    });
    observeHistogram("video_render_duration_seconds", 9, {
      format: "9:16",
      quality: "standard",
    });
    observeHistogram("video_render_duration_seconds", 41, {
      format: "9:16",
      quality: "standard",
    });

    const text = renderMetrics();
    expect(text).toContain("# TYPE video_render_duration_seconds histogram");
    // 1.5 falls in <=2; 9 in <=10; 41 in <=60. Cumulative:
    //   le=2 -> 1, le=5 -> 1, le=10 -> 2, le=20 -> 2, le=30 -> 2, le=60 -> 3
    expect(text).toMatch(
      /^video_render_duration_seconds_bucket\{format="9:16",quality="standard",le="2.0"\} 1$/m,
    );
    expect(text).toMatch(
      /^video_render_duration_seconds_bucket\{format="9:16",quality="standard",le="10.0"\} 2$/m,
    );
    expect(text).toMatch(
      /^video_render_duration_seconds_bucket\{format="9:16",quality="standard",le="60.0"\} 3$/m,
    );
    expect(text).toMatch(
      /^video_render_duration_seconds_count\{format="9:16",quality="standard"\} 3$/m,
    );
    expect(text).toMatch(
      /^video_render_duration_seconds_sum\{format="9:16",quality="standard"\} 51\.5$/m,
    );
  });
});

describe("gauge sampling", () => {
  it("setGauge + readGauge round-trip per label set", () => {
    setGauge("video_disk_free_bytes", 100, { mount: "/data/video" });
    setGauge("video_disk_free_bytes", 200, { mount: "/other" });
    expect(readGauge("video_disk_free_bytes", { mount: "/data/video" })).toBe(100);
    expect(readGauge("video_disk_free_bytes", { mount: "/other" })).toBe(200);
    expect(readGauge("video_disk_free_bytes", { mount: "/missing" })).toBeNull();
  });

  it("sampleResourceGauges populates RSS + chromium gauges", () => {
    const snap = sampleResourceGauges(workDir);
    expect(snap.rss_bytes).toBeGreaterThan(0);
    expect(readGauge("video_memory_rss_bytes", { component: "render_service" }))
      .toBe(snap.rss_bytes);
    // chromium count must be a non-negative integer
    expect(snap.chromium_processes).toBeGreaterThanOrEqual(0);
    expect(readGauge("video_chromium_processes")).toBe(snap.chromium_processes);

    // Disk probe is platform-dependent. When it does land, the gauges must
    // mirror the snapshot value exactly.
    if (snap.disk) {
      expect(readGauge("video_disk_free_bytes", { mount: snap.disk.mount }))
        .toBe(snap.disk.free_bytes);
      expect(readGauge("video_disk_used_bytes", { mount: snap.disk.mount }))
        .toBe(snap.disk.used_bytes);
    }
  });
});

describe("renderMetrics output", () => {
  it("starts with HELP/TYPE comments and ends with a newline", () => {
    const text = renderMetrics();
    // Must terminate with a single newline so Prometheus scrapers parse the
    // last line cleanly.
    expect(text.endsWith("\n")).toBe(true);
    // Every metric registered by default must surface a TYPE line.
    expect(text).toMatch(/^# TYPE video_jobs_submitted_total counter$/m);
    expect(text).toMatch(/^# TYPE video_jobs_succeeded_total counter$/m);
    expect(text).toMatch(/^# TYPE video_jobs_failed_total counter$/m);
    expect(text).toMatch(/^# TYPE video_jobs_cancelled_total counter$/m);
    expect(text).toMatch(/^# TYPE video_lint_failures_total counter$/m);
    expect(text).toMatch(/^# TYPE video_render_duration_seconds histogram$/m);
    expect(text).toMatch(/^# TYPE video_lint_duration_seconds histogram$/m);
    expect(text).toMatch(/^# TYPE video_total_lifecycle_seconds histogram$/m);
    expect(text).toMatch(/^# TYPE video_queue_depth gauge$/m);
    expect(text).toMatch(/^# TYPE video_worker_busy gauge$/m);
    expect(text).toMatch(/^# TYPE video_disk_used_bytes gauge$/m);
    expect(text).toMatch(/^# TYPE video_disk_free_bytes gauge$/m);
    expect(text).toMatch(/^# TYPE video_memory_rss_bytes gauge$/m);
    expect(text).toMatch(/^# TYPE video_chromium_processes gauge$/m);
  });
});

describe("probeDisk", () => {
  it("returns a sane usage envelope on the test workdir's filesystem", () => {
    const r = probeDisk(workDir);
    if (r === null) {
      // Windows or platform without statfsSync: skip rather than fail.
      return;
    }
    expect(r.total_bytes).toBeGreaterThan(0);
    expect(r.free_bytes).toBeGreaterThanOrEqual(0);
    expect(r.used_bytes).toBeGreaterThanOrEqual(0);
    expect(r.usage_ratio).toBeGreaterThanOrEqual(0);
    expect(r.usage_ratio).toBeLessThanOrEqual(1);
  });
});
