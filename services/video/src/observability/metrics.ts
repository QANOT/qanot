/**
 * Minimal Prometheus exposition.
 *
 * Phase 1 ships only `process_start_time_seconds` and `http_requests_total`.
 * Phase 4 expands to the full §8.1 catalog.
 *
 * Deliberately no prom-client dependency — keeps the dep tree small and the
 * exposition format is text. Counters/gauges are plain Maps.
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

const counters = new Map<string, CounterDefinition>();
const gauges = new Map<string, { help: string; value: number }>();

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
    gauges.set(name, { help, value: 0 });
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

/** Set a gauge value. Auto-registers if missing. */
export function setGauge(name: string, value: number): void {
  let def = gauges.get(name);
  if (!def) {
    def = { help: name, value: 0 };
    gauges.set(name, def);
  }
  def.value = value;
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
    lines.push(`${name} ${def.value}`);
  }

  return `${lines.join("\n")}\n`;
}

/** Test helper: drop all registered metrics. */
export function resetMetricsForTesting(): void {
  counters.clear();
  gauges.clear();
}

function stableKey(labels: Record<string, string>): string {
  const keys = Object.keys(labels).toSorted();
  return keys.map((k) => `${k}=${labels[k]}`).join("|");
}

function formatLabels(labels: Record<string, string>): string {
  const keys = Object.keys(labels).toSorted();
  if (keys.length === 0) return "";
  const parts = keys.map((k) => `${k}="${escapeLabel(labels[k] ?? "")}"`);
  return `{${parts.join(",")}}`;
}

function escapeLabel(v: string): string {
  return v.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/"/g, '\\"');
}

// Pre-register Phase 1 metrics so /metrics output is non-empty on a fresh service.
registerCounter("http_requests_total", "Total HTTP requests received, by route and status.");
