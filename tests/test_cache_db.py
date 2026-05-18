"""Tests for services/cache_db.py."""

from __future__ import annotations

import sqlite3

from backend import config
from services.cache_db import (
    clear_error_message,
    find_latest_arabic_for_video,
    get_record,
    init_db,
    insert_subtitle,
    list_subtitles,
    reset_translation_progress,
    set_failed,
    set_arabic_srt,
    set_translation_progress,
)


def test_init_db_creates_file() -> None:
    init_db(config.DB_PATH)
    assert config.DB_PATH.exists()


def test_insert_and_list_subtitles() -> None:
    rid = insert_subtitle(
        config.DB_PATH,
        video_id="tt1111111",
        video_type="movie",
        release_name="Test.Movie",
        english_srt_path="/tmp/x.srt",
        english_srt_hash="abc123",
        source_provider="subdl",
        source_subtitle_id="123",
        source_download_url="https://example.com/subtitle.zip",
    )
    assert rid >= 1

    rows = list_subtitles(config.DB_PATH)
    assert len(rows) == 1
    row = rows[0]
    assert row["video_id"] == "tt1111111"
    assert row["video_type"] == "movie"
    assert row["release_name"] == "Test.Movie"
    assert row["english_srt_path"] == "/tmp/x.srt"
    assert row["english_srt_hash"] == "abc123"
    assert row["arabic_srt_path"] is None
    assert row["timing_offset_ms"] is None
    assert row["user_note"] is None
    assert row["is_preferred"] == 0
    assert row["status"] == "uploaded"
    assert row["error_message"] is None
    assert row["source_provider"] == "subdl"
    assert row["source_subtitle_id"] == "123"
    assert row["source_download_url"] == "https://example.com/subtitle.zip"
    assert row["progress_total_chunks"] is None
    assert row["progress_done_chunks"] is None
    assert row["progress_message"] is None
    assert row["created_at"].endswith("Z")


def test_find_latest_arabic_only_returns_records_with_arabic() -> None:
    insert_subtitle(
        config.DB_PATH,
        video_id="tt2222222",
        video_type="movie",
        release_name=None,
        english_srt_path="/tmp/a.srt",
        english_srt_hash="aaa",
    )
    # No arabic yet → should not match.
    assert find_latest_arabic_for_video(config.DB_PATH, "tt2222222") is None

    rid = insert_subtitle(
        config.DB_PATH,
        video_id="tt2222222",
        video_type="movie",
        release_name=None,
        english_srt_path="/tmp/b.srt",
        english_srt_hash="bbb",
    )
    set_arabic_srt(config.DB_PATH, rid, "/tmp/b.ar.srt", status="translated")

    hit = find_latest_arabic_for_video(config.DB_PATH, "tt2222222")
    assert hit is not None
    assert hit["arabic_srt_path"] == "/tmp/b.ar.srt"
    assert hit["status"] == "translated"


def test_get_record_returns_dict_or_none() -> None:
    rid = insert_subtitle(
        config.DB_PATH,
        video_id="tt3333333",
        video_type="series",
        release_name=None,
        english_srt_path="/tmp/c.srt",
        english_srt_hash="ccc",
    )
    rec = get_record(config.DB_PATH, rid)
    assert rec is not None
    assert rec["video_id"] == "tt3333333"
    assert get_record(config.DB_PATH, 9999) is None


def test_init_db_adds_error_message_column_to_existing_db(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE subtitles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                video_type TEXT NOT NULL DEFAULT 'movie',
                release_name TEXT,
                english_srt_path TEXT NOT NULL,
                english_srt_hash TEXT NOT NULL,
                arabic_srt_path TEXT,
                status TEXT NOT NULL DEFAULT 'uploaded',
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()

    init_db(db_path)

    with sqlite3.connect(str(db_path)) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(subtitles)").fetchall()
        }
    assert "imdb_id" in columns
    assert "season" in columns
    assert "episode" in columns
    assert "canonical_video_key" in columns
    assert "timing_offset_ms" in columns
    assert "user_note" in columns
    assert "is_preferred" in columns
    assert "error_message" in columns
    assert "source_provider" in columns
    assert "source_subtitle_id" in columns
    assert "source_download_url" in columns
    assert "progress_total_chunks" in columns
    assert "progress_done_chunks" in columns
    assert "progress_message" in columns


def test_set_failed_and_clear_error_message() -> None:
    rid = insert_subtitle(
        config.DB_PATH,
        video_id="tt4444444",
        video_type="movie",
        release_name=None,
        english_srt_path="/tmp/d.srt",
        english_srt_hash="ddd",
    )

    set_failed(config.DB_PATH, rid, "Gemini request failed")
    failed = get_record(config.DB_PATH, rid)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error_message"] == "Gemini request failed"

    clear_error_message(config.DB_PATH, rid)
    cleared = get_record(config.DB_PATH, rid)
    assert cleared is not None
    assert cleared["error_message"] is None


def test_translation_progress_helpers() -> None:
    rid = insert_subtitle(
        config.DB_PATH,
        video_id="tt5555555",
        video_type="movie",
        release_name=None,
        english_srt_path="/tmp/e.srt",
        english_srt_hash="eee",
    )

    set_translation_progress(
        config.DB_PATH,
        rid,
        total_chunks=5,
        done_chunks=2,
        progress_message="Translated chunk 2 of 5.",
    )
    rec = get_record(config.DB_PATH, rid)
    assert rec is not None
    assert rec["status"] == "translating"
    assert rec["progress_total_chunks"] == 5
    assert rec["progress_done_chunks"] == 2
    assert rec["progress_message"] == "Translated chunk 2 of 5."

    reset_translation_progress(config.DB_PATH, rid, status="uploaded")
    reset = get_record(config.DB_PATH, rid)
    assert reset is not None
    assert reset["status"] == "uploaded"
    assert reset["progress_total_chunks"] is None
    assert reset["progress_done_chunks"] is None
    assert reset["progress_message"] is None


def test_insert_and_lookup_with_canonical_episode_key() -> None:
    rid = insert_subtitle(
        config.DB_PATH,
        video_id="tt6666666:1:5",
        imdb_id="tt6666666",
        season=1,
        episode=5,
        canonical_video_key="tt6666666:s01e05",
        video_type="series",
        release_name="Episode.Test",
        english_srt_path="/tmp/f.srt",
        english_srt_hash="fff",
    )
    set_arabic_srt(config.DB_PATH, rid, "/tmp/f.ar.srt", status="translated")

    hit = find_latest_arabic_for_video(
        config.DB_PATH,
        "tt6666666:1:5",
        canonical_video_key="tt6666666:s01e05",
    )
    assert hit is not None
    assert hit["canonical_video_key"] == "tt6666666:s01e05"
    assert hit["season"] == 1
    assert hit["episode"] == 5
