"""Stremio subtitles endpoint with honest real-vs-status behavior."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request

from . import config
from .routes_status import build_status_subtitle_item, resolve_video_subtitle_state

router = APIRouter()
def _cached_arabic_subtitle_item(record_id: int, *, base_url: str) -> Dict[str, Any]:
    """Stremio subtitle entry that resolves to a cached Arabic SRT."""
    subtitle_id = f"cached-{record_id}"
    return {
        "id": subtitle_id,
        "url": f"{base_url}/download/{subtitle_id}.srt",
        "lang": "ara",
        "name": config.ADDON_NAME,
    }


@router.get("/subtitles/{video_type}/{video_id}.json")
def get_subtitles(
    request: Request,
    video_type: str,
    video_id: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return subtitle options for the requested video.

    The `video_type` segment is accepted for Stremio compatibility.
    """
    base_url = config.get_base_url(request)

    state = resolve_video_subtitle_state(video_id)
    record = state.get("record")
    if state.get("state") == "translated" and record:
        return {
            "subtitles": [
                _cached_arabic_subtitle_item(int(record["id"]), base_url=base_url)
            ]
        }

    return {"subtitles": [build_status_subtitle_item(video_id, base_url=base_url)]}
