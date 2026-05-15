/**
 * Reels-via-service asset bridge.
 *
 * The reels pipeline (qanot plugins/reels) stages a render's assets into a
 * Docker volume shared with this container, at
 * `<REEL_SHARED_ASSET_ROOT>/<request_id>/assets`, and references them in the
 * composition by the project-relative path `assets/...`.
 *
 * Both the lint pass (lint.ts) and the render pass (render.ts) materialize
 * their own temp projectDir containing only `index.html` + `hyperframes.json`
 * and invoke the `hyperframes` CLI with cwd=projectDir. HyperFrames resolves
 * asset srcs project-relative and rejects anything not physically inside the
 * project ("not found in the project") — a symlink pointing outside the
 * project does not satisfy it. So we COPY the staged shared dir into each
 * projectDir as `assets/`.
 *
 * Additive and inert for every other caller (e.g. render_video): no shared
 * dir exists for their request_id, so nothing is copied. request_id is
 * UUID-validated at the route layer; re-validated here and the resolved
 * path is confined under the shared root.
 */

import { cpSync, existsSync, realpathSync, statSync } from "node:fs";
import { join } from "node:path";

export const REEL_SHARED_ASSET_ROOT =
  process.env["REEL_SHARED_ASSET_ROOT"] ?? "/app/assets/reel-share";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * If a shared asset dir exists for `requestId`, copy it into
 * `<projectDir>/assets`. No-op (with a diagnostic on stderr) otherwise.
 */
export function stageSharedAssets(
  projectDir: string,
  requestId: string | undefined,
): void {
  if (!requestId || !UUID_RE.test(requestId)) return;
  if (!existsSync(REEL_SHARED_ASSET_ROOT)) {
    console.warn(`[reel-assets] root missing: ${REEL_SHARED_ASSET_ROOT}`);
    return;
  }
  const candidate = join(REEL_SHARED_ASSET_ROOT, requestId, "assets");
  let real: string;
  let rootReal: string;
  try {
    rootReal = realpathSync(REEL_SHARED_ASSET_ROOT);
    real = realpathSync(candidate);
  } catch {
    console.warn(`[reel-assets] no staged dir for ${requestId} (${candidate})`);
    return;
  }
  // Defense in depth: the resolved path must stay under the shared root.
  if (real !== rootReal && !real.startsWith(rootReal + "/")) {
    console.warn(`[reel-assets] resolved path escapes root: ${real}`);
    return;
  }
  if (!statSync(real).isDirectory()) return;
  const dest = join(projectDir, "assets");
  cpSync(real, dest, { recursive: true, dereference: true });
  console.warn(`[reel-assets] staged ${real} -> ${dest}`);
}
