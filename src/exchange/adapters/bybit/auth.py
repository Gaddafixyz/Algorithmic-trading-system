"""
Bybit V5 API authentication helpers.

Bybit's unified V5 API signs a concatenation of
``timestamp + api_key + recv_window + payload`` with HMAC-SHA256, where
``payload`` is the sorted query string for GET requests or the raw JSON
body for POST requests. This differs from Binance's "sign the query
string only" scheme, so it lives in its own module rather than reusing
``exchange/binance/auth.py``.

Docs: https://bybit-exchange.github.io/docs/v5/guide#authentication
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any


def timestamp_ms(clock_offset_ms: int = 0) -> int:
    """Current time in milliseconds, adjusted for measured clock drift."""
    return int(time.time() * 1000) + clock_offset_ms


def _sorted_query_string(params: dict[str, Any]) -> str:
    """Render params as ``k=v&k2=v2`` sorted by key (Bybit's required form)."""
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)


def sign_request(
    api_key: str,
    api_secret: str,
    recv_window: int,
    payload: str,
    clock_offset_ms: int = 0,
) -> dict[str, str]:
    """
    Build the signed header set for a Bybit V5 request.

    Args:
        payload: sorted query string (GET) or compact JSON body (POST).

    Returns:
        Headers to attach to the request: ``X-BAPI-*`` + signature.
    """
    ts = timestamp_ms(clock_offset_ms)
    prehash = f"{ts}{api_key}{recv_window}{payload}"
    signature = hmac.new(
        api_secret.encode(), prehash.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": str(ts),
        "X-BAPI-RECV-WINDOW": str(recv_window),
        "X-BAPI-SIGN": signature,
    }


def query_string_for_signing(params: dict[str, Any]) -> str:
    """Public wrapper so the REST client can reuse the exact sort order."""
    return _sorted_query_string(params)
