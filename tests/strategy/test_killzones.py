"""Unit tests for src.smart_money.killzones.

Kill zone windows (UTC, from src.smart_money.sessions.KILL_ZONES):
  Asian KZ:        00:00-04:00
  London Open KZ:  07:00-09:00
  NY Open KZ:      13:00-15:00
  London Close KZ: 15:00-16:00
  NY Lunch KZ:     17:00-19:00
Outside all windows (e.g. 05:00, 10:00, 20:00) = no active kill zone.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.smart_money.killzones import kill_zone_score, should_trade_now

LONDON_OPEN = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
NY_OPEN = datetime(2024, 1, 2, 14, 0, tzinfo=UTC)
LONDON_CLOSE = datetime(2024, 1, 2, 15, 30, tzinfo=UTC)
ASIAN = datetime(2024, 1, 2, 2, 0, tzinfo=UTC)
NY_LUNCH = datetime(2024, 1, 2, 18, 0, tzinfo=UTC)
OFF_HOURS = datetime(2024, 1, 2, 5, 30, tzinfo=UTC)


class TestShouldTradeNowRequireKillZone:
    def test_true_during_an_active_kill_zone(self) -> None:
        assert should_trade_now(NY_OPEN, require_kill_zone=True) is True

    def test_false_outside_any_kill_zone(self) -> None:
        assert should_trade_now(OFF_HOURS, require_kill_zone=True) is False

    def test_defaults_to_requiring_a_kill_zone(self) -> None:
        assert should_trade_now(OFF_HOURS) is False
        assert should_trade_now(NY_OPEN) is True


class TestShouldTradeNowSessionScoreMode:
    def test_true_when_session_score_meets_the_0_4_threshold(self) -> None:
        # NY Lunch / Asian KZ score 0.5 per session_score's own docstring.
        assert should_trade_now(NY_LUNCH, require_kill_zone=False) is True

    def test_false_when_session_score_is_below_threshold(self) -> None:
        assert should_trade_now(OFF_HOURS, require_kill_zone=False) is False

    def test_true_during_a_peak_kill_zone_even_in_score_mode(self) -> None:
        assert should_trade_now(NY_OPEN, require_kill_zone=False) is True


class TestKillZoneScore:
    def test_london_open_scores_1_0(self) -> None:
        assert kill_zone_score(LONDON_OPEN) == 1.0

    def test_ny_open_scores_1_0(self) -> None:
        assert kill_zone_score(NY_OPEN) == 1.0

    def test_london_close_scores_0_8(self) -> None:
        assert kill_zone_score(LONDON_CLOSE) == 0.8

    def test_asian_scores_0_5(self) -> None:
        assert kill_zone_score(ASIAN) == 0.5

    def test_ny_lunch_scores_0_4(self) -> None:
        assert kill_zone_score(NY_LUNCH) == 0.4

    def test_no_active_kill_zone_scores_0_0(self) -> None:
        assert kill_zone_score(OFF_HOURS) == 0.0
