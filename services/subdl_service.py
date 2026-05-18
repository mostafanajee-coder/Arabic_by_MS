"""SubDL search and import helpers."""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from services import provider_reliability
from utils.subtitle_matcher import extract_release_name, sort_subtitle_matches

DEFAULT_BASE_URL = "https://api.subdl.com/api/v1"


class SubDLError(RuntimeError):
    """Generic SubDL provider failure."""


class SubDLNotConfiguredError(SubDLError):
    """Raised when SUBDL_API_KEY is missing."""


def get_config() -> Tuple[str, str]:
    """Return (api_key, base_url). Raises if SUBDL_API_KEY is missing."""
    api_key = (os.getenv("SUBDL_API_KEY") or "").strip()
    base_url = (os.getenv("SUBDL_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    if not api_key:
        raise provider_reliability.make_provider_error(
            SubDLNotConfiguredError,
            provider="subdl",
            operation="config",
            message=(
                "SUBDL_API_KEY is not set. Add it to your environment or .env file "
                "before searching or importing from SubDL."
            ),
            error_type="missing_config",
        )
    return api_key, base_url.rstrip("/")


def get_status() -> Dict[str, Any]:
    """Return SubDL configuration status for the companion UI."""
    api_key = (os.getenv("SUBDL_API_KEY") or "").strip()
    base_url = (os.getenv("SUBDL_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    if api_key:
        return {
            "configured": True,
            "base_url": base_url,
            "message": "SubDL is configured and ready for subtitle search/import.",
        }
    return {
        "configured": False,
        "base_url": base_url,
        "message": (
            "SUBDL_API_KEY is missing. Add it to your environment or .env file "
            "before searching or importing from SubDL."
        ),
    }


def search_subtitles(
    *,
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    query: Optional[str] = None,
    language: str = "EN",
    release_name: Optional[str] = None,
    timeout: float = provider_reliability.DEFAULT_SEARCH_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Search SubDL and return normalized subtitle matches sorted by score."""
    api_key, base_url = get_config()
    search_type = "tv" if (video_type or "").strip().lower() == "series" else "movie"

    candidates: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = []
    seen_keys: Set[str] = set()

    if _looks_like_imdb_id(video_id):
        response = _request_subtitles(
            base_url,
            api_key,
            {
                "imdb_id": video_id.strip(),
                "type": search_type,
                "languages": language,
                "season_number": season,
                "episode_number": episode,
                "releases": 1,
            },
            timeout=timeout,
        )
        _append_candidates(candidates, seen_keys, response)

    for fallback_mode, fallback_value in _build_fallback_queries(
        video_id=video_id,
        query=query,
        release_name=release_name,
    ):
        response = _request_subtitles(
            base_url,
            api_key,
            {
                fallback_mode: fallback_value,
                "type": search_type,
                "languages": language,
                "season_number": season,
                "episode_number": episode,
                "releases": 1,
            },
            timeout=timeout,
        )
        _append_candidates(candidates, seen_keys, response)

    normalized = []
    for raw, matched_video in candidates:
        normalized.append(
            normalize_result(
                raw,
                video_id=video_id,
                language=language,
                release_name=release_name or query,
                season=season,
                episode=episode,
                matched_video=matched_video,
            )
        )

    normalized.sort(
        key=lambda item: (-float(item.get("score", 0.0)), str(item.get("release_name", "")))
    )
    return normalized


def normalize_result(
    raw: Dict[str, Any],
    *,
    video_id: str,
    language: str,
    release_name: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    matched_video: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize one SubDL subtitle item."""
    release = extract_release_name(raw)
    language_value = _raw_language(raw) or language
    subtitle_id = _subtitle_id(raw)
    download_url = _download_url(raw)

    scored = sort_subtitle_matches(
        [
            {
                "subtitle_id": subtitle_id,
                "language": language_value,
                "release_name": release,
                "download_url": download_url,
                "raw": raw,
                "imdb_id": raw.get("imdb_id"),
                "season": raw.get("season") or raw.get("season_number"),
                "episode": raw.get("episode") or raw.get("episode_number"),
                "releases": raw.get("releases"),
            }
        ],
        video_id=video_id,
        language=language,
        release_name=release_name,
        season=season,
        episode=episode,
        matched_video=matched_video,
    )[0]

    return {
        "provider": "subdl",
        "subtitle_id": subtitle_id,
        "language": language_value,
        "release_name": release,
        "download_url": download_url,
        "score": scored["score"],
        "score_breakdown": scored["score_breakdown"],
        "match_confidence": scored["match_confidence"],
        "match_warnings": scored["match_warnings"],
        "raw": _public_raw(raw),
    }


def download_subtitle_data(download_url: str, *, timeout: float = 60.0) -> bytes:
    """Download a subtitle file from SubDL, extracting the first SRT from zips."""
    get_config()
    if not download_url or not str(download_url).strip():
        raise provider_reliability.make_provider_error(
            SubDLError,
            provider="subdl",
            operation="download",
            message="SubDL download URL is missing.",
            error_type="bad_request",
        )

    response = provider_reliability.run_with_retries(
        lambda: _request_download(download_url, timeout=timeout),
    )

    content = response.content
    content_type = response.headers.get("content-type", "").lower()
    if download_url.lower().endswith(".zip") or "zip" in content_type or _looks_like_zip(content):
        return _extract_srt_from_zip(content)
    return content


def _request_subtitles(
    base_url: str,
    api_key: str,
    params: Dict[str, Any],
    *,
    timeout: float,
) -> Dict[str, Any]:
    query = {"api_key": api_key}
    query.update({key: value for key, value in params.items() if value not in (None, "")})

    response = provider_reliability.run_with_retries(
        lambda: _send_search_request(
            base_url + "/subtitles",
            params=query,
            timeout=timeout,
        )
    )

    try:
        data = response.json()
    except ValueError as exc:
        raise provider_reliability.make_provider_error(
            SubDLError,
            provider="subdl",
            operation="search",
            message="SubDL returned invalid JSON.",
            error_type="invalid_response",
        ) from exc

    if not data.get("status", False):
        raise provider_reliability.make_provider_error(
            SubDLError,
            provider="subdl",
            operation="search",
            message=str(data.get("error") or "SubDL search failed."),
            error_type="provider_error",
        )
    return data


def _send_search_request(url: str, *, params: Dict[str, Any], timeout: float) -> httpx.Response:
    response = _http_get(
        url,
        params=params,
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    provider_reliability.raise_for_http_status(
        response,
        provider_label="SubDL",
        operation="search",
        error_cls=SubDLError,
    )
    return response


def _request_download(download_url: str, *, timeout: float) -> httpx.Response:
    response = _http_get(download_url, timeout=timeout, follow_redirects=True)
    provider_reliability.raise_for_http_status(
        response,
        provider_label="SubDL",
        operation="download",
        error_cls=SubDLError,
    )
    return response


def _append_candidates(
    out: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]],
    seen_keys: Set[str],
    response: Dict[str, Any],
) -> None:
    matched_video = _first_result(response)
    for subtitle in response.get("subtitles", []) or []:
        key = _dedupe_key(subtitle)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append((subtitle, matched_video))


def _build_fallback_queries(
    *,
    video_id: str,
    query: Optional[str],
    release_name: Optional[str],
) -> List[Tuple[str, str]]:
    values: List[Tuple[str, str]] = []
    candidates = []
    if query and query.strip():
        candidates.append(query.strip())
    if release_name and release_name.strip():
        candidates.append(release_name.strip())
    if video_id and not _looks_like_imdb_id(video_id):
        candidates.append(video_id.strip())

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_release(candidate):
            values.append(("file_name", candidate))
        values.append(("film_name", candidate))
    return values


def _raw_language(raw: Dict[str, Any]) -> str:
    for key in ("language", "lang", "language_code"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "EN"


def _subtitle_id(raw: Dict[str, Any]) -> str:
    for key in ("subtitle_id", "sd_id", "id", "sub_id"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _download_url(raw: Dict[str, Any]) -> str:
    for key in ("download_url", "download", "downloadLink", "link", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            stripped = value.strip()
            if stripped.startswith("http://") or stripped.startswith("https://"):
                return stripped

    file_id = raw.get("url") or raw.get("file_id")
    subtitle_id = raw.get("subtitle_id") or raw.get("sd_id") or raw.get("id")
    if file_id and subtitle_id:
        return "https://dl.subdl.com/subtitle/{0}-{1}.zip".format(subtitle_id, file_id)
    return ""


def _dedupe_key(raw: Dict[str, Any]) -> str:
    return _subtitle_id(raw) or _download_url(raw) or repr(sorted(raw.items()))


def _looks_like_imdb_id(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith("tt") and text[2:].isdigit()


def _looks_like_release(value: str) -> bool:
    lowered = (value or "").lower()
    return any(
        token in lowered
        for token in (".", "-", "web", "bluray", "hdtv", "s01e", "1080p", "720p")
    )


def _first_result(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    results = response.get("results") or []
    if results and isinstance(results[0], dict):
        return results[0]
    return None


def _looks_like_zip(content: bytes) -> bool:
    return bool(content and content[:4] == b"PK\x03\x04")


def _extract_srt_from_zip(content: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            srt_names = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and name.lower().endswith(".srt")
            ]
            if not srt_names:
                raise provider_reliability.make_provider_error(
                    SubDLError,
                    provider="subdl",
                    operation="download",
                    message="SubDL zip download does not contain an .srt file.",
                    error_type="invalid_srt",
                )
            return archive.read(srt_names[0])
    except zipfile.BadZipFile as exc:
        raise provider_reliability.make_provider_error(
            SubDLError,
            provider="subdl",
            operation="download",
            message="SubDL returned an invalid zip download.",
            error_type="invalid_srt",
        ) from exc


def _public_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": raw.get("id") if raw.get("id") not in (None, "") else _subtitle_id(raw),
        "imdb_id": raw.get("imdb_id"),
        "language": _raw_language(raw),
        "release_name": extract_release_name(raw),
        "download_url": _download_url(raw),
        "season": raw.get("season") or raw.get("season_number"),
        "episode": raw.get("episode") or raw.get("episode_number"),
        "releases": raw.get("releases"),
    }


def _http_get(url: str, **kwargs: Any) -> httpx.Response:
    try:
        return httpx.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise provider_reliability.make_provider_error(
            SubDLError,
            provider="subdl",
            operation="network",
            message="SubDL request failed because the provider could not be reached.",
            error_type="network_error",
            retryable=True,
        ) from exc
