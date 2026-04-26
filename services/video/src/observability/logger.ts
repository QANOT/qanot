/**
 * Structured JSON logging via pino.
 *
 * Per §8.2: every line is structured JSON. `request_id` is the join key
 * across the Python framework and this service.
 */

import pino, { type Logger } from "pino";
import { loadConfig } from "../config.js";

let rootLogger: Logger | null = null;

export function getLogger(): Logger {
  if (rootLogger) return rootLogger;

  const cfg = loadConfig();
  const isDev = cfg.NODE_ENV === "development";

  rootLogger = pino({
    level: cfg.LOG_LEVEL,
    base: {
      service: "qanot-video",
    },
    timestamp: pino.stdTimeFunctions.isoTime,
    formatters: {
      level: (label) => ({ level: label }),
    },
    ...(isDev
      ? {
          transport: {
            target: "pino-pretty",
            options: {
              colorize: true,
              translateTime: "SYS:HH:MM:ss.l",
              ignore: "pid,hostname,service",
            },
          },
        }
      : {}),
  });

  return rootLogger;
}

/**
 * Build a child logger with per-request context. The bound fields appear on
 * every log line emitted via the returned logger.
 */
export function childLogger(bindings: Record<string, unknown>): Logger {
  return getLogger().child(bindings);
}

/** Test helper: drop the cached root logger. */
export function resetLoggerForTesting(): void {
  rootLogger = null;
}
