"""Tests for Phase 6 SubSource search and import support."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service, subsource_service
from utils.subtitle_matcher import score_subtitle_match, sort_subtitle_matches


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
        status_code=200,
        json_data=None,
        text="",
        content=b"",
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


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_search_subsource_missing_api_key(client):
    resp = client.get("/companion/search-subsource", params={"video_id": "tt0944947"})
    assert resp.status_code == 400
    assert "SUBSOURCE_API_KEY" in resp.json()["detail"]


def test_subsource_status_missing_key(client):
    resp = client.get("/companion/subsource-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["configured"] is False
    assert "SUBSOURCE_API_KEY" in payload["message"]


def test_subsource_status_configured(client, monkeypatch):
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-test-key")
    monkeypatch.setenv("SUBSOURCE_BASE_URL", "https://mock.subsource.local")

    resp = client.get("/companion/subsource-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["configured"] is True
    assert payload["base_url"] == "https://mock.subsource.local"


def test_search_subsource_endpoint_with_mocked_response(client, monkeypatch):
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-test-key")

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse(
            json_data={
                "items": [
                    {
                        "id": "ss-100",
                        "imdb_id": "tt0944947",
                        "language": "en",
                        "release_name": "Show.S01E01.1080p.WEB-DL",
                        "download_url": "https://cdn.subsource.net/subs/ss-100.zip",
                        "season": 1,
                        "episode": 1,
                    },
                    {
                        "id": "ss-200",
                        "imdb_id": "tt0944947",
                        "language": "en",
                        "release_name": "Show.S01E01.720p.HDTV",
                        "download_url": "https://cdn.subsource.net/subs/ss-200.zip",
                        "season": 1,
                        "episode": 1,
                    },
                ]
            }
        )

    monkeypatch.setattr(subsource_service.httpx, "get", fake_get)

    resp = client.get(
        "/companion/search-subsource",
        params={
            "video_id": "tt0944947",
            "video_type": "series",
            "season": 1,
            "episode": 1,
            "query": "Show",
            "language": "en",
            "release_name": "Show.S01E01.1080p.WEB-DL",
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["provider"] == "subsource"
    assert items[0]["subtitle_id"] == "ss-100"
    assert items[0]["score"] >= items[1]["score"]
    assert calls[0]["headers"]["X-API-Key"] == "subsource-test-key"


def test_subsource_result_normalization():
    raw = {
        "id": "ss-500",
        "imdb_id": "tt1111111",
        "language": "en",
        "release_name": "My.Show.S01E02.1080p.WEB-DL",
        "download_url": "https://cdn.subsource.net/ss-500.zip",
        "season": 1,
        "episode": 2,
    }
    item = subsource_service.normalize_result(
        raw,
        video_id="tt1111111",
        language="en",
        release_name="My.Show.S01E02.1080p.WEB-DL",
        season=1,
        episode=2,
    )
    assert item["provider"] == "subsource"
    assert item["subtitle_id"] == "ss-500"
    assert item["language"] == "en"
    assert item["release_name"] == "My.Show.S01E02.1080p.WEB-DL"
    assert item["download_url"] == "https://cdn.subsource.net/ss-500.zip"
    assert item["score"] > 0


def test_subsource_scoring_compatibility():
    exact = {
        "language": "en",
        "release_name": "Anime.S01E03.1080p.WEB-DL.SubsPlease",
        "season": 1,
        "episode": 3,
        "imdb_id": "tt7654321",
    }
    loose = {
        "language": "en",
        "release_name": "Anime.S01E03.720p.HDTV",
        "season": 1,
        "episode": 3,
        "imdb_id": "tt7654321",
    }

    exact_score = score_subtitle_match(
        video_id="tt7654321",
        language="en",
        release_name="Anime.S01E03.1080p.WEB-DL.SubsPlease",
        season=1,
        episode=3,
        candidate=exact,
    )
    loose_score = score_subtitle_match(
        video_id="tt7654321",
        language="en",
        release_name="Anime.S01E03.1080p.WEB-DL.SubsPlease",
        season=1,
        episode=3,
        candidate=loose,
    )
    assert exact_score > loose_score

    ordered = sort_subtitle_matches(
        [loose, exact],
        video_id="tt7654321",
        language="en",
        release_name="Anime.S01E03.1080p.WEB-DL.SubsPlease",
        season=1,
        episode=3,
    )
    assert ordered[0]["release_name"] == exact["release_name"]


def test_import_subsource_endpoint_with_mocked_zip_download(client, monkeypatch):
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-test-key")

    def fake_get(url, timeout=None, follow_redirects=None):
        return FakeResponse(
            content=_make_zip_with_srt(),
            headers={"content-type": "application/zip"},
        )

    monkeypatch.setattr(subsource_service.httpx, "get", fake_get)

    resp = client.post(
        "/companion/import-subsource",
        json={
            "video_id": "tt9991001",
            "video_type": "movie",
            "release_name": "Import.Test.1080p.WEB-DL",
            "download_url": "https://cdn.subsource.net/subs/ss-100.zip",
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "uploaded"
    assert Path(payload["english_srt_path"]).exists()


def test_import_subsource_rejects_invalid_downloaded_srt(client, monkeypatch):
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-test-key")
    monkeypatch.setattr(subsource_service, "download_subtitle_data", lambda url: b"not a subtitle")

    resp = client.post(
        "/companion/import-subsource",
        json={
            "video_id": "tt9991002",
            "download_url": "https://cdn.subsource.net/subs/bad.zip",
        },
    )
    assert resp.status_code == 400
    assert "timestamp" in resp.json()["detail"].lower() or "-->" in resp.json()["detail"]


def test_imported_subsource_record_creates_db_record(client, monkeypatch):
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-test-key")
    monkeypatch.setattr(subsource_service, "download_subtitle_data", lambda url: VALID_SRT_BYTES)

    resp = client.post(
        "/companion/import-subsource",
        data={
            "video_id": "tt9991003",
            "video_type": "series",
            "release_name": "Some.Show.S01E01.WEB-DL",
            "download_url": "https://cdn.subsource.net/subs/ok.srt",
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    rec = cache_db.get_record(config.DB_PATH, payload["id"])
    assert rec is not None
    assert rec["video_id"] == "tt9991003"
    assert rec["video_type"] == "series"
    assert rec["release_name"] == "Some.Show.S01E01.WEB-DL"
    assert rec["status"] == "uploaded"


def test_imported_subsource_record_can_be_translated(client, monkeypatch):
    monkeypatch.setenv("SUBSOURCE_API_KEY", "subsource-test-key")
    monkeypatch.setattr(subsource_service, "download_subtitle_data", lambda url: VALID_SRT_BYTES)

    imported = client.post(
        "/companion/import-subsource",
        json={
            "video_id": "tt9991004",
            "video_type": "movie",
            "release_name": "Import.Translate.1080p.BluRay",
            "download_url": "https://cdn.subsource.net/subs/ss-200.zip",
        },
    )
    assert imported.status_code == 200, imported.text
    payload = imported.json()

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    translated = client.post("/companion/translate/{0}".format(payload["id"]))
    assert translated.status_code == 200, translated.text
    body = translated.json()
    assert body["status"] == "translated"
    assert Path(body["arabic_srt_path"]).exists()
