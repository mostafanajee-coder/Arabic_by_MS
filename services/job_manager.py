"""In-memory background translation jobs for local personal use."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from services import cache_db, translation_service

PathLike = Union[str, Path]
TranslateFn = Callable[..., Dict[str, Any]]

_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_RUNNING_BY_RECORD: Dict[int, str] = {}
_THREADS: Dict[str, threading.Thread] = {}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_ready() -> bool:
    """Return whether the in-memory job manager is available."""
    return True


def active_job_count() -> int:
    """Return the number of queued or running jobs."""
    with _LOCK:
        return sum(
            1
            for job in _JOBS.values()
            if job.get("status") in ("queued", "running")
        )


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Return a shallow copy of a job if it exists."""
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def get_running_job_for_record(record_id: int) -> Optional[Dict[str, Any]]:
    """Return the current queued/running job for a record when present."""
    with _LOCK:
        job_id = _RUNNING_BY_RECORD.get(record_id)
        if not job_id:
            return None
        job = _JOBS.get(job_id)
        if not job or job.get("status") not in ("queued", "running"):
            return None
        return dict(job)


def start_translation_job(
    *,
    record_id: int,
    force: bool,
    db_path: PathLike,
    arabic_cache_dir: PathLike,
    translate_fn: TranslateFn = translation_service.translate_record,
) -> Dict[str, Any]:
    """Start a background translation job, or reuse an existing running job."""
    with _LOCK:
        existing_id = _RUNNING_BY_RECORD.get(record_id)
        if existing_id and existing_id in _JOBS:
            return dict(_JOBS[existing_id])

        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "record_id": record_id,
            "status": "queued",
            "started_at": None,
            "finished_at": None,
            "error_message": None,
            "force": force,
        }
        _JOBS[job_id] = job
        _RUNNING_BY_RECORD[record_id] = job_id

    def worker() -> None:
        _mark_running(job_id)
        try:
            translate_fn(
                db_path,
                record_id,
                arabic_cache_dir=arabic_cache_dir,
                force=force,
            )
        except Exception as exc:
            _mark_failed(job_id, str(exc))
        else:
            _mark_completed(job_id)
        finally:
            with _LOCK:
                if _RUNNING_BY_RECORD.get(record_id) == job_id:
                    _RUNNING_BY_RECORD.pop(record_id, None)

    thread = threading.Thread(
        target=worker,
        name="translation-job-{0}".format(job_id[:8]),
        daemon=True,
    )
    with _LOCK:
        _THREADS[job_id] = thread
    thread.start()
    return get_job(job_id) or {}


def get_job_status(job_id: str, db_path: PathLike) -> Optional[Dict[str, Any]]:
    """Return a job merged with the latest translation progress from SQLite."""
    job = get_job(job_id)
    if not job:
        return None

    record = cache_db.get_record(db_path, int(job["record_id"])) or {}
    arabic_path = str(record.get("arabic_srt_path") or "").strip()
    status = {
        "job_id": job["job_id"],
        "record_id": job["record_id"],
        "status": job.get("status"),
        "error_message": job.get("error_message") or record.get("error_message"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "progress_total_chunks": record.get("progress_total_chunks"),
        "progress_done_chunks": record.get("progress_done_chunks"),
        "progress_message": record.get("progress_message"),
        "arabic_available": bool(arabic_path and Path(arabic_path).exists()),
        "force": job.get("force"),
    }
    if status["status"] == "completed" and record.get("status") == "failed":
        status["status"] = "failed"
    return status


def wait_for_all(timeout: float = 5.0) -> None:
    """Join any still-running worker threads for test cleanup."""
    with _LOCK:
        threads = list(_THREADS.values())
    for thread in threads:
        thread.join(timeout=timeout)


def reset_for_tests() -> None:
    """Clear finished job state between tests."""
    wait_for_all(timeout=5.0)
    with _LOCK:
        _JOBS.clear()
        _RUNNING_BY_RECORD.clear()
        _THREADS.clear()


def _mark_running(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = _utcnow_iso()
        job["finished_at"] = None
        job["error_message"] = None


def _mark_completed(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "completed"
        job["finished_at"] = _utcnow_iso()


def _mark_failed(job_id: str, error_message: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "failed"
        job["finished_at"] = _utcnow_iso()
        job["error_message"] = error_message
