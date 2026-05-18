"""Local usage guardrails for provider and Gemini quota safety."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, Path]

EVENT_PROVIDER_SEARCH = "provider_search"
EVENT_PROVIDER_IMPORT = "provider_import"
EVENT_GEMINI_TRANSLATE_SYNC = "gemini_translate_sync"
EVENT_GEMINI_TRANSLATE_BACKGROUND = "gemini_translate_background"
EVENT_PREPARE_REQUEST = "prepare_request"
EVENT_AUTO_PREPARE_REQUEST = "auto_prepare_request"
EVENT_PREPARE_SKIPPED_ALREADY_READY = "prepare_skipped_already_ready"
EVENT_DUPLICATE_JOB_REUSED = "duplicate_job_reused"

LIMIT_GEMINI_TRANSLATIONS = "MAX_DAILY_GEMINI_TRANSLATIONS"
LIMIT_PROVIDER_SEARCHES = "MAX_DAILY_PROVIDER_SEARCHES"
LIMIT_PREPARE_REQUESTS = "MAX_DAILY_PREPARE_REQUESTS"

DEFAULT_MAX_DAILY_GEMINI_TRANSLATIONS = 20
DEFAULT_MAX_DAILY_PROVIDER_SEARCHES = 100
DEFAULT_MAX_DAILY_PREPARE_REQUESTS = 50

_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    provider TEXT,
    canonical_video_key TEXT,
    record_id INTEGER,
    job_id TEXT,
    units INTEGER NOT NULL DEFAULT 1,
    details TEXT,
    created_at TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_positive_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default)) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def get_daily_limits() -> Dict[str, int]:
    """Return the effective daily usage limits."""
    return {
        LIMIT_GEMINI_TRANSLATIONS: _parse_positive_int(
            LIMIT_GEMINI_TRANSLATIONS,
            DEFAULT_MAX_DAILY_GEMINI_TRANSLATIONS,
        ),
        LIMIT_PROVIDER_SEARCHES: _parse_positive_int(
            LIMIT_PROVIDER_SEARCHES,
            DEFAULT_MAX_DAILY_PROVIDER_SEARCHES,
        ),
        LIMIT_PREPARE_REQUESTS: _parse_positive_int(
            LIMIT_PREPARE_REQUESTS,
            DEFAULT_MAX_DAILY_PREPARE_REQUESTS,
        ),
    }


def is_allow_auto_prepare_when_limited_enabled() -> bool:
    """Return whether auto-prepare may ignore daily limits."""
    value = (os.getenv("ALLOW_AUTO_PREPARE_WHEN_LIMITED") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _connect(db_path: PathLike) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_USAGE_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_created_at ON usage_events(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_type ON usage_events(event_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_provider ON usage_events(provider)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_events_canonical ON usage_events(canonical_video_key)"
    )
    conn.commit()
    return conn


def get_usage_table_columns(db_path: PathLike) -> List[str]:
    """Return the current `usage_events` table column names."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(usage_events)").fetchall()
    return [str(row[1]) for row in rows]


def record_event(
    db_path: PathLike,
    *,
    event_type: str,
    provider: Optional[str] = None,
    canonical_video_key: Optional[str] = None,
    record_id: Optional[int] = None,
    job_id: Optional[str] = None,
    units: int = 1,
    details: Optional[Any] = None,
) -> int:
    """Persist one usage event and return the new row id."""
    created_at = _utcnow_iso()
    normalized_units = int(units or 1)
    if normalized_units <= 0:
        normalized_units = 1
    if details in (None, ""):
        details_text = None
    elif isinstance(details, str):
        details_text = details.strip() or None
    else:
        details_text = json.dumps(details, ensure_ascii=False, sort_keys=True)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO usage_events (
                event_type, provider, canonical_video_key, record_id, job_id,
                units, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event_type or "").strip(),
                _normalize_text(provider),
                _normalize_text(canonical_video_key),
                record_id,
                _normalize_text(job_id),
                normalized_units,
                details_text,
                created_at,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_events(
    db_path: PathLike,
    *,
    limit: int = 50,
    event_type: Optional[str] = None,
    provider: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return latest usage events, newest first."""
    normalized_limit = max(1, int(limit or 50))
    clauses = []
    params: List[Any] = []
    if _normalize_text(event_type):
        clauses.append("event_type = ?")
        params.append(_normalize_text(event_type))
    if _normalize_text(provider):
        clauses.append("provider = ?")
        params.append(_normalize_text(provider))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(normalized_limit)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM usage_events{0} ORDER BY id DESC LIMIT ?".format(where),
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def clear_events(db_path: PathLike) -> int:
    """Delete all usage events and return the deleted row count."""
    with _connect(db_path) as conn:
        count = int(
            conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        )
        conn.execute("DELETE FROM usage_events")
        conn.commit()
    return count


def count_events_today(
    db_path: PathLike,
    *,
    event_types: List[str],
) -> int:
    """Return today's summed units for the requested event types."""
    if not event_types:
        return 0
    placeholders = ",".join("?" for _ in event_types)
    params: List[Any] = [*event_types, _today_utc()]
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(units), 0)
            FROM usage_events
            WHERE event_type IN ({0})
              AND substr(created_at, 1, 10) = ?
            """.format(placeholders),
            params,
        ).fetchone()
    return int(row[0] or 0)


def get_usage_counts(db_path: PathLike) -> Dict[str, int]:
    """Return today's usage counters by quota family."""
    return {
        "gemini_translations_used": count_events_today(
            db_path,
            event_types=[
                EVENT_GEMINI_TRANSLATE_SYNC,
                EVENT_GEMINI_TRANSLATE_BACKGROUND,
            ],
        ),
        "provider_searches_used": count_events_today(
            db_path,
            event_types=[EVENT_PROVIDER_SEARCH],
        ),
        "prepare_requests_used": count_events_today(
            db_path,
            event_types=[EVENT_PREPARE_REQUEST, EVENT_AUTO_PREPARE_REQUEST],
        ),
    }


def get_usage_status(db_path: PathLike, *, auto_prepare_enabled: bool) -> Dict[str, Any]:
    """Return current daily usage totals, limits, and remaining counts."""
    limits = get_daily_limits()
    counts = get_usage_counts(db_path)
    gemini_limit = limits[LIMIT_GEMINI_TRANSLATIONS]
    provider_limit = limits[LIMIT_PROVIDER_SEARCHES]
    prepare_limit = limits[LIMIT_PREPARE_REQUESTS]
    return {
        "today": _today_utc(),
        "gemini_translations_used": counts["gemini_translations_used"],
        "gemini_translations_limit": gemini_limit,
        "provider_searches_used": counts["provider_searches_used"],
        "provider_searches_limit": provider_limit,
        "prepare_requests_used": counts["prepare_requests_used"],
        "prepare_requests_limit": prepare_limit,
        "auto_prepare_enabled": bool(auto_prepare_enabled),
        "allow_auto_prepare_when_limited": is_allow_auto_prepare_when_limited_enabled(),
        "gemini_translations_remaining": max(
            0, gemini_limit - counts["gemini_translations_used"]
        ),
        "provider_searches_remaining": max(
            0, provider_limit - counts["provider_searches_used"]
        ),
        "prepare_requests_remaining": max(
            0, prepare_limit - counts["prepare_requests_used"]
        ),
    }


def check_limit(
    db_path: PathLike,
    *,
    limit_name: str,
    used_today: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return a structured limit-exceeded payload when blocked."""
    limits = get_daily_limits()
    daily_limit = int(limits[limit_name])
    if used_today is None:
        used_today = _count_for_limit(db_path, limit_name)
    if used_today < daily_limit:
        return None
    return {
        "status": "limit_exceeded",
        "message": _limit_message(limit_name, used_today, daily_limit),
        "limit_name": limit_name,
        "used_today": used_today,
        "daily_limit": daily_limit,
    }


def _count_for_limit(db_path: PathLike, limit_name: str) -> int:
    if limit_name == LIMIT_GEMINI_TRANSLATIONS:
        return count_events_today(
            db_path,
            event_types=[
                EVENT_GEMINI_TRANSLATE_SYNC,
                EVENT_GEMINI_TRANSLATE_BACKGROUND,
            ],
        )
    if limit_name == LIMIT_PROVIDER_SEARCHES:
        return count_events_today(db_path, event_types=[EVENT_PROVIDER_SEARCH])
    if limit_name == LIMIT_PREPARE_REQUESTS:
        return count_events_today(
            db_path,
            event_types=[EVENT_PREPARE_REQUEST, EVENT_AUTO_PREPARE_REQUEST],
        )
    raise KeyError("Unknown limit name: {0}".format(limit_name))


def _limit_message(limit_name: str, used_today: int, daily_limit: int) -> str:
    labels = {
        LIMIT_GEMINI_TRANSLATIONS: "Daily Gemini translation limit reached.",
        LIMIT_PROVIDER_SEARCHES: "Daily provider search limit reached.",
        LIMIT_PREPARE_REQUESTS: "Daily prepare request limit reached.",
    }
    prefix = labels.get(limit_name, "Daily limit reached.")
    return "{0} Used today: {1}/{2}.".format(prefix, used_today, daily_limit)


def _normalize_text(value: Optional[Any]) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
