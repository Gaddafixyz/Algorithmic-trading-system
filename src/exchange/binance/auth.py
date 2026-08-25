"""
Binance HMAC-SHA256 request signing.

Security: API secret is handled as bytes and never logged.
The signature is appended to query string params per Binance spec.

Binance (2026-01-15): payloads must be percent-encoded *before* computing
the HMAC-SHA256 signature.  Requests signed against a raw (un-encoded)
query string are rejected with -1022 INVALID_SIGNATURE.  We enforce this by
always building the canonical query string with urllib.parse.urlencode using
quote_via=urllib.parse.quote (strict %XX encoding, not quote_plus which
encodes spaces as "+").
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Any


def create_signature(secret: str, query_string: str) -> str:
    """
    Create HMAC-SHA256 signature for a Binance request.

    Args:
        secret: Raw API secret (never log this).
        query_string: Percent-encoded parameter string to sign.

    Returns:
        Lowercase hex digest signature.
    """
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_params(
    api_key: str,
    api_secret: str,
    params: dict[str, Any],
    recv_window: int = 5000,
    clock_offset_ms: int = 0,
) -> dict[str, Any]:
    """
    Add timestamp, recvWindow, and signature to request parameters.

    Modifies and returns the params dict.

    Args:
        clock_offset_ms: Correction applied to the local clock (server_time -
            local_time), as measured by ``BaseExchangeClient.sync_clock()``.
            Without this, a drifted host clock causes every signed request to
            fail with a "Timestamp for this request is outside of the
            recvWindow" error even though nothing about the request itself is
            wrong.

    Signing contract (Binance 2026-01-15):
        1. Build the complete params dict (including timestamp/recvWindow).
        2. Percent-encode it with urllib.parse.quote (%XX, NOT quote_plus).
        3. Compute HMAC-SHA256 over the encoded string.
        4. Append &signature=<hex> — never include signature in the signed
           payload itself.
    """
    params["timestamp"] = int(time.time() * 1000) + clock_offset_ms
    params["recvWindow"] = recv_window

    # Strict percent-encoding (%XX) per Binance 2026-01-15 requirement.
    # quote_plus would encode spaces as "+" which is not accepted.
    query = urllib.parse.urlencode(
        {k: str(v) for k, v in params.items() if v is not None},
        quote_via=urllib.parse.quote,
    )
    params["signature"] = create_signature(api_secret, query)
    return params


def get_headers(api_key: str) -> dict[str, str]:
    """Return Binance-required authentication headers."""
    return {"X-MBX-APIKEY": api_key}
