"""Tests for Phase 14 one-click prepare workflow."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, job_manager, prepare_service, provider_router


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


def _upload_record(client: TestClient, video_id: str, *, video_type: str = "movie") -> dict:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": video_id, "video_type": video_type},
        files={"srt_file": ("english.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _translated_record(
    client: TestClient,
    tmp_path: Path,
    video_id: str,
    *,
    video_type: str = "movie",
    arabic_text: str = "ترجمة جاهزة",
) -> dict:
    uploaded = _upload_record(client, video_id, video_type=video_type)
    arabic_file = tmp_path / "{0}.ar.srt".format(uploaded["id"])
    arabic_file.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n{0}\n".format(arabic_text),
        encoding="utf-8",
    )
    cache_db.set_arabic_srt(config.DB_PATH, int(uploaded["id"]), str(arabic_file), status="translated")
    return uploaded


def _configured_provider_status() -> dict:
    return {
        "gemini": {"configured": True},
        "subdl": {"configured": True, "message": "ok"},
        "subsource": {"configured": False, "message": "missing"},
    }


def test_prepare_returns_already_ready_when_arabic_exists(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    translated = _translated_record(client, tmp_path, "tt1400001")
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    response = client.post("/companion/prepare", json={"video_id": "tt1400001"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "already_ready"
    assert payload["record_id"] == translated["id"]


def test_prepare_returns_gemini_missing_when_key_missing(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)

    response = client.post("/companion/prepare", json={"video_id": "tt1400002"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "gemini_missing"


def test_prepare_returns_provider_missing_when_no_provider_configured(client: TestClient) -> None:
    response = client.post("/companion/prepare", json={"video_id": "tt1400003"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "provider_missing"


def test_prepare_returns_no_results_when_providers_find_nothing(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {"items": [], "provider_errors": {}, "searched_providers": ["subdl"]},
    )

    response = client.post("/companion/prepare", json={"video_id": "tt1400004"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "no_results"


def test_prepare_imports_best_result_and_starts_background_job(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "best-1",
                    "language": "EN",
                    "release_name": "Best.Movie.1080p",
                    "download_url": "https://subdl.local/best.zip",
                    "score": 91.5,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-prepare-1", "record_id": kwargs["record_id"], "status": "queued"},
    )

    response = client.post("/companion/prepare", json={"video_id": "tt1400005"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "started"
    assert payload["record_id"] is not None
    assert payload["job_id"] == "job-prepare-1"
    assert payload["provider"] == "subdl"
    stored = cache_db.get_record(config.DB_PATH, int(payload["record_id"]))
    assert stored is not None
    assert stored["source_provider"] == "subdl"


def test_prepare_handles_episode_canonical_identity(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subsource",
                    "subtitle_id": "ep-1",
                    "language": "en",
                    "release_name": "Show.S01E05.1080p.WEB-DL",
                    "download_url": "https://subsource.local/ep.zip",
                    "score": 88.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subsource"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-prepare-2", "record_id": kwargs["record_id"], "status": "queued"},
    )

    response = client.post("/companion/prepare", json={"video_id": "tt1400006:1:5", "video_type": "series"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["canonical_video_key"] == "tt1400006:s01e05"
    stored = cache_db.get_record(config.DB_PATH, int(payload["record_id"]))
    assert stored is not None
    assert stored["canonical_video_key"] == "tt1400006:s01e05"


def test_prepare_status_returns_arabic_ready(client: TestClient, tmp_path: Path) -> None:
    _translated_record(client, tmp_path, "tt1400007:1:5", video_type="series")

    response = client.get("/companion/prepare-status/tt1400007:s01e05")
    assert response.status_code == 200
    payload = response.json()
    assert payload["arabic_ready"] is True
    assert payload["record"]["canonical_video_key"] == "tt1400007:s01e05"


def test_prepare_status_returns_active_job(client: TestClient, monkeypatch) -> None:
    uploaded = _upload_record(client, "tt1400008:1:5", video_type="series")
    release = threading.Event()

    def fake_translate(db_path, record_id, *, arabic_cache_dir, force):
        cache_db.set_translation_progress(
            db_path,
            record_id,
            total_chunks=3,
            done_chunks=1,
            progress_message="Translating chunk 1 of 3.",
        )
        release.wait(timeout=2.0)
        return cache_db.get_record(db_path, record_id) or {}

    job_manager.start_translation_job(
        record_id=int(uploaded["id"]),
        force=False,
        db_path=config.DB_PATH,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        translate_fn=fake_translate,
    )
    try:
        response = client.get("/companion/prepare-status/tt1400008:s01e05")
        assert response.status_code == 200
        payload = response.json()
        assert payload["active_job"] is not None
        assert payload["active_job"]["status"] in ("queued", "running")
    finally:
        release.set()
        job_manager.wait_for_all()


def test_prepare_status_returns_latest_failure(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1400009:1:5", video_type="series")
    cache_db.set_failed(config.DB_PATH, int(uploaded["id"]), "prepare failed later")

    response = client.get("/companion/prepare-status/tt1400009:s01e05")
    assert response.status_code == 200
    assert response.json()["latest_error"] == "prepare failed later"


def test_auto_prepare_disabled_by_default(client: TestClient, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_request_prepare(**kwargs):
        calls["count"] += 1
        return {"status": "started"}

    monkeypatch.setattr(prepare_service, "request_prepare", fake_request_prepare)
    response = client.get("/subtitles/movie/tt1410010.json")
    assert response.status_code == 200
    assert config.is_auto_prepare_on_subtitles_request_enabled() is False
    assert calls["count"] == 0


def test_auto_prepare_enabled_starts_prepare_from_subtitles_without_blocking(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_PREPARE_ON_SUBTITLES_REQUEST", "true")
    calls = {"count": 0}

    def fake_request_prepare(**kwargs):
        calls["count"] += 1
        return {"status": "started", "canonical_video_key": "tt1410011"}

    monkeypatch.setattr(prepare_service, "request_prepare", fake_request_prepare)

    start = time.time()
    response = client.get("/subtitles/movie/tt1410011.json")
    elapsed = time.time() - start
    assert response.status_code == 200
    assert response.json()["subtitles"][0]["name"] == "Arabic by M.S - Status"
    assert calls["count"] == 1
    assert elapsed < 1.0


def test_duplicate_prepare_job_prevention(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "subtitles.db"
    english_dir = tmp_path / "english"
    arabic_dir = tmp_path / "arabic"
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)

    entered = threading.Event()
    release = threading.Event()
    calls = {"search": 0}

    def fake_search_all_subtitles(**kwargs):
        calls["search"] += 1
        entered.set()
        release.wait(timeout=2.0)
        return {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "dupe-1",
                    "language": "EN",
                    "release_name": "Duplicate.Movie",
                    "download_url": "https://subdl.local/dupe.zip",
                    "score": 95.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        }

    monkeypatch.setattr(provider_router, "search_all_subtitles", fake_search_all_subtitles)
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-dup-1", "record_id": kwargs["record_id"], "status": "queued"},
    )

    first = prepare_service.request_prepare(
        video_id="tt1410012",
        db_path=db_path,
        english_cache_dir=english_dir,
        arabic_cache_dir=arabic_dir,
        run_async=True,
    )
    assert first["status"] == "started"
    assert entered.wait(timeout=2.0)

    second = prepare_service.request_prepare(
        video_id="tt1410012",
        db_path=db_path,
        english_cache_dir=english_dir,
        arabic_cache_dir=arabic_dir,
        run_async=True,
    )
    assert second["status"] == "already_running"
    assert calls["search"] == 1

    release.set()
    prepare_service.reset_for_tests()


def test_diagnostics_include_prepare_flags(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_PREPARE_ON_SUBTITLES_REQUEST", "true")
    response = client.get("/companion/diagnostics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["prepare_service_ready"] is True
    assert payload["auto_prepare_enabled"] is True
