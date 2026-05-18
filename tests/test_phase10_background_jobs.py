"""Tests for Phase 10 background translation jobs and polling."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service, provider_router


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


def _build_long_srt(num_entries: int) -> bytes:
    blocks = []
    for index in range(1, num_entries + 1):
        blocks.append(
            "{0}\n00:00:{1:02d},000 --> 00:00:{2:02d},000\nLine {0}\n".format(
                index,
                index,
                index + 1,
            )
        )
    return ("\n".join(blocks)).encode("utf-8")


def _fake_translation_reply(prompt: str) -> str:
    out_lines = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        idx, _, body = stripped.partition(")")
        if body:
            out_lines.append("{0}) ترجمة: {1}".format(idx.strip(), body.strip()))
    return "\n".join(out_lines)


def _wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        resp = client.get(f"/companion/job-status/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish in time: {last}")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _upload_record(client: TestClient, video_id: str, content: bytes) -> dict:
    resp = client.post(
        "/companion/upload-srt",
        data={"video_id": video_id, "video_type": "movie"},
        files={"srt_file": ("e.srt", content, "application/x-subrip")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_translate_background_missing_gemini_api_key(client: TestClient) -> None:
    record = _upload_record(client, "ttbg0001", VALID_SRT_BYTES)
    resp = client.post(f"/companion/translate-background/{record['id']}")
    assert resp.status_code == 400
    assert "GEMINI_API_KEY" in resp.json()["detail"]


def test_translate_background_creates_job_with_mocked_gemini(
    client: TestClient,
    monkeypatch,
) -> None:
    record = _upload_record(client, "ttbg0002", VALID_SRT_BYTES)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    resp = client.post(f"/companion/translate-background/{record['id']}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["job_id"]
    assert payload["record_id"] == record["id"]
    assert payload["status"] in ("queued", "running")


def test_job_status_returns_progress(client: TestClient, monkeypatch) -> None:
    record = _upload_record(client, "ttbg0003", _build_long_srt(25))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    entered = threading.Event()
    release = threading.Event()

    def blocking_reply(prompt: str) -> str:
        entered.set()
        release.wait(timeout=2.0)
        return _fake_translation_reply(prompt)

    monkeypatch.setattr(gemini_service, "generate", blocking_reply)

    start = client.post(f"/companion/translate-background/{record['id']}")
    job_id = start.json()["job_id"]
    assert job_id
    assert entered.wait(timeout=2.0)

    status = client.get(f"/companion/job-status/{job_id}")
    assert status.status_code == 200, status.text
    payload = status.json()
    assert payload["status"] == "running"
    assert payload["progress_total_chunks"] == 2
    assert payload["progress_done_chunks"] in (0, 1)
    assert payload["progress_message"]

    release.set()
    final = _wait_for_job(client, job_id)
    assert final["status"] == "completed"


def test_completed_job_marks_record_translated(client: TestClient, monkeypatch) -> None:
    record = _upload_record(client, "ttbg0004", VALID_SRT_BYTES)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    start = client.post(f"/companion/translate-background/{record['id']}")
    job_id = start.json()["job_id"]
    final = _wait_for_job(client, job_id)
    assert final["status"] == "completed"
    assert final["arabic_available"] is True

    rec = cache_db.get_record(config.DB_PATH, record["id"])
    assert rec is not None
    assert rec["status"] == "translated"
    assert Path(rec["arabic_srt_path"]).exists()


def test_failed_job_marks_record_failed(client: TestClient, monkeypatch) -> None:
    record = _upload_record(client, "ttbg0005", VALID_SRT_BYTES)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "generate", lambda prompt: "bad output")

    start = client.post(f"/companion/translate-background/{record['id']}")
    job_id = start.json()["job_id"]
    final = _wait_for_job(client, job_id)
    assert final["status"] == "failed"
    assert final["error_message"]

    rec = cache_db.get_record(config.DB_PATH, record["id"])
    assert rec is not None
    assert rec["status"] == "failed"
    assert rec["error_message"]


def test_duplicate_job_prevention(client: TestClient, monkeypatch) -> None:
    record = _upload_record(client, "ttbg0006", _build_long_srt(25))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    entered = threading.Event()
    release = threading.Event()

    def blocking_reply(prompt: str) -> str:
        entered.set()
        release.wait(timeout=2.0)
        return _fake_translation_reply(prompt)

    monkeypatch.setattr(gemini_service, "generate", blocking_reply)

    first = client.post(f"/companion/translate-background/{record['id']}")
    second = client.post(f"/companion/translate-background/{record['id']}")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert entered.wait(timeout=2.0)
    release.set()
    _wait_for_job(client, first.json()["job_id"])


def test_force_false_does_not_overwrite_existing_arabic(
    client: TestClient,
    monkeypatch,
) -> None:
    record = _upload_record(client, "ttbg0007", VALID_SRT_BYTES)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    rec_path = Path(record["english_srt_path"]).with_name("existing.ar.srt")
    rec_path.write_text("original arabic", encoding="utf-8")
    cache_db.set_arabic_srt(config.DB_PATH, record["id"], str(rec_path), status="translated")

    def should_not_run(prompt: str) -> str:
        raise AssertionError("Gemini should not run when force=false and Arabic exists")

    monkeypatch.setattr(gemini_service, "generate", should_not_run)

    resp = client.post(f"/companion/translate-background/{record['id']}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["job_id"] is None
    assert payload["status"] == "already_translated"
    assert rec_path.read_text(encoding="utf-8") == "original arabic"


def test_force_true_allows_overwrite(client: TestClient, monkeypatch) -> None:
    record = _upload_record(client, "ttbg0008", VALID_SRT_BYTES)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    first = client.post(f"/companion/translate/{record['id']}")
    assert first.status_code == 200, first.text
    arabic_path = Path(first.json()["arabic_srt_path"])
    original_text = arabic_path.read_text(encoding="utf-8")

    def forced_reply(prompt: str) -> str:
        return _fake_translation_reply(prompt).replace("ترجمة:", "خلفية:")

    monkeypatch.setattr(gemini_service, "generate", forced_reply)

    start = client.post(f"/companion/translate-background/{record['id']}?force=true")
    job_id = start.json()["job_id"]
    final = _wait_for_job(client, job_id)
    assert final["status"] == "completed"
    updated_text = arabic_path.read_text(encoding="utf-8")
    assert updated_text != original_text
    assert "خلفية:" in updated_text


def test_import_best_with_background_translate_returns_job_id(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        provider_router,
        "search_all_subtitles",
        lambda **kwargs: {
            "items": [
                {
                    "provider": "subdl",
                    "subtitle_id": "bg-best",
                    "language": "EN",
                    "release_name": "Best.Movie.1080p",
                    "download_url": "https://subdl.local/best.zip",
                    "score": 99.0,
                }
            ],
            "provider_errors": {},
            "searched_providers": ["subdl"],
        },
    )
    monkeypatch.setattr(
        provider_router,
        "download_subtitle_data",
        lambda provider, url: _build_long_srt(25),
    )
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    resp = client.post(
        "/companion/import-best",
        json={
            "video_id": "ttbg0009",
            "auto_translate": True,
            "background_translate": True,
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["job_id"]
    assert payload["status"] in ("queued", "running")
    final = _wait_for_job(client, payload["job_id"])
    assert final["status"] == "completed"


def test_diagnostics_includes_active_translation_jobs(
    client: TestClient,
    monkeypatch,
) -> None:
    record = _upload_record(client, "ttbg0010", _build_long_srt(25))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    entered = threading.Event()
    release = threading.Event()

    def blocking_reply(prompt: str) -> str:
        entered.set()
        release.wait(timeout=2.0)
        return _fake_translation_reply(prompt)

    monkeypatch.setattr(gemini_service, "generate", blocking_reply)

    start = client.post(f"/companion/translate-background/{record['id']}")
    assert start.status_code == 200
    assert entered.wait(timeout=2.0)

    diag = client.get("/companion/diagnostics")
    assert diag.status_code == 200
    payload = diag.json()
    assert payload["job_manager_ready"] is True
    assert payload["active_translation_jobs"] >= 1

    release.set()
    _wait_for_job(client, start.json()["job_id"])
