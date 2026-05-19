"""Tests for Phase 27 recycle-bin cleanup and safe restore behavior."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, cache_maintenance


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


def _recycle_root() -> Path:
    return config.ENGLISH_CACHE_DIR.parent / cache_maintenance.RECYCLE_DIR_NAME


def test_actual_cleanup_moves_orphan_to_recycle_bin() -> None:
    orphan = config.ENGLISH_CACHE_DIR / "move-me.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    result = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )

    assert result["recycled_count"] == 1
    assert result["deleted_count"] == 1
    recycled = result["recycled_files"][0]
    recycled_path = Path(str(recycled["recycled_path"]))
    assert not orphan.exists()
    assert recycled_path.exists()
    assert _recycle_root().exists()
    assert str(recycled["original_path"]) == str(orphan.resolve())
    assert recycled["checksum_sha256"]


def test_restore_returns_file_to_safe_cache_path() -> None:
    orphan = config.ENGLISH_CACHE_DIR / "restore-me.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    cleanup = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )
    recycle_item = cleanup["recycled_files"][0]

    restored = cache_maintenance.restore_recycled_file(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        recycle_item_id=str(recycle_item["id"]),
    )

    restored_path = Path(str(restored["item"]["restored_path"]))
    assert restored["status"] == "restored"
    assert restored_path == orphan.resolve()
    assert restored_path.exists()
    assert cache_maintenance.get_recycle_bin_summary(config.DB_PATH)["count"] == 0


def test_occupied_original_path_restores_with_safe_suffix() -> None:
    orphan = config.ENGLISH_CACHE_DIR / "occupied-restore.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    cleanup = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )
    recycle_item = cleanup["recycled_files"][0]

    orphan.write_text("replacement file", encoding="utf-8")
    restored = cache_maintenance.restore_recycled_file(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        recycle_item_id=str(recycle_item["id"]),
    )

    restored_path = Path(str(restored["item"]["restored_path"]))
    assert orphan.read_text(encoding="utf-8") == "replacement file"
    assert restored_path.exists()
    assert restored_path != orphan.resolve()
    assert ".restored-" in restored_path.name
    assert restored_path.read_text(encoding="utf-8") == GOOD_SRT


def test_protected_files_are_never_recycled(monkeypatch) -> None:
    sample_path = config.ARABIC_CACHE_DIR / config.SAMPLE_SRT_NAME
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(ARABIC_SRT, encoding="utf-8")
    monkeypatch.setattr(config, "SAMPLE_SRT_PATH", sample_path)

    result = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )

    assert sample_path.exists()
    assert result["recycled_count"] == 0
    assert cache_maintenance.get_recycle_bin_summary(config.DB_PATH)["count"] == 0


def test_restore_blocks_path_traversal_records(tmp_path: Path) -> None:
    recycle_root = _recycle_root()
    recycle_root.mkdir(parents=True, exist_ok=True)
    recycled_file = recycle_root / "bad-path.srt"
    recycled_file.write_text(GOOD_SRT, encoding="utf-8")
    outside = tmp_path / "outside" / "evil.srt"
    outside.parent.mkdir(parents=True, exist_ok=True)

    cache_maintenance.init_db(config.DB_PATH)
    with sqlite3.connect(str(config.DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO cache_recycle_bin (
                id, original_path, recycled_path, size_bytes, reason,
                recycled_at, checksum_sha256, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bad-restore",
                str(outside),
                str(recycled_file),
                recycled_file.stat().st_size,
                "bad path",
                "2026-01-01T00:00:00Z",
                "checksum-bad",
                cache_maintenance.RECYCLE_STATUS_ACTIVE,
            ),
        )
        conn.commit()

    blocked = cache_maintenance.restore_recycled_file(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        recycle_item_id="bad-restore",
    )
    assert blocked["policy_level"] == cache_maintenance.POLICY_BLOCKED
    assert blocked["status"] in ("blocked", cache_maintenance.RECYCLE_INTEGRITY_TAMPERED)
    assert "outside the configured cache directories" in str(blocked["blocked_reason"])


def test_empty_recycle_bin_requires_allow_empty_true(client: TestClient) -> None:
    orphan = config.ENGLISH_CACHE_DIR / "empty-me.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(GOOD_SRT, encoding="utf-8")

    cleanup = cache_maintenance.cleanup_cache(
        config.DB_PATH,
        english_cache_dir=config.ENGLISH_CACHE_DIR,
        arabic_cache_dir=config.ARABIC_CACHE_DIR,
        dry_run=False,
        allow_delete=True,
    )
    recycled_path = Path(str(cleanup["recycled_files"][0]["recycled_path"]))

    denied = client.post("/companion/cache-recycle-bin/empty", json={})
    assert denied.status_code == 200
    assert denied.json()["status"] == "empty_denied"
    assert recycled_path.exists()

    allowed = client.post(
        "/companion/cache-recycle-bin/empty",
        json={"allow_empty": True, "confirmation_text": "EMPTY RECYCLE BIN"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["emptied_count"] == 1
    assert not recycled_path.exists()
    assert cache_maintenance.get_recycle_bin_summary(config.DB_PATH)["count"] == 0


def test_recycle_bin_endpoints_do_not_expose_raw_srt_text_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    orphan = config.ENGLISH_CACHE_DIR / "endpoint-recycle.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("Never expose this subtitle text.\n" + GOOD_SRT, encoding="utf-8")

    cleanup = client.post(
        "/companion/cache-maintenance/cleanup",
        json={"dry_run": False, "allow_delete": True},
    )
    assert cleanup.status_code == 200, cleanup.text
    recycle_summary = client.get("/companion/cache-recycle-bin")
    assert recycle_summary.status_code == 200, recycle_summary.text
    recycle_item = recycle_summary.json()["items"][0]
    restore = client.post(
        "/companion/cache-recycle-bin/restore",
        json={"recycle_item_id": recycle_item["id"]},
    )
    assert restore.status_code == 200, restore.text
    empty = client.post(
        "/companion/cache-recycle-bin/empty",
        json={"allow_empty": True, "confirmation_text": "EMPTY RECYCLE BIN"},
    )
    assert empty.status_code == 200, empty.text

    serialized = json.dumps(
        {
            "cleanup": cleanup.json(),
            "summary": recycle_summary.json(),
            "restore": restore.json(),
            "empty": empty.json(),
        }
    )
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
