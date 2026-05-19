"""Phase 34 torture workflow tests for maintenance and rollback safety."""

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


def _latest_cleanup_snapshot_id(client: TestClient) -> str:
    snapshots = client.get("/companion/cache-maintenance/snapshots?limit=50")
    assert snapshots.status_code == 200, snapshots.text
    for item in snapshots.json().get("items") or []:
        if item.get("action") == cache_maintenance.SNAPSHOT_ACTION_CLEANUP:
            return str(item["snapshot_id"])
    raise AssertionError("No cleanup snapshot was found.")


def test_torture_workflow_maintains_safety_guards(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-opensubtitles-key")

    # Keep sample asset checks isolated to the test cache path.
    sample_path = config.ARABIC_CACHE_DIR / config.SAMPLE_SRT_NAME
    monkeypatch.setattr(config, "SAMPLE_SRT_PATH", sample_path)
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text("1\n00:00:01,000 --> 00:00:03,000\nArabic by M.S\n", encoding="utf-8")
    gitkeep = config.ARABIC_CACHE_DIR / ".gitkeep"
    gitkeep.parent.mkdir(parents=True, exist_ok=True)
    gitkeep.write_text("", encoding="utf-8")

    orphan = config.ENGLISH_CACHE_DIR / "phase34-torture-orphan.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("Never expose this subtitle text.\n" + GOOD_SRT, encoding="utf-8")

    responses: dict[str, dict] = {}

    scan = client.post("/companion/cache-maintenance/scan")
    assert scan.status_code == 200, scan.text
    responses["scan"] = scan.json()
    operator_after_scan = client.get("/companion/cache-maintenance/operator-summary")
    assert operator_after_scan.status_code == 200, operator_after_scan.text
    responses["operator_after_scan"] = operator_after_scan.json()

    dry_run = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": True, "allow_delete": False},
    )
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["status"] == "planned"
    responses["dry_run"] = dry_run.json()
    operator_after_dry = client.get("/companion/cache-maintenance/operator-summary")
    assert operator_after_dry.status_code == 200, operator_after_dry.text
    responses["operator_after_dry"] = operator_after_dry.json()

    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert cleanup.status_code == 200, cleanup.text
    cleanup_payload = cleanup.json()
    assert cleanup_payload["recycled_count"] == 1
    recycled_item = cleanup_payload["recycled_files"][0]
    recycled_path = Path(str(recycled_item["recycled_path"]))
    assert not orphan.exists()
    assert recycled_path.exists()
    responses["cleanup"] = cleanup_payload

    operator_after_cleanup = client.get("/companion/cache-maintenance/operator-summary")
    assert operator_after_cleanup.status_code == 200, operator_after_cleanup.text
    responses["operator_after_cleanup"] = operator_after_cleanup.json()
    pending_actions = [str(item.get("action") or "") for item in operator_after_cleanup.json().get("pending_risky_actions") or []]
    assert "restore_recycle_item" in pending_actions

    recycle_integrity = client.post("/companion/cache-recycle-bin/integrity-scan")
    assert recycle_integrity.status_code == 200, recycle_integrity.text
    assert recycle_integrity.json()["count"] >= 1
    responses["recycle_integrity"] = recycle_integrity.json()
    operator_after_recycle_scan = client.get("/companion/cache-maintenance/operator-summary")
    assert operator_after_recycle_scan.status_code == 200, operator_after_recycle_scan.text
    responses["operator_after_recycle_scan"] = operator_after_recycle_scan.json()

    snapshot_id = _latest_cleanup_snapshot_id(client)
    rollback_plan = client.post(
        "/companion/cache-maintenance/rollback-plan",
        json={"snapshot_id": snapshot_id},
    )
    assert rollback_plan.status_code == 200, rollback_plan.text
    plan_payload = rollback_plan.json()
    assert plan_payload["snapshot_id"] == snapshot_id
    responses["rollback_plan"] = plan_payload

    rollback_dry_run = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": True,
            "allow_rollback": False,
        },
    )
    assert rollback_dry_run.status_code == 200, rollback_dry_run.text
    assert rollback_dry_run.json()["execution_status"] == "dry_run"
    responses["rollback_dry_run"] = rollback_dry_run.json()

    wrong_confirm = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": False,
            "allow_rollback": True,
            "confirmation_text": "execute rollback",
        },
    )
    assert wrong_confirm.status_code == 200, wrong_confirm.text
    wrong_payload = wrong_confirm.json()
    assert wrong_payload["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert wrong_payload["required_confirmation_text"] == cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT
    responses["wrong_confirm"] = wrong_payload

    rollback_execute = client.post(
        "/companion/cache-maintenance/rollback-execute",
        json={
            "snapshot_id": snapshot_id,
            "dry_run": False,
            "allow_rollback": True,
            "confirmation_text": cache_maintenance.ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
        },
    )
    assert rollback_execute.status_code == 200, rollback_execute.text
    execute_payload = rollback_execute.json()
    assert execute_payload["execution_status"] in ("executed", "partial")
    responses["rollback_execute"] = execute_payload

    operator_after_execute = client.get("/companion/cache-maintenance/operator-summary")
    assert operator_after_execute.status_code == 200, operator_after_execute.text
    responses["operator_after_execute"] = operator_after_execute.json()

    # Protected files remain intact throughout.
    assert config.SAMPLE_SRT_PATH.exists()
    assert gitkeep.exists()

    # No permanent delete path outside explicit recycle-empty action (not run here).
    assert orphan.exists()
    assert not recycled_path.exists()

    # Final cache integrity endpoint remains stable.
    final_integrity = client.get("/companion/cache-integrity")
    assert final_integrity.status_code == 200, final_integrity.text
    counts = final_integrity.json()["counts"]
    assert all(int(value) >= 0 for value in counts.values())
    responses["final_integrity"] = final_integrity.json()

    serialized = json.dumps(responses)
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-opensubtitles-key" not in serialized
