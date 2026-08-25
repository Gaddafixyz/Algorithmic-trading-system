"""Hashing utilities."""

from __future__ import annotations

import hashlib
import hmac


def sha256_hex(data: str | bytes) -> str:
    """SHA-256 hex digest."""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: str | bytes, data: str | bytes) -> str:
    """HMAC-SHA256 hex digest."""
    if isinstance(key, str):
        key = key.encode()
    if isinstance(data, str):
        data = data.encode()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def idempotency_key(*parts: str) -> str:
    """Create a stable idempotency key from ordered parts."""
    combined = "|".join(str(p) for p in parts)
    return sha256_hex(combined)[:32]
