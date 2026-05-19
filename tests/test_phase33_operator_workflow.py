"""Tests for Phase 33 operator maintenance workflow summary and readiness UX."""

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


def _create_orphan(filename: str, content: str = GOOD_SRT) -> Path:
    orphan = config.ENGLISH_CACHE_DIR / filename
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(content, encoding="utf-8")
    return orphan


def _recycle_orphan(client: TestClient, filename: str) -> dict:
    _create_orphan(filename)
    response = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return next(
        item
        for item in payload["recycled_files"]
        if Path(str(item["original_path"])).name == filename
    )


def _latest_cleanup_snapshot_id(client: TestClient) -> str:
    response = client.get("/companion/cache-maintenance/snapshots?limit=25")
    assert response.status_code == 200, response.text
    snapshot = next(
        item
        for item in response.json()["items"]
        if item["action"] == cache_maintenance.SNAPSHOT_ACTION_CLEANUP
    )
    return str(snapshot["snapshot_id"])


def test_operator_summary_returns_required_sections(client: TestClient) -> None:
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text
    payload = response.json()

    required = {
        "cache_integrity_counts",
        "maintenance_counts",
        "recycle_bin_counts",
        "latest_audit_items",
        "latest_snapshots",
        "pending_risky_actions",
        "safety_policy_summary",
        "recommended_next_action",
    }
    assert required.issubset(payload.keys())
    assert isinstance(payload["cache_integrity_counts"], dict)
    assert isinstance(payload["maintenance_counts"], dict)
    assert isinstance(payload["recycle_bin_counts"], dict)
    assert isinstance(payload["latest_audit_items"], list)
    assert isinstance(payload["latest_snapshots"], list)
    assert isinstance(payload["pending_risky_actions"], list)
    assert isinstance(payload["safety_policy_summary"], dict)
    assert isinstance(payload["recommended_next_action"], dict)


def test_recommended_next_action_clean_cache(client: TestClient) -> None:
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text
    assert response.json()["recommended_next_action"]["code"] == "clean_cache"


def test_recommended_next_action_orphan_files_available(client: TestClient) -> None:
    _create_orphan("phase33-orphan.srt")
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text
    assert response.json()["recommended_next_action"]["code"] == "orphan_files_available"


def test_recommended_next_action_recycle_bin_has_items(client: TestClient) -> None:
    recycled = _recycle_orphan(client, "phase33-recycle-items.srt")
    Path(str(recycled["recycled_path"])).unlink()
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text
    assert response.json()["recommended_next_action"]["code"] == "recycle_bin_has_items"


def test_recommended_next_action_rollback_available(client: TestClient) -> None:
    _recycle_orphan(client, "phase33-rollback-available.srt")
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["recommended_next_action"]["code"] == "rollback_available"


def test_recommended_next_action_risky_actions_blocked_without_confirmation(
    client: TestClient,
) -> None:
    _recycle_orphan(client, "phase33-risky-blocked.srt")
    denied = client.post("/companion/cache-recycle-bin/empty", json={})
    assert denied.status_code == 200, denied.text
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text
    assert response.json()["recommended_next_action"]["code"] == "risky_blocked_without_confirmation"


def test_action_readiness_metadata_is_returned(client: TestClient) -> None:
    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": True, "allow_delete": False},
    )
    assert cleanup.status_code == 200, cleanup.text
    for key in ("action_ready", "readiness_reason", "required_confirmation_text", "policy_level", "warning_count"):
        assert key in cleanup.json()

    recycled = _recycle_orphan(client, "phase33-readiness-restore.srt")
    restore = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycled["id"]},
    )
    assert restore.status_code == 200, restore.text
    for key in ("action_ready", "readiness_reason", "required_confirmation_text", "policy_level", "warning_count"):
        assert key in restore.json()

    denied_empty = client.post("/companion/cache-recycle-bin/empty", json={})
    assert denied_empty.status_code == 200, denied_empty.text
    empty_payload = denied_empty.json()
    assert empty_payload["action_ready"] is False
    assert empty_payload["required_confirmation_text"] == cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT

    _recycle_orphan(client, "phase33-readiness-rollback.srt")
    snapshot_id = _latest_cleanup_snapshot_id(client)
    rollback_plan = client.post(
        "/companion/cache-maintenance/rollback-plan",
        json={"snapshot_id": snapshot_id},
    )
    assert rollback_plan.status_code == 200, rollback_plan.text
    for key in ("action_ready", "readiness_reason", "required_confirmation_text", "policy_level", "warning_count"):
        assert key in rollback_plan.json()

    rollback_execute = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": True,
            "allow_rollback": False,
        },
    )
    assert rollback_execute.status_code == 200, rollback_execute.text
    for key in ("action_ready", "readiness_reason", "required_confirmation_text", "policy_level", "warning_count"):
        assert key in rollback_execute.json()


def test_operator_summary_does_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    _create_orphan(
        "phase33-secret-check.srt",
        content="Never expose this subtitle text.\n" + GOOD_SRT,
    )
    response = client.get("/companion/cache-maintenance/operator-summary")
    assert response.status_code == 200, response.text

    serialized = json.dumps(response.json())
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized


def test_phase29_confirmation_gates_remain_exact(client: TestClient) -> None:
    _recycle_orphan(client, "phase33-exact-empty.srt")
    wrong_empty = client.post(
        "/companion/cache-recycle-bin/empty",
        json={"allow_empty": True, "confirmation_text": "empty recycle bin"},
    )
    assert wrong_empty.status_code == 200, wrong_empty.text
    payload = wrong_empty.json()
    assert payload["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert payload["required_confirmation_text"] == cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT

    _recycle_orphan(client, "phase33-exact-rollback.srt")
    snapshot_id = _latest_cleanup_snapshot_id(client)
    wrong_rollback = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": False,
            "allow_rollback": True,
            "confirmation_text": "execute rollback",
        },
    )
    assert wrong_rollback.status_code == 200, wrong_rollback.text
    rollback_payload = wrong_rollback.json()
    assert rollback_payload["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert rollback_payload["required_confirmation_text"] == cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT


def test_phase32_rollback_execute_remains_gated(client: TestClient) -> None:
    _recycle_orphan(client, "phase33-phase32-gate.srt")
    snapshot_id = _latest_cleanup_snapshot_id(client)
    denied = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": False,
            "allow_rollback": False,
            "confirmation_text": cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
        },
    )
    assert denied.status_code == 200, denied.text
    payload = denied.json()
    assert payload["execution_status"] == "not_executable"
    assert payload["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert payload["requires_confirmation"] is True
