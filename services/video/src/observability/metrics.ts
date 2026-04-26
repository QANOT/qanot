/**
 * Minimal Prometheus exposition.
 *
 * Phase 2 wires up the per-job counters/histograms (the §8.1 subset that the
 * rendering pipeline can populate today). The gauges that need OS probes
 * (disk_used_bytes, memory_rss_bytes) are still TODO until Phase 4.
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

const DEFAULT_DURATION_BUCKETS_SECONDS = [
  0.1, 0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180,
] as const;

const processStartTimeSeconds = Date.now() / 1000;

/** Register a counter once. Calling twice with same name is a no-op. */
export function registerCounter(name: string, help: string): void {
  if (!counters.has(name)) {
    counters.set(name, { help, series: new Map() });
  }
}

/** Register a gauge once. */
export function registerGauge(name: string, help: string): void {
  if (!gauges.has(name)) {
    gauges.set(name, { help, series: new Map() });
  }
}

/**
 * Register a histogram with explicit bucket boundaries (in seconds when used
 * for durations). Calling twice with the same name is a no-op.
 */
export function registerHistogram(
  name: string,
  help: string,
  buckets: readonly number[] = DEFAULT_DURATION_BUCKETS_SECONDS,
): void {
  if (!histograms.has(name)) {
    histograms.set(name, {
      help,
      buckets: [...buckets].toSorted((a, b) => a - b),
      series: new Map(),
    });
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
      buckets: [...DEFAULT_DURATION_BUCKETS_SECONDS],
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
  registerCounter(
    "http_requests_total",
    "Total HTTP requests received, by route and status.",
  );
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
  registerHistogram(
    "video_render_duration_seconds",
    "Render subprocess wall-clock duration in seconds.",
  );
  registerHistogram(
    "video_lint_duration_seconds",
    "Lint subprocess wall-clock duration in seconds.",
    [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
  );
  registerGauge(
    "video_queue_depth",
    "Current number of jobs in status=queued.",
  );
  registerGauge(
    "video_worker_busy",
    "1 when the worker is currently processing a job, 0 otherwise.",
  );
}

// Pre-register the metrics the service publishes so /metrics is non-empty.
registerDefaultMetrics();
