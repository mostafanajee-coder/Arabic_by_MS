"""Tests for Phase 7 unified provider router endpoints and import-best flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service, provider_router, subdl_service, subsource_service


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


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


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_provider_status_missing_keys(client: TestClient) -> None:
    resp = client.get("/companion/provider-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["gemini"]["configured"] is False
    assert payload["subdl"]["configured"] is False
    assert payload["subsource"]["configured"] is False


def test_provider_status_configured_keys(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setenv("SUBDL_BASE_URL", "https://subdl.local")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-key")
    monkeypatch.setenv("SUBSOURCE_BASE_URL", "https://subsource.local")

    resp = client.get("/companion/provider-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["gemini"]["configured"] is True
    assert payload["gemini"]["model"] == "gemini-test"
    assert payload["subdl"]["configured"] is True
    assert payload["subdl"]["base_url"] == "https://subdl.local"
    assert payload["subsource"]["configured"] is True
    assert payload["subsource"]["base_url"] == "https://subsource.local"


def test_subdl_status_endpoint_exists(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    resp = client.get("/companion/subdl-status")
    assert resp.status_code == 200
    assert resp.json()["configured"] is True


def test_search_all_with_both_providers_mocked(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-key")
    monkeypatch.setattr(
        subdl_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subdl",
                "subtitle_id": "subdl-1",
                "language": "EN",
                "release_name": "Movie.1080p.BluRay",
                "download_url": "https://subdl.local/1.zip",
                "score": 91.5,
            }
        ],
    )
    monkeypatch.setattr(
        subsource_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subsource",
                "subtitle_id": "subsource-1",
                "language": "en",
                "release_name": "Movie.720p.WEB-DL",
                "download_url": "https://subsource.local/1.zip",
                "score": 88.0,
            }
        ],
    )

    resp = client.get("/companion/search-all", params={"video_id": "tt1375666"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["searched_providers"] == ["subdl", "subsource"]
    assert payload["provider_errors"] == {}
    assert [item["provider"] for item in payload["items"]] == ["subdl", "subsource"]


def test_search_all_one_provider_fails_other_succeeds(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-key")
    monkeypatch.setattr(
        subdl_service,
        "search_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(subdl_service.SubDLError("SubDL failed")),
    )
    monkeypatch.setattr(
        subsource_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subsource",
                "subtitle_id": "ok-1",
                "language": "en",
                "release_name": "Movie.1080p.WEB-DL",
                "download_url": "https://subsource.local/ok.zip",
                "score": 77.0,
            }
        ],
    )

    resp = client.get("/companion/search-all", params={"video_id": "tt1375666"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["provider"] == "subsource"
    assert payload["provider_errors"]["subdl"]["provider"] == "subdl"
    assert payload["provider_errors"]["subdl"]["error_type"] == "provider_error"
    assert payload["provider_errors"]["subdl"]["http_status"] is None
    assert payload["provider_errors"]["subdl"]["message"] == "SubDL failed"


def test_search_all_deduplicates_results(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-key")
    monkeypatch.setattr(
        subdl_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subdl",
                "subtitle_id": "dupe-1",
                "language": "EN",
                "release_name": "Shared.Release",
                "download_url": "https://shared.local/sub.zip",
                "score": 95.0,
            }
        ],
    )
    monkeypatch.setattr(
        subsource_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subsource",
                "subtitle_id": "other-id",
                "language": "en",
                "release_name": "Shared.Release",
                "download_url": "https://shared.local/sub.zip",
                "score": 90.0,
            }
        ],
    )

    resp = client.get("/companion/search-all", params={"video_id": "tt1375666"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["provider"] == "subdl"


def test_search_all_sorts_by_score_descending(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-key")
    monkeypatch.setattr(
        subdl_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subdl",
                "subtitle_id": "low",
                "language": "EN",
                "release_name": "Movie.Low",
                "download_url": "https://subdl.local/low.zip",
                "score": 10.0,
            }
        ],
    )
    monkeypatch.setattr(
        subsource_service,
        "search_subtitles",
        lambda **kwargs: [
            {
                "provider": "subsource",
                "subtitle_id": "high",
                "language": "en",
                "release_name": "Movie.High",
                "download_url": "https://subsource.local/high.zip",
                "score": 92.5,
            },
            {
                "provider": "subsource",
                "subtitle_id": "mid",
                "language": "en",
                "release_name": "Movie.Mid",
                "download_url": "https://subsource.local/mid.zip",
                "score": 55.0,
            },
        ],
    )

    resp = client.get("/companion/search-all", params={"video_id": "tt1375666"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["subtitle_id"] for item in items] == ["high", "mid", "low"]


def test_import_best_no_results_returns_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {"items": [], "provider_errors": {}, "searched_providers": []},
    )

    resp = client.post("/companion/import-best", json={"video_id": "tt4044044"})
    assert resp.status_code == 404
    assert "No subtitle results found" in resp.json()["detail"]


def test_import_best_imports_from_subdl(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "subdl-best",
                    "language": "EN",
                    "release_name": "Best.Movie.1080p",
                    "download_url": "https://subdl.local/best.zip",
                    "score": 97.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    resp = client.post("/companion/import-best", json={"video_id": "tt9000001"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["provider"] == "subdl"
    assert payload["status"] == "uploaded"

    record = cache_db.get_record(config.DB_PATH, payload["record_id"])
    assert record is not None
    assert record["source_provider"] == "subdl"
    assert record["source_subtitle_id"] == "subdl-best"
    assert record["source_download_url"] == "https://subdl.local/best.zip"


def test_import_best_imports_from_subsource(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subsource",
                    "subtitle_id": "subsource-best",
                    "language": "en",
                    "release_name": "Best.Show.S01E01",
                    "download_url": "https://subsource.local/best.zip",
                    "score": 89.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subsource"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    resp = client.post(
        "/companion/import-best",
        json={"video_id": "tt9000002", "video_type": "series", "season": 1, "episode": 1},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["provider"] == "subsource"
    assert payload["status"] == "uploaded"


def test_import_best_auto_translate_with_mocked_gemini(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "subdl-translated",
                    "language": "EN",
                    "release_name": "Best.Movie.1080p",
                    "download_url": "https://subdl.local/translated.zip",
                    "score": 97.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: _build_long_srt(25),
    )
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    resp = client.post(
        "/companion/import-best",
        json={"video_id": "tt9000003", "auto_translate": True},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "translated"
    assert payload["arabic_srt_path"]
    assert Path(payload["arabic_srt_path"]).exists()


def test_provider_metadata_stored_in_db(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    resp = client.post(
        "/companion/import-subdl",
        json={
            "video_id": "tt9000004",
            "video_type": "movie",
            "release_name": "Stored.Metadata",
            "subtitle_id": "subdl-meta",
            "download_url": "https://subdl.local/meta.zip",
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    record = cache_db.get_record(config.DB_PATH, payload["id"])
    assert record is not None
    assert record["source_provider"] == "subdl"
    assert record["source_subtitle_id"] == "subdl-meta"
    assert record["source_download_url"] == "https://subdl.local/meta.zip"
