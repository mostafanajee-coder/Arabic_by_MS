"""SRT download endpoint.

Phase 2 supports two subtitle-id shapes:

* ``cached-<record_id>`` — serve the Arabic SRT recorded in the cache DB.
* anything else (e.g. ``arabic-ms-<video_id>``) — serve the bundled
  sample SRT. This keeps the Phase 1 endpoint shape working as a
  fallback when nothing is cached yet.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import config
from services.cache_db import get_record

router = APIRouter()


def _serve_sample(subtitle_id: str) -> FileResponse:
    if not config.SAMPLE_SRT_PATH.exists():
        raise HTTPException(status_code=404, detail="Sample subtitle file not found")
    return FileResponse(
        path=str(config.SAMPLE_SRT_PATH),
        media_type="application/x-subrip",
        filename=f"{subtitle_id}.srt",
    )


@router.get("/download/{subtitle_id}.srt")
def download_subtitle(subtitle_id: str) -> FileResponse:
    """Serve either a cached Arabic SRT or the bundled sample."""
    if subtitle_id.startswith("cached-"):
        try:
            record_id = int(subtitle_id.split("-", 1)[1])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cached subtitle id") from exc

        record = get_record(config.DB_PATH, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Subtitle record not found")

        arabic_path_str = record.get("arabic_srt_path")
        if not arabic_path_str:
            raise HTTPException(
                status_code=404,
                detail="Arabic subtitle not yet available for this record",
            )

        arabic_path = Path(arabic_path_str)
        if not arabic_path.exists():
            raise HTTPException(status_code=404, detail="Cached subtitle file missing on disk")

        return FileResponse(
            path=str(arabic_path),
            media_type="application/x-subrip",
            filename=f"{subtitle_id}.srt",
        )

    return _serve_sample(subtitle_id)
