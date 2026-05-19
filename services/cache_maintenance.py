"""Safe local cache maintenance scanning and orphan cleanup planning."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from backend import config
from services import batch_prepare_service, cache_db, cache_integrity, job_manager

PathLike = Union[str, Path]

CLEANUP_ACTION_KEEP = "keep"
CLEANUP_ACTION_DELETE_CANDIDATE = "delete_candidate"
CLEANUP_ACTION_METADATA_ONLY = "metadata_only"

_FAILED_RECORD_MAX_AGE = timedelta(hours=24)

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_maintenance_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create metadata storage for cache maintenance summaries."""
    cache_db.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_META_SCHEMA)
        conn.commit()


def get_summary(db_path: PathLike) -> Dict[str, Any]:
    """Return the latest stored safe cache maintenance summary."""
    init_db(db_path)
    raw = _get_meta(db_path, "last_scan_summary")
    if not raw:
        return _default_summary()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _default_summary()
    if not isinstance(parsed, dict):
        return _default_summary()
    return _coerce_summary(parsed)


def scan_cache(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
) -> Dict[str, Any]:
    """Scan cache directories and DB references without deleting anything."""
    init_db(db_path)
    scan_started_at = _utcnow_iso()
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    english_root.mkdir(parents=True, exist_ok=True)
    arabic_root.mkdir(parents=True, exist_ok=True)

    integrity_summary = cache_integrity.scan_records(db_path, repair_metadata=False)
    integrity_items = list(integrity_summary.get("items") or [])

    records = list(cache_db.list_subtitles(db_path))
    active_record_ids = _active_record_ids(db_path)
    referenced_english = _referenced_paths(records, "english_srt_path")
    referenced_arabic = _referenced_paths(records, "arabic_srt_path")
    all_files = _scan_files(english_root, arabic_root)
    all_file_paths = {entry["path"] for entry in all_files}

    orphan_files: List[Dict[str, Any]] = []
    missing_references: List[Dict[str, Any]] = []
    stale_records: List[Dict[str, Any]] = []
    invalid_integrity_records: List[Dict[str, Any]] = []
    cleanup_candidates: List[Dict[str, Any]] = []
    protected_files: List[Dict[str, Any]] = []

    cleanup_keys: Set[Tuple[str, str, Optional[int]]] = set()

    for file_info in all_files:
        path_text = file_info["path"]
        language = str(file_info["language"])
        managed_reason = _project_managed_asset_reason(Path(path_text))
        if managed_reason:
            protected_files.append(
                _file_entry(
                    path_text,
                    reason=managed_reason,
                    size_bytes=file_info["size_bytes"],
                    protected=True,
                    language=language,
                )
            )
            continue
        referenced_set = referenced_english if language == "english" else referenced_arabic
        if path_text in referenced_set:
            protected_files.append(
                _file_entry(
                    path_text,
                    reason="Referenced by subtitle metadata.",
                    size_bytes=file_info["size_bytes"],
                    protected=True,
                    language=language,
                )
            )
            continue
        orphan = _file_entry(
            path_text,
            reason="File is under the cache directory but is not referenced by the DB.",
            size_bytes=file_info["size_bytes"],
            protected=False,
            language=language,
        )
        orphan_files.append(orphan)
        _append_cleanup_candidate(
            cleanup_candidates,
            cleanup_keys,
            candidate_type="orphan_file",
            path=path_text,
            reason="Unreferenced cache file.",
            size_bytes=file_info["size_bytes"],
            protected=False,
            action=CLEANUP_ACTION_DELETE_CANDIDATE,
            language=language,
            record_id=None,
        )

    records_by_id = {
        int(record.get("id") or 0): record
        for record in records
        if int(record.get("id") or 0) > 0
    }
    for item in integrity_items:
        normalized = _sanitize_integrity_item(
            item,
            records_by_id=records_by_id,
            active_record_ids=active_record_ids,
            english_root=english_root,
        )
        status = normalized["integrity_status"]
        if status == cache_integrity.STATUS_INVALID_SRT:
            invalid_integrity_records.append(normalized)
            _append_cleanup_candidate(
                cleanup_candidates,
                cleanup_keys,
                candidate_type="invalid_integrity_record",
                path=normalized.get("path"),
                reason="Cached subtitle integrity is invalid and should be reviewed in metadata.",
                size_bytes=normalized.get("size_bytes"),
                protected=bool(normalized.get("protected")),
                action=CLEANUP_ACTION_METADATA_ONLY,
                language="english",
                record_id=normalized.get("record_id"),
            )
        elif status in (
            cache_integrity.STATUS_MISSING_FILE,
            cache_integrity.STATUS_STALE_RECORD,
            cache_integrity.STATUS_UNREADABLE_FILE,
        ):
            stale_records.append(normalized)
            _append_cleanup_candidate(
                cleanup_candidates,
                cleanup_keys,
                candidate_type="stale_record",
                path=normalized.get("path"),
                reason="Cached subtitle integrity is stale and should be reviewed in metadata.",
                size_bytes=normalized.get("size_bytes"),
                protected=bool(normalized.get("protected")),
                action=CLEANUP_ACTION_METADATA_ONLY,
                language="english",
                record_id=normalized.get("record_id"),
            )

    for record in records:
        record_id = int(record.get("id") or 0)
        english_path = _normalize_path_text(record.get("english_srt_path"))
        arabic_path = _normalize_path_text(record.get("arabic_srt_path"))
        translated_or_preferred = _record_is_translated_or_preferred(record)
        active = record_id in active_record_ids

        for language, path_text, root in (
            ("english", english_path, english_root),
            ("arabic", arabic_path, arabic_root),
        ):
            if not path_text:
                continue
            path_obj = Path(path_text)
            exists = path_text in all_file_paths if _is_path_within_root(path_obj, root) else path_obj.exists()
            size_bytes = _safe_file_size(path_obj)
            within_root = _is_path_within_root(path_obj, root)
            protection_reason = None
            if translated_or_preferred:
                protection_reason = "Referenced by a translated or preferred record."
            elif active:
                protection_reason = "Referenced by an active translation or batch job."
            elif not within_root:
                protection_reason = "Referenced path is outside the configured cache directory."
            elif exists:
                protection_reason = "Referenced by subtitle metadata."

            if protection_reason:
                protected_files.append(
                    _file_entry(
                        path_text,
                        reason=protection_reason,
                        size_bytes=size_bytes,
                        protected=True,
                        language=language,
                        record_id=record_id,
                    )
                )

            if exists:
                continue

            missing_reason = (
                "DB record points outside the configured cache directory."
                if not within_root
                else "DB record points to a missing cached subtitle file."
            )
            missing_references.append(
                {
                    "record_id": record_id,
                    "language": language,
                    "path": path_text,
                    "reason": missing_reason,
                    "protected": bool(
                        translated_or_preferred or active or not within_root
                    ),
                }
            )
            _append_cleanup_candidate(
                cleanup_candidates,
                cleanup_keys,
                candidate_type="missing_reference",
                path=path_text,
                reason=missing_reason,
                size_bytes=size_bytes,
                protected=bool(translated_or_preferred or active or not within_root),
                action=CLEANUP_ACTION_METADATA_ONLY,
                language=language,
                record_id=record_id,
            )

        if record.get("status") == "failed" and _record_is_old_failed(record):
            protected = translated_or_preferred or active
            _append_cleanup_candidate(
                cleanup_candidates,
                cleanup_keys,
                candidate_type="old_failed_record",
                path=english_path or arabic_path,
                reason="Old failed subtitle record should be reviewed in metadata.",
                size_bytes=_safe_file_size(Path(english_path)) if english_path else None,
                protected=protected,
                action=CLEANUP_ACTION_METADATA_ONLY,
                language="english" if english_path else "arabic",
                record_id=record_id,
            )

    cleanup_candidates.extend(_duplicate_record_candidates(records, active_record_ids))
    cleanup_candidates.sort(
        key=lambda item: (
            0 if str(item.get("action") or "") == CLEANUP_ACTION_DELETE_CANDIDATE else 1,
            0 if not bool(item.get("protected")) else 1,
            str(item.get("path") or ""),
            int(item.get("record_id") or 0),
        )
    )

    summary = _coerce_summary(
        {
            "total_files": len(all_files),
            "total_bytes": sum(int(item["size_bytes"] or 0) for item in all_files),
            "orphan_files": orphan_files,
            "missing_references": missing_references,
            "stale_records": stale_records,
            "invalid_integrity_records": invalid_integrity_records,
            "cleanup_candidates": cleanup_candidates,
            "protected_files": _dedupe_entries(protected_files),
            "scan_started_at": scan_started_at,
            "scan_finished_at": _utcnow_iso(),
            "integrity_last_scan_at": integrity_summary.get("last_scan_at"),
        }
    )
    _set_meta(db_path, "last_scan_summary", json.dumps(summary))
    _set_meta(db_path, "last_scan_at", summary["scan_finished_at"])
    return summary


def cleanup_cache(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    dry_run: bool = True,
    allow_delete: bool = False,
) -> Dict[str, Any]:
    """Build a cleanup plan and optionally delete safe orphan files only."""
    summary = scan_cache(
        db_path,
        english_cache_dir=english_cache_dir,
        arabic_cache_dir=arabic_cache_dir,
    )
    deleted_files: List[Dict[str, Any]] = []
    delete_errors: List[Dict[str, Any]] = []
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()

    if not dry_run and allow_delete:
        for candidate in list(summary.get("cleanup_candidates") or []):
            if str(candidate.get("candidate_type") or "") != "orphan_file":
                continue
            if bool(candidate.get("protected")):
                continue
            path_text = _normalize_path_text(candidate.get("path"))
            if not path_text:
                continue
            target = Path(path_text)
            if not (
                _is_path_within_root(target, english_root)
                or _is_path_within_root(target, arabic_root)
            ):
                continue
            if not target.exists() or not target.is_file():
                continue
            try:
                size_bytes = _safe_file_size(target)
                target.unlink()
                deleted_files.append(
                    {
                        "path": path_text,
                        "size_bytes": size_bytes,
                        "reason": str(candidate.get("reason") or "Deleted orphan cache file."),
                    }
                )
            except OSError as exc:
                delete_errors.append({"path": path_text, "error": str(exc)})

    result = {
        **summary,
        "dry_run": bool(dry_run),
        "allow_delete": bool(allow_delete),
        "deleted_files": deleted_files,
        "deleted_count": len(deleted_files),
        "deleted_bytes": sum(int(item.get("size_bytes") or 0) for item in deleted_files),
        "delete_errors": delete_errors,
        "cleanup_finished_at": _utcnow_iso(),
    }
    _set_meta(db_path, "last_cleanup_summary", json.dumps(result))
    _set_meta(db_path, "last_cleanup_at", result["cleanup_finished_at"])
    return result


def _default_summary() -> Dict[str, Any]:
    return _coerce_summary(
        {
            "total_files": 0,
            "total_bytes": 0,
            "orphan_files": [],
            "missing_references": [],
            "stale_records": [],
            "invalid_integrity_records": [],
            "cleanup_candidates": [],
            "protected_files": [],
            "scan_started_at": None,
            "scan_finished_at": None,
            "integrity_last_scan_at": None,
        }
    )


def _coerce_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "total_files": int(payload.get("total_files") or 0),
        "total_bytes": int(payload.get("total_bytes") or 0),
        "orphan_files": list(payload.get("orphan_files") or []),
        "missing_references": list(payload.get("missing_references") or []),
        "stale_records": list(payload.get("stale_records") or []),
        "invalid_integrity_records": list(payload.get("invalid_integrity_records") or []),
        "cleanup_candidates": list(payload.get("cleanup_candidates") or []),
        "protected_files": list(payload.get("protected_files") or []),
        "scan_started_at": payload.get("scan_started_at"),
        "scan_finished_at": payload.get("scan_finished_at"),
        "integrity_last_scan_at": payload.get("integrity_last_scan_at"),
    }


def _scan_files(english_root: Path, arabic_root: Path) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    for language, root in (("english", english_root), ("arabic", arabic_root)):
        for target in sorted(root.rglob("*")):
            if not target.is_file():
                continue
            files.append(
                {
                    "language": language,
                    "path": str(target.resolve()),
                    "size_bytes": _safe_file_size(target),
                }
            )
    return files


def _referenced_paths(records: Iterable[Dict[str, Any]], field: str) -> Set[str]:
    paths: Set[str] = set()
    for record in records:
        value = _normalize_path_text(record.get(field))
        if value:
            paths.add(value)
    return paths


def _sanitize_integrity_item(
    item: Dict[str, Any],
    *,
    records_by_id: Dict[int, Dict[str, Any]],
    active_record_ids: Set[int],
    english_root: Path,
) -> Dict[str, Any]:
    record_id = int(item.get("record_id") or 0) or None
    warnings = [str(entry) for entry in (item.get("integrity_warnings") or []) if str(entry).strip()]
    record = records_by_id.get(record_id or 0) if record_id else None
    path_text = _normalize_path_text((record or {}).get("english_srt_path"))
    size_bytes = _safe_file_size(Path(path_text)) if path_text else None
    protected = bool(
        record
        and (
            _record_is_translated_or_preferred(record)
            or (record_id in active_record_ids if record_id is not None else False)
            or (path_text is not None and not _is_path_within_root(Path(path_text), english_root))
        )
    )
    return {
        "record_id": record_id,
        "provider": _normalize_text(item.get("provider")),
        "release_name": _normalize_text(item.get("release_name")),
        "video_identity": _normalize_text(item.get("video_identity")),
        "integrity_status": _normalize_text(item.get("integrity_status")) or cache_integrity.STATUS_STALE_RECORD,
        "integrity_warnings": warnings,
        "checked_at": _normalize_text(item.get("checked_at")),
        "quality_level": _normalize_text(item.get("quality_level")),
        "quality_score": item.get("quality_score"),
        "quality_acceptable": bool(item.get("quality_acceptable")),
        "path": path_text,
        "size_bytes": size_bytes,
        "protected": protected,
    }


def _active_record_ids(db_path: PathLike) -> Set[int]:
    active = set(_active_batch_record_ids(db_path))
    for record in cache_db.list_subtitles(db_path):
        record_id = int(record.get("id") or 0)
        if record_id <= 0:
            continue
        if job_manager.get_running_job_for_record(record_id):
            active.add(record_id)
    return active


def _active_batch_record_ids(db_path: PathLike) -> Set[int]:
    batch_prepare_service.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT record_id
            FROM batch_prepare_items
            WHERE record_id IS NOT NULL
              AND batch_id IN (
                  SELECT batch_id
                  FROM batch_prepare_jobs
                  WHERE status IN (?, ?)
              )
            """,
            (batch_prepare_service.STATUS_QUEUED, batch_prepare_service.STATUS_RUNNING),
        ).fetchall()
    return {int(row[0]) for row in rows if row and row[0] is not None}


def _record_is_translated_or_preferred(record: Dict[str, Any]) -> bool:
    return bool(record.get("is_preferred")) or (
        str(record.get("status") or "").strip().lower() == "translated"
        and _normalize_path_text(record.get("arabic_srt_path")) is not None
    )


def _record_is_old_failed(record: Dict[str, Any]) -> bool:
    if str(record.get("status") or "").strip().lower() != "failed":
        return False
    created_at = _parse_iso_datetime(record.get("created_at"))
    if created_at is None:
        return True
    return created_at <= (_utcnow() - _FAILED_RECORD_MAX_AGE)


def _duplicate_record_candidates(
    records: List[Dict[str, Any]],
    active_record_ids: Set[int],
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str, str, str], List[Dict[str, Any]]] = {}
    for record in records:
        provider = _normalize_text(record.get("source_provider"))
        subtitle_id = _normalize_text(record.get("source_subtitle_id"))
        download_url = _normalize_text(record.get("source_download_url"))
        release_name = _normalize_text(record.get("release_name"))
        identity = _normalize_text(record.get("canonical_video_key")) or _normalize_text(record.get("video_id"))
        if not (provider and (subtitle_id or download_url) and identity):
            continue
        key = (
            provider.lower(),
            (subtitle_id or "").lower(),
            (download_url or "").lower(),
            release_name or "",
            identity,
        )
        grouped.setdefault(key, []).append(record)

    candidates: List[Dict[str, Any]] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda record: (
                1 if _record_is_translated_or_preferred(record) else 0,
                int(record.get("id") or 0),
            ),
            reverse=True,
        )
        keeper = ordered[0]
        for record in ordered[1:]:
            record_id = int(record.get("id") or 0)
            protected = _record_is_translated_or_preferred(record) or record_id in active_record_ids
            candidates.append(
                {
                    "candidate_type": "duplicate_record",
                    "record_id": record_id,
                    "path": _normalize_path_text(record.get("english_srt_path")),
                    "reason": "Older imported duplicate is safely covered by a newer exact import-history match.",
                    "size_bytes": _safe_file_size(Path(str(record.get("english_srt_path") or "")))
                    if _normalize_path_text(record.get("english_srt_path"))
                    else None,
                    "protected": protected,
                    "action": CLEANUP_ACTION_KEEP if protected else CLEANUP_ACTION_METADATA_ONLY,
                    "language": "english",
                    "covered_by_record_id": int(keeper.get("id") or 0) or None,
                }
            )
    return candidates


def _append_cleanup_candidate(
    cleanup_candidates: List[Dict[str, Any]],
    cleanup_keys: Set[Tuple[str, str, Optional[int]]],
    *,
    candidate_type: str,
    path: Optional[str],
    reason: str,
    size_bytes: Optional[int],
    protected: bool,
    action: str,
    language: Optional[str],
    record_id: Optional[int],
) -> None:
    normalized_path = _normalize_path_text(path) or ""
    key = (candidate_type, normalized_path, int(record_id or 0) or None)
    if key in cleanup_keys:
        return
    cleanup_keys.add(key)
    cleanup_candidates.append(
        {
            "candidate_type": candidate_type,
            "record_id": int(record_id or 0) or None,
            "path": normalized_path or None,
            "reason": str(reason),
            "size_bytes": int(size_bytes or 0) if size_bytes is not None else None,
            "protected": bool(protected),
            "action": str(action),
            "language": _normalize_text(language),
        }
    )


def _dedupe_entries(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[Tuple[str, Optional[int], str]] = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        key = (
            _normalize_path_text(item.get("path")) or "",
            int(item.get("record_id") or 0) or None,
            str(item.get("reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _file_entry(
    path: Optional[str],
    *,
    reason: str,
    size_bytes: Optional[int],
    protected: bool,
    language: Optional[str],
    record_id: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "record_id": int(record_id or 0) or None,
        "language": _normalize_text(language),
        "path": _normalize_path_text(path),
        "reason": str(reason),
        "size_bytes": int(size_bytes or 0) if size_bytes is not None else None,
        "protected": bool(protected),
    }


def _safe_file_size(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _project_managed_asset_reason(path: Path) -> Optional[str]:
    resolved = path.resolve(strict=False)
    sample_path = config.SAMPLE_SRT_PATH.resolve(strict=False)
    if resolved == sample_path:
        return "Bundled project cache asset."
    if path.name == ".gitkeep":
        return "Bundled cache placeholder file."
    return None


def _is_path_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = _normalize_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalize_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _normalize_path_text(value: Any) -> Optional[str]:
    text = _normalize_text(value)
    if not text:
        return None
    try:
        return str(Path(text).resolve(strict=False))
    except OSError:
        return str(Path(text))


def _set_meta(db_path: PathLike, key: str, value: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO cache_maintenance_meta(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(key), str(value)),
        )
        conn.commit()


def _get_meta(db_path: PathLike, key: str) -> Optional[str]:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM cache_maintenance_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
    if not row:
        return None
    return _normalize_text(row[0])
