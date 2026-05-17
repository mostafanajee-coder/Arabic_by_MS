"""Tests for Phase 5 SubDL search and import support."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service, subdl_service
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


def test_search_subdl_missing_api_key(client):
    resp = client.get("/companion/search-subdl", params={"video_id": "tt1375666"})
    assert resp.status_code == 400
    assert "SUBDL_API_KEY" in resp.json()["detail"]


def test_search_subdl_endpoint_with_mocked_response(client, monkeypatch):
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-test-key")

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse(
            json_data={
                "status": True,
                "results": [
                    {
                        "imdb_id": "tt1375666",
                        "type": "movie",
                        "name": "Inception",
                        "sd_id": 123456,
                    }
                ],
                "subtitles": [
                    {
                        "id": 3197651,
                        "url": 3213944,
                        "language": "EN",
                        "release_name": "Inception.2010.1080p.BluRay.x264",
                    },
                    {
                        "id": 3197652,
                        "url": 3213945,
                        "language": "EN",
                        "release_name": "Inception.2010.720p.HDTV.x264",
                    },
                ],
            }
        )

    monkeypatch.setattr(subdl_service.httpx, "get", fake_get)

    resp = client.get(
        "/companion/search-subdl",
        params={
            "video_id": "tt1375666",
            "video_type": "movie",
            "query": "Inception",
            "language": "EN",
            "release_name": "Inception.2010.1080p.BluRay.x264",
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["provider"] == "subdl"
    assert items[0]["subtitle_id"] == "3197651"
    assert items[0]["download_url"] == "https://dl.subdl.com/subtitle/3197651-3213944.zip"
    assert items[0]["score"] >= items[1]["score"]
    assert calls[0]["params"]["imdb_id"] == "tt1375666"
    assert calls[0]["params"]["languages"] == "EN"


def test_subdl_result_normalization():
    raw = {
        "id": 3197651,
        "url": 3213944,
        "language": "EN",
        "release_name": "My.Show.S01E02.1080p.WEB-DL",
        "season_number": 1,
        "episode_number": 2,
    }
    item = subdl_service.normalize_result(
        raw,
        video_id="tt1111111",
        language="EN",
        release_name="My.Show.S01E02.1080p.WEB-DL",
        season=1,
        episode=2,
        matched_video={"imdb_id": "tt1111111"},
    )
    assert item["provider"] == "subdl"
    assert item["subtitle_id"] == "3197651"
    assert item["language"] == "EN"
    assert item["release_name"] == "My.Show.S01E02.1080p.WEB-DL"
    assert item["download_url"].endswith(".zip")
    assert item["score"] > 0
    assert item["raw"]["id"] == 3197651


def test_subtitle_matcher_scores_exact_match_higher():
    exact = {
        "language": "EN",
        "release_name": "Anime.S01E03.1080p.WEB-DL.SubsPlease",
        "season": 1,
        "episode": 3,
        "imdb_id": "tt7654321",
    }
    loose = {
        "language": "EN",
        "release_name": "Anime.S01E03.720p.HDTV",
        "season": 1,
        "episode": 3,
        "imdb_id": "tt7654321",
    }

    exact_score = score_subtitle_match(
        video_id="tt7654321",
        language="EN",
        release_name="Anime.S01E03.1080p.WEB-DL.SubsPlease",
        season=1,
        episode=3,
        candidate=exact,
    )
    loose_score = score_subtitle_match(
        video_id="tt7654321",
        language="EN",
        release_name="Anime.S01E03.1080p.WEB-DL.SubsPlease",
        season=1,
        episode=3,
        candidate=loose,
    )

    assert exact_score > loose_score

    ordered = sort_subtitle_matches(
        [loose, exact],
        video_id="tt7654321",
        language="EN",
        release_name="Anime.S01E03.1080p.WEB-DL.SubsPlease",
        season=1,
        episode=3,
    )
    assert ordered[0]["release_name"] == exact["release_name"]


def test_import_subdl_endpoint_with_mocked_zip_download(client, monkeypatch):
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-test-key")

    def fake_get(url, timeout=None, follow_redirects=None):
        return FakeResponse(
            content=_make_zip_with_srt(),
            headers={"content-type": "application/zip"},
        )

    monkeypatch.setattr(subdl_service.httpx, "get", fake_get)

    resp = client.post(
        "/companion/import-subdl",
        json={
            "video_id": "tt9990001",
            "video_type": "movie",
            "release_name": "Import.Test.1080p.WEB-DL",
            "download_url": "https://dl.subdl.com/subtitle/3197651-3213944.zip",
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "uploaded"
    assert Path(payload["english_srt_path"]).exists()


def test_import_subdl_rejects_invalid_downloaded_srt(client, monkeypatch):
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-test-key")
    monkeypatch.setattr(subdl_service, "download_subtitle_data", lambda url: b"not a subtitle")

    resp = client.post(
        "/companion/import-subdl",
        json={
            "video_id": "tt9990002",
            "download_url": "https://dl.subdl.com/subtitle/invalid.zip",
        },
    )
    assert resp.status_code == 400
    assert "timestamp" in resp.json()["detail"].lower() or "-->" in resp.json()["detail"]


def test_imported_subdl_record_creates_db_record(client, monkeypatch):
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-test-key")
    monkeypatch.setattr(subdl_service, "download_subtitle_data", lambda url: VALID_SRT_BYTES)

    resp = client.post(
        "/companion/import-subdl",
        data={
            "video_id": "tt9990003",
            "video_type": "series",
            "release_name": "Some.Show.S01E01.WEB-DL",
            "download_url": "https://dl.subdl.com/subtitle/ok.zip",
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    rec = cache_db.get_record(config.DB_PATH, payload["id"])
    assert rec is not None
    assert rec["video_id"] == "tt9990003"
    assert rec["video_type"] == "series"
    assert rec["release_name"] == "Some.Show.S01E01.WEB-DL"
    assert rec["status"] == "uploaded"


def test_imported_subdl_record_can_be_translated(client, monkeypatch):
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-test-key")
    monkeypatch.setattr(subdl_service, "download_subtitle_data", lambda url: VALID_SRT_BYTES)

    imported = client.post(
        "/companion/import-subdl",
        json={
            "video_id": "tt9990004",
            "video_type": "movie",
            "release_name": "Import.Translate.1080p.BluRay",
            "download_url": "https://dl.subdl.com/subtitle/3197651-3213944.zip",
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
