/**
 * Resource gauge sampler.
 *
 * §8.1 lists several gauges that require OS probes (disk usage, RSS,
 * chromium count). They are sampled here in one place so /metrics scrape
 * time stays predictable and the same probe can feed the disk-full degraded
 * mode in routes/render.ts.
 *
 * Cross-platform notes:
 *   - statvfs(2) is Linux/macOS. On Windows we degrade gracefully (no disk
 *     gauges), which is what test runners on developer laptops see when
 *     running on win32.
 *   - The chromium-process count is best-effort: walk /proc/<pid>/comm on
 *     Linux; otherwise return 0. The metric is documented as best-effort.
 */

import {
  readdirSync,
  readFileSync,
  statfsSync,
} from "node:fs";
import { setGauge } from "./metrics.js";

export interface DiskUsage {
  /** Mount point we measured -- usually OUTPUT_DIR's filesystem root. */
  mount: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  /** used / total, in [0, 1]. Returns 0 when total is unknown. */
  usage_ratio: number;
}

/**
 * Probe the filesystem that hosts `path`. Returns null on platforms where
 * statfs is not available (Windows test runners), or when the path itself
 * does not exist.
 */
export function probeDisk(path: string): DiskUsage | null {
  try {
    // Node 18.15+ exposes statfsSync. Skip if unavailable on this runtime.
    if (typeof statfsSync !== "function") return null;
    const st = statfsSync(path);
    const blockSize = Number(st.bsize);
    const totalBlocks = Number(st.blocks);
    const freeBlocks = Number(st.bavail);
    if (
      !Number.isFinite(blockSize) ||
      !Number.isFinite(totalBlocks) ||
      !Number.isFinite(freeBlocks) ||
      totalBlocks === 0
    ) {
      return null;
    }
    const totalBytes = blockSize * totalBlocks;
    const freeBytes = blockSize * freeBlocks;
    const usedBytes = Math.max(0, totalBytes - freeBytes);
    return {
      mount: path,
      total_bytes: totalBytes,
      used_bytes: usedBytes,
      free_bytes: freeBytes,
      usage_ratio: totalBytes > 0 ? usedBytes / totalBytes : 0,
    };
  } catch {
    return null;
  }
}

/**
 * Best-effort chromium child-process count.
 *
 *  - Linux: walk /proc/<pid>/comm and count those whose comm matches a
 *    chromium-family name. Cheap (<5ms on a host with 200 processes) and
 *    avoids spawning `pgrep`.
 *  - Other platforms: return 0. We document this as best-effort.
 */
export function countChromiumProcesses(): number {
  if (process.platform !== "linux") return 0;
  try {
    const entries = readdirSync("/proc");
    let count = 0;
    for (const e of entries) {
      // Only numeric directory names are PIDs.
      if (!/^\d+$/.test(e)) continue;
      try {
        const comm = readFileSync(`/proc/${e}/comm`, "utf8").trim();
        if (
          comm === "chromium" ||
          comm === "chrome" ||
          comm === "chrome-headless" ||
          comm === "chromium-browser" ||
          comm === "headless_shell"
        ) {
          count += 1;
        }
      } catch {
        // Process exited mid-scan, or we lost permission. Skip.
      }
    }
    return count;
  } catch {
    return 0;
  }
}

/** Snapshot of every resource gauge so callers can both publish + read. */
export interface ResourceSnapshot {
  rss_bytes: number;
  chromium_processes: number;
  disk: DiskUsage | null;
}

/**
 * Refresh every OS-probe gauge in one pass and return the snapshot. Called
 * from /metrics, /summary, and the disk-full degraded-mode probe.
 */
export function sampleResourceGauges(outputDir: string): ResourceSnapshot {
  const rss = process.memoryUsage().rss;
  setGauge("video_memory_rss_bytes", rss, { component: "render_service" });

  const chromium = countChromiumProcesses();
  setGauge("video_chromium_processes", chromium);

  const disk = probeDisk(outputDir);
  if (disk) {
    setGauge("video_disk_used_bytes", disk.used_bytes, { mount: disk.mount });
    setGauge("video_disk_free_bytes", disk.free_bytes, { mount: disk.mount });
  }

  return {
    rss_bytes: rss,
    chromium_processes: chromium,
    disk,
  };
}
