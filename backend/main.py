"""FastAPI entrypoint for the Arabic by M.S Stremio subtitle addon."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .manifest import build_manifest
from .routes_companion import router as companion_router
from .routes_download import router as download_router
from .routes_status import router as status_router
from .routes_subtitles import router as subtitles_router
from utils.status_srt import build_status_srt

app = FastAPI(
    title=config.ADDON_NAME,
    version=config.ADDON_VERSION,
    description=config.ADDON_DESCRIPTION,
)

# Stremio talks to the addon from a different origin (the Stremio web
# player, the desktop app, etc.), so CORS must be wide open.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root(request: Request) -> Dict[str, str]:
    """Friendly landing endpoint pointing the caller at the manifest."""
    return {
        "name": config.ADDON_NAME,
        "version": config.ADDON_VERSION,
        "manifest": f"{config.get_base_url(request)}/manifest.json",
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    """Return local readiness information for the app and cache."""
    cache_db_ready = False
    try:
        from services.cache_db import init_db

        init_db(config.DB_PATH)
        cache_db_ready = config.DB_PATH.exists()
    except Exception:
        cache_db_ready = False

    cache_dirs_ready = False
    try:
        config.ENGLISH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        config.ARABIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_dirs_ready = (
            config.ENGLISH_CACHE_DIR.exists() and config.ARABIC_CACHE_DIR.exists()
        )
    except Exception:
        cache_dirs_ready = False

    status_subtitle_ready = False
    try:
        status_subtitle_ready = "-->" in build_status_srt("اختبار")
    except Exception:
        status_subtitle_ready = False

    status = "ok" if cache_db_ready and cache_dirs_ready else "degraded"
    return {
        "app": config.ADDON_NAME,
        "version": config.ADDON_VERSION,
        "status": status,
        "cache_db_ready": cache_db_ready,
        "cache_dirs_ready": cache_dirs_ready,
        "status_subtitle_ready": status_subtitle_ready,
    }


@app.get("/manifest.json")
def manifest() -> Dict[str, Any]:
    """Stremio addon manifest."""
    return build_manifest()


app.include_router(subtitles_router)
app.include_router(status_router)
app.include_router(download_router)
app.include_router(companion_router)


if __name__ == "__main__":  # pragma: no cover - manual run helper
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=True,
    )
