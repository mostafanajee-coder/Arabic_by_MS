"""Unified provider router for companion search and import workflows."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services import subdl_service, subsource_service
from services.gemini_service import get_status as get_gemini_status
from services.subdl_service import SubDLError, SubDLNotConfiguredError
from services.subsource_service import SubSourceError, SubSourceNotConfiguredError

PROVIDER_SUBDL = "subdl"
PROVIDER_SUBSOURCE = "subsource"
SUPPORTED_PROVIDERS = (PROVIDER_SUBDL, PROVIDER_SUBSOURCE)


def get_provider_status() -> Dict[str, Any]:
    """Return a single payload describing translation and provider readiness."""
    return {
        "gemini": get_gemini_status(),
        "subdl": subdl_service.get_status(),
        "subsource": subsource_service.get_status(),
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
    provider_errors: Dict[str, str] = {}
    searched_providers: List[str] = []

    for provider in SUPPORTED_PROVIDERS:
        status = _get_single_provider_status(provider)
        if not status.get("configured"):
            provider_errors[provider] = str(status.get("message") or "Provider is not configured.")
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
        except (SubDLNotConfiguredError, SubSourceNotConfiguredError) as exc:
            provider_errors[provider] = str(exc)
        except (SubDLError, SubSourceError) as exc:
            provider_errors[provider] = str(exc)

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
    raise ValueError("Unsupported provider: {0}".format(provider))


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
    raise ValueError("Unsupported provider: {0}".format(provider))


def _get_single_provider_status(provider: str) -> Dict[str, Any]:
    provider_name = _normalize_provider(provider)
    if provider_name == PROVIDER_SUBDL:
        return subdl_service.get_status()
    if provider_name == PROVIDER_SUBSOURCE:
        return subsource_service.get_status()
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
