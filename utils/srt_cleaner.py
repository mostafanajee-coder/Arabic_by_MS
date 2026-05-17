"""Parse Gemini's numbered translation output back into per-cue strings.

Gemini is asked to reply with one line per cue in the form:

    1) Arabic text for cue 1
    2) Arabic text for cue 2
    ...

In practice it sometimes wraps the answer in code fences or adds a stray
preface line. This module is intentionally tolerant: it scans every line,
keeps the ones matching the numbered shape, and ignores the rest.
"""

from __future__ import annotations

import re
from typing import Dict


class TranslationFormatError(ValueError):
    """Raised when Gemini's reply can't be parsed as numbered translations."""


# Matches lines like "1) text", "1. text", "1: text", "1- text",
# with optional leading whitespace.
_LINE_RE = re.compile(r"^\s*(\d+)\s*[\)\.\:\-]\s*(.+?)\s*$")


def parse_numbered_translations(raw: str) -> Dict[int, str]:
    """Return a {index: translated_text} dict parsed from Gemini's output.

    Raises TranslationFormatError if the output contains no recognizable
    numbered lines at all.
    """
    if not raw or not raw.strip():
        raise TranslationFormatError("Empty translation output")

    out: Dict[int, str] = {}
    for line in raw.splitlines():
        # Skip Markdown code fences.
        if line.strip().startswith("```"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        out[idx] = m.group(2).strip()

    if not out:
        raise TranslationFormatError(
            "Translation output has no numbered lines (expected e.g. '1) ...')"
        )
    return out


def assert_complete(translations: Dict[int, str], expected_indices: list[int]) -> None:
    """Raise TranslationFormatError if any expected index is missing."""
    missing = [i for i in expected_indices if i not in translations]
    if missing:
        raise TranslationFormatError(
            f"Translation output missing entries for indices: {missing[:10]}"
            + (" …" if len(missing) > 10 else "")
        )
