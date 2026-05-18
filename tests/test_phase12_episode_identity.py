"""Tests for Phase 12 episode-aware Stremio identity handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, provider_router
from utils.stremio_id import build_canonical_video_key, parse_stremio_video_id


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_parse_movie_stremio_id() -> None:
    parsed = parse_stremio_video_id("tt1234567")
    assert parsed["imdb_id"] == "tt1234567"
    assert parsed["season"] is None
    assert parsed["episode"] is None
    assert parsed["canonical_video_key"] == "tt1234567"
    assert parsed["is_episode"] is False


def test_parse_series_episode_stremio_id() -> None:
    parsed = parse_stremio_video_id("tt1234567:1:5")
    assert parsed["imdb_id"] == "tt1234567"
    assert parsed["season"] == 1
    assert parsed["episode"] == 5
    assert parsed["canonical_video_key"] == "tt1234567:s01e05"
    assert parsed["is_episode"] is True


def test_canonical_key_formatting() -> None:
    assert build_canonical_video_key("tt1234567", 1, 5) == "tt1234567:s01e05"
    assert build_canonical_video_key("tt1234567") == "tt1234567"


def test_invalid_stremio_id_handled_safely() -> None:
    parsed = parse_stremio_video_id("not-an-imdb-id:abc:xyz:extra")
    assert parsed["imdb_id"] == "not-an-imdb-id"
    assert parsed["season"] is None
    assert parsed["episode"] is None
    assert parsed["canonical_video_key"] == "not-an-imdb-id"
    assert parsed["is_episode"] is False


def test_manual_upload_with_episode_video_id_stores_canonical_key(client: TestClient) -> None:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt1234567:1:5", "video_type": "series"},
        files={"srt_file": ("episode.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["imdb_id"] == "tt1234567"
    assert payload["season"] == 1
    assert payload["episode"] == 5
    assert payload["canonical_video_key"] == "tt1234567:s01e05"


def test_manual_upload_with_separate_season_episode_stores_canonical_key(client: TestClient) -> None:
    response = client.post(
        "/companion/upload-srt",
        data={
            "video_id": "tt1234567",
            "video_type": "series",
            "season": 1,
            "episode": 5,
        },
        files={"srt_file": ("episode.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["canonical_video_key"] == "tt1234567:s01e05"

    listing = client.get("/companion/list")
    item = listing.json()["items"][0]
    assert item["imdb_id"] == "tt1234567"
    assert item["season"] == 1
    assert item["episode"] == 5
    assert item["canonical_video_key"] == "tt1234567:s01e05"


def test_subdl_import_stores_canonical_metadata(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    response = client.post(
        "/companion/import-subdl",
        json={
            "video_id": "tt9992001:1:5",
            "video_type": "series",
            "download_url": "https://subdl.local/episode.zip",
        },
    )
    assert response.status_code == 200, response.text
    record = cache_db.get_record(config.DB_PATH, response.json()["id"])
    assert record is not None
    assert record["imdb_id"] == "tt9992001"
    assert record["season"] == 1
    assert record["episode"] == 5
    assert record["canonical_video_key"] == "tt9992001:s01e05"


def test_subsource_import_stores_canonical_metadata(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    response = client.post(
        "/companion/import-subsource",
        json={
            "video_id": "tt9992002",
            "video_type": "series",
            "season": 2,
            "episode": 7,
            "download_url": "https://subsource.local/episode.zip",
        },
    )
    assert response.status_code == 200, response.text
    record = cache_db.get_record(config.DB_PATH, response.json()["id"])
    assert record is not None
    assert record["imdb_id"] == "tt9992002"
    assert record["season"] == 2
    assert record["episode"] == 7
    assert record["canonical_video_key"] == "tt9992002:s02e07"


def test_import_best_stores_canonical_metadata(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "best-episode",
                    "language": "EN",
                    "release_name": "Show.S01E05.1080p.WEB-DL",
                    "download_url": "https://subdl.local/best-episode.zip",
                    "score": 95.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    response = client.post(
        "/companion/import-best",
        json={"video_id": "tt9992003:1:5", "video_type": "series"},
    )
    assert response.status_code == 200, response.text
    record = cache_db.get_record(config.DB_PATH, response.json()["record_id"])
    assert record is not None
    assert record["canonical_video_key"] == "tt9992003:s01e05"


def test_subtitles_returns_exact_episode_arabic_only(client: TestClient, tmp_path: Path) -> None:
    uploaded = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt7777001:1:5", "video_type": "series"},
        files={"srt_file": ("episode.srt", VALID_SRT_BYTES, "application/x-subrip")},
    ).json()
    arabic_file = tmp_path / "episode-5.ar.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nالحلقة 5\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, uploaded["id"], str(arabic_file), status="translated")

    response = client.get("/subtitles/series/tt7777001:1:5.json")
    assert response.status_code == 200
    subtitle = response.json()["subtitles"][0]
    assert subtitle["name"] == "Arabic by M.S"
    download = client.get(subtitle["url"].replace("http://testserver", ""))
    assert "الحلقة 5" in download.text


def test_subtitles_does_not_return_episode_five_for_episode_six(client: TestClient, tmp_path: Path) -> None:
    uploaded = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt7777002:1:5", "video_type": "series"},
        files={"srt_file": ("episode.srt", VALID_SRT_BYTES, "application/x-subrip")},
    ).json()
    arabic_file = tmp_path / "episode-5.ar.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nالحلقة 5\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, uploaded["id"], str(arabic_file), status="translated")

    response = client.get("/subtitles/series/tt7777002:1:6.json")
    assert response.status_code == 200
    subtitle = response.json()["subtitles"][0]
    assert subtitle["name"] == "Arabic by M.S - Status"
    assert "/status-subtitle/" in subtitle["url"]


def test_subtitles_movie_behavior_still_works(client: TestClient, tmp_path: Path) -> None:
    uploaded = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt7777003", "video_type": "movie"},
        files={"srt_file": ("movie.srt", VALID_SRT_BYTES, "application/x-subrip")},
    ).json()
    arabic_file = tmp_path / "movie.ar.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nفيلم\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, uploaded["id"], str(arabic_file), status="translated")

    response = client.get("/subtitles/movie/tt7777003.json")
    assert response.status_code == 200
    assert response.json()["subtitles"][0]["name"] == "Arabic by M.S"


def test_status_subtitle_for_episode_includes_season_episode_info(client: TestClient) -> None:
    client.post(
        "/companion/upload-srt",
        data={"video_id": "tt7777004:2:7", "video_type": "series"},
        files={"srt_file": ("episode.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )

    response = client.get("/status-subtitle/tt7777004:2:7.srt")
    assert response.status_code == 200
    assert "الموسم 2 الحلقة 7" in response.text


def test_legacy_video_id_fallback_still_works_for_old_episode_records(client: TestClient, tmp_path: Path) -> None:
    record_id = cache_db.insert_subtitle(
        config.DB_PATH,
        video_id="tt7777005:1:5",
        video_type="series",
        release_name="Legacy.Episode",
        english_srt_path=str(tmp_path / "legacy.srt"),
        english_srt_hash="legacyhash",
    )
    arabic_file = tmp_path / "legacy.ar.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nقديم\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, record_id, str(arabic_file), status="translated")

    response = client.get("/subtitles/series/tt7777005:1:5.json")
    assert response.status_code == 200
    assert response.json()["subtitles"][0]["name"] == "Arabic by M.S"


def test_diagnostics_includes_stremio_id_parser_ready(client: TestClient) -> None:
    response = client.get("/companion/diagnostics")
    assert response.status_code == 200
    assert response.json()["stremio_id_parser_ready"] is True
