"""SQLite-backed cache for subtitle metadata.

Schema (single table `subtitles`):

    id                INTEGER PRIMARY KEY
    video_id          TEXT NOT NULL                e.g. "tt1234567"
    imdb_id           TEXT                         nullable Phase 12 canonical base id
    season            INTEGER                      nullable Phase 12 episode season
    episode           INTEGER                      nullable Phase 12 episode number
    canonical_video_key TEXT                       nullable Phase 12 canonical lookup key
    timing_offset_ms  INTEGER                      nullable Phase 13 latest applied offset
    user_note         TEXT                         nullable Phase 13 short user note
    is_preferred      INTEGER                      nullable Phase 13 preferred translated row
    video_type        TEXT NOT NULL DEFAULT 'movie'
    release_name      TEXT
    english_srt_path  TEXT NOT NULL
    english_srt_hash  TEXT NOT NULL
    arabic_srt_path   TEXT                         (nullable in Phase 2)
    status            TEXT NOT NULL DEFAULT 'uploaded'
    error_message     TEXT
    source_provider   TEXT                         (nullable in Phase 7)
    source_subtitle_id TEXT                        (nullable in Phase 7)
    source_download_url TEXT                       (nullable in Phase 7)
    progress_total_chunks INTEGER                  (nullable in Phase 8)
    progress_done_chunks INTEGER                   (nullable in Phase 8)
    progress_message  TEXT                         (nullable in Phase 8)
    created_at        TEXT NOT NULL                ISO-8601 UTC timestamp

All functions take an explicit `db_path` so tests can use a temp database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, Path]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subtitles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    imdb_id TEXT,
    season INTEGER,
    episode INTEGER,
    canonical_video_key TEXT,
    timing_offset_ms INTEGER,
    user_note TEXT,
    is_preferred INTEGER NOT NULL DEFAULT 0,
    video_type TEXT NOT NULL DEFAULT 'movie',
    release_name TEXT,
    english_srt_path TEXT NOT NULL,
    english_srt_hash TEXT NOT NULL,
    arabic_srt_path TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded',
    error_message TEXT,
    source_provider TEXT,
    source_subtitle_id TEXT,
    source_download_url TEXT,
    progress_total_chunks INTEGER,
    progress_done_chunks INTEGER,
    progress_message TEXT,
    created_at TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create the database file (if missing) and the schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SCHEMA)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(subtitles)").fetchall()
        }
        if "imdb_id" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN imdb_id TEXT")
        if "season" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN season INTEGER")
        if "episode" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN episode INTEGER")
        if "canonical_video_key" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN canonical_video_key TEXT")
        if "timing_offset_ms" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN timing_offset_ms INTEGER")
        if "user_note" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN user_note TEXT")
        if "is_preferred" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN is_preferred INTEGER NOT NULL DEFAULT 0")
        if "error_message" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN error_message TEXT")
        if "source_provider" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN source_provider TEXT")
        if "source_subtitle_id" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN source_subtitle_id TEXT")
        if "source_download_url" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN source_download_url TEXT")
        if "progress_total_chunks" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN progress_total_chunks INTEGER")
        if "progress_done_chunks" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN progress_done_chunks INTEGER")
        if "progress_message" not in columns:
            conn.execute("ALTER TABLE subtitles ADD COLUMN progress_message TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subtitles_video_id ON subtitles(video_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subtitles_canonical_video_key ON subtitles(canonical_video_key)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subtitles_preferred ON subtitles(is_preferred)")
        conn.commit()


def _connect(db_path: PathLike) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def insert_subtitle(
    db_path: PathLike,
    *,
    video_id: str,
    imdb_id: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    canonical_video_key: Optional[str] = None,
    timing_offset_ms: Optional[int] = None,
    user_note: Optional[str] = None,
    is_preferred: int = 0,
    video_type: str,
    release_name: Optional[str],
    english_srt_path: str,
    english_srt_hash: str,
    arabic_srt_path: Optional[str] = None,
    status: str = "uploaded",
    error_message: Optional[str] = None,
    source_provider: Optional[str] = None,
    source_subtitle_id: Optional[str] = None,
    source_download_url: Optional[str] = None,
    progress_total_chunks: Optional[int] = None,
    progress_done_chunks: Optional[int] = None,
    progress_message: Optional[str] = None,
) -> int:
    """Insert a new subtitle record and return the new row id."""
    created_at = _utcnow_iso()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO subtitles (
                video_id, imdb_id, season, episode, canonical_video_key,
                timing_offset_ms, user_note, is_preferred,
                video_type, release_name,
                english_srt_path, english_srt_hash,
                arabic_srt_path, status, error_message,
                source_provider, source_subtitle_id, source_download_url,
                progress_total_chunks, progress_done_chunks, progress_message,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                imdb_id,
                season,
                episode,
                canonical_video_key,
                timing_offset_ms,
                user_note,
                int(is_preferred),
                video_type,
                release_name,
                english_srt_path,
                english_srt_hash,
                arabic_srt_path,
                status,
                error_message,
                source_provider,
                source_subtitle_id,
                source_download_url,
                progress_total_chunks,
                progress_done_chunks,
                progress_message,
                created_at,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_subtitles(db_path: PathLike) -> List[Dict[str, Any]]:
    """Return every subtitle record, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM subtitles ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_record(db_path: PathLike, record_id: int) -> Optional[Dict[str, Any]]:
    """Look up a single record by id."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM subtitles WHERE id = ?", (record_id,)
        ).fetchone()
    return dict(row) if row else None


def get_table_columns(db_path: PathLike) -> List[str]:
    """Return the current `subtitles` table column names."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(subtitles)").fetchall()
    return [str(row[1]) for row in rows]


def _lookup_params(
    *,
    canonical_video_key: Optional[str],
    legacy_video_id: str,
) -> tuple[str, tuple[Any, ...]]:
    normalized_canonical_key = str(canonical_video_key or "").strip()
    normalized_legacy_video_id = str(legacy_video_id or "").strip()
    if normalized_canonical_key:
        return (
            """
            (
                canonical_video_key = ?
                OR (canonical_video_key IS NULL AND video_id = ?)
            )
            """,
            (normalized_canonical_key, normalized_legacy_video_id),
        )
    return ("video_id = ?", (normalized_legacy_video_id,))


def find_latest_arabic_for_video(
    db_path: PathLike,
    video_id: str,
    *,
    canonical_video_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the most recent record for `video_id` that has an Arabic SRT.

    Used by the /subtitles endpoint to decide whether to serve a cached
    Arabic file or fall back to the bundled sample.
    """
    where_clause, params = _lookup_params(
        canonical_video_key=canonical_video_key,
        legacy_video_id=video_id,
    )
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT * FROM subtitles
            WHERE {where_clause}
              AND arabic_srt_path IS NOT NULL
              AND status = 'translated'
            ORDER BY
                CASE
                    WHEN canonical_video_key = ? THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN is_preferred = 1 THEN 0
                    ELSE 1
                END,
                id DESC
            LIMIT 1
            """,
            (*params, str(canonical_video_key or "").strip()),
        ).fetchone()
    return dict(row) if row else None


def list_records_for_video(
    db_path: PathLike,
    video_id: str,
    *,
    canonical_video_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return all records for one video_id, newest first."""
    where_clause, params = _lookup_params(
        canonical_video_key=canonical_video_key,
        legacy_video_id=video_id,
    )
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM subtitles
            WHERE {where_clause}
            ORDER BY
                CASE
                    WHEN canonical_video_key = ? THEN 0
                    ELSE 1
                END,
                id DESC
            """,
            (*params, str(canonical_video_key or "").strip()),
        ).fetchall()
    return [dict(row) for row in rows]


def find_best_record_for_video(
    db_path: PathLike,
    video_id: str,
    *,
    canonical_video_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the best record for Stremio status decisions.

    Preference order:
      1. Any translated record with a stored Arabic SRT path.
      2. Otherwise the newest record for the video.
    """
    where_clause, params = _lookup_params(
        canonical_video_key=canonical_video_key,
        legacy_video_id=video_id,
    )
    with _connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT * FROM subtitles
            WHERE {where_clause}
            ORDER BY
                CASE
                    WHEN canonical_video_key = ? THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN status = 'translated' AND arabic_srt_path IS NOT NULL AND is_preferred = 1 THEN 0
                    WHEN status = 'translated' AND arabic_srt_path IS NOT NULL THEN 1
                    ELSE 2
                END,
                id DESC
            LIMIT 1
            """,
            (*params, str(canonical_video_key or "").strip()),
        ).fetchone()
    return dict(row) if row else None


def set_arabic_srt(
    db_path: PathLike,
    record_id: int,
    arabic_srt_path: str,
    status: str = "translated",
    error_message: Optional[str] = None,
) -> None:
    """Attach an Arabic SRT file path to an existing record."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE subtitles
            SET arabic_srt_path = ?, status = ?, error_message = ?,
                progress_total_chunks = NULL,
                progress_done_chunks = NULL,
                progress_message = NULL
            WHERE id = ?
            """,
            (arabic_srt_path, status, error_message, record_id),
        )
        conn.commit()


def update_record_media(
    db_path: PathLike,
    record_id: int,
    *,
    english_srt_path: Optional[str] = None,
    arabic_srt_path: Optional[str] = None,
    timing_offset_ms: Optional[int] = None,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    clear_error_message: bool = False,
) -> None:
    """Update one record's media paths and timing metadata."""
    assignments = []
    params: List[Any] = []
    if english_srt_path is not None:
        assignments.append("english_srt_path = ?")
        params.append(english_srt_path)
    if arabic_srt_path is not None:
        assignments.append("arabic_srt_path = ?")
        params.append(arabic_srt_path)
    if timing_offset_ms is not None:
        assignments.append("timing_offset_ms = ?")
        params.append(int(timing_offset_ms))
    if status is not None:
        assignments.append("status = ?")
        params.append(status)
    if error_message is not None or clear_error_message:
        assignments.append("error_message = ?")
        params.append(error_message)
    if not assignments:
        return

    params.append(record_id)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE subtitles SET {0} WHERE id = ?".format(", ".join(assignments)),
            params,
        )
        conn.commit()


def set_user_note(db_path: PathLike, record_id: int, user_note: Optional[str]) -> None:
    """Store a short user note for one subtitle record."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE subtitles SET user_note = ? WHERE id = ?",
            (user_note, record_id),
        )
        conn.commit()


def set_preferred_record(
    db_path: PathLike,
    record_id: int,
    *,
    canonical_video_key: Optional[str],
    legacy_video_id: str,
) -> None:
    """Mark one record as preferred and clear the flag for sibling records."""
    where_clause, params = _lookup_params(
        canonical_video_key=canonical_video_key,
        legacy_video_id=legacy_video_id,
    )
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE subtitles SET is_preferred = 0 WHERE {0}".format(where_clause),
            params,
        )
        conn.execute(
            "UPDATE subtitles SET is_preferred = 1 WHERE id = ?",
            (record_id,),
        )
        conn.commit()


def set_failed(
    db_path: PathLike,
    record_id: int,
    error_message: Optional[str],
    *,
    status: str = "failed",
    progress_message: Optional[str] = None,
) -> None:
    """Mark a subtitle record as failed and store a short error message."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE subtitles
            SET status = ?, error_message = ?, progress_message = ?
            WHERE id = ?
            """,
            (status, error_message, progress_message, record_id),
        )
        conn.commit()


def clear_error_message(db_path: PathLike, record_id: int) -> None:
    """Clear any stored translation error for a record."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE subtitles SET error_message = NULL WHERE id = ?",
            (record_id,),
        )
        conn.commit()


def reset_translation_progress(
    db_path: PathLike,
    record_id: int,
    *,
    status: Optional[str] = None,
) -> None:
    """Clear stored translation errors and progress fields for a retry."""
    with _connect(db_path) as conn:
        if status is None:
            conn.execute(
                """
                UPDATE subtitles
                SET error_message = NULL,
                    progress_total_chunks = NULL,
                    progress_done_chunks = NULL,
                    progress_message = NULL
                WHERE id = ?
                """,
                (record_id,),
            )
        else:
            conn.execute(
                """
                UPDATE subtitles
                SET status = ?,
                    error_message = NULL,
                    progress_total_chunks = NULL,
                    progress_done_chunks = NULL,
                    progress_message = NULL
                WHERE id = ?
                """,
                (status, record_id),
            )
        conn.commit()


def set_translation_progress(
    db_path: PathLike,
    record_id: int,
    *,
    total_chunks: Optional[int],
    done_chunks: Optional[int],
    progress_message: Optional[str],
    status: str = "translating",
) -> None:
    """Persist chunked-translation progress for a record."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE subtitles
            SET status = ?,
                progress_total_chunks = ?,
                progress_done_chunks = ?,
                progress_message = ?,
                error_message = NULL
            WHERE id = ?
            """,
            (status, total_chunks, done_chunks, progress_message, record_id),
        )
        conn.commit()


def delete_record(db_path: PathLike, record_id: int) -> None:
    """Delete a subtitle record when test cleanup is needed."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM subtitles WHERE id = ?", (record_id,))
        conn.commit()
