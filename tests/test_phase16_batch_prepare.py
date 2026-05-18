"""Tests for Phase 16 batch episode prepare queue."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import batch_prepare_service, cache_db, prepare_service, usage_guard


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _upload_record(client: TestClient, video_id: str, *, video_type: str = "series") -> dict:
    response = client.post(
        "/companion/upload-srt",
        data={"video_id": video_id, "video_type": video_type},
        files={"srt_file": ("english.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _translated_record(
    client: TestClient,
    tmp_path: Path,
    video_id: str,
    *,
    video_type: str = "series",
) -> dict:
    uploaded = _upload_record(client, video_id, video_type=video_type)
    arabic_file = tmp_path / "{0}.ar.srt".format(uploaded["id"])
    arabic_file.write_text("1\n00:00:01,000 --> 00:00:04,000\nجاهز\n", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, int(uploaded["id"]), str(arabic_file), status="translated")
    return uploaded


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


def test_batch_tables_creation_and_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "subtitles.db"
    batch_prepare_service.init_db(db_path)
    job_columns = set(batch_prepare_service.get_table_columns(db_path, "batch_prepare_jobs"))
    item_columns = set(batch_prepare_service.get_table_columns(db_path, "batch_prepare_items"))
    assert job_columns == {
        "id",
        "batch_id",
        "imdb_id",
        "video_type",
        "season",
        "episode_start",
        "episode_end",
        "query",
        "release_name",
        "status",
        "total_items",
        "done_items",
        "failed_items",
        "created_at",
        "updated_at",
        "error_message",
    }
    assert item_columns == {
        "id",
        "batch_id",
        "canonical_video_key",
        "video_id",
        "season",
        "episode",
        "status",
        "record_id",
        "job_id",
        "provider",
        "score",
        "quality_score",
        "quality_level",
        "quality_warnings",
        "reject_hint",
        "local_first_reused",
        "local_reuse_reason",
        "cache_integrity_status",
        "cache_integrity_warnings",
        "cache_integrity_checked_at",
        "error_message",
        "created_at",
        "updated_at",
    }

    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(str(legacy_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE batch_prepare_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                imdb_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE batch_prepare_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                canonical_video_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()

    batch_prepare_service.init_db(legacy_db)
    assert "status" in set(batch_prepare_service.get_table_columns(legacy_db, "batch_prepare_jobs"))
    assert "job_id" in set(batch_prepare_service.get_table_columns(legacy_db, "batch_prepare_items"))


def test_batch_prepare_rejects_invalid_range(client: TestClient) -> None:
    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600001", "season": 1, "episode_start": 5, "episode_end": 4},
    )
    assert response.status_code == 400
    assert "episode_end" in response.json()["detail"]


def test_batch_prepare_rejects_too_many_items(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_BATCH_PREPARE_ITEMS", "2")
    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600002", "season": 1, "episode_start": 1, "episode_end": 3},
    )
    assert response.status_code == 400
    assert "too large" in response.json()["detail"]


def test_batch_prepare_creates_batch_and_item_rows(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_service,
        "request_prepare",
        lambda **kwargs: {
            "status": "started",
            "record_id": kwargs["episode"] + 100,
            "job_id": "job-{0}".format(kwargs["episode"]),
            "provider": "subdl",
            "score": 90.0,
        },
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600003", "season": 1, "episode_start": 1, "episode_end": 3},
    )
    assert response.status_code == 200, response.text
    created = response.json()
    final = _wait_for_batch(client, created["batch_id"])
    assert final["total_items"] == 3
    assert len(final["items"]) == 3
    assert all(item["status"] == "completed" for item in final["items"])


def test_batch_prepare_marks_skipped_ready_when_arabic_exists(
    client: TestClient,
    tmp_path: Path,
) -> None:
    ready = _translated_record(client, tmp_path, "tt1600004:1:2")

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600004", "season": 1, "episode_start": 2, "episode_end": 2},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["items"][0]["status"] == "skipped_ready"
    assert payload["items"][0]["record_id"] == ready["id"]


def test_batch_processing_calls_prepare_workflow_with_mocks(client: TestClient, monkeypatch) -> None:
    calls = []

    def fake_request_prepare(**kwargs):
        calls.append((kwargs["season"], kwargs["episode"], kwargs["video_id"]))
        return {
            "status": "started",
            "record_id": kwargs["episode"] + 200,
            "job_id": "job-{0}".format(kwargs["episode"]),
            "provider": "subsource",
            "score": 77.0,
        }

    monkeypatch.setattr(prepare_service, "request_prepare", fake_request_prepare)

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600005", "season": 3, "episode_start": 7, "episode_end": 8},
    )
    assert response.status_code == 200, response.text
    final = _wait_for_batch(client, response.json()["batch_id"])
    assert final["status"] == "completed"
    assert calls == [
        (3, 7, "tt1600005:3:7"),
        (3, 8, "tt1600005:3:8"),
    ]


def test_one_failed_item_does_not_stop_whole_batch(client: TestClient, monkeypatch) -> None:
    def fake_request_prepare(**kwargs):
        episode = int(kwargs["episode"])
        if episode == 2:
            return {"status": "no_results", "message": "No subtitle results"}
        return {
            "status": "started",
            "record_id": episode + 300,
            "job_id": "job-{0}".format(episode),
            "provider": "subdl",
            "score": 80.0,
        }

    monkeypatch.setattr(prepare_service, "request_prepare", fake_request_prepare)

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600006", "season": 1, "episode_start": 1, "episode_end": 3},
    )
    final = _wait_for_batch(client, response.json()["batch_id"])
    assert final["status"] == "partial"
    assert [item["status"] for item in final["items"]] == ["completed", "failed", "completed"]


def test_batch_status_returns_item_states(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_service,
        "request_prepare",
        lambda **kwargs: {
            "status": "already_running",
            "record_id": 55,
            "job_id": "job-reused",
            "provider": "subdl",
            "score": 88.0,
        },
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600007", "season": 2, "episode_start": 4, "episode_end": 4},
    )
    assert response.status_code == 200, response.text
    final = _wait_for_batch(client, response.json()["batch_id"])
    status_response = client.get(f"/companion/batch-status/{final['batch_id']}")
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["items"][0]["canonical_video_key"] == "tt1600007:s02e04"
    assert payload["items"][0]["status"] == "completed"
    assert payload["items"][0]["job_id"] == "job-reused"


def test_cancel_batch_cancels_queued_items(client: TestClient, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_request_prepare(**kwargs):
        entered.set()
        release.wait(timeout=2.0)
        return {
            "status": "started",
            "record_id": 400 + int(kwargs["episode"]),
            "job_id": "job-{0}".format(kwargs["episode"]),
            "provider": "subdl",
            "score": 91.0,
        }

    monkeypatch.setattr(prepare_service, "request_prepare", blocking_request_prepare)

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600008", "season": 1, "episode_start": 1, "episode_end": 2},
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]
    assert entered.wait(timeout=2.0)

    cancel = client.post(f"/companion/cancel-batch/{batch_id}")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"

    release.set()
    final = _wait_for_batch(client, batch_id)
    assert final["status"] == "cancelled"
    assert final["items"][0]["status"] == "completed"
    assert final["items"][1]["status"] == "cancelled"


def test_batch_list_returns_latest_batches(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_service,
        "request_prepare",
        lambda **kwargs: {
            "status": "started",
            "record_id": kwargs["episode"] + 500,
            "job_id": "job-{0}".format(kwargs["episode"]),
            "provider": "subdl",
            "score": 90.0,
        },
    )

    first = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600009", "season": 1, "episode_start": 1, "episode_end": 1},
    ).json()
    second = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600010", "season": 1, "episode_start": 1, "episode_end": 1},
    ).json()
    _wait_for_batch(client, first["batch_id"])
    _wait_for_batch(client, second["batch_id"])

    listed = client.get("/companion/batch-list?limit=2")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 2
    assert items[0]["batch_id"] == second["batch_id"]
    assert items[1]["batch_id"] == first["batch_id"]


def test_duplicate_active_prepare_or_job_is_reused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_service,
        "request_prepare",
        lambda **kwargs: {
            "status": "already_running",
            "record_id": 611,
            "job_id": "job-duplicate",
            "provider": "subsource",
            "score": 79.0,
        },
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600011", "season": 1, "episode_start": 5, "episode_end": 5},
    )
    payload = _wait_for_batch(client, response.json()["batch_id"])
    assert payload["status"] == "completed"
    assert payload["items"][0]["job_id"] == "job-duplicate"
    assert payload["items"][0]["status"] == "completed"


def test_skipped_ready_does_not_consume_provider_or_gemini_quota(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _translated_record(client, tmp_path, "tt1600012:1:1")

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600012", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    usage = client.get("/companion/usage-status").json()
    assert usage["gemini_translations_used"] == 0
    assert usage["provider_searches_used"] == 0
    assert usage["prepare_requests_used"] == 0
    assert usage["batch_prepare_requests_used"] == 1


def test_usage_event_batch_prepare_request_is_recorded(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_service,
        "request_prepare",
        lambda **kwargs: {
            "status": "started",
            "record_id": 700,
            "job_id": "job-700",
            "provider": "subdl",
            "score": 75.0,
        },
    )

    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600013", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    assert response.status_code == 200, response.text
    events = client.get("/companion/usage-events?limit=5").json()["items"]
    assert any(event["event_type"] == usage_guard.EVENT_BATCH_PREPARE_REQUEST for event in events)
    usage = client.get("/companion/usage-status").json()
    assert usage["batch_prepare_requests_used"] == 1


def test_diagnostics_include_batch_prepare_flags(client: TestClient, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_request_prepare(**kwargs):
        entered.set()
        release.wait(timeout=2.0)
        return {
            "status": "started",
            "record_id": 801,
            "job_id": "job-801",
            "provider": "subdl",
            "score": 82.0,
        }

    monkeypatch.setattr(prepare_service, "request_prepare", blocking_request_prepare)
    response = client.post(
        "/companion/batch-prepare",
        json={"imdb_id": "tt1600014", "season": 1, "episode_start": 1, "episode_end": 1},
    )
    batch_id = response.json()["batch_id"]
    assert entered.wait(timeout=2.0)

    diagnostics = client.get("/companion/diagnostics")
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["batch_prepare_ready"] is True
    assert payload["active_batch_jobs"] == 1

    release.set()
    _wait_for_batch(client, batch_id)
