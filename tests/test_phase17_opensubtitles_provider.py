"""Tests for Phase 17 OpenSubtitles provider integration."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import (
    cache_db,
    gemini_service,
    job_manager,
    opensubtitles_service,
    provider_router,
    subdl_service,
    subsource_service,
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


def _make_zip_with_srt() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sample.srt", VALID_SRT_BYTES)
    return buf.getvalue()


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


def _wait_for_batch(client: TestClient, batch_id: str, timeout: float = 5.0) -> dict:
    import time

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


def test_opensubtitles_status_disabled_and_enabled(client: TestClient, monkeypatch) -> None:
    disabled = client.get("/companion/opensubtitles-status")
    assert disabled.status_code == 200
    payload = disabled.json()
    assert payload["configured"] is False
    assert "OPENSUBTITLES_API_KEY" in payload["message"]

    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setenv("OPENSUBTITLES_BASE_URL", "https://api.opensubtitles.test/api/v1")

    enabled = client.get("/companion/opensubtitles-status")
    assert enabled.status_code == 200
    payload = enabled.json()
    assert payload["configured"] is True
    assert payload["base_url"] == "https://api.opensubtitles.test/api/v1"


def test_opensubtitles_search_normalization(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setenv("OPENSUBTITLES_BASE_URL", "https://api.opensubtitles.test/api/v1")

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        return FakeResponse(
            json_data={
                "data": [
                    {
                        "id": "7421396",
                        "type": "subtitle",
                        "attributes": {
                            "subtitle_id": "7421396",
                            "language": "en",
                            "release": "My.Show.S01E02.1080p.WEB-DL",
                            "files": [
                                {
                                    "file_id": 8353887,
                                    "file_name": "My.Show.S01E02.1080p.WEB-DL.srt",
                                }
                            ],
                            "feature_details": {
                                "parent_imdb_id": 1111111,
                                "season_number": 1,
                                "episode_number": 2,
                            },
                        },
                    }
                ]
            }
        )

    monkeypatch.setattr(opensubtitles_service.httpx, "get", fake_get)

    response = client.get(
        "/companion/search-opensubtitles",
        params={
            "video_id": "tt1111111",
            "video_type": "series",
            "season": 1,
            "episode": 2,
            "query": "My Show",
            "release_name": "My.Show.S01E02.1080p.WEB-DL",
            "language": "en",
        },
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["provider"] == "opensubtitles"
    assert items[0]["subtitle_id"] == "7421396"
    assert items[0]["language"] == "en"
    assert items[0]["release_name"] == "My.Show.S01E02.1080p.WEB-DL"
    assert items[0]["download_url"] == "https://api.opensubtitles.test/api/v1/download?file_id=8353887&sub_format=srt"
    assert items[0]["score"] > 0
    assert calls[0]["params"]["parent_imdb_id"] == "1111111"
    assert "imdb_id" not in calls[0]["params"]
    assert calls[0]["params"]["season_number"] == 1
    assert calls[0]["params"]["episode_number"] == 2
    assert calls[0]["params"]["type"] == "episode"
    assert calls[0]["headers"]["Api-Key"] == "os-key"
    assert calls[0]["headers"]["User-Agent"] == "ArabicByMS/0.17.0"


def test_opensubtitles_search_episode_video_type_uses_imdb_id(monkeypatch) -> None:
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setenv("OPENSUBTITLES_BASE_URL", "https://api.opensubtitles.test/api/v1")

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        return FakeResponse(json_data={"data": []})

    monkeypatch.setattr(opensubtitles_service.httpx, "get", fake_get)

    items = opensubtitles_service.search_subtitles(
        video_id="tt2222222",
        video_type="episode",
        season=1,
        episode=2,
        query="Episode Title",
        release_name="Show.S01E02.1080p.WEB-DL",
        language="en",
    )

    assert items == []
    assert calls[0]["params"]["imdb_id"] == "2222222"
    assert "parent_imdb_id" not in calls[0]["params"]
    assert calls[0]["params"]["season_number"] == 1
    assert calls[0]["params"]["episode_number"] == 2
    assert calls[0]["params"]["type"] == "episode"


def test_opensubtitles_download_uses_post_for_generated_file_id_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setenv("OPENSUBTITLES_BASE_URL", "https://api.opensubtitles.test/api/v1")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, follow_redirects=None):
        calls.append(
            {
                "method": "POST",
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        return FakeResponse(json_data={"link": "https://cdn.opensubtitles.test/files/sample.srt"})

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        calls.append(
            {
                "method": "GET",
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        return FakeResponse(content=VALID_SRT_BYTES, headers={"content-type": "application/x-subrip"})

    monkeypatch.setattr(opensubtitles_service, "_http_post", fake_post)
    monkeypatch.setattr(opensubtitles_service, "_http_get", fake_get)

    content = opensubtitles_service.download_subtitle_data(
        "https://api.opensubtitles.test/api/v1/download?file_id=8353887&sub_format=srt"
    )

    assert content == VALID_SRT_BYTES
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.opensubtitles.test/api/v1/download"
    assert calls[0]["json"] == {"file_id": "8353887", "sub_format": "srt"}
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == "https://cdn.opensubtitles.test/files/sample.srt"


def test_import_opensubtitles_successful_into_cache(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setenv("OPENSUBTITLES_BASE_URL", "https://api.opensubtitles.test/api/v1")

    post_calls = []

    def fake_post(url, json=None, headers=None, timeout=None, follow_redirects=None):
        post_calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
                "follow_redirects": follow_redirects,
            }
        )
        return FakeResponse(json_data={"link": "https://cdn.opensubtitles.test/files/sample.zip"})

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        return FakeResponse(
            content=_make_zip_with_srt(),
            headers={"content-type": "application/zip"},
        )

    monkeypatch.setattr(opensubtitles_service.httpx, "get", fake_get)
    monkeypatch.setattr(opensubtitles_service.httpx, "post", fake_post)

    response = client.post(
        "/companion/import-opensubtitles",
        json={
            "video_id": "tt1700001",
            "video_type": "movie",
            "release_name": "Import.OpenSubtitles.1080p.WEB-DL",
            "subtitle_id": "7421396",
            "download_url": "https://api.opensubtitles.test/api/v1/download?file_id=8353887&sub_format=srt",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "uploaded"
    assert Path(payload["english_srt_path"]).exists()

    record = cache_db.get_record(config.DB_PATH, payload["id"])
    assert record is not None
    assert record["source_provider"] == "opensubtitles"
    assert record["source_subtitle_id"] == "7421396"
    assert post_calls[0]["url"] == "https://api.opensubtitles.test/api/v1/download"
    assert post_calls[0]["json"] == {"file_id": "8353887", "sub_format": "srt"}


def test_provider_router_search_all_includes_opensubtitles(monkeypatch) -> None:
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setattr(subdl_service, "get_status", lambda: {"configured": False, "message": "disabled"})
    monkeypatch.setattr(subsource_service, "get_status", lambda: {"configured": False, "message": "disabled"})
    monkeypatch.setattr(
        opensubtitles_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "opensubtitles",
                "subtitle_id": "os-1",
                "language": "en",
                "release_name": "Movie.1080p.WEB-DL",
                "download_url": "https://api.opensubtitles.test/api/v1/download?file_id=1&sub_format=srt",
                "score": 93.0,
            }
        ],
    )

    payload = provider_router.search_all_subtitles(video_id="tt1375666")
    assert payload["searched_providers"] == ["opensubtitles"]
    assert payload["provider_errors"] == {}
    assert payload["items"][0]["provider"] == "opensubtitles"


def test_import_best_can_select_opensubtitles(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "opensubtitles",
                    "subtitle_id": "os-best",
                    "language": "en",
                    "release_name": "Best.Movie.1080p.WEB-DL",
                    "download_url": "https://api.opensubtitles.test/api/v1/download?file_id=9&sub_format=srt",
                    "score": 98.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["opensubtitles"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    response = client.post("/companion/import-best", json={"video_id": "tt1700002"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "opensubtitles"
    assert payload["status"] == "uploaded"

    record = cache_db.get_record(config.DB_PATH, payload["record_id"])
    assert record is not None
    assert record["source_provider"] == "opensubtitles"


def test_batch_prepare_still_works_with_opensubtitles_via_provider_router(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setenv("OPENSUBTITLES_USER_AGENT", "ArabicByMS/0.17.0")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "opensubtitles",
                    "subtitle_id": "os-batch",
                    "language": "en",
                    "release_name": "Show.S01E01.1080p.WEB-DL",
                    "download_url": "https://api.opensubtitles.test/api/v1/download?file_id=11&sub_format=srt",
                    "score": 96.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["opensubtitles"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-opensubtitles-batch", "status": "queued"},
    )
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1700003", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    payload = _wait_for_batch(client, response.json()["batch_id"])
    assert payload["status"] == "completed"
    assert payload["items"][0]["provider"] == "opensubtitles"
    assert payload["items"][0]["status"] == "completed"

    record = cache_db.get_record(config.DB_PATH, int(payload["items"][0]["record_id"]))
    assert record is not None
    assert record["source_provider"] == "opensubtitles"
