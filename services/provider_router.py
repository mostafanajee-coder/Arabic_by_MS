"""Unified provider router for companion search and import workflows."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services import (
    provider_import_history,
    opensubtitles_service,
    provider_quarantine,
    provider_reliability,
    subdl_service,
    subsource_service,
)
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
IMPORT_BEST_FALLBACK_LIMIT = 5


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


def import_best_with_quality_fallback(
    items: List[Dict[str, Any]],
    *,
    expected_language: str = "en",
    max_candidates: int = IMPORT_BEST_FALLBACK_LIMIT,
    db_path: Optional[str] = None,
    legacy_video_id: Optional[str] = None,
    canonical_video_key: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Dict[str, Any]:
    """Try ranked candidates in order and fall back when download/quality is unsafe."""
    quarantine_context = _annotate_candidates_with_quarantine(
        items,
        db_path=db_path,
        legacy_video_id=legacy_video_id,
        canonical_video_key=canonical_video_key,
        season=season,
        episode=episode,
    )
    ranked_items = quarantine_context["ordered_items"]
    if not ranked_items:
        return {
            "selected_item": None,
            "selected_index": None,
            "selected_quality": None,
            "tried_candidates": [],
            "fallback_reason": None,
            "selection_warning": None,
            "remaining_ranked_items": [],
            "selected_quarantine": None,
            "quarantine_affected_selection": False,
            "quarantine_note": None,
            "selected_import_history": None,
            "reused_existing_record": False,
            "import_history_note": None,
        }

    limit = max(1, int(max_candidates or IMPORT_BEST_FALLBACK_LIMIT))
    tried_candidates: List[Dict[str, Any]] = []
    best_bad_item: Optional[Dict[str, Any]] = None
    best_bad_index: Optional[int] = None
    best_bad_quality: Optional[Dict[str, Any]] = None
    best_bad_reason: Optional[str] = None

    for index, item in enumerate(ranked_items[:limit]):
        tried_entry = _build_tried_candidate_entry(item, rank=index + 1)
        import_history = dict(tried_entry.get("import_history") or {})
        if bool(import_history.get("is_previously_imported")) and import_history.get("record_id"):
            history_quality = _quality_summary_from_import_history(import_history)
            tried_entry.update(
                {
                    "status": "selected_reused",
                    "skip_reason": None,
                    "skip_message": None,
                }
            )
            tried_entry.update(history_quality)
            if history_quality.get("reject_hint"):
                _record_quarantine_issue(
                    db_path=db_path,
                    tried_entry=tried_entry,
                    reason="bad_quality",
                    quality=history_quality,
                )
            tried_candidates.append(tried_entry)
            fallback_reason = None
            if index > 0:
                fallback_reason = _build_fallback_reason(tried_candidates[:-1])
                fallback_reason = _merge_reason_text(
                    fallback_reason,
                    quarantine_context.get("quarantine_note"),
                )
            return {
                "selected_item": item,
                "selected_index": index,
                "selected_quality": history_quality,
                "tried_candidates": tried_candidates,
                "fallback_reason": fallback_reason,
                "selection_warning": None,
                "remaining_ranked_items": ranked_items[index:],
                "selected_quarantine": tried_entry.get("quarantine") or item.get("quarantine"),
                "quarantine_affected_selection": bool(quarantine_context.get("ranking_changed")),
                "quarantine_note": quarantine_context.get("quarantine_note"),
                "selected_import_history": import_history,
                "reused_existing_record": True,
                "import_history_note": "This exact provider subtitle was already imported for this title, so the cached record was reused.",
            }

        download_url = str(item.get("download_url") or "").strip()
        if not download_url:
            tried_entry.update(
                {
                    "status": "skipped",
                    "skip_reason": "missing_url",
                    "skip_message": "Missing download URL.",
                }
            )
            _record_quarantine_issue(
                db_path=db_path,
                tried_entry=tried_entry,
                reason="missing_url",
                quality=None,
            )
            tried_candidates.append(tried_entry)
            continue

        provider = _normalize_provider(item.get("provider"))
        try:
            inspected = download_and_analyze_subtitle(
                provider,
                download_url,
                expected_language=expected_language,
                strict_quality=False,
            )
        except SRTValidationError as exc:
            tried_entry.update(
                {
                    "status": "skipped",
                    "skip_reason": "invalid_srt",
                    "skip_message": str(exc),
                }
            )
            _record_quarantine_issue(
                db_path=db_path,
                tried_entry=tried_entry,
                reason="invalid_srt",
                quality=None,
            )
            tried_candidates.append(tried_entry)
            continue
        except (
            SubDLNotConfiguredError,
            SubSourceNotConfiguredError,
            OpenSubtitlesNotConfiguredError,
            SubDLError,
            SubSourceError,
            OpenSubtitlesError,
            ValueError,
        ) as exc:
            tried_entry.update(
                {
                    "status": "skipped",
                    "skip_reason": "provider_error",
                    "skip_message": str(exc),
                    "provider_error": _normalize_provider_error(provider or str(item.get("provider") or ""), exc),
                }
            )
            _record_quarantine_issue(
                db_path=db_path,
                tried_entry=tried_entry,
                reason="provider_error",
                quality=None,
            )
            tried_candidates.append(tried_entry)
            continue

        tried_entry.update(_quality_summary_from_payload(inspected))
        if inspected.get("reject_hint"):
            tried_entry.update(
                {
                    "status": "skipped",
                    "skip_reason": "bad_quality",
                    "skip_message": str(
                        inspected.get("quality_message")
                        or "Subtitle quality checks suggest rejection."
                    ),
                }
            )
            _record_quarantine_issue(
                db_path=db_path,
                tried_entry=tried_entry,
                reason="bad_quality",
                quality=inspected,
            )
            tried_candidates.append(tried_entry)
            if best_bad_item is None:
                best_bad_item = item
                best_bad_index = index
                best_bad_quality = inspected
                best_bad_reason = tried_entry["skip_message"]
            continue

        tried_entry.update(
            {
                "status": "selected",
                "skip_reason": None,
                "skip_message": None,
            }
        )
        tried_candidates.append(tried_entry)
        fallback_reason = None
        if index > 0:
            fallback_reason = _build_fallback_reason(tried_candidates[:-1])
        fallback_reason = _merge_reason_text(
            fallback_reason,
            quarantine_context.get("quarantine_note"),
        )
        return {
            "selected_item": item,
            "selected_index": index,
            "selected_quality": inspected,
            "tried_candidates": tried_candidates,
            "fallback_reason": fallback_reason,
            "selection_warning": None,
            "remaining_ranked_items": ranked_items[index:],
            "selected_quarantine": tried_entry.get("quarantine") or item.get("quarantine"),
            "quarantine_affected_selection": bool(quarantine_context.get("ranking_changed")),
            "quarantine_note": quarantine_context.get("quarantine_note"),
            "selected_import_history": tried_entry.get("import_history") or item.get("import_history"),
            "reused_existing_record": False,
            "import_history_note": None,
        }

    if best_bad_item is not None and best_bad_quality is not None:
        selected_rank = int(best_bad_index or 0) + 1
        selected_entry: Optional[Dict[str, Any]] = None
        for entry in tried_candidates:
            if int(entry.get("rank") or 0) == selected_rank:
                entry["status"] = "selected_with_warning"
                entry["skip_reason"] = None
                entry["skip_message"] = None
                selected_entry = entry
                break
        fallback_reason = (
            "All ranked candidates within the fallback limit were skipped or flagged for bad quality. "
            "Using the highest-ranked downloadable subtitle with warnings."
        )
        fallback_reason = _merge_reason_text(
            fallback_reason,
            quarantine_context.get("quarantine_note"),
        )
        return {
            "selected_item": best_bad_item,
            "selected_index": best_bad_index,
            "selected_quality": best_bad_quality,
            "tried_candidates": tried_candidates,
            "fallback_reason": fallback_reason,
            "selection_warning": best_bad_reason,
            "remaining_ranked_items": ranked_items[int(best_bad_index or 0):],
            "selected_quarantine": (selected_entry or {}).get("quarantine") or best_bad_item.get("quarantine"),
            "quarantine_affected_selection": bool(quarantine_context.get("ranking_changed")),
            "quarantine_note": quarantine_context.get("quarantine_note"),
            "selected_import_history": (selected_entry or {}).get("import_history") or best_bad_item.get("import_history"),
            "reused_existing_record": False,
            "import_history_note": None,
        }

    return {
        "selected_item": None,
        "selected_index": None,
        "selected_quality": None,
        "tried_candidates": tried_candidates,
        "fallback_reason": _merge_reason_text(
            _build_fallback_reason(tried_candidates),
            quarantine_context.get("quarantine_note"),
        ),
        "selection_warning": None,
        "remaining_ranked_items": [],
        "selected_quarantine": None,
        "quarantine_affected_selection": bool(quarantine_context.get("ranking_changed")),
        "quarantine_note": quarantine_context.get("quarantine_note"),
        "selected_import_history": None,
        "reused_existing_record": False,
        "import_history_note": None,
    }


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


def describe_selected_item(items: List[Dict[str, Any]], *, quarantine_note: Optional[str] = None) -> str:
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
    if quarantine_note:
        message += " " + str(quarantine_note)
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


def _build_tried_candidate_entry(item: Dict[str, Any], *, rank: int) -> Dict[str, Any]:
    return {
        "rank": rank,
        "original_rank": int(item.get("original_rank") or rank),
        "provider": _normalize_provider(item.get("provider")),
        "subtitle_id": _normalize_identifier(item.get("subtitle_id")),
        "download_url": str(item.get("download_url") or "").strip(),
        "release_name": str(item.get("release_name") or "").strip() or None,
        "score": item.get("score"),
        "match_confidence": str(item.get("match_confidence") or "").strip() or None,
        "status": "pending",
        "skip_reason": None,
        "skip_message": None,
        "quality_score": None,
        "quality_level": None,
        "quality_warnings": [],
        "reject_hint": False,
        "provider_error": None,
        "quarantine": dict(item.get("quarantine") or {}),
        "import_history": dict(item.get("import_history") or {}),
    }


def _quality_summary_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "quality_score": payload.get("quality_score"),
        "quality_level": payload.get("quality_level"),
        "quality_warnings": list(payload.get("quality_warnings") or []),
        "reject_hint": bool(payload.get("reject_hint")),
    }


def _quality_summary_from_import_history(history: Dict[str, Any]) -> Dict[str, Any]:
    quality_score = history.get("quality_score")
    quality_level = str(history.get("quality_level") or "").strip() or None
    if quality_score is None and not quality_level:
        return {}
    return {
        "quality_score": quality_score,
        "quality_level": quality_level,
        "quality_warnings": [],
        "reject_hint": quality_level == "bad",
    }


def _build_fallback_reason(tried_candidates: List[Dict[str, Any]]) -> Optional[str]:
    skipped = [item for item in tried_candidates if str(item.get("status") or "") == "skipped"]
    if not skipped:
        return None
    reasons = []
    for item in skipped[:3]:
        provider_name = str(item.get("provider") or "provider").strip() or "provider"
        rank = item.get("rank")
        reason = str(item.get("skip_reason") or "skipped").replace("_", " ")
        reasons.append("#{0} from {1}: {2}".format(rank, provider_name, reason))
    suffix = " Tried next ranked candidate." if len(skipped) == 1 else " Tried the next ranked candidates."
    return "Higher-ranked candidates were skipped (" + "; ".join(reasons) + ")." + suffix


def _annotate_candidates_with_quarantine(
    items: List[Dict[str, Any]],
    *,
    db_path: Optional[str],
    legacy_video_id: Optional[str],
    canonical_video_key: Optional[str],
    season: Optional[int],
    episode: Optional[int],
) -> Dict[str, Any]:
    annotated: List[Dict[str, Any]] = []
    for index, raw_item in enumerate(list(items or [])):
        item = dict(raw_item)
        item["original_rank"] = index + 1
        quarantine = (
            provider_quarantine.get_candidate_summary(
                db_path,
                provider=item.get("provider"),
                subtitle_id=item.get("subtitle_id"),
                download_url=item.get("download_url"),
                release_name=item.get("release_name"),
            )
            if db_path
            else {
                "provider": _normalize_provider(item.get("provider")) or None,
                "subtitle_id": _normalize_identifier(item.get("subtitle_id")) or None,
                "release_name": str(item.get("release_name") or "").strip() or None,
                "download_url_hash": "",
                "is_quarantined": False,
                "fail_count": 0,
                "reason_count": 0,
                "reasons": [],
                "first_seen": None,
                "last_seen": None,
                "last_quality_level": None,
                "last_quality_warnings": [],
                "penalty": 0.0,
            }
        )
        video_identity = _video_identity(canonical_video_key=canonical_video_key, legacy_video_id=legacy_video_id)
        item["import_history"] = (
            provider_import_history.get_candidate_summary(
                db_path,
                provider=item.get("provider"),
                subtitle_id=item.get("subtitle_id"),
                download_url=item.get("download_url"),
                release_name=item.get("release_name"),
                video_identity=video_identity,
                season=season,
                episode=episode,
                legacy_video_id=legacy_video_id,
                canonical_video_key=canonical_video_key,
            )
            if db_path and video_identity
            else {
                "provider": _normalize_provider(item.get("provider")) or None,
                "subtitle_id": _normalize_identifier(item.get("subtitle_id")) or None,
                "release_name": str(item.get("release_name") or "").strip() or None,
                "video_identity": video_identity or None,
                "season": season,
                "episode": episode,
                "download_url_hash": "",
                "is_previously_imported": False,
                "record_id": None,
                "record_status": None,
                "import_count": 0,
                "first_imported_at": None,
                "last_imported_at": None,
                "quality_level": None,
                "quality_score": None,
            }
        )
        item["quarantine"] = quarantine
        item["quarantine_penalty"] = float(quarantine.get("penalty") or 0.0)
        item["adjusted_score"] = float(item.get("score", 0.0) or 0.0) - float(item["quarantine_penalty"])
        annotated.append(item)

    ordered = sorted(
        annotated,
        key=lambda item: (
            -float(item.get("adjusted_score", 0.0) or 0.0),
            -float(item.get("score", 0.0) or 0.0),
            int(item.get("original_rank") or 0),
        ),
    )
    ranking_changed = any(
        int(item.get("original_rank") or 0) != index + 1
        for index, item in enumerate(ordered)
    )
    quarantined_moved = [
        item for index, item in enumerate(ordered)
        if int(item.get("original_rank") or 0) != index + 1
        and float(item.get("quarantine_penalty", 0.0) or 0.0) > 0
    ]
    quarantine_note = None
    if ranking_changed and quarantined_moved:
        quarantine_note = (
            "Quarantine memory deprioritized repeatedly bad candidate(s) before import."
        )
    return {
        "ordered_items": ordered,
        "ranking_changed": ranking_changed,
        "quarantine_note": quarantine_note,
    }


def _record_quarantine_issue(
    *,
    db_path: Optional[str],
    tried_entry: Dict[str, Any],
    reason: str,
    quality: Optional[Dict[str, Any]],
) -> None:
    if not db_path:
        return
    quarantine = provider_quarantine.record_candidate_issue(
        db_path,
        provider=tried_entry.get("provider"),
        subtitle_id=tried_entry.get("subtitle_id"),
        download_url=tried_entry.get("download_url"),
        release_name=tried_entry.get("release_name"),
        reason=reason,
        quality_level=(quality or {}).get("quality_level"),
        quality_warnings=list((quality or {}).get("quality_warnings") or []),
    )
    tried_entry["quarantine"] = quarantine


def _merge_reason_text(primary: Optional[str], secondary: Optional[str]) -> Optional[str]:
    left = str(primary or "").strip()
    right = str(secondary or "").strip()
    if left and right:
        return left + " " + right
    return left or right or None


def _video_identity(*, canonical_video_key: Optional[str], legacy_video_id: Optional[str]) -> str:
    return str(canonical_video_key or legacy_video_id or "").strip()
