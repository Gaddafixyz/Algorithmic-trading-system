"""Time and timezone utilities."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def utc_from_ms(ms: int | float) -> datetime:
    """Convert millisecond UNIX timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def to_ms(dt: datetime) -> int:
    """Convert datetime to millisecond UNIX timestamp."""
    return int(dt.timestamp() * 1000)


def floor_to_timeframe(dt: datetime, seconds: int) -> datetime:
    """Floor a datetime to the nearest timeframe boundary."""
    ts = int(dt.timestamp())
    floored = (ts // seconds) * seconds
    return datetime.fromtimestamp(floored, tz=dt.tzinfo or UTC)


def is_same_day(a: datetime, b: datetime) -> bool:
    """True if both datetimes fall on the same UTC calendar day."""
    return a.astimezone(UTC).date() == b.astimezone(UTC).date()


def trading_day_start(dt: datetime) -> datetime:
    """Return midnight UTC of the given day."""
    d = dt.astimezone(UTC).date()
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def humanize_duration(seconds: float) -> str:
    """Convert seconds to human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
