"""Configuration for the Arabic by M.S Stremio subtitle addon.

Phase 1: only basic addon identity and paths.
Phase 2: adds cache/english/ and the SQLite metadata DB.
Phase 3: adds Gemini env-var settings (read at call time in
services/gemini_service.py — kept out of this module so tests can
monkeypatch them with monkeypatch.setenv).
Phase 5: adds SubDL env-var settings (read at call time in
services/subdl_service.py).
Phase 6: adds SubSource env-var settings (read at call time in
services/subsource_service.py).

External providers other than Gemini, SubDL, and SubSource (Nvidia,
OpenSubtitles) are intentionally NOT wired up yet.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Request

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root = parent of the backend/ package
BASE_DIR: Path = Path(__file__).resolve().parent.parent


def load_environment(env_path: Optional[Path] = None) -> bool:
    """Load environment variables from `.env` without overriding real env vars."""
    target = env_path or (BASE_DIR / ".env")
    return load_dotenv(target, override=False)


load_environment()

# Local cache directory where pre-made / downloaded SRT files live.
# Phase 1 ships a single sample file under cache/arabic/sample_arabic.srt
# Phase 2 adds cache/english/ for user uploads and cache/subtitles.db.
# Phase 3 writes translated Arabic SRTs under cache/arabic/.
CACHE_DIR: Path = BASE_DIR / "cache"
ARABIC_CACHE_DIR: Path = CACHE_DIR / "arabic"
ENGLISH_CACHE_DIR: Path = CACHE_DIR / "english"

SAMPLE_SRT_NAME: str = "sample_arabic.srt"
SAMPLE_SRT_PATH: Path = ARABIC_CACHE_DIR / SAMPLE_SRT_NAME

# SQLite database storing subtitle metadata.
DB_PATH: Path = CACHE_DIR / "subtitles.db"

# ---------------------------------------------------------------------------
# Addon identity (exposed via /manifest.json)
# ---------------------------------------------------------------------------

ADDON_ID: str = os.getenv("ADDON_ID", "community.arabic.by.ms")
ADDON_NAME: str = os.getenv("ADDON_NAME", "Arabic by M.S")
ADDON_VERSION: str = os.getenv("ADDON_VERSION", "0.12.0")
ADDON_DESCRIPTION: str = os.getenv(
    "ADDON_DESCRIPTION",
    "Arabic subtitles for Stremio. Phase 12 adds episode-aware canonical matching for tt1234567 and tt1234567:1:5 while keeping honest status subtitles until real Arabic is ready.",
)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8787"))

# Public base URL used when building absolute subtitle download links.
# When running locally Stremio talks to http://127.0.0.1:PORT.
PUBLIC_BASE_URL: str = (
    os.getenv("BASE_URL")
    or os.getenv("PUBLIC_BASE_URL")
    or f"http://127.0.0.1:{PORT}"
)


def get_explicit_base_url() -> str:
    """Return an explicitly configured public base URL when available."""
    return (
        (os.getenv("BASE_URL") or "").strip()
        or (os.getenv("PUBLIC_BASE_URL") or "").strip()
        or ""
    ).rstrip("/")


def get_base_url(request: Optional[Request] = None) -> str:
    """Return the best base URL for building local links."""
    explicit = get_explicit_base_url()
    if explicit:
        return explicit
    if request is not None:
        return str(request.base_url).rstrip("/")
    return PUBLIC_BASE_URL.rstrip("/")
