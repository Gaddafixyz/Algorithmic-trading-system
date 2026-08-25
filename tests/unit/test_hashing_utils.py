"""Unit tests for src.utils.hashing."""

from __future__ import annotations

import hashlib
import hmac

from src.utils.hashing import hmac_sha256, idempotency_key, sha256_hex


class TestSha256Hex:
    def test_matches_stdlib_hashlib_for_str_input(self) -> None:
        result = sha256_hex("hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected

    def test_matches_stdlib_hashlib_for_bytes_input(self) -> None:
        result = sha256_hex(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected

    def test_str_and_bytes_of_same_content_produce_same_digest(self) -> None:
        assert sha256_hex("abc") == sha256_hex(b"abc")

    def test_digest_is_64_hex_characters(self) -> None:
        result = sha256_hex("anything")
        assert len(result) == 64
        int(result, 16)  # raises ValueError if not valid hex

    def test_different_input_produces_different_digest(self) -> None:
        assert sha256_hex("a") != sha256_hex("b")

    def test_empty_string_has_the_well_known_sha256_digest(self) -> None:
        assert (
            sha256_hex("")
            == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )


class TestHmacSha256:
    def test_matches_stdlib_hmac_for_str_inputs(self) -> None:
        result = hmac_sha256("secret-key", "message")
        expected = hmac.new(b"secret-key", b"message", hashlib.sha256).hexdigest()
        assert result == expected

    def test_matches_stdlib_hmac_for_bytes_inputs(self) -> None:
        result = hmac_sha256(b"secret-key", b"message")
        expected = hmac.new(b"secret-key", b"message", hashlib.sha256).hexdigest()
        assert result == expected

    def test_mixed_str_and_bytes_inputs_are_normalized_consistently(self) -> None:
        assert hmac_sha256("k", "m") == hmac_sha256(b"k", b"m")

    def test_different_key_produces_different_signature(self) -> None:
        sig_a = hmac_sha256("key-a", "same-message")
        sig_b = hmac_sha256("key-b", "same-message")
        assert sig_a != sig_b

    def test_different_message_produces_different_signature(self) -> None:
        sig_a = hmac_sha256("same-key", "message-a")
        sig_b = hmac_sha256("same-key", "message-b")
        assert sig_a != sig_b

    def test_is_deterministic_for_the_same_inputs(self) -> None:
        assert hmac_sha256("k", "m") == hmac_sha256("k", "m")


class TestIdempotencyKey:
    def test_is_deterministic_for_the_same_parts(self) -> None:
        assert idempotency_key("order", "123", "BUY") == idempotency_key(
            "order", "123", "BUY"
        )

    def test_order_of_parts_matters(self) -> None:
        assert idempotency_key("a", "b") != idempotency_key("b", "a")

    def test_returns_32_character_hex_prefix_of_sha256(self) -> None:
        key = idempotency_key("x", "y", "z")
        assert len(key) == 32
        int(key, 16)  # raises ValueError if not valid hex

    def test_is_a_prefix_of_the_full_sha256_of_the_joined_parts(self) -> None:
        key = idempotency_key("order", "42")
        full = sha256_hex("order|42")
        assert full.startswith(key)

    def test_different_number_of_parts_produces_different_key(self) -> None:
        assert idempotency_key("a", "b", "c") != idempotency_key("a", "b")

    def test_no_parts_does_not_raise(self) -> None:
        key = idempotency_key()
        assert len(key) == 32
