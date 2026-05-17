"""Tests for utils/srt_validator.py."""

from __future__ import annotations

import pytest

from utils.srt_validator import (
    SRTValidationError,
    validate_srt_content,
    validate_srt_filename,
)


VALID_SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Hello world\n"
    "\n"
    "2\n"
    "00:00:05,000 --> 00:00:08,000\n"
    "Second line\n"
)


# ---- filename ------------------------------------------------------------


def test_validate_srt_filename_accepts_srt() -> None:
    validate_srt_filename("movie.srt")
    validate_srt_filename("MOVIE.SRT")
    validate_srt_filename("path/to/file.srt")


def test_validate_srt_filename_rejects_non_srt() -> None:
    with pytest.raises(SRTValidationError):
        validate_srt_filename("movie.txt")
    with pytest.raises(SRTValidationError):
        validate_srt_filename("movie.ass")
    with pytest.raises(SRTValidationError):
        validate_srt_filename("")


# ---- content -------------------------------------------------------------


def test_validate_srt_content_accepts_valid_utf8() -> None:
    text = validate_srt_content(VALID_SRT.encode("utf-8"))
    assert "-->" in text
    assert "Hello world" in text


def test_validate_srt_content_rejects_empty() -> None:
    with pytest.raises(SRTValidationError):
        validate_srt_content(b"")
    with pytest.raises(SRTValidationError):
        validate_srt_content(b"   \n  \n")


def test_validate_srt_content_rejects_missing_timestamp() -> None:
    with pytest.raises(SRTValidationError):
        validate_srt_content(b"just some text without arrows")


def test_validate_srt_content_handles_bom() -> None:
    # UTF-8 BOM in front of a valid file should still decode.
    text = validate_srt_content(b"\xef\xbb\xbf" + VALID_SRT.encode("utf-8"))
    assert "Hello world" in text


def test_validate_srt_content_falls_back_to_non_utf8_encoding() -> None:
    # Bytes that are NOT valid UTF-8 (0xe9 is é in latin-1 / cp1252, but
    # invalid as a standalone UTF-8 byte). The validator must still decode
    # via one of its fallback encodings and return a UTF-8-encodable string.
    raw = b"1\n00:00:01,000 --> 00:00:02,000\nCaf\xe9 time\n"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # sanity check: really not UTF-8

    text = validate_srt_content(raw)
    assert "Caf" in text
    assert "-->" in text
    # And the returned string must encode cleanly as UTF-8.
    text.encode("utf-8")
