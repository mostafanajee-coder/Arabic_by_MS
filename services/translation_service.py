"""Translate an uploaded English SRT into Arabic via Gemini.

This is the orchestrator between:
  * the cache DB (services/cache_db.py)
  * the SRT parser/renderer (utils/srt_chunker.py)
  * the numbered-output parser (utils/srt_cleaner.py)
  * the Gemini client (services/gemini_service.py)

Tests inject a fake `gemini_call` to avoid live API traffic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from services import cache_db, gemini_service
from utils.srt_chunker import (
    SRTEntry,
    SRTParseError,
    chunk_entries,
    parse_srt,
    render_srt,
)
from utils.srt_cleaner import (
    TranslationFormatError,
    assert_complete,
    parse_numbered_translations,
)
from utils.srt_quality import SRTQualityError, validate_translated_entries

PathLike = Union[str, Path]
GeminiCall = Callable[[str], str]


# ---- Errors --------------------------------------------------------------


class TranslationError(RuntimeError):
    """Base class for translate-record failures."""


class RecordNotFoundError(TranslationError):
    """The requested DB record does not exist."""


class EnglishFileMissingError(TranslationError):
    """The record's english_srt_path does not exist on disk."""


# ---- Prompt --------------------------------------------------------------


_PROMPT_PREAMBLE = (
    "You are a professional Arabic translator for movie and TV subtitles. "
    "Translate the numbered English subtitle lines below into Modern Standard "
    "Arabic. Reply with ONLY the translations, in the exact same numbered "
    "format, one entry per line. Do not include code fences, prefaces, or "
    "commentary. If an entry spans multiple lines in the source, collapse them "
    "into a single Arabic line.\n\n"
    "Output format example:\n"
    "1) <arabic translation of entry 1>\n"
    "2) <arabic translation of entry 2>\n\n"
    "Input:\n"
)


def _build_prompt(chunk: List[SRTEntry]) -> str:
    lines = []
    for entry in chunk:
        # Collapse multi-line cues to a single line so the parser can rely on
        # one translation per cue.
        collapsed = " ".join(entry.text.split())
        lines.append(f"{entry.index}) {collapsed}")
    return _PROMPT_PREAMBLE + "\n".join(lines)


# ---- Orchestration -------------------------------------------------------


def translate_record(
    db_path: PathLike,
    record_id: int,
    *,
    arabic_cache_dir: PathLike,
    chunk_size: int = 20,
    gemini_call: Optional[GeminiCall] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Translate the English SRT for `record_id` and persist the Arabic file.

    Returns a dict describing the updated record. Raises subclasses of
    TranslationError for predictable failures and
    gemini_service.GeminiNotConfiguredError when no API key is set.
    """
    record = cache_db.get_record(db_path, record_id)
    if not record:
        raise RecordNotFoundError(f"No subtitle record with id={record_id}")

    try:
        existing_arabic_path = record.get("arabic_srt_path")
        if (
            not force
            and record.get("status") == "translated"
            and existing_arabic_path
            and Path(existing_arabic_path).exists()
        ):
            return dict(record)

        cache_db.reset_translation_progress(db_path, record_id, status="uploaded")
        record = cache_db.get_record(db_path, record_id) or record

        english_path = Path(record["english_srt_path"])
        if not english_path.exists():
            raise EnglishFileMissingError(
                f"English SRT file is missing on disk: {english_path}"
            )

        english_text = english_path.read_text(encoding="utf-8")
        entries = parse_srt(english_text)
        if gemini_call is None:
            gemini_call = gemini_service.generate  # raises GeminiNotConfiguredError lazily

        chunks = list(chunk_entries(entries, size=chunk_size))
        total_chunks = len(chunks)
        cache_db.set_translation_progress(
            db_path,
            record_id,
            total_chunks=total_chunks,
            done_chunks=0,
            progress_message="Starting translation (0/{0} chunks).".format(total_chunks),
        )

        translated_entries: List[SRTEntry] = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            cache_db.set_translation_progress(
                db_path,
                record_id,
                total_chunks=total_chunks,
                done_chunks=chunk_index - 1,
                progress_message="Translating chunk {0} of {1}.".format(
                    chunk_index,
                    total_chunks,
                ),
            )
            try:
                prompt = _build_prompt(chunk)
                raw_reply = gemini_call(prompt)
                translations = parse_numbered_translations(raw_reply)
                assert_complete(translations, [e.index for e in chunk])
            except (
                TranslationFormatError,
                gemini_service.GeminiError,
                OSError,
            ) as exc:
                message = _short_error_message(
                    "Chunk {0}/{1} failed: {2}".format(
                        chunk_index,
                        total_chunks,
                        exc,
                    )
                )
                cache_db.set_failed(
                    db_path,
                    record_id,
                    message,
                    progress_message="Failed on chunk {0} of {1}.".format(
                        chunk_index,
                        total_chunks,
                    ),
                )
                raise

            for entry in chunk:
                translated_entries.append(
                    SRTEntry(
                        index=entry.index,
                        timestamp=entry.timestamp,
                        text=translations.get(entry.index, ""),
                    )
                )
            cache_db.set_translation_progress(
                db_path,
                record_id,
                total_chunks=total_chunks,
                done_chunks=chunk_index,
                progress_message="Translated chunk {0} of {1}.".format(
                    chunk_index,
                    total_chunks,
                ),
            )

        translated_entries = validate_translated_entries(entries, translated_entries)
        arabic_text = render_srt(translated_entries)

        out_dir = Path(arabic_cache_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{record['video_id']}_{record['english_srt_hash'][:12]}.ar.srt"
        target.write_text(arabic_text, encoding="utf-8")

        cache_db.set_arabic_srt(
            db_path,
            record_id,
            str(target),
            status="translated",
            error_message=None,
        )
        updated = cache_db.get_record(db_path, record_id)
        return dict(updated) if updated else {}
    except SRTParseError as exc:
        message = _short_error_message(f"Invalid English SRT: {exc}")
        cache_db.set_failed(db_path, record_id, message, progress_message="English SRT parsing failed.")
        raise TranslationFormatError(str(exc)) from exc
    except (
        EnglishFileMissingError,
        TranslationFormatError,
        SRTQualityError,
        gemini_service.GeminiError,
        OSError,
    ) as exc:
        current = cache_db.get_record(db_path, record_id) or {}
        if current.get("status") != "failed" or not current.get("error_message"):
            cache_db.set_failed(
                db_path,
                record_id,
                _short_error_message(str(exc)),
                progress_message="Translation failed.",
            )
        raise


def _short_error_message(message: str, limit: int = 240) -> str:
    """Normalize and shorten stored DB errors for the companion UI."""
    compact = " ".join(message.split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."
