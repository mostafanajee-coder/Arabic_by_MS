"""Shared pytest setup.

* Makes the project root importable so `from backend...`, `from services...`,
  and `from utils...` all work.
* Auto-redirects the SQLite DB and cache directories to a temporary
  per-test location so tests never touch the real cache.
* Strips any GEMINI_* and SUBDL_* env vars by default; individual tests opt-in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402  (import after sys.path tweak)
from services import job_manager  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Point the DB and cache dirs at a temp dir for each test."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "subtitles.db")
    monkeypatch.setattr(config, "ENGLISH_CACHE_DIR", tmp_path / "english")
    monkeypatch.setattr(config, "ARABIC_CACHE_DIR", tmp_path / "arabic")
    # Default: no Gemini key. Tests that need one set it explicitly.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("SUBDL_API_KEY", raising=False)
    monkeypatch.delenv("SUBDL_BASE_URL", raising=False)
    monkeypatch.delenv("SUBSOURCE_API_KEY", raising=False)
    monkeypatch.delenv("SUBSOURCE_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    job_manager.reset_for_tests()
    yield
    job_manager.reset_for_tests()
