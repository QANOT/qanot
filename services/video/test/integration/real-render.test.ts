/**
 * Real-render integration test (gated).
 *
 * Skipped unless `RUN_INTEGRATION=1`. Renders compositions/_smoke.html via
 * the real `hyperframes render` CLI through the Phase 2 wrapper, then
 * verifies the output MP4 exists and has a non-zero size.
 *
 * Why gated: this test pulls hyperframes from npm (~30s on first run), spawns
 * Chromium, and renders 60 frames. It is the closest thing to an end-to-end
 * smoke we can run in CI, but it is not appropriate for every PR. The
 * dedicated `video-service-real-render` workflow runs it on demand
 * (workflow_dispatch) before deploys.
 */

import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { renderComposition } from "../../src/render/render.js";
import { RenderQuality } from "../../src/types.js";

const ENABLED = process.env.RUN_INTEGRATION === "1";
const describeMaybe = ENABLED ? describe : describe.skip;

let workDir: string;
let outputDir: string;

beforeAll(() => {
  if (!ENABLED) return;
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-real-"));
  outputDir = join(workDir, "renders");
  mkdirSync(outputDir, { recursive: true });
});

afterAll(() => {
  if (!ENABLED) return;
  rmSync(workDir, { recursive: true, force: true });
});

describeMaybe("real hyperframes render (RUN_INTEGRATION=1)", () => {
  it(
    "renders the smoke composition end-to-end and produces a non-zero MP4",
    async () => {
      const compositionPath = join(
        process.cwd(),
        "compositions",
        "_smoke.html",
      );
      const html = readFileSync(compositionPath, "utf8");
      expect(html).toContain('data-composition-id="main"');

      const jobId = `smoke-${Date.now().toString(36)}`;
      const result = await renderComposition({
        job_id: jobId,
        composition_html: html,
        output_dir: outputDir,
        fps: 30,
        quality: RenderQuality.Draft,
        deadline_seconds: 180,
        workdir_root: workDir,
      });

      if (!result.ok) {
        throw new Error(
          `real render failed: code=${result.code} message=${result.message}\nstderr_tail:\n${result.stderr_tail.slice(-2000)}`,
        );
      }
      expect(result.ok).toBe(true);
      expect(existsSync(result.output_path)).toBe(true);
      const stat = statSync(result.output_path);
      expect(stat.size).toBeGreaterThan(0);
      // Sanity bound: a 2-second 1080x1920 30fps draft output is at least a
      // few KB, but well under 100 MB. Loose bounds catch obvious corruption.
      expect(stat.size).toBeGreaterThan(1024);
      expect(stat.size).toBeLessThan(100 * 1024 * 1024);
    },
    // Real render needs a generous timeout: hyperframes pulls Chromium on
    // first invocation in CI.
    300_000,
  );
});
