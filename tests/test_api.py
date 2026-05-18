"""Tests for the core Stremio endpoints (Phase 1 behavior, still required)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_manifest_endpoint(client: TestClient) -> None:
    """/manifest.json must return a valid Stremio subtitle manifest."""
    response = client.get("/manifest.json")
    assert response.status_code == 200

    data = response.json()
    for key in ("id", "version", "name", "resources", "types"):
        assert key in data, f"manifest missing required key: {key}"

    assert data["name"] == "Arabic by M.S"
    assert "subtitles" in data["resources"]
    assert "movie" in data["types"]


def test_subtitles_endpoint_returns_single_status_item_when_real_arabic_missing(client: TestClient) -> None:
    """/subtitles/{type}/{id}.json returns a status subtitle until real Arabic exists."""
    response = client.get("/subtitles/movie/tt1234567.json")
    assert response.status_code == 200

    data = response.json()
    assert "subtitles" in data
    subs = data["subtitles"]
    assert isinstance(subs, list) and len(subs) == 1

    item = subs[0]
    assert item["lang"] == "ara"
    assert item["name"] == "Arabic by M.S - Status"
    assert item["url"].endswith(".srt")
    assert "/status-subtitle/" in item["url"]


def test_sample_srt_download_endpoint(client: TestClient) -> None:
    """/download/{id}.srt returns the bundled sample Arabic SRT."""
    response = client.get("/download/arabic-ms-tt1234567.srt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-subrip")

    body = response.content.decode("utf-8")
    assert "-->" in body
    assert "M.S" in body


def test_sample_srt_file_is_packaged() -> None:
    """The bundled sample file must exist on disk."""
    assert config.SAMPLE_SRT_PATH.exists(), (
        f"Expected sample SRT at {config.SAMPLE_SRT_PATH}"
    )
    assert config.SAMPLE_SRT_PATH.stat().st_size > 0
