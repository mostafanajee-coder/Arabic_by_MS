"""Tests for Phase 20 subtitle quality inspection and non-blocking import hints."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from services import job_manager, provider_router
from utils.srt_quality import analyze_srt_quality


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
    "We have a clean subtitle file.\n"
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_good_srt_quality() -> None:
    quality = analyze_srt_quality(GOOD_SRT, expected_language="en")

    assert quality["quality_level"] == "good"
    assert quality["quality_score"] >= 85
    assert quality["quality_warnings"] == []
    assert quality["reject_hint"] is False


def test_numeric_cue_numbering_out_of_sequence_warns() -> None:
    quality = analyze_srt_quality(
        "1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n"
        "3\n00:00:04,000 --> 00:00:06,000\nGeneral Kenobi\n",
        expected_language="en",
    )

    assert quality["quality_level"] == "warning"
    assert any("cue numbering looks invalid" in warning.lower() for warning in quality["quality_warnings"])


def test_missing_cue_numbers_with_valid_timestamps_warns() -> None:
    quality = analyze_srt_quality(
        "00:00:01,000 --> 00:00:03,000\nHello there\n\n"
        "00:00:04,000 --> 00:00:06,000\nGeneral Kenobi\n\n"
        "00:00:07,000 --> 00:00:09,000\nWe have no counters here.\n",
        expected_language="en",
    )

    assert quality["quality_level"] == "warning"
    assert any("cue numbering looks invalid" in warning.lower() for warning in quality["quality_warnings"])


def test_non_numeric_cue_number_line_before_timestamp_warns() -> None:
    quality = analyze_srt_quality(
        "Intro\n00:00:01,000 --> 00:00:03,000\nHello there\n\n"
        "Second\n00:00:04,000 --> 00:00:06,000\nGeneral Kenobi\n",
        expected_language="en",
    )

    assert quality["quality_level"] == "warning"
    assert any("cue numbering looks invalid" in warning.lower() for warning in quality["quality_warnings"])


def test_valid_srt_numbering_still_good_without_warning() -> None:
    quality = analyze_srt_quality(GOOD_SRT, expected_language="en")

    assert quality["quality_level"] == "good"
    assert not any("cue numbering looks invalid" in warning.lower() for warning in quality["quality_warnings"])


def test_malformed_timestamp_detection() -> None:
    quality = analyze_srt_quality(
        "1\n00:00:01 --> 00:00:03\nHello\n",
        expected_language="en",
    )

    assert any("malformed timestamp" in warning.lower() for warning in quality["quality_warnings"])
    assert quality["reject_hint"] is True


def test_overlapping_cue_detection() -> None:
    quality = analyze_srt_quality(
        "1\n00:00:01,000 --> 00:00:04,000\nHello\n\n"
        "2\n00:00:03,500 --> 00:00:05,000\nOverlap\n",
        expected_language="en",
    )

    assert any("overlapping cues" in warning.lower() for warning in quality["quality_warnings"])
    assert quality["reject_hint"] is True


def test_repeated_line_warning() -> None:
    quality = analyze_srt_quality(
        "1\n00:00:01,000 --> 00:00:02,000\nSame line\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nSame line\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nSame line\n\n"
        "4\n00:00:07,000 --> 00:00:08,000\nSame line\n",
        expected_language="en",
    )

    assert any("repeats the same text" in warning.lower() for warning in quality["quality_warnings"])


def test_wrong_language_and_suspicious_tiny_file_warning() -> None:
    quality = analyze_srt_quality(
        "1\n00:00:01,000 --> 00:00:02,000\nهذه ترجمة عربية جدا\n",
        expected_language="en",
    )

    assert any("does not look like en" in warning.lower() for warning in quality["quality_warnings"])
    assert any("suspiciously tiny" in warning.lower() for warning in quality["quality_warnings"])
    assert quality["quality_level"] == "bad"
    assert quality["reject_hint"] is True


def test_import_best_returns_quality_metadata(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "phase20-best",
                    "language": "EN",
                    "release_name": "Movie.1080p.WEB-DL",
                    "download_url": "https://subdl.local/phase20-best.srt",
                    "score": 96.0,
                    "score_breakdown": {
                        "imdb_match": 40.0,
                        "language_match": 15.0,
                        "season_match": 0.0,
                        "episode_match": 0.0,
                        "release_similarity": 22.0,
                        "important_tokens": 2.5,
                        "release_episode_match": 0.0,
                    },
                    "match_confidence": "high",
                    "match_warnings": [],
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: GOOD_SRT.encode("utf-8"),
    )

    response = client.post("/companion/import-best", json={"video_id": "tt2000001"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["quality_level"] == "good"
    assert isinstance(payload["quality_score"], int)
    assert payload["quality_warnings"] == []
    assert payload["reject_hint"] is False


def test_bad_quality_does_not_crash_batch_prepare(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "phase20-bad",
                    "language": "EN",
                    "release_name": "Show.S01E01.1080p.WEB-DL",
                    "download_url": "https://subdl.local/phase20-bad.srt",
                    "score": 74.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: (
            "1\n00:00:01,000 --> 00:00:02,000\nهذه ترجمة عربية جدا\n"
        ).encode("utf-8"),
    )
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-phase20-bad", "status": "queued"},
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2000002", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]

    for _ in range(100):
        status_response = client.get(f"/companion/batch-status/{batch_id}")
        assert status_response.status_code == 200, status_response.text
        payload = status_response.json()
        if payload["status"] in {"completed", "partial", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Batch prepare did not finish in time.")

    assert payload["status"] == "completed"
    assert payload["items"][0]["status"] == "completed"
    assert payload["items"][0]["quality_level"] == "bad"
    assert payload["items"][0]["reject_hint"] is True
    assert any(
        "suspiciously tiny" in warning.lower() or "does not look like en" in warning.lower()
        for warning in payload["items"][0]["quality_warnings"]
    )
