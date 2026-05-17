"""Minimal Gemini REST client (sync, single endpoint).

Only the `generateContent` endpoint is used. We talk to the public
generativelanguage.googleapis.com v1beta API with an API key from the
``GEMINI_API_KEY`` environment variable; the model is chosen via
``GEMINI_MODEL`` (default: ``gemini-2.5-flash``).

Env vars are read *at call time* on purpose, so tests and runtime can
flip them with ``monkeypatch.setenv``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

import httpx


DEFAULT_MODEL = "gemini-2.5-flash"
_ENDPOINT_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(RuntimeError):
    """Generic Gemini call failure."""


class GeminiNotConfiguredError(GeminiError):
    """Raised when GEMINI_API_KEY is missing or blank."""


def get_config() -> Tuple[str, str]:
    """Return (api_key, model). Raises GeminiNotConfiguredError if no key."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    model = (os.getenv("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL
    if not api_key:
        raise GeminiNotConfiguredError(
            "GEMINI_API_KEY is not set. Add it to your environment "
            "(or .env file) before requesting a translation."
        )
    return api_key, model


def get_status() -> Dict[str, Any]:
    """Return Gemini configuration status for the companion UI."""
    model = (os.getenv("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if api_key:
        return {
            "configured": True,
            "model": model,
            "message": "Gemini is configured and ready for translation.",
        }
    return {
        "configured": False,
        "model": model,
        "message": (
            "GEMINI_API_KEY is missing. Add it to your environment or .env file "
            "before translating subtitles."
        ),
    }


def generate(prompt: str, *, timeout: float = 60.0) -> str:
    """Send `prompt` to Gemini and return the model's text reply.

    Raises:
        GeminiNotConfiguredError: when GEMINI_API_KEY is missing.
        GeminiError: for any other failure (HTTP error, malformed response).
    """
    api_key, model = get_config()
    url = _ENDPOINT_TMPL.format(model=model)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        resp = httpx.post(url, params={"key": api_key}, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise GeminiError(f"Gemini request failed: {exc}") from exc

    if resp.status_code != 200:
        raise GeminiError(
            f"Gemini returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {resp.text[:300]}") from exc
