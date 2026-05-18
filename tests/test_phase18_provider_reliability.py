"""Tests for Phase 18 provider reliability and diagnostics."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import (
    batch_prepare_service,
    provider_reliability,
    provider_router,
    subdl_service,
    subsource_service,
    usage_guard,
)


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        text: str = "",
        content: bytes = b"",
        headers=None,
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON payload")
        return self._json_data


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_for_batch(client: TestClient, batch_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        response = client.get(f"/companion/batch-status/{batch_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in ("completed", "partial", "failed", "cancelled"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"Batch {batch_id} did not finish in time: {last}")


def test_subdl_search_retries_transient_http_and_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if len(calls) == 1:
            return FakeResponse(status_code=503, text="temporary outage")
        return FakeResponse(
            json_data={
                "status": True,
                "results": [{"imdb_id": "tt1375666", "type": "movie", "name": "Inception"}],
                "subtitles": [
                    {
                        "id": 3197651,
                        "url": 3213944,
                        "language": "EN",
                        "release_name": "Inception.2010.1080p.BluRay.x264",
                    }
                ],
            }
        )

    monkeypatch.setattr(subdl_service.httpx, "get", fake_get)

    items = subdl_service.search_subtitles(video_id="tt1375666")

    assert len(items) == 1
    assert items[0]["provider"] == "subdl"
    assert len(calls) == 2


def test_subdl_download_retries_transient_http_and_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        calls.append({"url": url, "follow_redirects": follow_redirects})
        if len(calls) == 1:
            return FakeResponse(status_code=504, text="gateway timeout")
        return FakeResponse(
            content=VALID_SRT_BYTES,
            headers={"content-type": "application/x-subrip"},
        )

    monkeypatch.setattr(subdl_service.httpx, "get", fake_get)

    content = subdl_service.download_subtitle_data("https://dl.subdl.com/subtitle/sample.srt")

    assert content == VALID_SRT_BYTES
    assert len(calls) == 2


def test_subdl_search_does_not_retry_bad_request(monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(status_code=400, text="bad request")

    monkeypatch.setattr(subdl_service.httpx, "get", fake_get)

    with pytest.raises(subdl_service.SubDLError) as exc_info:
        subdl_service.search_subtitles(video_id="tt1375666")

    assert len(calls) == 1
    assert getattr(exc_info.value, "http_status", None) == 400
    assert getattr(exc_info.value, "error_type", None) == "bad_request"


def test_subdl_missing_config_does_not_attempt_http(monkeypatch) -> None:
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse(status_code=200, json_data={})

    monkeypatch.setattr(subdl_service.httpx, "get", fake_get)

    with pytest.raises(subdl_service.SubDLNotConfiguredError) as exc_info:
        subdl_service.search_subtitles(video_id="tt1375666")

    assert "SUBDL_API_KEY" in str(exc_info.value)
    assert calls == []


def test_search_all_returns_structured_provider_errors(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-key")
    monkeypatch.setattr(
        subdl_service,
        "search_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(
            provider_reliability.make_provider_error(
                subdl_service.SubDLError,
                provider="subdl",
                operation="search",
                message="SubDL search is temporarily unavailable (HTTP 503).",
                error_type="transient_http_error",
                http_status=503,
                retryable=True,
            )
        ),
    )
    monkeypatch.setattr(
        subsource_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subsource",
                "subtitle_id": "ss-1",
                "language": "en",
                "release_name": "Movie.1080p.WEB-DL",
                "download_url": "https://subsource.local/ok.srt",
                "score": 80.0,
            }
        ],
    )

    response = client.get("/companion/search-all", params={"video_id": "tt1375666"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"][0]["provider"] == "subsource"
    assert payload["provider_errors"]["subdl"] == {
        "provider": "subdl",
        "error_type": "transient_http_error",
        "http_status": 503,
        "message": "SubDL search is temporarily unavailable (HTTP 503).",
    }


def test_provider_diagnostics_has_no_secrets(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-secret-value")
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-secret-value")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-secret-value")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-secret-value")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.18.0")
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PROVIDER_SEARCH, provider="subdl")
    usage_guard.record_event(config.DB_PATH, event_type=usage_guard.EVENT_PROVIDER_IMPORT, provider="subsource")

    response = client.get("/companion/provider-diagnostics")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["providers"]["gemini"]["configured"] is True
    assert payload["providers"]["subdl"]["configured"] is True
    assert payload["providers"]["subsource"]["configured"] is True
    assert payload["providers"]["opensubtitles"]["configured"] is True
    assert payload["provider_usage_counters"]["provider_searches_today"]["subdl"] == 1
    assert payload["provider_usage_counters"]["provider_imports_today"]["subsource"] == 1
    assert payload["reliability"]["retries"]["max_retries"] == 1
    encoded = json.dumps(payload, sort_keys=True)
    assert "gem-secret-value" not in encoded
    assert "subdl-secret-value" not in encoded
    assert "subsource-secret-value" not in encoded
    assert "os-secret-value" not in encoded


def test_batch_prepare_surfaces_safe_provider_error_message(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setattr(
        provider_router,
        "get_provider_status",
        lambda: {
            "gemini": {"configured": True, "message": "ok"},
            "subdl": {"configured": True, "message": "ok"},
            "subsource": {"configured": False, "message": "disabled"},
            "opensubtitles": {"configured": False, "message": "disabled"},
        },
    )
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [],
            "provider_errors": {
                "subdl": {
                    "provider": "subdl",
                    "error_type": "transient_http_error",
                    "http_status": 503,
                    "message": "SubDL search is temporarily unavailable (HTTP 503).",
                }
            },
            "searched_providers": ["subdl"],
        },
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1800001", "season": 1, "episode_start": 1, "episode_end": 1},
    )

    assert response.status_code == 200, response.text
    payload = _wait_for_batch(client, response.json()["batch_id"])
    assert payload["status"] == "failed"
    assert payload["items"][0]["status"] == "failed"
    assert payload["items"][0]["error_message"] == (
        "Provider issues: SubDL: SubDL search is temporarily unavailable (HTTP 503)."
    )
