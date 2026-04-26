/**
 * Generic "spawn a subprocess with a hard wall clock and SIGTERM-then-SIGKILL
 * escalation" helper. Used by both the lint wrapper and the render wrapper so
 * the kill ladder is implemented in exactly one place.
 *
 * Per docs/video-engine/ARCHITECTURE.md §9.2: "Per-job timeout
 * (deadline_seconds, default 120). Exceeded -> SIGTERM, 5s grace, SIGKILL."
 *
 * Why two signals: SIGTERM lets HyperFrames clean up its Chromium child
 * process; if it ignores us (or is wedged inside a syscall), SIGKILL
 * guarantees process exit so the worker is not stuck.
 */

import {
  spawn,
  type ChildProcessByStdio,
  type SpawnOptionsWithoutStdio,
} from "node:child_process";
import type { Readable } from "node:stream";

const SIGKILL_GRACE_MS = 5_000;

export interface SpawnWithTimeoutOptions {
  /** Subprocess command. */
  command: string;
  /** Subprocess args. */
  args: readonly string[];
  /** Hard timeout. After this, SIGTERM. After +5s, SIGKILL. */
  timeoutMs: number;
  /** Forwarded to child_process.spawn (cwd, env, ...). */
  spawnOptions?: SpawnOptionsWithoutStdio;
  /** Called per stdout chunk (already a string). */
  onStdout?: (chunk: string) => void;
  /** Called per stderr chunk (already a string). */
  onStderr?: (chunk: string) => void;
  /** Optional external cancel signal: aborting triggers the same kill ladder. */
  cancelSignal?: AbortSignal;
  /**
   * Hook fired when the process is sent SIGTERM (timeout OR cancel). Useful
   * for tests asserting the kill ladder; pure observation, no side effects.
   */
  onTerminate?: (reason: "timeout" | "cancelled") => void;
}

export interface SpawnWithTimeoutResult {
  /** Process exit code if it exited normally; null if killed by signal. */
  exit_code: number | null;
  /** Signal name if the kernel killed the process; null otherwise. */
  signal: NodeJS.Signals | null;
  /** Concatenated stdout. */
  stdout: string;
  /** Concatenated stderr. */
  stderr: string;
  /** Wall clock duration in ms, from spawn() to exit. */
  duration_ms: number;
  /** True if our timeout escalation fired (regardless of exit cause). */
  timed_out: boolean;
  /** True if cancellation was requested via cancelSignal. */
  cancelled: boolean;
  /** True if the process never started (ENOENT, EACCES, ...). */
  spawn_failed: boolean;
  /** The Error from `spawn`'s `error` event, if any. */
  spawn_error?: Error;
}

/**
 * Spawn a subprocess with a hard timeout and graceful escalation.
 *
 * Promise resolves with the result regardless of how the process exited; it
 * does not reject for non-zero exit codes. The caller decides how to
 * interpret exit codes based on the tool's contract.
 *
 * Spawn-time errors (e.g. ENOENT when the binary is missing) resolve with
 * `spawn_failed=true` rather than rejecting, because callers want a uniform
 * result envelope.
 */
export function spawnWithTimeout(
  opts: SpawnWithTimeoutOptions,
): Promise<SpawnWithTimeoutResult> {
  return new Promise((resolve) => {
    const start = Date.now();
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let cancelled = false;
    let resolved = false;
    let child: ChildProcessByStdio<null, Readable, Readable>;

    const finish = (
      partial: Pick<
        SpawnWithTimeoutResult,
        "exit_code" | "signal" | "spawn_failed" | "spawn_error"
      >,
    ): void => {
      if (resolved) return;
      resolved = true;
      clearTimeout(termTimer);
      clearTimeout(killTimer);
      cancelSignal?.removeEventListener("abort", onCancel);
      resolve({
        exit_code: partial.exit_code,
        signal: partial.signal,
        stdout,
        stderr,
        duration_ms: Date.now() - start,
        timed_out: timedOut,
        cancelled,
        spawn_failed: partial.spawn_failed,
        spawn_error: partial.spawn_error,
      });
    };

    try {
      child = spawn(opts.command, [...opts.args], {
        ...opts.spawnOptions,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (err) {
      finish({
        exit_code: null,
        signal: null,
        spawn_failed: true,
        spawn_error: err instanceof Error ? err : new Error(String(err)),
      });
      return;
    }

    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
      opts.onStdout?.(chunk);
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
      opts.onStderr?.(chunk);
    });

    const escalateKill = (): void => {
      if (!child.killed) {
        // SIGKILL is the only guaranteed-fatal escalation. We do not catch
        // errors here: if the kernel says no such process, the exit handler
        // already fired and resolved us.
        try {
          child.kill("SIGKILL");
        } catch {
          /* already exited */
        }
      }
    };

    const requestTerminate = (reason: "timeout" | "cancelled"): void => {
      if (resolved) return;
      if (reason === "timeout") timedOut = true;
      if (reason === "cancelled") cancelled = true;
      opts.onTerminate?.(reason);
      try {
        child.kill("SIGTERM");
      } catch {
        /* already exited */
      }
      // Schedule the SIGKILL escalation. If the child exits cleanly within
      // the grace window the killTimer is cleared in finish().
      killTimer = setTimeout(escalateKill, SIGKILL_GRACE_MS);
      // Make sure the test process can exit even if the child somehow ignores
      // both signals -- unref the kill timer.
      killTimer.unref?.();
    };

    const termTimer = setTimeout(() => {
      requestTerminate("timeout");
    }, opts.timeoutMs);
    termTimer.unref?.();
    let killTimer: NodeJS.Timeout = setTimeout(() => undefined, 0);
    clearTimeout(killTimer);

    const cancelSignal = opts.cancelSignal;
    const onCancel = (): void => {
      requestTerminate("cancelled");
    };
    if (cancelSignal) {
      if (cancelSignal.aborted) {
        // Signal already fired before we attached -- terminate immediately.
        requestTerminate("cancelled");
      } else {
        cancelSignal.addEventListener("abort", onCancel, { once: true });
      }
    }

    child.on("error", (err) => {
      finish({
        exit_code: null,
        signal: null,
        spawn_failed: true,
        spawn_error: err,
      });
    });

    child.on("close", (code, signal) => {
      finish({
        exit_code: code,
        signal,
        spawn_failed: false,
      });
    });
  });
}
