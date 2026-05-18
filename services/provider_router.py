"""Unified provider router for companion search and import workflows."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services import opensubtitles_service, provider_reliability, subdl_service, subsource_service
from services.gemini_service import get_status as get_gemini_status
from services.opensubtitles_service import OpenSubtitlesError, OpenSubtitlesNotConfiguredError
from services.subdl_service import SubDLError, SubDLNotConfiguredError
from services.subsource_service import SubSourceError, SubSourceNotConfiguredError
from utils.srt_quality import analyze_srt_quality, merge_quality_metadata
from utils.srt_validator import SRTValidationError, validate_srt_content

PROVIDER_SUBDL = "subdl"
PROVIDER_SUBSOURCE = "subsource"
PROVIDER_OPENSUBTITLES = "opensubtitles"
SUPPORTED_PROVIDERS = (
    PROVIDER_SUBDL,
    PROVIDER_SUBSOURCE,
    PROVIDER_OPENSUBTITLES,
)


def get_provider_status() -> Dict[str, Any]:
    """Return a single payload describing translation and provider readiness."""
    return {
        "gemini": get_gemini_status(),
        "subdl": subdl_service.get_status(),
        "subsource": subsource_service.get_status(),
        "opensubtitles": opensubtitles_service.get_status(),
    }


def search_all_subtitles(
    *,
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    query: Optional[str] = None,
    language: str = "en",
    release_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Search all configured providers without failing the whole request."""
    items: List[Dict[str, Any]] = []
    provider_errors: Dict[str, Dict[str, Any]] = {}
    searched_providers: List[str] = []

    for provider in SUPPORTED_PROVIDERS:
        status = _get_single_provider_status(provider)
        if not status.get("configured"):
            continue

        searched_providers.append(provider)
        try:
            items.extend(
                _search_provider(
                    provider,
                    video_id=video_id,
                    video_type=video_type,
                    season=season,
                    episode=episode,
                    query=query,
                    language=language,
                    release_name=release_name,
                )
            )
        except (
            SubDLNotConfiguredError,
            SubSourceNotConfiguredError,
            OpenSubtitlesNotConfiguredError,
        ) as exc:
            provider_errors[provider] = _normalize_provider_error(provider, exc)
        except (SubDLError, SubSourceError, OpenSubtitlesError) as exc:
            provider_errors[provider] = _normalize_provider_error(provider, exc)

    return {
        "items": _dedupe_and_sort(items),
        "provider_errors": provider_errors,
        "searched_providers": searched_providers,
    }


def download_subtitle_data(provider: str, download_url: str) -> bytes:
    """Download subtitle bytes using the provider-specific downloader."""
    provider_name = _normalize_provider(provider)
    if provider_name == PROVIDER_SUBDL:
        return subdl_service.download_subtitle_data(download_url)
    if provider_name == PROVIDER_SUBSOURCE:
        return subsource_service.download_subtitle_data(download_url)
    if provider_name == PROVIDER_OPENSUBTITLES:
        return opensubtitles_service.download_subtitle_data(download_url)
    raise ValueError("Unsupported provider: {0}".format(provider))


def download_and_analyze_subtitle(
    provider: str,
    download_url: str,
    *,
    expected_language: str = "en",
    strict_quality: bool = False,
) -> Dict[str, Any]:
    """Download, validate, and analyze subtitle quality after provider selection."""
    raw = download_subtitle_data(provider, download_url)
    text = validate_srt_content(raw)
    quality = analyze_srt_quality(text, expected_language=expected_language)
    payload = merge_quality_metadata({"text": text}, quality)
    if strict_quality and payload["reject_hint"]:
        raise SRTValidationError(
            payload.get("quality_message") or "Subtitle quality checks suggest rejection."
        )
    return payload


def summarize_provider_errors(provider_errors: Dict[str, Any]) -> Optional[str]:
    """Return a safe one-line summary of provider failures."""
    return provider_reliability.summarize_provider_errors(
        provider_errors,
        providers=SUPPORTED_PROVIDERS,
    )


def get_reliability_settings() -> Dict[str, Any]:
    """Return the shared provider timeout and retry defaults."""
    return {
        "retries": provider_reliability.get_retry_settings(),
        "timeouts": {
            PROVIDER_SUBDL: {
                "search_seconds": provider_reliability.DEFAULT_SEARCH_TIMEOUT,
                "download_seconds": provider_reliability.DEFAULT_DOWNLOAD_TIMEOUT,
            },
            PROVIDER_SUBSOURCE: {
                "search_seconds": provider_reliability.DEFAULT_SEARCH_TIMEOUT,
                "download_seconds": provider_reliability.DEFAULT_DOWNLOAD_TIMEOUT,
            },
            PROVIDER_OPENSUBTITLES: {
                "search_seconds": provider_reliability.DEFAULT_SEARCH_TIMEOUT,
                "download_seconds": provider_reliability.DEFAULT_DOWNLOAD_TIMEOUT,
            },
        },
    }


def describe_selected_item(items: List[Dict[str, Any]]) -> str:
    """Return a short safe explanation for why the top-ranked item won."""
    if not items:
        return "No ranked subtitle result was available."
    best = items[0]
    reasons: List[str] = []
    breakdown = best.get("score_breakdown") or {}
    if float(breakdown.get("imdb_match", 0.0) or 0.0) > 0:
        reasons.append("IMDb matched")
    if float(breakdown.get("language_match", 0.0) or 0.0) > 0:
        reasons.append("language matched")
    if float(breakdown.get("season_match", 0.0) or 0.0) > 0 and float(breakdown.get("episode_match", 0.0) or 0.0) > 0:
        reasons.append("season and episode matched")
    elif float(breakdown.get("season_match", 0.0) or 0.0) > 0:
        reasons.append("season matched")
    elif float(breakdown.get("episode_match", 0.0) or 0.0) > 0:
        reasons.append("episode matched")
    if float(breakdown.get("release_episode_match", 0.0) or 0.0) > 0:
        reasons.append("release tag matched the episode")
    release_component = float(breakdown.get("release_similarity", 0.0) or 0.0)
    if release_component >= 18.0:
        reasons.append("strong release similarity")
    elif release_component >= 8.0:
        reasons.append("some release similarity")

    confidence = str(best.get("match_confidence") or "unknown")
    score = float(best.get("score", 0.0) or 0.0)
    message = "Selected because it had the highest score ({0:.2f}) with {1} confidence".format(score, confidence)
    if reasons:
        message += ", including " + ", ".join(reasons[:4])
    if len(items) > 1:
        gap = round(score - float(items[1].get("score", 0.0) or 0.0), 2)
        if gap > 0:
            message += ", and it beat the next result by {0:.2f} point(s)".format(gap)
    warnings = list(best.get("match_warnings") or [])
    if warnings:
        message += ". Warnings: " + "; ".join(str(item) for item in warnings[:2])
    else:
        message += "."
    return message


def _search_provider(
    provider: str,
    *,
    video_id: str,
    video_type: str,
    season: Optional[int],
    episode: Optional[int],
    query: Optional[str],
    language: str,
    release_name: Optional[str],
) -> List[Dict[str, Any]]:
    provider_name = _normalize_provider(provider)
    if provider_name == PROVIDER_SUBDL:
        return subdl_service.search_subtitles(
            video_id=video_id,
            video_type=video_type,
            season=season,
            episode=episode,
            query=query,
            language=language,
            release_name=release_name,
        )
    if provider_name == PROVIDER_SUBSOURCE:
        return subsource_service.search_subtitles(
            video_id=video_id,
            video_type=video_type,
            season=season,
            episode=episode,
            query=query,
            language=language,
            release_name=release_name,
        )
    if provider_name == PROVIDER_OPENSUBTITLES:
        return opensubtitles_service.search_subtitles(
            video_id=video_id,
            video_type=video_type,
            season=season,
            episode=episode,
            query=query,
            language=language,
            release_name=release_name,
        )
    raise ValueError("Unsupported provider: {0}".format(provider))


def _get_single_provider_status(provider: str) -> Dict[str, Any]:
    provider_name = _normalize_provider(provider)
    if provider_name == PROVIDER_SUBDL:
        return subdl_service.get_status()
    if provider_name == PROVIDER_SUBSOURCE:
        return subsource_service.get_status()
    if provider_name == PROVIDER_OPENSUBTITLES:
        return opensubtitles_service.get_status()
    raise ValueError("Unsupported provider: {0}".format(provider))


def _dedupe_and_sort(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda item: (
            -float(item.get("score", 0.0)),
            str(item.get("provider", "")),
            str(item.get("subtitle_id", "")),
            str(item.get("release_name", "")),
        ),
    )
    deduped: List[Dict[str, Any]] = []
    seen_identifiers = set()
    for item in ordered:
        identifiers = _dedupe_identifiers(item)
        if identifiers and any(identifier in seen_identifiers for identifier in identifiers):
            continue
        deduped.append(item)
        for identifier in identifiers:
            seen_identifiers.add(identifier)
    return deduped


def _dedupe_identifiers(item: Dict[str, Any]) -> List[str]:
    identifiers = []
    for key in ("download_url", "subtitle_id", "release_name"):
        value = _normalize_identifier(item.get(key))
        if value:
            identifiers.append("{0}:{1}".format(key, value))
    return identifiers


def _normalize_identifier(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip().lower()


def _normalize_provider(provider: str) -> str:
    return str(provider or "").strip().lower()


def _normalize_provider_error(provider: str, exc: Exception) -> Dict[str, Any]:
    return provider_reliability.normalize_provider_error(provider, exc)
