"""Tests for Phase 34 final readiness gate endpoints and safety coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app


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


def _sections_map(payload: dict) -> dict:
    return {
        str(item.get("section") or ""): item
        for item in list(payload.get("checked_sections") or [])
        if isinstance(item, dict)
    }


def test_final_readiness_endpoints_return_required_shape_and_sections(
    client: TestClient,
) -> None:
    get_response = client.get("/companion/final-readiness")
    run_response = client.post("/companion/final-readiness/run", json={})
    assert get_response.status_code == 200, get_response.text
    assert run_response.status_code == 200, run_response.text

    payload = run_response.json()
    for key in (
        "readiness_status",
        "readiness_score",
        "blocking_issues",
        "warnings",
        "checked_sections",
        "generated_at",
    ):
        assert key in payload

    sections = _sections_map(payload)
    required_sections = {
        "provider_configuration",
        "provider_diagnostics",
        "opensubtitles_behavior",
        "search_import_best",
        "quality_fallback",
        "quarantine",
        "import_history",
        "local_first_reuse",
        "cache_integrity",
        "cache_maintenance",
        "recycle_bin",
        "audit_trail",
        "snapshots",
        "rollback_plan",
        "rollback_execution",
        "operator_summary",
    }
    assert required_sections.issubset(sections.keys())


def test_final_readiness_does_not_expose_raw_srt_or_secrets(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBDL_API_KEY", "secret-subdl-key")
    monkeypatch.setenv("SUBSOURCE_API_KEY", "secret-subsource-key")
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "secret-opensubtitles-key")
    orphan = config.ENGLISH_CACHE_DIR / "phase34-final-readiness-secret.srt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("Never expose this subtitle text.\n" + GOOD_SRT, encoding="utf-8")

    response = client.post("/companion/final-readiness/run", json={})
    assert response.status_code == 200, response.text
    serialized = json.dumps(response.json())
    assert "Never expose this subtitle text" not in serialized
    assert "Hello there" not in serialized
    assert "secret-subdl-key" not in serialized
    assert "secret-subsource-key" not in serialized
    assert "secret-opensubtitles-key" not in serialized


def test_final_readiness_detects_missing_sample_asset_as_warning_or_blocking(
    client: TestClient,
    monkeypatch,
) -> None:
    sample_path = config.ARABIC_CACHE_DIR / config.SAMPLE_SRT_NAME
    monkeypatch.setattr(config, "SAMPLE_SRT_PATH", sample_path)
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text("1\n00:00:01,000 --> 00:00:03,000\nArabic by M.S\n", encoding="utf-8")
    if sample_path.exists():
        sample_path.unlink()
    response = client.get("/companion/final-readiness")
    assert response.status_code == 200, response.text
    payload = response.json()
    sections = _sections_map(payload)
    cache_maintenance_section = sections["cache_maintenance"]
    assert cache_maintenance_section["readiness_status"] in ("warning", "blocked")
    assert "sample" in str(cache_maintenance_section.get("message") or "").lower()


def test_final_readiness_handles_provider_disabled_state_without_crash(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.delenv("SUBDL_API_KEY", raising=False)
    monkeypatch.delenv("SUBSOURCE_API_KEY", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_USER_AGENT", raising=False)

    response = client.get("/companion/final-readiness")
    assert response.status_code == 200, response.text
    section = _sections_map(response.json())["provider_configuration"]
    assert section["readiness_status"] in ("ready", "warning", "blocked")


def test_final_readiness_confirms_phase17_opensubtitles_guardrails(
    client: TestClient,
) -> None:
    response = client.get("/companion/final-readiness")
    assert response.status_code == 200, response.text
    section = _sections_map(response.json())["opensubtitles_behavior"]
    details = section.get("details") or {}
    assert details["requires_parent_imdb_id_for_series_episode"] is True
    assert details["uses_imdb_id_for_movie_lookup"] is True
    assert details["download_path"] == "/download"


def test_final_readiness_confirms_phase29_to_phase33_maintenance_sections(
    client: TestClient,
) -> None:
    response = client.post("/companion/final-readiness/run", json={})
    assert response.status_code == 200, response.text
    sections = _sections_map(response.json())
    for name in (
        "cache_maintenance",
        "recycle_bin",
        "audit_trail",
        "snapshots",
        "rollback_plan",
        "rollback_execution",
        "operator_summary",
    ):
        assert name in sections
        assert sections[name]["readiness_status"] in ("ready", "warning", "blocked")
