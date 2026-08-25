"""Tests for src.persistence.signals.SignalRepository."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.domain.signal import Signal
from src.persistence.signals import SignalRepository


def make_signal(symbol: str = "BTCUSDT", strategy_id: str = "strat-1") -> Signal:
    return Signal(
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=Timeframe.H1,
        direction=SignalDirection.LONG,
        score=70.0,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
    )


class TestSaveSignal:
    async def test_persists_all_mapped_fields(self, db_session: AsyncSession) -> None:
        repo = SignalRepository(db_session)
        signal = make_signal()
        result = await repo.save_signal(signal)

        assert result.signal_id == signal.signal_id
        assert result.strategy_id == "strat-1"
        assert result.symbol == "BTCUSDT"
        assert result.direction == "long"
        assert result.score == 70.0
        assert result.confidence == 0.7
        assert result.entry_price == Decimal("50000")
        assert result.stop_loss == Decimal("49000")
        assert result.take_profit == Decimal("52000")

    async def test_atr_none_maps_to_null(self, db_session: AsyncSession) -> None:
        repo = SignalRepository(db_session)
        result = await repo.save_signal(make_signal())
        assert result.atr is None

    async def test_two_signals_for_different_symbols_are_both_persisted(
        self, db_session: AsyncSession
    ) -> None:
        repo = SignalRepository(db_session)
        await repo.save_signal(make_signal(symbol="BTCUSDT"))
        await repo.save_signal(make_signal(symbol="ETHUSDT"))
        assert await repo.count() == 2
