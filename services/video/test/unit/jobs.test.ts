/**
 * Jobs CRUD unit tests.
 *
 * Covers: insert + getById round-trip, listByStatus filters by status,
 * idempotency by request_id (UNIQUE constraint), atomic status transitions.
 */

import type { Database as SqliteDb } from "better-sqlite3";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  getById,
  insertJob,
  listByStatus,
  transitionStatus,
} from "../../src/queue/jobs.js";
import { openDatabase } from "../../src/queue/db.js";
import {
  JobStatus,
  RenderQuality,
  VideoFormat,
  type RenderRequest,
} from "../../src/types.js";

let workDir: string;
let db: SqliteDb;

const baseRequest: RenderRequest = {
  request_id: "req-abc-123",
  bot_id: "topkeydevbot",
  user_id: "u-555",
  composition_html: "<!doctype html><html></html>",
  format: VideoFormat.Vertical,
  duration_seconds: 30,
  fps: 30,
  quality: RenderQuality.Standard,
  deadline_seconds: 120,
};

beforeEach(() => {
  workDir = mkdtempSync(join(tmpdir(), "qanot-video-jobs-"));
  db = openDatabase(join(workDir, "jobs.db"));
});

afterEach(() => {
  db.close();
  rmSync(workDir, { recursive: true, force: true });
});

describe("insertJob + getById", () => {
  it("round-trips a freshly inserted job", () => {
    const { job, duplicate } = insertJob(db, baseRequest);
    expect(duplicate).toBe(false);
    expect(job.id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
    expect(job.status).toBe(JobStatus.Queued);
    expect(job.bot_id).toBe("topkeydevbot");
    expect(job.composition_html).toBe(baseRequest.composition_html);
    expect(job.fps).toBe(30);
    expect(job.expires_at).toBeGreaterThan(job.queued_at);
    expect(job.expires_at - job.queued_at).toBe(24 * 60 * 60);

    const fetched = getById(db, job.id);
    expect(fetched).not.toBeNull();
    expect(fetched?.id).toBe(job.id);
    expect(fetched?.request_id).toBe(baseRequest.request_id);
  });

  it("applies defaults for fps/quality/deadline when omitted", () => {
    const minimal: RenderRequest = {
      request_id: "req-defaults",
      bot_id: "b",
      user_id: "u",
      composition_html: "<!doctype html>",
      format: VideoFormat.Square,
      duration_seconds: 10,
    };
    const { job } = insertJob(db, minimal);
    expect(job.fps).toBe(30);
    expect(job.quality).toBe(RenderQuality.Standard);
    expect(job.deadline_seconds).toBe(120);
  });
});

describe("idempotency via request_id UNIQUE", () => {
  it("returns the existing job when re-submitting the same request_id", () => {
    const first = insertJob(db, baseRequest);
    expect(first.duplicate).toBe(false);

    const second = insertJob(db, {
      ...baseRequest,
      // Different content, same request_id -- caller is retrying.
      composition_html: "<!doctype html><body>changed</body>",
      duration_seconds: 60,
    });
    expect(second.duplicate).toBe(true);
    expect(second.job.id).toBe(first.job.id);
    // Original content preserved -- we did NOT overwrite.
    expect(second.job.composition_html).toBe(baseRequest.composition_html);
    expect(second.job.duration_seconds).toBe(30);
  });
});

describe("listByStatus", () => {
  it("filters jobs by status and orders by queued_at ASC", () => {
    const a = insertJob(db, { ...baseRequest, request_id: "r-a" }).job;
    insertJob(db, { ...baseRequest, request_id: "r-b" });
    insertJob(db, { ...baseRequest, request_id: "r-c" });

    // Move one to a different status.
    const moved = transitionStatus(db, a.id, JobStatus.Queued, JobStatus.Cancelled);
    expect(moved).toBe(true);

    const queued = listByStatus(db, JobStatus.Queued);
    expect(queued.map((j) => j.request_id)).toEqual(["r-b", "r-c"]);

    const cancelled = listByStatus(db, JobStatus.Cancelled);
    expect(cancelled.map((j) => j.request_id)).toEqual(["r-a"]);
  });
});

describe("transitionStatus", () => {
  it("succeeds only when the row is currently in the expected status", () => {
    const { job } = insertJob(db, baseRequest);

    const ok = transitionStatus(db, job.id, JobStatus.Queued, JobStatus.Linting, {
      stage: "linting",
      started_at: 1234567890,
    });
    expect(ok).toBe(true);

    const after = getById(db, job.id);
    expect(after?.status).toBe(JobStatus.Linting);
    expect(after?.stage).toBe("linting");
    expect(after?.started_at).toBe(1234567890);

    // Trying to transition from queued again must fail.
    const failed = transitionStatus(db, job.id, JobStatus.Queued, JobStatus.Rendering);
    expect(failed).toBe(false);
  });

  it("returns false when the job does not exist", () => {
    const ok = transitionStatus(db, "nonexistent", JobStatus.Queued, JobStatus.Cancelled);
    expect(ok).toBe(false);
  });
});
