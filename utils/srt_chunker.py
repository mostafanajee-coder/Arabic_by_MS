"""Parse / render / chunk SRT files for the translation pipeline.

The SRT format is a sequence of blocks separated by blank lines:

    1
    00:00:01,000 --> 00:00:04,000
    Hello world
    (optionally more text lines)

    2
    00:00:05,000 --> 00:00:08,000
    Second line

We keep timestamps and numbering as opaque strings — only the text body
is sent to the translator — so the Arabic output is guaranteed to land
on exactly the same cues as the English input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional


_TS_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}.*$"
)


@dataclass
class SRTEntry:
    """One subtitle cue."""

    index: int
    timestamp: str  # raw "HH:MM:SS,mmm --> HH:MM:SS,mmm" (and any tail)
    text: str       # may contain '\n' for multi-line cues


class SRTParseError(ValueError):
    """Raised when the input doesn't look like a usable SRT file."""


def parse_srt(content: str) -> List[SRTEntry]:
    """Parse an SRT string into a list of SRTEntry objects."""
    # Normalize line endings and strip BOM.
    text = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    entries: List[SRTEntry] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue

        # Find the timestamp line; everything before it is treated as the
        # numeric index (some files omit the index line, so we're tolerant).
        ts_idx = None
        for i, line in enumerate(lines):
            if _TS_RE.match(line):
                ts_idx = i
                break
        if ts_idx is None:
            # Block with no timestamp -> skip silently rather than fail
            # the whole file.
            continue

        # Index: explicit if present on the first line, otherwise sequential.
        if ts_idx > 0 and lines[ts_idx - 1].strip().isdigit():
            index = int(lines[ts_idx - 1].strip())
        else:
            index = len(entries) + 1

        timestamp = lines[ts_idx].strip()
        body = "\n".join(lines[ts_idx + 1 :]).strip()
        entries.append(SRTEntry(index=index, timestamp=timestamp, text=body))

    if not entries:
        raise SRTParseError("No SRT cues could be parsed from input")
    return entries


def render_srt(entries: Iterable[SRTEntry]) -> str:
    """Render a sequence of SRTEntry back to standard SRT text."""
    parts: List[str] = []
    for e in entries:
        # Preserve the original cue index and timestamp exactly.
        parts.append(f"{e.index}\n{e.timestamp}\n{e.text}".rstrip())
    # Trailing newline keeps players happy.
    return "\n\n".join(parts) + "\n"


def chunk_entries(
    entries: List[SRTEntry],
    size: int = 20,
    *,
    max_chars: Optional[int] = 1800,
) -> Iterator[List[SRTEntry]]:
    """Yield translation-safe chunks bounded by entry count and text size."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if max_chars is not None and max_chars <= 0:
        raise ValueError("max_chars must be positive when provided")

    chunk: List[SRTEntry] = []
    chunk_chars = 0
    for entry in entries:
        entry_chars = len(" ".join(entry.text.split()))
        would_exceed_size = len(chunk) >= size
        would_exceed_chars = (
            bool(chunk)
            and max_chars is not None
            and chunk_chars + entry_chars > max_chars
        )
        if would_exceed_size or would_exceed_chars:
            yield chunk
            chunk = []
            chunk_chars = 0
        chunk.append(entry)
        chunk_chars += entry_chars
    if chunk:
        yield chunk
