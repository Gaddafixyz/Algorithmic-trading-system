"""Unit tests for src.exchange.binance.auth."""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse

from src.exchange.binance.auth import create_signature, get_headers, sign_params


class TestCreateSignature:
    def test_matches_stdlib_hmac_sha256_directly(self) -> None:
        secret = "my-secret-key"
        query = "symbol=BTCUSDT&side=BUY&quantity=0.1"
        result = create_signature(secret, query)
        expected = hmac.new(
            secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert result == expected

    def test_is_deterministic_for_the_same_inputs(self) -> None:
        assert create_signature("k", "q") == create_signature("k", "q")

    def test_different_secret_produces_a_different_signature(self) -> None:
        sig_a = create_signature("secret-a", "query=1")
        sig_b = create_signature("secret-b", "query=1")
        assert sig_a != sig_b

    def test_different_query_produces_a_different_signature(self) -> None:
        sig_a = create_signature("secret", "query=1")
        sig_b = create_signature("secret", "query=2")
        assert sig_a != sig_b

    def test_returns_a_64_character_lowercase_hex_digest(self) -> None:
        result = create_signature("secret", "query=1")
        assert len(result) == 64
        assert result == result.lower()
        int(result, 16)  # raises ValueError if not valid hex


class TestSignParams:
    def test_adds_timestamp_recv_window_and_signature(self) -> None:
        params = {"symbol": "BTCUSDT", "side": "BUY"}
        result = sign_params("api-key", "api-secret", params)
        assert "timestamp" in result
        assert result["recvWindow"] == 5000
        assert "signature" in result
        assert len(result["signature"]) == 64

    def test_mutates_and_returns_the_same_dict(self) -> None:
        params = {"symbol": "BTCUSDT"}
        result = sign_params("k", "s", params)
        assert result is params

    def test_applies_a_custom_recv_window(self) -> None:
        params = {"symbol": "BTCUSDT"}
        result = sign_params("k", "s", params, recv_window=10000)
        assert result["recvWindow"] == 10000

    def test_applies_clock_offset_ms_to_the_timestamp(self) -> None:
        import time
        before = int(time.time() * 1000)
        params = {"symbol": "BTCUSDT"}
        result = sign_params("k", "s", params, clock_offset_ms=100_000)
        # timestamp should be roughly `before + 100000`, not the raw
        # local time -- allow a small tolerance for test execution time.
        assert result["timestamp"] >= before + 100_000 - 1000
        assert result["timestamp"] <= before + 100_000 + 5000

    def test_signature_matches_manual_query_string_construction(self) -> None:
        params = {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.1"}
        result = sign_params("api-key", "api-secret", dict(params))

        # Reconstruct exactly what the query string should have been
        # (timestamp + recvWindow included, signature excluded) and
        # verify the signature independently.
        expected_params = {
            "symbol": "BTCUSDT", "side": "BUY", "quantity": "0.1",
            "timestamp": result["timestamp"], "recvWindow": result["recvWindow"],
        }
        expected_query = urllib.parse.urlencode(
            {k: str(v) for k, v in expected_params.items()}
        )
        expected_sig = hmac.new(
            b"api-secret", expected_query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert result["signature"] == expected_sig

    def test_none_valued_params_are_excluded_from_the_signed_query_string(
        self,
    ) -> None:
        params_with_none = {"symbol": "BTCUSDT", "side": "BUY", "extra": None}
        params_without = {"symbol": "BTCUSDT", "side": "BUY"}

        result_with = sign_params("k", "s", params_with_none, clock_offset_ms=0)
        result_without = sign_params("k", "s", params_without, clock_offset_ms=0)

        # Force identical timestamps so the only difference is the
        # None-valued key's presence/absence in the dict.
        result_without["timestamp"] = result_with["timestamp"]
        query_without = urllib.parse.urlencode({
            k: str(v) for k, v in result_without.items()
            if k != "signature" and v is not None
        })
        expected_sig = hmac.new(
            b"s", query_without.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert result_with["signature"] == expected_sig


class TestGetHeaders:
    def test_returns_the_api_key_header(self) -> None:
        result = get_headers("my-api-key")
        assert result == {"X-MBX-APIKEY": "my-api-key"}
