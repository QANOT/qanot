/**
 * `npx hyperframes lint --json` wrapper.
 *
 * Per docs/video-engine/ARCHITECTURE.md §3.5: every job is linted before any
 * Chrome process is spawned. Lint failure -> fast fail with structured
 * `error.details` so the agent can self-correct.
 *
 * Implementation note: the spec text says "pass composition HTML via stdin",
 * but the actual `hyperframes lint` CLI (v0.4.30) only accepts a project
 * directory containing `hyperframes.json` + `index.html`. We materialize a
 * temporary project directory with those two files and lint that. This
 * matches what `hyperframes render` also requires, so it keeps lint and
 * render symmetric, and the temp dir is cheap (two small text files).
 *
 * Exit-code contract from observation against v0.4.30:
 *   - exit 0, JSON `ok: true`  -> composition passed
 *   - exit 0, JSON `ok: false` -> composition has lint findings (NOT a tool
 *     failure -- the tool successfully reported errors)
 *   - non-zero exit              -> the tool itself broke
 *
 * We surface all four shapes in LintResult so the worker can decide.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnWithTimeout } from "./timeout.js";
import type { LintError, LintResult } from "../types.js";

const DEFAULT_LINT_TIMEOUT_MS = 30_000;

/** Minimal hyperframes.json that lint accepts as a valid project. */
const HYPERFRAMES_JSON = JSON.stringify({
  $schema: "https://hyperframes.heygen.com/schema/hyperframes.json",
  registry:
    "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
  paths: {
    blocks: "compositions",
    components: "compositions/components",
    assets: "assets",
  },
});

export interface LintOptions {
  /** Composition HTML to lint (full document). */
  composition_html: string;
  /** Override the spawn timeout. Default 30s. */
  timeout_ms?: number;
  /**
   * Override the binary. Default: `npx`. Tests inject a fake bin (`/bin/echo`,
   * a script that emits JSON, etc.) here.
   */
  bin?: string;
  /**
   * Override the args after the binary. Default: hyperframes lint --json
   * <project_dir>. When `bin` is set in tests, `extraArgsBefore` lets the
   * test add its own positional args (e.g. a script path).
   */
  extraArgsBefore?: readonly string[];
  /**
   * Override what comes after the project dir. Almost always empty; exposed
   * for tests that need to drive the fake bin.
   */
  extraArgsAfter?: readonly string[];
  /** Allow tests to pass a custom temp parent (default os.tmpdir()). */
  workdirRoot?: string;
}

/**
 * Lint a composition. Always resolves; never throws on lint failure or tool
 * failure -- both are captured in the LintResult discriminator.
 */
export async function lintComposition(opts: LintOptions): Promise<LintResult> {
  const start = Date.now();
  const timeout_ms = opts.timeout_ms ?? DEFAULT_LINT_TIMEOUT_MS;
  const root = opts.workdirRoot ?? tmpdir();
  const projectDir = mkdtempSync(join(root, "qanot-lint-"));

  try {
    writeFileSync(join(projectDir, "hyperframes.json"), HYPERFRAMES_JSON);
    writeFileSync(join(projectDir, "index.html"), opts.composition_html);

    // Default to the globally-installed hyperframes binary (pinned at image
    // build time -- see Dockerfile). Falls back to `npx hyperframes` only if
    // a caller explicitly opts in via QANOT_VIDEO_LINT_BIN env. Skipping npx
    // avoids npm's deprecation warnings polluting stdout on the first run
    // (which previously caused JSON.parse to fail and surface a misleading
    // lint_failed result).
    const bin = opts.bin ?? "hyperframes";
    const args =
      opts.bin === undefined
        ? ["lint", "--json", projectDir]
        : [
            ...(opts.extraArgsBefore ?? []),
            ...(opts.extraArgsAfter && opts.extraArgsAfter.length > 0
              ? opts.extraArgsAfter
              : ["lint", "--json", projectDir]),
          ];

    const result = await spawnWithTimeout({
      command: bin,
      args,
      timeoutMs: timeout_ms,
    });

    const duration_ms = Date.now() - start;

    if (result.spawn_failed) {
      return {
        ok: false,
        errors: [],
        reason: "spawn_failed",
        message: result.spawn_error?.message ?? "lint subprocess failed to start",
        duration_ms,
      };
    }

    if (result.timed_out) {
      return {
        ok: false,
        errors: [],
        reason: "timeout",
        message: `lint exceeded ${timeout_ms}ms`,
        duration_ms,
      };
    }

    // Tool itself broken: non-zero exit AND no parseable JSON envelope.
    let parsed: unknown;
    try {
      parsed = JSON.parse(result.stdout);
    } catch {
      return {
        ok: false,
        errors: [],
        reason: "invalid_output",
        message:
          result.stderr.trim().slice(0, 500) ||
          `hyperframes lint produced non-JSON output (exit=${String(result.exit_code)})`,
        duration_ms,
      };
    }

    if (!isLintEnvelope(parsed)) {
      return {
        ok: false,
        errors: [],
        reason: "invalid_output",
        message: "hyperframes lint output missing expected fields",
        duration_ms,
      };
    }

    if (result.exit_code !== 0 && parsed.ok !== false) {
      // Non-zero exit but no errors reported: the tool itself is broken.
      return {
        ok: false,
        errors: [],
        reason: "tool_error",
        message: `hyperframes lint exited ${String(result.exit_code)} without error envelope`,
        duration_ms,
      };
    }

    const findings = (parsed.findings ?? []).map(toLintError);
    const errors = findings.filter((f) => f.severity === "error");
    const warnings = findings.filter((f) => f.severity !== "error");

    if (parsed.ok && errors.length === 0) {
      return { ok: true, warnings, duration_ms };
    }
    return { ok: false, errors, duration_ms };
  } finally {
    rmSync(projectDir, { recursive: true, force: true });
  }
}

interface LintEnvelope {
  ok?: boolean;
  errorCount?: number;
  findings?: ReadonlyArray<unknown>;
}

function isLintEnvelope(v: unknown): v is LintEnvelope {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  // Must have at least one of the canonical fields.
  return "ok" in o || "findings" in o || "errorCount" in o;
}

function toLintError(raw: unknown): LintError {
  const r =
    typeof raw === "object" && raw !== null
      ? (raw as Record<string, unknown>)
      : {};
  const sev = r.severity;
  const severity: LintError["severity"] =
    sev === "error" || sev === "warning" || sev === "info" ? sev : "error";
  return {
    rule: typeof r.code === "string" ? r.code : "unknown",
    severity,
    message: typeof r.message === "string" ? r.message : "",
    element: typeof r.elementId === "string" ? r.elementId : undefined,
    line: typeof r.line === "number" ? r.line : undefined,
    fix_hint: typeof r.fixHint === "string" ? r.fixHint : undefined,
    snippet: typeof r.snippet === "string" ? r.snippet : undefined,
  };
}
