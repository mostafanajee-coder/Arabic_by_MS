"""Tests for Phase 15 usage guardrails and duplicate-cost prevention."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service, job_manager, prepare_service, provider_router, usage_guard


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


def _upload_record(client: TestClient, video_id: str, *, content: bytes = VALID_SRT_BYTES) -> dict:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": video_id, "video_type": "movie"},
        files={"srt_file": ("english.srt", content, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fake_translation_reply(prompt: str) -> str:
    out_lines = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        idx, _, body = stripped.partition(")")
        if body:
            out_lines.append("{0}) ترجمة: {1}".format(idx.strip(), body.strip()))
    return "\n".join(out_lines)


def _configured_provider_status() -> dict:
    return {
        "gemini": {"configured": True},
        "subdl": {"configured": True, "message": "ok"},
        "subsource": {"configured": True, "message": "ok"},
    }


def _build_long_srt(num_entries: int) -> bytes:
    blocks = []
    for index in range(1, num_entries + 1):
        blocks.append(
            "{0}\n00:00:{1:02d},000 --> 00:00:{2:02d},000\nLine {0}\n".format(
                index,
                index,
                index + 1,
            )
        )
    return ("\n".join(blocks)).encode("utf-8")


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        response = client.get(f"/companion/job-status/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish in time: {last}")


def test_usage_event_table_creation_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "subtitles.db"
    columns = set(usage_guard.get_usage_table_columns(db_path))
    assert columns == {
        "id",
        "event_type",
        "provider",
        "canonical_video_key",
        "record_id",
        "job_id",
        "units",
        "details",
        "created_at",
    }


def test_usage_status_default_limits(client: TestClient) -> None:
    response = client.get("/companion/usage-status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["gemini_translations_limit"] == 20
    assert payload["provider_searches_limit"] == 100
    assert payload["prepare_requests_limit"] == 50
    assert payload["auto_prepare_enabled"] is False
    assert payload["allow_auto_prepare_when_limited"] is False


def test_recording_provider_search_event(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {"items": [], "provider_errors": {}, "searched_providers": ["subdl"]},
    )

    response = client.get("/companion/search-all?video_id=tt1500001")
    assert response.status_code == 200
    events = client.get("/companion/usage-events").json()["items"]
    assert events[0]["event_type"] == usage_guard.EVENT_PROVIDER_SEARCH
    assert events[0]["provider"] == "all"


def test_recording_gemini_translation_event(client: TestClient, monkeypatch) -> None:
    uploaded = _upload_record(client, "tt1500002")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    response = client.post(f"/companion/translate/{uploaded['id']}")
    assert response.status_code == 200, response.text

    events = client.get("/companion/usage-events").json()["items"]
    assert any(event["event_type"] == usage_guard.EVENT_GEMINI_TRANSLATE_SYNC for event in events)


def test_limit_exceeded_for_gemini_sync_translation(client: TestClient, monkeypatch) -> None:
    first = _upload_record(client, "tt1500003")
    second = _upload_record(client, "tt1500004")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("MAX_DAILY_GEMINI_TRANSLATIONS", "1")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    ok = client.post(f"/companion/translate/{first['id']}")
    assert ok.status_code == 200, ok.text

    blocked = client.post(f"/companion/translate/{second['id']}")
    assert blocked.status_code == 200, blocked.text
    payload = blocked.json()
    assert payload["status"] == "limit_exceeded"
    assert payload["limit_name"] == usage_guard.LIMIT_GEMINI_TRANSLATIONS


def test_limit_exceeded_for_background_translation(client: TestClient, monkeypatch) -> None:
    first = _upload_record(client, "tt1500005")
    second = _upload_record(client, "tt1500006")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("MAX_DAILY_GEMINI_TRANSLATIONS", "1")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    started = client.post(f"/companion/translate-background/{first['id']}")
    assert started.status_code == 200, started.text
    _wait_for_job(client, started.json()["job_id"])

    blocked = client.post(f"/companion/translate-background/{second['id']}")
    assert blocked.status_code == 200, blocked.text
    payload = blocked.json()
    assert payload["status"] == "limit_exceeded"
    assert payload["limit_name"] == usage_guard.LIMIT_GEMINI_TRANSLATIONS


def test_limit_exceeded_for_prepare_request(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("MAX_DAILY_PREPARE_REQUESTS", "1")
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {"items": [], "provider_errors": {}, "searched_providers": ["subdl"]},
    )

    first = client.post("/companion/prepare", json={"video_id": "tt1500007"})
    assert first.status_code == 200, first.text

    second = client.post("/companion/prepare", json={"video_id": "tt1500008"})
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["status"] == "limit_exceeded"
    assert payload["limit_name"] == usage_guard.LIMIT_PREPARE_REQUESTS


def test_manual_upload_not_blocked_by_usage_limits(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_DAILY_GEMINI_TRANSLATIONS", "1")
    monkeypatch.setenv("MAX_DAILY_PROVIDER_SEARCHES", "1")
    monkeypatch.setenv("MAX_DAILY_PREPARE_REQUESTS", "1")
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_GEMINI_TRANSLATE_SYNC)
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PROVIDER_SEARCH)
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PREPARE_REQUEST)

    response = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt1500009", "video_type": "movie"},
        files={"srt_file": ("manual.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text


def test_prepare_already_ready_does_not_consume_provider_or_gemini_quota(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    uploaded = _upload_record(client, "tt1500010:1:5")
    arabic_file = tmp_path / "ready.ar.srt"
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nجاهز\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, int(uploaded["id"]), str(arabic_file), status="translated")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)

    response = client.post("/companion/prepare", json={"video_id": "tt1500010:1:5", "video_type": "series"})
    assert response.status_code == 200
    assert response.json()["status"] == "already_ready"

    usage = client.get("/companion/usage-status").json()
    assert usage["gemini_translations_used"] == 0
    assert usage["provider_searches_used"] == 0
    assert usage["prepare_requests_used"] == 0
    events = client.get("/companion/usage-events").json()["items"]
    assert events[0]["event_type"] == usage_guard.EVENT_PREPARE_SKIPPED_ALREADY_READY


def test_duplicate_background_job_reuse_does_not_consume_extra_gemini_quota(
    client: TestClient,
    monkeypatch,
) -> None:
    uploaded = _upload_record(client, "tt1500011", content=_build_long_srt(25))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    entered = threading.Event()
    release = threading.Event()

    def blocking_reply(prompt: str) -> str:
        entered.set()
        release.wait(timeout=2.0)
        return _fake_translation_reply(prompt)

    monkeypatch.setattr(gemini_service, "generate", blocking_reply)

    first = client.post(f"/companion/translate-background/{uploaded['id']}")
    assert first.status_code == 200, first.text
    assert entered.wait(timeout=2.0)

    second = client.post(f"/companion/translate-background/{uploaded['id']}")
    assert second.status_code == 200, second.text
    assert first.json()["job_id"] == second.json()["job_id"]

    release.set()
    _wait_for_job(client, first.json()["job_id"])

    events = client.get("/companion/usage-events?limit=10").json()["items"]
    gemini_events = [event for event in events if event["event_type"] == usage_guard.EVENT_GEMINI_TRANSLATE_BACKGROUND]
    duplicate_events = [event for event in events if event["event_type"] == usage_guard.EVENT_DUPLICATE_JOB_REUSED]
    assert len(gemini_events) == 1
    assert len(duplicate_events) == 1


def test_auto_prepare_disabled_by_default_still_safe(client: TestClient) -> None:
    response = client.get("/subtitles/movie/tt1500012.json")
    assert response.status_code == 200
    events = client.get("/companion/usage-events").json()["items"]
    assert events == []


def test_auto_prepare_limit_exceeded_does_not_break_subtitles(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_PREPARE_ON_SUBTITLES_REQUEST", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("MAX_DAILY_PREPARE_REQUESTS", "1")
    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PREPARE_REQUEST)
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Provider search should not run when limited")),
    )

    response = client.get("/subtitles/movie/tt1500013.json")
    assert response.status_code == 200
    payload = response.json()
    assert payload["subtitles"][0]["name"] == "Arabic by M.S - Status"


def test_usage_events_returns_latest_events(client: TestClient) -> None:
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PROVIDER_SEARCH, provider="subdl")
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PREPARE_REQUEST, canonical_video_key="tt1500014")

    response = client.get("/companion/usage-events?limit=2")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["event_type"] == usage_guard.EVENT_PREPARE_REQUEST
    assert items[1]["event_type"] == usage_guard.EVENT_PROVIDER_SEARCH


def test_clear_usage_events_clears_only_usage_events(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1500015")
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PROVIDER_SEARCH)

    response = client.post("/companion/clear-usage-events")
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert client.get("/companion/usage-events").json()["items"] == []
    assert cache_db.get_record(config.DB_PATH, int(uploaded["id"])) is not None


def test_diagnostics_include_usage_guard_and_counts(client: TestClient) -> None:
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PROVIDER_SEARCH)
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_GEMINI_TRANSLATE_SYNC)

    response = client.get("/companion/diagnostics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["usage_guard_ready"] is True
    assert payload["max_daily_gemini_translations"] == 20
    assert payload["max_daily_provider_searches"] == 100
    assert payload["max_daily_prepare_requests"] == 50
    assert payload["today_provider_searches_used"] == 1
    assert payload["today_gemini_translations_used"] == 1
