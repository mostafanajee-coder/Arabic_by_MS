"""Small hashing helpers used by the cache layer."""

from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str, encoding: str = "utf-8") -> str:
    """Return the hex SHA-256 digest of `text` encoded with `encoding`."""
    return sha256_bytes(text.encode(encoding))
