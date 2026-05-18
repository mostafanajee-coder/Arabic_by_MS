"""Local cache integrity verification for reusable provider-imported subtitles."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from services import cache_db, provider_import_history
from utils.srt_validator import SRTValidationError, validate_srt_content

PathLike = Union[str, Path]

STATUS_VALID = "valid"
STATUS_MISSING_FILE = "missing_file"
STATUS_INVALID_SRT = "invalid_srt"
STATUS_UNREADABLE_FILE = "unreadable_file"
STATUS_STALE_RECORD = "stale_record"

VALID_STATUSES = {
    STATUS_VALID,
    STATUS_MISSING_FILE,
    STATUS_INVALID_SRT,
    STATUS_UNREADABLE_FILE,
    STATUS_STALE_RECORD,
}

_RECORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_integrity_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL UNIQUE,
    provider TEXT,
    release_name TEXT,
    video_identity TEXT,
    integrity_status TEXT NOT NULL,
    integrity_warnings TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT NOT NULL,
    quality_level TEXT,
    quality_score INTEGER,
    quality_acceptable INTEGER NOT NULL DEFAULT 0
);
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_integrity_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create or migrate cache integrity tables."""
    cache_db.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_RECORDS_SCHEMA)
        conn.executescript(_META_SCHEMA)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cache_integrity_records)").fetchall()
        }
        if "provider" not in columns:
            conn.execute("ALTER TABLE cache_integrity_records ADD COLUMN provider TEXT")
        if "release_name" not in columns:
            conn.execute("ALTER TABLE cache_integrity_records ADD COLUMN release_name TEXT")
        if "video_identity" not in columns:
            conn.execute("ALTER TABLE cache_integrity_records ADD COLUMN video_identity TEXT")
        if "quality_level" not in columns:
            conn.execute("ALTER TABLE cache_integrity_records ADD COLUMN quality_level TEXT")
        if "quality_score" not in columns:
            conn.execute("ALTER TABLE cache_integrity_records ADD COLUMN quality_score INTEGER")
        if "quality_acceptable" not in columns:
            conn.execute(
                "ALTER TABLE cache_integrity_records ADD COLUMN quality_acceptable INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cache_integrity_record_id ON cache_integrity_records(record_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_integrity_status ON cache_integrity_records(integrity_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_integrity_checked_at ON cache_integrity_records(checked_at DESC)"
        )
        conn.commit()


def verify_import_history_summary(
    db_path: PathLike,
    summary: Optional[Dict[str, Any]],
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    """Verify one previously imported subtitle record referenced by import history."""
    item = dict(summary or {})
    record_id = int(item.get("record_id") or 0)
    return verify_record(
        db_path,
        record_id=record_id,
        provider=item.get("provider"),
        release_name=item.get("release_name"),
        video_identity=item.get("video_identity"),
        quality_level=item.get("quality_level"),
        quality_score=item.get("quality_score"),
        persist=persist,
    )


def verify_record(
    db_path: PathLike,
    *,
    record_id: int,
    provider: Optional[str] = None,
    release_name: Optional[str] = None,
    video_identity: Optional[str] = None,
    quality_level: Optional[str] = None,
    quality_score: Optional[int] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Verify one cached English subtitle record before local reuse."""
    init_db(db_path)
    checked_at = _utcnow_iso()
    normalized_quality_level = _normalize_text(quality_level)
    quality_acceptable = normalized_quality_level in {"good", "warning"}
    warnings: List[str] = []
    status = STATUS_VALID

    record = cache_db.get_record(db_path, int(record_id or 0))
    if not record:
        status = STATUS_STALE_RECORD
        warnings.append("Cached subtitle record is missing.")
    else:
        english_path = _normalize_text(record.get("english_srt_path"))
        if not english_path:
            status = STATUS_MISSING_FILE
            warnings.append("Cached English subtitle file path is missing.")
        else:
            target = Path(english_path)
            if not target.exists() or not target.is_file():
                status = STATUS_MISSING_FILE
                warnings.append("Cached English subtitle file is missing.")
            else:
                try:
                    raw = target.read_bytes()
                except OSError:
                    status = STATUS_UNREADABLE_FILE
                    warnings.append("Cached English subtitle file is unreadable.")
                else:
                    try:
                        validate_srt_content(raw)
                    except SRTValidationError as exc:
                        status = STATUS_INVALID_SRT
                        warnings.append(str(exc))

    if status == STATUS_VALID:
        if normalized_quality_level is None:
            status = STATUS_STALE_RECORD
            warnings.append("Cached subtitle quality metadata is missing.")
        elif not quality_acceptable:
            warnings.append(
                "Cached subtitle quality metadata is not eligible for immediate local-first reuse."
            )

    payload = {
        "record_id": int(record_id or 0) or None,
        "provider": _normalize_text(provider),
        "release_name": _normalize_text(release_name),
        "video_identity": _normalize_text(video_identity),
        "integrity_status": status if status in VALID_STATUSES else STATUS_STALE_RECORD,
        "integrity_warnings": warnings,
        "checked_at": checked_at,
        "quality_level": normalized_quality_level,
        "quality_score": quality_score,
        "quality_acceptable": bool(quality_acceptable),
    }
    if persist and payload["record_id"]:
        _save_record(db_path, payload)
    return payload


def get_summary(db_path: PathLike, *, limit: int = 25) -> Dict[str, Any]:
    """Return safe cache integrity counts and latest checked rows."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM cache_integrity_records
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit or 25)),),
        ).fetchall()
        counts = {
            STATUS_VALID: 0,
            STATUS_MISSING_FILE: 0,
            STATUS_INVALID_SRT: 0,
            STATUS_UNREADABLE_FILE: 0,
            STATUS_STALE_RECORD: 0,
        }
        count_rows = conn.execute(
            """
            SELECT integrity_status, COUNT(*) AS count
            FROM cache_integrity_records
            GROUP BY integrity_status
            """
        ).fetchall()
    for row in count_rows:
        status = str(row["integrity_status"] or "").strip()
        if status in counts:
            counts[status] = int(row["count"] or 0)
    return {
        "counts": counts,
        "last_scan_at": _get_meta(db_path, "last_scan_at"),
        "last_repair_metadata_at": _get_meta(db_path, "last_repair_metadata_at"),
        "items": [_row_to_payload(dict(row)) for row in rows],
    }


def scan_records(db_path: PathLike, *, repair_metadata: bool = False) -> Dict[str, Any]:
    """Scan all imported provider records and persist fresh integrity metadata."""
    init_db(db_path)
    entries = provider_import_history.list_entries(db_path, limit=None)
    latest_by_record: Dict[int, Dict[str, Any]] = {}
    for entry in entries:
        record_id = int(entry.get("record_id") or 0)
        if record_id <= 0 or record_id in latest_by_record:
            continue
        latest_by_record[record_id] = dict(entry)

    for entry in latest_by_record.values():
        verify_import_history_summary(db_path, entry, persist=True)

    now = _utcnow_iso()
    _set_meta(db_path, "last_scan_at", now)
    if repair_metadata:
        _set_meta(db_path, "last_repair_metadata_at", now)

    payload = get_summary(db_path)
    payload["scanned_records"] = len(latest_by_record)
    payload["status"] = "repaired" if repair_metadata else "scanned"
    return payload


def _connect(db_path: PathLike) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _save_record(db_path: PathLike, payload: Dict[str, Any]) -> None:
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM cache_integrity_records WHERE record_id = ?",
            (int(payload["record_id"]),),
        ).fetchone()
        values = (
            _normalize_text(payload.get("provider")),
            _normalize_text(payload.get("release_name")),
            _normalize_text(payload.get("video_identity")),
            str(payload.get("integrity_status") or STATUS_STALE_RECORD),
            json.dumps(list(payload.get("integrity_warnings") or [])),
            str(payload.get("checked_at") or _utcnow_iso()),
            _normalize_text(payload.get("quality_level")),
            payload.get("quality_score"),
            int(bool(payload.get("quality_acceptable"))),
        )
        if existing:
            conn.execute(
                """
                UPDATE cache_integrity_records
                SET provider = ?, release_name = ?, video_identity = ?,
                    integrity_status = ?, integrity_warnings = ?, checked_at = ?,
                    quality_level = ?, quality_score = ?, quality_acceptable = ?
                WHERE record_id = ?
                """,
                (*values, int(payload["record_id"])),
            )
        else:
            conn.execute(
                """
                INSERT INTO cache_integrity_records (
                    record_id, provider, release_name, video_identity,
                    integrity_status, integrity_warnings, checked_at,
                    quality_level, quality_score, quality_acceptable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(payload["record_id"]),
                    *values,
                ),
            )
        conn.commit()


def _row_to_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "record_id": int(row.get("record_id") or 0) or None,
        "provider": _normalize_text(row.get("provider")),
        "release_name": _normalize_text(row.get("release_name")),
        "video_identity": _normalize_text(row.get("video_identity")),
        "integrity_status": str(row.get("integrity_status") or STATUS_STALE_RECORD),
        "integrity_warnings": _decode_warnings(row.get("integrity_warnings")),
        "checked_at": _normalize_text(row.get("checked_at")),
        "quality_level": _normalize_text(row.get("quality_level")),
        "quality_score": row.get("quality_score"),
        "quality_acceptable": bool(row.get("quality_acceptable")),
    }


def _set_meta(db_path: PathLike, key: str, value: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cache_integrity_meta(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(key), str(value)),
        )
        conn.commit()


def _get_meta(db_path: PathLike, key: str) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM cache_integrity_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
    if not row:
        return None
    return _normalize_text(row["value"])


def _decode_warnings(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _normalize_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
