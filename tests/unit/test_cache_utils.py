"""Unit tests for src.utils.cache.TTLCache."""

from __future__ import annotations

import time

import pytest

from src.utils.cache import TTLCache


class TestGetAndSet:
    def test_get_returns_none_for_a_missing_key(self) -> None:
        cache: TTLCache[str] = TTLCache()
        assert cache.get("missing") is None

    def test_set_then_get_returns_the_stored_value(self) -> None:
        cache: TTLCache[str] = TTLCache()
        cache.set("key", "value")
        assert cache.get("key") == "value"

    def test_set_overwrites_an_existing_key(self) -> None:
        cache: TTLCache[str] = TTLCache()
        cache.set("key", "first")
        cache.set("key", "second")
        assert cache.get("key") == "second"

    def test_len_reflects_number_of_stored_entries(self) -> None:
        cache: TTLCache[int] = TTLCache()
        cache.set("a", 1)
        cache.set("b", 2)
        assert len(cache) == 2


class TestExpiry:
    def test_entry_expires_after_its_ttl_elapses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: fake_clock["t"])

        cache: TTLCache[str] = TTLCache()
        cache.set("key", "value", ttl=10.0)

        fake_clock["t"] += 5.0
        assert cache.get("key") == "value"  # not yet expired

        fake_clock["t"] += 6.0  # total elapsed: 11s > ttl of 10s
        assert cache.get("key") is None

    def test_expired_entry_is_removed_from_internal_store_on_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_clock = {"t": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: fake_clock["t"])

        cache: TTLCache[str] = TTLCache()
        cache.set("key", "value", ttl=1.0)
        fake_clock["t"] = 2.0

        assert cache.get("key") is None
        assert len(cache) == 0

    def test_uses_default_ttl_when_none_is_specified_per_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_clock = {"t": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: fake_clock["t"])

        cache: TTLCache[str] = TTLCache(default_ttl=5.0)
        cache.set("key", "value")  # no explicit ttl -> uses default_ttl

        fake_clock["t"] = 4.9
        assert cache.get("key") == "value"

        fake_clock["t"] = 5.1
        assert cache.get("key") is None


class TestDeleteAndClear:
    def test_delete_returns_true_and_removes_an_existing_key(self) -> None:
        cache: TTLCache[str] = TTLCache()
        cache.set("key", "value")
        assert cache.delete("key") is True
        assert cache.get("key") is None

    def test_delete_returns_false_for_a_missing_key(self) -> None:
        cache: TTLCache[str] = TTLCache()
        assert cache.delete("missing") is False

    def test_clear_removes_all_entries(self) -> None:
        cache: TTLCache[int] = TTLCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert len(cache) == 0
        assert cache.get("a") is None


class TestEvictionAtMaxSize:
    def test_evicts_the_entry_closest_to_expiry_when_full(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_clock = {"t": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: fake_clock["t"])

        cache: TTLCache[str] = TTLCache(max_size=2)
        cache.set("first", "v1", ttl=10.0)  # expires_at = 10.0
        cache.set("second", "v2", ttl=100.0)  # expires_at = 100.0

        # Cache is full (size == max_size); inserting a third entry must
        # evict the one with the soonest expiry ("first").
        cache.set("third", "v3", ttl=50.0)

        assert len(cache) == 2
        assert cache.get("first") is None
        assert cache.get("second") == "v2"
        assert cache.get("third") == "v3"

    def test_never_exceeds_max_size(self) -> None:
        cache: TTLCache[int] = TTLCache(max_size=3)
        for i in range(10):
            cache.set(f"key-{i}", i)
        assert len(cache) <= 3
