"""Cleanup and validation helpers for translated Arabic SRT content."""

from __future__ import annotations

import re
from typing import Iterable, List

from utils.srt_chunker import SRTEntry


class SRTQualityError(ValueError):
    """Raised when translated subtitle output fails quality checks."""


_LABEL_RE = re.compile(
    r"^\s*(?:arabic|translation|translated|translated text|answer)\s*[:\-]\s*",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_translation_text(text: str) -> str:
    """Remove common Gemini formatting mistakes from one translated cue."""
    cleaned = (text or "").replace("```", " ").replace("`", " ").strip()
    while True:
        updated = _LABEL_RE.sub("", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def validate_translated_entries(
    english_entries: Iterable[SRTEntry],
    translated_entries: Iterable[SRTEntry],
) -> List[SRTEntry]:
    """Return cleaned translated entries or raise if output is unusable."""
    expected = list(english_entries)
    actual = list(translated_entries)

    if len(expected) != len(actual):
        raise SRTQualityError(
            "Translated subtitle block count does not match the English source."
        )

    cleaned_entries: List[SRTEntry] = []
    for source, translated in zip(expected, actual):
        if source.index != translated.index:
            raise SRTQualityError(
                "Translated subtitle index {0} does not match source index {1}.".format(
                    translated.index,
                    source.index,
                )
            )
        if source.timestamp != translated.timestamp:
            raise SRTQualityError(
                "Translated subtitle timestamp mismatch for index {0}.".format(
                    source.index
                )
            )

        cleaned_text = clean_translation_text(translated.text)
        if not cleaned_text:
            raise SRTQualityError(
                "Translated subtitle block {0} is empty.".format(source.index)
            )
        if "```" in cleaned_text:
            raise SRTQualityError(
                "Translated subtitle block {0} still contains Markdown fences.".format(
                    source.index
                )
            )
        if _HTML_TAG_RE.search(cleaned_text):
            raise SRTQualityError(
                "Translated subtitle block {0} contains HTML tags.".format(
                    source.index
                )
            )

        cleaned_entries.append(
            SRTEntry(
                index=source.index,
                timestamp=source.timestamp,
                text=cleaned_text,
            )
        )
    return cleaned_entries
