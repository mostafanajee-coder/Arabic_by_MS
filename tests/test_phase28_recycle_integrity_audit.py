"""Tests for Phase 28 recycle-bin integrity verification and audit trails."""

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
    recycled = next(
        item
        for item in result["recycled_files"]
        if Path(str(item["original_path"])).name == filename
    )
    return recycled


def _audit_actions(client: TestClient) -> list[str]:
    response = client.get("/companion/cache-maintenance/audit?limit=50")
    assert response.status_code == 200, response.text
    return [str(item.get("action") or "") for item in response.json()["items"]]


def test_restore_succeeds_when_recycle_checksum_matches(client: TestClient) -> None:
    recycle_item = _recycle_orphan("phase28-restore-ok.srt")

    response = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "restored"
    assert payload["integrity"]["status"] == "ok"
    assert payload["integrity"]["checksum_verified"] is True
    assert Path(payload["item"]["restored_path"]).exists()
    assert "restore" in _audit_actions(client)


def test_restore_is_blocked_when_recycled_file_checksum_changes(client: TestClient) -> None:
    recycle_item = _recycle_orphan("phase28-restore-tampered.srt")
    recycled_path = Path(str(recycle_item["recycled_path"]))
    recycled_path.write_text("Tampered file content.\n", encoding="utf-8")

    response = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == cache_maintenance.RECYCLE_INTEGRITY_TAMPERED
    assert payload["integrity"]["status"] == cache_maintenance.RECYCLE_INTEGRITY_TAMPERED
    assert payload["integrity"]["checksum_verified"] is False
    assert Path(str(recycle_item["original_path"])).exists() is False
    assert recycled_path.exists()
    assert "restore_blocked" in _audit_actions(client)


def test_missing_recycled_file_is_reported_safely(client: TestClient) -> None:
    recycle_item = _recycle_orphan("phase28-restore-missing.srt")
    recycled_path = Path(str(recycle_item["recycled_path"]))
    recycled_path.unlink()

    restore = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert restore.status_code == 200, restore.text
    restore_payload = restore.json()
    assert restore_payload["status"] == cache_maintenance.RECYCLE_INTEGRITY_MISSING
    assert restore_payload["integrity"]["status"] == cache_maintenance.RECYCLE_INTEGRITY_MISSING

    scan = client.post("/companion/cache-recycle-bin/integrity-scan")
    assert scan.status_code == 200, scan.text
    scan_payload = scan.json()
    assert scan_payload["counts"]["missing_recycle_file"] == 1
    assert scan_payload["status"] == "issues_found"


def test_audit_records_cleanup_restore_blocked_restore_and_empty_attempts(
    client: TestClient,
) -> None:
    scan = client.post("/companion/cache-maintenance/scan")
    assert scan.status_code == 200, scan.text
    dry_run = client.post("/companion/cache-maintenance/cleanup", json={"dry_run": True})
    assert dry_run.status_code == 200, dry_run.text

    recycle_ok = _recycle_orphan("phase28-audit-restore.srt")
    restored = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_ok["id"]},
    )
    assert restored.status_code == 200, restored.text

    recycle_blocked = _recycle_orphan("phase28-audit-blocked.srt")
    Path(str(recycle_blocked["recycled_path"])).write_text("changed\n", encoding="utf-8")
    blocked = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_blocked["id"]},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == cache_maintenance.RECYCLE_INTEGRITY_TAMPERED

    denied = client.post("/companion/cache-recycle-bin/empty", json={})
    assert denied.status_code == 200, denied.text
    assert denied.json()["status"] == "empty_denied"
    confirmed = client.post(
        "/companion/cache-recycle-bin/empty",
        json={"allow_empty": True, "confirmation_text": "EMPTY RECYCLE BIN"},
    )
    assert confirmed.status_code == 200, confirmed.text

    audit = client.get("/companion/cache-maintenance/audit?limit=20")
    assert audit.status_code == 200, audit.text
    items = audit.json()["items"]
    actions = {str(item.get("action") or "") for item in items}
    assert "scan" in actions
    assert "cleanup_dry_run" in actions
    assert "cleanup_recycle" in actions
    assert "restore" in actions
    assert "restore_blocked" in actions
    assert "empty_denied" in actions
    assert "empty_confirmed" in actions


def test_audit_endpoints_do_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    recycle_item = _recycle_orphan(
        "phase28-secret-check.srt",
        content="Never expose this subtitle text.\n" + GOOD_SRT,
    )
    Path(str(recycle_item["recycled_path"])).write_text("tampered secret-like body\n", encoding="utf-8")

    blocked = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert blocked.status_code == 200, blocked.text
    audit = client.get("/companion/cache-maintenance/audit?limit=20")
    assert audit.status_code == 200, audit.text
    integrity = client.post("/companion/cache-recycle-bin/integrity-scan")
    assert integrity.status_code == 200, integrity.text

    serialized = json.dumps(
        {
            "blocked": blocked.json(),
            "audit": audit.json(),
            "integrity": integrity.json(),
        }
    )
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized
