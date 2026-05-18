"""One-click prepare workflow for searching, importing, and translating."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from services import cache_db, job_manager, provider_router, usage_guard
from services.gemini_service import get_status as get_gemini_status
from utils.hash_utils import sha256_text
from utils.srt_validator import SRTValidationError, validate_srt_content
from utils.stremio_id import parse_stremio_video_id

PathLike = Union[str, Path]

_LOCK = threading.Lock()
_PREPARE_JOBS: Dict[str, Dict[str, Any]] = {}
_ACTIVE_BY_CANONICAL: Dict[str, str] = {}
_THREADS: Dict[str, threading.Thread] = {}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_ready() -> bool:
    """Return whether the in-memory prepare coordinator is available."""
    return True


def get_prepare_job(canonical_video_key: str) -> Optional[Dict[str, Any]]:
    """Return the latest prepare job snapshot for one canonical video key."""
    with _LOCK:
        for job in reversed(list(_PREPARE_JOBS.values())):
            if job.get("canonical_video_key") == canonical_video_key:
                return dict(job)
    return None


def get_active_prepare_job(canonical_video_key: str) -> Optional[Dict[str, Any]]:
    """Return the queued/running prepare job for one canonical video key."""
    with _LOCK:
        job_id = _ACTIVE_BY_CANONICAL.get(canonical_video_key)
        if not job_id:
            return None
        job = _PREPARE_JOBS.get(job_id)
        if not job or job.get("status") not in ("queued", "running"):
            return None
        return dict(job)


def request_prepare(
    *,
    video_id: str,
    video_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    query: Optional[str] = None,
    release_name: Optional[str] = None,
    language: str = "en",
    force: bool = False,
    db_path: PathLike,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    run_async: bool = False,
    request_source: str = "prepare",
) -> Dict[str, Any]:
    """Prepare Arabic subtitles now or in a fire-and-forget background thread."""
    identity = parse_stremio_video_id(video_id, season=season, episode=episode)
    canonical_video_key = str(identity.get("canonical_video_key") or "").strip()
    normalized_video_id = str(video_id or "").strip()
    normalized_video_type = str(video_type or "movie").strip() or "movie"
    normalized_query = _normalize_optional_text(query)
    normalized_release_name = _normalize_optional_text(release_name)
    normalized_language = str(language or "en").strip() or "en"

    ready_record = cache_db.find_latest_arabic_for_video(
        db_path,
        normalized_video_id,
        canonical_video_key=canonical_video_key,
    )
    if ready_record and not force:
        usage_guard.record_event(
            db_path,
            event_type=usage_guard.EVENT_PREPARE_SKIPPED_ALREADY_READY,
            canonical_video_key=canonical_video_key,
            record_id=int(ready_record["id"]),
            details={"source": request_source},
        )
        return _result(
            status="already_ready",
            canonical_video_key=canonical_video_key,
            record_id=int(ready_record["id"]),
            job_id=_running_translation_job_id(
                db_path,
                normalized_video_id,
                canonical_video_key,
            ),
            provider=ready_record.get("source_provider"),
            score=None,
            message="Arabic subtitle is already ready for this exact title.",
        )

    running_translation = _running_translation_for_video(
        db_path,
        normalized_video_id,
        canonical_video_key,
    )
    if running_translation:
        usage_guard.record_event(
            db_path,
            event_type=usage_guard.EVENT_DUPLICATE_JOB_REUSED,
            canonical_video_key=canonical_video_key,
            record_id=int(running_translation["record"]["id"]),
            job_id=str(running_translation["job"]["job_id"]),
            details={"source": request_source},
        )
        return _result(
            status="already_running",
            canonical_video_key=canonical_video_key,
            record_id=int(running_translation["record"]["id"]),
            job_id=str(running_translation["job"]["job_id"]),
            provider=running_translation["record"].get("source_provider"),
            score=None,
            message="Preparation is already in progress for this exact title.",
        )

    active_prepare = get_active_prepare_job(canonical_video_key)
    if active_prepare:
        usage_guard.record_event(
            db_path,
            event_type=usage_guard.EVENT_DUPLICATE_JOB_REUSED,
            canonical_video_key=canonical_video_key,
            record_id=active_prepare.get("record_id"),
            job_id=str(active_prepare.get("job_id") or active_prepare.get("prepare_id") or ""),
            details={"source": request_source},
        )
        return _result(
            status="already_running",
            canonical_video_key=canonical_video_key,
            record_id=active_prepare.get("record_id"),
            job_id=active_prepare.get("job_id"),
            provider=active_prepare.get("provider"),
            score=active_prepare.get("score"),
            message="Preparation is already in progress for this exact title.",
        )

    provider_status = provider_router.get_provider_status()
    provider_ready = any(
        bool(provider_status.get(name, {}).get("configured"))
        for name in ("subdl", "subsource", "opensubtitles")
    )
    if not provider_ready:
        return _result(
            status="provider_missing",
            canonical_video_key=canonical_video_key,
            record_id=None,
            job_id=None,
            provider=None,
            score=None,
            message="No subtitle provider is configured. Add SubDL, SubSource, and/or OpenSubtitles first.",
        )

    gemini_status = get_gemini_status()
    if not gemini_status.get("configured"):
        return _result(
            status="gemini_missing",
            canonical_video_key=canonical_video_key,
            record_id=None,
            job_id=None,
            provider=None,
            score=None,
            message=str(gemini_status.get("message") or "Gemini is not configured."),
        )

    bypass_limits = (
        request_source == "auto_prepare"
        and usage_guard.is_allow_auto_prepare_when_limited_enabled()
    )
    if not bypass_limits:
        for limit_name in (
            usage_guard.LIMIT_PREPARE_REQUESTS,
            usage_guard.LIMIT_PROVIDER_SEARCHES,
            usage_guard.LIMIT_GEMINI_TRANSLATIONS,
        ):
            exceeded = usage_guard.check_limit(db_path, limit_name=limit_name)
            if exceeded:
                return exceeded

    usage_guard.record_event(
        db_path,
        event_type=(
            usage_guard.EVENT_AUTO_PREPARE_REQUEST
            if request_source == "auto_prepare"
            else usage_guard.EVENT_PREPARE_REQUEST
        ),
        canonical_video_key=canonical_video_key,
        details={
            "video_id": normalized_video_id,
            "video_type": normalized_video_type,
            "force": bool(force),
            "source": request_source,
        },
    )

    if run_async:
        return _start_prepare_thread(
            canonical_video_key=canonical_video_key,
            video_id=normalized_video_id,
            video_type=normalized_video_type,
            season=identity.get("season"),
            episode=identity.get("episode"),
            query=normalized_query,
            release_name=normalized_release_name,
            language=normalized_language,
            force=force,
            db_path=db_path,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
            request_source=request_source,
        )

    return _perform_prepare(
        canonical_video_key=canonical_video_key,
        video_id=normalized_video_id,
        video_type=normalized_video_type,
        season=identity.get("season"),
        episode=identity.get("episode"),
        query=normalized_query,
        release_name=normalized_release_name,
        language=normalized_language,
        force=force,
        db_path=db_path,
        english_cache_dir=english_cache_dir,
        arabic_cache_dir=arabic_cache_dir,
        request_source=request_source,
    )


def get_prepare_status(
    *,
    canonical_video_key: str,
    db_path: PathLike,
) -> Dict[str, Any]:
    """Return readiness plus latest record/job/error information."""
    active_prepare = get_active_prepare_job(canonical_video_key)
    latest_prepare = get_prepare_job(canonical_video_key)
    records = cache_db.list_records_for_video(
        db_path,
        canonical_video_key,
        canonical_video_key=canonical_video_key,
    )
    translated = cache_db.find_latest_arabic_for_video(
        db_path,
        canonical_video_key,
        canonical_video_key=canonical_video_key,
    )
    best_record = cache_db.find_best_record_for_video(
        db_path,
        canonical_video_key,
        canonical_video_key=canonical_video_key,
    )
    active_translation = _running_translation_for_video(
        db_path,
        canonical_video_key,
        canonical_video_key,
    )
    latest_failed = next(
        (record for record in records if str(record.get("status") or "").strip().lower() == "failed"),
        None,
    )
    record = translated or best_record
    return {
        "canonical_video_key": canonical_video_key,
        "arabic_ready": bool(translated),
        "record": _summarize_record(record),
        "active_job": (
            active_translation["job"]
            if active_translation
            else active_prepare
        ),
        "latest_error": (
            (latest_failed or {}).get("error_message")
            or (active_prepare or {}).get("error_message")
            or (latest_prepare or {}).get("error_message")
        ),
    }


def reset_for_tests() -> None:
    """Clear prepare state between tests."""
    with _LOCK:
        threads = list(_THREADS.values())
    for thread in threads:
        thread.join(timeout=5.0)
    with _LOCK:
        _PREPARE_JOBS.clear()
        _ACTIVE_BY_CANONICAL.clear()
        _THREADS.clear()


def _start_prepare_thread(**kwargs: Any) -> Dict[str, Any]:
    canonical_video_key = str(kwargs["canonical_video_key"])
    prepare_id = uuid.uuid4().hex
    job = {
        "prepare_id": prepare_id,
        "canonical_video_key": canonical_video_key,
        "status": "queued",
        "record_id": None,
        "job_id": None,
        "provider": None,
        "score": None,
        "message": "Preparation has started in the background.",
        "error_message": None,
        "started_at": None,
        "finished_at": None,
    }
    with _LOCK:
        existing = _ACTIVE_BY_CANONICAL.get(canonical_video_key)
        if existing and existing in _PREPARE_JOBS:
            active_job = _PREPARE_JOBS[existing]
            usage_guard.record_event(
                kwargs["db_path"],
                event_type=usage_guard.EVENT_DUPLICATE_JOB_REUSED,
                canonical_video_key=canonical_video_key,
                record_id=active_job.get("record_id"),
                job_id=str(active_job.get("job_id") or active_job.get("prepare_id") or ""),
                details={"source": kwargs.get("request_source") or "prepare"},
            )
            return _result(
                status="already_running",
                canonical_video_key=canonical_video_key,
                record_id=active_job.get("record_id"),
                job_id=active_job.get("job_id"),
                provider=active_job.get("provider"),
                score=active_job.get("score"),
                message="Preparation is already in progress for this exact title.",
            )
        _PREPARE_JOBS[prepare_id] = job
        _ACTIVE_BY_CANONICAL[canonical_video_key] = prepare_id

    def worker() -> None:
        _mark_prepare_running(prepare_id)
        try:
            result = _perform_prepare(**kwargs)
        except Exception as exc:
            _mark_prepare_failed(prepare_id, str(exc))
        else:
            _mark_prepare_completed(prepare_id, result)

    thread = threading.Thread(
        target=worker,
        name="prepare-job-{0}".format(prepare_id[:8]),
        daemon=True,
    )
    with _LOCK:
        _THREADS[prepare_id] = thread
    thread.start()
    return _result(
        status="started",
        canonical_video_key=canonical_video_key,
        record_id=None,
        job_id=None,
        provider=None,
        score=None,
        message="Preparation has started in the background.",
    )


def _perform_prepare(
    *,
    canonical_video_key: str,
    video_id: str,
    video_type: str,
    season: Optional[int],
    episode: Optional[int],
    query: Optional[str],
    release_name: Optional[str],
    language: str,
    force: bool,
    db_path: PathLike,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    request_source: str,
) -> Dict[str, Any]:
    usage_guard.record_event(
        db_path,
        event_type=usage_guard.EVENT_PROVIDER_SEARCH,
        provider="all",
        canonical_video_key=canonical_video_key,
        details={"video_id": video_id, "video_type": video_type, "source": request_source},
    )
    search_result = provider_router.search_all_subtitles(
        video_id=(parse_stremio_video_id(video_id).get("imdb_id") or video_id),
        video_type=video_type,
        season=season,
        episode=episode,
        query=query,
        language=language,
        release_name=release_name,
    )
    items = search_result.get("items") or []
    if not items:
        return _result(
            status="no_results",
            canonical_video_key=canonical_video_key,
            record_id=None,
            job_id=None,
            provider=None,
            score=None,
            message="No English subtitle results were found for this title.",
        )

    best = dict(items[0])
    download_url = str(best.get("download_url") or "").strip()
    if not download_url:
        return _result(
            status="no_results",
            canonical_video_key=canonical_video_key,
            record_id=None,
            job_id=None,
            provider=None,
            score=None,
            message="The best subtitle result did not include a usable download URL.",
        )

    raw = provider_router.download_subtitle_data(
        str(best.get("provider") or ""),
        download_url,
    )
    text = validate_srt_content(raw)
    record_id = _store_imported_record(
        db_path=db_path,
        english_cache_dir=english_cache_dir,
        video_id=video_id,
        video_type=video_type,
        season=season,
        episode=episode,
        release_name=_normalize_optional_text(best.get("release_name")) or release_name,
        text=text,
        source_provider=_normalize_optional_text(best.get("provider")),
        source_subtitle_id=_normalize_optional_text(best.get("subtitle_id")),
        source_download_url=download_url,
    )
    translation_job = job_manager.start_translation_job(
        record_id=record_id,
        force=force,
        db_path=db_path,
        arabic_cache_dir=arabic_cache_dir,
    )
    usage_guard.record_event(
        db_path,
        event_type=usage_guard.EVENT_GEMINI_TRANSLATE_BACKGROUND,
        canonical_video_key=canonical_video_key,
        record_id=record_id,
        job_id=str(translation_job.get("job_id") or ""),
        details={"force": bool(force), "source": request_source},
    )
    result = _result(
        status="started",
        canonical_video_key=canonical_video_key,
        record_id=record_id,
        job_id=translation_job.get("job_id"),
        provider=str(best.get("provider") or ""),
        score=best.get("score"),
        message="Best English subtitle imported and background Arabic translation started.",
    )
    _clear_active_prepare(canonical_video_key)
    return result


def _store_imported_record(
    *,
    db_path: PathLike,
    english_cache_dir: PathLike,
    video_id: str,
    video_type: str,
    season: Optional[int],
    episode: Optional[int],
    release_name: Optional[str],
    text: str,
    source_provider: Optional[str],
    source_subtitle_id: Optional[str],
    source_download_url: Optional[str],
) -> int:
    identity = parse_stremio_video_id(video_id, season=season, episode=episode)
    english_hash = sha256_text(text)
    out_dir = Path(english_cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "{0}_{1}.srt".format(_slug_video_id(video_id), english_hash[:12])
    target.write_text(text, encoding="utf-8")

    record_id = cache_db.insert_subtitle(
        db_path,
        video_id=str(video_id or "").strip(),
        imdb_id=_normalize_optional_text(identity.get("imdb_id")),
        season=identity.get("season"),
        episode=identity.get("episode"),
        canonical_video_key=_normalize_optional_text(identity.get("canonical_video_key")),
        video_type=str(video_type or "movie").strip() or "movie",
        release_name=release_name,
        english_srt_path=str(target),
        english_srt_hash=english_hash,
        arabic_srt_path=None,
        status="uploaded",
        source_provider=_normalize_optional_text(source_provider),
        source_subtitle_id=_normalize_optional_text(source_subtitle_id),
        source_download_url=_normalize_optional_text(source_download_url),
    )
    usage_guard.record_event(
        db_path,
        event_type=usage_guard.EVENT_PROVIDER_IMPORT,
        provider=_normalize_optional_text(source_provider),
        canonical_video_key=_normalize_optional_text(identity.get("canonical_video_key")),
        record_id=record_id,
        details={"video_id": video_id, "video_type": video_type},
    )
    return record_id


def _running_translation_for_video(
    db_path: PathLike,
    video_id: str,
    canonical_video_key: str,
) -> Optional[Dict[str, Any]]:
    records = cache_db.list_records_for_video(
        db_path,
        video_id,
        canonical_video_key=canonical_video_key,
    )
    for record in records:
        running_job = job_manager.get_running_job_for_record(int(record["id"]))
        if running_job:
            return {"record": record, "job": running_job}
    return None


def _running_translation_job_id(
    db_path: PathLike,
    video_id: str,
    canonical_video_key: str,
) -> Optional[str]:
    running = _running_translation_for_video(db_path, video_id, canonical_video_key)
    if not running:
        return None
    return str(running["job"]["job_id"])


def _result(
    *,
    status: str,
    canonical_video_key: str,
    record_id: Optional[int],
    job_id: Optional[str],
    provider: Optional[str],
    score: Any,
    message: str,
) -> Dict[str, Any]:
    return {
        "status": status,
        "canonical_video_key": canonical_video_key,
        "record_id": record_id,
        "job_id": job_id,
        "provider": provider,
        "score": score,
        "message": message,
    }


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _slug_video_id(video_id: str) -> str:
    safe = []
    for char in str(video_id or "").strip():
        safe.append(char if char.isalnum() or char in "._-" else "_")
    return "".join(safe) or "unknown"


def _summarize_record(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not record:
        return None
    return {
        "id": record.get("id"),
        "video_id": record.get("video_id"),
        "canonical_video_key": record.get("canonical_video_key"),
        "status": record.get("status"),
        "source_provider": record.get("source_provider"),
        "is_preferred": record.get("is_preferred"),
        "error_message": record.get("error_message"),
    }


def _mark_prepare_running(prepare_id: str) -> None:
    with _LOCK:
        job = _PREPARE_JOBS.get(prepare_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = _utcnow_iso()


def _mark_prepare_completed(prepare_id: str, result: Dict[str, Any]) -> None:
    with _LOCK:
        job = _PREPARE_JOBS.get(prepare_id)
        if not job:
            return
        job["status"] = "completed"
        job["finished_at"] = _utcnow_iso()
        job["record_id"] = result.get("record_id")
        job["job_id"] = result.get("job_id")
        job["provider"] = result.get("provider")
        job["score"] = result.get("score")
        job["message"] = result.get("message")
        canonical_video_key = str(job.get("canonical_video_key") or "")
        if _ACTIVE_BY_CANONICAL.get(canonical_video_key) == prepare_id:
            _ACTIVE_BY_CANONICAL.pop(canonical_video_key, None)


def _mark_prepare_failed(prepare_id: str, error_message: str) -> None:
    with _LOCK:
        job = _PREPARE_JOBS.get(prepare_id)
        if not job:
            return
        job["status"] = "failed"
        job["finished_at"] = _utcnow_iso()
        job["error_message"] = error_message
        canonical_video_key = str(job.get("canonical_video_key") or "")
        if _ACTIVE_BY_CANONICAL.get(canonical_video_key) == prepare_id:
            _ACTIVE_BY_CANONICAL.pop(canonical_video_key, None)


def _clear_active_prepare(canonical_video_key: str) -> None:
    with _LOCK:
        _ACTIVE_BY_CANONICAL.pop(canonical_video_key, None)
