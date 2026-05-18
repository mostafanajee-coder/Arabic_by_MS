"""Cleanup, validation, and safe quality analysis helpers for SRT content."""

from __future__ import annotations

import re
from math import ceil
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.srt_chunker import SRTEntry


class SRTQualityError(ValueError):
    """Raised when translated subtitle output fails quality checks."""


_LABEL_RE = re.compile(
    r"^\s*(?:arabic|translation|translated|translated text|answer)\s*[:\-]\s*",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TIMESTAMP_RE = re.compile(
    r"^\s*(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})(?:\s+.*)?$"
)
_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
_LINE_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)

_MIN_DURATION_MS = 400
_MAX_DURATION_MS = 20_000
_TINY_FILE_BYTES = 80
_HUGE_FILE_BYTES = 500_000
_HUGE_CUE_COUNT = 5_000


def analyze_srt_quality(
    text: str,
    *,
    expected_language: str = "en",
) -> Dict[str, Any]:
    """Analyze subtitle quality without hard-blocking by default."""
    normalized = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    utf8_size = len(normalized.encode("utf-8"))
    cues = _parse_quality_cues(normalized)
    cue_count = len(cues)

    warnings: List[str] = []
    penalty = 0
    severe = False

    empty_cues = sum(1 for cue in cues if not cue["text"].strip())
    if cue_count == 0 or empty_cues == cue_count:
        warnings.append("Subtitle content is empty.")
        penalty += 60
        severe = True
    elif empty_cues:
        warnings.append(
            "Subtitle contains {0} empty cue(s).".format(empty_cues)
        )
        penalty += min(24, empty_cues * 6)

    numbering_issues, numbering_penalty = _analyze_numbering_issues(cues)
    if numbering_issues:
        warnings.append(
            "Subtitle cue numbering looks invalid in {0} place(s).".format(numbering_issues)
        )
        penalty += numbering_penalty

    malformed_timestamps = sum(1 for cue in cues if cue["timestamp_error"])
    if malformed_timestamps:
        warnings.append(
            "Subtitle has malformed timestamp data in {0} cue(s).".format(
                malformed_timestamps
            )
        )
        penalty += min(45, 20 + malformed_timestamps * 8)
        severe = True

    overlap_count = _count_overlaps(cues)
    if overlap_count:
        warnings.append(
            "Subtitle has overlapping cues ({0}).".format(overlap_count)
        )
        penalty += min(35, 15 + overlap_count * 7)
        severe = True

    short_count, long_count = _count_duration_issues(cues)
    if short_count:
        warnings.append(
            "Subtitle has {0} extremely short cue(s).".format(short_count)
        )
        penalty += min(12, short_count * 2)
    if long_count:
        warnings.append(
            "Subtitle has {0} extremely long cue(s).".format(long_count)
        )
        penalty += min(12, long_count * 2)

    repeated_lines = _repeated_line_warning(cues)
    if repeated_lines:
        warnings.append(repeated_lines)
        penalty += 12

    if _looks_like_wrong_language(cues, expected_language):
        warnings.append(
            "Subtitle language does not look like {0}.".format(
                _normalize_expected_language(expected_language)
            )
        )
        penalty += 25
        severe = True

    if utf8_size < _TINY_FILE_BYTES:
        warnings.append("Subtitle file looks suspiciously tiny.")
        penalty += 20
        severe = True

    if utf8_size > _HUGE_FILE_BYTES or cue_count > _HUGE_CUE_COUNT:
        warnings.append("Subtitle file looks suspiciously large.")
        penalty += 12

    quality_score = max(0, 100 - penalty)
    quality_level = _quality_level_for_score(quality_score)
    reject_hint = severe or quality_level == "bad"
    return {
        "quality_score": quality_score,
        "quality_level": quality_level,
        "quality_warnings": warnings,
        "reject_hint": bool(reject_hint),
    }


def quality_warning_message(quality: Dict[str, Any]) -> Optional[str]:
    """Return a short warning summary when the analyzed subtitle looks risky."""
    if not quality:
        return None
    warnings = [str(item) for item in (quality.get("quality_warnings") or []) if str(item).strip()]
    if not warnings:
        return None
    level = str(quality.get("quality_level") or "warning")
    score = int(quality.get("quality_score") or 0)
    prefix = "Imported subtitle quality is {0} (score {1}/100).".format(level, score)
    if bool(quality.get("reject_hint")):
        prefix += " Review before translating."
    return prefix + " " + warnings[0]


def merge_quality_metadata(payload: Dict[str, Any], quality: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of `payload` with the standard quality fields attached."""
    merged = dict(payload)
    merged.update(
        {
            "quality_score": int(quality.get("quality_score") or 0),
            "quality_level": str(quality.get("quality_level") or "warning"),
            "quality_warnings": list(quality.get("quality_warnings") or []),
            "reject_hint": bool(quality.get("reject_hint")),
        }
    )
    warning_message = quality_warning_message(quality)
    if warning_message:
        merged["quality_message"] = warning_message
    return merged


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


def _quality_level_for_score(score: int) -> str:
    if score >= 85:
        return "good"
    if score >= 60:
        return "warning"
    return "bad"


def _parse_quality_cues(text: str) -> List[Dict[str, Any]]:
    cues: List[Dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", text.strip()) if text.strip() else []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        explicit_index: Optional[int] = None
        numbering_issue: Optional[str] = None
        timestamp_index = next(
            (index for index, line in enumerate(lines) if "-->" in line),
            None,
        )
        if timestamp_index is None:
            timestamp_line = ""
            body_start = 0
            leading_lines: List[str] = []
        else:
            timestamp_line = lines[timestamp_index]
            body_start = timestamp_index + 1
            leading_lines = lines[:timestamp_index]
            if timestamp_index == 0:
                numbering_issue = "missing"
            elif timestamp_index == 1 and leading_lines[0].isdigit():
                explicit_index = int(leading_lines[0])
            else:
                numbering_issue = "non_numeric_leading"

        start_ms, end_ms, timestamp_error = _parse_timestamp_range(timestamp_line)
        body = "\n".join(lines[body_start:]).strip()
        cues.append(
            {
                "position": len(cues) + 1,
                "explicit_index": explicit_index,
                "numbering_issue": numbering_issue,
                "timestamp": timestamp_line,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "timestamp_error": timestamp_error,
                "text": body,
            }
        )
    return cues


def _parse_timestamp_range(timestamp_line: str) -> Tuple[Optional[int], Optional[int], bool]:
    match = _TIMESTAMP_RE.match(timestamp_line or "")
    if not match:
        return None, None, True
    start_ms = _parts_to_ms(match.group(1), match.group(2), match.group(3), match.group(4))
    end_ms = _parts_to_ms(match.group(5), match.group(6), match.group(7), match.group(8))
    if end_ms <= start_ms:
        return start_ms, end_ms, True
    return start_ms, end_ms, False


def _parts_to_ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(millis)
    )


def _analyze_numbering_issues(cues: List[Dict[str, Any]]) -> Tuple[int, int]:
    issues = 0
    missing_count = 0
    non_numeric_count = 0
    numeric_count = 0
    for expected_index, cue in enumerate(cues, start=1):
        explicit_index = cue.get("explicit_index")
        numbering_issue = cue.get("numbering_issue")
        if explicit_index is None:
            if numbering_issue == "missing":
                missing_count += 1
                issues += 1
            elif numbering_issue == "non_numeric_leading":
                non_numeric_count += 1
                issues += 1
            continue
        numeric_count += 1
        if int(explicit_index) != expected_index:
            issues += 1
    penalty = 0
    if issues:
        penalty += 18
        penalty += min(18, max(0, issues - 1) * 4)
    if missing_count and numeric_count == 0 and missing_count >= max(2, ceil(len(cues) * 0.6)):
        penalty += 10
    if non_numeric_count and non_numeric_count >= max(1, ceil(len(cues) * 0.5)):
        penalty += 4
    return issues, penalty


def _count_overlaps(cues: List[Dict[str, Any]]) -> int:
    overlaps = 0
    previous_end: Optional[int] = None
    for cue in cues:
        if cue["timestamp_error"]:
            continue
        start_ms = int(cue["start_ms"])
        end_ms = int(cue["end_ms"])
        if previous_end is not None and start_ms < previous_end:
            overlaps += 1
        previous_end = max(previous_end or end_ms, end_ms)
    return overlaps


def _count_duration_issues(cues: List[Dict[str, Any]]) -> Tuple[int, int]:
    short_count = 0
    long_count = 0
    for cue in cues:
        if cue["timestamp_error"]:
            continue
        duration_ms = int(cue["end_ms"]) - int(cue["start_ms"])
        if duration_ms < _MIN_DURATION_MS:
            short_count += 1
        if duration_ms > _MAX_DURATION_MS:
            long_count += 1
    return short_count, long_count


def _repeated_line_warning(cues: List[Dict[str, Any]]) -> Optional[str]:
    lines: List[str] = []
    for cue in cues:
        for line in str(cue["text"]).splitlines():
            normalized = _normalize_line(line)
            if normalized:
                lines.append(normalized)
    if len(lines) < 4:
        return None

    counts: Dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    repeated_count = max(counts.values(), default=0)
    if repeated_count >= 4 and repeated_count / len(lines) >= 0.35:
        return "Subtitle repeats the same text unusually often."
    return None


def _normalize_line(value: str) -> str:
    compact = _LINE_NORMALIZE_RE.sub("", (value or "").strip().lower())
    return compact


def _looks_like_wrong_language(cues: List[Dict[str, Any]], expected_language: str) -> bool:
    normalized_language = _normalize_expected_language(expected_language)
    if normalized_language not in {"en", "english"}:
        return False

    text = " ".join(str(cue["text"] or "") for cue in cues)
    arabic_chars = len(_ARABIC_CHAR_RE.findall(text))
    latin_chars = len(_LATIN_CHAR_RE.findall(text))
    total_letters = arabic_chars + latin_chars
    if total_letters < 12:
        return False
    if arabic_chars >= max(12, latin_chars * 2):
        return True
    return latin_chars / max(total_letters, 1) < 0.4


def _normalize_expected_language(value: str) -> str:
    return str(value or "").strip().lower() or "en"
