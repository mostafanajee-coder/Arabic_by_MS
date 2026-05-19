"""Final regression readiness report helpers for Phase 34."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services import (
    cache_integrity,
    cache_maintenance,
    opensubtitles_service,
    provider_import_history,
    provider_quarantine,
    provider_router,
    usage_guard,
)

READINESS_READY = "ready"
READINESS_WARNING = "warning"
READINESS_BLOCKED = "blocked"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_final_readiness(
    *,
    db_path: Path,
    english_cache_dir: Path,
    arabic_cache_dir: Path,
    sample_srt_path: Path,
) -> Dict[str, Any]:
    """Build one structured read-only final readiness report."""
    blocking_issues: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    checked_sections: List[Dict[str, Any]] = []

    provider_status = provider_router.get_provider_status()
    provider_usage = usage_guard.get_provider_usage_counters(db_path)
    reliability = provider_router.get_reliability_settings()
    quarantine_items = provider_quarantine.list_entries(db_path, limit=50)
    import_history_items = provider_import_history.list_entries(db_path, limit=50)
    integrity_summary = cache_integrity.get_summary(db_path, limit=25)
    maintenance_summary = cache_maintenance.get_summary(db_path)
    recycle_summary = cache_maintenance.get_recycle_bin_summary(db_path, limit=25)
    audit_summary = cache_maintenance.get_audit_trail(db_path, limit=25)
    snapshots_summary = cache_maintenance.list_maintenance_snapshots(db_path, limit=25)
    operator_summary = cache_maintenance.get_operator_summary(
        db_path,
        english_cache_dir=english_cache_dir,
        arabic_cache_dir=arabic_cache_dir,
        refresh_scan=False,
    )

    configured_provider_count = sum(
        1
        for key in ("subdl", "subsource", "opensubtitles")
        if bool((provider_status.get(key) or {}).get("configured"))
    )
    unavailable_providers = [
        key
        for key in ("subdl", "subsource", "opensubtitles")
        if not bool((provider_status.get(key) or {}).get("configured"))
    ]
    if configured_provider_count == 0:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="provider_configuration",
            readiness_status=READINESS_WARNING,
            message="No subtitle provider API is configured.",
            details={
                "configured_provider_count": configured_provider_count,
                "unavailable_providers": unavailable_providers,
                "gemini_configured": bool((provider_status.get("gemini") or {}).get("configured")),
            },
        )
    elif unavailable_providers:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="provider_configuration",
            readiness_status=READINESS_WARNING,
            message="Some providers are not configured.",
            details={
                "configured_provider_count": configured_provider_count,
                "unavailable_providers": unavailable_providers,
                "gemini_configured": bool((provider_status.get("gemini") or {}).get("configured")),
            },
        )
    else:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="provider_configuration",
            readiness_status=READINESS_READY,
            message="All configured providers are available.",
            details={
                "configured_provider_count": configured_provider_count,
                "gemini_configured": bool((provider_status.get("gemini") or {}).get("configured")),
            },
        )

    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="provider_diagnostics",
        readiness_status=READINESS_READY,
        message="Provider diagnostics are readable.",
        details={
            "provider_usage_counters": provider_usage,
            "retry_settings": (reliability.get("retries") or {}),
        },
    )

    parent_guard = bool(opensubtitles_service._uses_parent_imdb_id("series", 1, 1))
    movie_guard = not bool(opensubtitles_service._uses_parent_imdb_id("movie", None, None))
    opensubtitles_guard_ok = parent_guard and movie_guard
    os_details = {
        "configured": bool((provider_status.get("opensubtitles") or {}).get("configured")),
        "requires_parent_imdb_id_for_series_episode": parent_guard,
        "uses_imdb_id_for_movie_lookup": movie_guard,
        "search_path": opensubtitles_service.SEARCH_PATH,
        "download_path": opensubtitles_service.DOWNLOAD_PATH,
    }
    if not opensubtitles_guard_ok:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="opensubtitles_behavior",
            readiness_status=READINESS_BLOCKED,
            message="OpenSubtitles Phase 17 guardrails did not pass validation.",
            details=os_details,
        )
    elif not os_details["configured"]:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="opensubtitles_behavior",
            readiness_status=READINESS_WARNING,
            message="OpenSubtitles is disabled but guardrails are intact.",
            details=os_details,
        )
    else:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="opensubtitles_behavior",
            readiness_status=READINESS_READY,
            message="OpenSubtitles guardrails are intact.",
            details=os_details,
        )

    fallback_limit = int(provider_router.IMPORT_BEST_FALLBACK_LIMIT)
    if fallback_limit <= 0:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="search_import_best",
            readiness_status=READINESS_BLOCKED,
            message="Import-best fallback limit is invalid.",
            details={"import_best_fallback_limit": fallback_limit},
        )
    elif configured_provider_count == 0:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="search_import_best",
            readiness_status=READINESS_WARNING,
            message="Import-best is available but provider search is currently disabled.",
            details={"import_best_fallback_limit": fallback_limit},
        )
    else:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="search_import_best",
            readiness_status=READINESS_READY,
            message="Import-best fallback workflow is available.",
            details={"import_best_fallback_limit": fallback_limit},
        )

    if fallback_limit < 2:
        quality_status = READINESS_WARNING
        quality_message = "Quality fallback is enabled with a minimal candidate window."
    else:
        quality_status = READINESS_READY
        quality_message = "Quality fallback candidate window is available."
    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="quality_fallback",
        readiness_status=quality_status,
        message=quality_message,
        details={"import_best_fallback_limit": fallback_limit},
    )

    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="quarantine",
        readiness_status=READINESS_READY,
        message="Quarantine memory is readable.",
        details={
            "quarantine_threshold": provider_quarantine.QUARANTINE_THRESHOLD,
            "quarantine_item_count": len(quarantine_items),
        },
    )

    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="import_history",
        readiness_status=READINESS_READY,
        message="Import history is readable.",
        details={"import_history_count": len(import_history_items)},
    )

    valid_local_count = int((integrity_summary.get("counts") or {}).get(cache_integrity.STATUS_VALID, 0))
    if valid_local_count <= 0:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="local_first_reuse",
            readiness_status=READINESS_WARNING,
            message="No currently valid local-first reuse candidates are recorded.",
            details={"valid_integrity_records": valid_local_count},
        )
    else:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="local_first_reuse",
            readiness_status=READINESS_READY,
            message="Local-first reuse has valid cache records available.",
            details={"valid_integrity_records": valid_local_count},
        )

    integrity_counts = dict(integrity_summary.get("counts") or {})
    bad_integrity_total = (
        int(integrity_counts.get(cache_integrity.STATUS_MISSING_FILE, 0))
        + int(integrity_counts.get(cache_integrity.STATUS_INVALID_SRT, 0))
        + int(integrity_counts.get(cache_integrity.STATUS_UNREADABLE_FILE, 0))
    )
    integrity_status = READINESS_WARNING if bad_integrity_total > 0 else READINESS_READY
    integrity_message = (
        "Cache integrity has issues that need review."
        if bad_integrity_total > 0
        else "Cache integrity metadata is stable."
    )
    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="cache_integrity",
        readiness_status=integrity_status,
        message=integrity_message,
        details={
            "counts": integrity_counts,
            "last_scan_at": integrity_summary.get("last_scan_at"),
        },
    )

    sample_exists = bool(sample_srt_path.exists() and sample_srt_path.is_file())
    orphan_count = len(list(maintenance_summary.get("orphan_files") or []))
    maintenance_details = {
        "sample_cache_asset_present": sample_exists,
        "orphan_files": orphan_count,
        "cleanup_candidates": len(list(maintenance_summary.get("cleanup_candidates") or [])),
        "protected_files": len(list(maintenance_summary.get("protected_files") or [])),
    }
    if not sample_exists:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="cache_maintenance",
            readiness_status=READINESS_BLOCKED,
            message="Bundled sample cache asset is missing.",
            details=maintenance_details,
        )
    elif orphan_count > 0:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="cache_maintenance",
            readiness_status=READINESS_WARNING,
            message="Cache maintenance found orphan files that require review.",
            details=maintenance_details,
        )
    else:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="cache_maintenance",
            readiness_status=READINESS_READY,
            message="Cache maintenance baseline is stable.",
            details=maintenance_details,
        )

    recycle_count = int(recycle_summary.get("count") or 0)
    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="recycle_bin",
        readiness_status=READINESS_WARNING if recycle_count > 0 else READINESS_READY,
        message=(
            "Recycle bin has items pending restore or confirmed empty."
            if recycle_count > 0
            else "Recycle bin is clear."
        ),
        details={
            "active_items": recycle_count,
            "total_bytes": int(recycle_summary.get("total_bytes") or 0),
            "last_recycled_at": recycle_summary.get("last_recycled_at"),
        },
    )

    audit_count = int(audit_summary.get("count") or 0)
    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="audit_trail",
        readiness_status=READINESS_WARNING if audit_count <= 0 else READINESS_READY,
        message=(
            "No maintenance audit entries are recorded yet."
            if audit_count <= 0
            else "Maintenance audit trail is available."
        ),
        details={"audit_count": audit_count},
    )

    snapshots_count = int(snapshots_summary.get("count") or 0)
    _add_section(
        checked_sections,
        blocking_issues,
        warnings,
        section="snapshots",
        readiness_status=READINESS_WARNING if snapshots_count <= 0 else READINESS_READY,
        message=(
            "No maintenance snapshots are recorded yet."
            if snapshots_count <= 0
            else "Maintenance snapshots are available."
        ),
        details={"snapshot_count": snapshots_count},
    )

    latest_cleanup_snapshot_id = _latest_cleanup_snapshot_id(snapshots_summary.get("items") or [])
    rollback_plan: Optional[Dict[str, Any]] = None
    if latest_cleanup_snapshot_id:
        try:
            rollback_plan = cache_maintenance.build_snapshot_rollback_plan(
                db_path,
                snapshot_id=latest_cleanup_snapshot_id,
                english_cache_dir=english_cache_dir,
                arabic_cache_dir=arabic_cache_dir,
            )
        except Exception:
            rollback_plan = None

    if rollback_plan is None:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="rollback_plan",
            readiness_status=READINESS_WARNING,
            message="No cleanup snapshot is available for rollback planning.",
            details={"snapshot_id": latest_cleanup_snapshot_id},
        )
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="rollback_execution",
            readiness_status=READINESS_WARNING,
            message="Rollback execution readiness is unavailable without a cleanup snapshot.",
            details={"snapshot_id": latest_cleanup_snapshot_id},
        )
    else:
        rollback_supported = bool(rollback_plan.get("rollback_supported"))
        rollback_candidates = len(list(rollback_plan.get("candidate_items") or []))
        rollback_level = str(rollback_plan.get("rollback_level") or "")
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="rollback_plan",
            readiness_status=READINESS_READY if rollback_supported else READINESS_WARNING,
            message=(
                "Rollback plan metadata is available."
                if rollback_supported
                else "Rollback plan is currently not supported for this snapshot."
            ),
            details={
                "snapshot_id": latest_cleanup_snapshot_id,
                "rollback_level": rollback_level,
                "candidate_count": rollback_candidates,
            },
        )
        execute_policy = cache_maintenance._build_rollback_execute_policy(
            action=cache_maintenance.SNAPSHOT_ACTION_CLEANUP,
            rollback_level=rollback_level,
            dry_run=True,
            allow_rollback=False,
            confirmation_text=None,
            candidate_count=rollback_candidates,
        )
        exec_ready = str(execute_policy.get("policy_level") or "") != cache_maintenance.POLICY_BLOCKED
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="rollback_execution",
            readiness_status=READINESS_READY if exec_ready else READINESS_WARNING,
            message=(
                "Rollback execution dry-run readiness is available."
                if exec_ready
                else str(
                    execute_policy.get("blocked_reason")
                    or "Rollback execution is currently blocked."
                )
            ),
            details={
                "snapshot_id": latest_cleanup_snapshot_id,
                "policy_level": execute_policy.get("policy_level"),
                "required_confirmation_text": execute_policy.get("required_confirmation_text"),
            },
        )

    operator_required = {
        "cache_integrity_counts",
        "maintenance_counts",
        "recycle_bin_counts",
        "latest_audit_items",
        "latest_snapshots",
        "pending_risky_actions",
        "safety_policy_summary",
        "recommended_next_action",
    }
    if operator_required.issubset(set(operator_summary.keys())):
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="operator_summary",
            readiness_status=READINESS_READY,
            message="Operator summary is available with required safety sections.",
            details={
                "pending_risky_actions_count": len(list(operator_summary.get("pending_risky_actions") or [])),
                "recommended_next_action": (
                    (operator_summary.get("recommended_next_action") or {}).get("code")
                ),
            },
        )
    else:
        _add_section(
            checked_sections,
            blocking_issues,
            warnings,
            section="operator_summary",
            readiness_status=READINESS_BLOCKED,
            message="Operator summary is missing required sections.",
            details={
                "present_keys": sorted(list(operator_summary.keys())),
            },
        )

    blocked_count = len(blocking_issues)
    warning_count = len(warnings)
    readiness_score = max(0, 100 - blocked_count * 12 - warning_count * 4)
    readiness_status = (
        READINESS_BLOCKED
        if blocked_count
        else (READINESS_WARNING if warning_count else READINESS_READY)
    )
    return {
        "readiness_status": readiness_status,
        "readiness_score": readiness_score,
        "blocking_issues": blocking_issues,
        "warnings": warnings,
        "checked_sections": checked_sections,
        "generated_at": _utcnow_iso(),
    }


def _latest_cleanup_snapshot_id(items: List[Dict[str, Any]]) -> Optional[str]:
    for item in list(items or []):
        if str(item.get("action") or "") == cache_maintenance.SNAPSHOT_ACTION_CLEANUP:
            value = str(item.get("snapshot_id") or "").strip()
            if value:
                return value
    return None


def _add_section(
    checked_sections: List[Dict[str, Any]],
    blocking_issues: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    *,
    section: str,
    readiness_status: str,
    message: str,
    details: Dict[str, Any],
) -> None:
    status = (
        READINESS_BLOCKED
        if readiness_status == READINESS_BLOCKED
        else (READINESS_WARNING if readiness_status == READINESS_WARNING else READINESS_READY)
    )
    payload = {
        "section": str(section or "").strip(),
        "readiness_status": status,
        "message": str(message or "").strip(),
        "details": dict(details or {}),
    }
    checked_sections.append(payload)
    if status == READINESS_BLOCKED:
        blocking_issues.append({"section": payload["section"], "message": payload["message"]})
    elif status == READINESS_WARNING:
        warnings.append({"section": payload["section"], "message": payload["message"]})
