"""Local provider subtitle import history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from services import cache_db
from utils.hash_utils import sha256_text

PathLike = Union[str, Path]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_import_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    subtitle_id TEXT NOT NULL DEFAULT '',
    download_url_hash TEXT NOT NULL DEFAULT '',
    release_name TEXT NOT NULL DEFAULT '',
    video_identity TEXT NOT NULL DEFAULT '',
    season INTEGER,
    episode INTEGER,
    record_id INTEGER NOT NULL,
    first_imported_at TEXT NOT NULL,
    last_imported_at TEXT NOT NULL,
    import_count INTEGER NOT NULL DEFAULT 0,
    quality_level TEXT,
    quality_score INTEGER
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create or migrate the provider import history table."""
    cache_db.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(provider_import_history)").fetchall()
        }
        if "quality_level" not in columns:
            conn.execute("ALTER TABLE provider_import_history ADD COLUMN quality_level TEXT")
        if "quality_score" not in columns:
            conn.execute("ALTER TABLE provider_import_history ADD COLUMN quality_score INTEGER")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_import_history_identity
            ON provider_import_history(
                provider, subtitle_id, download_url_hash, release_name, video_identity, season, episode
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_import_history_last_imported_at
            ON provider_import_history(last_imported_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_import_history_video_lookup
            ON provider_import_history(video_identity, season, episode, last_imported_at DESC)
            """
        )
        conn.commit()


def record_import(
    db_path: PathLike,
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
    video_identity: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    record_id: int,
    quality_level: Optional[str],
    quality_score: Optional[int],
) -> Dict[str, Any]:
    """Upsert one imported provider subtitle history row and return its summary."""
    key = _history_key(
        provider=provider,
        subtitle_id=subtitle_id,
        download_url=download_url,
        release_name=release_name,
        video_identity=video_identity,
        season=season,
        episode=episode,
    )
    now = _utcnow_iso()
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, import_count, first_imported_at
            FROM provider_import_history
            WHERE provider = ?
              AND subtitle_id = ?
              AND download_url_hash = ?
              AND release_name = ?
              AND video_identity = ?
              AND season IS ?
              AND episode IS ?
            """,
            (
                key["provider"],
                key["subtitle_id"],
                key["download_url_hash"],
                key["release_name"],
                key["video_identity"],
                key["season"],
                key["episode"],
            ),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE provider_import_history
                SET record_id = ?,
                    last_imported_at = ?,
                    import_count = ?,
                    quality_level = ?,
                    quality_score = ?
                WHERE id = ?
                """,
                (
                    int(record_id),
                    now,
                    int(row["import_count"] or 0) + 1,
                    _normalize_text(quality_level),
                    quality_score,
                    int(row["id"]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO provider_import_history (
                    provider, subtitle_id, download_url_hash, release_name,
                    video_identity, season, episode, record_id,
                    first_imported_at, last_imported_at, import_count,
                    quality_level, quality_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key["provider"],
                    key["subtitle_id"],
                    key["download_url_hash"],
                    key["release_name"],
                    key["video_identity"],
                    key["season"],
                    key["episode"],
                    int(record_id),
                    now,
                    now,
                    1,
                    _normalize_text(quality_level),
                    quality_score,
                ),
            )
        conn.commit()
    return get_candidate_summary(
        db_path,
        provider=provider,
        subtitle_id=subtitle_id,
        download_url=download_url,
        release_name=release_name,
        video_identity=video_identity,
        season=season,
        episode=episode,
        legacy_video_id=None,
        canonical_video_key=video_identity,
    )


def get_candidate_summary(
    db_path: PathLike,
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
    video_identity: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    legacy_video_id: Optional[str],
    canonical_video_key: Optional[str],
) -> Dict[str, Any]:
    """Return existing import status for one provider subtitle and video identity."""
    key = _history_key(
        provider=provider,
        subtitle_id=subtitle_id,
        download_url=download_url,
        release_name=release_name,
        video_identity=video_identity,
        season=season,
        episode=episode,
    )
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM provider_import_history
            WHERE provider = ?
              AND subtitle_id = ?
              AND download_url_hash = ?
              AND release_name = ?
              AND video_identity = ?
              AND season IS ?
              AND episode IS ?
            ORDER BY last_imported_at DESC, id DESC
            LIMIT 1
            """,
            (
                key["provider"],
                key["subtitle_id"],
                key["download_url_hash"],
                key["release_name"],
                key["video_identity"],
                key["season"],
                key["episode"],
            ),
        ).fetchone()

    history_row = dict(row) if row else None
    record = None
    if history_row and history_row.get("record_id"):
        record = cache_db.get_record(db_path, int(history_row["record_id"]))
    if record is None and legacy_video_id:
        record = _find_matching_record(
            db_path,
            provider=provider,
            subtitle_id=subtitle_id,
            download_url=download_url,
            release_name=release_name,
            legacy_video_id=legacy_video_id,
            canonical_video_key=canonical_video_key,
        )
    return _build_summary(key, history_row, record)


def list_entries(db_path: PathLike, *, limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    """Return recent provider import history rows, newest first."""
    with _connect(db_path) as conn:
        if limit is None:
            rows = conn.execute(
                """
                SELECT *
                FROM provider_import_history
                ORDER BY last_imported_at DESC, import_count DESC, id DESC
                """
            ).fetchall()
        else:
            normalized_limit = max(1, int(limit or 50))
            rows = conn.execute(
                """
                SELECT *
                FROM provider_import_history
                ORDER BY last_imported_at DESC, import_count DESC, id DESC
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
        item["video_identity"] = _normalize_text(item.get("video_identity"))
        item["import_count"] = int(item.get("import_count") or 0)
        item["record_id"] = int(item.get("record_id") or 0) or None
        item["quality_level"] = _normalize_text(item.get("quality_level"))
        item["quality_score"] = item.get("quality_score")
        items.append(item)
    return items


def clear_entries(db_path: PathLike) -> int:
    """Delete all import history rows and return how many were removed."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM provider_import_history").fetchone()
        count = int(row[0] or 0)
        conn.execute("DELETE FROM provider_import_history")
        conn.commit()
    return count


def find_best_existing_import_for_video(
    db_path: PathLike,
    *,
    video_identity: Optional[str],
    legacy_video_id: Optional[str],
    canonical_video_key: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    allow_bad_quality: bool = False,
) -> Optional[Dict[str, Any]]:
    """Return the best locally imported subtitle for one video identity."""
    identities = _video_identity_candidates(
        video_identity=video_identity,
        legacy_video_id=legacy_video_id,
        canonical_video_key=canonical_video_key,
    )
    if not identities:
        return None

    rows = _find_history_rows_for_video(
        db_path,
        identities=identities,
        season=season,
        episode=episode,
    )
    if not rows:
        return None

    records = cache_db.list_records_for_video(
        db_path,
        str(legacy_video_id or video_identity or canonical_video_key or "").strip(),
        canonical_video_key=canonical_video_key,
    )
    records_by_id = {
        int(record["id"]): record
        for record in records
        if record.get("id") is not None
    }

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        record_id = int(row.get("record_id") or 0)
        record = records_by_id.get(record_id) or cache_db.get_record(db_path, record_id)
        if not record:
            continue
        summary = {
            "provider": _normalize_text(row.get("provider")),
            "subtitle_id": _normalize_text(row.get("subtitle_id")),
            "release_name": _normalize_text(row.get("release_name")),
            "video_identity": _normalize_text(row.get("video_identity")),
            "season": row.get("season"),
            "episode": row.get("episode"),
            "record_id": record_id,
            "record_status": record.get("status"),
            "record_has_arabic": bool(record.get("arabic_srt_path")),
            "import_count": int(row.get("import_count") or 0),
            "first_imported_at": row.get("first_imported_at"),
            "last_imported_at": row.get("last_imported_at"),
            "quality_level": _normalize_text(row.get("quality_level")),
            "quality_score": row.get("quality_score"),
        }
        if not allow_bad_quality and _quality_bucket(summary.get("quality_level")) <= 0:
            continue
        candidates.append(summary)

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            _quality_bucket(item.get("quality_level")),
            _translated_bucket(item.get("record_status"), item.get("record_has_arabic")),
            int(item.get("quality_score") or 0),
            int(item.get("import_count") or 0),
            str(item.get("last_imported_at") or ""),
            int(item.get("record_id") or 0),
        ),
        reverse=True,
    )
    return dict(candidates[0])


def _find_matching_record(
    db_path: PathLike,
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
    legacy_video_id: str,
    canonical_video_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    records = cache_db.list_records_for_video(
        db_path,
        legacy_video_id,
        canonical_video_key=canonical_video_key,
    )
    normalized_provider = (_normalize_text(provider) or "").lower()
    normalized_subtitle_id = (_normalize_text(subtitle_id) or "").lower()
    normalized_release_name = _normalize_text(release_name) or ""
    expected_hash = sha256_text(str(download_url or "").strip())
    for record in records:
        record_provider = (_normalize_text(record.get("source_provider")) or "").lower()
        record_subtitle_id = (_normalize_text(record.get("source_subtitle_id")) or "").lower()
        record_release_name = _normalize_text(record.get("release_name")) or ""
        record_hash = sha256_text(str(record.get("source_download_url") or "").strip())
        if (
            record_provider == normalized_provider
            and record_subtitle_id == normalized_subtitle_id
            and record_release_name == normalized_release_name
            and record_hash == expected_hash
        ):
            return record
    return None


def _find_history_rows_for_video(
    db_path: PathLike,
    *,
    identities: Iterable[str],
    season: Optional[int],
    episode: Optional[int],
) -> List[Dict[str, Any]]:
    identity_list = [str(item).strip() for item in identities if str(item).strip()]
    if not identity_list:
        return []
    placeholders = ", ".join("?" for _ in identity_list)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM provider_import_history
            WHERE video_identity IN ({0})
              AND season IS ?
              AND episode IS ?
            ORDER BY last_imported_at DESC, import_count DESC, id DESC
            """.format(placeholders),
            (*identity_list, season, episode),
        ).fetchall()
    return [dict(row) for row in rows]


def _connect(db_path: PathLike) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _history_key(
    *,
    provider: Optional[str],
    subtitle_id: Optional[str],
    download_url: Optional[str],
    release_name: Optional[str],
    video_identity: Optional[str],
    season: Optional[int],
    episode: Optional[int],
) -> Dict[str, Any]:
    return {
        "provider": (_normalize_text(provider) or "").lower(),
        "subtitle_id": (_normalize_text(subtitle_id) or "").lower(),
        "download_url_hash": sha256_text(str(download_url or "").strip()),
        "release_name": _normalize_text(release_name) or "",
        "video_identity": _normalize_text(video_identity) or "",
        "season": season,
        "episode": episode,
    }


def _video_identity_candidates(
    *,
    video_identity: Optional[str],
    legacy_video_id: Optional[str],
    canonical_video_key: Optional[str],
) -> List[str]:
    seen = set()
    values: List[str] = []
    for raw in (video_identity, canonical_video_key, legacy_video_id):
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _quality_bucket(level: Optional[str]) -> int:
    normalized = str(level or "").strip().lower()
    if normalized == "good":
        return 3
    if normalized == "warning":
        return 2
    if not normalized:
        return 2
    if normalized == "bad":
        return 0
    return 1


def _translated_bucket(status: Optional[str], has_arabic: bool) -> int:
    normalized = str(status or "").strip().lower()
    if normalized == "translated" and has_arabic:
        return 2
    if has_arabic:
        return 1
    return 0


def _build_summary(
    key: Dict[str, Any],
    row: Optional[Dict[str, Any]],
    record: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not row:
        return {
            "provider": key["provider"] or None,
            "subtitle_id": key["subtitle_id"] or None,
            "release_name": key["release_name"] or None,
            "video_identity": key["video_identity"] or None,
            "season": key["season"],
            "episode": key["episode"],
            "download_url_hash": key["download_url_hash"],
            "is_previously_imported": bool(record),
            "record_id": int(record["id"]) if record else None,
            "record_status": record.get("status") if record else None,
            "import_count": 0,
            "first_imported_at": None,
            "last_imported_at": None,
            "quality_level": None,
            "quality_score": None,
        }
    return {
        "provider": key["provider"] or None,
        "subtitle_id": key["subtitle_id"] or None,
        "release_name": key["release_name"] or None,
        "video_identity": key["video_identity"] or None,
        "season": key["season"],
        "episode": key["episode"],
        "download_url_hash": key["download_url_hash"],
        "is_previously_imported": bool(record),
        "record_id": int(row.get("record_id") or 0) if row.get("record_id") else (int(record["id"]) if record else None),
        "record_status": (record or {}).get("status"),
        "import_count": int(row.get("import_count") or 0),
        "first_imported_at": row.get("first_imported_at"),
        "last_imported_at": row.get("last_imported_at"),
        "quality_level": _normalize_text(row.get("quality_level")),
        "quality_score": row.get("quality_score"),
    }


def _normalize_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
