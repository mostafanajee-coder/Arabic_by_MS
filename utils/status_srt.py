"""Generate small, honest status SRT files for Stremio."""

from __future__ import annotations

import re
from typing import Dict

STATUS_MESSAGES: Dict[str, str] = {
    "no_record": (
        "لا توجد ترجمة عربية جاهزة لهذا المحتوى. افتح Arabic by M.S Companion "
        "وابحث أو ارفع ملف SRT إنجليزي."
    ),
    "uploaded_not_translated": (
        "تم العثور على ملف ترجمة إنجليزي، لكنه لم يُترجم بعد. افتح Companion "
        "واضغط Translate أو Translate in Background."
    ),
    "preparing": (
        "يجري تجهيز الترجمة العربية الآن. انتظر قليلا ثم افتح قائمة الترجمة "
        "مرة أخرى بعد وقت قصير."
    ),
    "failed": "فشلت آخر محاولة ترجمة. افتح Companion لمراجعة الخطأ وإعادة المحاولة.",
    "translating": (
        "الترجمة قيد المعالجة. انتظر قليلا ثم أعد فتح قائمة الترجمة بعد وقت قصير."
    ),
    "unknown": "الترجمة العربية غير جاهزة بعد.",
}

_TIMESTAMP = "00:00:01,000 --> 00:00:08,000"


def cleanup_status_text(text: str) -> str:
    """Normalize a fixed Arabic status message into safe subtitle text."""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"```+", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(
        r"^\s*(Arabic|Translation)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = " ".join(part.strip() for part in cleaned.splitlines() if part.strip())
    return cleaned.strip() or STATUS_MESSAGES["unknown"]


def get_status_message(state: str) -> str:
    """Return the Arabic message for a status-subtitle state."""
    return cleanup_status_text(STATUS_MESSAGES.get(state, STATUS_MESSAGES["unknown"]))


def build_status_srt(message: str) -> str:
    """Return a minimal UTF-8 SRT string for a status message."""
    text = cleanup_status_text(message)
    return "1\n{0}\n{1}\n".format(_TIMESTAMP, text)
