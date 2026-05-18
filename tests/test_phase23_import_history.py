"""Tests for Phase 23 provider import history and duplicate prevention."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, job_manager, provider_import_history, provider_router


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
    video_id: str,
    items: list[dict],
    payload_by_url: dict[str, bytes] | None = None,
    download_side_effect=None,
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
    if download_side_effect is not None:
        monkeypatch.setattr(provider_router, "download_subtitle_data", download_side_effect)
    else:
        payload_map = payload_by_url or {}
        monkeypatch.setattr(
            provider_router,
            "download_subtitle_data",
            lambda provider, url: payload_map[url],
        )
    payload = {"video_id": video_id}
    if body:
        payload.update(body)
    response = client.post("/companion/import-best", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


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


def test_imported_candidate_is_recorded(client: TestClient, monkeypatch) -> None:
    payload = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2300001",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="hist-1",
                download_url="https://subdl.local/hist-1.srt",
                score=96.0,
                release_name="Movie.History.One",
            )
        ],
        payload_by_url={"https://subdl.local/hist-1.srt": GOOD_SRT.encode("utf-8")},
    )

    history_payload = client.get("/companion/provider-import-history")
    assert history_payload.status_code == 200, history_payload.text
    items = history_payload.json()["items"]
    assert len(items) == 1
    assert items[0]["provider"] == "subdl"
    assert items[0]["release_name"] == "Movie.History.One"
    assert items[0]["video_identity"] == "tt2300001"
    assert items[0]["import_count"] == 1
    assert payload["import_history"]["record_id"] == payload["record_id"]


def test_repeated_import_best_reuses_existing_record(client: TestClient, monkeypatch) -> None:
    first = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2300002",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="reuse-me",
                download_url="https://subdl.local/reuse-me.srt",
                score=95.0,
                release_name="Movie.Reuse.Me",
            )
        ],
        payload_by_url={"https://subdl.local/reuse-me.srt": GOOD_SRT.encode("utf-8")},
    )
    second = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2300002",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="reuse-me",
                download_url="https://subdl.local/reuse-me.srt",
                score=95.0,
                release_name="Movie.Reuse.Me",
            )
        ],
        download_side_effect=lambda provider, url: (_ for _ in ()).throw(
            provider_router.SubDLError("provider download should not be called for reused import")
        ),
        body={"force_provider_search": True},
    )

    assert second["record_id"] == first["record_id"]
    assert second["reused_existing_record"] is True
    assert "cached record was reused" in second["selected_reason"]
    assert len(cache_db.list_subtitles(config.DB_PATH)) == 1


def test_different_better_candidate_can_still_be_imported(client: TestClient, monkeypatch) -> None:
    first = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2300003",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="old-choice",
                download_url="https://subdl.local/old-choice.srt",
                score=90.0,
                release_name="Movie.Old.Choice",
            )
        ],
        payload_by_url={"https://subdl.local/old-choice.srt": GOOD_SRT.encode("utf-8")},
    )
    second = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2300003",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="old-choice",
                download_url="https://subdl.local/old-choice.srt",
                score=90.0,
                release_name="Movie.Old.Choice",
            ),
            _candidate(
                provider="subsource",
                subtitle_id="better-choice",
                download_url="https://subsource.local/better-choice.srt",
                score=97.0,
                release_name="Movie.Better.Choice",
            ),
        ],
        payload_by_url={
            "https://subdl.local/old-choice.srt": GOOD_SRT.encode("utf-8"),
            "https://subsource.local/better-choice.srt": GOOD_SRT.encode("utf-8"),
        },
        body={"force_provider_search": True},
    )

    assert second["record_id"] != first["record_id"]
    assert second["provider"] == "subsource"
    assert second["reused_existing_record"] is False
    assert len(cache_db.list_subtitles(config.DB_PATH)) == 2


def test_import_count_increments(client: TestClient, monkeypatch) -> None:
    for _ in range(2):
        _run_import_best(
            client,
            monkeypatch,
            video_id="tt2300004",
            items=[
                _candidate(
                    provider="subdl",
                    subtitle_id="count-me",
                    download_url="https://subdl.local/count-me.srt",
                    score=94.0,
                    release_name="Movie.Count.Me",
                )
            ],
            payload_by_url={"https://subdl.local/count-me.srt": GOOD_SRT.encode("utf-8")},
        )

    history_payload = client.get("/companion/provider-import-history")
    assert history_payload.status_code == 200, history_payload.text
    assert history_payload.json()["items"][0]["import_count"] == 2


def test_clear_endpoint(client: TestClient, monkeypatch) -> None:
    _run_import_best(
        client,
        monkeypatch,
        video_id="tt2300005",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="clear-hist",
                download_url="https://subdl.local/clear-hist.srt",
                score=94.0,
                release_name="Movie.Clear.History",
            )
        ],
        payload_by_url={"https://subdl.local/clear-hist.srt": GOOD_SRT.encode("utf-8")},
    )

    clear_response = client.post("/companion/provider-import-history/clear")
    assert clear_response.status_code == 200, clear_response.text
    assert clear_response.json()["status"] == "cleared"
    assert clear_response.json()["cleared_count"] >= 1

    history_payload = client.get("/companion/provider-import-history")
    assert history_payload.status_code == 200, history_payload.text
    assert history_payload.json()["items"] == []


def test_batch_prepare_avoids_duplicate_provider_import_records(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("SUBDL_API_KEY", "subdl-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="batch-reuse",
                    download_url="https://subdl.local/batch-reuse.srt",
                    score=96.0,
                    release_name="Show.S01E01.Batch.Reuse",
                )
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
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-phase23-reuse", "status": "queued", "record_id": kwargs["record_id"]},
    )

    first = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2300006", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert first.status_code == 200, first.text
    first_payload = _wait_for_batch(client, first.json()["batch_id"])
    first_record_id = int(first_payload["items"][0]["record_id"])

    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: (_ for _ in ()).throw(
            provider_router.SubDLError("provider download should not be called for reused batch import")
        ),
    )

    second = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2300006", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert second.status_code == 200, second.text
    second_payload = _wait_for_batch(client, second.json()["batch_id"])
    second_record_id = int(second_payload["items"][0]["record_id"])

    assert first_record_id == second_record_id
    assert len(cache_db.list_subtitles(config.DB_PATH)) == 1

    history = provider_import_history.list_entries(config.DB_PATH)
    assert len(history) == 1
    assert history[0]["import_count"] == 2
