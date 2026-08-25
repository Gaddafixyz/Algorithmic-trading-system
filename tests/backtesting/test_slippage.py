"""Unit tests for src.backtesting.slippage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.backtesting.slippage import FixedSlippage, SpreadSlippage, ZeroSlippage
from src.core.models import OHLCV


def make_candle(o: str = "100", h: str = "102", low: str = "98", c: str = "100") -> OHLCV:
    return OHLCV(
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("1000"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


class TestFixedSlippage:
    def test_buy_increases_the_price(self) -> None:
        model = FixedSlippage(slippage_pct=Decimal("0.001"))
        result = model.apply(Decimal("100"), is_buy=True, candle=make_candle())
        assert result == Decimal("100") * (Decimal("1") + Decimal("0.001"))

    def test_sell_decreases_the_price(self) -> None:
        model = FixedSlippage(slippage_pct=Decimal("0.001"))
        result = model.apply(Decimal("100"), is_buy=False, candle=make_candle())
        assert result == Decimal("100") * (Decimal("1") - Decimal("0.001"))

    def test_uses_the_default_slippage_pct_when_not_specified(self) -> None:
        model = FixedSlippage()
        result = model.apply(Decimal("100"), is_buy=True, candle=make_candle())
        assert result == Decimal("100") * (Decimal("1") + Decimal("0.0001"))


class TestZeroSlippage:
    def test_returns_the_price_unchanged_for_buy(self) -> None:
        model = ZeroSlippage()
        assert model.apply(Decimal("100"), is_buy=True, candle=make_candle()) == Decimal("100")

    def test_returns_the_price_unchanged_for_sell(self) -> None:
        model = ZeroSlippage()
        assert model.apply(Decimal("100"), is_buy=False, candle=make_candle()) == Decimal("100")


class TestSpreadSlippage:
    def test_buy_adds_half_the_spread_fraction(self) -> None:
        model = SpreadSlippage(spread_fraction=Decimal("0.5"))
        candle = make_candle(h="102", low="98")  # range = 4
        result = model.apply(Decimal("100"), is_buy=True, candle=candle)
        # half_spread = 4 * 0.5 / 2 = 1.0
        assert result == Decimal("101")

    def test_sell_subtracts_half_the_spread_fraction(self) -> None:
        model = SpreadSlippage(spread_fraction=Decimal("0.5"))
        candle = make_candle(h="102", low="98")
        result = model.apply(Decimal("100"), is_buy=False, candle=candle)
        assert result == Decimal("99")

    def test_uses_the_default_spread_fraction_of_0_3(self) -> None:
        model = SpreadSlippage()
        candle = make_candle(h="102", low="98")  # range = 4
        result = model.apply(Decimal("100"), is_buy=True, candle=candle)
        # half_spread = 4 * 0.3 / 2 = 0.6
        assert result == Decimal("100.6")

    def test_zero_range_candle_produces_zero_slippage(self) -> None:
        model = SpreadSlippage()
        candle = make_candle(o="100", h="100", low="100", c="100")
        result = model.apply(Decimal("100"), is_buy=True, candle=candle)
        assert result == Decimal("100")
