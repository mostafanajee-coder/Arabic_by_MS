"""Helpers for parsing Stremio movie and episode IDs safely."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_IMDB_ID_RE = re.compile(r"^tt\d+$", re.IGNORECASE)


def _parse_positive_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def build_canonical_video_key(
    imdb_id: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> str:
    """Build the canonical lookup key for one movie or episode."""
    normalized_imdb_id = str(imdb_id or "").strip()
    if season is not None and episode is not None and normalized_imdb_id:
        return "{0}:s{1:02d}e{2:02d}".format(normalized_imdb_id, season, episode)
    return normalized_imdb_id


def parse_stremio_video_id(
    video_id: str,
    *,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Dict[str, Any]:
    """Parse a Stremio video id into canonical movie or episode identity.

    Supported shapes:
      - movie: ``tt1234567``
      - episode: ``tt1234567:1:5``

    Extra colon-separated parts are ignored safely. Invalid or custom ids are
    preserved instead of raising so older local workflows keep working.
    """
    raw_video_id = str(video_id or "").strip()
    parts = raw_video_id.split(":") if raw_video_id else []
    imdb_id = parts[0].strip() if parts else ""

    if _IMDB_ID_RE.match(imdb_id):
        imdb_id = imdb_id.lower()

    parsed_season = _parse_positive_int(parts[1]) if len(parts) >= 2 else None
    parsed_episode = _parse_positive_int(parts[2]) if len(parts) >= 3 else None

    resolved_season = _parse_positive_int(season)
    if resolved_season is None:
        resolved_season = parsed_season

    resolved_episode = _parse_positive_int(episode)
    if resolved_episode is None:
        resolved_episode = parsed_episode

    is_episode = resolved_season is not None and resolved_episode is not None
    canonical_video_key = build_canonical_video_key(
        imdb_id,
        resolved_season,
        resolved_episode,
    )

    return {
        "imdb_id": imdb_id,
        "season": resolved_season,
        "episode": resolved_episode,
        "canonical_video_key": canonical_video_key,
        "is_episode": is_episode,
    }
