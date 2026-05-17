"""Shared pytest setup.

* Makes the project root importable so `from backend...`, `from services...`,
  and `from utils...` all work.
* Auto-redirects the SQLite DB and cache directories to a temporary
  per-test location so tests never touch the real cache.
* Strips any GEMINI_* env vars by default; individual tests opt-in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402  (import after sys.path tweak)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Point the DB and cache dirs at a temp dir for each test."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "subtitles.db")
    monkeypatch.setattr(config, "ENGLISH_CACHE_DIR", tmp_path / "english")
    monkeypatch.setattr(config, "ARABIC_CACHE_DIR", tmp_path / "arabic")
    # Default: no Gemini key. Tests that need one set it explicitly.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    yield
