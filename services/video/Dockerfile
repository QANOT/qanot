# qanot-video render service.
#
# Runtime: Node.js 22 LTS per docs/video-engine/ARCHITECTURE.md §3.2 ("Match
# HyperFrames requirements"). Bun is the package manager + lockfile authority,
# but the running process is Node so better-sqlite3's native binding loads
# (Bun does not yet support better-sqlite3, oven-sh/bun#4290).
#
# Image bundles Chromium + FFmpeg + fonts now so Phase 2 doesn't have to
# rebuild a fatter image; Phase 1 itself does not invoke Chromium.

FROM node:22-bookworm-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    NODE_ENV=production \
    HOST=127.0.0.1 \
    PORT=8770 \
    DB_PATH=/data/video/jobs.db \
    OUTPUT_DIR=/data/video/renders \
    LOG_LEVEL=info \
    PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    BUN_INSTALL=/usr/local

# System packages: chromium for headless rendering, ffmpeg for encoding,
# fonts for text rendering, build tools for better-sqlite3 native build,
# ca-certificates for HTTPS, dumb-init for clean signal forwarding,
# unzip + curl so we can install Bun (used to install deps).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        ffmpeg \
        fonts-liberation \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
        unzip \
        dumb-init \
        python3 \
        make \
        g++ \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://bun.sh/install | bash -s "bun-v1.3.13" \
    && /usr/local/bin/bun --version

WORKDIR /app

# Dependency layer -- copy lockfile first for cacheability. Bun reads its
# own lockfile and writes a node_modules/ tree usable by Node.
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile --production

# Application source. tsx (devDep) is the runtime TS loader for Node;
# pull it in as a production-allowed package via --include-dev for the run
# stage. Easiest path: install everything (incl. dev) for the runtime image
# since image size is dominated by chromium anyway.
RUN bun install --frozen-lockfile

COPY src ./src
COPY compositions ./compositions
COPY tsconfig.json ./

# Pre-install hyperframes globally so the wrappers in src/render/ can call
# the `hyperframes` binary directly instead of `npx --yes --package=...`.
# Reasons: (1) avoid npm install warnings polluting stdout on first call
# (npm prints `npm warn deprecated ...` which made our JSON parser see
# unparseable output and surface lint_failed with a warning string as the
# message), (2) deterministic version pinned at image build time, (3) cold
# render starts in <1s instead of waiting for npx to fetch packages.
RUN npm install -g hyperframes@0.4.30 \
    && hyperframes --version

# Non-root user. Chromium's user-namespace sandbox needs the binary readable
# (which it is by default) plus the process to NOT be uid 0 -- otherwise
# Chromium refuses to start without --no-sandbox (which we will not use).
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 qanot \
    && mkdir -p /data/video /data/video/renders \
    && chown -R qanot:qanot /app /data/video

# Pre-seed the qanot user's hyperframes config so the very first runtime
# invocation does NOT print the telemetry notice ("Hyperframes collects
# anonymous usage data ...") ahead of lint JSON output — which would make
# JSON.parse fail and surface a misleading lint_failed result.
#
# The config file lives at $HOME/.hyperframes/config.json. Setting
# `telemetryNoticeShown: true` suppresses the prompt; `telemetryEnabled:
# false` opts us out of the data collection. Hyperframes writes back to
# this file on each command (commandCount counter, lastUpdateCheck), so
# it must be writable by the runtime user.
RUN mkdir -p /home/qanot/.hyperframes \
    && cat > /home/qanot/.hyperframes/config.json <<'EOF'
{
  "telemetryEnabled": false,
  "telemetryNoticeShown": true,
  "anonymousId": "00000000-0000-0000-0000-000000000000",
  "commandCount": 0,
  "lastUpdateCheck": "2026-01-01T00:00:00.000Z",
  "latestVersion": "0.4.30"
}
EOF
RUN chown -R qanot:qanot /home/qanot/.hyperframes

USER qanot

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --silent --fail http://127.0.0.1:8770/health || exit 1

ENTRYPOINT ["dumb-init", "--"]
CMD ["npx", "tsx", "src/server.ts"]
