/**
 * Minimal Prometheus exposition for the qanot-video render service.
 *
 * Phase 4 populates the full §8.1 metric catalog:
 *
 *   Counters
 *     video_jobs_submitted_total{bot_id}
 *     video_jobs_succeeded_total{bot_id}
 *     video_jobs_failed_total{bot_id, error_code}
 *     video_jobs_cancelled_total{bot_id}
 *     video_lint_failures_total{bot_id}
 *
 *   Histograms (seconds; le-bucketed)
 *     video_render_duration_seconds{format,quality}
 *     video_lint_duration_seconds
 *     video_total_lifecycle_seconds{bot_id}
 *
 *   Gauges (sampled on demand by sampleResourceGauges())
 *     video_queue_depth
 *     video_worker_busy
 *     video_disk_used_bytes{mount}
 *     video_disk_free_bytes{mount}
 *     video_memory_rss_bytes{component}
 *     video_chromium_processes
 *
 * Deliberately no prom-client dependency -- keeps the dep tree small and the
 * exposition format is plain text. Counters/gauges/histograms are plain Maps.
 */

interface CounterEntry {
  /** Sorted-key label string used for Prom exposition. */
  labelLine: string;
  value: number;
}

interface CounterDefinition {
  help: string;
  /** Map of label-key (sorted JSON) -> count entry. */
  series: Map<string, CounterEntry>;
}

interface HistogramSeriesEntry {
  labelLine: string;
  /** Pre-formatted label string with sorted keys; used for {le=...} buckets. */
  labelsForBucket: string;
  buckets: number[];
  count: number;
  sum: number;
}

interface HistogramDefinition {
  help: string;
  buckets: readonly number[];
  series: Map<string, HistogramSeriesEntry>;
}

interface GaugeSeriesEntry {
  labelLine: string;
  value: number;
}

interface GaugeDefinition {
  help: string;
  /** Series keyed by sorted-label string; "" key for unlabeled. */
  series: Map<string, GaugeSeriesEntry>;
}

const counters = new Map<string, CounterDefinition>();
const gauges = new Map<string, GaugeDefinition>();
const histograms = new Map<string, HistogramDefinition>();

/**
 * Standard duration buckets per ARCHITECTURE §8.1: render and lifecycle
 * histograms share these so dashboards line up.
 */
const STANDARD_DURATION_BUCKETS_SECONDS = [
  0.5, 1, 2, 5, 10, 20, 30, 60, 120,
] as const;

/** Lint typically completes in <1s; use a tighter low end. */
const LINT_DURATION_BUCKETS_SECONDS = [
  0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30,
] as const;

const processStartTimeSeconds = Date.now() / 1000;

/** Register a counter once. Calling twice with same name is a no-op. */
export function registerCounter(name: string, help: string): void {
  if (!counters.has(name)) {
    counters.set(name, { help, series: new Map() });
  } else {
    // Refresh the help string so re-registration after reset wins.
    const existing = counters.get(name);
    if (existing) existing.help = help;
  }
}

/** Register a gauge once. */
export function registerGauge(name: string, help: string): void {
  if (!gauges.has(name)) {
    gauges.set(name, { help, series: new Map() });
  } else {
    const existing = gauges.get(name);
    if (existing) existing.help = help;
  }
}

/**
 * Register a histogram with explicit bucket boundaries (in seconds when used
 * for durations). Calling twice with the same name is a no-op.
 */
export function registerHistogram(
  name: string,
  help: string,
  buckets: readonly number[] = STANDARD_DURATION_BUCKETS_SECONDS,
): void {
  if (!histograms.has(name)) {
    histograms.set(name, {
      help,
      buckets: [...buckets].toSorted((a, b) => a - b),
      series: new Map(),
    });
  } else {
    const existing = histograms.get(name);
    if (existing) existing.help = help;
  }
}

/** Increment a counter, optionally with labels. Auto-registers if missing. */
export function incCounter(
  name: string,
  labels: Record<string, string> = {},
  by = 1,
): void {
  let def = counters.get(name);
  if (!def) {
    def = { help: name, series: new Map() };
    counters.set(name, def);
  }
  const key = stableKey(labels);
  const existing = def.series.get(key);
  if (existing) {
    existing.value += by;
  } else {
    def.series.set(key, { labelLine: formatLabels(labels), value: by });
  }
}

/** Set a gauge value, optionally with labels. Auto-registers if missing. */
export function setGauge(
  name: string,
  value: number,
  labels: Record<string, string> = {},
): void {
  let def = gauges.get(name);
  if (!def) {
    def = { help: name, series: new Map() };
    gauges.set(name, def);
  }
  const key = stableKey(labels);
  const existing = def.series.get(key);
  if (existing) {
    existing.value = value;
  } else {
    def.series.set(key, { labelLine: formatLabels(labels), value });
  }
}

/**
 * Read a single labelled gauge value. Returns null when the gauge has not been
 * set yet for this label set. Used by /summary to expose disk + worker_busy
 * without re-running the OS probes.
 */
export function readGauge(
  name: string,
  labels: Record<string, string> = {},
): number | null {
  const def = gauges.get(name);
  if (!def) return null;
  const entry = def.series.get(stableKey(labels));
  return entry ? entry.value : null;
}

/**
 * Sum a counter across every label combination (e.g. all bot_ids). Used by
 * /summary to compute "jobs today" totals without re-aggregating from SQLite.
 */
export function readCounterSum(name: string): number {
  const def = counters.get(name);
  if (!def) return 0;
  let total = 0;
  for (const entry of def.series.values()) {
    total += entry.value;
  }
  return total;
}

/**
 * Observe a histogram value (typically duration in seconds). Auto-registers
 * with default duration buckets if the name is unknown.
 */
export function observeHistogram(
  name: string,
  value: number,
  labels: Record<string, string> = {},
): void {
  let def = histograms.get(name);
  if (!def) {
    def = {
      help: name,
      buckets: [...STANDARD_DURATION_BUCKETS_SECONDS],
      series: new Map(),
    };
    histograms.set(name, def);
  }
  const key = stableKey(labels);
  let entry = def.series.get(key);
  if (!entry) {
    entry = {
      labelLine: formatLabels(labels),
      labelsForBucket: formatLabels(labels, true),
      buckets: Array.from({ length: def.buckets.length }, () => 0),
      count: 0,
      sum: 0,
    };
    def.series.set(key, entry);
  }
  entry.count += 1;
  entry.sum += value;
  for (let i = 0; i < def.buckets.length; i++) {
    const upper = def.buckets[i];
    if (upper !== undefined && value <= upper) {
      const cur = entry.buckets[i] ?? 0;
      entry.buckets[i] = cur + 1;
    }
  }
}

/** Build the Prometheus exposition text. */
export function renderMetrics(): string {
  const lines: string[] = [];

  // Process start time is always present.
  lines.push("# HELP process_start_time_seconds Service start time, unix epoch seconds.");
  lines.push("# TYPE process_start_time_seconds gauge");
  lines.push(`process_start_time_seconds ${processStartTimeSeconds}`);

  for (const [name, def] of counters) {
    lines.push(`# HELP ${name} ${def.help}`);
    lines.push(`# TYPE ${name} counter`);
    if (def.series.size === 0) {
      lines.push(`${name} 0`);
    } else {
      for (const entry of def.series.values()) {
        lines.push(`${name}${entry.labelLine} ${entry.value}`);
      }
    }
  }

  for (const [name, def] of gauges) {
    lines.push(`# HELP ${name} ${def.help}`);
    lines.push(`# TYPE ${name} gauge`);
    if (def.series.size === 0) {
      lines.push(`${name} 0`);
    } else {
      for (const entry of def.series.values()) {
        lines.push(`${name}${entry.labelLine} ${entry.value}`);
      }
    }
  }

  for (const [name, def] of histograms) {
    lines.push(`# HELP ${name} ${def.help}`);
    lines.push(`# TYPE ${name} histogram`);
    if (def.series.size === 0) {
      // Emit a zero-count placeholder so scrapers see the metric exists.
      for (const upper of def.buckets) {
        lines.push(`${name}_bucket{le="${formatBucketUpper(upper)}"} 0`);
      }
      lines.push(`${name}_bucket{le="+Inf"} 0`);
      lines.push(`${name}_sum 0`);
      lines.push(`${name}_count 0`);
      continue;
    }
    for (const entry of def.series.values()) {
      for (let i = 0; i < def.buckets.length; i++) {
        const upper = def.buckets[i];
        if (upper === undefined) continue;
        const bucketLabel = withBucketLabel(entry.labelsForBucket, formatBucketUpper(upper));
        lines.push(`${name}_bucket${bucketLabel} ${entry.buckets[i] ?? 0}`);
      }
      const infLabel = withBucketLabel(entry.labelsForBucket, "+Inf");
      lines.push(`${name}_bucket${infLabel} ${entry.count}`);
      lines.push(`${name}_sum${entry.labelLine} ${entry.sum}`);
      lines.push(`${name}_count${entry.labelLine} ${entry.count}`);
    }
  }

  return `${lines.join("\n")}\n`;
}

function formatBucketUpper(upper: number): string {
  // Plain decimal; Prometheus accepts "0.1", "10", etc.
  return Number.isInteger(upper) ? upper.toFixed(1) : String(upper);
}

function withBucketLabel(existing: string, leValue: string): string {
  const lePart = `le="${leValue}"`;
  if (existing.length === 0) return `{${lePart}}`;
  // existing looks like `{a="b",c="d"}`; insert le inside.
  return `${existing.slice(0, -1)},${lePart}}`;
}

/** Test helper: drop all registered metrics. */
export function resetMetricsForTesting(): void {
  counters.clear();
  gauges.clear();
  histograms.clear();
  registerDefaultMetrics();
}

function stableKey(labels: Record<string, string>): string {
  const keys = Object.keys(labels).toSorted();
  return keys.map((k) => `${k}=${labels[k]}`).join("|");
}

function formatLabels(labels: Record<string, string>, forBucket = false): string {
  const keys = Object.keys(labels).toSorted();
  if (keys.length === 0) return forBucket ? "" : "";
  const parts = keys.map((k) => `${k}="${escapeLabel(labels[k] ?? "")}"`);
  return `{${parts.join(",")}}`;
}

function escapeLabel(v: string): string {
  return v.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/"/g, '\\"');
}

function registerDefaultMetrics(): void {
  // HTTP plumbing
  registerCounter(
    "http_requests_total",
    "Total HTTP requests received, by route and status.",
  );

  // §8.1 counters
  registerCounter(
    "video_jobs_submitted_total",
    "Total render jobs accepted (POST /render -> 202 or 200 idempotent).",
  );
  registerCounter(
    "video_jobs_succeeded_total",
    "Total render jobs that finished with status=succeeded.",
  );
  registerCounter(
    "video_jobs_failed_total",
    "Total render jobs that finished with status=failed, by error_code.",
  );
  registerCounter(
    "video_jobs_cancelled_total",
    "Total render jobs cancelled by client (DELETE /jobs/:id).",
  );
  registerCounter(
    "video_lint_failures_total",
    "Total compositions that failed lint (precedes a video_jobs_failed_total bump).",
  );

  // §8.1 histograms
  registerHistogram(
    "video_render_duration_seconds",
    "Render subprocess wall-clock duration in seconds.",
    STANDARD_DURATION_BUCKETS_SECONDS,
  );
  registerHistogram(
    "video_lint_duration_seconds",
    "Lint subprocess wall-clock duration in seconds.",
    LINT_DURATION_BUCKETS_SECONDS,
  );
  registerHistogram(
    "video_total_lifecycle_seconds",
    "End-to-end job lifecycle duration (queued -> terminal) in seconds.",
    STANDARD_DURATION_BUCKETS_SECONDS,
  );

  // §8.1 gauges (populated by sampleResourceGauges + worker hooks)
  registerGauge(
    "video_queue_depth",
    "Current number of jobs in status=queued.",
  );
  registerGauge(
    "video_worker_busy",
    "1 when the worker is currently processing a job, 0 otherwise.",
  );
  registerGauge(
    "video_disk_used_bytes",
    "Bytes used on the OUTPUT_DIR filesystem (mount label set per probe).",
  );
  registerGauge(
    "video_disk_free_bytes",
    "Bytes available on the OUTPUT_DIR filesystem (mount label set per probe).",
  );
  registerGauge(
    "video_memory_rss_bytes",
    "Resident set size (bytes) of the render-service process.",
  );
  registerGauge(
    "video_chromium_processes",
    "Best-effort count of running chromium child processes.",
  );
}

// Pre-register the metrics the service publishes so /metrics is non-empty.
registerDefaultMetrics();
