"""Tests for Phase 26 cache maintenance scanning and safe orphan cleanup."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, cache_maintenance, provider_router


GOOD_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,000\n"
    "Hello there\n"
    "\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,000\n"
    "General Kenobi\n"
    "\n"
)

ARABIC_SRT = "1\n00:00:01,000 --> 00:00:03,000\nمرحبا\n"


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


def _contains_path(items: list[dict], target: Path) -> bool:
    normalized = str(target.resolve())
    return any(str(item.get("path") or "") == normalized for item in items)


def test_orphan_english_file_detection() -> None:
    orphan = config.ENGLISH_CACHE_DIR / "orphan-english.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    assert _contains_path(summary["orphan_files"], orphan)
    assert any(
        item["action"] == cache_maintenance.CLEANUP_ACTION_DELETE_CANDIDATE
        and str(item.get("path") or "") == str(orphan.resolve())
        for item in summary["cleanup_candidates"]
    )


def test_orphan_arabic_file_detection() -> None:
    orphan = config.ARABIC_CACHE_DIR / "orphan-arabic.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(ARABIC_SRT, encoding="utf-8")

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    assert _contains_path(summary["orphan_files"], orphan)
    assert any(
        str(item.get("path") or "") == str(orphan.resolve())
        and str(item.get("language") or "") == "arabic"
        for item in summary["cleanup_candidates"]
    )


def test_referenced_valid_files_are_protected(client: TestClient, monkeypatch) -> None:
    seeded = _run_import_best(
        client,
        monkeypatch,
        video_id="tt2600001",
        items=[
            _candidate(
                provider="subdl",
                subtitle_id="protected-record",
                download_url="https://subdl.local/protected-record.srt",
                score=96.0,
                release_name="Movie.Protected.Record",
            )
        ],
        payload_by_url={"https://subdl.local/protected-record.srt": GOOD_SRT.encode("utf-8")},
    )
    record = cache_db.get_record(config.DB_PATH, int(seeded["record_id"]))
    assert record is not None
    arabic_path = config.ARABIC_CACHE_DIR / "protected-record.ar.srt"
    arabic_path.parent.mkdir(parents=True, exist_ok=True)
    arabic_path.write_text(ARABIC_SRT, encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, int(seeded["record_id"]), str(arabic_path), status="translated")

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    english_path = Path(str(record["english_srt_path"]))
    assert _contains_path(summary["protected_files"], english_path)
    assert _contains_path(summary["protected_files"], arabic_path)
    assert not any(
        str(item.get("path") or "") in {str(english_path.resolve()), str(arabic_path.resolve())}
        and item["action"] == cache_maintenance.CLEANUP_ACTION_DELETE_CANDIDATE
        for item in summary["cleanup_candidates"]
    )


def test_missing_db_file_reference_is_reported() -> None:
    missing_path = config.ENGLISH_CACHE_DIR / "missing-record.srt"
    record_id = cache_db.insert_subtitle(
        config.DB_PATH,
        video_id="tt2600002",
        video_type="movie",
        release_name="Missing.Record",
        english_srt_path=str(missing_path),
        english_srt_hash="hash-missing",
        status="uploaded",
    )

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    assert any(int(item["record_id"]) == record_id for item in summary["missing_references"])


def test_dry_run_cleanup_deletes_nothing() -> None:
    orphan = config.ENGLISH_CACHE_DIR / "dry-run-orphan.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    result = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=True,
        allow_delete=False,
    )

    assert result["dry_run"] is True
    assert result["deleted_count"] == 0
    assert orphan.exists()


def test_allow_delete_deletes_only_safe_orphan_files() -> None:
    orphan = config.ENGLISH_CACHE_DIR / "delete-me.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    referenced = config.ENGLISH_CACHE_DIR / "keep-me.srt"
    referenced.write_text(GOOD_SRT, encoding="utf-8")
    cache_db.insert_subtitle(
        config.DB_PATH,
        video_id="tt2600003",
        video_type="movie",
        release_name="Keep.Me",
        english_srt_path=str(referenced),
        english_srt_hash="hash-keep",
        status="uploaded",
    )

    result = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )

    assert result["deleted_count"] == 1
    assert not orphan.exists()
    assert referenced.exists()


def test_path_traversal_or_outside_cache_paths_are_protected(tmp_path: Path) -> None:
    outside = tmp_path / "outside-cache" / "external.srt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(GOOD_SRT, encoding="utf-8")
    record_id = cache_db.insert_subtitle(
        config.DB_PATH,
        video_id="tt2600004",
        video_type="movie",
        release_name="Outside.Cache",
        english_srt_path=str(outside),
        english_srt_hash="hash-outside",
        status="uploaded",
    )

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    assert any(
        int(item.get("record_id") or 0) == record_id
        and str(item.get("path") or "") == str(outside.resolve())
        for item in summary["protected_files"]
    )
    assert not any(
        str(item.get("path") or "") == str(outside.resolve())
        and item["action"] == cache_maintenance.CLEANUP_ACTION_DELETE_CANDIDATE
        for item in summary["cleanup_candidates"]
    )


def test_preferred_or_translated_records_are_never_cleanup_candidates() -> None:
    english_path = config.ENGLISH_CACHE_DIR / "preferred-keep.srt"
    english_path.parent.mkdir(parents=True, exist_ok=True)
    english_path.write_text(GOOD_SRT, encoding="utf-8")
    arabic_path = config.ARABIC_CACHE_DIR / "preferred-keep.ar.srt"
    arabic_path.parent.mkdir(parents=True, exist_ok=True)
    arabic_path.write_text(ARABIC_SRT, encoding="utf-8")
    record_id = cache_db.insert_subtitle(
        config.DB_PATH,
        video_id="tt2600005",
        canonical_video_key="tt2600005",
        video_type="movie",
        release_name="Preferred.Keep",
        english_srt_path=str(english_path),
        english_srt_hash="hash-preferred",
        arabic_srt_path=str(arabic_path),
        status="translated",
    )
    cache_db.set_preferred_record(
        config.DB_PATH,
        record_id,
        canonical_video_key="tt2600005",
        legacy_video_id="tt2600005",
    )

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    assert not any(int(item.get("record_id") or 0) == record_id for item in summary["cleanup_candidates"])
    assert any(int(item.get("record_id") or 0) == record_id for item in summary["protected_files"])


def test_old_failed_records_are_reported_as_metadata_only_candidates() -> None:
    failed_path = config.ENGLISH_CACHE_DIR / "old-failed.srt"
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    failed_path.write_text(GOOD_SRT, encoding="utf-8")
    record_id = cache_db.insert_subtitle(
        config.DB_PATH,
        video_id="tt2600006",
        video_type="movie",
        release_name="Old.Failed",
        english_srt_path=str(failed_path),
        english_srt_hash="hash-failed",
        status="failed",
        error_message="boom",
    )
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(str(config.DB_PATH)) as conn:
        conn.execute("UPDATE subtitles SET created_at = ? WHERE id = ?", (old_timestamp, record_id))
        conn.commit()

    summary = cache_maintenance.scan_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
    )

    assert any(
        int(item.get("record_id") or 0) == record_id
        and item["action"] == cache_maintenance.CLEANUP_ACTION_METADATA_ONLY
        for item in summary["cleanup_candidates"]
    )


def test_cache_maintenance_endpoints_do_not_expose_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    orphan = config.ENGLISH_CACHE_DIR / "endpoint-orphan.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("Never expose this subtitle text.\n" + GOOD_SRT, encoding="utf-8")

    scan = client.post("/companion/cache-maintenance/scan")
    assert scan.status_code == 200, scan.text
    summary = client.get("/companion/cache-maintenance")
    assert summary.status_code == 200, summary.text
    cleanup = client.post("/companion/cache-maintenance/cleanup", json={})
    assert cleanup.status_code == 200, cleanup.text
    assert cleanup.json()["dry_run"] is True
    assert orphan.exists()

    serialized = json.dumps(
        {
            "scan": scan.json(),
            "summary": summary.json(),
            "cleanup": cleanup.json(),
        }
    )
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized

