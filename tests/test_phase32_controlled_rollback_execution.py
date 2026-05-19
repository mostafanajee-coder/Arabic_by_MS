"""Tests for Phase 32 controlled rollback execution for cleanup snapshots."""

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


def _create_cleanup_snapshot(client: TestClient, filename: str, content: str = GOOD_SRT) -> dict:
    orphan = config.ENGLISH_CACHE_DIR / filename
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(content, encoding="utf-8")
    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert cleanup.status_code == 200, cleanup.text
    recycled = next(
        item
        for item in cleanup.json()["recycled_files"]
        if Path(str(item["original_path"])).name == filename
    )
    return recycled


def _latest_snapshot_id(client: TestClient, action: str) -> str:
    response = client.get("/companion/cache-maintenance/snapshots?limit=50")
    assert response.status_code == 200, response.text
    snapshot = next(item for item in response.json()["items"] if item["action"] == action)
    return str(snapshot["snapshot_id"])


def _execute(
    client: TestClient,
    *,
    snapshot_id: str,
    dry_run: bool = True,
    allow_rollback: bool = False,
    confirmation_text: str | None = None,
) -> dict:
    response = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": dry_run,
            "allow_rollback": allow_rollback,
            "confirmation_text": confirmation_text,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_dry_run_rollback_executes_nothing(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase32-dry-run.srt")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    payload = _execute(client, snapshot_id=snapshot_id, dry_run=True)
    assert payload["execution_status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["restored_items"] == []
    assert Path(str(recycle_item["recycled_path"])).exists()


def test_confirmed_rollback_restores_recycle_item_for_cleanup_snapshot(
    client: TestClient,
) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase32-execute-ok.srt")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    payload = _execute(
        client,
        snapshot_id=snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text=cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
    )
    assert payload["execution_status"] in ("executed", "partial")
    assert payload["restored_items"]
    restored = payload["restored_items"][0]
    assert restored["recycle_item_id"] == recycle_item["id"]
    assert not Path(str(recycle_item["recycled_path"])).exists()
    audit_actions = {
        str(item.get("action") or "")
        for item in client.get("/companion/cache-maintenance/audit?limit=50").json()["items"]
    }
    assert cache_maintenance.AUDIT_ACTION_ROLLBACK_EXECUTED in audit_actions or cache_maintenance.AUDIT_ACTION_ROLLBACK_PARTIAL in audit_actions


def test_occupied_original_path_restores_with_safe_suffix(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase32-occupied.srt")
    original_path = Path(str(recycle_item["original_path"]))
    original_path.write_text("occupied", encoding="utf-8")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    payload = _execute(
        client,
        snapshot_id=snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text=cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
    )
    restored = next(item for item in payload["restored_items"] if item["recycle_item_id"] == recycle_item["id"])
    assert restored["rollback_status"] == "restored_with_safe_suffix"
    assert ".restored-" in str(restored["restored_path"])


def test_tampered_recycle_item_is_blocked(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase32-tampered.srt")
    Path(str(recycle_item["recycled_path"])).write_text("tampered\n", encoding="utf-8")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    payload = _execute(
        client,
        snapshot_id=snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text=cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
    )
    assert any(item.get("recycle_item_id") == recycle_item["id"] for item in payload["tampered_items"])


def test_missing_recycle_item_is_skipped_safely(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase32-missing.srt")
    Path(str(recycle_item["recycled_path"])).unlink()
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    payload = _execute(
        client,
        snapshot_id=snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text=cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
    )
    assert any(item.get("recycle_item_id") == recycle_item["id"] for item in payload["missing_items"])


def test_restore_snapshot_rollback_execution_is_blocked(client: TestClient) -> None:
    recycle_item = _create_cleanup_snapshot(client, "phase32-restore-blocked.srt")
    restored = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert restored.status_code == 200, restored.text
    restore_snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_RESTORE)

    payload = _execute(
        client,
        snapshot_id=restore_snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text=cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
    )
    assert payload["execution_status"] == "blocked"
    assert payload["policy_level"] == cache_maintenance.POLICY_BLOCKED


def test_empty_recycle_snapshot_rollback_execution_is_blocked(client: TestClient) -> None:
    _create_cleanup_snapshot(client, "phase32-empty-blocked.srt")
    emptied = client.post(
        "/companion/cache-recycle-bin/empty",
        json={
            "allow_empty": True,
            "confirmation_text": cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT,
        },
    )
    assert emptied.status_code == 200, emptied.text
    empty_snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_EMPTY)

    payload = _execute(
        client,
        snapshot_id=empty_snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text=cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
    )
    assert payload["execution_status"] == "blocked"
    assert payload["policy_level"] == cache_maintenance.POLICY_BLOCKED


def test_confirmation_text_is_required(client: TestClient) -> None:
    _create_cleanup_snapshot(client, "phase32-confirm-required.srt")
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    denied = _execute(
        client,
        snapshot_id=snapshot_id,
        dry_run=False,
        allow_rollback=True,
        confirmation_text="wrong",
    )
    assert denied["execution_status"] == "not_executable"
    assert denied["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert denied["requires_confirmation"] is True


def test_rollback_execute_endpoint_does_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    _create_cleanup_snapshot(
        client,
        "phase32-secret-check.srt",
        content="Never expose this subtitle text.\n" + GOOD_SRT,
    )
    snapshot_id = _latest_snapshot_id(client, cache_maintenance.SNAPSHOT_ACTION_CLEANUP)

    payload = _execute(client, snapshot_id=snapshot_id, dry_run=True)
    serialized = json.dumps(payload)
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized
