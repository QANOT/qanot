/**
 * HTTP server entrypoint.
 *
 * Wires the Hono app together: per-request logger, auth middleware, route
 * mounts, error envelope. Starts the worker. Handles graceful shutdown
 * (SIGTERM/SIGINT) per the spec: stop accepting connections, drain in-flight,
 * stop worker, close DB. 5s timeout.
 *
 * Runtime: Node.js 22 LTS (per docs/video-engine/ARCHITECTURE.md §3.2 --
 * "Match HyperFrames requirements"). Bun is the package manager and lint
 * runner. better-sqlite3 has a native binding that does not load under the
 * Bun runtime today (oven-sh/bun#4290), and the spec mandates better-sqlite3
 * for the queue. Production therefore runs on Node via `tsx src/server.ts`
 * (no build step). Tests run via vitest on Node.
 */

import { serve, type ServerType } from "@hono/node-server";
import type { Database as SqliteDb } from "better-sqlite3";
import { Hono, type Context } from "hono";
import type { Env } from "hono";
import type { Logger } from "pino";
import { ulid } from "ulid";
import { serviceKeyAuth } from "./auth/service-key.js";
import { loadConfig, type Config } from "./config.js";
import { childLogger, getLogger } from "./observability/logger.js";
import { incCounter } from "./observability/metrics.js";
import { CronManager } from "./queue/cron.js";
import { openDatabase } from "./queue/db.js";
import { Worker } from "./queue/worker.js";
import { buildHealthRoutes } from "./routes/health.js";
import { buildJobsRoutes } from "./routes/jobs.js";
import { buildMetricsRoutes } from "./routes/metrics.js";
import { buildRenderRoutes } from "./routes/render.js";
import { buildSummaryRoutes } from "./routes/summary.js";
import type { ErrorEnvelope } from "./types.js";

const SHUTDOWN_TIMEOUT_MS = 5000;

export interface AppDeps {
  db: SqliteDb;
  config: Config;
  worker: Worker;
}

/** Hono Variables map for `c.set` / `c.get` -- keeps types honest. */
export interface AppEnv extends Env {
  Variables: {
    requestId: string;
    logger: Logger;
  };
}

/**
 * Build the Hono app. Pure function -- no listening, no side effects beyond
 * DB queries triggered by handlers.
 */
export function buildApp(deps: AppDeps): Hono<AppEnv> {
  const app = new Hono<AppEnv>();

  // Per-request logger + request_id propagation.
  app.use("*", async (c, next) => {
    const requestId = c.req.header("x-request-id") ?? ulid();
    const log = childLogger({
      request_id: requestId,
      method: c.req.method,
      path: c.req.path,
    });
    c.set("requestId", requestId);
    c.set("logger", log);
    c.header("X-Request-ID", requestId);

    const start = Date.now();
    try {
      await next();
    } finally {
      const duration_ms = Date.now() - start;
      const status = c.res.status;
      log.info({ status, duration_ms }, "request");
      incCounter("http_requests_total", {
        method: c.req.method,
        path: routeLabel(c),
        status: String(status),
      });
    }
  });

  // Auth -- /health bypasses inside the middleware.
  app.use("*", serviceKeyAuth({ secret: deps.config.SERVICE_SECRET }));

  // Routes.
  app.route("/", buildHealthRoutes({ db: deps.db }));
  app.route(
    "/",
    buildRenderRoutes({ db: deps.db, outputDir: deps.config.OUTPUT_DIR }),
  );
  app.route(
    "/",
    buildJobsRoutes({ db: deps.db, worker: deps.worker, outputDir: deps.config.OUTPUT_DIR }),
  );
  app.route("/", buildMetricsRoutes({ outputDir: deps.config.OUTPUT_DIR }));
  app.route(
    "/",
    buildSummaryRoutes({ db: deps.db, worker: deps.worker, outputDir: deps.config.OUTPUT_DIR }),
  );

  // 404 envelope.
  app.notFound((c) => {
    const body: ErrorEnvelope = {
      error: { code: "not_found", message: `Route not found: ${c.req.method} ${c.req.path}` },
    };
    return c.json(body, 404);
  });

  // Error envelope.
  app.onError((err, c) => {
    const log = c.get("logger") ?? getLogger();
    log.error(
      {
        err: err instanceof Error ? { message: err.message, stack: err.stack } : { value: String(err) },
      },
      "unhandled error",
    );
    const body: ErrorEnvelope = {
      error: { code: "internal", message: "Internal server error." },
    };
    return c.json(body, 500);
  });

  return app;
}

function routeLabel(c: Context<AppEnv>): string {
  // Hono exposes the matched route pattern as `c.req.routePath` in v4.
  // Strip query strings to keep label cardinality bounded; fall back to the
  // literal path on 404 (no matched route).
  const route = c.req.routePath;
  if (route && route !== "/*") return route;
  return c.req.path;
}

export interface StartOptions {
  /** Override port for tests (use 0 to bind to a free port). */
  port?: number;
  /** Override host for tests. */
  host?: string;
}

export interface RunningServer {
  server: ServerType;
  worker: Worker;
  cron: CronManager;
  db: SqliteDb;
  port: number;
  host: string;
  /** Resolves after listeners removed, worker stopped, DB closed. */
  close: () => Promise<void>;
}

/** Open the DB, build the app, start listening, start the worker. */
export async function startServer(opts: StartOptions = {}): Promise<RunningServer> {
  const config = loadConfig();
  const log = getLogger();
  const db = openDatabase(config.DB_PATH);
  const worker = new Worker({ db, outputDir: config.OUTPUT_DIR });
  const cron = new CronManager({
    db,
    outputDir: config.OUTPUT_DIR,
    dbPath: config.DB_PATH,
  });
  const app = buildApp({ db, config, worker });

  const host = opts.host ?? config.HOST;
  const port = opts.port ?? config.PORT;

  const server = await new Promise<ServerType>((resolve) => {
    const s = serve(
      {
        fetch: app.fetch,
        hostname: host,
        port,
      },
      (info) => {
        log.info({ host: info.address, port: info.port }, "qanot-video listening");
        resolve(s);
      },
    );
  });

  worker.start();
  cron.start();

  const addr = server.address();
  const boundPort =
    addr && typeof addr === "object" && "port" in addr ? addr.port : port;
  const boundHost =
    addr && typeof addr === "object" && "address" in addr ? addr.address : host;

  let closed = false;
  const close = async (): Promise<void> => {
    if (closed) return;
    closed = true;
    log.info("graceful shutdown begin");

    const stopHttp = new Promise<void>((resolve) => {
      server.close(() => resolve());
    });
    const timeout = new Promise<void>((resolve) => {
      setTimeout(() => {
        log.warn({ timeout_ms: SHUTDOWN_TIMEOUT_MS }, "graceful shutdown timeout");
        resolve();
      }, SHUTDOWN_TIMEOUT_MS);
    });
    await Promise.race([stopHttp, timeout]);

    await worker.stop();
    await cron.stop();
    db.close();
    log.info("graceful shutdown complete");
  };

  return { server, worker, cron, db, port: boundPort, host: boundHost, close };
}

// Module-level run when executed directly (bun run src/server.ts).
const isDirectRun =
  typeof process !== "undefined" &&
  process.argv[1] !== undefined &&
  process.argv[1].endsWith("server.ts");

if (isDirectRun) {
  startServer()
    .then((running) => {
      const shutdown = (signal: NodeJS.Signals) => {
        getLogger().info({ signal }, "shutdown signal received");
        running.close().then(
          () => process.exit(0),
          (err) => {
            getLogger().error(
              { err: err instanceof Error ? err.message : String(err) },
              "shutdown failed",
            );
            process.exit(1);
          },
        );
      };
      process.on("SIGTERM", shutdown);
      process.on("SIGINT", shutdown);
    })
    .catch((err) => {
      // eslint-disable-next-line no-console
      console.error("startup failed:", err);
      process.exit(1);
    });
}
