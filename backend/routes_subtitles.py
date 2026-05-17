"""Stremio subtitles endpoint.

Phase 2 lookup order for a given video_id:

  1. If the cache DB has a record for this video_id with an Arabic SRT
     attached (arabic_srt_path IS NOT NULL), return that.
  2. Otherwise, fall back to the bundled sample Arabic SRT.

Translation isn't implemented yet, so in practice Phase 2 still serves
the sample for every request — but the cache-first plumbing is in place.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter

from . import config
from services.cache_db import find_latest_arabic_for_video

router = APIRouter()


def _sample_subtitle_item(video_id: str) -> Dict[str, Any]:
    """Stremio subtitle entry that resolves to the bundled sample SRT."""
    subtitle_id = f"arabic-ms-{video_id}"
    return {
        "id": subtitle_id,
        "url": f"{config.PUBLIC_BASE_URL}/download/{subtitle_id}.srt",
        "lang": "ara",
        "name": config.ADDON_NAME,
    }


def _cached_arabic_subtitle_item(record_id: int) -> Dict[str, Any]:
    """Stremio subtitle entry that resolves to a cached Arabic SRT."""
    subtitle_id = f"cached-{record_id}"
    return {
        "id": subtitle_id,
        "url": f"{config.PUBLIC_BASE_URL}/download/{subtitle_id}.srt",
        "lang": "ara",
        "name": config.ADDON_NAME,
    }


@router.get("/subtitles/{video_type}/{video_id}.json")
def get_subtitles(video_type: str, video_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Return subtitle options for the requested video.

    The `video_type` segment is accepted but not used for matching yet.
    """
    clean_id = video_id.split(":", 1)[0]

    record = find_latest_arabic_for_video(config.DB_PATH, clean_id)
    if record:
        return {"subtitles": [_cached_arabic_subtitle_item(int(record["id"]))]}

    return {"subtitles": [_sample_subtitle_item(clean_id)]}
