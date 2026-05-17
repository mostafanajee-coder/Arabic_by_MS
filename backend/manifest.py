"""Stremio addon manifest for Arabic by M.S."""

from __future__ import annotations

from typing import Any, Dict

from . import config


def build_manifest() -> Dict[str, Any]:
    """Return the Stremio addon manifest as a plain dict.

    Stremio expects a JSON document describing what the addon provides.
    For a subtitles-only addon we declare resources=["subtitles"] and the
    content types we want to receive subtitle requests for.
    """
    return {
        "id": config.ADDON_ID,
        "version": config.ADDON_VERSION,
        "name": config.ADDON_NAME,
        "description": config.ADDON_DESCRIPTION,
        "resources": ["subtitles"],
        "types": ["movie", "series"],
        "catalogs": [],
        "idPrefixes": ["tt"],
        "behaviorHints": {
            "configurable": False,
            "configurationRequired": False,
        },
    }
