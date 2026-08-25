"""Unit tests for src.utils.time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from src.utils.time import (
    floor_to_timeframe,
    humanize_duration,
    is_same_day,
    to_ms,
    trading_day_start,
    utc_from_ms,
    utc_now,
)


class TestUtcNow:
    def test_returns_timezone_aware_datetime_in_utc(self) -> None:
        result = utc_now()
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_advances_between_calls(self) -> None:
        first = utc_now()
        second = utc_now()
        assert second >= first


class TestUtcFromMs:
    def test_converts_millisecond_epoch_to_utc_datetime(self) -> None:
        # 2021-01-01T00:00:00Z
        result = utc_from_ms(1609459200000)
        assert result == datetime(2021, 1, 1, tzinfo=UTC)

    def test_accepts_float_milliseconds(self) -> None:
        result = utc_from_ms(1609459200000.0)
        assert result == datetime(2021, 1, 1, tzinfo=UTC)

    def test_round_trip_with_to_ms(self) -> None:
        original_ms = 1700000000123
        dt = utc_from_ms(original_ms)
        # Millisecond precision is preserved (no truncation beyond ms).
        assert to_ms(dt) == original_ms


class TestToMs:
    def test_converts_utc_datetime_to_millisecond_epoch(self) -> None:
        dt = datetime(2021, 1, 1, tzinfo=UTC)
        assert to_ms(dt) == 1609459200000

    def test_non_utc_timezone_is_normalized_by_timestamp(self) -> None:
        # datetime.timestamp() correctly accounts for tzinfo offset,
        # so a non-UTC aware datetime still yields the correct epoch ms.
        eastern = timezone(timedelta(hours=-5))
        dt = datetime(2020, 12, 31, 19, 0, 0, tzinfo=eastern)
        assert to_ms(dt) == 1609459200000


class TestFloorToTimeframe:
    def test_floors_to_one_minute_boundary(self) -> None:
        dt = datetime(2021, 1, 1, 12, 34, 59, tzinfo=UTC)
        result = floor_to_timeframe(dt, 60)
        assert result == datetime(2021, 1, 1, 12, 34, 0, tzinfo=UTC)

    def test_floors_to_one_hour_boundary(self) -> None:
        dt = datetime(2021, 1, 1, 12, 59, 59, tzinfo=UTC)
        result = floor_to_timeframe(dt, 3600)
        assert result == datetime(2021, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_already_on_boundary_is_unchanged(self) -> None:
        dt = datetime(2021, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = floor_to_timeframe(dt, 60)
        assert result == dt

    def test_preserves_naive_or_defaults_to_utc(self) -> None:
        # dt.tzinfo is None for a naive datetime; function falls back to UTC.
        naive = datetime(2021, 1, 1, 12, 34, 59)  # noqa: DTZ001  # intentional naive
        result = floor_to_timeframe(naive, 60)
        assert result.tzinfo == UTC


class TestIsSameDay:
    def test_same_calendar_day_returns_true(self) -> None:
        a = datetime(2021, 6, 1, 0, 0, 1, tzinfo=UTC)
        b = datetime(2021, 6, 1, 23, 59, 59, tzinfo=UTC)
        assert is_same_day(a, b) is True

    def test_different_calendar_day_returns_false(self) -> None:
        a = datetime(2021, 6, 1, 23, 59, 59, tzinfo=UTC)
        b = datetime(2021, 6, 2, 0, 0, 1, tzinfo=UTC)
        assert is_same_day(a, b) is False

    def test_compares_after_normalizing_to_utc(self) -> None:
        # 23:00 in UTC-5 is 04:00 the next day in UTC.
        eastern = timezone(timedelta(hours=-5))
        a = datetime(2021, 6, 1, 23, 0, 0, tzinfo=eastern)
        b = datetime(2021, 6, 2, 4, 0, 0, tzinfo=UTC)
        assert is_same_day(a, b) is True


class TestTradingDayStart:
    def test_returns_midnight_utc_of_the_given_day(self) -> None:
        dt = datetime(2021, 6, 1, 14, 30, tzinfo=UTC)
        result = trading_day_start(dt)
        assert result == datetime(2021, 6, 1, tzinfo=UTC)

    def test_normalizes_non_utc_input_before_taking_the_date(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        # 23:30 Eastern on May 31 is 04:30 UTC on June 1.
        dt = datetime(2021, 5, 31, 23, 30, tzinfo=eastern)
        result = trading_day_start(dt)
        assert result == datetime(2021, 6, 1, tzinfo=UTC)


class TestHumanizeDuration:
    def test_seconds_under_a_minute(self) -> None:
        assert humanize_duration(45) == "45s"

    def test_minutes_under_an_hour(self) -> None:
        assert humanize_duration(150) == "2.5m"

    def test_hours_under_a_day(self) -> None:
        assert humanize_duration(7200) == "2.0h"

    def test_days_for_long_durations(self) -> None:
        assert humanize_duration(172800) == "2.0d"

    def test_boundary_at_exactly_sixty_seconds_is_minutes(self) -> None:
        assert humanize_duration(60) == "1.0m"

    def test_boundary_at_exactly_one_hour_is_hours(self) -> None:
        assert humanize_duration(3600) == "1.0h"

    def test_boundary_at_exactly_one_day_is_days(self) -> None:
        assert humanize_duration(86400) == "1.0d"
