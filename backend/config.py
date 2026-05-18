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
Phase 17: adds OpenSubtitles env-var settings (read at call time in
services/opensubtitles_service.py).
Phase 18: adds provider reliability diagnostics and hardened retry handling.
Phase 19: adds subtitle match intelligence and best-choice explanations.
Phase 20: adds subtitle quality inspection and safe import warnings.
Phase 21: adds quality-aware import-best fallback across ranked candidates.
Phase 22: adds provider candidate quarantine memory and safe future deprioritization.
Phase 23: adds provider subtitle import history and duplicate-import prevention.
Phase 24: adds local-first subtitle reuse before provider search when safe.
Phase 25: adds cache integrity verification before cached local reuse.
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
ADDON_VERSION: str = os.getenv("ADDON_VERSION", "0.25.0")
ADDON_DESCRIPTION: str = os.getenv(
    "ADDON_DESCRIPTION",
    "Arabic subtitles for Stremio. Phase 25 keeps the Phase 24 local-first subtitle reuse, Phase 23 import history, Phase 22 quarantine memory, Phase 21 quality-aware fallback, Phase 20 subtitle quality inspection, Phase 19 match intelligence, Phase 18 provider reliability, and Phase 17 OpenSubtitles behavior, and adds cache integrity verification before cached subtitle reuse.",
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


def is_auto_prepare_on_subtitles_request_enabled() -> bool:
    """Return whether /subtitles should trigger background prepare attempts."""
    value = (os.getenv("AUTO_PREPARE_ON_SUBTITLES_REQUEST") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def is_allow_auto_prepare_when_limited_enabled() -> bool:
    """Return whether auto-prepare may continue even after daily limits."""
    value = (os.getenv("ALLOW_AUTO_PREPARE_WHEN_LIMITED") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def get_max_batch_prepare_items() -> int:
    """Return the safe maximum number of episodes allowed in one batch request."""
    raw = str(os.getenv("MAX_BATCH_PREPARE_ITEMS", "10") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10
    return value if value > 0 else 10


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
