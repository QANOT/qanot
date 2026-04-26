/**
 * Unit tests for the hyperframes lint wrapper.
 *
 * Strategy: instead of mocking child_process.spawn (which would couple us to
 * implementation details), we point the wrapper at a fake binary -- a small
 * Node script under tests/_fixtures/lint_fake/ -- that emits deterministic
 * stdout/stderr/exit-code. This exercises the real spawn ladder + JSON parser
 * end-to-end. Tests pass `bin: "node"` plus the script path so the wrapper
 * never invokes npx.
 */

import { mkdtempSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { lintComposition } from "../../src/render/lint.js";

let workDir: string;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-lint-tests-"));
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
});

/**
 * Materialize a Node script that emits the given stdout/stderr/exit, then
 * return the args to pass to `lintComposition` so it executes that script
 * via `node <script>`.
 */
function fakeBin(opts: {
  stdout?: string;
  stderr?: string;
  exit?: number;
  /** Sleep this many ms before exiting (used to trigger our 30s timeout). */
  delayMs?: number;
}): { bin: string; extraArgsBefore: string[] } {
  const script = join(workDir, `fake-${Math.random().toString(36).slice(2)}.cjs`);
  const body = `
const stdout = ${JSON.stringify(opts.stdout ?? "")};
const stderr = ${JSON.stringify(opts.stderr ?? "")};
const delay = ${String(opts.delayMs ?? 0)};
const exit = ${String(opts.exit ?? 0)};
if (stdout) process.stdout.write(stdout);
if (stderr) process.stderr.write(stderr);
setTimeout(() => process.exit(exit), delay);
`;
  writeFileSync(script, body);
  chmodSync(script, 0o755);
  return { bin: process.execPath, extraArgsBefore: [script] };
}

describe("lintComposition", () => {
  it("returns ok when lint exits 0 with {ok:true}", async () => {
    const { bin, extraArgsBefore } = fakeBin({
      stdout: JSON.stringify({ ok: true, errorCount: 0, findings: [] }),
      exit: 0,
    });
    const r = await lintComposition({
      composition_html: "<!doctype html><html></html>",
      bin,
      extraArgsBefore,
      workdirRoot: workDir,
    });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.warnings).toEqual([]);
      expect(r.duration_ms).toBeGreaterThanOrEqual(0);
    }
  });

  it("returns ok=false with parsed errors when lint reports findings", async () => {
    const { bin, extraArgsBefore } = fakeBin({
      stdout: JSON.stringify({
        ok: false,
        errorCount: 2,
        findings: [
          {
            code: "root_missing_dimensions",
            severity: "error",
            message: "Root composition is missing data-width.",
            elementId: "root",
            fixHint: "Set numeric data-width on root.",
            snippet: '<div id="root">',
          },
          {
            code: "missing_timeline_registry",
            severity: "error",
            message: "Missing window.__timelines registration.",
          },
        ],
      }),
      exit: 0,
    });
    const r = await lintComposition({
      composition_html: "<!doctype html><html></html>",
      bin,
      extraArgsBefore,
      workdirRoot: workDir,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.errors).toHaveLength(2);
      expect(r.errors[0]?.rule).toBe("root_missing_dimensions");
      expect(r.errors[0]?.element).toBe("root");
      expect(r.errors[0]?.fix_hint).toBe("Set numeric data-width on root.");
      expect(r.errors[1]?.rule).toBe("missing_timeline_registry");
    }
  });

  it("returns reason=timeout when the subprocess exceeds the deadline", async () => {
    const { bin, extraArgsBefore } = fakeBin({
      stdout: JSON.stringify({ ok: true, findings: [] }),
      delayMs: 5_000,
      exit: 0,
    });
    const r = await lintComposition({
      composition_html: "<!doctype html><html></html>",
      bin,
      extraArgsBefore,
      workdirRoot: workDir,
      timeout_ms: 100,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("timeout");
      expect(r.errors).toEqual([]);
    }
  });

  it("returns reason=spawn_failed when the binary does not exist (ENOENT)", async () => {
    const r = await lintComposition({
      composition_html: "<!doctype html><html></html>",
      bin: "/nonexistent/binary/qanot-lint-zzz",
      extraArgsBefore: [],
      workdirRoot: workDir,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("spawn_failed");
      expect(r.message).toBeTruthy();
    }
  });

  it("returns reason=invalid_output when the binary emits non-JSON", async () => {
    const { bin, extraArgsBefore } = fakeBin({
      stdout: "this is not JSON, just text",
      exit: 0,
    });
    const r = await lintComposition({
      composition_html: "<!doctype html><html></html>",
      bin,
      extraArgsBefore,
      workdirRoot: workDir,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("invalid_output");
    }
  });

  it("returns reason=tool_error when the binary exits non-zero with a structured envelope but ok!=false", async () => {
    const { bin, extraArgsBefore } = fakeBin({
      stdout: JSON.stringify({ ok: true, findings: [] }),
      stderr: "internal panic",
      exit: 2,
    });
    const r = await lintComposition({
      composition_html: "<!doctype html><html></html>",
      bin,
      extraArgsBefore,
      workdirRoot: workDir,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.reason).toBe("tool_error");
    }
  });
});
