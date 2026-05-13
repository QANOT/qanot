"""Tests for qanot.memos.store — atomic writes, scope filtering, archive."""

from __future__ import annotations

from pathlib import Path

import pytest

from qanot.memos import (
    ARCHIVE_DIR_NAME,
    MEMOS_DIR_NAME,
    MemoStore,
    MemoType,
    StoreError,
    parse_memo_file,
)


@pytest.fixture
def store(tmp_path):
    return MemoStore(tmp_path)


# ─── upsert ──────────────────────────────────────────────────────


class TestUpsert:
    def test_create(self, store, tmp_path):
        r = store.upsert(
            "user-name", "user's name", MemoType.USER, "Goes by Umurzoq.",
        )
        assert r.action == "created"
        assert r.path == tmp_path / MEMOS_DIR_NAME / "user-name.md"
        assert r.path.is_file()
        m = parse_memo_file(r.path)
        assert m.name == "user-name"

    def test_update_existing(self, store):
        store.upsert("u", "v1 desc", MemoType.USER, "v1 body")
        r = store.upsert("u", "v2 desc", MemoType.USER, "v2 body")
        assert r.action == "updated"
        m = parse_memo_file(r.path)
        assert m.description == "v2 desc"
        assert "v2 body" in m.body

    def test_validation_via_render(self, store):
        with pytest.raises(Exception):
            store.upsert("BadName", "d", MemoType.USER, "b")

    def test_post_write_validation(self, store):
        # Smoke-test that a successful write round-trips correctly.
        store.upsert(
            "feedback-title-fmt", "title format rule", MemoType.FEEDBACK,
            "Title: 12-may, 2026",
            user_scope="1545224574", thread_scope="kunlik",
            why="user said", how_to_apply="daily notes",
        )
        m = store.load("feedback-title-fmt")
        assert m is not None
        assert m.user_scope == "1545224574"
        assert m.thread_scope == "kunlik"
        assert m.why == "user said"


# ─── list_all + list_in_scope ────────────────────────────────────


class TestListing:
    def test_list_all_empty(self, store):
        assert store.list_all() == []

    def test_list_all_skips_subdirs(self, store, tmp_path):
        # Create a legacy daily-notes subdir with an .md file — must be skipped.
        store.upsert("real", "real memo", MemoType.USER, "body")
        legacy = tmp_path / MEMOS_DIR_NAME / "daily-notes"
        legacy.mkdir(parents=True)
        (legacy / "2026-05-13.md").write_text(
            "# Daily Notes\nnot a memo", encoding="utf-8",
        )
        names = [m.name for m in store.list_all()]
        assert names == ["real"]

    def test_list_all_skips_archive(self, store, tmp_path):
        store.upsert("real", "real memo", MemoType.USER, "body")
        store.upsert("trash", "trash memo", MemoType.USER, "body")
        store.archive("trash")
        names = [m.name for m in store.list_all()]
        assert names == ["real"]

    def test_list_skips_malformed_files(self, store, tmp_path):
        store.upsert("good", "good memo", MemoType.USER, "body")
        bad = tmp_path / MEMOS_DIR_NAME / "broken.md"
        bad.write_text("not even frontmatter", encoding="utf-8")
        names = [m.name for m in store.list_all()]
        assert names == ["good"]

    def test_list_in_scope_global_caller(self, store):
        store.upsert("g", "global memo", MemoType.USER, "b")
        store.upsert("u", "user memo", MemoType.USER, "b", user_scope="alice")
        store.upsert("t", "thread memo", MemoType.FEEDBACK, "b",
                     thread_scope="ielts")
        # Caller without user_id / thread_id sees only the global memo.
        names = [m.name for m in store.list_in_scope()]
        assert names == ["g"]

    def test_list_in_scope_matched_user(self, store):
        store.upsert("g", "global memo", MemoType.USER, "b")
        store.upsert("alice", "for alice", MemoType.USER, "b", user_scope="alice")
        store.upsert("bob", "for bob", MemoType.USER, "b", user_scope="bob")
        names = sorted(m.name for m in store.list_in_scope(user_id="alice"))
        assert names == ["alice", "g"]

    def test_list_in_scope_matched_thread(self, store):
        store.upsert("g", "global memo", MemoType.USER, "b")
        store.upsert("ielts-rule", "ielts only", MemoType.FEEDBACK, "b",
                     thread_scope="ielts")
        store.upsert("daily-rule", "daily only", MemoType.FEEDBACK, "b",
                     thread_scope="daily")
        names = sorted(m.name for m in store.list_in_scope(thread_id="ielts"))
        assert names == ["g", "ielts-rule"]

    def test_list_in_scope_user_and_thread(self, store):
        store.upsert("g", "global", MemoType.USER, "b")
        store.upsert("u", "alice only", MemoType.USER, "b", user_scope="alice")
        store.upsert("t", "ielts only", MemoType.FEEDBACK, "b",
                     thread_scope="ielts")
        store.upsert("ut", "alice in ielts", MemoType.FEEDBACK, "b",
                     user_scope="alice", thread_scope="ielts")
        names = sorted(
            m.name for m in store.list_in_scope(
                user_id="alice", thread_id="ielts",
            )
        )
        assert names == ["g", "t", "u", "ut"]


# ─── archive / unarchive ────────────────────────────────────────


class TestArchive:
    def test_archive_moves_to_subdir(self, store, tmp_path):
        store.upsert("tmp", "tmp memo", MemoType.USER, "b")
        r = store.archive("tmp")
        assert r.action == "archived"
        assert not (tmp_path / MEMOS_DIR_NAME / "tmp.md").exists()
        assert (tmp_path / MEMOS_DIR_NAME / ARCHIVE_DIR_NAME / "tmp.md").is_file()

    def test_unarchive(self, store, tmp_path):
        store.upsert("tmp", "tmp memo", MemoType.USER, "b")
        store.archive("tmp")
        store.unarchive("tmp")
        assert (tmp_path / MEMOS_DIR_NAME / "tmp.md").is_file()

    def test_archive_missing(self, store):
        with pytest.raises(StoreError, match="not found"):
            store.archive("ghost")

    def test_archive_collision(self, store):
        # Create, archive, recreate, then second archive must fail —
        # the archive slot is already occupied.
        store.upsert("tmp", "v1", MemoType.USER, "b")
        store.archive("tmp")
        store.upsert("tmp", "v2", MemoType.USER, "b")
        with pytest.raises(StoreError, match="archive slot"):
            store.archive("tmp")


# ─── load / delete ──────────────────────────────────────────────


class TestLoadDelete:
    def test_load_missing(self, store):
        assert store.load("ghost") is None

    def test_load_existing(self, store):
        store.upsert("x", "desc", MemoType.USER, "body")
        m = store.load("x")
        assert m is not None
        assert m.description == "desc"

    def test_delete(self, store, tmp_path):
        store.upsert("x", "d", MemoType.USER, "b")
        r = store.delete("x")
        assert r.action == "deleted"
        assert not (tmp_path / MEMOS_DIR_NAME / "x.md").exists()

    def test_delete_missing(self, store):
        with pytest.raises(StoreError, match="not found"):
            store.delete("ghost")


# ─── write_many (migration use) ─────────────────────────────────


class TestWriteMany:
    def test_bulk_write(self, store):
        results = store.write_many([
            {"name": "a", "description": "first", "memo_type": MemoType.USER,
             "body": "body a"},
            {"name": "b", "description": "second",
             "memo_type": MemoType.FEEDBACK, "body": "body b",
             "thread_scope": "ielts"},
        ])
        assert len(results) == 2
        assert all(r.action == "created" for r in results)
        loaded = store.list_all()
        assert {m.name for m in loaded} == {"a", "b"}


# ─── atomic write under failure ─────────────────────────────────


class TestAtomic:
    def test_existing_file_survives_render_failure(self, store, monkeypatch):
        # If render_memo raises, the existing file must not be clobbered.
        store.upsert("x", "v1", MemoType.USER, "body v1")
        # Attempt an invalid update — render_memo will raise.
        with pytest.raises(Exception):
            store.upsert("x", "", MemoType.USER, "body v2")  # blank desc
        # v1 still on disk.
        m = store.load("x")
        assert "v1" in m.body
