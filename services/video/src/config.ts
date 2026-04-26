/**
 * Environment configuration.
 *
 * Loads + validates env vars at startup. Throws clearly on missing/invalid.
 * Phase 1: minimal set per docs/video-engine/ARCHITECTURE.md §3 + §7.1.
 */

import { z } from "zod";

const ConfigSchema = z.object({
  HOST: z.string().min(1).default("127.0.0.1"),
  // 0 is allowed and means "ask the kernel for a free port" (used in tests).
  PORT: z.coerce.number().int().min(0).max(65535).default(8770),
  SERVICE_SECRET: z
    .string()
    .min(16, "SERVICE_SECRET must be at least 16 characters (use `openssl rand -hex 32`)"),
  DB_PATH: z.string().min(1).default("./data/jobs.db"),
  OUTPUT_DIR: z.string().min(1).default("./data/renders"),
  LOG_LEVEL: z
    .enum(["fatal", "error", "warn", "info", "debug", "trace", "silent"])
    .default("info"),
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
});

export type Config = z.infer<typeof ConfigSchema>;

let cached: Config | null = null;

/**
 * Parse and validate process.env. Memoized after first call.
 *
 * Set `force=true` in tests to re-read after mutating process.env.
 */
export function loadConfig(force = false): Config {
  if (cached && !force) return cached;

  const result = ConfigSchema.safeParse(process.env);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  - ${i.path.join(".")}: ${i.message}`)
      .join("\n");
    throw new Error(`Invalid configuration:\n${issues}`);
  }
  cached = result.data;
  return cached;
}

/** Test helper: clear the memoized config so the next loadConfig() re-reads env. */
export function resetConfigForTesting(): void {
  cached = null;
}
