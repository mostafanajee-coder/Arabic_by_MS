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
    return evaluate_subtitle_match(
        video_id=video_id,
        language=language,
        release_name=release_name,
        season=season,
        episode=episode,
        candidate=candidate,
        matched_video=matched_video,
    )["score"]


def evaluate_subtitle_match(
    *,
    video_id: str,
    language: str,
    release_name: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    candidate: Dict[str, Any],
    matched_video: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return score plus transparent scoring details for one candidate."""
    breakdown = {
        "imdb_match": 0.0,
        "language_match": 0.0,
        "season_match": 0.0,
        "episode_match": 0.0,
        "release_similarity": 0.0,
        "important_tokens": 0.0,
        "release_episode_match": 0.0,
    }

    if _video_id_matches(video_id, candidate, matched_video):
        breakdown["imdb_match"] = 40.0

    candidate_language = _candidate_language(candidate)
    if _is_english_match(language, candidate_language):
        breakdown["language_match"] = 15.0

    candidate_season = _extract_int(candidate, ("season", "season_number"))
    if season is not None and candidate_season == season:
        breakdown["season_match"] = 10.0

    candidate_episode = _extract_int(candidate, ("episode", "episode_number"))
    if episode is not None and candidate_episode == episode:
        breakdown["episode_match"] = 10.0

    candidate_release = extract_release_name(candidate)
    release_similarity = 0.0
    important_token_overlap = 0.0
    if release_name and candidate_release:
        release_similarity = _release_similarity(release_name, candidate_release)
        important_token_overlap = _important_token_overlap(release_name, candidate_release)
        breakdown["release_similarity"] = round(25.0 * release_similarity, 2)
        breakdown["important_tokens"] = round(2.5 * important_token_overlap, 2)
    elif candidate_release:
        breakdown["release_similarity"] = 5.0

    release_season, release_episode = (None, None)
    if season is not None and episode is not None and candidate_release:
        release_season, release_episode = _extract_season_episode(candidate_release)
        if release_season == season and release_episode == episode:
            breakdown["release_episode_match"] = 8.0

    score = round(sum(float(value) for value in breakdown.values()), 2)
    warnings = _build_match_warnings(
        requested_language=language,
        candidate_language=candidate_language,
        requested_release=release_name,
        candidate_release=candidate_release,
        release_similarity=release_similarity,
        requested_season=season,
        requested_episode=episode,
        candidate_season=candidate_season,
        candidate_episode=candidate_episode,
        release_season=release_season,
        release_episode=release_episode,
        download_url=_candidate_download_url(candidate),
    )
    return {
        "score": score,
        "score_breakdown": breakdown,
        "match_confidence": _match_confidence(score, warnings),
        "match_warnings": warnings,
    }


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
        evaluation = evaluate_subtitle_match(
            video_id=video_id,
            language=language,
            release_name=release_name,
            season=season,
            episode=episode,
            candidate=item,
            matched_video=matched_video,
        )
        item["score"] = evaluation["score"]
        item["score_breakdown"] = evaluation["score_breakdown"]
        item["match_confidence"] = evaluation["match_confidence"]
        item["match_warnings"] = evaluation["match_warnings"]
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


def _candidate_download_url(candidate: Dict[str, Any]) -> str:
    value = candidate.get("download_url")
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


def _build_match_warnings(
    *,
    requested_language: str,
    candidate_language: str,
    requested_release: Optional[str],
    candidate_release: str,
    release_similarity: float,
    requested_season: Optional[int],
    requested_episode: Optional[int],
    candidate_season: Optional[int],
    candidate_episode: Optional[int],
    release_season: Optional[int],
    release_episode: Optional[int],
    download_url: str,
) -> List[str]:
    warnings: List[str] = []
    if requested_release and candidate_release and release_similarity < 0.45:
        warnings.append("Weak release match.")
    if not download_url:
        warnings.append("Download URL is missing.")
    if candidate_language and not _is_english_match(requested_language, candidate_language):
        warnings.append("Requested language does not match candidate language.")
    if _has_season_episode_mismatch(
        requested_season=requested_season,
        requested_episode=requested_episode,
        candidate_season=candidate_season,
        candidate_episode=candidate_episode,
        release_season=release_season,
        release_episode=release_episode,
    ):
        warnings.append("Season/episode details do not match the requested title.")
    return warnings


def _has_season_episode_mismatch(
    *,
    requested_season: Optional[int],
    requested_episode: Optional[int],
    candidate_season: Optional[int],
    candidate_episode: Optional[int],
    release_season: Optional[int],
    release_episode: Optional[int],
) -> bool:
    if requested_season is not None:
        if candidate_season is not None and candidate_season != requested_season:
            return True
        if release_season is not None and release_season != requested_season:
            return True
    if requested_episode is not None:
        if candidate_episode is not None and candidate_episode != requested_episode:
            return True
        if release_episode is not None and release_episode != requested_episode:
            return True
    return False


def _match_confidence(score: float, warnings: List[str]) -> str:
    if score >= 80.0 and not warnings:
        return "high"
    if score >= 45.0 and len(warnings) <= 2:
        return "medium"
    return "low"
