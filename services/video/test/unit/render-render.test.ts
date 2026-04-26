/**
 * Unit tests for the hyperframes render wrapper.
 *
 * Same fake-bin strategy as render-lint.test.ts: the test materializes a
 * Node script that emulates whatever success/failure shape we want from
 * `hyperframes render`, including writing the .tmp.mp4 the wrapper expects
 * on the success path.
 */

import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderComposition } from "../../src/render/render.js";
import {
  JobErrorCode,
  RenderQuality,
  type RenderProgressCallback,
} from "../../src/types.js";

let workDir: string;
let outputDir: string;

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-render-tests-"));
  outputDir = join(workDir, "renders");
  mkdirSync(outputDir, { recursive: true });
});

afterEach(() => {
  rmSync(workDir, { recursive: true, force: true });
});

interface FakeOptions {
  /** Stderr lines to emit one at a time (with a 5ms gap), good for progress. */
  stderrLines?: string[];
  /** Single stdout payload, written immediately. */
  stdout?: string;
  /** Single stderr payload, written immediately (alternative to stderrLines). */
  stderr?: string;
  /** Exit code; default 0. */
  exit?: number;
  /** Sleep this many ms before exiting (used to trigger timeouts). */
  delayMs?: number;
  /**
   * If provided, write a small placeholder MP4 to this path BEFORE exiting.
   * This emulates what a successful hyperframes render would do.
   */
  writeOutputBytes?: string;
}

/** Build the fake render binary script + the args_override. */
function fakeRenderBin(
  jobId: string,
  opts: FakeOptions,
): { bin: string; args_override: string[] } {
  const script = join(workDir, `fake-render-${jobId}.cjs`);
  const tmpOut = join(outputDir, `${jobId}.tmp.mp4`);
  const body = `
const fs = require("node:fs");
const stderrLines = ${JSON.stringify(opts.stderrLines ?? [])};
const stderr = ${JSON.stringify(opts.stderr ?? "")};
const stdout = ${JSON.stringify(opts.stdout ?? "")};
const delay = ${String(opts.delayMs ?? 0)};
const exit = ${String(opts.exit ?? 0)};
const tmpOut = ${JSON.stringify(tmpOut)};
const writeBytes = ${JSON.stringify(opts.writeOutputBytes ?? "")};

(async () => {
  if (stdout) process.stdout.write(stdout);
  if (stderr) process.stderr.write(stderr);
  for (const line of stderrLines) {
    process.stderr.write(line + "\\n");
    await new Promise((r) => setTimeout(r, 5));
  }
  if (writeBytes) {
    fs.writeFileSync(tmpOut, writeBytes);
  }
  setTimeout(() => process.exit(exit), delay);
})();
`;
  writeFileSync(script, body);
  chmodSync(script, 0o755);
  return { bin: process.execPath, args_override: [script] };
}

describe("renderComposition", () => {
  it("renames tmp.mp4 to <job_id>.mp4 on exit 0 and returns the file size", async () => {
    const jobId = "job-success-001";
    const payload = "FAKE_MP4_BYTES_PRETEND";
    const { bin, args_override } = fakeRenderBin(jobId, {
      exit: 0,
      writeOutputBytes: payload,
    });
    const r = await renderComposition({
      job_id: jobId,
      composition_html: "<!doctype html><html></html>",
      output_dir: outputDir,
      fps: 30,
      quality: RenderQuality.Standard,
      deadline_seconds: 5,
      bin,
      args_override,
      workdir_root: workDir,
    });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.output_path).toBe(join(outputDir, `${jobId}.mp4`));
      expect(r.output_size_bytes).toBe(payload.length);
      expect(existsSync(r.output_path)).toBe(true);
    }
    // .tmp.mp4 must be gone (renamed).
    expect(existsSync(join(outputDir, `${jobId}.tmp.mp4`))).toBe(false);
  });

  it("classifies stderr containing 'Chromium' as chrome_crash on non-zero exit", async () => {
    const jobId = "job-chrome-crash";
    const { bin, args_override } = fakeRenderBin(jobId, {
      exit: 1,
      stderr: "FATAL: Chromium target closed unexpectedly\n",
    });
    const r = await renderComposition({
      job_id: jobId,
      composition_html: "<!doctype html><html></html>",
      output_dir: outputDir,
      fps: 30,
      quality: RenderQuality.Standard,
      deadline_seconds: 5,
      bin,
      args_override,
      workdir_root: workDir,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.code).toBe(JobErrorCode.ChromeCrash);
    }
  });

  it("classifies stderr containing 'ENOTFOUND' as asset_fetch_failed on non-zero exit", async () => {
    const jobId = "job-asset-fail";
    const { bin, args_override } = fakeRenderBin(jobId, {
      exit: 1,
      stderr: "Error: getaddrinfo ENOTFOUND fonts.example.com\n",
    });
    const r = await renderComposition({
      job_id: jobId,
      composition_html: "<!doctype html><html></html>",
      output_dir: outputDir,
      fps: 30,
      quality: RenderQuality.Standard,
      deadline_seconds: 5,
      bin,
      args_override,
      workdir_root: workDir,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.code).toBe(JobErrorCode.AssetFetchFailed);
    }
  });

  it("returns code=render_timeout when the deadline elapses", async () => {
    const jobId = "job-timeout";
    const { bin, args_override } = fakeRenderBin(jobId, {
      exit: 0,
      delayMs: 5_000,
    });
    const start = Date.now();
    const r = await renderComposition({
      job_id: jobId,
      composition_html: "<!doctype html><html></html>",
      output_dir: outputDir,
      fps: 30,
      quality: RenderQuality.Standard,
      // Spec requires deadline_seconds >= 1; we use 1 so the timeout fires fast.
      deadline_seconds: 1,
      bin,
      args_override,
      workdir_root: workDir,
    });
    const elapsed = Date.now() - start;
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.code).toBe(JobErrorCode.RenderTimeout);
    }
    // Deadline 1s + 5s SIGKILL grace; we expect to be killed before our fake's
    // 5s delay -- give a little margin for slow CI.
    expect(elapsed).toBeLessThan(8_000);
    // No published output on timeout.
    expect(existsSync(join(outputDir, `${jobId}.mp4`))).toBe(false);
  });

  it("forwards parsed progress percentages to the on_progress callback", async () => {
    const jobId = "job-progress";
    const { bin, args_override } = fakeRenderBin(jobId, {
      stderrLines: [
        "  ░░░░  10% Compiling composition",
        "  █░░░  25% Starting frame capture",
        "  ███░  50% Capturing frame 30/60",
        "  ████ 100% Render complete",
      ],
      writeOutputBytes: "PAYLOAD",
      exit: 0,
    });
    const events: Array<{ percent: number; stage?: string }> = [];
    const onProgress: RenderProgressCallback = (e) => {
      const evt: { percent: number; stage?: string } = { percent: e.percent };
      if (e.stage) evt.stage = e.stage;
      events.push(evt);
    };
    const r = await renderComposition({
      job_id: jobId,
      composition_html: "<!doctype html><html></html>",
      output_dir: outputDir,
      fps: 30,
      quality: RenderQuality.Draft,
      deadline_seconds: 5,
      bin,
      args_override,
      workdir_root: workDir,
      on_progress: onProgress,
    });
    expect(r.ok).toBe(true);
    expect(events.map((e) => e.percent)).toEqual([10, 25, 50, 100]);
    expect(events.find((e) => e.percent === 50)?.stage).toBe("rendering_frames");
  });

  it("aborts the subprocess and returns failure when cancel_signal fires mid-render", async () => {
    const jobId = "job-cancel";
    const { bin, args_override } = fakeRenderBin(jobId, {
      // Long-running fake, never actually exits on its own within the test.
      delayMs: 5_000,
      exit: 0,
    });
    const ctrl = new AbortController();
    const promise = renderComposition({
      job_id: jobId,
      composition_html: "<!doctype html><html></html>",
      output_dir: outputDir,
      fps: 30,
      quality: RenderQuality.Standard,
      deadline_seconds: 30,
      bin,
      args_override,
      workdir_root: workDir,
      cancel_signal: ctrl.signal,
    });
    // Fire abort almost immediately.
    setTimeout(() => ctrl.abort(), 50);
    const r = await promise;
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.code).toBe(JobErrorCode.Internal);
      expect(r.message).toMatch(/cancel/i);
    }
  });
});
