/**
 * Shared types for the render service.
 *
 * Phase 1 only includes types needed by the skeleton. Phase 2 will add
 * RenderResult, LintResult, etc. as the worker grows.
 */

/** Job lifecycle states per docs/video-engine/ARCHITECTURE.md §5.1. */
export const JobStatus = {
  Queued: "queued",
  Linting: "linting",
  Rendering: "rendering",
  Succeeded: "succeeded",
  Failed: "failed",
  Cancelled: "cancelled",
  Expired: "expired",
} as const;

export type JobStatus = (typeof JobStatus)[keyof typeof JobStatus];

/** Finer-grained progress stage; subset shown to clients. */
export const JobStage = {
  Queued: "queued",
  Linting: "linting",
  RenderingFrames: "rendering_frames",
  EncodingVideo: "encoding_video",
  Succeeded: "succeeded",
  Failed: "failed",
  Expired: "expired",
  Cancelled: "cancelled",
} as const;

export type JobStage = (typeof JobStage)[keyof typeof JobStage];

/** Closed set of error codes per §3.4. */
export const JobErrorCode = {
  LintFailed: "lint_failed",
  RenderTimeout: "render_timeout",
  ChromeCrash: "chrome_crash",
  AssetFetchFailed: "asset_fetch_failed",
  DiskFull: "disk_full",
  OomKilled: "oom_killed",
  Internal: "internal",
} as const;

export type JobErrorCode = (typeof JobErrorCode)[keyof typeof JobErrorCode];

export const VideoFormat = {
  Vertical: "9:16",
  Horizontal: "16:9",
  Square: "1:1",
} as const;

export type VideoFormat = (typeof VideoFormat)[keyof typeof VideoFormat];

export const RenderQuality = {
  Draft: "draft",
  Standard: "standard",
  High: "high",
} as const;

export type RenderQuality = (typeof RenderQuality)[keyof typeof RenderQuality];

/** Row shape mirroring the SQLite schema in §5.2. */
export interface Job {
  id: string;
  request_id: string;
  bot_id: string;
  user_id: string;
  composition_html: string;
  format: VideoFormat;
  duration_seconds: number;
  fps: number;
  quality: RenderQuality;
  deadline_seconds: number;

  status: JobStatus;
  stage: JobStage | null;
  progress_percent: number;
  error_code: JobErrorCode | null;
  error_message: string | null;
  /** JSON-serialized error details. */
  error_details: string | null;

  output_path: string | null;
  output_size_bytes: number | null;
  render_duration_ms: number | null;

  /** Unix epoch seconds; for crash recovery. */
  leased_until: number | null;
  queued_at: number;
  started_at: number | null;
  finished_at: number | null;
  /** queued_at + 24h. */
  expires_at: number;
}

/** POST /render request body shape (Phase 2 implements full validation). */
export interface RenderRequest {
  request_id: string;
  bot_id: string;
  user_id: string;
  composition_html: string;
  format: VideoFormat;
  duration_seconds: number;
  fps?: number;
  quality?: RenderQuality;
  deadline_seconds?: number;
}

/** Standard error envelope used on every 4xx/5xx response. */
export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}
