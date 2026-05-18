"""Tests for Phase 19 subtitle match intelligence and selection explanations."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from services import opensubtitles_service, provider_router, subdl_service, subsource_service
from utils.subtitle_matcher import evaluate_subtitle_match, score_subtitle_match


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


def test_score_breakdown_components_preserve_existing_score() -> None:
    candidate = {
        "imdb_id": "tt1375666",
        "language": "EN",
        "release_name": "Inception.2010.1080p.BluRay.x264",
        "download_url": "https://example.com/inception.srt",
    }

    evaluation = evaluate_subtitle_match(
        video_id="tt1375666",
        language="EN",
        release_name="Inception.2010.1080p.BluRay.x264",
        season=None,
        episode=None,
        candidate=candidate,
    )
    legacy_score = score_subtitle_match(
        video_id="tt1375666",
        language="EN",
        release_name="Inception.2010.1080p.BluRay.x264",
        season=None,
        episode=None,
        candidate=candidate,
    )

    assert evaluation["score"] == legacy_score
    assert evaluation["score_breakdown"]["imdb_match"] == 40.0
    assert evaluation["score_breakdown"]["language_match"] == 15.0
    assert evaluation["score_breakdown"]["release_similarity"] > 20.0
    assert evaluation["score_breakdown"]["important_tokens"] >= 2.5
    assert round(sum(evaluation["score_breakdown"].values()), 2) == evaluation["score"]


@pytest.mark.parametrize(
    ("candidate", "release_name", "season", "episode", "expected_confidence"),
    [
        (
            {
                "imdb_id": "tt1375666",
                "language": "EN",
                "release_name": "Inception.2010.1080p.BluRay.x264",
                "download_url": "https://example.com/exact.srt",
            },
            "Inception.2010.1080p.BluRay.x264",
            None,
            None,
            "high",
        ),
        (
            {
                "imdb_id": "tt1375666",
                "language": "EN",
                "release_name": "Inception.2010.720p.HDTV.x264",
                "download_url": "https://example.com/loose.srt",
            },
            "Inception.2010.1080p.BluRay.x264",
            None,
            None,
            "medium",
        ),
        (
            {
                "imdb_id": "tt1375666",
                "language": "FR",
                "release_name": "Different.Release.S02E03",
                "download_url": "",
                "season": 2,
                "episode": 3,
            },
            "Inception.2010.1080p.BluRay.x264",
            1,
            1,
            "low",
        ),
    ],
)
def test_match_confidence_levels(candidate, release_name, season, episode, expected_confidence) -> None:
    evaluation = evaluate_subtitle_match(
        video_id="tt1375666",
        language="EN",
        release_name=release_name,
        season=season,
        episode=episode,
        candidate=candidate,
    )

    assert evaluation["match_confidence"] == expected_confidence


def test_match_warnings_cover_weak_missing_and_mismatched_data() -> None:
    evaluation = evaluate_subtitle_match(
        video_id="tt9999999",
        language="EN",
        release_name="Show.S01E01.1080p.WEB-DL",
        season=1,
        episode=1,
        candidate={
            "imdb_id": "tt9999999",
            "language": "AR",
            "release_name": "Different.Show.S02E07.HDTV",
            "season": 2,
            "episode": 7,
            "download_url": "",
        },
    )

    assert "Weak release match." in evaluation["match_warnings"]
    assert "Download URL is missing." in evaluation["match_warnings"]
    assert "Requested language does not match candidate language." in evaluation["match_warnings"]
    assert "Season/episode details do not match the requested title." in evaluation["match_warnings"]


def test_provider_normalized_results_include_confidence_and_warnings() -> None:
    subdl_item = subdl_service.normalize_result(
        {
            "id": 1,
            "imdb_id": "tt1375666",
            "language": "EN",
            "release_name": "Inception.2010.1080p.BluRay.x264",
            "download_url": "https://dl.subdl.com/subtitle/1.zip",
        },
        video_id="tt1375666",
        language="EN",
        release_name="Inception.2010.1080p.BluRay.x264",
        season=None,
        episode=None,
    )
    subsource_item = subsource_service.normalize_result(
        {
            "id": "ss-1",
            "imdb_id": "tt0944947",
            "language": "en",
            "release_name": "Show.S01E01.1080p.WEB-DL",
            "download_url": "https://cdn.subsource.net/ss-1.zip",
            "season": 1,
            "episode": 1,
        },
        video_id="tt0944947",
        language="en",
        release_name="Show.S01E01.1080p.WEB-DL",
        season=1,
        episode=1,
    )
    opensubtitles_item = opensubtitles_service.normalize_result(
        {
            "id": "7421396",
            "attributes": {
                "subtitle_id": "7421396",
                "language": "en",
                "release": "My.Show.S01E02.1080p.WEB-DL",
                "files": [{"file_id": 8353887, "file_name": "My.Show.S01E02.1080p.WEB-DL.srt"}],
                "feature_details": {
                    "parent_imdb_id": 1111111,
                    "season_number": 1,
                    "episode_number": 2,
                },
            },
        },
        video_id="tt1111111",
        language="en",
        release_name="My.Show.S01E02.1080p.WEB-DL",
        season=1,
        episode=2,
        base_url="https://api.opensubtitles.test/api/v1",
    )

    for item in (subdl_item, subsource_item, opensubtitles_item):
        assert "score" in item
        assert "score_breakdown" in item
        assert item["match_confidence"] in {"high", "medium", "low"}
        assert isinstance(item["match_warnings"], list)
        assert isinstance(item["raw"], dict)


def test_search_all_ranking_remains_stable_with_match_metadata(client: TestClient, monkeypatch) -> None:
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
                "download_url": "https://subdl.local/low.srt",
                "score": 10.0,
                "score_breakdown": {"imdb_match": 0.0},
                "match_confidence": "low",
                "match_warnings": ["Weak release match."],
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
                "download_url": "https://subsource.local/high.srt",
                "score": 92.5,
                "score_breakdown": {"imdb_match": 40.0, "language_match": 15.0},
                "match_confidence": "high",
                "match_warnings": [],
            }
        ],
    )

    response = client.get("/companion/search-all", params={"video_id": "tt1375666"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["subtitle_id"] for item in payload["items"]] == ["high", "low"]
    assert payload["items"][0]["match_confidence"] == "high"
    assert payload["items"][1]["match_confidence"] == "low"


def test_import_best_returns_selected_reason(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "best",
                    "language": "EN",
                    "release_name": "Best.Movie.1080p.WEB-DL",
                    "download_url": "https://subdl.local/best.srt",
                    "score": 97.0,
                    "score_breakdown": {
                        "imdb_match": 40.0,
                        "language_match": 15.0,
                        "season_match": 0.0,
                        "episode_match": 0.0,
                        "release_similarity": 24.0,
                        "important_tokens": 2.5,
                        "release_episode_match": 0.0,
                    },
                    "match_confidence": "high",
                    "match_warnings": [],
                },
                {
                    "provider": "subsource",
                    "subtitle_id": "runner-up",
                    "language": "en",
                    "release_name": "Runner.Up.720p",
                    "download_url": "https://subsource.local/runner-up.srt",
                    "score": 80.0,
                    "score_breakdown": {
                        "imdb_match": 40.0,
                        "language_match": 15.0,
                        "season_match": 0.0,
                        "episode_match": 0.0,
                        "release_similarity": 10.0,
                        "important_tokens": 0.0,
                        "release_episode_match": 0.0,
                    },
                    "match_confidence": "medium",
                    "match_warnings": ["Weak release match."],
                },
            ],
            "provider_errors": {},
            "searched_providers": ["subdl", "subsource"],
        },
    )
    monkeypatch.setattr(provider_router, "download_subtitle_data", lambda provider, url: VALID_SRT_BYTES)

    response = client.post("/companion/import-best", json={"video_id": "tt1700002"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "subdl"
    assert "selected_reason" in payload
    assert "highest score (97.00)" in payload["selected_reason"]
    assert "high confidence" in payload["selected_reason"]
