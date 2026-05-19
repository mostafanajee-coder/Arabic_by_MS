"""Tests for Phase 31 snapshot rollback planning metadata."""

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


def _latest_snapshot_id(client: TestClient, action: str) -> str:
    response = client.get("/companion/cache-maintenance/snapshots?limit=25")
    assert response.status_code == 200, response.text
    snapshot = next(item for item in response.json()["items"] if item["action"] == action)
    return str(snapshot["snapshot_id"])


def _create_cleanup_snapshot(client: TestClient, filename: str, content: str = GOOD_SRT) -> dict:
    orphan = config.ENGLISH_CACHE_DIR / filename
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(content, encoding="utf-8")
    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert cleanup.status_code == 200, cleanup.text
    recycle_item = next(
        item
        for item in cleanup.json()["recycled_files"]
        if Path(str(item["original_path"])).name == filename
    )
    return recycle_item


def _rollback_plan(client: TestClient, snapshot_id: str) -> dict:
    response = client.post(
        "/companion/cache-maintenance/rollback-plan",
        json={"snapshot_id": snapshot_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_cleanup_snapshot_with_active_recycle_item_returns_restorable_candidates(
    client: TestClient,
) -> None:
    _create_cleanup_snapshot(client, "phase31-cleanup-restorable.srt")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    plan = _rollback_plan(client, snapshot_id)
    assert plan["rollback_supported"] is True
    assert plan["action"] == cache_maintenance.SNAPSHOT_ACTION_CLEANUP
    assert len(plan["candidate_items"]) == 1
    assert plan["candidate_items"][0]["rollback_status"] == "restorable"
    assert plan["rollback_level"] in (
        cache_maintenance.ROLLBACK_LEVEL_RESTORABLE,
        cache_maintenance.ROLLBACK_LEVEL_PARTIALLY_RESTORABLE,
    )


def test_tampered_recycle_item_returns_tampered_items(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase31-cleanup-tampered.srt")
    Path(str(recycle_item["recycled_path"])).write_text("tampered\n", encoding="utf-8")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    plan = _rollback_plan(client, snapshot_id)
    assert plan["tampered_items"]
    assert any(item["recycle_item_id"] == recycle_item["id"] for item in plan["tampered_items"])
    assert plan["rollback_level"] in (
        cache_maintenance.ROLLBACK_LEVEL_PARTIALLY_RESTORABLE,
        cache_maintenance.ROLLBACK_LEVEL_NOT_RESTORABLE,
    )


def test_missing_recycle_file_is_reported_safely(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase31-cleanup-missing.srt")
    Path(str(recycle_item["recycled_path"])).unlink()
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    plan = _rollback_plan(client, snapshot_id)
    assert plan["missing_items"]
    assert any(item["recycle_item_id"] == recycle_item["id"] for item in plan["missing_items"])


def test_occupied_original_path_is_restorable_with_safe_suffix(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase31-cleanup-occupied.srt")
    original_path = Path(str(recycle_item["original_path"]))
    original_path.write_text("occupied", encoding="utf-8")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    plan = _rollback_plan(client, snapshot_id)
    candidate = next(item for item in plan["candidate_items"] if item["recycle_item_id"] == recycle_item["id"])
    assert candidate["rollback_status"] == "restorable_with_safe_suffix"


def test_restore_snapshot_returns_metadata_only_rollback_explanation(
    client: TestClient,
) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase31-restore-meta.srt")
    restored = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert restored.status_code == 200, restored.text
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_RESTORE)

    plan = _rollback_plan(client, snapshot_id)
    assert plan["action"] == cache_maintenance.SNAPSHOT_ACTION_RESTORE
    assert plan["rollback_supported"] is True
    assert "metadata-only" in " ".join(plan["rollback_warnings"]).lower()
    assert plan["rollback_level"] in (
        cache_maintenance.ROLLBACK_LEVEL_PARTIALLY_RESTORABLE,
        cache_maintenance.ROLLBACK_LEVEL_NOT_RESTORABLE,
    )


def test_empty_recycle_bin_snapshot_returns_not_restorable(client: TestClient) -> None:
    _create_cleanup_snapshot(client, "phase31-empty-not-restorable.srt")
    emptied = client.post(
        "/companion/cache-recycle-bin/empty",
        json={
            "allow_empty": True,
            "confirmation_text": cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT,
        },
    )
    assert emptied.status_code == 200, emptied.text
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_EMPTY)

    plan = _rollback_plan(client, snapshot_id)
    assert plan["rollback_level"] == cache_maintenance.ROLLBACK_LEVEL_NOT_RESTORABLE
    assert plan["candidate_items"] == []
    assert plan["blocked_items"]


def test_rollback_plan_endpoint_does_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    _create_cleanup_snapshot(
        client,
        "phase31-secret-check.srt",
        content="Never expose this subtitle text.\n" + GOOD_SRT,
    )
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)
    plan = _rollback_plan(client, snapshot_id)

    serialized = json.dumps(plan)
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized
