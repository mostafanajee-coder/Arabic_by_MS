"""Scoring helpers for ranking external subtitle search results."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_SEASON_RE = re.compile(r"\bS(\d{1,2})E(\d{1,2})\b", re.IGNORECASE)
_IMPORTANT_TOKENS = (
    "web dl",
    "webrip",
    "bluray",
    "bdrip",
    "hdtv",
    "subsplease",
    "erai raws",
)


def score_subtitle_match(
    *,
    video_id: str,
    language: str,
    release_name: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    candidate: Dict[str, Any],
    matched_video: Optional[Dict[str, Any]] = None,
) -> float:
    """Return a heuristic score for how well a subtitle candidate matches."""
    score = 0.0

    if _video_id_matches(video_id, candidate, matched_video):
        score += 40.0

    candidate_language = _candidate_language(candidate)
    if _is_english_match(language, candidate_language):
        score += 15.0

    if season is not None:
        candidate_season = _extract_int(candidate, ("season", "season_number"))
        if candidate_season == season:
            score += 10.0

    if episode is not None:
        candidate_episode = _extract_int(candidate, ("episode", "episode_number"))
        if candidate_episode == episode:
            score += 10.0

    candidate_release = extract_release_name(candidate)
    if release_name and candidate_release:
        score += 25.0 * _release_similarity(release_name, candidate_release)
        score += 2.5 * _important_token_overlap(release_name, candidate_release)
    elif candidate_release:
        score += 5.0

    if season is not None and episode is not None and candidate_release:
        release_season, release_episode = _extract_season_episode(candidate_release)
        if release_season == season and release_episode == episode:
            score += 8.0

    return round(score, 2)


def sort_subtitle_matches(
    candidates: Iterable[Dict[str, Any]],
    *,
    video_id: str,
    language: str,
    release_name: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    matched_video: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return candidates sorted by descending score."""
    scored: List[Dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        item["score"] = score_subtitle_match(
            video_id=video_id,
            language=language,
            release_name=release_name,
            season=season,
            episode=episode,
            candidate=item,
            matched_video=matched_video,
        )
        scored.append(item)
    scored.sort(
        key=lambda item: (-float(item.get("score", 0.0)), str(item.get("release_name", "")))
    )
    return scored


def extract_release_name(candidate: Dict[str, Any]) -> str:
    """Return the best available release-name string from a raw candidate."""
    for key in ("release_name", "name", "title"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    releases = candidate.get("releases")
    if isinstance(releases, list):
        cleaned = [str(item).strip() for item in releases if str(item).strip()]
        if cleaned:
            return " | ".join(cleaned)

    return ""


def _video_id_matches(
    video_id: str,
    candidate: Dict[str, Any],
    matched_video: Optional[Dict[str, Any]],
) -> bool:
    expected = (video_id or "").strip().lower()
    if not expected:
        return False

    values = []
    for container in (candidate, matched_video or {}):
        values.extend(
            [
                container.get("imdb_id"),
                container.get("video_id"),
                container.get("imdb"),
            ]
        )
    for value in values:
        if str(value or "").strip().lower() == expected:
            return True
    return False


def _candidate_language(candidate: Dict[str, Any]) -> str:
    for key in ("language", "lang", "language_code"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_english_match(requested: str, actual: str) -> bool:
    requested_norm = (requested or "").strip().lower()
    actual_norm = (actual or "").strip().lower()
    if requested_norm in ("en", "eng", "english"):
        return actual_norm in ("en", "eng", "english")
    return requested_norm == actual_norm


def _extract_int(candidate: Dict[str, Any], keys: Iterable[str]) -> Optional[int]:
    for key in keys:
        value = candidate.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _release_similarity(left: str, right: str) -> float:
    left_norm = _normalize_release(left)
    right_norm = _normalize_release(right)
    if not left_norm or not right_norm:
        return 0.0

    seq = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(_tokenize_release(left_norm))
    right_tokens = set(_tokenize_release(right_norm))
    if not left_tokens or not right_tokens:
        return seq

    overlap = len(left_tokens & right_tokens) / float(len(left_tokens | right_tokens))
    return max(seq, overlap)


def _important_token_overlap(left: str, right: str) -> float:
    left_tokens = set(_important_tokens(left))
    right_tokens = set(_important_tokens(right))
    return float(len(left_tokens & right_tokens))


def _important_tokens(value: str) -> List[str]:
    normalized = _normalize_release(value)
    return [token for token in _IMPORTANT_TOKENS if token in normalized]


def _extract_season_episode(value: str) -> Tuple[Optional[int], Optional[int]]:
    match = _SEASON_RE.search(value or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _normalize_release(value: str) -> str:
    out = (value or "").lower().strip()
    out = out.replace("_", " ").replace(".", " ").replace("-", " ")
    out = re.sub(r"\s+", " ", out)
    return out


def _tokenize_release(value: str) -> List[str]:
    return _TOKEN_RE.findall(value.lower())
