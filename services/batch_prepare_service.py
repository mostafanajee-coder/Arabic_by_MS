"""Sequential batch episode prepare queue built on top of prepare_service."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from services import cache_db, prepare_service, usage_guard

PathLike = Union[str, Path]

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ITEM_STATUS_SKIPPED_READY = "skipped_ready"

_LOCK = threading.Lock()
_THREADS: Dict[str, threading.Thread] = {}
_CANCELLED_BATCHES: set[str] = set()

_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_prepare_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    imdb_id TEXT NOT NULL,
    video_type TEXT NOT NULL,
    season INTEGER NOT NULL,
    episode_start INTEGER NOT NULL,
    episode_end INTEGER NOT NULL,
    query TEXT,
    release_name TEXT,
    status TEXT NOT NULL,
    total_items INTEGER NOT NULL DEFAULT 0,
    done_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT
);
"""

_ITEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_prepare_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    canonical_video_key TEXT NOT NULL,
    video_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    status TEXT NOT NULL,
    record_id INTEGER,
    job_id TEXT,
    provider TEXT,
    score REAL,
    quality_score INTEGER,
    quality_level TEXT,
    quality_warnings TEXT,
    reject_hint INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class BatchPrepareError(Exception):
    """Raised when a batch prepare request is invalid."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create or migrate the Phase 16 batch prepare tables."""
    cache_db.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_JOB_SCHEMA)
        conn.executescript(_ITEM_SCHEMA)
        _ensure_columns(
            conn,
            "batch_prepare_jobs",
            {
                "batch_id": "TEXT",
                "imdb_id": "TEXT",
                "video_type": "TEXT DEFAULT 'series'",
                "season": "INTEGER DEFAULT 0",
                "episode_start": "INTEGER DEFAULT 0",
                "episode_end": "INTEGER DEFAULT 0",
                "query": "TEXT",
                "release_name": "TEXT",
                "status": "TEXT DEFAULT 'queued'",
                "total_items": "INTEGER NOT NULL DEFAULT 0",
                "done_items": "INTEGER NOT NULL DEFAULT 0",
                "failed_items": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "error_message": "TEXT",
            },
        )
        _ensure_columns(
            conn,
            "batch_prepare_items",
            {
                "batch_id": "TEXT",
                "canonical_video_key": "TEXT",
                "video_id": "TEXT",
                "season": "INTEGER DEFAULT 0",
                "episode": "INTEGER DEFAULT 0",
                "status": "TEXT DEFAULT 'queued'",
                "record_id": "INTEGER",
                "job_id": "TEXT",
                "provider": "TEXT",
                "score": "REAL",
                "quality_score": "INTEGER",
                "quality_level": "TEXT",
                "quality_warnings": "TEXT",
                "reject_hint": "INTEGER NOT NULL DEFAULT 0",
                "error_message": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_batch_prepare_jobs_status ON batch_prepare_jobs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_batch_prepare_jobs_batch_id ON batch_prepare_jobs(batch_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_batch_prepare_items_batch_id ON batch_prepare_items(batch_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_batch_prepare_items_canonical ON batch_prepare_items(canonical_video_key)"
        )
        conn.commit()


def is_ready(db_path: PathLike) -> bool:
    """Return whether the batch prepare service can initialize its storage."""
    try:
        init_db(db_path)
    except Exception:
        return False
    return True


def get_table_columns(db_path: PathLike, table_name: str) -> List[str]:
    """Return the current column names for a batch prepare table."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info({0})".format(table_name)).fetchall()
    return [str(row[1]) for row in rows]


def active_batch_job_count(db_path: PathLike) -> int:
    """Return the number of queued or running batch jobs."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM batch_prepare_jobs
            WHERE status IN (?, ?)
            """,
            (STATUS_QUEUED, STATUS_RUNNING),
        ).fetchone()
    return int(row[0] or 0)


def request_batch_prepare(
    *,
    imdb_id: str,
    video_type: str,
    season: int,
    episode_start: int,
    episode_end: int,
    query: Optional[str],
    release_name: Optional[str],
    force: bool,
    max_items: int,
    db_path: PathLike,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
) -> Dict[str, Any]:
    """Create a batch job, enqueue its items, and start sequential processing."""
    normalized_imdb_id = _normalize_text(imdb_id)
    normalized_video_type = _normalize_text(video_type) or "series"
    normalized_query = _normalize_text(query)
    normalized_release_name = _normalize_text(release_name)
    if not normalized_imdb_id:
        raise BatchPrepareError("imdb_id is required")
    if normalized_video_type != "series":
        raise BatchPrepareError("Batch prepare currently supports series episodes only")
    if season <= 0:
        raise BatchPrepareError("season must be a positive integer")
    if episode_start <= 0 or episode_end <= 0:
        raise BatchPrepareError("episode_start and episode_end must be positive integers")
    if episode_end < episode_start:
        raise BatchPrepareError("episode_end must be greater than or equal to episode_start")

    total_requested = episode_end - episode_start + 1
    if total_requested > max_items:
        raise BatchPrepareError(
            "Requested episode range is too large. Limit: {0} item(s).".format(max_items)
        )

    batch_id = uuid.uuid4().hex
    created_at = _utcnow_iso()
    items: List[Dict[str, Any]] = []
    skipped_ready = 0
    for episode in range(episode_start, episode_end + 1):
        video_id = "{0}:{1}:{2}".format(normalized_imdb_id, season, episode)
        canonical_video_key = "{0}:s{1:02d}e{2:02d}".format(normalized_imdb_id, season, episode)
        item_status = STATUS_QUEUED
        record_id = None
        ready_record = None
        if not force:
            ready_record = cache_db.find_latest_arabic_for_video(
                db_path,
                video_id,
                canonical_video_key=canonical_video_key,
            )
            if ready_record:
                item_status = ITEM_STATUS_SKIPPED_READY
                record_id = int(ready_record["id"])
                skipped_ready += 1
        items.append(
            {
                "batch_id": batch_id,
                "canonical_video_key": canonical_video_key,
                "video_id": video_id,
                "season": season,
                "episode": episode,
                "status": item_status,
                "record_id": record_id,
                "job_id": None,
                "provider": ready_record.get("source_provider") if item_status == ITEM_STATUS_SKIPPED_READY else None,
                "score": None,
                "quality_score": None,
                "quality_level": None,
                "quality_warnings": [],
                "reject_hint": False,
                "error_message": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    usage_guard.record_event(
        db_path,
        event_type=usage_guard.EVENT_BATCH_PREPARE_REQUEST,
        details={
            "batch_id": batch_id,
            "imdb_id": normalized_imdb_id,
            "season": season,
            "episode_start": episode_start,
            "episode_end": episode_end,
            "force": bool(force),
            "max_items": max_items,
        },
    )

    initial_status = STATUS_COMPLETED if skipped_ready == len(items) else STATUS_QUEUED
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO batch_prepare_jobs (
                batch_id, imdb_id, video_type, season, episode_start, episode_end,
                query, release_name, status, total_items, done_items, failed_items,
                created_at, updated_at, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                normalized_imdb_id,
                normalized_video_type,
                season,
                episode_start,
                episode_end,
                normalized_query,
                normalized_release_name,
                initial_status,
                len(items),
                skipped_ready,
                0,
                created_at,
                created_at,
                None,
            ),
        )
        conn.executemany(
            """
            INSERT INTO batch_prepare_items (
                batch_id, canonical_video_key, video_id, season, episode, status,
                record_id, job_id, provider, score, quality_score, quality_level,
                quality_warnings, reject_hint, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["batch_id"],
                    item["canonical_video_key"],
                    item["video_id"],
                    item["season"],
                    item["episode"],
                    item["status"],
                    item["record_id"],
                    item["job_id"],
                    item["provider"],
                    item["score"],
                    item["quality_score"],
                    item["quality_level"],
                    json.dumps(item["quality_warnings"]),
                    int(bool(item["reject_hint"])),
                    item["error_message"],
                    item["created_at"],
                    item["updated_at"],
                )
                for item in items
            ],
        )
        conn.commit()

    if initial_status != STATUS_COMPLETED:
        _start_worker(
            batch_id=batch_id,
            db_path=db_path,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
            force=force,
        )
    return get_batch_status(batch_id, db_path)


def get_batch_status(batch_id: str, db_path: PathLike) -> Optional[Dict[str, Any]]:
    """Return one batch job plus its items."""
    job = _get_job_row(db_path, batch_id)
    if not job:
        return None
    items = _list_items(db_path, batch_id)
    return {
        "batch_id": job["batch_id"],
        "status": job["status"],
        "imdb_id": job["imdb_id"],
        "video_type": job["video_type"],
        "season": job["season"],
        "episode_start": job["episode_start"],
        "episode_end": job["episode_end"],
        "query": job["query"],
        "release_name": job["release_name"],
        "total_items": job["total_items"],
        "done_items": job["done_items"],
        "failed_items": job["failed_items"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "error_message": job["error_message"],
        "items": [
            {
                "canonical_video_key": item["canonical_video_key"],
                "video_id": item["video_id"],
                "season": item["season"],
                "episode": item["episode"],
                "status": item["status"],
                "record_id": item["record_id"],
                "job_id": item["job_id"],
                "provider": item["provider"],
                "score": item["score"],
                "quality_score": item["quality_score"],
                "quality_level": item["quality_level"],
                "quality_warnings": _decode_quality_warnings(item.get("quality_warnings")),
                "reject_hint": bool(item.get("reject_hint")),
                "error_message": item["error_message"],
            }
            for item in items
        ],
    }


def list_batches(db_path: PathLike, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the latest batch prepare jobs."""
    normalized_limit = max(1, int(limit or 20))
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM batch_prepare_jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def cancel_batch(batch_id: str, db_path: PathLike) -> Optional[Dict[str, Any]]:
    """Request cancellation and mark remaining queued items as cancelled."""
    job = _get_job_row(db_path, batch_id)
    if not job:
        return None
    if job["status"] in (STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED):
        return {
            "batch_id": batch_id,
            "status": job["status"],
            "message": "Batch is already finished.",
            "cancelled_items": 0,
        }

    with _LOCK:
        _CANCELLED_BATCHES.add(batch_id)

    now = _utcnow_iso()
    with _connect(db_path) as conn:
        running_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM batch_prepare_items
            WHERE batch_id = ?
              AND status = ?
            """,
            (batch_id, STATUS_RUNNING),
        ).fetchone()
        running_items = int(running_row[0] or 0)
        conn.execute(
            """
            UPDATE batch_prepare_items
            SET status = ?, error_message = ?, updated_at = ?
            WHERE batch_id = ?
              AND status = ?
            """,
            (STATUS_CANCELLED, "Batch cancelled by user.", now, batch_id, STATUS_QUEUED),
        )
        cancelled_items = int(conn.total_changes)
        conn.execute(
            """
            UPDATE batch_prepare_jobs
            SET status = ?, updated_at = ?, error_message = ?
            WHERE batch_id = ?
            """,
            (
                STATUS_CANCELLED if running_items == 0 else STATUS_RUNNING,
                now,
                "Batch cancelled by user.",
                batch_id,
            ),
        )
        conn.commit()

    _refresh_batch_counts(db_path, batch_id)
    return {
        "batch_id": batch_id,
        "status": STATUS_CANCELLED,
        "message": "Batch cancellation requested.",
        "cancelled_items": cancelled_items,
    }


def wait_for_all(timeout: float = 5.0) -> None:
    """Join any still-running batch worker threads for test cleanup."""
    with _LOCK:
        threads = list(_THREADS.values())
    for thread in threads:
        thread.join(timeout=timeout)


def reset_for_tests() -> None:
    """Clear batch worker state between tests."""
    wait_for_all(timeout=5.0)
    with _LOCK:
        _THREADS.clear()
        _CANCELLED_BATCHES.clear()


def _connect(db_path: PathLike) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection, table_name: str, columns: Dict[str, str]) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info({0})".format(table_name)).fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(
                "ALTER TABLE {0} ADD COLUMN {1} {2}".format(table_name, name, ddl)
            )


def _normalize_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _start_worker(
    *,
    batch_id: str,
    db_path: PathLike,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    force: bool,
) -> None:
    def worker() -> None:
        _run_batch(
            batch_id=batch_id,
            db_path=db_path,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
            force=force,
        )

    thread = threading.Thread(
        target=worker,
        name="batch-prepare-{0}".format(batch_id[:8]),
        daemon=True,
    )
    with _LOCK:
        _THREADS[batch_id] = thread
    thread.start()


def _run_batch(
    *,
    batch_id: str,
    db_path: PathLike,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    force: bool,
) -> None:
    _update_job_status(db_path, batch_id, status=STATUS_RUNNING, error_message=None)
    try:
        while True:
            if _is_cancel_requested(batch_id):
                _cancel_remaining_items(db_path, batch_id)
                break

            item = _get_next_queued_item(db_path, batch_id)
            if not item:
                break

            job = _get_job_row(db_path, batch_id) or {}
            _update_item(
                db_path,
                int(item["id"]),
                status=STATUS_RUNNING,
                error_message=None,
            )
            try:
                result = prepare_service.request_prepare(
                    video_id=str(item["video_id"]),
                    video_type="series",
                    season=int(item["season"]),
                    episode=int(item["episode"]),
                    query=job.get("query"),
                    release_name=job.get("release_name"),
                    language="en",
                    force=force,
                    db_path=db_path,
                    english_cache_dir=english_cache_dir,
                    arabic_cache_dir=arabic_cache_dir,
                    run_async=False,
                    request_source="batch_prepare",
                )
                _apply_prepare_result(db_path, int(item["id"]), result)
            except Exception as exc:
                _update_item(
                    db_path,
                    int(item["id"]),
                    status=STATUS_FAILED,
                    error_message=str(exc),
                )
            _refresh_batch_counts(db_path, batch_id)
    finally:
        _finalize_batch(db_path, batch_id)
        with _LOCK:
            _THREADS.pop(batch_id, None)
            _CANCELLED_BATCHES.discard(batch_id)


def _apply_prepare_result(db_path: PathLike, item_id: int, result: Dict[str, Any]) -> None:
    status = str(result.get("status") or "").strip().lower()
    if status == "already_ready":
        _update_item(
            db_path,
            item_id,
            status=ITEM_STATUS_SKIPPED_READY,
            record_id=result.get("record_id"),
            job_id=result.get("job_id"),
            provider=result.get("provider"),
            score=result.get("score"),
            quality_score=result.get("quality_score"),
            quality_level=result.get("quality_level"),
            quality_warnings=result.get("quality_warnings"),
            reject_hint=result.get("reject_hint"),
            error_message=None,
        )
        return
    if status in ("started", "already_running"):
        _update_item(
            db_path,
            item_id,
            status=STATUS_COMPLETED,
            record_id=result.get("record_id"),
            job_id=result.get("job_id"),
            provider=result.get("provider"),
            score=result.get("score"),
            quality_score=result.get("quality_score"),
            quality_level=result.get("quality_level"),
            quality_warnings=result.get("quality_warnings"),
            reject_hint=result.get("reject_hint"),
            error_message=None,
        )
        return
    _update_item(
        db_path,
        item_id,
        status=STATUS_FAILED,
        record_id=result.get("record_id"),
        job_id=result.get("job_id"),
        provider=result.get("provider"),
        score=result.get("score"),
        quality_score=result.get("quality_score"),
        quality_level=result.get("quality_level"),
        quality_warnings=result.get("quality_warnings"),
        reject_hint=result.get("reject_hint"),
        error_message=str(result.get("message") or status or "Batch prepare item failed."),
    )


def _update_job_status(
    db_path: PathLike,
    batch_id: str,
    *,
    status: str,
    error_message: Optional[str],
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE batch_prepare_jobs
            SET status = ?, updated_at = ?, error_message = ?
            WHERE batch_id = ?
            """,
            (status, _utcnow_iso(), error_message, batch_id),
        )
        conn.commit()


def _update_item(
    db_path: PathLike,
    item_id: int,
    *,
    status: str,
    record_id: Optional[Any] = None,
    job_id: Optional[Any] = None,
    provider: Optional[Any] = None,
    score: Optional[Any] = None,
    quality_score: Optional[Any] = None,
    quality_level: Optional[Any] = None,
    quality_warnings: Optional[Any] = None,
    reject_hint: Optional[Any] = None,
    error_message: Optional[str] = None,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE batch_prepare_items
            SET status = ?,
                record_id = ?,
                job_id = ?,
                provider = ?,
                score = ?,
                quality_score = ?,
                quality_level = ?,
                quality_warnings = ?,
                reject_hint = ?,
                error_message = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                record_id,
                _normalize_text(job_id),
                _normalize_text(provider),
                score,
                quality_score,
                _normalize_text(quality_level),
                json.dumps(list(quality_warnings or [])),
                int(bool(reject_hint)),
                _normalize_text(error_message),
                _utcnow_iso(),
                item_id,
            ),
        )
        conn.commit()


def _get_job_row(db_path: PathLike, batch_id: str) -> Optional[Dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM batch_prepare_jobs WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    return dict(row) if row else None


def _list_items(db_path: PathLike, batch_id: str) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM batch_prepare_items
            WHERE batch_id = ?
            ORDER BY episode ASC, id ASC
            """,
            (batch_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _get_next_queued_item(db_path: PathLike, batch_id: str) -> Optional[Dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM batch_prepare_items
            WHERE batch_id = ?
              AND status = ?
            ORDER BY episode ASC, id ASC
            LIMIT 1
            """,
            (batch_id, STATUS_QUEUED),
        ).fetchone()
    return dict(row) if row else None


def _refresh_batch_counts(db_path: PathLike, batch_id: str) -> None:
    items = _list_items(db_path, batch_id)
    done_items = sum(
        1
        for item in items
        if item["status"] in (STATUS_COMPLETED, ITEM_STATUS_SKIPPED_READY)
    )
    failed_items = sum(1 for item in items if item["status"] == STATUS_FAILED)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE batch_prepare_jobs
            SET done_items = ?, failed_items = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (done_items, failed_items, _utcnow_iso(), batch_id),
        )
        conn.commit()


def _cancel_remaining_items(db_path: PathLike, batch_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE batch_prepare_items
            SET status = ?, error_message = ?, updated_at = ?
            WHERE batch_id = ?
              AND status = ?
            """,
            (STATUS_CANCELLED, "Batch cancelled by user.", _utcnow_iso(), batch_id, STATUS_QUEUED),
        )
        conn.commit()
    _refresh_batch_counts(db_path, batch_id)


def _is_cancel_requested(batch_id: str) -> bool:
    with _LOCK:
        return batch_id in _CANCELLED_BATCHES


def _finalize_batch(db_path: PathLike, batch_id: str) -> None:
    items = _list_items(db_path, batch_id)
    if not items:
        _update_job_status(db_path, batch_id, status=STATUS_FAILED, error_message="Batch has no items.")
        return

    statuses = {str(item["status"]) for item in items}
    if _is_cancel_requested(batch_id) or STATUS_CANCELLED in statuses:
        final_status = STATUS_CANCELLED
        error_message = "Batch cancelled by user."
    elif statuses.issubset({STATUS_COMPLETED, ITEM_STATUS_SKIPPED_READY}):
        final_status = STATUS_COMPLETED
        error_message = None
    elif statuses == {STATUS_FAILED}:
        final_status = STATUS_FAILED
        error_message = _first_item_error(items) or "All batch items failed."
    elif STATUS_FAILED in statuses:
        final_status = STATUS_PARTIAL
        error_message = _first_item_error(items) or "One or more batch items failed."
    else:
        final_status = STATUS_COMPLETED
        error_message = None

    _refresh_batch_counts(db_path, batch_id)
    _update_job_status(db_path, batch_id, status=final_status, error_message=error_message)


def _first_item_error(items: List[Dict[str, Any]]) -> Optional[str]:
    for item in items:
        message = _normalize_text(item.get("error_message"))
        if message:
            return message
    return None


def _decode_quality_warnings(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]
