"""Stremio subtitles endpoint with honest real-vs-status behavior."""

from __future__ import annotations

from urllib.parse import unquote
from typing import Any, Dict, List

from fastapi import APIRouter, Request

from . import config
from services import prepare_service
from .routes_status import build_status_subtitle_item, resolve_video_subtitle_state

router = APIRouter()


def _safe_extra_value(value: str) -> str:
    """Decode and normalize a Stremio extra route value safely."""
    text = unquote(str(value or ""))
    text = text.replace("\x00", "").strip()
    return text[:512]


def _parse_stremio_extra(extra: str) -> Dict[str, str]:
    """Parse optional `key=value` route extras without exposing unsafe input."""
    payload: Dict[str, str] = {}
    raw = _safe_extra_value(extra)
    if not raw:
        return payload
    for part in raw.split("&"):
        segment = str(part or "").strip()
        if not segment:
            continue
        if "=" in segment:
            key, value = segment.split("=", 1)
        else:
            key, value = "extra", segment
        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            continue
        payload[normalized_key[:64]] = _safe_extra_value(value)
    return payload


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
    return _build_subtitles_response(
        request=request,
        video_type=video_type,
        video_id=video_id,
        extra=None,
    )


@router.get("/subtitles/{video_type}/{video_id}/{extra}.json")
def get_subtitles_with_extra(
    request: Request,
    video_type: str,
    video_id: str,
    extra: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return subtitles for Stremio requests that include an extra route segment."""
    return _build_subtitles_response(
        request=request,
        video_type=video_type,
        video_id=video_id,
        extra=extra,
    )


def _build_subtitles_response(
    *,
    request: Request,
    video_type: str,
    video_id: str,
    extra: str | None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Shared subtitle route behavior for base and extra-path requests."""
    _ = _parse_stremio_extra(extra or "")
    base_url = config.get_base_url(request)

    state = resolve_video_subtitle_state(video_id)
    record = state.get("record")
    if state.get("state") == "translated" and record:
        return {
            "subtitles": [
                _cached_arabic_subtitle_item(int(record["id"]), base_url=base_url)
            ]
        }

    if config.is_auto_prepare_on_subtitles_request_enabled():
        try:
            prepare_service.request_prepare(
                video_id=video_id,
                video_type=video_type,
                db_path=config.DB_PATH,
                english_cache_dir=config.ENGLISH_CACHE_DIR,
                arabic_cache_dir=config.ARABIC_CACHE_DIR,
                run_async=True,
                request_source="auto_prepare",
            )
        except Exception:
            pass

    return {"subtitles": [build_status_subtitle_item(video_id, base_url=base_url)]}
