"""Tests for the /companion/upload-srt and /companion/list endpoints,
plus the subtitles endpoint's cache-first fallback behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services.cache_db import list_subtitles, set_arabic_srt


@pytest.fixture
def client() -> TestClient:
    # Function-scope so each test gets a fresh client with the isolated DB.
    return TestClient(app)


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


# ---- companion page ------------------------------------------------------


def test_companion_page_loads(client: TestClient) -> None:
    response = client.get("/companion")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Arabic by M.S" in response.text
    assert 'name="srt_file"' in response.text
    assert "Gemini status" in response.text
    assert "Search SubDL" in response.text
    assert "Search SubSource" in response.text


# ---- upload --------------------------------------------------------------


def test_upload_valid_srt_creates_db_record(client: TestClient) -> None:
    response = client.post(
        "/companion/upload-srt",
        data={
            "video_id": "tt1234567",
            "video_type": "movie",
            "release_name": "Test.Release",
        },
        files={"srt_file": ("english.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] >= 1
    assert payload["video_id"] == "tt1234567"
    assert payload["status"] == "uploaded"
    assert payload["arabic_srt_path"] is None
    assert payload["english_srt_path"].endswith(".srt")
    assert Path(payload["english_srt_path"]).exists()

    rows = list_subtitles(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["video_id"] == "tt1234567"
    assert rows[0]["english_srt_hash"] == payload["english_srt_hash"]


def test_upload_rejects_non_srt_extension(client: TestClient) -> None:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt1234567"},
        files={"srt_file": ("english.txt", VALID_SRT_BYTES, "text/plain")},
    )
    assert response.status_code == 400
    assert ".srt" in response.json()["detail"].lower()


def test_upload_rejects_empty_file(client: TestClient) -> None:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt1234567"},
        files={"srt_file": ("empty.srt", b"", "application/x-subrip")},
    )
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_upload_rejects_missing_timestamp(client: TestClient) -> None:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt1234567"},
        files={"srt_file": ("bad.srt", b"just text, no arrow", "application/x-subrip")},
    )
    assert response.status_code == 400
    assert "-->" in response.json()["detail"]


def test_upload_requires_video_id(client: TestClient) -> None:
    # video_id is a required Form field; FastAPI returns 422 when it's absent.
    response = client.post(
        "/companion/upload-srt",
        files={"srt_file": ("ok.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code in (400, 422)


# ---- list ----------------------------------------------------------------


def test_companion_list_returns_uploaded_records(client: TestClient) -> None:
    client.post(
        "/companion/upload-srt",
        data={"video_id": "tt5555555", "video_type": "series"},
        files={"srt_file": ("e.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    response = client.get("/companion/list")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["video_id"] == "tt5555555"
    assert items[0]["video_type"] == "series"
    assert "error_message" in items[0]


# ---- subtitles endpoint cache fallback ----------------------------------


def test_subtitles_falls_back_to_sample_when_no_arabic_cached(client: TestClient) -> None:
    """Uploading English doesn't populate Arabic, so we keep returning the sample."""
    client.post(
        "/companion/upload-srt",
        data={"video_id": "tt7777777"},
        files={"srt_file": ("e.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    response = client.get("/subtitles/movie/tt7777777.json")
    assert response.status_code == 200
    sub = response.json()["subtitles"][0]
    # Sample-shaped id (Phase 1 fallback), not cached-N.
    assert sub["id"].startswith("arabic-ms-")
    assert sub["name"] == "Arabic by M.S"


def test_subtitles_returns_cached_when_arabic_present(client: TestClient, tmp_path) -> None:
    """If a record has arabic_srt_path set, the subtitles endpoint must surface it."""
    arabic_file = tmp_path / "fake_arabic.srt"
    arabic_file.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nترجمة\n", encoding="utf-8"
    )

    upload = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt8888888"},
        files={"srt_file": ("e.srt", VALID_SRT_BYTES, "application/x-subrip")},
    ).json()
    set_arabic_srt(config.DB_PATH, upload["id"], str(arabic_file), status="translated")

    response = client.get("/subtitles/movie/tt8888888.json")
    sub = response.json()["subtitles"][0]
    assert sub["id"].startswith("cached-")

    # And the cached download endpoint should serve the file we wrote.
    dl = client.get(f"/download/{sub['id']}.srt")
    assert dl.status_code == 200
    assert "ترجمة" in dl.content.decode("utf-8")
