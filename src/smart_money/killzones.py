"""Kill zone utilities (re-exports + helpers)."""

from __future__ import annotations

from datetime import datetime

from src.smart_money.sessions import (
    KillZone,
    get_active_kill_zone,
    is_in_kill_zone,
    session_score,
)


def should_trade_now(dt: datetime, *, require_kill_zone: bool = True) -> bool:
    """
    Return True if it is currently an appropriate time to enter a trade.

    Args:
        dt: UTC datetime to evaluate.
        require_kill_zone: If True, require an active ICT kill zone.
    """
    if require_kill_zone:
        return is_in_kill_zone(dt)
    return session_score(dt) >= 0.4


def kill_zone_score(dt: datetime) -> float:
    """Return a 0–1 quality score for the current kill zone."""
    kz = get_active_kill_zone(dt)
    if kz is None:
        return 0.0
    scores = {
        KillZone.LONDON_OPEN_KZ: 1.0,
        KillZone.NEW_YORK_OPEN_KZ: 1.0,
        KillZone.LONDON_CLOSE_KZ: 0.8,
        KillZone.ASIAN_KZ: 0.5,
        KillZone.NEW_YORK_LUNCH_KZ: 0.4,
    }
    return scores.get(kz, 0.3)
