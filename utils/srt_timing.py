"""Safe subtitle timing adjustments for SRT files."""

from __future__ import annotations

import re
from typing import List, Tuple

from .srt_chunker import SRTParseError, parse_srt

_TIMESTAMP_LINE_RE = re.compile(
    r"^(?P<prefix>\s*)"
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    r"(?P<arrow>\s*-->\s*)"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    r"(?P<tail>.*)$"
)


class SRTTimingError(ValueError):
    """Raised when SRT timing could not be parsed or shifted safely."""


def parse_timestamp_to_ms(timestamp: str) -> int:
    """Convert one SRT timestamp into milliseconds."""
    text = str(timestamp or "").strip().replace(".", ",")
    parts = text.split(":")
    if len(parts) != 3:
        raise SRTTimingError("Invalid SRT timestamp.")
    seconds_part, milliseconds_part = parts[2].split(",", 1)
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(seconds_part)
        milliseconds = int(milliseconds_part.ljust(3, "0")[:3])
    except (TypeError, ValueError) as exc:
        raise SRTTimingError("Invalid SRT timestamp.") from exc
    if min(hours, minutes, seconds, milliseconds) < 0:
        raise SRTTimingError("Invalid SRT timestamp.")
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + milliseconds


def format_timestamp_ms(total_ms: int) -> str:
    """Convert milliseconds back to SRT timestamp text."""
    if total_ms < 0:
        raise SRTTimingError("Offset would create a negative SRT timestamp.")
    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return "{0:02d}:{1:02d}:{2:02d},{3:03d}".format(
        hours,
        minutes,
        seconds,
        milliseconds,
    )


def shift_timestamp_line(timestamp_line: str, offset_ms: int) -> str:
    """Shift a single SRT timestamp line by `offset_ms`."""
    match = _TIMESTAMP_LINE_RE.match(str(timestamp_line or ""))
    if not match:
        raise SRTTimingError("Invalid SRT timestamp line.")

    start_ms = parse_timestamp_to_ms(match.group("start")) + int(offset_ms)
    end_ms = parse_timestamp_to_ms(match.group("end")) + int(offset_ms)
    if start_ms < 0 or end_ms < 0:
        raise SRTTimingError("Offset would create a negative SRT timestamp.")

    return "{0}{1}{2}{3}{4}".format(
        match.group("prefix"),
        format_timestamp_ms(start_ms),
        match.group("arrow"),
        format_timestamp_ms(end_ms),
        match.group("tail"),
    )


def shift_srt_content(content: str, offset_ms: int) -> str:
    """Shift every cue timestamp in an SRT string while preserving text."""
    normalized = str(content or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    _validate_srt_structure(normalized)

    shifted_lines: List[str] = []
    shifted_count = 0
    for line in normalized.split("\n"):
        if _TIMESTAMP_LINE_RE.match(line):
            shifted_lines.append(shift_timestamp_line(line, offset_ms))
            shifted_count += 1
        else:
            shifted_lines.append(line)

    if shifted_count <= 0:
        raise SRTTimingError("No SRT timestamps found to adjust.")
    return "\n".join(shifted_lines)


def _validate_srt_structure(content: str) -> None:
    """Reject clearly malformed SRT input before shifting timestamps."""
    try:
        parse_srt(content)
    except SRTParseError as exc:
        raise SRTTimingError("Invalid SRT content.") from exc

    blocks = re.split(r"\n\s*\n", content.strip())
    valid_blocks = 0
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timestamp_indexes: List[int] = [
            index for index, line in enumerate(lines) if _TIMESTAMP_LINE_RE.match(line)
        ]
        if len(timestamp_indexes) != 1:
            raise SRTTimingError("Invalid SRT block structure.")
        timestamp_index = timestamp_indexes[0]
        if timestamp_index > 1:
            raise SRTTimingError("Invalid SRT block structure.")
        if timestamp_index == 1 and not lines[0].strip().isdigit():
            raise SRTTimingError("Invalid SRT block index.")
        valid_blocks += 1

    if valid_blocks <= 0:
        raise SRTTimingError("Invalid SRT content.")
