"""Tests for Phase 11 Stremio-facing real-vs-status subtitle behavior."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, job_manager


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


def _upload_record(client: TestClient, video_id: str) -> dict:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": video_id, "video_type": "movie"},
        files={"srt_file": ("english.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_subtitles_returns_real_arabic_when_translated_file_exists(client: TestClient, tmp_path: Path) -> None:
    uploaded = _upload_record(client, "tt5511001")
    arabic_file = tmp_path / "translated.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nترجمة جاهزة\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, int(uploaded["id"]), str(arabic_file), status="translated")

    response = client.get("/subtitles/movie/tt5511001.json")
    assert response.status_code == 200
    item = response.json()["subtitles"][0]
    assert item["name"] == "Arabic by M.S"
    assert item["url"].startswith("http://testserver/download/cached-")


def test_subtitles_returns_status_when_no_record_exists(client: TestClient) -> None:
    response = client.get("/subtitles/movie/tt5511002.json")
    assert response.status_code == 200
    item = response.json()["subtitles"][0]
    assert item["name"] == "Arabic by M.S - Status"
    assert item["lang"] == "ara"
    assert item["url"] == "http://testserver/status-subtitle/tt5511002.srt"


def test_subtitles_returns_uploaded_not_translated_status_when_english_exists_but_arabic_missing(client: TestClient) -> None:
    _upload_record(client, "tt5511003")

    response = client.get("/status-subtitle/tt5511003.srt")
    assert response.status_code == 200
    body = response.text
    assert "تم العثور على ملف ترجمة إنجليزي" in body


def test_subtitles_returns_failed_status_when_latest_record_failed(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt5511004")
    cache_db.set_failed(config.DB_PATH, int(uploaded["id"]), "boom")

    response = client.get("/status-subtitle/tt5511004.srt")
    assert response.status_code == 200
    assert "فشلت آخر محاولة ترجمة" in response.text


def test_subtitles_returns_translating_status_when_background_job_running(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt5511005")
    release_job = threading.Event()

    def fake_translate(db_path, record_id, *, arabic_cache_dir, force):
        cache_db.set_translation_progress(
            db_path,
            record_id,
            total_chunks=3,
            done_chunks=1,
            progress_message="Translating chunk 1 of 3.",
        )
        release_job.wait(timeout=1.0)
        return cache_db.get_record(db_path, record_id) or {}

    job_manager.start_translation_job(
        record_id=int(uploaded["id"]),
        force=False,
        db_path=config.DB_PATH,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        translate_fn=fake_translate,
    )
    response = client.get("/status-subtitle/tt5511005.srt")
    release_job.set()
    job_manager.wait_for_all()

    assert response.status_code == 200
    assert "الترجمة قيد المعالجة" in response.text


def test_status_subtitle_endpoint_returns_valid_srt(client: TestClient) -> None:
    response = client.get("/status-subtitle/tt5511006.srt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-subrip")
    body = response.text
    assert body.startswith("1\n00:00:01,000 --> 00:00:08,000\n")
    assert "لا توجد ترجمة عربية جاهزة" in body


def test_status_srt_has_no_html_tags_or_markdown_fences(client: TestClient) -> None:
    response = client.get("/status-subtitle/tt5511007.srt")
    body = response.text
    assert "```" not in body
    assert "<b>" not in body
    assert "<p>" not in body


def test_sample_arabic_is_not_used_as_normal_subtitles_fallback(client: TestClient) -> None:
    response = client.get("/subtitles/movie/tt5511008.json")
    item = response.json()["subtitles"][0]
    assert item["name"] == "Arabic by M.S - Status"
    assert "/download/arabic-ms-" not in item["url"]


def test_diagnostics_includes_status_subtitle_ready(client: TestClient) -> None:
    response = client.get("/companion/diagnostics")
    assert response.status_code == 200
    assert response.json()["status_subtitle_ready"] is True
