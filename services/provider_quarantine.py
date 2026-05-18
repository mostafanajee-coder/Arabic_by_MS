"""Local provider candidate quarantine memory."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from services import cache_db
from utils.hash_utils import sha256_text

PathLike = Union[str, Path]

QUARANTINE_THRESHOLD = 2
_VALID_REASONS = {
    "invalid_srt",
    "bad_quality",
    "missing_url",
    "provider_error",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    subtitle_id TEXT NOT NULL DEFAULT '',
    download_url_hash TEXT NOT NULL DEFAULT '',
    release_name TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_quality_level TEXT,
    last_quality_warnings TEXT NOT NULL DEFAULT '[]'
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create or migrate the provider quarantine table."""
    cache_db.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(provider_quarantine)").fetchall()
        }
        if "last_quality_level" not in columns:
            conn.execute("ALTER TABLE provider_quarantine ADD COLUMN last_quality_level TEXT")
        if "last_quality_warnings" not in columns:
            conn.execute(
                "ALTER TABLE provider_quarantine ADD COLUMN last_quality_warnings TEXT NOT NULL DEFAULT '[]'"
            )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_quarantine_identity
            ON provider_quarantine(provider, subtitle_id, download_url_hash, release_name, reason)
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_quarantine_last_seen ON provider_quarantine(last_seen DESC)"
        )
        conn.commit()


def record_candidate_issue(
    db_path: PathLike,
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
    reason: str,
    quality_level: Optional[str] = None,
    quality_warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Store one quarantine failure signal and return the aggregated candidate summary."""
    normalized_reason = _normalize_reason(reason)
    candidate_key = _candidate_key(
        provider=provider,
        subtitle_id=subtitle_id,
        download_url=download_url,
        release_name=release_name,
    )
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, fail_count, first_seen
            FROM provider_quarantine
            WHERE provider = ?
              AND subtitle_id = ?
              AND download_url_hash = ?
              AND release_name = ?
              AND reason = ?
            """,
            (
                candidate_key["provider"],
                candidate_key["subtitle_id"],
                candidate_key["download_url_hash"],
                candidate_key["release_name"],
                normalized_reason,
            ),
        ).fetchone()
        warnings_json = json.dumps([str(item) for item in list(quality_warnings or [])])
        if row:
            conn.execute(
                """
                UPDATE provider_quarantine
                SET last_seen = ?,
                    fail_count = ?,
                    last_quality_level = ?,
                    last_quality_warnings = ?
                WHERE id = ?
                """,
                (
                    now,
                    int(row["fail_count"] or 0) + 1,
                    _normalize_text(quality_level),
                    warnings_json,
                    int(row["id"]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO provider_quarantine (
                    provider, subtitle_id, download_url_hash, release_name, reason,
                    first_seen, last_seen, fail_count, last_quality_level, last_quality_warnings
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_key["provider"],
                    candidate_key["subtitle_id"],
                    candidate_key["download_url_hash"],
                    candidate_key["release_name"],
                    normalized_reason,
                    now,
                    now,
                    1,
                    _normalize_text(quality_level),
                    warnings_json,
                ),
            )
        conn.commit()
    return get_candidate_summary(
        db_path,
        provider=provider,
        subtitle_id=subtitle_id,
        download_url=download_url,
        release_name=release_name,
    )


def get_candidate_summary(
    db_path: PathLike,
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
) -> Dict[str, Any]:
    """Return aggregated quarantine state for one candidate."""
    candidate_key = _candidate_key(
        provider=provider,
        subtitle_id=subtitle_id,
        download_url=download_url,
        release_name=release_name,
    )
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM provider_quarantine
            WHERE provider = ?
              AND subtitle_id = ?
              AND download_url_hash = ?
              AND release_name = ?
            ORDER BY last_seen DESC, fail_count DESC, id DESC
            """,
            (
                candidate_key["provider"],
                candidate_key["subtitle_id"],
                candidate_key["download_url_hash"],
                candidate_key["release_name"],
            ),
        ).fetchall()
    return _summarize_rows(candidate_key, [dict(row) for row in rows])


def list_entries(db_path: PathLike, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent quarantine entries, newest first."""
    normalized_limit = max(1, int(limit or 50))
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM provider_quarantine
            ORDER BY last_seen DESC, fail_count DESC, id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["provider"] = _normalize_text(item.get("provider"))
        item["subtitle_id"] = _normalize_text(item.get("subtitle_id"))
        item["release_name"] = _normalize_text(item.get("release_name"))
        item["reason"] = _normalize_reason(item.get("reason"))
        item["fail_count"] = int(item.get("fail_count") or 0)
        item["last_quality_level"] = _normalize_text(item.get("last_quality_level"))
        item["last_quality_warnings"] = _decode_warnings(item.get("last_quality_warnings"))
        items.append(item)
    return items


def clear_entries(db_path: PathLike) -> int:
    """Delete all quarantine entries and return how many rows were removed."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM provider_quarantine").fetchone()
        count = int(row[0] or 0)
        conn.execute("DELETE FROM provider_quarantine")
        conn.commit()
    return count


def _connect(db_path: PathLike) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _candidate_key(
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
) -> Dict[str, str]:
    normalized_url = str(download_url or "").strip()
    return {
        "provider": (_normalize_text(provider) or "").lower(),
        "subtitle_id": (_normalize_text(subtitle_id) or "").lower(),
        "download_url_hash": sha256_text(normalized_url),
        "release_name": _normalize_text(release_name) or "",
    }


def _summarize_rows(candidate_key: Dict[str, str], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {
            "provider": candidate_key["provider"] or None,
            "subtitle_id": candidate_key["subtitle_id"] or None,
            "release_name": candidate_key["release_name"] or None,
            "download_url_hash": candidate_key["download_url_hash"],
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

    ordered = sorted(
        rows,
        key=lambda item: (
            -int(item.get("fail_count") or 0),
            str(item.get("last_seen") or ""),
        ),
    )
    latest = sorted(rows, key=lambda item: str(item.get("last_seen") or ""), reverse=True)[0]
    total_fail_count = sum(int(item.get("fail_count") or 0) for item in rows)
    penalty = _quarantine_penalty(total_fail_count)
    return {
        "provider": candidate_key["provider"] or None,
        "subtitle_id": candidate_key["subtitle_id"] or None,
        "release_name": candidate_key["release_name"] or None,
        "download_url_hash": candidate_key["download_url_hash"],
        "is_quarantined": total_fail_count >= QUARANTINE_THRESHOLD,
        "fail_count": total_fail_count,
        "reason_count": len(rows),
        "reasons": [
            {
                "reason": _normalize_reason(item.get("reason")),
                "fail_count": int(item.get("fail_count") or 0),
                "last_seen": item.get("last_seen"),
            }
            for item in ordered
        ],
        "first_seen": min(str(item.get("first_seen") or "") for item in rows) or None,
        "last_seen": latest.get("last_seen"),
        "last_quality_level": _normalize_text(latest.get("last_quality_level")),
        "last_quality_warnings": _decode_warnings(latest.get("last_quality_warnings")),
        "penalty": penalty,
    }


def _quarantine_penalty(fail_count: int) -> float:
    if int(fail_count or 0) < QUARANTINE_THRESHOLD:
        return 0.0
    return min(10.0, 4.0 + max(0, int(fail_count or 0) - QUARANTINE_THRESHOLD) * 2.0)


def _normalize_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _normalize_reason(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in _VALID_REASONS:
        return "provider_error" if text else "provider_error"
    return text


def _decode_warnings(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]
