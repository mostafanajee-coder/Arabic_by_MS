"""FastAPI entrypoint for the Arabic by M.S Stremio subtitle addon."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .manifest import build_manifest
from .routes_companion import router as companion_router
from .routes_download import router as download_router
from .routes_subtitles import router as subtitles_router

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
def root() -> Dict[str, str]:
    """Friendly landing endpoint pointing the caller at the manifest."""
    return {
        "name": config.ADDON_NAME,
        "version": config.ADDON_VERSION,
        "manifest": f"{config.PUBLIC_BASE_URL}/manifest.json",
    }


@app.get("/manifest.json")
def manifest() -> Dict[str, Any]:
    """Stremio addon manifest."""
    return build_manifest()


app.include_router(subtitles_router)
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
