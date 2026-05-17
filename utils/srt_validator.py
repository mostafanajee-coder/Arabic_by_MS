"""SRT validation helpers.

Phase 2 only needs basic sanity checks:
- filename must end with .srt (case insensitive)
- bytes must not be empty
- decoded text must contain at least one timestamp line (contains "-->")
- always return the content re-encoded as UTF-8 text
"""

from __future__ import annotations

from typing import Optional, Tuple


class SRTValidationError(ValueError):
    """Raised when an uploaded SRT file fails validation."""


# Encodings we try after UTF-8 has failed. UTF-16 is intentionally only
# attempted when the file starts with a BOM, because raw UTF-16 decoding
# happily turns any byte sequence into garbage characters.
_NON_UTF8_FALLBACKS: Tuple[str, ...] = (
    "cp1252",
    "latin-1",
)


def validate_srt_filename(filename: str) -> None:
    """Ensure the uploaded filename has the .srt extension."""
    if not filename:
        raise SRTValidationError("Missing filename")
    if not filename.lower().endswith(".srt"):
        raise SRTValidationError("Only .srt files are accepted")


def _try_decode(data: bytes) -> Optional[str]:
    # UTF-8 (with or without BOM) first — most common case.
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue

    # UTF-16 only when we see an explicit BOM.
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass

    # Last-resort single-byte fallbacks.
    for enc in _NON_UTF8_FALLBACKS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue

    return None


def validate_srt_content(data: bytes) -> str:
    """Validate SRT bytes and return them decoded as a UTF-8 string.

    Raises:
        SRTValidationError: if the file is empty, can't be decoded, or
        doesn't contain a timestamp line.
    """
    if not data or not data.strip():
        raise SRTValidationError("SRT file is empty")

    text = _try_decode(data)
    if text is None:
        raise SRTValidationError("Could not decode SRT file as text")

    if "-->" not in text:
        raise SRTValidationError(
            "SRT file has no timestamp lines (missing '-->')"
        )

    # Normalize line endings to \n so the on-disk file is consistent.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text
