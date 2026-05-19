"""Tests for Phase 25 cache integrity verification and safe reuse behavior."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, cache_integrity, provider_router


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

INVALID_SRT = "not actually an srt file"
MALFORMED_TIMESTAMP_SRT = "1\nBAD --> TIMESTAMP\nText\n"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _configured_provider_status() -> dict:
    return {
        "gemini": {"configured": False, "message": "disabled"},
        "subdl": {"configured": True, "message": "ok"},
        "subsource": {"configured": False, "message": "disabled"},
        "opensubtitles": {"configured": False, "message": "disabled"},
    }


def _disabled_provider_status() -> dict:
    return {
        "gemini": {"configured": False, "message": "disabled"},
        "subdl": {"configured": False, "message": "disabled"},
        "subsource": {"configured": False, "message": "disabled"},
        "opensubtitles": {"configured": False, "message": "disabled"},
    }


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


def _cached_record(record_id: int) -> dict:
    record = cache_db.get_record(config.DB_PATH, record_id)
    assert record is not None
    return record


def test_valid_local_cache_reuse_includes_integrity_metadata(client: TestClient, monkeypatch) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500001",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="valid-local",
                download_url="https://subdl.local/valid-local.srt",
                score=96.0,
                release_name="Movie.Valid.Local",
            )
        ],
        payload_by_url={"https://subdl.local/valid-local.srt": GOOD_SRT.encode("utf-8")},
    )

    monkeypatch.setattr(provider_router, "get_provider_status", _configured_provider_status)
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("provider search should not run for valid local-first reuse")
        ),
    )

    reused = client.post("/companion/import-best", json={"video_id": "tt2500001"})
    assert reused.status_code == 200, reused.text
    payload = reused.json()
    assert payload["record_id"] == seeded["record_id"]
    assert payload["local_first_reused"] is True
    assert payload["cache_integrity"]["integrity_status"] == cache_integrity.STATUS_VALID


def test_missing_cached_file_is_not_reused_before_provider_search(client: TestClient, monkeypatch) -> None:
    first = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500002",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="missing-local",
                download_url="https://subdl.local/missing-local.srt",
                score=96.0,
                release_name="Movie.Missing.Local",
            )
        ],
        payload_by_url={"https://subdl.local/missing-local.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(first["record_id"]))
    Path(str(record["english_srt_path"])).unlink()

    second = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500002",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="missing-local",
                download_url="https://subdl.local/missing-local.srt",
                score=96.0,
                release_name="Movie.Missing.Local",
            )
        ],
        payload_by_url={"https://subdl.local/missing-local.srt": GOOD_SRT.encode("utf-8")},
    )

    assert second["record_id"] != first["record_id"]
    assert second["local_first_reused"] is False
    assert second["searched_providers"] == ["subdl"]
    assert second["cache_integrity"]["integrity_status"] == cache_integrity.STATUS_MISSING_FILE


def test_invalid_cached_srt_is_not_reused(client: TestClient, monkeypatch) -> None:
    first = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500003",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="invalid-local",
                download_url="https://subdl.local/invalid-local.srt",
                score=95.0,
                release_name="Movie.Invalid.Local",
            )
        ],
        payload_by_url={"https://subdl.local/invalid-local.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(first["record_id"]))
    Path(str(record["english_srt_path"])).write_text(INVALID_SRT, encoding="utf-8")

    second = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500003",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="invalid-local",
                download_url="https://subdl.local/invalid-local.srt",
                score=95.0,
                release_name="Movie.Invalid.Local",
            )
        ],
        payload_by_url={"https://subdl.local/invalid-local.srt": GOOD_SRT.encode("utf-8")},
    )

    assert second["record_id"] != first["record_id"]
    assert second["cache_integrity"]["integrity_status"] == cache_integrity.STATUS_INVALID_SRT


def test_verify_record_rejects_malformed_timestamp_cached_file(
    client: TestClient,
    monkeypatch,
) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt25000031",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="direct-malformed",
                download_url="https://subdl.local/direct-malformed.srt",
                score=95.0,
                release_name="Movie.Direct.Malformed",
            )
        ],
        payload_by_url={"https://subdl.local/direct-malformed.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(seeded["record_id"]))
    Path(str(record["english_srt_path"])).write_text(MALFORMED_TIMESTAMP_SRT, encoding="utf-8")

    integrity = cache_integrity.verify_record(
        config.DB_PATH,
        record_id=int(seeded["record_id"]),
        provider="subdl",
        release_name="Movie.Direct.Malformed",
        video_identity="tt25000031",
        quality_level="good",
        quality_score=100,
    )

    assert integrity["integrity_status"] == cache_integrity.STATUS_INVALID_SRT
    assert integrity["quality_acceptable"] is False
    assert integrity["integrity_warnings"]
    assert "timestamp" in integrity["integrity_warnings"][0].lower()


def test_malformed_timestamp_cached_srt_is_not_reused_before_provider_search(
    client: TestClient,
    monkeypatch,
) -> None:
    first = _run_import_best(
        client,
        monkeypatch,
        video_id="tt25000032",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="malformed-local",
                download_url="https://subdl.local/malformed-local.srt",
                score=96.0,
                release_name="Movie.Malformed.Local",
            )
        ],
        payload_by_url={"https://subdl.local/malformed-local.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(first["record_id"]))
    Path(str(record["english_srt_path"])).write_text(MALFORMED_TIMESTAMP_SRT, encoding="utf-8")

    calls = {"search": 0}

    def fake_search(**kwargs):
        calls["search"] += 1
        return {
            "items": [
                _candidate(
                    provider="subdl",
                    subtitle_id="malformed-local",
                    download_url="https://subdl.local/malformed-local.srt",
                    score=96.0,
                    release_name="Movie.Malformed.Local",
                )
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        }

    monkeypatch.setattr(provider_router, "search_all_subtitles", fake_search)
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: GOOD_SRT.encode("utf-8"),
    )

    second = client.post("/companion/import-best", json={"video_id": "tt25000032"})
    assert second.status_code == 200, second.text
    payload = second.json()

    assert calls["search"] == 1
    assert payload["record_id"] != first["record_id"]
    assert payload["local_first_reused"] is False
    assert payload["cache_integrity"]["integrity_status"] != cache_integrity.STATUS_VALID or payload["cache_integrity"]["quality_acceptable"] is False


def test_exact_candidate_reuse_verifies_integrity_before_reusing(client: TestClient, monkeypatch) -> None:
    first = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500004",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="exact-stale",
                download_url="https://subdl.local/exact-stale.srt",
                score=95.0,
                release_name="Movie.Exact.Stale",
            )
        ],
        payload_by_url={"https://subdl.local/exact-stale.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(first["record_id"]))
    Path(str(record["english_srt_path"])).unlink()

    second = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500004",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="exact-stale",
                download_url="https://subdl.local/exact-stale.srt",
                score=95.0,
                release_name="Movie.Exact.Stale",
            )
        ],
        payload_by_url={"https://subdl.local/exact-stale.srt": GOOD_SRT.encode("utf-8")},
        body={"force_provider_search": True},
    )

    assert second["record_id"] != first["record_id"]
    assert second["reused_existing_record"] is False
    assert second["cache_integrity"]["integrity_status"] == cache_integrity.STATUS_MISSING_FILE


def test_cache_integrity_endpoints_do_not_expose_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500005",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="endpoint-check",
                download_url="https://subdl.local/endpoint-check.srt",
                score=94.0,
                release_name="Movie.Endpoint.Check",
            )
        ],
        payload_by_url={"https://subdl.local/endpoint-check.srt": GOOD_SRT.encode("utf-8")},
    )

    scan = client.post("/companion/cache-integrity/scan")
    assert scan.status_code == 200, scan.text
    payload = client.get("/companion/cache-integrity")
    assert payload.status_code == 200, payload.text
    body = payload.json()
    serialized = json.dumps(body)
    assert "Hello there" not in serialized
    assert "General Kenobi" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized
    assert body["counts"]["valid"] >= 1

    repaired = client.post("/companion/cache-integrity/repair-metadata")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["last_repair_metadata_at"] is not None


def test_batch_prepare_handles_stale_local_record_safely(client: TestClient, monkeypatch) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500006:1:1",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="batch-stale",
                download_url="https://subdl.local/batch-stale.srt",
                score=95.0,
                release_name="Show.S01E01.Batch.Stale",
            )
        ],
        payload_by_url={"https://subdl.local/batch-stale.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(seeded["record_id"]))
    Path(str(record["english_srt_path"])).unlink()

    monkeypatch.setattr(provider_router, "get_provider_status", _disabled_provider_status)
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("provider search should not run when providers are unavailable")
        ),
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2500006", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    final = _wait_for_batch(client, response.json()["batch_id"])
    item = final["items"][0]

    assert final["status"] == "failed"
    assert item["status"] == "failed"
    assert item["cache_integrity"]["integrity_status"] == cache_integrity.STATUS_MISSING_FILE
    assert "could not be reused safely" in str(item["error_message"] or "").lower()


def test_batch_prepare_does_not_reuse_malformed_cached_srt(client: TestClient, monkeypatch) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2500007:1:1",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="batch-malformed",
                download_url="https://subdl.local/batch-malformed.srt",
                score=95.0,
                release_name="Show.S01E01.Batch.Malformed",
            )
        ],
        payload_by_url={"https://subdl.local/batch-malformed.srt": GOOD_SRT.encode("utf-8")},
    )
    record = _cached_record(int(seeded["record_id"]))
    Path(str(record["english_srt_path"])).write_text(MALFORMED_TIMESTAMP_SRT, encoding="utf-8")

    monkeypatch.setattr(provider_router, "get_provider_status", _disabled_provider_status)
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("provider search should not run when providers are unavailable")
        ),
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt2500007", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    final = _wait_for_batch(client, response.json()["batch_id"])
    item = final["items"][0]

    assert final["status"] == "failed"
    assert item["status"] == "failed"
    assert item["cache_integrity"]["integrity_status"] == cache_integrity.STATUS_INVALID_SRT
    assert item["local_first_reused"] is False
