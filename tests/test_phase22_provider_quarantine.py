"""Tests for Phase 22 provider candidate quarantine memory."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from services import provider_quarantine, provider_router


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


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _candidate(
    *,
    provider: str,
    subtitle_id: str,
    download_url: str,
    score: float,
    release_name: str,
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
            "release_similarity": 18.0,
            "important_tokens": 2.5,
            "release_episode_match": 0.0,
        },
        "match_confidence": "high",
        "match_warnings": [],
    }


def _run_import_best(
    client: TestClient,
    monkeypatch,
    *,
    items: list[dict],
    payload_by_url: dict[str, bytes],
    body: dict | None = None,
) -> dict:
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": items,
            "provider_errors": {},
            "searched_providers": sorted({str(item.get("provider") or "") for item in items}),
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: payload_by_url[url],
    )
    payload = {"video_id": "tt2200001"}
    if body:
        payload.update(body)
    response = client.post("/companion/import-best", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_skipped_bad_candidate_is_recorded(client: TestClient, monkeypatch) -> None:
    payload = _run_import_best(
        client,
        monkeypatch,
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="bad-top",
                download_url="https://subdl.local/bad-top.srt",
                score=98.0,
                release_name="Movie.Bad.Top",
            ),
            _candidate(
                provider="subsource",
                subtitle_id="good-second",
                download_url="https://subsource.local/good-second.srt",
                score=90.0,
                release_name="Movie.Good.Second",
            ),
        ],
        payload_by_url={
            "https://subdl.local/bad-top.srt": BAD_QUALITY_SRT.encode("utf-8"),
            "https://subsource.local/good-second.srt": GOOD_SRT.encode("utf-8"),
        },
    )

    assert payload["tried_candidates"][0]["skip_reason"] == "bad_quality"
    quarantine_payload = client.get("/companion/provider-quarantine")
    assert quarantine_payload.status_code == 200, quarantine_payload.text
    items = quarantine_payload.json()["items"]
    assert len(items) == 1
    assert items[0]["provider"] == "subdl"
    assert items[0]["release_name"] == "Movie.Bad.Top"
    assert items[0]["reason"] == "bad_quality"
    assert items[0]["fail_count"] == 1


def test_fail_count_increments(client: TestClient, monkeypatch) -> None:
    for _ in range(2):
        _run_import_best(
            client,
            monkeypatch,
            items=[
                _candidate(
                    provider="subdl",
                    subtitle_id="repeat-bad",
                    download_url="https://subdl.local/repeat-bad.srt",
                    score=97.0,
                    release_name="Movie.Repeat.Bad",
                ),
                _candidate(
                    provider="subsource",
                    subtitle_id="clean-fallback",
                    download_url="https://subsource.local/clean-fallback.srt",
                    score=90.0,
                    release_name="Movie.Clean.Fallback",
                ),
            ],
            payload_by_url={
                "https://subdl.local/repeat-bad.srt": BAD_QUALITY_SRT.encode("utf-8"),
                "https://subsource.local/clean-fallback.srt": GOOD_SRT.encode("utf-8"),
            },
            body={"force_provider_search": True},
        )

    quarantine_payload = client.get("/companion/provider-quarantine")
    assert quarantine_payload.status_code == 200, quarantine_payload.text
    items = quarantine_payload.json()["items"]
    assert items[0]["fail_count"] == 2


def test_repeated_bad_candidate_is_deprioritized(client: TestClient, monkeypatch) -> None:
    for _ in range(2):
        _run_import_best(
            client,
            monkeypatch,
            items=[
                _candidate(
                    provider="subdl",
                    subtitle_id="tainted",
                    download_url="https://subdl.local/tainted.srt",
                    score=95.0,
                    release_name="Movie.Tainted",
                ),
                _candidate(
                    provider="subsource",
                    subtitle_id="bootstrap-good",
                    download_url="https://subsource.local/bootstrap-good.srt",
                    score=88.0,
                    release_name="Movie.Bootstrap.Good",
                ),
            ],
            payload_by_url={
                "https://subdl.local/tainted.srt": BAD_QUALITY_SRT.encode("utf-8"),
                "https://subsource.local/bootstrap-good.srt": GOOD_SRT.encode("utf-8"),
            },
            body={"force_provider_search": True},
        )

    payload = _run_import_best(
        client,
        monkeypatch,
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="tainted",
                download_url="https://subdl.local/tainted.srt",
                score=94.0,
                release_name="Movie.Tainted",
            ),
            _candidate(
                provider="subsource",
                subtitle_id="clean-close",
                download_url="https://subsource.local/clean-close.srt",
                score=91.0,
                release_name="Movie.Clean.Close",
            ),
        ],
        payload_by_url={
            "https://subdl.local/tainted.srt": GOOD_SRT.encode("utf-8"),
            "https://subsource.local/clean-close.srt": GOOD_SRT.encode("utf-8"),
        },
        body={"force_provider_search": True},
    )

    assert payload["provider"] == "subsource"
    assert payload["quarantine_affected_selection"] is True
    assert "Quarantine memory deprioritized" in payload["selected_reason"]
    assert "Quarantine memory deprioritized" in payload["fallback_reason"]


def test_quarantine_does_not_hard_block_when_all_candidates_quarantined(
    client: TestClient,
    monkeypatch,
) -> None:
    for _ in range(2):
        _run_import_best(
            client,
            monkeypatch,
            items=[
                _candidate(
                    provider="subdl",
                    subtitle_id="only-candidate",
                    download_url="https://subdl.local/only-candidate.srt",
                    score=96.0,
                    release_name="Movie.Only.Candidate",
                ),
            ],
            payload_by_url={
                "https://subdl.local/only-candidate.srt": BAD_QUALITY_SRT.encode("utf-8"),
            },
            body={"force_provider_search": True},
        )

    payload = _run_import_best(
        client,
        monkeypatch,
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="only-candidate",
                download_url="https://subdl.local/only-candidate.srt",
                score=96.0,
                release_name="Movie.Only.Candidate",
            ),
        ],
        payload_by_url={
            "https://subdl.local/only-candidate.srt": GOOD_SRT.encode("utf-8"),
        },
        body={"force_provider_search": True},
    )

    assert payload["provider"] == "subdl"
    assert payload["status"] == "uploaded"
    assert payload["quarantine"]["fail_count"] >= provider_quarantine.QUARANTINE_THRESHOLD


def test_clear_endpoint(client: TestClient, monkeypatch) -> None:
    _run_import_best(
        client,
        monkeypatch,
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="to-clear",
                download_url="https://subdl.local/to-clear.srt",
                score=97.0,
                release_name="Movie.To.Clear",
            ),
            _candidate(
                provider="subsource",
                subtitle_id="clear-good",
                download_url="https://subsource.local/clear-good.srt",
                score=90.0,
                release_name="Movie.Clear.Good",
            ),
        ],
        payload_by_url={
            "https://subdl.local/to-clear.srt": BAD_QUALITY_SRT.encode("utf-8"),
            "https://subsource.local/clear-good.srt": GOOD_SRT.encode("utf-8"),
        },
    )

    clear_response = client.post("/companion/provider-quarantine/clear")
    assert clear_response.status_code == 200, clear_response.text
    assert clear_response.json()["status"] == "cleared"
    assert clear_response.json()["cleared_count"] >= 1

    quarantine_payload = client.get("/companion/provider-quarantine")
    assert quarantine_payload.status_code == 200, quarantine_payload.text
    assert quarantine_payload.json()["items"] == []
