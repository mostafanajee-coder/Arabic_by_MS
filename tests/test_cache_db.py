"""Tests for services/cache_db.py."""

from __future__ import annotations

from backend import config
from services.cache_db import (
    find_latest_arabic_for_video,
    get_record,
    init_db,
    insert_subtitle,
    list_subtitles,
    set_arabic_srt,
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
    assert row["status"] == "uploaded"
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
