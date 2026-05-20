"""Tests for Phase 34.1 Stremio subtitle extra-path route compatibility."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_stremio_filename_extra_route_movie_returns_200(client: TestClient) -> None:
    extra = quote("filename=My Movie (2026) [WEB-DL].mkv", safe="")
    response = client.get(f"/subtitles/movie/tt0123456/{extra}.json")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "subtitles" in payload
    assert isinstance(payload["subtitles"], list)


def test_stremio_filename_extra_route_series_episode_returns_200(client: TestClient) -> None:
    extra = quote("filename=Family Guy S02E15.mkv", safe="")
    response = client.get(f"/subtitles/series/tt012576:2:15/{extra}.json")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "subtitles" in payload
    assert isinstance(payload["subtitles"], list)


def test_url_encoded_filename_with_spaces_brackets_and_percent_encoding(client: TestClient) -> None:
    filename_value = "filename=Movie Name [1080p] 100% Ready.mkv"
    extra = quote(filename_value, safe="")
    response = client.get(f"/subtitles/movie/tt0777700/{extra}.json")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "subtitles" in payload


def test_existing_subtitle_routes_still_work(client: TestClient) -> None:
    movie_response = client.get("/subtitles/movie/tt7777003.json")
    series_response = client.get("/subtitles/series/tt7777005:1:5.json")
    assert movie_response.status_code == 200, movie_response.text
    assert series_response.status_code == 200, series_response.text
    assert "subtitles" in movie_response.json()
    assert "subtitles" in series_response.json()


def test_no_provider_configured_does_not_crash_or_return_404_for_extra_route(
    client: TestClient,
) -> None:
    extra = quote("filename=Family Guy S02E15.mkv", safe="")
    response = client.get(
        f"/subtitles/series/tt012576%3A2%3A15/{extra}.json"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "subtitles" in payload
    assert isinstance(payload["subtitles"], list)
