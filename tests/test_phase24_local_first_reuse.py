"""Tests for Phase 24 local-first subtitle reuse."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, job_manager, provider_import_history, provider_router

ORIGINAL_SEARCH_ALL_SUBTITLES = provider_router.search_all_subtitles


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
    items: list[dict] | None = None,
    payload_by_url: dict[str, bytes] | None = None,
    body: dict | None = None,
) -> dict:
    if items is not None:
        monkeypatch.setattr(
            provider_router,
            "search_all_subtitles",
            lambda **kwargs: {
                "items": items,
                "provider_errors": {},
                "searched_providers": sorted({str(item.get("provider") or "") for item in items}),
            },
        )
    if payload_by_url is not None:
        monkeypatch.setattr(
            provider_router,
            "download_subtitle_data",
            lambda provider, url: payload_by_url[url],
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


def _seed_bad_local_import(tmp_path: Path, *, video_id: str, release_name: str) -> int:
    identity = video_id if ":" not in video_id else video_id
    english_path = tmp_path / "bad-local.srt"
    english_path.write_text(GOOD_SRT, encoding="utf-8")
    record_id = cache_db.insert_subtitle(
        config.DB_PATH,
        video_id=video_id,
        imdb_id=video_id.split(":")[0],
        season=None,
        episode=None,
        canonical_video_key=identity,
        video_type="movie",
        release_name=release_name,
        english_srt_path=str(english_path),
        english_srt_hash="bad-local-hash",
        status="uploaded",
        source_provider="subdl",
        source_subtitle_id="bad-local",
        source_download_url="https://subdl.local/bad-local.srt",
    )
    provider_import_history.record_import(
        config.DB_PATH,
        provider="subdl",
        subtitle_id="bad-local",
        download_url="https://subdl.local/bad-local.srt",
        release_name=release_name,
        video_identity=identity,
        season=None,
        episode=None,
        record_id=record_id,
        quality_level="bad",
        quality_score=22,
    )
    return record_id


def test_import_best_reuses_existing_local_record_before_provider_search(client: TestClient, monkeypatch) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2400001",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="local-first",
                download_url="https://subdl.local/local-first.srt",
                score=96.0,
                release_name="Movie.Local.First",
            )
        ],
        payload_by_url={"https://subdl.local/local-first.srt": GOOD_SRT.encode("utf-8")},
    )

    monkeypatch.setattr(provider_router, "search_all_subtitles", ORIGINAL_SEARCH_ALL_SUBTITLES)
    reused = _run_import_best(client, monkeypatch, video_id="tt2400001", items=None)

    assert reused["record_id"] == seeded["record_id"]
    assert reused["reused_existing_record"] is True
    assert reused["local_first_reused"] is True
    assert reused["searched_providers"] == []
    assert reused["provider_errors"] == {}
    assert "before provider search" in reused["local_reuse_reason"]
    assert len(cache_db.list_subtitles(config.DB_PATH)) == 1


def test_force_provider_search_bypasses_local_reuse(client: TestClient, monkeypatch) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2400002",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="force-search",
                download_url="https://subdl.local/force-search.srt",
                score=95.0,
                release_name="Movie.Force.Search",
            )
        ],
        payload_by_url={"https://subdl.local/force-search.srt": GOOD_SRT.encode("utf-8")},
    )

    calls = {"search": 0}

    def fake_search(**kwargs):
        calls["search"] += 1
        return {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="force-search",
                    download_url="https://subdl.local/force-search.srt",
                    score=95.0,
                    release_name="Movie.Force.Search",
                )
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        }

    monkeypatch.setattr(provider_router, "search_all_subtitles", fake_search)
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: (_ for _ in ()).throw(
            provider_router.SubDLError("download should not be needed after exact-candidate reuse")
        ),
    )

    reused = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2400002",
        items=None,
        body={"force_provider_search": True},
    )

    assert calls["search"] == 1
    assert reused["record_id"] == seeded["record_id"]
    assert reused["reused_existing_record"] is True
    assert reused["local_first_reused"] is False
    assert reused["searched_providers"] == ["subdl"]


def test_bad_quality_local_record_is_not_preferred_over_provider_search(
    client: TestClient,
    monkeypatch,
    tmp_path: Path,
) -> None:
    bad_record_id = _seed_bad_local_import(
        tmp_path,
        video_id="tt2400003",
        release_name="Movie.Bad.Local",
    )

    imported = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2400003",
        items=[
            _candidate(
                provider="subsource",
                subtitle_id="better-provider",
                download_url="https://subsource.local/better-provider.srt",
                score=97.0,
                release_name="Movie.Better.Provider",
            )
        ],
        payload_by_url={"https://subsource.local/better-provider.srt": GOOD_SRT.encode("utf-8")},
    )

    assert imported["record_id"] != bad_record_id
    assert imported["provider"] == "subsource"
    assert imported["local_first_reused"] is False
    assert len(cache_db.list_subtitles(config.DB_PATH)) == 2


def test_batch_prepare_reuses_local_record_when_providers_are_unavailable(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2400004:1:1",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="batch-local",
                download_url="https://subdl.local/batch-local.srt",
                score=96.0,
                release_name="Show.S01E01.Batch.Local",
            )
        ],
        payload_by_url={"https://subdl.local/batch-local.srt": GOOD_SRT.encode("utf-8")},
    )

    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("provider search should not run for local-first batch reuse")
        ),
    )
    monkeypatch.setattr(
        job_manager,
        "start_translation_job",
        lambda **kwargs: {"job_id": "job-phase24-local", "status": "queued", "record_id": kwargs["record_id"]},
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2400004", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    final = _wait_for_batch(client, response.json()["batch_id"])
    item = final["items"][0]

    assert item["record_id"] == seeded["record_id"]
    assert item["local_first_reused"] is True
    assert "before provider search" in item["local_reuse_reason"]
    assert len(cache_db.list_subtitles(config.DB_PATH)) == 1
