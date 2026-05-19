"""Tests for Phase 30 maintenance snapshot metadata and compare endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_maintenance


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


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _recycle_orphan(filename: str, content: str = GOOD_SRT) -> dict:
    orphan = config.ENGLISH_CACHE_DIR / filename
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(content, encoding="utf-8")
    result = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )
    return next(
        item
        for item in result["recycled_files"]
        if Path(str(item["original_path"])).name == filename
    )


def _latest_snapshot(client: TestClient, action: str) -> dict:
    response = client.get("/companion/cache-maintenance/snapshots?limit=25")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    return next(item for item in items if item["action"] == action)


def test_cleanup_creates_before_after_snapshot_metadata(client: TestClient) -> None:
    orphan = config.ENGLISH_CACHE_DIR / "phase30-cleanup.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert cleanup.status_code == 200, cleanup.text
    snapshot = _latest_snapshot(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)
    assert snapshot["policy_level"] == cache_maintenance.POLICY_SAFE_RECYCLE
    assert snapshot["result_status"] == "recycled"
    assert snapshot["counts_before"]["cleanup_candidates"] >= 1
    assert snapshot["counts_after"]["recycled_count"] == 1
    assert snapshot["affected_count"] == 1
    assert all(not str(path).startswith(str(config.BASE_DIR.resolve())) for path in snapshot["affected_paths"])


def test_restore_creates_snapshot_metadata(client: TestClient) -> None:
    recycle_item = _recycle_orphan("phase30-restore.srt")
    restored = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert restored.status_code == 200, restored.text
    snapshot = _latest_snapshot(client, cache_maintenance.SNAPSHOT_ACTION_RESTORE)
    assert snapshot["result_status"] == "restored"
    assert recycle_item["id"] in snapshot["recycle_item_ids"]
    assert snapshot["affected_count"] == 1
    assert snapshot["counts_after"]["restored_count"] == 1


def test_empty_recycle_bin_creates_snapshot_after_confirmation(client: TestClient) -> None:
    _recycle_orphan("phase30-empty-confirmed.srt")
    confirmed = client.post(
        "/companion/cache-recycle-bin/empty",
        json={
            "allow_empty": True,
            "confirmation_text": cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    snapshot = _latest_snapshot(client, cache_maintenance.SNAPSHOT_ACTION_EMPTY)
    assert snapshot["result_status"] == "emptied"
    assert snapshot["affected_count"] == 1
    assert snapshot["counts_after"]["emptied_count"] == 1


def test_denied_empty_attempt_records_safe_snapshot_or_audit_without_deletion(
    client: TestClient,
) -> None:
    recycle_item = _recycle_orphan("phase30-empty-denied.srt")
    denied = client.post("/companion/cache-recycle-bin/empty", json={})
    assert denied.status_code == 200, denied.text
    assert denied.json()["status"] == "empty_denied"
    assert Path(str(recycle_item["recycled_path"])).exists()

    snapshots = client.get("/companion/cache-maintenance/snapshots?limit=25")
    assert snapshots.status_code == 200, snapshots.text
    items = snapshots.json()["items"]
    has_denied_snapshot = any(
        item["action"] == cache_maintenance.SNAPSHOT_ACTION_EMPTY
        and item["result_status"] == "empty_denied"
        for item in items
    )
    audit = client.get("/companion/cache-maintenance/audit?limit=25")
    assert audit.status_code == 200, audit.text
    has_denied_audit = any(
        str(item.get("action") or "") == cache_maintenance.AUDIT_ACTION_EMPTY_DENIED
        for item in audit.json()["items"]
    )
    assert has_denied_snapshot or has_denied_audit


def test_snapshot_endpoints_do_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    _recycle_orphan(
        "phase30-secret-snapshot.srt",
        content="Never expose this subtitle text.\n" + GOOD_SRT,
    )
    snapshots = client.get("/companion/cache-maintenance/snapshots?limit=25")
    assert snapshots.status_code == 200, snapshots.text
    snapshot_id = snapshots.json()["items"][0]["snapshot_id"]
    detail = client.get(f"/companion/cache-maintenance/snapshots/{snapshot_id}")
    assert detail.status_code == 200, detail.text
    compare = client.post(
        "/companion/cache-maintenance/snapshots/compare",
        json={"snapshot_id": snapshot_id},
    )
    assert compare.status_code == 200, compare.text

    serialized = json.dumps(
        {
            "list": snapshots.json(),
            "detail": detail.json(),
            "compare": compare.json(),
        }
    )
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized


def test_compare_endpoint_returns_before_after_counts(client: TestClient) -> None:
    orphan = config.ENGLISH_CACHE_DIR / "phase30-compare.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")
    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert cleanup.status_code == 200, cleanup.text
    snapshot = _latest_snapshot(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    compare = client.post(
        "/companion/cache-maintenance/snapshots/compare",
        json={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert compare.status_code == 200, compare.text
    payload = compare.json()
    assert isinstance(payload["before_counts"], dict)
    assert isinstance(payload["after_counts"], dict)
    assert payload["after_counts"]["recycled_count"] == 1
    assert payload["affected_count"] == 1
