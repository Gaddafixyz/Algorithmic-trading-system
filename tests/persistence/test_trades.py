"""Tests for src.persistence.trades.TradeRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import PositionSide
from src.domain.trade import Trade
from src.persistence.trades import TradeRepository


def make_trade(
    symbol: str = "BTCUSDT",
    strategy_id: str = "strat-1",
    position_id: str = "pos-1",
) -> Trade:
    now = datetime.now(UTC)
    return Trade(
        position_id=position_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        entry_quantity=Decimal("0.1"),
        entry_price=Decimal("50000"),
        exit_price=Decimal("51000"),
        gross_pnl=Decimal("100"),
        fees=Decimal("2"),
        opened_at=now,
        closed_at=now,
    )


class TestSaveTrade:
    async def test_persists_all_mapped_fields(self, db_session: AsyncSession) -> None:
        repo = TradeRepository(db_session)
        trade = make_trade()
        result = await repo.save_trade(trade)

        assert result.trade_id == trade.trade_id
        assert result.position_id == "pos-1"
        assert result.symbol == "BTCUSDT"
        assert result.side == "long"
        assert result.entry_price == Decimal("50000")
        assert result.exit_price == Decimal("51000")
        assert result.gross_pnl == Decimal("100")
        assert result.fees == Decimal("2")

    async def test_timeframe_none_maps_to_null(self, db_session: AsyncSession) -> None:
        repo = TradeRepository(db_session)
        result = await repo.save_trade(make_trade())
        assert result.timeframe is None


class TestFindByStrategy:
    async def test_returns_only_trades_for_the_given_strategy(
        self, db_session: AsyncSession
    ) -> None:
        repo = TradeRepository(db_session)
        await repo.save_trade(make_trade(strategy_id="strat-a", position_id="p1"))
        await repo.save_trade(make_trade(strategy_id="strat-b", position_id="p2"))

        result = await repo.find_by_strategy("strat-a")
        assert len(result) == 1
        assert result[0].strategy_id == "strat-a"

    async def test_returns_empty_list_for_an_unknown_strategy(
        self, db_session: AsyncSession
    ) -> None:
        repo = TradeRepository(db_session)
        await repo.save_trade(make_trade())
        assert await repo.find_by_strategy("nonexistent") == []


class TestFindBySymbol:
    async def test_returns_only_trades_for_the_given_symbol(
        self, db_session: AsyncSession
    ) -> None:
        repo = TradeRepository(db_session)
        await repo.save_trade(make_trade(symbol="BTCUSDT", position_id="p1"))
        await repo.save_trade(make_trade(symbol="ETHUSDT", position_id="p2"))

        result = await repo.find_by_symbol("ETHUSDT")
        assert len(result) == 1
        assert result[0].symbol == "ETHUSDT"
