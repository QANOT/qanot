/**
 * `npx hyperframes render` wrapper.
 *
 * Per docs/video-engine/ARCHITECTURE.md §3.6: a single render subprocess per
 * job. Progress is streamed from the child's stderr/stdout, parsed line by
 * line, and surfaced via a callback so the worker can update SQLite in real
 * time.
 *
 * Atomic output write (§5.1, §9.1): the renderer writes to
 * `<job_id>.tmp.mp4`, and on a successful exit code we rename to
 * `<job_id>.mp4`. Readers therefore never see a partial file. On any
 * failure, the .tmp.mp4 (which may or may not exist) is removed.
 *
 * Error classification (§3.4 closed set):
 *   - exit 0                                                  -> ok
 *   - non-zero + stderr matches /chromium|page crashed|       -> chrome_crash
 *     target closed/i
 *   - non-zero + stderr matches /asset|fetch|404|net::|       -> asset_fetch_failed
 *     ENOTFOUND|ERR_/i with no chromium signature
 *   - timed_out                                               -> render_timeout
 *   - everything else                                         -> internal
 *
 * Progress regex: terminal output looks like `█░░ 35% Capturing frame ...`.
 * We strip ANSI escape sequences and match the first NN% on each line.
 */

import {
  mkdtempSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnWithTimeout } from "./timeout.js";
import { JobErrorCode, JobStage } from "../types.js";
import type {
  RenderProgressCallback,
  RenderResult,
  RenderQuality,
} from "../types.js";

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

// Strips CSI sequences (ESC [ <params> <final-byte>) emitted by progress
// bars. We build the regex via new RegExp from String.fromCharCode so the
// source stays ASCII-clean and oxlint's no-control-regex rule does not flag
// the literal escape byte.
const ANSI_ESCAPE_RE = new RegExp(
  `${String.fromCharCode(0x1B)}\\[[0-?]*[ -/]*[@-~]`,
  "g",
);
const PERCENT_RE = /(\d{1,3})\s*%/;
const STAGE_PATTERNS: ReadonlyArray<{ re: RegExp; stage: JobStage }> = [
  { re: /Capturing frame|Extracting video frames/i, stage: JobStage.RenderingFrames },
  { re: /Encoding video|Assembling final video/i, stage: JobStage.EncodingVideo },
];

const STDERR_TAIL_BYTES = 4_000;
const CHROME_CRASH_RE = /chromium|page crashed|target closed|chrome.*crash/i;
const ASSET_RE = /asset|fetch|net::|ENOTFOUND|ERR_NAME|ERR_CONNECTION|404\s/i;

export interface RenderOptions {
  job_id: string;
  composition_html: string;
  /** Output directory; the final `<job_id>.mp4` is written here. */
  output_dir: string;
  fps: number;
  quality: RenderQuality;
  /** Hard deadline. The kill ladder fires at `deadline_seconds * 1000` ms. */
  deadline_seconds: number;
  /** Optional progress hook. */
  on_progress?: RenderProgressCallback;
  /** External cancel signal (DELETE /jobs/:id wires this up). */
  cancel_signal?: AbortSignal;
  /** Override the binary; tests inject a fake. Default: `npx`. */
  bin?: string;
  /**
   * When `bin` is provided, replaces the entire arg list. The output_dir
   * file write contract still applies -- the test's fake bin is responsible
   * for creating the .tmp.mp4 if it wants the success path tested.
   */
  args_override?: readonly string[];
  /** Override the temp project root (default os.tmpdir()). */
  workdir_root?: string;
}

export async function renderComposition(
  opts: RenderOptions,
): Promise<RenderResult> {
  const start = Date.now();
  const root = opts.workdir_root ?? tmpdir();
  const projectDir = mkdtempSync(join(root, "qanot-render-"));
  const tmpOut = join(opts.output_dir, `${opts.job_id}.tmp.mp4`);
  const finalOut = join(opts.output_dir, `${opts.job_id}.mp4`);

  // Make sure no stale tmp lingers from a previous attempt at the same job_id.
  safeUnlink(tmpOut);

  try {
    writeFileSync(join(projectDir, "hyperframes.json"), HYPERFRAMES_JSON);
    writeFileSync(join(projectDir, "index.html"), opts.composition_html);

    // Default to the globally-installed `hyperframes` binary (image-baked at
    // pinned version). Skipping npx avoids npm's first-run install warnings
    // landing in stderr/stdout and tripping our parsers.
    const bin = opts.bin ?? "hyperframes";
    const args =
      opts.args_override ??
      [
        "render",
        projectDir,
        "--output",
        tmpOut,
        "--fps",
        String(opts.fps),
        "--quality",
        opts.quality,
        "--workers",
        "1",
        "--quiet",
      ];

    let lineBuffer = "";
    let lastReportedPercent = -1;
    let lastReportedStage: JobStage | undefined;

    const onLine = (raw: string): void => {
      const stripped = raw.replace(ANSI_ESCAPE_RE, "").trim();
      if (stripped.length === 0) return;
      const m = PERCENT_RE.exec(stripped);
      if (!m || m[1] === undefined) return;
      const percent = Math.min(100, Math.max(0, Number.parseInt(m[1], 10)));
      let stage: JobStage | undefined;
      for (const sp of STAGE_PATTERNS) {
        if (sp.re.test(stripped)) {
          stage = sp.stage;
          break;
        }
      }
      // Suppress duplicate emissions to avoid hammering SQLite.
      if (percent === lastReportedPercent && stage === lastReportedStage) return;
      lastReportedPercent = percent;
      lastReportedStage = stage;
      opts.on_progress?.({ percent, stage, raw_line: stripped });
    };

    const drain = (chunk: string): void => {
      lineBuffer += chunk;
      // HyperFrames uses CR (`\r`) to redraw the progress bar; treat both \r
      // and \n as line terminators so we get one onLine per visual update.
      const parts = lineBuffer.split(/[\r\n]/);
      lineBuffer = parts.pop() ?? "";
      for (const part of parts) onLine(part);
    };

    const deadlineMs = Math.max(1_000, opts.deadline_seconds * 1_000);

    const result = await spawnWithTimeout({
      command: bin,
      args,
      timeoutMs: deadlineMs,
      cancelSignal: opts.cancel_signal,
      onStdout: drain,
      onStderr: drain,
      spawnOptions: {
        cwd: projectDir,
      },
    });

    if (lineBuffer.length > 0) onLine(lineBuffer);

    const render_duration_ms = Date.now() - start;
    const stderrTail = result.stderr.slice(-STDERR_TAIL_BYTES);
    const stdoutTail = result.stdout.slice(-STDERR_TAIL_BYTES);
    const haystack = `${stderrTail}\n${stdoutTail}`;

    if (result.spawn_failed) {
      safeUnlink(tmpOut);
      return {
        ok: false,
        code: JobErrorCode.Internal,
        message: result.spawn_error?.message ?? "render subprocess failed to start",
        stderr_tail: stderrTail,
        render_duration_ms,
      };
    }

    if (result.timed_out) {
      safeUnlink(tmpOut);
      return {
        ok: false,
        code: JobErrorCode.RenderTimeout,
        message: `render exceeded ${String(opts.deadline_seconds)}s deadline`,
        stderr_tail: stderrTail,
        render_duration_ms,
      };
    }

    if (result.cancelled) {
      safeUnlink(tmpOut);
      return {
        ok: false,
        code: JobErrorCode.Internal,
        message: "render cancelled",
        stderr_tail: stderrTail,
        render_duration_ms,
      };
    }

    if (result.exit_code === 0) {
      // Atomic publish: rename tmp -> final. If the binary did not produce
      // the file (e.g. a misconfigured fake), surface as internal.
      try {
        const stat = statSync(tmpOut);
        renameSync(tmpOut, finalOut);
        return {
          ok: true,
          output_path: finalOut,
          output_size_bytes: stat.size,
          render_duration_ms,
        };
      } catch (err) {
        return {
          ok: false,
          code: JobErrorCode.Internal,
          message: `render reported success but output missing: ${
            err instanceof Error ? err.message : String(err)
          }`,
          stderr_tail: stderrTail,
          render_duration_ms,
        };
      }
    }

    // Non-zero exit. Classify.
    safeUnlink(tmpOut);
    if (CHROME_CRASH_RE.test(haystack)) {
      return {
        ok: false,
        code: JobErrorCode.ChromeCrash,
        message: "Chromium renderer crashed during composition",
        stderr_tail: stderrTail,
        render_duration_ms,
      };
    }
    if (ASSET_RE.test(haystack)) {
      return {
        ok: false,
        code: JobErrorCode.AssetFetchFailed,
        message: "Composition asset fetch failed",
        stderr_tail: stderrTail,
        render_duration_ms,
      };
    }
    return {
      ok: false,
      code: JobErrorCode.Internal,
      message: `render exited with code ${String(result.exit_code)}`,
      stderr_tail: stderrTail,
      render_duration_ms,
    };
  } finally {
    rmSync(projectDir, { recursive: true, force: true });
  }
}

function safeUnlink(path: string): void {
  try {
    unlinkSync(path);
  } catch {
    /* ignore -- file might not exist */
  }
}
