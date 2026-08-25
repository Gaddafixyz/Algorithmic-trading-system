"""
Smart Money Concepts: Trading Sessions and Kill Zones.

Markets move most during institutional trading hours.
ICT kill zones are specific windows within sessions with the
highest probability for manipulation and subsequent directional moves.

All times are UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

from src.core.enums import KillZone, SessionType


@dataclass(frozen=True)
class SessionWindow:
    """A UTC time window for a trading session."""

    session: SessionType
    start_utc: time
    end_utc: time
    name: str

    def is_active(self, dt: datetime) -> bool:
        t = dt.astimezone(UTC).time().replace(tzinfo=None)
        if self.start_utc <= self.end_utc:
            return self.start_utc <= t <= self.end_utc
        # Spans midnight
        return t >= self.start_utc or t <= self.end_utc


@dataclass(frozen=True)
class KillZoneWindow:
    """An ICT kill zone time window."""

    kill_zone: KillZone
    start_utc: time
    end_utc: time
    name: str
    session: SessionType

    def is_active(self, dt: datetime) -> bool:
        t = dt.astimezone(UTC).time().replace(tzinfo=None)
        if self.start_utc <= self.end_utc:
            return self.start_utc <= t <= self.end_utc
        return t >= self.start_utc or t <= self.end_utc


# Session definitions (UTC)
SESSIONS: list[SessionWindow] = [
    SessionWindow(SessionType.ASIAN, time(0, 0), time(9, 0), "Asian"),
    SessionWindow(SessionType.LONDON, time(7, 0), time(16, 0), "London"),
    SessionWindow(SessionType.NEW_YORK, time(13, 0), time(22, 0), "New York"),
    SessionWindow(SessionType.OVERLAP, time(13, 0), time(16, 0), "London/NY Overlap"),
]

# ICT Kill Zones (UTC)
KILL_ZONES: list[KillZoneWindow] = [
    KillZoneWindow(KillZone.ASIAN_KZ, time(0, 0), time(4, 0), "Asian KZ", SessionType.ASIAN),
    KillZoneWindow(
        KillZone.LONDON_OPEN_KZ, time(7, 0), time(9, 0), "London Open KZ", SessionType.LONDON
    ),
    KillZoneWindow(
        KillZone.NEW_YORK_OPEN_KZ, time(13, 0), time(15, 0), "NY Open KZ", SessionType.NEW_YORK
    ),
    KillZoneWindow(
        KillZone.NEW_YORK_LUNCH_KZ, time(17, 0), time(19, 0), "NY Lunch KZ", SessionType.NEW_YORK
    ),
    KillZoneWindow(
        KillZone.LONDON_CLOSE_KZ, time(15, 0), time(16, 0), "London Close KZ", SessionType.LONDON
    ),
]


def get_active_session(dt: datetime) -> SessionType:
    """Return the most specific session active at the given UTC time."""
    for session in reversed(SESSIONS):  # Overlap first
        if session.is_active(dt):
            return session.session
    return SessionType.OFF_HOURS


def get_active_kill_zone(dt: datetime) -> KillZone | None:
    """Return the kill zone active at the given UTC time, or None."""
    for kz in KILL_ZONES:
        if kz.is_active(dt):
            return kz.kill_zone
    return None


def is_in_kill_zone(dt: datetime) -> bool:
    """True if the current time is within any kill zone."""
    return get_active_kill_zone(dt) is not None


def is_high_probability_window(dt: datetime) -> bool:
    """True if current time is in London Open or NY Open kill zones."""
    kz = get_active_kill_zone(dt)
    return kz in (KillZone.LONDON_OPEN_KZ, KillZone.NEW_YORK_OPEN_KZ)


def session_score(dt: datetime) -> float:
    """
    Return a 0-1 score representing session quality.

    1.0 = peak kill zone (London/NY open)
    0.7 = active session overlap
    0.4 = single active session
    0.1 = off hours / Asian session
    """
    kz = get_active_kill_zone(dt)
    if kz in (KillZone.LONDON_OPEN_KZ, KillZone.NEW_YORK_OPEN_KZ):
        return 1.0
    if kz == KillZone.LONDON_CLOSE_KZ:
        return 0.8
    if kz in (KillZone.NEW_YORK_LUNCH_KZ, KillZone.ASIAN_KZ):
        return 0.5
    session = get_active_session(dt)
    if session == SessionType.OVERLAP:
        return 0.7
    if session in (SessionType.LONDON, SessionType.NEW_YORK):
        return 0.4
    if session == SessionType.ASIAN:
        return 0.2
    return 0.1
