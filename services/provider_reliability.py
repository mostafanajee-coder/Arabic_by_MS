"""Shared provider retry and error-normalization helpers."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence, TypeVar

RETRYABLE_HTTP_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_RETRY_COUNT = 1
DEFAULT_SEARCH_TIMEOUT = 30.0
DEFAULT_DOWNLOAD_TIMEOUT = 60.0

T = TypeVar("T")


def get_retry_settings() -> Dict[str, Any]:
    """Return the safe shared retry settings exposed to diagnostics/UI."""
    return {
        "max_retries": DEFAULT_RETRY_COUNT,
        "retryable_http_statuses": list(RETRYABLE_HTTP_STATUSES),
        "default_search_timeout_seconds": DEFAULT_SEARCH_TIMEOUT,
        "default_download_timeout_seconds": DEFAULT_DOWNLOAD_TIMEOUT,
    }


def make_provider_error(
    error_cls: type[Exception],
    *,
    provider: str,
    operation: str,
    message: str,
    error_type: str,
    http_status: Optional[int] = None,
    retryable: bool = False,
) -> Exception:
    """Create one provider exception with normalized diagnostic metadata."""
    exc = error_cls(message)
    setattr(exc, "provider_name", provider)
    setattr(exc, "operation", operation)
    setattr(exc, "error_type", error_type)
    setattr(exc, "http_status", http_status)
    setattr(exc, "retryable", retryable)
    setattr(exc, "safe_message", message)
    return exc


def run_with_retries(
    func: Callable[[], T],
    *,
    retries: int = DEFAULT_RETRY_COUNT,
) -> T:
    """Retry only transient provider failures a small number of times."""
    attempts = max(0, int(retries or 0)) + 1
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:  # pragma: no cover - exercised via provider tests
            last_error = exc
            if attempt >= attempts - 1 or not is_retryable_error(exc):
                raise
    assert last_error is not None
    raise last_error


def is_retryable_error(exc: Exception) -> bool:
    """Return whether an exception represents a transient provider failure."""
    return bool(getattr(exc, "retryable", False))


def raise_for_http_status(
    response: Any,
    *,
    provider_label: str,
    operation: str,
    error_cls: type[Exception],
) -> None:
    """Raise a safe provider exception for non-200 responses."""
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 200:
        return
    raise make_provider_error(
        error_cls,
        provider=provider_label.lower(),
        operation=operation,
        message=_http_status_message(provider_label, operation, status),
        error_type=_http_error_type(status),
        http_status=status or None,
        retryable=status in RETRYABLE_HTTP_STATUSES,
    )


def normalize_provider_error(provider: str, exc: Exception) -> Dict[str, Any]:
    """Return one structured provider error payload safe for API/UI output."""
    provider_name = str(getattr(exc, "provider_name", provider) or provider).strip().lower()
    error_type = str(getattr(exc, "error_type", "") or "").strip().lower() or "provider_error"
    http_status = getattr(exc, "http_status", None)
    message = _safe_message(exc)
    payload: Dict[str, Any] = {
        "provider": provider_name,
        "error_type": error_type,
        "message": message,
    }
    if http_status not in (None, ""):
        payload["http_status"] = int(http_status)
    else:
        payload["http_status"] = None
    return payload


def summarize_provider_errors(
    provider_errors: Dict[str, Any],
    *,
    providers: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Return a short safe message summarizing one or more provider failures."""
    if not provider_errors:
        return None
    ordered = list(providers or provider_errors.keys())
    parts = []
    for provider in ordered:
        details = provider_errors.get(provider)
        if not isinstance(details, dict):
            continue
        label = _provider_label(str(details.get("provider") or provider or "").strip().lower())
        message = str(details.get("message") or "").strip()
        if not label or not message:
            continue
        parts.append("{0}: {1}".format(label, message))
    if not parts:
        return None
    return "Provider issues: {0}".format(" | ".join(parts))


def _http_error_type(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (500, 502, 503, 504):
        return "transient_http_error"
    if status == 400:
        return "bad_request"
    if status in (401, 403):
        return "unauthorized"
    return "http_error"


def _http_status_message(provider_label: str, operation: str, status: int) -> str:
    action = operation or "request"
    if status == 429:
        return "{0} {1} is temporarily rate limited (HTTP 429).".format(provider_label, action)
    if status in (500, 502, 503, 504):
        return "{0} {1} is temporarily unavailable (HTTP {2}).".format(
            provider_label,
            action,
            status,
        )
    if status == 400:
        return "{0} {1} request was rejected (HTTP 400).".format(provider_label, action)
    if status in (401, 403):
        return "{0} {1} is not authorized (HTTP {2}).".format(provider_label, action, status)
    if status == 404:
        return "{0} {1} was not found (HTTP 404).".format(provider_label, action)
    return "{0} {1} failed (HTTP {2}).".format(provider_label, action, status)


def _safe_message(exc: Exception) -> str:
    message = str(getattr(exc, "safe_message", "") or "").strip()
    if message:
        return message
    text = str(exc or "").strip()
    return text or "Provider request failed."


def _provider_label(provider: str) -> str:
    if provider == "subdl":
        return "SubDL"
    if provider == "subsource":
        return "SubSource"
    if provider == "opensubtitles":
        return "OpenSubtitles"
    return provider
