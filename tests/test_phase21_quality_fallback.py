"""Tests for Phase 21 quality-aware import-best fallback behavior."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, job_manager, provider_router


GOOD_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,000\n"
    "Hello there\n"
    "\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,000\n"
    "General Kenobi\n"
    "\n"
    "3\n"
    "00:00:07,000 --> 00:00:09,000\n"
    "This subtitle is valid and readable.\n"
)

BAD_QUALITY_SRT = "1\n00:00:01,000 --> 00:00:02,000\nهذه ترجمة عربية جدا\n"
INVALID_SRT_BYTES = b"not-an-srt"


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


def _candidate(
    *,
    provider: str,
    subtitle_id: str,
    download_url: str,
    score: float,
    release_name: str,
    confidence: str = "high",
) -> dict:
    return {
        "provider": provider,
        "subtitle_id": subtitle_id,
        "language": "en",
        "release_name": release_name,
        "download_url": download_url,
        "score": score,
        "score_breakdown": {
            "imdb_match": 40.0,
            "language_match": 15.0,
            "season_match": 0.0,
            "episode_match": 0.0,
            "release_similarity": 20.0,
            "important_tokens": 2.5,
            "release_episode_match": 0.0,
        },
        "match_confidence": confidence,
        "match_warnings": [],
    }


def test_top_ranked_bad_quality_falls_back_to_second_good_candidate(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="bad-top",
                    download_url="https://subdl.local/bad-top.srt",
                    score=98.0,
                    release_name="Movie.Bad.1080p.WEB-DL",
                ),
                _candidate(
                    provider="subsource",
                    subtitle_id="good-second",
                    download_url="https://subsource.local/good-second.srt",
                    score=91.0,
                    release_name="Movie.Good.1080p.WEB-DL",
                ),
            ],
            "provider_errors": {},
            "searched_providers": ["subdl", "subsource"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: (
            BAD_QUALITY_SRT.encode("utf-8")
            if "bad-top" in url
            else GOOD_SRT.encode("utf-8")
        ),
    )

    response = client.post("/companion/import-best", json={"video_id": "tt2100001"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "subsource"
    assert payload["quality_level"] == "good"
    assert "Higher-ranked candidates were skipped" in payload["fallback_reason"]
    assert payload["tried_candidates"][0]["skip_reason"] == "bad_quality"
    assert payload["tried_candidates"][1]["status"] == "selected"
    assert "highest score (91.00)" in payload["selected_reason"]

    record = cache_db.get_record(config.DB_PATH, int(payload["record_id"]))
    assert record is not None
    assert record["source_subtitle_id"] == "good-second"


def test_invalid_srt_candidate_is_skipped_safely(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="invalid-top",
                    download_url="https://subdl.local/invalid-top.srt",
                    score=97.0,
                    release_name="Movie.Invalid.1080p.WEB-DL",
                ),
                _candidate(
                    provider="subsource",
                    subtitle_id="good-next",
                    download_url="https://subsource.local/good-next.srt",
                    score=90.0,
                    release_name="Movie.Good.720p.WEB-DL",
                ),
            ],
            "provider_errors": {},
            "searched_providers": ["subdl", "subsource"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: INVALID_SRT_BYTES if "invalid-top" in url else GOOD_SRT.encode("utf-8"),
    )

    response = client.post("/companion/import-best", json={"video_id": "tt2100002"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "subsource"
    assert payload["tried_candidates"][0]["skip_reason"] == "invalid_srt"
    assert payload["tried_candidates"][1]["status"] == "selected"


def test_all_candidates_bad_returns_clear_warning_and_does_not_crash(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="bad-first",
                    download_url="https://subdl.local/bad-first.srt",
                    score=95.0,
                    release_name="Movie.Bad.First",
                ),
                _candidate(
                    provider="subsource",
                    subtitle_id="bad-second",
                    download_url="https://subsource.local/bad-second.srt",
                    score=85.0,
                    release_name="Movie.Bad.Second",
                ),
            ],
            "provider_errors": {},
            "searched_providers": ["subdl", "subsource"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: BAD_QUALITY_SRT.encode("utf-8"),
    )

    response = client.post("/companion/import-best", json={"video_id": "tt2100003"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "subdl"
    assert payload["reject_hint"] is True
    assert payload["quality_level"] == "bad"
    assert "All ranked candidates within the fallback limit" in payload["fallback_reason"]
    assert any(item["status"] == "selected_with_warning" for item in payload["tried_candidates"])


def test_import_best_returns_tried_candidates_and_fallback_reason(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="missing-url",
                    download_url="",
                    score=94.0,
                    release_name="Movie.Missing.URL",
                ),
                _candidate(
                    provider="subsource",
                    subtitle_id="good-after-missing",
                    download_url="https://subsource.local/good-after-missing.srt",
                    score=89.0,
                    release_name="Movie.Available",
                ),
            ],
            "provider_errors": {},
            "searched_providers": ["subdl", "subsource"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: GOOD_SRT.encode("utf-8"),
    )

    response = client.post("/companion/import-best", json={"video_id": "tt2100004"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload["tried_candidates"], list)
    assert payload["tried_candidates"][0]["skip_reason"] == "missing_url"
    assert payload["fallback_reason"]


def test_batch_prepare_benefits_from_fallback(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="batch-bad-top",
                    download_url="https://subdl.local/batch-bad-top.srt",
                    score=99.0,
                    release_name="Show.S01E01.Bad",
                ),
                _candidate(
                    provider="subdl",
                    subtitle_id="batch-good-second",
                    download_url="https://subdl.local/batch-good-second.srt",
                    score=92.0,
                    release_name="Show.S01E01.Good",
                ),
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: (
            BAD_QUALITY_SRT.encode("utf-8")
            if "batch-bad-top" in url
            else GOOD_SRT.encode("utf-8")
        ),
    )
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-phase21-fallback", "status": "queued"},
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2100005", "season": 1, "episode_start": 1, "episode_end": 1},
    )

    assert response.status_code == 200, response.text
    payload = _wait_for_batch(client, response.json()["batch_id"])
    assert payload["status"] == "completed"
    assert payload["items"][0]["status"] == "completed"
    assert payload["items"][0]["quality_level"] == "good"

    record = cache_db.get_record(config.DB_PATH, int(payload["items"][0]["record_id"]))
    assert record is not None
    assert record["source_subtitle_id"] == "batch-good-second"
