"""Tests for Phase 9 local Stremio integration hardening endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service


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


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["app"] == "Arabic by M.S"
    assert payload["version"] == "0.17.0"
    assert payload["status"] == "ok"
    assert payload["cache_db_ready"] is True
    assert payload["cache_dirs_ready"] is True
    assert payload["status_subtitle_ready"] is True


def test_install_info_endpoint(client: TestClient) -> None:
    resp = client.get("/companion/install-info")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["addon_name"] == "Arabic by M.S"
    assert payload["version"] == "0.17.0"
    assert payload["manifest_url"].endswith("/manifest.json")
    assert payload["companion_url"].endswith("/companion")
    assert payload["base_url"] == "http://testserver"


def test_dynamic_base_url_behavior_in_subtitles() -> None:
    client = TestClient(app, base_url="http://localhost:9999")
    resp = client.get("/subtitles/movie/tt1234567.json")
    assert resp.status_code == 200
    item = resp.json()["subtitles"][0]
    assert item["url"].startswith("http://localhost:9999/status-subtitle/")


def test_companion_diagnostics(client: TestClient) -> None:
    resp = client.get("/companion/diagnostics")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["python_app_import_ok"] is True
    assert payload["cache_db_ready"] is True
    assert payload["cache_english_dir_ready"] is True
    assert payload["cache_arabic_dir_ready"] is True
    assert payload["sample_arabic_exists"] is True
    assert payload["manifest_ok"] is True
    assert payload["subtitles_route_ok"] is True
    assert payload["job_manager_ready"] is True
    assert payload["active_translation_jobs"] == 0
    assert payload["active_batch_jobs"] == 0
    assert payload["status_subtitle_ready"] is True
    assert payload["stremio_id_parser_ready"] is True
    assert payload["srt_timing_ready"] is True
    assert payload["preferred_record_ready"] is True
    assert payload["prepare_service_ready"] is True
    assert payload["batch_prepare_ready"] is True
    assert payload["auto_prepare_enabled"] is False
    assert payload["usage_guard_ready"] is True
    assert payload["max_daily_gemini_translations"] == 20
    assert payload["max_daily_provider_searches"] == 100
    assert payload["max_daily_prepare_requests"] == 50
    assert payload["today_batch_prepare_requests_used"] == 0


def test_companion_test_gemini_missing_key(client: TestClient) -> None:
    resp = client.post("/companion/test-gemini")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["configured"] is False
    assert payload["success"] is False
    assert "GEMINI_API_KEY" in payload["message"]


def test_companion_test_gemini_mocked_success(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "generate", lambda prompt: "مرحبا")

    resp = client.post("/companion/test-gemini")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["configured"] is True
    assert payload["success"] is True
    assert payload["reply"] == "مرحبا"


def test_companion_self_test(client: TestClient) -> None:
    resp = client.post("/companion/self-test")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["db_ready"] is True
    assert payload["cache_dirs_ready"] is True
    assert payload["created_record"] is True
    assert payload["translation_status_ok"] is True
    assert payload["cleanup_performed"] is True
    assert payload["gemini_called"] is False


def test_cached_subtitle_url_uses_active_request_host(client: TestClient, tmp_path) -> None:
    uploaded = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt1110099"},
        files={"srt_file": ("e.srt", VALID_SRT_BYTES, "application/x-subrip")},
    ).json()
    arabic_file = tmp_path / "cached_arabic.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nترجمة\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, uploaded["id"], str(arabic_file), status="translated")

    custom_client = TestClient(app, base_url="http://127.0.0.1:8787")
    resp = custom_client.get("/subtitles/movie/tt1110099.json")
    assert resp.status_code == 200
    assert resp.json()["subtitles"][0]["url"].startswith("http://127.0.0.1:8787/download/")
