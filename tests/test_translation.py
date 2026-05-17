"""Phase 3 tests: /companion/translate/{record_id} + translation_service.

All tests use mocks; nothing reaches the real Gemini API.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app
from services import cache_db, gemini_service, translation_service
from services.translation_service import (
    EnglishFileMissingError,
    RecordNotFoundError,
    translate_record,
)
from utils.srt_cleaner import TranslationFormatError


# ---- helpers -------------------------------------------------------------


VALID_SRT_BYTES = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
).encode("utf-8")


def _fake_translation_reply(prompt: str) -> str:
    """Return a Gemini-shaped reply that translates each input line to a stub."""
    out_lines = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        # Lines look like "1) Hello world" -> emit "1) <ar>Hello world</ar>"
        idx, _, body = stripped.partition(")")
        if not body:
            continue
        out_lines.append(f"{idx.strip()}) ترجمة: {body.strip()}")
    return "\n".join(out_lines)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def uploaded_record(client: TestClient) -> dict:
    """Upload a small English SRT and return the record JSON."""
    resp = client.post(
        "/companion/upload-srt",
        data={"video_id": "tt4242424", "video_type": "movie"},
        files={"srt_file": ("e.srt", VALID_SRT_BYTES, "application/x-subrip")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- route: missing API key ---------------------------------------------


def test_translate_returns_400_when_gemini_api_key_missing(client, uploaded_record):
    # The autouse fixture already strips GEMINI_API_KEY, but be explicit.
    resp = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert resp.status_code == 400
    body = resp.json()
    assert "GEMINI_API_KEY" in body["detail"]
    rec = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert rec["status"] == "failed"
    assert "GEMINI_API_KEY" in rec["error_message"]


def test_gemini_status_reports_missing_key(client):
    resp = client.get("/companion/gemini-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["configured"] is False
    assert payload["model"] == gemini_service.DEFAULT_MODEL
    assert "GEMINI_API_KEY" in payload["message"]


def test_gemini_status_reports_configured_key(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-pro")

    resp = client.get("/companion/gemini-status")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["configured"] is True
    assert payload["model"] == "gemini-2.0-pro"
    assert "ready" in payload["message"].lower()


# ---- route: successful translation (mocked) -----------------------------


def test_translate_with_mocked_gemini_creates_arabic_file(
    client, uploaded_record, monkeypatch
):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    resp = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "translated"
    assert payload["arabic_srt_path"]

    arabic_path = Path(payload["arabic_srt_path"])
    assert arabic_path.exists()
    arabic_text = arabic_path.read_text(encoding="utf-8")
    # Same number of cues, same timestamps preserved.
    assert "00:00:01,000 --> 00:00:04,000" in arabic_text
    assert "00:00:05,000 --> 00:00:08,000" in arabic_text
    assert "ترجمة:" in arabic_text

    # DB row updated.
    rec = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert rec["status"] == "translated"
    assert rec["arabic_srt_path"] == str(arabic_path)


def test_subtitles_returns_cached_arabic_after_translation(
    client, uploaded_record, monkeypatch
):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    client.post(f"/companion/translate/{uploaded_record['id']}")

    sub_resp = client.get(f"/subtitles/movie/{uploaded_record['video_id']}.json")
    item = sub_resp.json()["subtitles"][0]
    assert item["id"].startswith("cached-")
    # Downloading that subtitle returns the cached Arabic file.
    dl = client.get(f"/download/{item['id']}.srt")
    assert dl.status_code == 200
    assert "ترجمة:" in dl.content.decode("utf-8")


# ---- route: bad Gemini output -------------------------------------------


def test_translate_rejects_malformed_gemini_output(client, uploaded_record, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", lambda prompt: "Sorry, I cannot do that.")

    resp = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert resp.status_code == 502
    assert "unusable" in resp.json()["detail"].lower() or "numbered" in resp.json()["detail"].lower()

    # DB must persist the failure.
    rec = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert rec["status"] == "failed"
    assert rec["arabic_srt_path"] is None
    assert rec["error_message"]

    listed = client.get("/companion/list").json()["items"][0]
    assert listed["status"] == "failed"
    assert listed["error_message"] == rec["error_message"]


def test_translate_rejects_partial_gemini_output(client, uploaded_record, monkeypatch):
    # Only entry 1 returned, entry 2 missing.
    def partial(prompt: str) -> str:
        return "1) فقط الأولى"

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", partial)

    resp = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert resp.status_code == 502
    rec = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert rec["status"] == "failed"
    assert rec["arabic_srt_path"] is None
    assert rec["error_message"]


# ---- route: record / file lookup failures -------------------------------


def test_translate_record_not_found(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    resp = client.post("/companion/translate/9999")
    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_translate_english_file_missing(client, uploaded_record, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    # Delete the file the upload created.
    Path(uploaded_record["english_srt_path"]).unlink()

    resp = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert resp.status_code == 404
    assert "missing" in resp.json()["detail"].lower()
    rec = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert rec["status"] == "failed"
    assert "missing" in rec["error_message"].lower()


def test_retry_clears_error_and_succeeds(client, uploaded_record, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", lambda prompt: "bad output")

    failed = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert failed.status_code == 502
    failed_record = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert failed_record["status"] == "failed"
    assert failed_record["error_message"]

    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    retried = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert retried.status_code == 200, retried.text
    payload = retried.json()
    assert payload["status"] == "translated"
    assert payload["error_message"] is None

    rec = cache_db.get_record(config.DB_PATH, uploaded_record["id"])
    assert rec["status"] == "translated"
    assert rec["error_message"] is None
    assert Path(rec["arabic_srt_path"]).exists()


def test_translated_record_is_not_overwritten_without_force(
    client, uploaded_record, monkeypatch
):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    first = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert first.status_code == 200, first.text
    path = Path(first.json()["arabic_srt_path"])
    original_text = path.read_text(encoding="utf-8")

    def should_not_run(prompt: str) -> str:
        raise AssertionError("Gemini should not run when force=false and Arabic file exists")

    monkeypatch.setattr(gemini_service, "generate", should_not_run)

    second = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "translated"
    assert path.read_text(encoding="utf-8") == original_text


def test_force_true_overwrites_existing_arabic_file(client, uploaded_record, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(gemini_service, "generate", _fake_translation_reply)

    first = client.post(f"/companion/translate/{uploaded_record['id']}")
    assert first.status_code == 200, first.text
    path = Path(first.json()["arabic_srt_path"])
    original_text = path.read_text(encoding="utf-8")

    def forced_translation(prompt: str) -> str:
        return _fake_translation_reply(prompt).replace("ترجمة:", "إعادة ترجمة:")

    monkeypatch.setattr(gemini_service, "generate", forced_translation)

    second = client.post(f"/companion/translate/{uploaded_record['id']}?force=true")
    assert second.status_code == 200, second.text
    updated_text = path.read_text(encoding="utf-8")
    assert updated_text != original_text
    assert "إعادة ترجمة:" in updated_text


# ---- service-layer unit tests -------------------------------------------


def test_translate_record_unit_with_injected_call(tmp_path):
    """Call translate_record directly with an injected gemini_call (no HTTP, no env)."""
    english = tmp_path / "english"
    english.mkdir()
    arabic_dir = tmp_path / "arabic"
    db = tmp_path / "subtitles.db"

    en_file = english / "x.srt"
    en_file.write_text(VALID_SRT_BYTES.decode("utf-8"), encoding="utf-8")

    rid = cache_db.insert_subtitle(
        db,
        video_id="tt0001",
        video_type="movie",
        release_name=None,
        english_srt_path=str(en_file),
        english_srt_hash="abc1234567890def",
    )
    result = translate_record(
        db, rid,
        arabic_cache_dir=arabic_dir,
        gemini_call=_fake_translation_reply,
    )
    assert result["status"] == "translated"
    assert Path(result["arabic_srt_path"]).exists()


def test_translate_record_unit_raises_on_missing_record(tmp_path):
    db = tmp_path / "subtitles.db"
    cache_db.init_db(db)
    with pytest.raises(RecordNotFoundError):
        translate_record(
            db, 12345,
            arabic_cache_dir=tmp_path / "arabic",
            gemini_call=_fake_translation_reply,
        )


def test_translate_record_unit_raises_on_missing_english_file(tmp_path):
    db = tmp_path / "subtitles.db"
    rid = cache_db.insert_subtitle(
        db,
        video_id="tt0002",
        video_type="movie",
        release_name=None,
        english_srt_path=str(tmp_path / "no-such.srt"),
        english_srt_hash="deadbeef",
    )
    with pytest.raises(EnglishFileMissingError):
        translate_record(
            db, rid,
            arabic_cache_dir=tmp_path / "arabic",
            gemini_call=_fake_translation_reply,
        )
    rec = cache_db.get_record(db, rid)
    assert rec["status"] == "failed"
    assert "missing" in rec["error_message"].lower()


def test_translate_record_does_not_overwrite_without_force(tmp_path):
    english = tmp_path / "english"
    english.mkdir()
    arabic_dir = tmp_path / "arabic"
    arabic_dir.mkdir()
    db = tmp_path / "subtitles.db"

    en_file = english / "x.srt"
    en_file.write_text(VALID_SRT_BYTES.decode("utf-8"), encoding="utf-8")
    arabic_file = arabic_dir / "tt0003_abc123456789.ar.srt"
    arabic_file.write_text("original arabic", encoding="utf-8")

    rid = cache_db.insert_subtitle(
        db,
        video_id="tt0003",
        video_type="movie",
        release_name=None,
        english_srt_path=str(en_file),
        english_srt_hash="abc1234567890def",
        arabic_srt_path=str(arabic_file),
        status="translated",
    )

    def should_not_run(prompt: str) -> str:
        raise AssertionError("gemini_call should not run")

    result = translate_record(
        db,
        rid,
        arabic_cache_dir=arabic_dir,
        gemini_call=should_not_run,
    )

    assert result["status"] == "translated"
    assert arabic_file.read_text(encoding="utf-8") == "original arabic"


# ---- gemini_service config helper ---------------------------------------


def test_gemini_get_config_raises_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(gemini_service.GeminiNotConfiguredError):
        gemini_service.get_config()


def test_gemini_get_config_uses_default_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    key, model = gemini_service.get_config()
    assert key == "k"
    assert model == gemini_service.DEFAULT_MODEL


def test_gemini_get_config_honors_env_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-1.5-flash")
    _, model = gemini_service.get_config()
    assert model == "gemini-1.5-flash"


# ---- srt_chunker + srt_cleaner unit checks ------------------------------


def test_srt_chunker_round_trip():
    from utils.srt_chunker import parse_srt, render_srt
    entries = parse_srt(VALID_SRT_BYTES.decode("utf-8"))
    assert len(entries) == 2
    assert entries[0].timestamp == "00:00:01,000 --> 00:00:04,000"
    assert entries[0].text == "Hello world"
    rendered = render_srt(entries)
    assert "00:00:01,000 --> 00:00:04,000" in rendered
    assert "Hello world" in rendered


def test_srt_cleaner_tolerates_code_fences():
    from utils.srt_cleaner import parse_numbered_translations
    raw = "```\n1) one\n2) two\n3) three\n```"
    out = parse_numbered_translations(raw)
    assert out == {1: "one", 2: "two", 3: "three"}


def test_srt_cleaner_raises_when_no_lines():
    from utils.srt_cleaner import parse_numbered_translations
    with pytest.raises(TranslationFormatError):
        parse_numbered_translations("no numbers here at all")
