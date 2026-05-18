"""Generated status-subtitle route for honest Stremio responses."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from . import config
from services import job_manager
from services.cache_db import (
    find_best_record_for_video,
    find_latest_arabic_for_video,
    list_records_for_video,
)
from utils.status_srt import build_status_srt, get_status_message
from utils.stremio_id import parse_stremio_video_id

router = APIRouter()

STATUS_SUBTITLE_NAME = f"{config.ADDON_NAME} - Status"


def _episode_suffix(identity: Dict[str, Any]) -> str:
    season = identity.get("season")
    episode = identity.get("episode")
    if identity.get("is_episode") and season is not None and episode is not None:
        return " الموسم {0} الحلقة {1}".format(season, episode)
    return ""


def resolve_video_subtitle_state(video_id: str) -> Dict[str, Any]:
    """Resolve whether a video has real Arabic or only a status subtitle."""
    identity = parse_stremio_video_id(video_id)
    translated = find_latest_arabic_for_video(
        config.DB_PATH,
        video_id,
        canonical_video_key=identity["canonical_video_key"],
    )
    if translated:
        arabic_path = str(translated.get("arabic_srt_path") or "").strip()
        if arabic_path and Path(arabic_path).exists():
            return {
                "video_id": video_id,
                "identity": identity,
                "state": "translated",
                "record": translated,
            }

    records = list_records_for_video(
        config.DB_PATH,
        video_id,
        canonical_video_key=identity["canonical_video_key"],
    )
    if not records:
        return {
            "video_id": video_id,
            "identity": identity,
            "state": "no_record",
            "record": None,
        }

    for record in records:
        record_id = int(record["id"])
        running_job = job_manager.get_running_job_for_record(record_id)
        if running_job:
            return {
                "video_id": video_id,
                "identity": identity,
                "state": "translating",
                "record": record,
                "job": running_job,
            }

    record = find_best_record_for_video(
        config.DB_PATH,
        video_id,
        canonical_video_key=identity["canonical_video_key"],
    ) or records[0]
    status = str(record.get("status") or "").strip().lower()
    if status in ("queued", "running", "translating"):
        state = "translating"
    elif status == "failed":
        state = "failed"
    elif status == "uploaded":
        state = "uploaded_not_translated"
    else:
        state = "unknown"
    return {
        "video_id": video_id,
        "identity": identity,
        "state": state,
        "record": record,
    }


def build_status_subtitle_item(video_id: str, *, base_url: str) -> Dict[str, Any]:
    """Return the Stremio subtitle entry for a generated status SRT."""
    return {
        "id": "status-{0}".format(
            parse_stremio_video_id(video_id)["canonical_video_key"] or video_id
        ),
        "url": f"{base_url}/status-subtitle/{quote(video_id, safe='')}.srt",
        "lang": "ara",
        "name": STATUS_SUBTITLE_NAME,
    }


@router.get("/status-subtitle/{video_id}.srt")
def status_subtitle(video_id: str) -> Response:
    """Return a generated status subtitle for the requested video."""
    state = resolve_video_subtitle_state(video_id)
    identity = state.get("identity") or {}
    message = get_status_message(str(state["state"])) + _episode_suffix(identity)
    body = build_status_srt(message)
    return Response(content=body, media_type="application/x-subrip; charset=utf-8")
