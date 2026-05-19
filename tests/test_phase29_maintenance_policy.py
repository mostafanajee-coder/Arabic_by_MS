"""Tests for Phase 29 maintenance safety policy and approval gates."""

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

ARABIC_SRT = "1\n00:00:01,000 --> 00:00:03,000\nمرحبا\n"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _recycle_orphan(filename: str, content: str = GOOD_SRT) -> dict:
    orphan = config.ENGLISH_CACHE_DIR / filename
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(content, encoding="utf-8")
    cleanup = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )
    return next(
        item
        for item in cleanup["recycled_files"]
        if Path(str(item["original_path"])).name == filename
    )


def test_dry_run_policy_is_safe_readonly(client: TestClient) -> None:
    response = client.post("/companion/cache-maintenance/cleanup", json={"dry_run": True})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["policy_level"] == cache_maintenance.POLICY_SAFE_READONLY
    assert payload["requires_confirmation"] is False

    policy = client.get("/companion/cache-maintenance/policy")
    assert policy.status_code == 200, policy.text
    assert policy.json()["cleanup_dry_run"]["policy_level"] == cache_maintenance.POLICY_SAFE_READONLY


def test_cleanup_to_recycle_is_safe_recycle_for_orphan_files(client: TestClient) -> None:
    orphan = config.ENGLISH_CACHE_DIR / "phase29-safe-recycle.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    response = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["policy_level"] == cache_maintenance.POLICY_SAFE_RECYCLE
    assert payload["recycled_count"] == 1
    assert payload["status"] == "recycled"


def test_protected_files_are_blocked_from_actual_cleanup(client: TestClient, monkeypatch) -> None:
    sample_path = config.ARABIC_CACHE_DIR / config.SAMPLE_SRT_NAME
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(ARABIC_SRT, encoding="utf-8")
    monkeypatch.setattr(config, "SAMPLE_SRT_PATH", sample_path)

    response = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert "Protected cache files" in str(payload["blocked_reason"])
    assert payload["recycled_count"] == 0


def test_restore_with_occupied_original_path_is_risky_restore_and_uses_safe_suffix(
    client: TestClient,
) -> None:
    recycle_item = _recycle_orphan("phase29-occupied-restore.srt")
    original_path = Path(str(recycle_item["original_path"]))
    original_path.write_text("replacement file", encoding="utf-8")

    response = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "restored"
    assert payload["policy_level"] == cache_maintenance.POLICY_RISKY_RESTORE
    assert any(".restored-" in warning or "safe .restored- suffix" in warning for warning in payload["safety_warnings"])
    restored_path = Path(payload["item"]["restored_path"])
    assert restored_path.exists()
    assert restored_path != original_path.resolve()
    assert ".restored-" in restored_path.name


def test_checksum_mismatch_restore_is_blocked(client: TestClient) -> None:
    recycle_item = _recycle_orphan("phase29-tampered-restore.srt")
    Path(str(recycle_item["recycled_path"])).write_text("tampered\n", encoding="utf-8")

    response = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert payload["status"] == cache_maintenance.RECYCLE_INTEGRITY_TAMPERED
    assert "checksum changed" in str(payload["blocked_reason"])


def test_empty_recycle_bin_requires_allow_flag_and_exact_confirmation_text(
    client: TestClient,
) -> None:
    _recycle_orphan("phase29-empty-confirm.srt")

    missing_all = client.post("/companion/cache-recycle-bin/empty", json={})
    assert missing_all.status_code == 200, missing_all.text
    assert missing_all.json()["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert missing_all.json()["requires_confirmation"] is True

    wrong_text = client.post(
        "/companion/cache-recycle-bin/empty",
        json={"allow_empty": True, "confirmation_text": "empty recycle bin"},
    )
    assert wrong_text.status_code == 200, wrong_text.text
    assert wrong_text.json()["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert wrong_text.json()["required_confirmation_text"] == cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT

    confirmed = client.post(
        "/companion/cache-recycle-bin/empty",
        json={
            "allow_empty": True,
            "confirmation_text": cache_maintenance.EMPTY_RECYCLE_CONFIRMATION_TEXT,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["policy_level"] == cache_maintenance.POLICY_RISKY_EMPTY
    assert payload["emptied_count"] == 1


def test_policy_endpoints_do_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-os-key")
    recycle_item = _recycle_orphan(
        "phase29-secret-policy.srt",
        content="Never expose this subtitle text.\n" + GOOD_SRT,
    )
    Path(str(recycle_item["recycled_path"])).write_text("tampered\n", encoding="utf-8")

    policy = client.get("/companion/cache-maintenance/policy")
    blocked_restore = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    denied_empty = client.post("/companion/cache-recycle-bin/empty", json={"allow_empty": True})

    serialized = json.dumps(
        {
            "policy": policy.json(),
            "restore": blocked_restore.json(),
            "empty": denied_empty.json(),
        }
    )
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-os-key" not in serialized
