"""Safe local cache maintenance scanning, recycle-bin cleanup, and restore helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from backend import config
from services import batch_prepare_service, cache_db, cache_integrity, job_manager

PathLike = Union[str, Path]

CLEANUP_ACTION_KEEP = "keep"
CLEANUP_ACTION_DELETE_CANDIDATE = "delete_candidate"
CLEANUP_ACTION_METADATA_ONLY = "metadata_only"

RECYCLE_STATUS_ACTIVE = "active"
RECYCLE_STATUS_RESTORED = "restored"
RECYCLE_STATUS_EMPTIED = "emptied"

RECYCLE_DIR_NAME = ".recycle"

POLICY_SAFE_READONLY = "safe_readonly"
POLICY_SAFE_RECYCLE = "safe_recycle"
POLICY_RISKY_RESTORE = "risky_restore"
POLICY_RISKY_EMPTY = "risky_empty"
POLICY_BLOCKED = "blocked"

EMPTY_RECYCLE_CONFIRMATION_TEXT = "EMPTY RECYCLE BIN"

AUDIT_ACTION_SCAN = "scan"
AUDIT_ACTION_CLEANUP_DRY_RUN = "cleanup_dry_run"
AUDIT_ACTION_CLEANUP_RECYCLE = "cleanup_recycle"
AUDIT_ACTION_RESTORE = "restore"
AUDIT_ACTION_RESTORE_BLOCKED = "restore_blocked"
AUDIT_ACTION_EMPTY_DENIED = "empty_denied"
AUDIT_ACTION_EMPTY_CONFIRMED = "empty_confirmed"
AUDIT_ACTION_RECYCLE_INTEGRITY_SCAN = "recycle_integrity_scan"
AUDIT_ACTION_ROLLBACK_DRY_RUN = "rollback_dry_run"
AUDIT_ACTION_ROLLBACK_EXECUTED = "rollback_executed"
AUDIT_ACTION_ROLLBACK_DENIED = "rollback_denied"
AUDIT_ACTION_ROLLBACK_PARTIAL = "rollback_partial"
AUDIT_ACTION_ROLLBACK_BLOCKED = "rollback_blocked"

SNAPSHOT_ACTION_CLEANUP = "cleanup"
SNAPSHOT_ACTION_RESTORE = "restore"
SNAPSHOT_ACTION_EMPTY = "empty_recycle_bin"
SNAPSHOT_ACTION_ROLLBACK_EXECUTE = "rollback_execute"

ROLLBACK_EXECUTE_CONFIRMATION_TEXT = "EXECUTE ROLLBACK"

ROLLBACK_LEVEL_RESTORABLE = "restorable"
ROLLBACK_LEVEL_PARTIALLY_RESTORABLE = "partially_restorable"
ROLLBACK_LEVEL_NOT_RESTORABLE = "not_restorable"
ROLLBACK_LEVEL_BLOCKED = "blocked"

RECYCLE_INTEGRITY_OK = "ok"
RECYCLE_INTEGRITY_TAMPERED = "tampered_recycle_item"
RECYCLE_INTEGRITY_MISSING = "missing_recycle_file"
RECYCLE_INTEGRITY_INVALID_PATH = "invalid_recycle_path"

OPERATOR_STATUS_BLOCKED = "blocked"
OPERATOR_STATUS_DENIED = "denied"
OPERATOR_STATUS_DRY_RUN = "dry_run"
OPERATOR_STATUS_PARTIAL = "partial"
OPERATOR_STATUS_EXECUTED = "executed"
OPERATOR_STATUS_NOT_EXECUTABLE = "not_executable"

_OPERATOR_STATUS_MESSAGES = {
    OPERATOR_STATUS_BLOCKED: "Action blocked by safety policy. Review warnings and inputs.",
    OPERATOR_STATUS_DENIED: "Action denied. Required safety confirmation was not provided.",
    OPERATOR_STATUS_DRY_RUN: "Dry run completed. No files were changed.",
    OPERATOR_STATUS_PARTIAL: "Action partially completed. Review blocked or skipped items.",
    OPERATOR_STATUS_EXECUTED: "Action executed successfully.",
    OPERATOR_STATUS_NOT_EXECUTABLE: "Action cannot run safely with the current state.",
}

_FAILED_RECORD_MAX_AGE = timedelta(hours=24)

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_maintenance_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_RECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_recycle_bin (
    id TEXT PRIMARY KEY,
    original_path TEXT NOT NULL,
    recycled_path TEXT NOT NULL,
    size_bytes INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    recycled_at TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    restored_at TEXT,
    restore_path TEXT,
    emptied_at TEXT
);
"""

_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_maintenance_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    recycle_item_id TEXT
);
"""

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_maintenance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    policy_level TEXT NOT NULL DEFAULT '',
    operator_confirmation_json TEXT NOT NULL DEFAULT '{}',
    counts_before_json TEXT NOT NULL DEFAULT '{}',
    relevant_file_metadata_json TEXT NOT NULL DEFAULT '[]',
    recycle_item_ids_json TEXT NOT NULL DEFAULT '[]',
    counts_after_json TEXT NOT NULL DEFAULT '{}',
    result_status TEXT NOT NULL DEFAULT '',
    affected_paths_json TEXT NOT NULL DEFAULT '[]',
    affected_count INTEGER NOT NULL DEFAULT 0,
    audit_id INTEGER,
    source_snapshot_id TEXT,
    finalized_at TEXT
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path: PathLike) -> None:
    """Create metadata storage for cache maintenance and recycle summaries."""
    cache_db.init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_META_SCHEMA)
        conn.executescript(_RECYCLE_SCHEMA)
        conn.executescript(_AUDIT_SCHEMA)
        conn.executescript(_SNAPSHOT_SCHEMA)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cache_recycle_bin)").fetchall()
        }
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE cache_recycle_bin ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        if "restored_at" not in columns:
            conn.execute("ALTER TABLE cache_recycle_bin ADD COLUMN restored_at TEXT")
        if "restore_path" not in columns:
            conn.execute("ALTER TABLE cache_recycle_bin ADD COLUMN restore_path TEXT")
        if "emptied_at" not in columns:
            conn.execute("ALTER TABLE cache_recycle_bin ADD COLUMN emptied_at TEXT")
        snapshot_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cache_maintenance_snapshots)").fetchall()
        }
        if "source_snapshot_id" not in snapshot_columns:
            conn.execute("ALTER TABLE cache_maintenance_snapshots ADD COLUMN source_snapshot_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_recycle_bin_status ON cache_recycle_bin(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_recycle_bin_recycled_at ON cache_recycle_bin(recycled_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_recycle_bin_checksum ON cache_recycle_bin(checksum_sha256)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_maintenance_audit_occurred_at "
            "ON cache_maintenance_audit(occurred_at DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_maintenance_audit_action "
            "ON cache_maintenance_audit(action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_maintenance_snapshots_created_at "
            "ON cache_maintenance_snapshots(created_at DESC, snapshot_id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_maintenance_snapshots_action "
            "ON cache_maintenance_snapshots(action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_maintenance_snapshots_audit "
            "ON cache_maintenance_snapshots(audit_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_maintenance_snapshots_source "
            "ON cache_maintenance_snapshots(source_snapshot_id)"
        )
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


def get_recycle_bin_summary(db_path: PathLike, *, limit: int = 25) -> Dict[str, Any]:
    """Return active recycled-file metadata without exposing file contents."""
    init_db(db_path)
    normalized_limit = max(1, int(limit or 25))
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM cache_recycle_bin
            WHERE status = ?
            ORDER BY recycled_at DESC, id DESC
            LIMIT ?
            """,
            (RECYCLE_STATUS_ACTIVE, normalized_limit),
        ).fetchall()
        count_row = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS total_bytes,
                   MAX(recycled_at) AS last_recycled_at
            FROM cache_recycle_bin
            WHERE status = ?
            """,
            (RECYCLE_STATUS_ACTIVE,),
        ).fetchone()
    return {
        "count": int((count_row or {})["count"] or 0) if count_row else 0,
        "total_bytes": int((count_row or {})["total_bytes"] or 0) if count_row else 0,
        "last_recycled_at": _normalize_text((count_row or {})["last_recycled_at"]) if count_row else None,
        "items": [_recycle_row_payload(dict(row)) for row in rows],
    }


def get_audit_trail(db_path: PathLike, *, limit: int = 25) -> Dict[str, Any]:
    """Return recent safe cache-maintenance audit entries."""
    init_db(db_path)
    normalized_limit = max(1, int(limit or 25))
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM cache_maintenance_audit
            ORDER BY occurred_at DESC, id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
        count_row = conn.execute(
            "SELECT COUNT(*) AS count FROM cache_maintenance_audit"
        ).fetchone()
    return {
        "count": int((count_row or {})["count"] or 0) if count_row else 0,
        "items": [_audit_row_payload(dict(row)) for row in rows],
    }


def list_maintenance_snapshots(db_path: PathLike, *, limit: int = 25) -> Dict[str, Any]:
    """Return recent maintenance snapshot metadata."""
    init_db(db_path)
    normalized_limit = max(1, int(limit or 25))
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM cache_maintenance_snapshots
            ORDER BY created_at DESC, snapshot_id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
        count_row = conn.execute(
            "SELECT COUNT(*) AS count FROM cache_maintenance_snapshots"
        ).fetchone()
    return {
        "count": int((count_row or {})["count"] or 0) if count_row else 0,
        "items": [_snapshot_row_payload(dict(row), include_relevant=False) for row in rows],
    }


def get_maintenance_snapshot(db_path: PathLike, snapshot_id: str) -> Dict[str, Any]:
    """Return one maintenance snapshot metadata entry."""
    normalized_id = _normalize_text(snapshot_id)
    if not normalized_id:
        raise ValueError("snapshot_id is required.")
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM cache_maintenance_snapshots
            WHERE snapshot_id = ?
            LIMIT 1
            """,
            (normalized_id,),
        ).fetchone()
    if not row:
        raise ValueError("Snapshot was not found.")
    return _snapshot_row_payload(dict(row), include_relevant=True)


def compare_maintenance_snapshots(
    db_path: PathLike,
    *,
    snapshot_id: str,
    compare_snapshot_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare one snapshot before/after, or compare two snapshots."""
    current = get_maintenance_snapshot(db_path, snapshot_id)
    baseline = current
    target = current
    comparison_mode = "before_after"
    if _normalize_text(compare_snapshot_id):
        other = get_maintenance_snapshot(db_path, str(compare_snapshot_id))
        baseline = other
        comparison_mode = "snapshot_to_snapshot"

    baseline_after = _coerce_count_map(
        baseline["counts_after"] if comparison_mode == "snapshot_to_snapshot" else baseline["counts_before"]
    )
    target_after = _coerce_count_map(target["counts_after"])
    delta: Dict[str, Any] = {}
    for key in sorted(set(baseline_after.keys()) | set(target_after.keys())):
        before_value = baseline_after.get(key)
        after_value = target_after.get(key)
        if isinstance(before_value, int) and isinstance(after_value, int):
            delta[key] = after_value - before_value
        elif before_value != after_value:
            delta[key] = {"before": before_value, "after": after_value}

    return {
        "comparison_mode": comparison_mode,
        "snapshot_id": current["snapshot_id"],
        "compare_snapshot_id": baseline["snapshot_id"] if comparison_mode == "snapshot_to_snapshot" else None,
        "action": current["action"],
        "before_counts": baseline_after,
        "after_counts": target_after,
        "delta": delta,
        "affected_count": int(target.get("affected_count") or 0),
        "affected_paths": list(target.get("affected_paths") or []),
        "result_status": str(target.get("result_status") or ""),
        "audit_id": target.get("audit_id"),
    }


def build_snapshot_rollback_plan(
    db_path: PathLike,
    *,
    snapshot_id: str,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
) -> Dict[str, Any]:
    """Build a metadata-only rollback plan for a maintenance snapshot."""
    snapshot = get_maintenance_snapshot(db_path, snapshot_id)
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    recycle_root = _recycle_root(english_root, arabic_root)
    action = str(snapshot.get("action") or "")

    base_plan = {
        "snapshot_id": snapshot["snapshot_id"],
        "action": action,
        "rollback_supported": action in (
            SNAPSHOT_ACTION_CLEANUP,
            SNAPSHOT_ACTION_RESTORE,
            SNAPSHOT_ACTION_EMPTY,
        ),
        "rollback_level": ROLLBACK_LEVEL_BLOCKED,
        "rollback_warnings": [],
        "candidate_items": [],
        "blocked_items": [],
        "missing_items": [],
        "tampered_items": [],
        "generated_at": _utcnow_iso(),
    }

    if action == SNAPSHOT_ACTION_CLEANUP:
        plan = _build_cleanup_rollback_plan(
            base_plan,
            db_path=db_path,
            snapshot=snapshot,
            english_root=english_root,
            arabic_root=arabic_root,
            recycle_root=recycle_root,
        )
        plan.update(
            _build_action_readiness(
                policy=_build_policy_payload(
                    POLICY_SAFE_READONLY,
                    requires_confirmation=False,
                    required_confirmation_text=None,
                    blocked_reason=None,
                    safety_warnings=list(plan.get("rollback_warnings") or []),
                ),
                ready_reason="Rollback plan is available for this cleanup snapshot.",
                blocked_fallback_reason="Rollback planning is not available for this snapshot.",
            )
        )
        plan["message"] = _status_message(OPERATOR_STATUS_DRY_RUN)
        return plan
    if action == SNAPSHOT_ACTION_RESTORE:
        plan = _build_restore_rollback_plan(
            base_plan,
            snapshot=snapshot,
            english_root=english_root,
            arabic_root=arabic_root,
        )
        plan.update(
            _build_action_readiness(
                policy=_build_policy_payload(
                    POLICY_SAFE_READONLY,
                    requires_confirmation=False,
                    required_confirmation_text=None,
                    blocked_reason=None,
                    safety_warnings=list(plan.get("rollback_warnings") or []),
                ),
                ready_reason="Rollback plan metadata is available for this restore snapshot.",
                blocked_fallback_reason="Rollback planning is not available for this snapshot.",
            )
        )
        plan["message"] = _status_message(OPERATOR_STATUS_DRY_RUN)
        return plan
    if action == SNAPSHOT_ACTION_EMPTY:
        plan = _build_empty_rollback_plan(base_plan, snapshot=snapshot)
        plan.update(
            _build_action_readiness(
                policy=_build_policy_payload(
                    POLICY_SAFE_READONLY,
                    requires_confirmation=False,
                    required_confirmation_text=None,
                    blocked_reason=None,
                    safety_warnings=list(plan.get("rollback_warnings") or []),
                ),
                ready_reason="Rollback plan metadata is available for this empty action snapshot.",
                blocked_fallback_reason="Rollback planning is not available for this snapshot.",
            )
        )
        plan["message"] = _status_message(OPERATOR_STATUS_DRY_RUN)
        return plan

    base_plan["rollback_supported"] = False
    base_plan["rollback_level"] = ROLLBACK_LEVEL_BLOCKED
    base_plan["rollback_warnings"] = [
        "Rollback planning is not supported for this snapshot action in Phase 31.",
    ]
    base_plan.update(
        _build_action_readiness(
            policy=_build_policy_payload(
                POLICY_BLOCKED,
                requires_confirmation=False,
                required_confirmation_text=None,
                blocked_reason="Rollback planning is not supported for this snapshot action.",
                safety_warnings=list(base_plan.get("rollback_warnings") or []),
            ),
            ready_reason="Rollback plan is available.",
            blocked_fallback_reason="Rollback planning is not supported for this snapshot action.",
        )
    )
    base_plan["message"] = _status_message(OPERATOR_STATUS_NOT_EXECUTABLE)
    return base_plan


def execute_snapshot_rollback(
    db_path: PathLike,
    *,
    snapshot_id: str,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    dry_run: bool = True,
    allow_rollback: bool = False,
    confirmation_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute controlled rollback for cleanup snapshots using safe restore safeguards."""
    snapshot = get_maintenance_snapshot(db_path, snapshot_id)
    plan = build_snapshot_rollback_plan(
        db_path,
        snapshot_id=snapshot_id,
        english_cache_dir=english_cache_dir,
        arabic_cache_dir=arabic_cache_dir,
    )
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    recycle_root = _recycle_root(english_root, arabic_root)
    rollback_policy = _build_rollback_execute_policy(
        action=str(snapshot.get("action") or ""),
        rollback_level=str(plan.get("rollback_level") or ""),
        dry_run=bool(dry_run),
        allow_rollback=bool(allow_rollback),
        confirmation_text=confirmation_text,
        candidate_count=len(list(plan.get("candidate_items") or [])),
    )
    execution_snapshot_id = _create_maintenance_snapshot(
        db_path,
        action=SNAPSHOT_ACTION_ROLLBACK_EXECUTE,
        policy_level=str(rollback_policy.get("policy_level") or POLICY_BLOCKED),
        counts_before={
            "candidate_count": len(list(plan.get("candidate_items") or [])),
            "blocked_count": len(list(plan.get("blocked_items") or [])),
            "missing_count": len(list(plan.get("missing_items") or [])),
            "tampered_count": len(list(plan.get("tampered_items") or [])),
            "dry_run": int(bool(dry_run)),
        },
        relevant_file_metadata=[
            {
                "id": _normalize_text(item.get("recycle_item_id")),
                "original_path": _normalize_text(item.get("original_path")),
                "recycled_path": _normalize_text(item.get("recycled_path")),
                "size_bytes": int(item.get("size_bytes") or 0),
                "action": "rollback_candidate",
            }
            for item in list(plan.get("candidate_items") or [])[:200]
        ],
        recycle_item_ids=[
            str(item.get("recycle_item_id") or "").strip()
            for item in list(plan.get("candidate_items") or [])
            if _normalize_text(item.get("recycle_item_id"))
        ],
        operator_confirmation_flags={
            "dry_run": bool(dry_run),
            "allow_rollback": bool(allow_rollback),
            "confirmation_supplied": bool(_normalize_text(confirmation_text)),
            "confirmation_matches_required": str(confirmation_text or "") == ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
        },
        source_snapshot_id=str(snapshot.get("snapshot_id") or ""),
    )

    restored_items: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    blocked_items = list(plan.get("blocked_items") or [])
    tampered_items = list(plan.get("tampered_items") or [])
    missing_items = list(plan.get("missing_items") or [])

    execution_status = "dry_run"
    audit_action = AUDIT_ACTION_ROLLBACK_DRY_RUN
    audit_status = "ok"
    audit_reason = "Generated rollback dry-run plan without restoring files."

    if str(rollback_policy.get("policy_level") or POLICY_BLOCKED) == POLICY_BLOCKED:
        execution_status = (
            "blocked"
            if str(snapshot.get("action") or "") != SNAPSHOT_ACTION_CLEANUP
            else "not_executable"
        )
        audit_action = AUDIT_ACTION_ROLLBACK_DENIED if not bool(dry_run) else AUDIT_ACTION_ROLLBACK_BLOCKED
        audit_status = str(rollback_policy.get("blocked_reason") or execution_status)[:120]
        audit_reason = str(rollback_policy.get("blocked_reason") or "Rollback execution was blocked.")[:240]
    elif bool(dry_run):
        execution_status = "dry_run"
        audit_action = AUDIT_ACTION_ROLLBACK_DRY_RUN
        audit_status = "ok"
    else:
        for candidate in list(plan.get("candidate_items") or []):
            recycle_item_id = _normalize_text(candidate.get("recycle_item_id"))
            if not recycle_item_id:
                skipped_items.append({"reason": "Missing recycle item id in rollback candidate metadata."})
                continue
            row = _find_recycle_item_by_id(db_path, recycle_item_id)
            if not row:
                skipped_items.append(
                    {
                        "recycle_item_id": recycle_item_id,
                        "reason": "Recycle item is not available anymore.",
                    }
                )
                continue
            if str(row.get("status") or "") != RECYCLE_STATUS_ACTIVE:
                skipped_items.append(
                    {
                        "recycle_item_id": recycle_item_id,
                        "reason": "Recycle item is not active and cannot be restored.",
                    }
                )
                continue
            integrity = _verify_recycle_item_integrity(row, recycle_root=recycle_root)
            integrity_status = str(integrity.get("status") or "")
            if integrity_status == RECYCLE_INTEGRITY_TAMPERED:
                tampered_items.append(
                    {
                        "recycle_item_id": recycle_item_id,
                        "reason": "Recycle file checksum does not match and is blocked.",
                    }
                )
                continue
            if integrity_status == RECYCLE_INTEGRITY_MISSING:
                missing_items.append(
                    {
                        "recycle_item_id": recycle_item_id,
                        "reason": "Recycle file is missing.",
                    }
                )
                continue
            if integrity_status == RECYCLE_INTEGRITY_INVALID_PATH:
                blocked_items.append(
                    {
                        "recycle_item_id": recycle_item_id,
                        "reason": "Recycle path is outside managed cache recycle directories.",
                    }
                )
                continue

            row_policy = _build_restore_policy(
                row,
                english_root=english_root,
                arabic_root=arabic_root,
                recycle_root=recycle_root,
                integrity=integrity,
            )
            if str(row_policy.get("policy_level") or "") == POLICY_BLOCKED:
                blocked_items.append(
                    {
                        "recycle_item_id": recycle_item_id,
                        "reason": str(row_policy.get("blocked_reason") or "Restore blocked by safety policy."),
                    }
                )
                continue

            recycled_path = Path(str(row["recycled_path"]))
            original_path = Path(str(row["original_path"]))
            restore_target = _build_available_restore_path(original_path, recycle_item_id)
            restore_target.parent.mkdir(parents=True, exist_ok=True)
            recycled_path.replace(restore_target)
            restored_at = _utcnow_iso()
            with _connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE cache_recycle_bin
                    SET status = ?, restored_at = ?, restore_path = ?
                    WHERE id = ?
                    """,
                    (
                        RECYCLE_STATUS_RESTORED,
                        restored_at,
                        str(restore_target.resolve()),
                        recycle_item_id,
                    ),
                )
                conn.commit()
            restored_items.append(
                {
                    "recycle_item_id": recycle_item_id,
                    "original_path": _safe_relative_path(str(original_path.resolve()), english_root=english_root, arabic_root=arabic_root),
                    "restored_path": _safe_relative_path(str(restore_target.resolve()), english_root=english_root, arabic_root=arabic_root),
                    "size_bytes": int(row.get("size_bytes") or 0),
                    "restored_at": restored_at,
                    "rollback_status": (
                        "restored_with_safe_suffix"
                        if restore_target.resolve() != original_path.resolve(strict=False)
                        else "restored"
                    ),
                }
            )

        if restored_items and (blocked_items or tampered_items or missing_items or skipped_items):
            execution_status = "partial"
            audit_action = AUDIT_ACTION_ROLLBACK_PARTIAL
            audit_status = "partial"
            audit_reason = "Executed rollback for safe candidates and skipped blocked or unavailable items."
        elif restored_items:
            execution_status = "executed"
            audit_action = AUDIT_ACTION_ROLLBACK_EXECUTED
            audit_status = "ok"
            audit_reason = "Executed rollback for cleanup snapshot safe candidates."
        else:
            execution_status = "not_executable"
            audit_action = AUDIT_ACTION_ROLLBACK_BLOCKED
            audit_status = "blocked"
            audit_reason = "Rollback execution found no safe restorable candidates."

    if execution_status == "partial":
        rollback_level = ROLLBACK_LEVEL_PARTIALLY_RESTORABLE
    elif execution_status in ("executed", "dry_run") and plan.get("candidate_items"):
        rollback_level = str(plan.get("rollback_level") or ROLLBACK_LEVEL_RESTORABLE)
    else:
        rollback_level = ROLLBACK_LEVEL_BLOCKED if execution_status == "blocked" else ROLLBACK_LEVEL_NOT_RESTORABLE

    audit_id = _record_audit_event(
        db_path,
        action=audit_action,
        status=audit_status,
        counts={
            "restored_count": len(restored_items),
            "skipped_count": len(skipped_items),
            "blocked_count": len(blocked_items),
            "tampered_count": len(tampered_items),
            "missing_count": len(missing_items),
            "dry_run": int(bool(dry_run)),
        },
        reason=audit_reason,
        recycle_item_id=_normalize_text(snapshot.get("snapshot_id")),
    )

    response = {
        "execution_status": execution_status,
        "dry_run": bool(dry_run),
        "rollback_level": rollback_level,
        "rollback_supported": bool(plan.get("rollback_supported")),
        "candidate_items": list(plan.get("candidate_items") or []),
        "rollback_warnings": list(plan.get("rollback_warnings") or []),
        "restored_items": restored_items,
        "skipped_items": skipped_items,
        "blocked_items": blocked_items,
        "tampered_items": tampered_items,
        "missing_items": missing_items,
        "audit_id": audit_id,
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "source_snapshot_id": str(snapshot.get("snapshot_id") or ""),
        **rollback_policy,
    }
    response.update(
        _build_action_readiness(
            policy=rollback_policy,
            ready_reason=(
                "Rollback dry-run is ready."
                if bool(dry_run)
                else "Rollback execution is ready."
            ),
            blocked_fallback_reason=(
                str(rollback_policy.get("blocked_reason") or "Rollback execution is blocked by policy.")
            ),
        )
    )
    response["message"] = _status_message(
        {
            "blocked": OPERATOR_STATUS_BLOCKED,
            "dry_run": OPERATOR_STATUS_DRY_RUN,
            "partial": OPERATOR_STATUS_PARTIAL,
            "executed": OPERATOR_STATUS_EXECUTED,
            "not_executable": OPERATOR_STATUS_NOT_EXECUTABLE,
        }.get(str(execution_status), OPERATOR_STATUS_NOT_EXECUTABLE)
    )

    _finalize_maintenance_snapshot(
        db_path,
        snapshot_id=execution_snapshot_id,
        counts_after={
            "restored_count": len(restored_items),
            "skipped_count": len(skipped_items),
            "blocked_count": len(blocked_items),
            "tampered_count": len(tampered_items),
            "missing_count": len(missing_items),
        },
        result_status=execution_status,
        affected_paths=[
            item.get("restored_path")
            for item in restored_items
        ],
        affected_count=len(restored_items),
        audit_id=audit_id,
        recycle_item_ids=[
            str(item.get("recycle_item_id") or "")
            for item in restored_items
            if _normalize_text(item.get("recycle_item_id"))
        ],
    )
    response["execution_snapshot_id"] = execution_snapshot_id
    return response


def get_policy_snapshot(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
) -> Dict[str, Any]:
    """Return current maintenance safety-policy classifications."""
    summary = scan_cache(
        db_path,
        english_cache_dir=english_cache_dir,
        arabic_cache_dir=arabic_cache_dir,
        record_audit=False,
    )
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    recycle_bin = get_recycle_bin_summary(db_path)
    return {
        "scan": _build_scan_policy(),
        "cleanup_dry_run": _build_cleanup_policy(
            summary,
            english_root=english_root,
            arabic_root=arabic_root,
            dry_run=True,
            allow_delete=False,
        ),
        "cleanup_recycle": _build_cleanup_policy(
            summary,
            english_root=english_root,
            arabic_root=arabic_root,
            dry_run=False,
            allow_delete=False,
        ),
        "restore": _build_policy_payload(
            POLICY_RISKY_RESTORE,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason=None,
            safety_warnings=[
                "Restore writes a cache file back into the cache directories.",
                "If the original path is occupied, a safe .restored- suffix path will be used.",
                "Checksum mismatch, missing recycle files, outside-cache paths, and protected path collisions are blocked.",
            ],
        ),
        "empty_recycle_bin": _build_empty_policy(
            recycle_summary=recycle_bin,
            allow_empty=False,
            confirmation_text=None,
        ),
        "counts": {
            "orphan_files": len(summary.get("orphan_files") or []),
            "protected_files": len(summary.get("protected_files") or []),
            "cleanup_candidates": len(summary.get("cleanup_candidates") or []),
            "recycle_items": int(recycle_bin.get("count") or 0),
        },
        "checked_at": _utcnow_iso(),
    }


def get_policy_snapshot_read_only(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
) -> Dict[str, Any]:
    """Return maintenance safety-policy classifications using last known summary only."""
    summary = get_summary(db_path)
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    recycle_bin = get_recycle_bin_summary(db_path)
    return {
        "scan": _build_scan_policy(),
        "cleanup_dry_run": _build_cleanup_policy(
            summary,
            english_root=english_root,
            arabic_root=arabic_root,
            dry_run=True,
            allow_delete=False,
        ),
        "cleanup_recycle": _build_cleanup_policy(
            summary,
            english_root=english_root,
            arabic_root=arabic_root,
            dry_run=False,
            allow_delete=False,
        ),
        "restore": _build_policy_payload(
            POLICY_RISKY_RESTORE,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason=None,
            safety_warnings=[
                "Restore writes a cache file back into the cache directories.",
                "If the original path is occupied, a safe .restored- suffix path will be used.",
                "Checksum mismatch, missing recycle files, outside-cache paths, and protected path collisions are blocked.",
            ],
        ),
        "empty_recycle_bin": _build_empty_policy(
            recycle_summary=recycle_bin,
            allow_empty=False,
            confirmation_text=None,
        ),
        "counts": {
            "orphan_files": len(summary.get("orphan_files") or []),
            "protected_files": len(summary.get("protected_files") or []),
            "cleanup_candidates": len(summary.get("cleanup_candidates") or []),
            "recycle_items": int(recycle_bin.get("count") or 0),
        },
        "checked_at": _utcnow_iso(),
    }


def get_operator_summary(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    audit_limit: int = 5,
    snapshot_limit: int = 5,
    refresh_scan: bool = True,
) -> Dict[str, Any]:
    """Return a consolidated safe operator summary for maintenance workflows."""
    maintenance_summary = (
        scan_cache(
            db_path,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
            record_audit=False,
        )
        if refresh_scan
        else get_summary(db_path)
    )
    integrity_summary = cache_integrity.get_summary(db_path)
    recycle_summary = get_recycle_bin_summary(db_path, limit=25)
    audit_summary = get_audit_trail(db_path, limit=max(1, int(audit_limit or 5)))
    snapshots_summary = list_maintenance_snapshots(db_path, limit=max(1, int(snapshot_limit or 5)))
    policy_summary = (
        get_policy_snapshot(
            db_path,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
        )
        if refresh_scan
        else get_policy_snapshot_read_only(
            db_path,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
        )
    )

    latest_cleanup_snapshot_id = None
    for item in list(snapshots_summary.get("items") or []):
        if str(item.get("action") or "") == SNAPSHOT_ACTION_CLEANUP:
            latest_cleanup_snapshot_id = _normalize_text(item.get("snapshot_id"))
            break
    rollback_plan = (
        build_snapshot_rollback_plan(
            db_path,
            snapshot_id=latest_cleanup_snapshot_id,
            english_cache_dir=english_cache_dir,
            arabic_cache_dir=arabic_cache_dir,
        )
        if latest_cleanup_snapshot_id
        else None
    )

    recycle_count = int(recycle_summary.get("count") or 0)
    orphan_count = len(list(maintenance_summary.get("orphan_files") or []))
    empty_policy = _build_empty_policy(
        recycle_summary=recycle_summary,
        allow_empty=False,
        confirmation_text=None,
    )
    pending_risky_actions: List[Dict[str, Any]] = []
    if recycle_count > 0:
        pending_risky_actions.append(
            {
                "action": "restore_recycle_item",
                **_build_action_readiness(
                    policy=_build_policy_payload(
                        POLICY_RISKY_RESTORE,
                        requires_confirmation=False,
                        required_confirmation_text=None,
                        blocked_reason=None,
                        safety_warnings=[
                            "Restore writes a cache file back into the cache directories.",
                        ],
                    ),
                    ready_reason="Recycle restore is available when an item id or checksum is provided.",
                    blocked_fallback_reason="Recycle restore is currently blocked.",
                ),
            }
        )
        pending_risky_actions.append(
            {
                "action": "empty_recycle_bin",
                **_build_action_readiness(
                    policy=empty_policy,
                    ready_reason="Recycle bin empty is ready.",
                    blocked_fallback_reason=str(
                        empty_policy.get("blocked_reason")
                        or "Recycle bin empty is blocked until confirmation is provided."
                    ),
                ),
            }
        )
    if rollback_plan is not None:
        pending_risky_actions.append(
            {
                "action": "rollback_plan",
                "snapshot_id": latest_cleanup_snapshot_id,
                **_build_action_readiness(
                    policy=_build_policy_payload(
                        POLICY_SAFE_READONLY if bool(rollback_plan.get("rollback_supported")) else POLICY_BLOCKED,
                        requires_confirmation=False,
                        required_confirmation_text=None,
                        blocked_reason=(
                            None
                            if bool(rollback_plan.get("rollback_supported"))
                            else "Rollback planning is not supported for this snapshot."
                        ),
                        safety_warnings=list(rollback_plan.get("rollback_warnings") or []),
                    ),
                    ready_reason="Rollback planning is available for the latest cleanup snapshot.",
                    blocked_fallback_reason="Rollback planning is not available for the latest cleanup snapshot.",
                ),
            }
        )
        execute_policy = _build_rollback_execute_policy(
            action=SNAPSHOT_ACTION_CLEANUP,
            rollback_level=str(rollback_plan.get("rollback_level") or ""),
            dry_run=False,
            allow_rollback=False,
            confirmation_text=None,
            candidate_count=len(list(rollback_plan.get("candidate_items") or [])),
        )
        pending_risky_actions.append(
            {
                "action": "rollback_execute",
                "snapshot_id": latest_cleanup_snapshot_id,
                **_build_action_readiness(
                    policy=execute_policy,
                    ready_reason="Rollback execution is ready after explicit confirmation.",
                    blocked_fallback_reason=str(
                        execute_policy.get("blocked_reason")
                        or "Rollback execution is currently blocked."
                    ),
                ),
            }
        )

    latest_audit_items = [
        {
            "timestamp": _normalize_text(item.get("timestamp")),
            "action": _normalize_text(item.get("action")) or "",
            "status": _normalize_text(item.get("status")) or "",
            "reason": _normalize_text(item.get("reason")) or "",
            "counts": _coerce_audit_counts(item.get("counts") or {}),
        }
        for item in list(audit_summary.get("items") or [])[: max(1, int(audit_limit or 5))]
    ]
    latest_snapshots = [
        {
            "snapshot_id": _normalize_text(item.get("snapshot_id")) or "",
            "created_at": _normalize_text(item.get("created_at")),
            "action": _normalize_text(item.get("action")) or "",
            "result_status": _normalize_text(item.get("result_status")) or "",
            "policy_level": _normalize_text(item.get("policy_level")) or "",
            "affected_count": int(item.get("affected_count") or 0),
        }
        for item in list(snapshots_summary.get("items") or [])[: max(1, int(snapshot_limit or 5))]
    ]
    safety_policy_summary = {
        "scan": _policy_summary_item(policy_summary.get("scan") or {}),
        "cleanup_dry_run": _policy_summary_item(policy_summary.get("cleanup_dry_run") or {}),
        "cleanup_recycle": _policy_summary_item(policy_summary.get("cleanup_recycle") or {}),
        "restore": _policy_summary_item(policy_summary.get("restore") or {}),
        "empty_recycle_bin": _policy_summary_item(policy_summary.get("empty_recycle_bin") or {}),
        "checked_at": _normalize_text(policy_summary.get("checked_at")),
    }
    risky_blocked_recently = any(
        str(item.get("action") or "") in (
            AUDIT_ACTION_EMPTY_DENIED,
            AUDIT_ACTION_ROLLBACK_DENIED,
            AUDIT_ACTION_ROLLBACK_BLOCKED,
        )
        for item in latest_audit_items
    )

    recommended = _recommended_next_action(
        orphan_count=orphan_count,
        recycle_count=recycle_count,
        rollback_plan=rollback_plan,
        risky_blocked_recently=risky_blocked_recently,
        latest_cleanup_snapshot_id=latest_cleanup_snapshot_id,
    )

    return {
        "cache_integrity_counts": dict(integrity_summary.get("counts") or {}),
        "maintenance_counts": {
            "total_files": int(maintenance_summary.get("total_files") or 0),
            "orphan_files": orphan_count,
            "missing_references": len(list(maintenance_summary.get("missing_references") or [])),
            "invalid_integrity_records": len(list(maintenance_summary.get("invalid_integrity_records") or [])),
            "stale_records": len(list(maintenance_summary.get("stale_records") or [])),
            "cleanup_candidates": len(list(maintenance_summary.get("cleanup_candidates") or [])),
            "protected_files": len(list(maintenance_summary.get("protected_files") or [])),
        },
        "recycle_bin_counts": {
            "active_items": recycle_count,
            "total_bytes": int(recycle_summary.get("total_bytes") or 0),
            "last_recycled_at": _normalize_text(recycle_summary.get("last_recycled_at")),
        },
        "latest_audit_items": latest_audit_items,
        "latest_snapshots": latest_snapshots,
        "pending_risky_actions": pending_risky_actions,
        "safety_policy_summary": safety_policy_summary,
        "recommended_next_action": recommended,
    }


def scan_recycle_bin_integrity(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    limit: int = 100,
) -> Dict[str, Any]:
    """Verify active recycle-bin files still exist and match their stored checksums."""
    init_db(db_path)
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    recycle_root = _recycle_root(english_root, arabic_root)
    checked_at = _utcnow_iso()
    items: List[Dict[str, Any]] = []
    counts = {
        RECYCLE_INTEGRITY_OK: 0,
        RECYCLE_INTEGRITY_TAMPERED: 0,
        RECYCLE_INTEGRITY_MISSING: 0,
        RECYCLE_INTEGRITY_INVALID_PATH: 0,
    }

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM cache_recycle_bin
            WHERE status = ?
            ORDER BY recycled_at DESC, id DESC
            LIMIT ?
            """,
            (RECYCLE_STATUS_ACTIVE, max(1, int(limit or 100))),
        ).fetchall()

    for raw_row in rows:
        row = dict(raw_row)
        integrity = _verify_recycle_item_integrity(row, recycle_root=recycle_root, checked_at=checked_at)
        counts[str(integrity["status"])] = counts.get(str(integrity["status"]), 0) + 1
        item = _recycle_row_payload(row)
        item["integrity"] = integrity
        items.append(item)

    result = {
        "status": "issues_found"
        if (counts[RECYCLE_INTEGRITY_TAMPERED] or counts[RECYCLE_INTEGRITY_MISSING] or counts[RECYCLE_INTEGRITY_INVALID_PATH])
        else "ok",
        "scanned_at": checked_at,
        "count": len(items),
        "counts": counts,
        "items": items,
    }
    _set_meta(db_path, "last_recycle_integrity_scan_summary", json.dumps(result))
    _set_meta(db_path, "last_recycle_integrity_scan_at", checked_at)
    _record_audit_event(
        db_path,
        action=AUDIT_ACTION_RECYCLE_INTEGRITY_SCAN,
        status=str(result["status"]),
        counts={
            "scanned_items": len(items),
            "ok_count": counts.get(RECYCLE_INTEGRITY_OK, 0),
            "tampered_count": counts.get(RECYCLE_INTEGRITY_TAMPERED, 0),
            "missing_count": counts.get(RECYCLE_INTEGRITY_MISSING, 0),
            "invalid_path_count": counts.get(RECYCLE_INTEGRITY_INVALID_PATH, 0),
        },
        reason="Verified active recycle-bin items against stored checksums.",
    )
    return result


def scan_cache(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    record_audit: bool = True,
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
                    "protected": bool(translated_or_preferred or active or not within_root),
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
    summary.update(_build_scan_policy())
    _set_meta(db_path, "last_scan_summary", json.dumps(summary))
    _set_meta(db_path, "last_scan_at", summary["scan_finished_at"])
    if record_audit:
        _record_audit_event(
            db_path,
            action=AUDIT_ACTION_SCAN,
            status="ok",
            counts={
                "total_files": summary["total_files"],
                "orphan_files": len(summary["orphan_files"]),
                "missing_references": len(summary["missing_references"]),
                "stale_records": len(summary["stale_records"]),
                "invalid_integrity_records": len(summary["invalid_integrity_records"]),
                "cleanup_candidates": len(summary["cleanup_candidates"]),
            },
            reason="Scanned cache directories and subtitle metadata.",
        )
    return summary


def cleanup_cache(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    dry_run: bool = True,
    allow_delete: bool = False,
) -> Dict[str, Any]:
    """Build a cleanup plan and optionally recycle safe orphan files only."""
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    summary = scan_cache(
        db_path,
        english_cache_dir=english_cache_dir,
        arabic_cache_dir=arabic_cache_dir,
        record_audit=False,
    )
    policy = _build_cleanup_policy(
        summary,
        english_root=english_root,
        arabic_root=arabic_root,
        dry_run=dry_run,
        allow_delete=allow_delete,
    )
    recycle_before = get_recycle_bin_summary(db_path)
    snapshot_id = _create_maintenance_snapshot(
        db_path,
        action=SNAPSHOT_ACTION_CLEANUP,
        policy_level=str(policy.get("policy_level") or POLICY_BLOCKED),
        counts_before={
            "orphan_files": len(summary.get("orphan_files") or []),
            "cleanup_candidates": len(summary.get("cleanup_candidates") or []),
            "protected_files": len(summary.get("protected_files") or []),
            "recycle_items": int(recycle_before.get("count") or 0),
            "dry_run": int(bool(dry_run)),
        },
        relevant_file_metadata=[
            {
                "candidate_type": str(item.get("candidate_type") or ""),
                "path": _safe_relative_path(item.get("path"), english_root=english_root, arabic_root=arabic_root),
                "size_bytes": int(item.get("size_bytes") or 0),
                "action": str(item.get("action") or ""),
                "protected": bool(item.get("protected")),
                "record_id": int(item.get("record_id") or 0) if item.get("record_id") not in (None, "") else None,
            }
            for item in list(summary.get("cleanup_candidates") or [])[:200]
        ],
        recycle_item_ids=[],
        operator_confirmation_flags={
            "dry_run": bool(dry_run),
            "allow_delete": bool(allow_delete),
        },
    )
    recycled_files: List[Dict[str, Any]] = []
    recycle_errors: List[Dict[str, Any]] = []
    recycle_root = _recycle_root(english_root, arabic_root)

    if not dry_run and str(policy["policy_level"]) == POLICY_SAFE_RECYCLE:
        recycle_root.mkdir(parents=True, exist_ok=True)
        for candidate in list(summary.get("cleanup_candidates") or []):
            if str(candidate.get("candidate_type") or "") != "orphan_file":
                continue
            if bool(candidate.get("protected")):
                continue
            path_text = _normalize_path_text(candidate.get("path"))
            if not path_text:
                continue
            target = Path(path_text)
            if not _can_recycle_file(target, english_root=english_root, arabic_root=arabic_root):
                continue
            try:
                recycled = _move_to_recycle(
                    db_path,
                    target=target,
                    recycle_root=recycle_root,
                    reason=str(candidate.get("reason") or "Unreferenced cache file."),
                )
                recycled_files.append(recycled)
            except OSError as exc:
                recycle_errors.append({"path": path_text, "error": str(exc)})

    result = {
        **summary,
        "dry_run": bool(dry_run),
        "allow_delete": bool(allow_delete),
        "recycled_files": recycled_files,
        "recycled_count": len(recycled_files),
        "recycled_bytes": sum(int(item.get("size_bytes") or 0) for item in recycled_files),
        "recycle_errors": recycle_errors,
        "cleanup_finished_at": _utcnow_iso(),
        "recycle_bin": get_recycle_bin_summary(db_path),
        # Backward-compatible aliases for existing cleanup consumers/tests.
        "deleted_files": recycled_files,
        "deleted_count": len(recycled_files),
        "deleted_bytes": sum(int(item.get("size_bytes") or 0) for item in recycled_files),
        "delete_errors": recycle_errors,
        **policy,
    }
    result.update(
        _build_action_readiness(
            policy=policy,
            ready_reason=(
                "Dry-run cleanup is ready and will not move files."
                if bool(dry_run)
                else "Cleanup is ready to move confirmed orphan files into recycle."
            ),
            blocked_fallback_reason=(
                str(policy.get("blocked_reason") or "Cleanup action is blocked by policy.")
            ),
        )
    )
    if str(result["policy_level"]) == POLICY_BLOCKED and not dry_run:
        result["status"] = "blocked"
        result["message"] = _status_message(OPERATOR_STATUS_BLOCKED)
    elif dry_run:
        result["status"] = "planned"
        result["message"] = _status_message(OPERATOR_STATUS_DRY_RUN)
    else:
        result["status"] = "recycled"
        result["message"] = _status_message(OPERATOR_STATUS_EXECUTED)
    _set_meta(db_path, "last_cleanup_summary", json.dumps(result))
    _set_meta(db_path, "last_cleanup_at", result["cleanup_finished_at"])
    audit_id = _record_audit_event(
        db_path,
        action=AUDIT_ACTION_CLEANUP_DRY_RUN if dry_run else AUDIT_ACTION_CLEANUP_RECYCLE,
        status=(
            str(result["status"])
            if str(result["policy_level"]) == POLICY_BLOCKED
            else ("ok" if not recycle_errors else "partial")
        ),
        counts={
            "orphan_files": len(summary["orphan_files"]),
            "cleanup_candidates": len(summary["cleanup_candidates"]),
            "recycled_count": result["recycled_count"],
            "recycle_error_count": len(recycle_errors),
        },
        reason=(
            "Built a dry-run recycle plan without moving files."
            if dry_run
            else "Moved safe orphan cache files into the recycle bin."
        ),
    )
    _finalize_maintenance_snapshot(
        db_path,
        snapshot_id=snapshot_id,
        counts_after={
            "orphan_files": len(result.get("orphan_files") or []),
            "cleanup_candidates": len(result.get("cleanup_candidates") or []),
            "recycled_count": int(result.get("recycled_count") or 0),
            "recycle_items": int((result.get("recycle_bin") or {}).get("count") or 0),
            "recycle_error_count": len(recycle_errors),
        },
        result_status=str(result.get("status") or ""),
        affected_paths=[
            _safe_relative_path(item.get("original_path"), english_root=english_root, arabic_root=arabic_root)
            for item in recycled_files
        ],
        affected_count=len(recycled_files),
        audit_id=audit_id,
        recycle_item_ids=[str(item.get("id") or "") for item in recycled_files if _normalize_text(item.get("id"))],
    )
    return result


def restore_recycled_file(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    recycle_item_id: Optional[str] = None,
    checksum_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore one recycled cache file back into a safe cache directory path."""
    row = _find_active_recycle_item(
        db_path,
        recycle_item_id=recycle_item_id,
        checksum_sha256=checksum_sha256,
    )
    if not row:
        raise ValueError("Recycle item was not found.")

    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    recycle_root = _recycle_root(english_root, arabic_root)
    integrity = _verify_recycle_item_integrity(row, recycle_root=recycle_root)
    policy = _build_restore_policy(
        row,
        english_root=english_root,
        arabic_root=arabic_root,
        recycle_root=recycle_root,
        integrity=integrity,
    )
    snapshot_id = _create_maintenance_snapshot(
        db_path,
        action=SNAPSHOT_ACTION_RESTORE,
        policy_level=str(policy.get("policy_level") or POLICY_BLOCKED),
        counts_before={
            "recycle_items": int(get_recycle_bin_summary(db_path).get("count") or 0),
            "item_size_bytes": int(row.get("size_bytes") or 0),
        },
        relevant_file_metadata=[
            {
                "id": str(row.get("id") or ""),
                "original_path": _safe_relative_path(row.get("original_path"), english_root=english_root, arabic_root=arabic_root),
                "recycled_path": _safe_relative_path(row.get("recycled_path"), english_root=english_root, arabic_root=arabic_root),
                "size_bytes": int(row.get("size_bytes") or 0),
                "checksum_sha256": str(row.get("checksum_sha256") or ""),
                "integrity_status": str(integrity.get("status") or ""),
            }
        ],
        recycle_item_ids=[str(row.get("id") or "")],
        operator_confirmation_flags={
            "input_recycle_item_id": bool(_normalize_text(recycle_item_id)),
            "input_checksum_sha256": bool(_normalize_text(checksum_sha256)),
        },
    )
    item_payload = _recycle_row_payload(row)
    if str(policy["policy_level"]) == POLICY_BLOCKED:
        audit_id = _record_audit_event(
            db_path,
            action=AUDIT_ACTION_RESTORE_BLOCKED,
            status=str(policy["blocked_reason"] or integrity.get("status") or "blocked"),
            counts={"recycled_count": 1},
            reason=(
                str(policy["blocked_reason"] or "Restore was blocked by the maintenance safety policy.")
            )[:240],
            recycle_item_id=str(row["id"]),
        )
        blocked_result = {
            "status": (
                str(integrity.get("status") or "")
                if str(integrity.get("status") or "") not in ("", RECYCLE_INTEGRITY_OK)
                else "blocked"
            ),
            "item": item_payload,
            "integrity": integrity,
            "recycle_bin": get_recycle_bin_summary(db_path),
            **policy,
        }
        blocked_result.update(
            _build_action_readiness(
                policy=policy,
                ready_reason="Restore action is ready.",
                blocked_fallback_reason=(
                    str(policy.get("blocked_reason") or "Restore action is blocked by policy.")
                ),
            )
        )
        blocked_result["message"] = _status_message(OPERATOR_STATUS_BLOCKED)
        _finalize_maintenance_snapshot(
            db_path,
            snapshot_id=snapshot_id,
            counts_after={
                "recycle_items": int((blocked_result.get("recycle_bin") or {}).get("count") or 0),
                "restored_count": 0,
            },
            result_status=str(blocked_result.get("status") or "blocked"),
            affected_paths=[],
            affected_count=0,
            audit_id=audit_id,
        )
        return blocked_result

    original_path = Path(str(row["original_path"]))
    recycled_path = Path(str(row["recycled_path"]))
    restore_target = _build_available_restore_path(original_path, str(row["id"]))
    restore_target.parent.mkdir(parents=True, exist_ok=True)
    recycled_path.replace(restore_target)

    restored_at = _utcnow_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE cache_recycle_bin
            SET status = ?, restored_at = ?, restore_path = ?
            WHERE id = ?
            """,
            (
                RECYCLE_STATUS_RESTORED,
                restored_at,
                str(restore_target.resolve()),
                str(row["id"]),
            ),
        )
        conn.commit()

    audit_id = _record_audit_event(
        db_path,
        action=AUDIT_ACTION_RESTORE,
        status="restored",
        counts={"restored_count": 1, "size_bytes": int(row["size_bytes"] or 0)},
        reason="Restored a recycle-bin item after checksum verification passed.",
        recycle_item_id=str(row["id"]),
    )

    result = {
        "status": "restored",
        "item": {
            "id": str(row["id"]),
            "original_path": str(original_path.resolve()),
            "restored_path": str(restore_target.resolve()),
            "size_bytes": int(row["size_bytes"] or 0),
            "reason": str(row["reason"] or ""),
            "checksum_sha256": str(row["checksum_sha256"] or ""),
            "recycled_at": str(row["recycled_at"] or ""),
            "restored_at": restored_at,
        },
        "integrity": integrity,
        "recycle_bin": get_recycle_bin_summary(db_path),
        **policy,
    }
    result.update(
        _build_action_readiness(
            policy=policy,
            ready_reason="Restore action completed safely.",
            blocked_fallback_reason="Restore action is blocked by policy.",
        )
    )
    result["message"] = _status_message(OPERATOR_STATUS_EXECUTED)
    _finalize_maintenance_snapshot(
        db_path,
        snapshot_id=snapshot_id,
        counts_after={
            "recycle_items": int((result.get("recycle_bin") or {}).get("count") or 0),
            "restored_count": 1,
        },
        result_status="restored",
        affected_paths=[
            _safe_relative_path(str(restore_target.resolve()), english_root=english_root, arabic_root=arabic_root),
        ],
        affected_count=1,
        audit_id=audit_id,
    )
    return result


def empty_recycle_bin(
    db_path: PathLike,
    *,
    english_cache_dir: PathLike,
    arabic_cache_dir: PathLike,
    allow_empty: bool = False,
    confirmation_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Permanently remove active recycled files only when explicitly allowed."""
    recycle_summary = get_recycle_bin_summary(db_path)
    active_before_items = list(recycle_summary.get("items") or [])
    policy = _build_empty_policy(
        recycle_summary=recycle_summary,
        allow_empty=allow_empty,
        confirmation_text=confirmation_text,
    )
    english_root = Path(english_cache_dir).resolve()
    arabic_root = Path(arabic_cache_dir).resolve()
    snapshot_id = _create_maintenance_snapshot(
        db_path,
        action=SNAPSHOT_ACTION_EMPTY,
        policy_level=str(policy.get("policy_level") or POLICY_BLOCKED),
        counts_before={
            "recycle_items": int(recycle_summary.get("count") or 0),
            "recycle_total_bytes": int(recycle_summary.get("total_bytes") or 0),
        },
        relevant_file_metadata=[
            {
                "id": str(item.get("id") or ""),
                "original_path": _safe_relative_path(item.get("original_path"), english_root=english_root, arabic_root=arabic_root),
                "recycled_path": _safe_relative_path(item.get("recycled_path"), english_root=english_root, arabic_root=arabic_root),
                "size_bytes": int(item.get("size_bytes") or 0),
                "checksum_sha256": str(item.get("checksum_sha256") or ""),
            }
            for item in active_before_items[:200]
        ],
        recycle_item_ids=[str(item.get("id") or "") for item in active_before_items if _normalize_text(item.get("id"))],
        operator_confirmation_flags={
            "allow_empty": bool(allow_empty),
            "confirmation_supplied": bool(_normalize_text(confirmation_text)),
            "confirmation_matches_required": str(confirmation_text or "") == EMPTY_RECYCLE_CONFIRMATION_TEXT,
        },
    )
    if str(policy["policy_level"]) == POLICY_BLOCKED:
        audit_id = _record_audit_event(
            db_path,
            action=AUDIT_ACTION_EMPTY_DENIED,
            status="denied",
            counts={"active_recycle_items": int(recycle_summary.get("count") or 0)},
            reason=str(policy["blocked_reason"] or "Recycle bin empty request was denied.")[:240],
        )
        denied_result = {
            "status": "empty_denied",
            "emptied_count": 0,
            "emptied_bytes": 0,
            "items": [],
            "errors": [],
            "recycle_bin": recycle_summary,
            **policy,
        }
        denied_result.update(
            _build_action_readiness(
                policy=policy,
                ready_reason="Recycle bin empty action is ready.",
                blocked_fallback_reason=(
                    str(policy.get("blocked_reason") or "Recycle bin empty action is blocked by policy.")
                ),
            )
        )
        denied_result["message"] = _status_message(OPERATOR_STATUS_DENIED)
        _finalize_maintenance_snapshot(
            db_path,
            snapshot_id=snapshot_id,
            counts_after={
                "recycle_items": int((denied_result.get("recycle_bin") or {}).get("count") or 0),
                "emptied_count": 0,
            },
            result_status="empty_denied",
            affected_paths=[],
            affected_count=0,
            audit_id=audit_id,
        )
        return denied_result

    recycle_root = _recycle_root(english_root, arabic_root)
    emptied_items: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    emptied_at = _utcnow_iso()

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM cache_recycle_bin
            WHERE status = ?
            ORDER BY recycled_at DESC, id DESC
            """,
            (RECYCLE_STATUS_ACTIVE,),
        ).fetchall()

    for raw_row in rows:
        row = dict(raw_row)
        recycled_path = Path(str(row["recycled_path"]))
        if not _is_path_within_root(recycled_path, recycle_root):
            errors.append(
                {"id": str(row["id"]), "path": str(recycled_path), "error": "Recycle item path is outside the recycle directory."}
            )
            continue
        if recycled_path.exists() and recycled_path.is_file():
            try:
                recycled_path.unlink()
            except OSError as exc:
                errors.append({"id": str(row["id"]), "path": str(recycled_path), "error": str(exc)})
                continue
        with _connect(db_path) as conn:
            conn.execute(
                """
                UPDATE cache_recycle_bin
                SET status = ?, emptied_at = ?
                WHERE id = ?
                """,
                (RECYCLE_STATUS_EMPTIED, emptied_at, str(row["id"])),
            )
            conn.commit()
        emptied_items.append(
            {
                "id": str(row["id"]),
                "recycled_path": str(recycled_path),
                "size_bytes": int(row["size_bytes"] or 0),
                "checksum_sha256": str(row["checksum_sha256"] or ""),
            }
        )

    result = {
        "status": "emptied",
        "emptied_count": len(emptied_items),
        "emptied_bytes": sum(int(item.get("size_bytes") or 0) for item in emptied_items),
        "items": emptied_items,
        "errors": errors,
        "recycle_bin": get_recycle_bin_summary(db_path),
        **policy,
    }
    result.update(
        _build_action_readiness(
            policy=policy,
            ready_reason="Recycle bin empty action completed.",
            blocked_fallback_reason="Recycle bin empty action is blocked by policy.",
        )
    )
    result["message"] = _status_message(
        OPERATOR_STATUS_PARTIAL if errors else OPERATOR_STATUS_EXECUTED
    )
    audit_id = _record_audit_event(
        db_path,
        action=AUDIT_ACTION_EMPTY_CONFIRMED,
        status="ok" if not errors else "partial",
        counts={
            "emptied_count": result["emptied_count"],
            "emptied_bytes": result["emptied_bytes"],
            "error_count": len(errors),
        },
        reason="Permanently removed active recycle-bin files after confirmation.",
    )
    _finalize_maintenance_snapshot(
        db_path,
        snapshot_id=snapshot_id,
        counts_after={
            "recycle_items": int((result.get("recycle_bin") or {}).get("count") or 0),
            "emptied_count": int(result.get("emptied_count") or 0),
            "error_count": len(errors),
        },
        result_status=str(result.get("status") or ""),
        affected_paths=[
            _safe_relative_path(item.get("recycled_path"), english_root=english_root, arabic_root=arabic_root)
            for item in emptied_items
        ],
        affected_count=len(emptied_items),
        audit_id=audit_id,
    )
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


def _can_recycle_file(target: Path, *, english_root: Path, arabic_root: Path) -> bool:
    if _project_managed_asset_reason(target):
        return False
    if not (
        _is_path_within_root(target, english_root)
        or _is_path_within_root(target, arabic_root)
    ):
        return False
    return target.exists() and target.is_file()


def _move_to_recycle(
    db_path: PathLike,
    *,
    target: Path,
    recycle_root: Path,
    reason: str,
) -> Dict[str, Any]:
    checksum_sha256 = _checksum_sha256(target)
    size_bytes = _safe_file_size(target) or 0
    item_id = uuid.uuid4().hex
    safe_name = _sanitize_filename(target.stem or target.name)
    suffix = target.suffix or ""
    recycled_target = _available_recycle_path(
        recycle_root,
        filename="{0}_{1}{2}".format(item_id, safe_name, suffix),
    )
    recycled_target.parent.mkdir(parents=True, exist_ok=True)
    target.replace(recycled_target)

    payload = {
        "id": item_id,
        "original_path": str(target.resolve(strict=False)),
        "recycled_path": str(recycled_target.resolve()),
        "size_bytes": size_bytes,
        "reason": str(reason),
        "recycled_at": _utcnow_iso(),
        "checksum_sha256": checksum_sha256,
        "status": RECYCLE_STATUS_ACTIVE,
        "restored_at": None,
        "restore_path": None,
        "emptied_at": None,
    }
    _insert_recycle_item(db_path, payload)
    return _recycle_row_payload(payload)


def _insert_recycle_item(db_path: PathLike, payload: Dict[str, Any]) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cache_recycle_bin (
                id, original_path, recycled_path, size_bytes, reason,
                recycled_at, checksum_sha256, status, restored_at, restore_path, emptied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(payload["id"]),
                str(payload["original_path"]),
                str(payload["recycled_path"]),
                int(payload.get("size_bytes") or 0),
                str(payload.get("reason") or ""),
                str(payload["recycled_at"]),
                str(payload["checksum_sha256"]),
                str(payload.get("status") or RECYCLE_STATUS_ACTIVE),
                payload.get("restored_at"),
                payload.get("restore_path"),
                payload.get("emptied_at"),
            ),
        )
        conn.commit()


def _find_active_recycle_item(
    db_path: PathLike,
    *,
    recycle_item_id: Optional[str],
    checksum_sha256: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not _normalize_text(recycle_item_id) and not _normalize_text(checksum_sha256):
        raise ValueError("recycle_item_id or checksum_sha256 is required.")
    with _connect(db_path) as conn:
        row = None
        if _normalize_text(recycle_item_id):
            row = conn.execute(
                """
                SELECT *
                FROM cache_recycle_bin
                WHERE id = ? AND status = ?
                LIMIT 1
                """,
                (str(recycle_item_id).strip(), RECYCLE_STATUS_ACTIVE),
            ).fetchone()
        if row is None and _normalize_text(checksum_sha256):
            row = conn.execute(
                """
                SELECT *
                FROM cache_recycle_bin
                WHERE checksum_sha256 = ? AND status = ?
                ORDER BY recycled_at DESC, id DESC
                LIMIT 1
                """,
                (str(checksum_sha256).strip(), RECYCLE_STATUS_ACTIVE),
            ).fetchone()
    return dict(row) if row else None


def _find_recycle_item_by_id(
    db_path: PathLike,
    recycle_item_id: str,
) -> Optional[Dict[str, Any]]:
    normalized_id = _normalize_text(recycle_item_id)
    if not normalized_id:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM cache_recycle_bin
            WHERE id = ?
            LIMIT 1
            """,
            (normalized_id,),
        ).fetchone()
    return dict(row) if row else None


def _recycle_row_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "original_path": _normalize_path_text(row.get("original_path")),
        "recycled_path": _normalize_path_text(row.get("recycled_path")),
        "size_bytes": int(row.get("size_bytes") or 0),
        "reason": str(row.get("reason") or ""),
        "recycled_at": _normalize_text(row.get("recycled_at")),
        "checksum_sha256": str(row.get("checksum_sha256") or ""),
        "status": str(row.get("status") or RECYCLE_STATUS_ACTIVE),
        "restored_at": _normalize_text(row.get("restored_at")),
        "restore_path": _normalize_path_text(row.get("restore_path")),
        "emptied_at": _normalize_text(row.get("emptied_at")),
    }


def _audit_row_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    raw_counts = row.get("counts_json")
    try:
        counts = json.loads(str(raw_counts or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    return {
        "id": int(row.get("id") or 0),
        "timestamp": _normalize_text(row.get("occurred_at")),
        "action": _normalize_text(row.get("action")) or "",
        "status": _normalize_text(row.get("status")) or "",
        "counts": _coerce_audit_counts(counts),
        "reason": _normalize_text(row.get("reason")) or "",
        "recycle_item_id": _normalize_text(row.get("recycle_item_id")),
    }


def _snapshot_row_payload(row: Dict[str, Any], *, include_relevant: bool) -> Dict[str, Any]:
    counts_before = _coerce_count_map(_parse_json_dict(row.get("counts_before_json")))
    counts_after = _coerce_count_map(_parse_json_dict(row.get("counts_after_json")))
    recycle_item_ids = [
        str(item).strip()
        for item in _parse_json_list(row.get("recycle_item_ids_json"))
        if str(item).strip()
    ]
    affected_paths = [
        str(item).strip()
        for item in _parse_json_list(row.get("affected_paths_json"))
        if str(item).strip()
    ]
    payload = {
        "snapshot_id": str(row.get("snapshot_id") or ""),
        "created_at": _normalize_text(row.get("created_at")),
        "action": str(row.get("action") or ""),
        "source_snapshot_id": _normalize_text(row.get("source_snapshot_id")),
        "policy_level": str(row.get("policy_level") or ""),
        "operator_confirmation_flags": _sanitize_operator_confirmation_flags(
            _parse_json_dict(row.get("operator_confirmation_json"))
        ),
        "counts_before": counts_before,
        "counts_after": counts_after,
        "recycle_item_ids": recycle_item_ids,
        "result_status": str(row.get("result_status") or ""),
        "affected_paths": affected_paths,
        "affected_count": int(row.get("affected_count") or 0),
        "audit_id": int(row["audit_id"]) if row.get("audit_id") not in (None, "") else None,
        "finalized_at": _normalize_text(row.get("finalized_at")),
    }
    if include_relevant:
        payload["relevant_file_metadata"] = _sanitize_snapshot_file_metadata(
            _parse_json_list(row.get("relevant_file_metadata_json"))
        )
    return payload


def _build_cleanup_rollback_plan(
    base_plan: Dict[str, Any],
    *,
    db_path: PathLike,
    snapshot: Dict[str, Any],
    english_root: Path,
    arabic_root: Path,
    recycle_root: Path,
) -> Dict[str, Any]:
    recycle_rows = _load_snapshot_recycle_rows(
        db_path,
        snapshot,
        english_root=english_root,
        arabic_root=arabic_root,
    )
    if not recycle_rows:
        base_plan["rollback_level"] = ROLLBACK_LEVEL_NOT_RESTORABLE
        base_plan["rollback_warnings"] = [
            "Snapshot metadata does not include restorable recycle items for this cleanup action.",
        ]
        return base_plan

    for row in recycle_rows:
        item = {
            "recycle_item_id": str(row.get("id") or ""),
            "status": str(row.get("status") or ""),
            "original_path": _safe_relative_path(row.get("original_path"), english_root=english_root, arabic_root=arabic_root),
            "recycled_path": _safe_relative_path(row.get("recycled_path"), english_root=english_root, arabic_root=arabic_root),
            "size_bytes": int(row.get("size_bytes") or 0),
            "checksum_sha256": str(row.get("checksum_sha256") or ""),
        }
        if str(row.get("status") or "") != RECYCLE_STATUS_ACTIVE:
            item["reason"] = "Recycle item is no longer active."
            base_plan["blocked_items"].append(item)
            continue
        integrity = _verify_recycle_item_integrity(row, recycle_root=recycle_root)
        if str(integrity.get("status") or "") == RECYCLE_INTEGRITY_MISSING:
            item["reason"] = "Recycle file is missing."
            base_plan["missing_items"].append(item)
            continue
        if str(integrity.get("status") or "") == RECYCLE_INTEGRITY_TAMPERED:
            item["reason"] = "Recycle file checksum does not match snapshot metadata."
            base_plan["tampered_items"].append(item)
            continue
        if str(integrity.get("status") or "") == RECYCLE_INTEGRITY_INVALID_PATH:
            item["reason"] = "Recycle path is outside the managed recycle directory."
            base_plan["blocked_items"].append(item)
            continue

        original_path = Path(str(row.get("original_path") or ""))
        if not _is_path_within_any_root(original_path, (english_root, arabic_root)):
            item["reason"] = "Original path is outside managed cache directories."
            base_plan["blocked_items"].append(item)
            continue
        protected_reason = _project_managed_asset_reason(original_path)
        if protected_reason:
            item["reason"] = "Protected path collision blocks rollback: {0}".format(protected_reason)
            base_plan["blocked_items"].append(item)
            continue
        if original_path.exists():
            item["rollback_status"] = "restorable_with_safe_suffix"
            item["reason"] = "Original path is occupied; safe suffix restore path would be used."
        else:
            item["rollback_status"] = "restorable"
            item["reason"] = "Recycle item can be restored using normal restore safeguards."
        base_plan["candidate_items"].append(item)

    candidate_count = len(base_plan["candidate_items"])
    blocked_count = len(base_plan["blocked_items"]) + len(base_plan["missing_items"]) + len(base_plan["tampered_items"])
    if candidate_count and not blocked_count:
        base_plan["rollback_level"] = ROLLBACK_LEVEL_RESTORABLE
    elif candidate_count:
        base_plan["rollback_level"] = ROLLBACK_LEVEL_PARTIALLY_RESTORABLE
    else:
        base_plan["rollback_level"] = ROLLBACK_LEVEL_NOT_RESTORABLE

    warnings: List[str] = [
        "Phase 31 rollback planning is metadata-only and does not perform file restore automatically.",
    ]
    if base_plan["tampered_items"]:
        warnings.append("Some recycle items are tampered and blocked from safe restore.")
    if base_plan["missing_items"]:
        warnings.append("Some recycle items are missing and cannot be restored.")
    if base_plan["blocked_items"]:
        warnings.append("Some recycle items are blocked by path or status safety checks.")
    if any(item.get("rollback_status") == "restorable_with_safe_suffix" for item in base_plan["candidate_items"]):
        warnings.append("Occupied original paths are marked as restorable_with_safe_suffix.")
    base_plan["rollback_warnings"] = warnings
    return base_plan


def _build_restore_rollback_plan(
    base_plan: Dict[str, Any],
    *,
    snapshot: Dict[str, Any],
    english_root: Path,
    arabic_root: Path,
) -> Dict[str, Any]:
    reviewed: List[Dict[str, Any]] = []
    for path_text in list(snapshot.get("affected_paths") or []):
        resolved = _absolute_path_from_safe_relative(path_text, english_root=english_root, arabic_root=arabic_root)
        exists = bool(resolved and resolved.exists() and resolved.is_file())
        reviewed.append(
            {
                "path": str(path_text or ""),
                "exists": exists,
                "review_status": "present" if exists else "missing",
            }
        )
    base_plan["candidate_items"] = reviewed
    if any(bool(item.get("exists")) for item in reviewed):
        base_plan["rollback_level"] = ROLLBACK_LEVEL_PARTIALLY_RESTORABLE
    else:
        base_plan["rollback_level"] = ROLLBACK_LEVEL_NOT_RESTORABLE
    base_plan["rollback_warnings"] = [
        "Restore reversal is metadata-only in Phase 31; no automatic reverse-restore is executed.",
        "Any follow-up restore action must be explicitly run by an operator via existing safe endpoints.",
    ]
    return base_plan


def _build_empty_rollback_plan(
    base_plan: Dict[str, Any],
    *,
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    base_plan["rollback_level"] = ROLLBACK_LEVEL_NOT_RESTORABLE
    base_plan["rollback_warnings"] = [
        "Empty recycle bin permanently removed recycle files; automatic rollback is unavailable.",
        "Snapshot and audit metadata remain available for traceability only.",
    ]
    base_plan["blocked_items"] = [
        {
            "reason": "Recycle items were emptied and cannot be reconstructed from metadata.",
            "affected_count": int(snapshot.get("affected_count") or 0),
            "audit_id": snapshot.get("audit_id"),
        }
    ]
    return base_plan


def _load_snapshot_recycle_rows(
    db_path: PathLike,
    snapshot: Dict[str, Any],
    *,
    english_root: Path,
    arabic_root: Path,
) -> List[Dict[str, Any]]:
    ids = [str(item).strip() for item in list(snapshot.get("recycle_item_ids") or []) if str(item).strip()]
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    with _connect(db_path) as conn:
        for recycle_item_id in ids:
            row = conn.execute(
                "SELECT * FROM cache_recycle_bin WHERE id = ? LIMIT 1",
                (recycle_item_id,),
            ).fetchone()
            if row:
                row_dict = dict(row)
                key = str(row_dict.get("id") or "")
                if key and key not in seen:
                    rows.append(row_dict)
                    seen.add(key)
        if rows:
            return rows
        absolute_paths = _snapshot_original_paths(snapshot, english_root=english_root, arabic_root=arabic_root)
        for original_path in absolute_paths:
            row = conn.execute(
                """
                SELECT *
                FROM cache_recycle_bin
                WHERE original_path = ?
                ORDER BY recycled_at DESC, id DESC
                LIMIT 1
                """,
                (str(original_path),),
            ).fetchone()
            if row:
                row_dict = dict(row)
                key = str(row_dict.get("id") or "")
                if key and key not in seen:
                    rows.append(row_dict)
                    seen.add(key)
    return rows


def _snapshot_original_paths(
    snapshot: Dict[str, Any],
    *,
    english_root: Path,
    arabic_root: Path,
) -> List[Path]:
    found: List[Path] = []
    seen: Set[str] = set()
    for rel_text in list(snapshot.get("affected_paths") or []):
        resolved = _absolute_path_from_safe_relative(rel_text, english_root=english_root, arabic_root=arabic_root)
        if resolved:
            key = str(resolved.resolve(strict=False))
            if key not in seen:
                found.append(resolved.resolve(strict=False))
                seen.add(key)
    for item in list(snapshot.get("relevant_file_metadata") or []):
        rel_text = _normalize_text(item.get("original_path") or item.get("path"))
        resolved = _absolute_path_from_safe_relative(rel_text, english_root=english_root, arabic_root=arabic_root)
        if resolved:
            key = str(resolved.resolve(strict=False))
            if key not in seen:
                found.append(resolved.resolve(strict=False))
                seen.add(key)
    return found


def _create_maintenance_snapshot(
    db_path: PathLike,
    *,
    action: str,
    policy_level: str,
    counts_before: Dict[str, Any],
    relevant_file_metadata: List[Dict[str, Any]],
    recycle_item_ids: List[str],
    operator_confirmation_flags: Dict[str, Any],
    source_snapshot_id: Optional[str] = None,
) -> str:
    snapshot_id = uuid.uuid4().hex
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cache_maintenance_snapshots (
                snapshot_id, created_at, action, policy_level,
                operator_confirmation_json, counts_before_json,
                relevant_file_metadata_json, recycle_item_ids_json, source_snapshot_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                _utcnow_iso(),
                str(action or "").strip(),
                str(policy_level or "").strip(),
                json.dumps(_sanitize_operator_confirmation_flags(operator_confirmation_flags), sort_keys=True),
                json.dumps(_coerce_count_map(counts_before), sort_keys=True),
                json.dumps(
                    _sanitize_snapshot_file_metadata(relevant_file_metadata),
                    sort_keys=True,
                ),
                json.dumps(
                    [str(item).strip() for item in recycle_item_ids if str(item).strip()],
                    sort_keys=True,
                ),
                _normalize_text(source_snapshot_id),
            ),
        )
        conn.commit()
    return snapshot_id


def _finalize_maintenance_snapshot(
    db_path: PathLike,
    *,
    snapshot_id: str,
    counts_after: Dict[str, Any],
    result_status: str,
    affected_paths: List[Optional[str]],
    affected_count: int,
    audit_id: Optional[int],
    recycle_item_ids: Optional[List[str]] = None,
) -> None:
    normalized_id = _normalize_text(snapshot_id)
    if not normalized_id:
        return
    recycle_ids_json = None
    if recycle_item_ids is not None:
        recycle_ids_json = json.dumps(
            [str(item).strip() for item in recycle_item_ids if str(item).strip()],
            sort_keys=True,
        )
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE cache_maintenance_snapshots
            SET counts_after_json = ?,
                result_status = ?,
                affected_paths_json = ?,
                affected_count = ?,
                audit_id = ?,
                finalized_at = ?,
                recycle_item_ids_json = CASE
                    WHEN ? IS NULL THEN recycle_item_ids_json
                    ELSE ?
                END
            WHERE snapshot_id = ?
            """,
            (
                json.dumps(_coerce_count_map(counts_after), sort_keys=True),
                str(result_status or "").strip(),
                json.dumps(
                    [str(item).strip() for item in affected_paths if _normalize_text(item)],
                    sort_keys=True,
                ),
                max(0, int(affected_count or 0)),
                int(audit_id) if audit_id is not None else None,
                _utcnow_iso(),
                recycle_ids_json,
                recycle_ids_json,
                normalized_id,
            ),
        )
        conn.commit()


def _record_audit_event(
    db_path: PathLike,
    *,
    action: str,
    status: str,
    counts: Optional[Dict[str, Any]] = None,
    reason: str = "",
    recycle_item_id: Optional[str] = None,
) -> int:
    normalized_counts = _coerce_audit_counts(counts or {})
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO cache_maintenance_audit (
                occurred_at, action, status, counts_json, reason, recycle_item_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _utcnow_iso(),
                str(action or "").strip(),
                str(status or "").strip(),
                json.dumps(normalized_counts, sort_keys=True),
                str(reason or "").strip(),
                _normalize_text(recycle_item_id),
            ),
        )
        conn.commit()
    return int(cursor.lastrowid or 0)


def _coerce_audit_counts(payload: Dict[str, Any]) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}
    for key, value in dict(payload or {}).items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, bool):
            counts[name] = int(value)
        elif isinstance(value, int):
            counts[name] = value
        else:
            try:
                counts[name] = int(value)
            except (TypeError, ValueError):
                text = _normalize_text(value)
                if text is not None:
                    counts[name] = text[:120]
    return counts


def _coerce_count_map(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _coerce_audit_counts(payload)


def _parse_json_dict(raw: Any) -> Dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_json_list(raw: Any) -> List[Any]:
    try:
        data = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(data) if isinstance(data, list) else []


def _sanitize_snapshot_file_metadata(items: List[Any]) -> List[Dict[str, Any]]:
    sanitized: List[Dict[str, Any]] = []
    for raw_item in list(items or [])[:200]:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        sanitized.append(
            {
                "id": _normalize_text(item.get("id")),
                "candidate_type": _normalize_text(item.get("candidate_type")),
                "action": _normalize_text(item.get("action")),
                "path": _normalize_text(item.get("path")),
                "original_path": _normalize_text(item.get("original_path")),
                "recycled_path": _normalize_text(item.get("recycled_path")),
                "size_bytes": int(item.get("size_bytes") or 0),
                "protected": bool(item.get("protected")),
                "record_id": int(item.get("record_id") or 0) if item.get("record_id") not in (None, "") else None,
                "checksum_sha256": _normalize_text(item.get("checksum_sha256")),
                "integrity_status": _normalize_text(item.get("integrity_status")),
            }
        )
    return sanitized


def _sanitize_operator_confirmation_flags(payload: Dict[str, Any]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    for key, value in dict(payload or {}).items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, bool):
            sanitized[name] = value
            continue
        if isinstance(value, int):
            sanitized[name] = value
            continue
        text = _normalize_text(value)
        if text is not None:
            sanitized[name] = text[:120]
    return sanitized


def _verify_recycle_item_integrity(
    row: Dict[str, Any],
    *,
    recycle_root: Path,
    checked_at: Optional[str] = None,
) -> Dict[str, Any]:
    recycled_path = Path(str(row["recycled_path"]))
    expected_checksum = str(row.get("checksum_sha256") or "")
    payload = {
        "status": RECYCLE_INTEGRITY_OK,
        "checked_at": checked_at or _utcnow_iso(),
        "checksum_verified": False,
        "expected_checksum_sha256": expected_checksum,
        "current_checksum_sha256": None,
    }
    if not _is_path_within_root(recycled_path, recycle_root):
        payload["status"] = RECYCLE_INTEGRITY_INVALID_PATH
        return payload
    if not recycled_path.exists() or not recycled_path.is_file():
        payload["status"] = RECYCLE_INTEGRITY_MISSING
        return payload
    current_checksum = _checksum_sha256(recycled_path)
    payload["current_checksum_sha256"] = current_checksum
    payload["checksum_verified"] = current_checksum == expected_checksum
    if not payload["checksum_verified"]:
        payload["status"] = RECYCLE_INTEGRITY_TAMPERED
    return payload


def _build_scan_policy() -> Dict[str, Any]:
    return _build_policy_payload(
        POLICY_SAFE_READONLY,
        requires_confirmation=False,
        required_confirmation_text=None,
        blocked_reason=None,
        safety_warnings=[
            "This action scans cache metadata and files without moving or deleting anything.",
        ],
    )


def _build_cleanup_policy(
    summary: Dict[str, Any],
    *,
    english_root: Path,
    arabic_root: Path,
    dry_run: bool,
    allow_delete: bool,
) -> Dict[str, Any]:
    if dry_run:
        warnings = [
            "Dry-run cleanup only classifies cache items and does not move files.",
        ]
        protected_count = len(summary.get("protected_files") or [])
        if protected_count:
            warnings.append(
                "Protected cache files remain blocked from maintenance actions ({0}).".format(protected_count)
            )
        return _build_policy_payload(
            POLICY_SAFE_READONLY,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason=None,
            safety_warnings=warnings,
        )

    eligible_orphans = _eligible_cleanup_recycle_candidates(
        summary,
        english_root=english_root,
        arabic_root=arabic_root,
    )
    protected_count = len(summary.get("protected_files") or [])
    warnings = [
        "Actual cleanup is recycle-only and never permanently deletes cache files.",
    ]
    if protected_count:
        warnings.append(
            "Protected cache files are blocked from recycle cleanup ({0}).".format(protected_count)
        )
    if not allow_delete:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=True,
            required_confirmation_text=None,
            blocked_reason="allow_delete=true is required before orphan files may be recycled.",
            safety_warnings=warnings,
        )
    if eligible_orphans:
        warnings.append(
            "Only confirmed orphan files under the configured cache directories will be moved to the recycle bin ({0}).".format(
                len(eligible_orphans)
            )
        )
        return _build_policy_payload(
            POLICY_SAFE_RECYCLE,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason=None,
            safety_warnings=warnings,
        )
    blocked_reason = "No confirmed orphan files under the configured cache directories are available to recycle."
    if protected_count:
        blocked_reason = "Protected cache files are blocked from recycle cleanup."
    return _build_policy_payload(
        POLICY_BLOCKED,
        requires_confirmation=False,
        required_confirmation_text=None,
        blocked_reason=blocked_reason,
        safety_warnings=warnings,
    )


def _build_restore_policy(
    row: Dict[str, Any],
    *,
    english_root: Path,
    arabic_root: Path,
    recycle_root: Path,
    integrity: Dict[str, Any],
) -> Dict[str, Any]:
    original_path = Path(str(row["original_path"]))
    recycled_path = Path(str(row["recycled_path"]))
    warnings = [
        "Restore writes a file back into the configured cache directories.",
    ]

    if not _is_path_within_root(recycled_path, recycle_root):
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason="Recycle item path is outside the recycle directory.",
            safety_warnings=warnings,
        )
    if not _is_path_within_any_root(original_path, (english_root, arabic_root)):
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason="Recycle item original path is outside the configured cache directories.",
            safety_warnings=warnings,
        )
    protected_reason = _project_managed_asset_reason(original_path)
    if protected_reason:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason="Protected path collision blocked restore: {0}".format(protected_reason),
            safety_warnings=warnings,
        )
    integrity_status = str(integrity.get("status") or "")
    if integrity_status == RECYCLE_INTEGRITY_MISSING:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason="Restore is blocked because the recycled file is missing.",
            safety_warnings=warnings,
        )
    if integrity_status == RECYCLE_INTEGRITY_TAMPERED:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason="Restore is blocked because the recycled file checksum changed.",
            safety_warnings=warnings,
        )
    if integrity_status == RECYCLE_INTEGRITY_INVALID_PATH:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=None,
            blocked_reason="Restore is blocked because the recycle item path is invalid.",
            safety_warnings=warnings,
        )
    if original_path.exists():
        if _project_managed_asset_reason(original_path):
            return _build_policy_payload(
                POLICY_BLOCKED,
                requires_confirmation=False,
                required_confirmation_text=None,
                blocked_reason="Protected path collision blocked restore.",
                safety_warnings=warnings,
            )
        warnings.append("Original restore path is occupied; a safe .restored- suffix path will be used.")
    return _build_policy_payload(
        POLICY_RISKY_RESTORE,
        requires_confirmation=False,
        required_confirmation_text=None,
        blocked_reason=None,
        safety_warnings=warnings,
    )


def _build_empty_policy(
    *,
    recycle_summary: Dict[str, Any],
    allow_empty: bool,
    confirmation_text: Optional[str],
) -> Dict[str, Any]:
    warnings = [
        "Empty recycle bin permanently removes active recycled files and cannot be undone.",
    ]
    recycle_count = int(recycle_summary.get("count") or 0)
    if recycle_count <= 0:
        warnings.append("Recycle bin is currently empty.")
    if not allow_empty:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=True,
            required_confirmation_text=EMPTY_RECYCLE_CONFIRMATION_TEXT,
            blocked_reason="allow_empty=true is required before emptying the recycle bin.",
            safety_warnings=warnings,
        )
    if str(confirmation_text or "") != EMPTY_RECYCLE_CONFIRMATION_TEXT:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=True,
            required_confirmation_text=EMPTY_RECYCLE_CONFIRMATION_TEXT,
            blocked_reason='confirmation_text must exactly equal "EMPTY RECYCLE BIN".',
            safety_warnings=warnings,
        )
    return _build_policy_payload(
        POLICY_RISKY_EMPTY,
        requires_confirmation=False,
        required_confirmation_text=EMPTY_RECYCLE_CONFIRMATION_TEXT,
        blocked_reason=None,
        safety_warnings=warnings,
    )


def _build_rollback_execute_policy(
    *,
    action: str,
    rollback_level: str,
    dry_run: bool,
    allow_rollback: bool,
    confirmation_text: Optional[str],
    candidate_count: int,
) -> Dict[str, Any]:
    warnings = [
        "Rollback execution in Phase 32 is limited to cleanup snapshots and safe recycle restore candidates.",
        "Rollback never overwrites existing files; occupied original paths use safe suffix restore paths.",
    ]
    normalized_action = str(action or "")
    if normalized_action != SNAPSHOT_ACTION_CLEANUP:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
            blocked_reason="Rollback execution is only supported for cleanup snapshots.",
            safety_warnings=warnings,
        )
    if candidate_count <= 0:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
            blocked_reason="No safe rollback candidates are available for execution.",
            safety_warnings=warnings,
        )
    if rollback_level in (ROLLBACK_LEVEL_BLOCKED, ROLLBACK_LEVEL_NOT_RESTORABLE):
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=False,
            required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
            blocked_reason="Rollback plan is not restorable.",
            safety_warnings=warnings,
        )
    if bool(dry_run):
        return _build_policy_payload(
            POLICY_SAFE_READONLY,
            requires_confirmation=False,
            required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
            blocked_reason=None,
            safety_warnings=warnings,
        )
    if not bool(allow_rollback):
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=True,
            required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
            blocked_reason="allow_rollback=true is required before rollback execution.",
            safety_warnings=warnings,
        )
    if str(confirmation_text or "") != ROLLBACK_EXECUTE_CONFIRMATION_TEXT:
        return _build_policy_payload(
            POLICY_BLOCKED,
            requires_confirmation=True,
            required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
            blocked_reason='confirmation_text must exactly equal "EXECUTE ROLLBACK".',
            safety_warnings=warnings,
        )
    return _build_policy_payload(
        POLICY_RISKY_RESTORE,
        requires_confirmation=False,
        required_confirmation_text=ROLLBACK_EXECUTE_CONFIRMATION_TEXT,
        blocked_reason=None,
        safety_warnings=warnings,
    )


def _eligible_cleanup_recycle_candidates(
    summary: Dict[str, Any],
    *,
    english_root: Path,
    arabic_root: Path,
) -> List[Dict[str, Any]]:
    eligible: List[Dict[str, Any]] = []
    for candidate in list(summary.get("cleanup_candidates") or []):
        if str(candidate.get("candidate_type") or "") != "orphan_file":
            continue
        if bool(candidate.get("protected")):
            continue
        path_text = _normalize_path_text(candidate.get("path"))
        if not path_text:
            continue
        path_obj = Path(path_text)
        if not _can_recycle_file(path_obj, english_root=english_root, arabic_root=arabic_root):
            continue
        eligible.append(candidate)
    return eligible


def _build_policy_payload(
    policy_level: str,
    *,
    requires_confirmation: bool,
    required_confirmation_text: Optional[str],
    blocked_reason: Optional[str],
    safety_warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    warnings = [
        str(item).strip()
        for item in list(safety_warnings or [])
        if str(item).strip()
    ]
    deduped_warnings = list(dict.fromkeys(warnings))
    return {
        "policy_level": str(policy_level or POLICY_BLOCKED),
        "requires_confirmation": bool(requires_confirmation),
        "required_confirmation_text": _normalize_text(required_confirmation_text),
        "blocked_reason": _normalize_text(blocked_reason),
        "safety_warnings": deduped_warnings,
    }


def _build_action_readiness(
    *,
    policy: Dict[str, Any],
    ready_reason: str,
    blocked_fallback_reason: str,
) -> Dict[str, Any]:
    policy_level = str(policy.get("policy_level") or POLICY_BLOCKED)
    warnings = list(policy.get("safety_warnings") or [])
    action_ready = policy_level != POLICY_BLOCKED
    readiness_reason = (
        str(ready_reason).strip()
        if action_ready
        else str(policy.get("blocked_reason") or blocked_fallback_reason).strip()
    )
    return {
        "action_ready": bool(action_ready),
        "readiness_reason": readiness_reason[:240],
        "required_confirmation_text": _normalize_text(policy.get("required_confirmation_text")),
        "policy_level": policy_level,
        "warning_count": len([item for item in warnings if str(item).strip()]),
    }


def _status_message(status_key: str) -> str:
    return _OPERATOR_STATUS_MESSAGES.get(
        str(status_key or ""),
        "Action completed. Review the response details.",
    )


def _policy_summary_item(policy: Dict[str, Any]) -> Dict[str, Any]:
    warnings = list(policy.get("safety_warnings") or [])
    return {
        "policy_level": str(policy.get("policy_level") or POLICY_BLOCKED),
        "requires_confirmation": bool(policy.get("requires_confirmation")),
        "required_confirmation_text": _normalize_text(policy.get("required_confirmation_text")),
        "warning_count": len([item for item in warnings if str(item).strip()]),
    }


def _recommended_next_action(
    *,
    orphan_count: int,
    recycle_count: int,
    rollback_plan: Optional[Dict[str, Any]],
    risky_blocked_recently: bool,
    latest_cleanup_snapshot_id: Optional[str],
) -> Dict[str, Any]:
    rollback_candidates = len(list((rollback_plan or {}).get("candidate_items") or []))
    if risky_blocked_recently:
        return {
            "code": "risky_blocked_without_confirmation",
            "label": "Review blocked risky action",
            "reason": "A recent risky action was blocked without the exact confirmation inputs.",
            "next_endpoint": "/companion/cache-maintenance/policy",
            "snapshot_id": latest_cleanup_snapshot_id,
        }
    if rollback_candidates > 0 and latest_cleanup_snapshot_id:
        return {
            "code": "rollback_available",
            "label": "Run rollback dry-run",
            "reason": "A cleanup snapshot has safe rollback candidates available.",
            "next_endpoint": "/companion/cache-maintenance/rollback-execute",
            "snapshot_id": latest_cleanup_snapshot_id,
        }
    if recycle_count > 0:
        return {
            "code": "recycle_bin_has_items",
            "label": "Review recycle bin",
            "reason": "Recycled cache items are available for restore or confirmed empty.",
            "next_endpoint": "/companion/cache-recycle-bin",
            "snapshot_id": latest_cleanup_snapshot_id,
        }
    if orphan_count > 0:
        return {
            "code": "orphan_files_available",
            "label": "Run cleanup dry-run",
            "reason": "Orphan cache files are available for safe cleanup planning.",
            "next_endpoint": "/companion/cache-maintenance/cleanup",
            "snapshot_id": latest_cleanup_snapshot_id,
        }
    return {
        "code": "clean_cache",
        "label": "Cache is clean",
        "reason": "No orphan files or recycle backlog were detected.",
        "next_endpoint": "/companion/cache-maintenance/scan",
        "snapshot_id": latest_cleanup_snapshot_id,
    }


def _connect(db_path: PathLike) -> sqlite3.Connection:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _safe_file_size(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _checksum_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _project_managed_asset_reason(path: Path) -> Optional[str]:
    resolved = path.resolve(strict=False)
    sample_path = config.SAMPLE_SRT_PATH.resolve(strict=False)
    if resolved == sample_path:
        return "Bundled project cache asset."
    if path.name == ".gitkeep":
        return "Bundled cache placeholder file."
    return None


def _recycle_root(english_root: Path, arabic_root: Path) -> Path:
    common_parent = english_root.parent
    if arabic_root.parent == common_parent:
        return common_parent / RECYCLE_DIR_NAME
    return common_parent / RECYCLE_DIR_NAME


def _available_recycle_path(recycle_root: Path, *, filename: str) -> Path:
    candidate = recycle_root / filename
    index = 1
    while candidate.exists():
        stem = candidate.stem
        suffix = candidate.suffix
        candidate = recycle_root / "{0}_{1}{2}".format(stem, index, suffix)
        index += 1
    return candidate


def _build_available_restore_path(original_path: Path, item_id: str) -> Path:
    if not original_path.exists():
        return original_path
    safe_suffix = str(item_id or "")[:8] or "restored"
    stem = original_path.stem or original_path.name
    suffix = original_path.suffix
    candidate = original_path.with_name("{0}.restored-{1}{2}".format(stem, safe_suffix, suffix))
    index = 1
    while candidate.exists():
        candidate = original_path.with_name(
            "{0}.restored-{1}-{2}{3}".format(stem, safe_suffix, index, suffix)
        )
        index += 1
    return candidate


def _sanitize_filename(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in str(value or "file"))
    cleaned = cleaned.strip("._") or "file"
    return cleaned[:80]


def _is_path_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _is_path_within_any_root(path: Path, roots: Iterable[Path]) -> bool:
    return any(_is_path_within_root(path, root) for root in roots)


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


def _safe_relative_path(
    value: Any,
    *,
    english_root: Path,
    arabic_root: Path,
) -> Optional[str]:
    text = _normalize_path_text(value)
    if not text:
        return None
    path_obj = Path(text)
    recycle_root = _recycle_root(english_root, arabic_root)
    for prefix, root in (
        ("cache/english", english_root),
        ("cache/arabic", arabic_root),
        ("cache/.recycle", recycle_root),
        ("project", config.BASE_DIR),
    ):
        try:
            rel = path_obj.resolve(strict=False).relative_to(root.resolve(strict=False))
            rel_text = str(rel).replace("\\", "/")
            return prefix if rel_text in ("", ".") else "{0}/{1}".format(prefix, rel_text)
        except ValueError:
            continue
    return path_obj.name


def _absolute_path_from_safe_relative(
    value: Any,
    *,
    english_root: Path,
    arabic_root: Path,
) -> Optional[Path]:
    text = _normalize_text(value)
    if not text:
        return None
    normalized = str(text).replace("\\", "/")
    recycle_root = _recycle_root(english_root, arabic_root)
    prefixes = (
        ("cache/english", english_root),
        ("cache/arabic", arabic_root),
        ("cache/.recycle", recycle_root),
        ("project", config.BASE_DIR),
    )
    for prefix, root in prefixes:
        if normalized == prefix:
            return root.resolve(strict=False)
        marker = prefix + "/"
        if normalized.startswith(marker):
            suffix = normalized[len(marker) :]
            return (root / Path(suffix)).resolve(strict=False)
    # Fallback for legacy absolute values.
    try:
        return Path(normalized).resolve(strict=False)
    except OSError:
        return None


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
