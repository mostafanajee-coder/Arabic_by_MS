"""OpenSubtitles search and import helpers."""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

from services import provider_reliability
from utils.subtitle_matcher import extract_release_name, sort_subtitle_matches

DEFAULT_BASE_URL = "https://api.opensubtitles.com/api/v1"
SEARCH_PATH = "/subtitles"
DOWNLOAD_PATH = "/download"


class OpenSubtitlesError(RuntimeError):
    """Generic OpenSubtitles provider failure."""


class OpenSubtitlesNotConfiguredError(OpenSubtitlesError):
    """Raised when OpenSubtitles env vars are missing."""


def get_config() -> Tuple[str, str, str]:
    """Return (api_key, user_agent, base_url). Raises if disabled."""
    api_key = (os.getenv("OPENSUBTITLES_API_KEY") or "").strip()
    user_agent = (os.getenv("OPENSUBTITLES_USER_AGENT") or "").strip()
    base_url = (os.getenv("OPENSUBTITLES_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    missing = _missing_config_vars(api_key, user_agent)
    if missing:
        raise provider_reliability.make_provider_error(
            OpenSubtitlesNotConfiguredError,
            provider="opensubtitles",
            operation="config",
            message=(
                "{0} missing. Add {1} to your environment or .env file before "
                "searching or importing from OpenSubtitles."
            ).format(
                ", ".join(missing),
                ", ".join(missing),
            ),
            error_type="missing_config",
        )
    return api_key, user_agent, base_url.rstrip("/")


def get_status() -> Dict[str, Any]:
    """Return OpenSubtitles configuration status for the companion UI."""
    api_key = (os.getenv("OPENSUBTITLES_API_KEY") or "").strip()
    user_agent = (os.getenv("OPENSUBTITLES_USER_AGENT") or "").strip()
    base_url = (os.getenv("OPENSUBTITLES_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    missing = _missing_config_vars(api_key, user_agent)
    if not missing:
        return {
            "configured": True,
            "base_url": base_url,
            "message": "OpenSubtitles is configured and ready for subtitle search/import.",
        }
    return {
        "configured": False,
        "base_url": base_url,
        "message": (
            "{0} missing. Add them to your environment or .env file before "
            "searching or importing from OpenSubtitles."
        ).format(", ".join(missing)),
    }


def search_subtitles(
    *,
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    query: Optional[str] = None,
    language: str = "en",
    release_name: Optional[str] = None,
    timeout: float = provider_reliability.DEFAULT_SEARCH_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Search OpenSubtitles and return normalized subtitle matches sorted by score."""
    api_key, user_agent, base_url = get_config()

    candidates: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()

    if _looks_like_imdb_id(video_id):
        response = _request_search(
            base_url,
            api_key,
            user_agent,
            _build_id_search_params(
                video_id=video_id,
                video_type=video_type,
                season=season,
                episode=episode,
                language=language,
            ),
            timeout=timeout,
        )
        _append_candidates(candidates, seen_keys, response)

    for fallback_query in _build_fallback_queries(
        video_id=video_id,
        query=query,
        release_name=release_name,
    ):
        response = _request_search(
            base_url,
            api_key,
            user_agent,
            {
                "query": fallback_query,
                "languages": _normalize_language(language),
                "type": _normalize_type(video_type),
                "season_number": season,
                "episode_number": episode,
            },
            timeout=timeout,
        )
        _append_candidates(candidates, seen_keys, response)

    normalized = []
    for raw in candidates:
        normalized.append(
            normalize_result(
                raw,
                video_id=video_id,
                language=language,
                release_name=release_name or query,
                season=season,
                episode=episode,
                base_url=base_url,
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
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one OpenSubtitles subtitle item."""
    attrs = _attributes(raw)
    feature = _feature_details(raw)
    release = (
        _normalize_text(attrs.get("release"))
        or _normalize_text(raw.get("release"))
        or extract_release_name(attrs)
        or extract_release_name(raw)
        or _file_name(raw)
    )
    language_value = _raw_language(raw) or _normalize_language(language)
    subtitle_id = _subtitle_id(raw)
    download_url = _download_url(raw, base_url=base_url or DEFAULT_BASE_URL)
    imdb_id = _normalized_candidate_imdb(feature)

    scored = sort_subtitle_matches(
        [
            {
                "subtitle_id": subtitle_id,
                "language": language_value,
                "release_name": release,
                "download_url": download_url,
                "raw": raw,
                "imdb_id": imdb_id,
                "season": feature.get("season_number"),
                "episode": feature.get("episode_number"),
                "releases": attrs.get("files"),
            }
        ],
        video_id=video_id,
        language=language,
        release_name=release_name,
        season=season,
        episode=episode,
    )[0]

    return {
        "provider": "opensubtitles",
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


def download_subtitle_data(
    download_url: str,
    *,
    timeout: float = provider_reliability.DEFAULT_DOWNLOAD_TIMEOUT,
) -> bytes:
    """Download subtitle bytes from OpenSubtitles, extracting the first SRT from zips."""
    api_key, user_agent, base_url = get_config()
    if not download_url or not str(download_url).strip():
        raise provider_reliability.make_provider_error(
            OpenSubtitlesError,
            provider="opensubtitles",
            operation="download",
            message="OpenSubtitles download URL is missing.",
            error_type="bad_request",
        )

    resolved_url = str(download_url).strip()
    file_id = ""
    if resolved_url.startswith(base_url + DOWNLOAD_PATH):
        file_id = _extract_file_id_from_url(resolved_url)

    if file_id:
        initial = provider_reliability.run_with_retries(
            lambda: _request_generated_download(
                base_url + DOWNLOAD_PATH,
                file_id=file_id,
                api_key=api_key,
                user_agent=user_agent,
                timeout=timeout,
            ),
        )
    else:
        headers = {"User-Agent": user_agent}
        if resolved_url.startswith(base_url):
            headers = _api_headers(api_key, user_agent)
        initial = provider_reliability.run_with_retries(
            lambda: _request_direct_download(
                resolved_url,
                headers=headers,
                timeout=timeout,
            ),
        )

    return _handle_download_response(
        initial,
        resolved_url=resolved_url,
        user_agent=user_agent,
        timeout=timeout,
    )


def _request_search(
    base_url: str,
    api_key: str,
    user_agent: str,
    params: Dict[str, Any],
    *,
    timeout: float,
) -> Dict[str, Any]:
    query = {key: value for key, value in params.items() if value not in (None, "")}
    response = provider_reliability.run_with_retries(
        lambda: _request_search_response(
            base_url + SEARCH_PATH,
            params=query,
            headers=_api_headers(api_key, user_agent),
            timeout=timeout,
        ),
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise provider_reliability.make_provider_error(
            OpenSubtitlesError,
            provider="opensubtitles",
            operation="search",
            message="OpenSubtitles returned invalid JSON.",
            error_type="invalid_response",
        ) from exc
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data
    if isinstance(data, list):
        return {"data": data}
    raise provider_reliability.make_provider_error(
        OpenSubtitlesError,
        provider="opensubtitles",
        operation="search",
        message="OpenSubtitles response did not include a subtitle list.",
        error_type="invalid_response",
    )


def _append_candidates(
    out: List[Dict[str, Any]],
    seen_keys: Set[str],
    response: Dict[str, Any],
) -> None:
    for subtitle in response.get("data", []) or []:
        key = _dedupe_key(subtitle)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(subtitle)


def _build_id_search_params(
    *,
    video_id: str,
    video_type: str,
    season: Optional[int],
    episode: Optional[int],
    language: str,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "languages": _normalize_language(language),
        "type": _normalize_type(video_type),
        "season_number": season,
        "episode_number": episode,
    }
    imdb_key = "parent_imdb_id" if _uses_parent_imdb_id(video_type, season, episode) else "imdb_id"
    params[imdb_key] = _normalize_imdb_id(video_id)
    return params


def _build_fallback_queries(
    *,
    video_id: str,
    query: Optional[str],
    release_name: Optional[str],
) -> List[str]:
    values: List[str] = []
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
        values.append(candidate)
    return values


def _api_headers(api_key: str, user_agent: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Api-Key": api_key,
        "User-Agent": user_agent,
    }


def _http_get(url: str, **kwargs: Any) -> httpx.Response:
    try:
        return httpx.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise provider_reliability.make_provider_error(
            OpenSubtitlesError,
            provider="opensubtitles",
            operation="network",
            message="OpenSubtitles request failed because the provider could not be reached.",
            error_type="network_error",
            retryable=True,
        ) from exc


def _http_post(url: str, **kwargs: Any) -> httpx.Response:
    try:
        return httpx.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise provider_reliability.make_provider_error(
            OpenSubtitlesError,
            provider="opensubtitles",
            operation="network",
            message="OpenSubtitles request failed because the provider could not be reached.",
            error_type="network_error",
            retryable=True,
        ) from exc


def _attributes(raw: Dict[str, Any]) -> Dict[str, Any]:
    attrs = raw.get("attributes")
    return attrs if isinstance(attrs, dict) else raw


def _feature_details(raw: Dict[str, Any]) -> Dict[str, Any]:
    details = _attributes(raw).get("feature_details")
    return details if isinstance(details, dict) else {}


def _normalize_language(language: str) -> str:
    return str(language or "en").strip().lower() or "en"


def _normalize_type(video_type: str) -> str:
    return "episode" if _is_episode_type(video_type) else "movie"


def _is_episode_type(video_type: str) -> bool:
    return str(video_type or "").strip().lower() in {"series", "episode", "tv"}


def _uses_parent_imdb_id(video_type: str, season: Optional[int], episode: Optional[int]) -> bool:
    normalized = str(video_type or "").strip().lower()
    return normalized in {"series", "tv"} and season is not None and episode is not None


def _normalize_imdb_id(video_id: str) -> str:
    text = str(video_id or "").strip().lower()
    if text.startswith("tt") and text[2:].isdigit():
        return text[2:]
    return text


def _normalized_candidate_imdb(feature: Dict[str, Any]) -> Optional[str]:
    parent_imdb = feature.get("parent_imdb_id")
    if parent_imdb not in (None, ""):
        return "tt{0}".format(parent_imdb)
    imdb = feature.get("imdb_id")
    if imdb not in (None, ""):
        return "tt{0}".format(imdb)
    return None


def _looks_like_imdb_id(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith("tt") and text[2:].isdigit()


def _raw_language(raw: Dict[str, Any]) -> str:
    attrs = _attributes(raw)
    for key in ("language", "lang", "language_code"):
        value = attrs.get(key) or raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "en"


def _subtitle_id(raw: Dict[str, Any]) -> str:
    attrs = _attributes(raw)
    for key in ("subtitle_id", "id", "sub_id"):
        value = attrs.get(key)
        if value not in (None, ""):
            return str(value)
    if raw.get("id") not in (None, ""):
        return str(raw.get("id"))
    return ""


def _file_name(raw: Dict[str, Any]) -> str:
    attrs = _attributes(raw)
    files = attrs.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                name = _normalize_text(item.get("file_name"))
                if name:
                    return name
    return ""


def _file_id(raw: Dict[str, Any]) -> str:
    attrs = _attributes(raw)
    files = attrs.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("file_id") not in (None, ""):
                return str(item.get("file_id"))
    return ""


def _download_url(raw: Dict[str, Any], *, base_url: str) -> str:
    attrs = _attributes(raw)
    for key in ("download_url", "download", "link"):
        value = attrs.get(key) or raw.get(key)
        if isinstance(value, str) and value.strip():
            stripped = value.strip()
            if stripped.startswith("http://") or stripped.startswith("https://"):
                return stripped
    file_id = _file_id(raw)
    if file_id:
        return "{0}{1}?file_id={2}&sub_format=srt".format(base_url.rstrip("/"), DOWNLOAD_PATH, file_id)
    page_url = attrs.get("url") or raw.get("url")
    if isinstance(page_url, str) and page_url.strip():
        return page_url.strip()
    return ""


def _dedupe_key(raw: Dict[str, Any]) -> str:
    return _file_id(raw) or _subtitle_id(raw) or _download_url(raw, base_url=DEFAULT_BASE_URL) or repr(sorted(raw.items()))


def _extract_file_id_from_url(download_url: str) -> str:
    parsed = urlparse(download_url)
    values = parse_qs(parsed.query).get("file_id") or []
    return str(values[0]).strip() if values else ""


def _decode_download_bytes(content: bytes, content_type: str, download_url: str) -> bytes:
    normalized_type = str(content_type or "").lower()
    if download_url.lower().endswith(".zip") or "zip" in normalized_type or _looks_like_zip(content):
        return _extract_srt_from_zip(content)
    return content


def _handle_download_response(
    response: httpx.Response,
    *,
    resolved_url: str,
    user_agent: str,
    timeout: float,
) -> bytes:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        link = _normalize_text(payload.get("link"))
        if link:
            binary = provider_reliability.run_with_retries(
                lambda: _request_direct_download(
                    link,
                    headers={"User-Agent": user_agent},
                    timeout=timeout,
                ),
            )
            return _decode_download_bytes(binary.content, binary.headers.get("content-type", ""), link)
        requested = _normalize_text(payload.get("requested_downloads")) or _normalize_text(payload.get("message"))
        raise provider_reliability.make_provider_error(
            OpenSubtitlesError,
            provider="opensubtitles",
            operation="download",
            message=requested or "OpenSubtitles download response did not include a link.",
            error_type="invalid_response",
        )

    return _decode_download_bytes(response.content, response.headers.get("content-type", ""), resolved_url)


def _request_search_response(
    url: str,
    *,
    params: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float,
) -> httpx.Response:
    response = _http_get(url, params=params, headers=headers, timeout=timeout)
    provider_reliability.raise_for_http_status(
        response,
        provider_label="OpenSubtitles",
        operation="search",
        error_cls=OpenSubtitlesError,
    )
    return response


def _request_generated_download(
    url: str,
    *,
    file_id: str,
    api_key: str,
    user_agent: str,
    timeout: float,
) -> httpx.Response:
    response = _http_post(
        url,
        json={"file_id": file_id, "sub_format": "srt"},
        headers=_api_headers(api_key, user_agent),
        timeout=timeout,
        follow_redirects=True,
    )
    provider_reliability.raise_for_http_status(
        response,
        provider_label="OpenSubtitles",
        operation="download",
        error_cls=OpenSubtitlesError,
    )
    return response


def _request_direct_download(
    url: str,
    *,
    headers: Dict[str, str],
    timeout: float,
) -> httpx.Response:
    response = _http_get(
        url,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    )
    provider_reliability.raise_for_http_status(
        response,
        provider_label="OpenSubtitles",
        operation="download",
        error_cls=OpenSubtitlesError,
    )
    return response


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
                    OpenSubtitlesError,
                    provider="opensubtitles",
                    operation="download",
                    message="OpenSubtitles zip download does not contain an .srt file.",
                    error_type="invalid_srt",
                )
            return archive.read(srt_names[0])
    except zipfile.BadZipFile as exc:
        raise provider_reliability.make_provider_error(
            OpenSubtitlesError,
            provider="opensubtitles",
            operation="download",
            message="OpenSubtitles returned an invalid zip download.",
            error_type="invalid_srt",
        ) from exc


def _public_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    attrs = _attributes(raw)
    feature = _feature_details(raw)
    return {
        "id": raw.get("id") if raw.get("id") not in (None, "") else _subtitle_id(raw),
        "language": _raw_language(raw),
        "release_name": _normalize_text(attrs.get("release")) or _normalize_text(raw.get("release")),
        "download_url": _download_url(raw, base_url=DEFAULT_BASE_URL),
        "feature_details": {
            "imdb_id": feature.get("imdb_id"),
            "parent_imdb_id": feature.get("parent_imdb_id"),
            "season_number": feature.get("season_number"),
            "episode_number": feature.get("episode_number"),
        },
        "files": attrs.get("files"),
    }


def _normalize_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _missing_config_vars(api_key: str, user_agent: str) -> List[str]:
    missing: List[str] = []
    if not api_key:
        missing.append("OPENSUBTITLES_API_KEY")
    if not user_agent:
        missing.append("OPENSUBTITLES_USER_AGENT")
    return missing
