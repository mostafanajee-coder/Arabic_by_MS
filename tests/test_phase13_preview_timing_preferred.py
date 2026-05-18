"""Tests for Phase 13 preview, timing adjustment, and preferred records."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db
from utils.srt_timing import SRTTimingError, shift_srt_content


VALID_SRT_TEXT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
)
VALID_SRT_BYTES = VALID_SRT_TEXT.encode("utf-8")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _upload_record(client: TestClient, video_id: str, *, video_type: str = "movie") -> dict:
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
    video_type: str = "movie",
    arabic_text: str = "ترجمة جاهزة",
) -> dict:
    uploaded = _upload_record(client, video_id, video_type=video_type)
    arabic_file = tmp_path / ("{0}.ar.srt".format(uploaded["id"]))
    arabic_file.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n{0}\n".format(arabic_text),
        encoding="utf-8",
    )
    cache_db.set_arabic_srt(config.DB_PATH, int(uploaded["id"]), str(arabic_file), status="translated")
    return uploaded


def test_srt_timing_shifts_positive_offset_correctly() -> None:
    shifted = shift_srt_content(VALID_SRT_TEXT, 500)
    assert "00:00:01,500 --> 00:00:04,500" in shifted
    assert "00:00:05,500 --> 00:00:08,500" in shifted


def test_srt_timing_shifts_negative_offset_correctly_when_safe() -> None:
    shifted = shift_srt_content(VALID_SRT_TEXT, -500)
    assert "00:00:00,500 --> 00:00:03,500" in shifted
    assert "00:00:04,500 --> 00:00:07,500" in shifted


def test_srt_timing_rejects_negative_resulting_timestamps() -> None:
    with pytest.raises(SRTTimingError):
        shift_srt_content(VALID_SRT_TEXT, -1500)


def test_preview_english_endpoint(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300001")
    response = client.get(f"/companion/preview/{uploaded['id']}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["record_id"] == uploaded["id"]
    assert payload["lang"] == "english"
    assert payload["available"] is True
    assert payload["preview_blocks"][0]["text"] == "Hello world"


def test_preview_arabic_endpoint(client: TestClient, tmp_path: Path) -> None:
    uploaded = _translated_record(client, tmp_path, "tt1300002")
    response = client.get(f"/companion/preview/{uploaded['id']}?lang=arabic")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert "ترجمة جاهزة" in payload["preview_blocks"][0]["text"]


def test_preview_missing_arabic_returns_available_false(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300003")
    response = client.get(f"/companion/preview/{uploaded['id']}?lang=arabic")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["preview_blocks"] == []


def test_adjust_arabic_timing_creates_adjusted_arabic_file(client: TestClient, tmp_path: Path) -> None:
    uploaded = _translated_record(client, tmp_path, "tt1300004")
    original = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert original is not None
    old_arabic_path = Path(str(original["arabic_srt_path"]))

    response = client.post(
        f"/companion/adjust-timing/{uploaded['id']}",
        json={"offset_ms": 500, "target": "arabic"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    updated = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert updated is not None
    new_arabic_path = Path(str(updated["arabic_srt_path"]))
    assert new_arabic_path.exists()
    assert new_arabic_path != old_arabic_path
    assert "00:00:01,500 --> 00:00:04,500" in new_arabic_path.read_text(encoding="utf-8")
    assert updated["timing_offset_ms"] == 500
    assert payload["adjusted_targets"] == ["arabic"]


def test_adjust_english_timing_creates_adjusted_english_file(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300005")
    original = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert original is not None
    old_english_path = Path(str(original["english_srt_path"]))

    response = client.post(
        f"/companion/adjust-timing/{uploaded['id']}",
        json={"offset_ms": 250, "target": "english"},
    )
    assert response.status_code == 200, response.text
    updated = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert updated is not None
    new_english_path = Path(str(updated["english_srt_path"]))
    assert new_english_path.exists()
    assert new_english_path != old_english_path
    assert "00:00:01,250 --> 00:00:04,250" in new_english_path.read_text(encoding="utf-8")
    assert updated["status"] == "uploaded"


def test_adjust_both_works_when_both_files_exist(client: TestClient, tmp_path: Path) -> None:
    uploaded = _translated_record(client, tmp_path, "tt1300006")
    response = client.post(
        f"/companion/adjust-timing/{uploaded['id']}",
        json={"offset_ms": 1000, "target": "both"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["adjusted_targets"] == ["english", "arabic"]
    updated = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert updated is not None
    assert "00:00:02,000 --> 00:00:05,000" in Path(str(updated["english_srt_path"])).read_text(encoding="utf-8")
    assert "00:00:02,000 --> 00:00:05,000" in Path(str(updated["arabic_srt_path"])).read_text(encoding="utf-8")


def test_force_false_does_not_destructively_overwrite_original_file(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300007")
    original = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert original is not None
    old_path = Path(str(original["english_srt_path"]))
    old_text = old_path.read_text(encoding="utf-8")

    response = client.post(
        f"/companion/adjust-timing/{uploaded['id']}",
        json={"offset_ms": 300, "target": "english", "force": False},
    )
    assert response.status_code == 200, response.text
    updated = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert updated is not None
    assert Path(str(updated["english_srt_path"])) != old_path
    assert old_path.read_text(encoding="utf-8") == old_text


def test_force_true_overwrites_update_target_safely(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300008")
    original = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert original is not None
    old_path = Path(str(original["english_srt_path"]))

    response = client.post(
        f"/companion/adjust-timing/{uploaded['id']}",
        json={"offset_ms": 400, "target": "english", "force": True},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    updated = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert updated is not None
    assert Path(str(updated["english_srt_path"])) == old_path
    assert "00:00:01,400 --> 00:00:04,400" in old_path.read_text(encoding="utf-8")
    assert payload["backup_paths"]["english"].endswith(".srt")
    assert Path(str(payload["backup_paths"]["english"])).exists()


def test_set_preferred_only_allows_translated_arabic_records(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300009:1:5", video_type="series")
    response = client.post(f"/companion/set-preferred/{uploaded['id']}")
    assert response.status_code == 400
    assert "translated records" in response.json()["detail"]


def test_set_preferred_clears_other_preferred_records_for_same_canonical_key(
    client: TestClient,
    tmp_path: Path,
) -> None:
    first = _translated_record(client, tmp_path, "tt1300010:1:5", video_type="series", arabic_text="نسخة أولى")
    second = _translated_record(client, tmp_path, "tt1300010:1:5", video_type="series", arabic_text="نسخة ثانية")

    first_resp = client.post(f"/companion/set-preferred/{first['id']}")
    assert first_resp.status_code == 200
    second_resp = client.post(f"/companion/set-preferred/{second['id']}")
    assert second_resp.status_code == 200

    first_record = cache_db.get_record(config.DB_PATH, int(first["id"]))
    second_record = cache_db.get_record(config.DB_PATH, int(second["id"]))
    assert first_record is not None and first_record["is_preferred"] == 0
    assert second_record is not None and second_record["is_preferred"] == 1


def test_subtitles_prefers_preferred_translated_record(client: TestClient, tmp_path: Path) -> None:
    first = _translated_record(client, tmp_path, "tt1300011:1:5", video_type="series", arabic_text="النسخة المفضلة")
    second = _translated_record(client, tmp_path, "tt1300011:1:5", video_type="series", arabic_text="أحدث غير مفضلة")
    preferred = client.post(f"/companion/set-preferred/{first['id']}")
    assert preferred.status_code == 200

    response = client.get("/subtitles/series/tt1300011:1:5.json")
    assert response.status_code == 200
    subtitle = response.json()["subtitles"][0]
    download = client.get(subtitle["url"].replace("http://testserver", ""))
    assert "النسخة المفضلة" in download.text


def test_subtitles_still_falls_back_to_latest_translated_when_no_preferred_exists(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _translated_record(client, tmp_path, "tt1300012:1:5", video_type="series", arabic_text="نسخة قديمة")
    _translated_record(client, tmp_path, "tt1300012:1:5", video_type="series", arabic_text="نسخة أحدث")

    response = client.get("/subtitles/series/tt1300012:1:5.json")
    assert response.status_code == 200
    subtitle = response.json()["subtitles"][0]
    download = client.get(subtitle["url"].replace("http://testserver", ""))
    assert "نسخة أحدث" in download.text


def test_update_note_saves_user_note(client: TestClient) -> None:
    uploaded = _upload_record(client, "tt1300013")
    response = client.post(
        f"/companion/update-note/{uploaded['id']}",
        json={"user_note": "SubsPlease WEB-DL"},
    )
    assert response.status_code == 200, response.text
    record = cache_db.get_record(config.DB_PATH, int(uploaded["id"]))
    assert record is not None
    assert record["user_note"] == "SubsPlease WEB-DL"


def test_diagnostics_include_timing_and_preferred_flags(client: TestClient) -> None:
    response = client.get("/companion/diagnostics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["srt_timing_ready"] is True
    assert payload["preferred_record_ready"] is True
