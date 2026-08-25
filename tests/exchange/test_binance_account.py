"""Unit tests for src.exchange.binance.account.BinanceAccountService.

BinanceAccountService is now wired into Application (see
bootstrap/app.py: self.account_service, and sync_equity()) and
registered in the DI container/registry as "account_service".
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.enums import PositionSide
from src.domain.position import Position
from src.exchange.binance.account import BinanceAccountService
from src.exchange.binance.rest import BinanceFuturesRESTClient


@pytest.fixture
def rest_client() -> AsyncMock:
    return AsyncMock(spec=BinanceFuturesRESTClient)


@pytest.fixture
def account_service(rest_client: AsyncMock) -> BinanceAccountService:
    return BinanceAccountService(rest_client)


class TestGetAccount:
    async def test_parses_top_level_account_fields(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        rest_client.get_account_info.return_value = {
            "totalWalletBalance": "10000", "totalUnrealizedProfit": "500",
            "totalMarginBalance": "10500", "availableBalance": "9000",
            "totalInitialMargin": "1000", "totalMaintMargin": "100",
            "canTrade": True, "canDeposit": True, "canWithdraw": False,
            "assets": [],
        }
        account = await account_service.get_account()
        assert account.total_wallet_balance == Decimal("10000")
        assert account.total_unrealized_pnl == Decimal("500")
        assert account.can_withdraw is False

    async def test_parses_per_asset_balances(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        rest_client.get_account_info.return_value = {
            "totalWalletBalance": "0", "totalUnrealizedProfit": "0",
            "totalMarginBalance": "0", "availableBalance": "0",
            "totalInitialMargin": "0", "totalMaintMargin": "0",
            "assets": [
                {"asset": "USDT", "walletBalance": "10000", "unrealizedProfit": "0",
                 "marginBalance": "10000", "availableBalance": "9000",
                 "initialMargin": "1000", "maintMargin": "100"},
            ],
        }
        account = await account_service.get_account()
        assert "USDT" in account.assets
        assert account.assets["USDT"].wallet_balance == Decimal("10000")

    async def test_skips_asset_entries_with_no_asset_name(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        rest_client.get_account_info.return_value = {
            "totalWalletBalance": "0", "totalUnrealizedProfit": "0",
            "totalMarginBalance": "0", "availableBalance": "0",
            "totalInitialMargin": "0", "totalMaintMargin": "0",
            "assets": [{"asset": "", "walletBalance": "100"}],
        }
        account = await account_service.get_account()
        assert account.assets == {}

    async def test_missing_fields_default_to_documented_values(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        rest_client.get_account_info.return_value = {}
        account = await account_service.get_account()
        assert account.total_wallet_balance == Decimal("0")
        assert account.can_trade is True  # documented default


class TestGetPositions:
    async def test_delegates_to_the_rest_client(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        pos = Position(
            symbol="BTCUSDT", side=PositionSide.LONG, quantity=Decimal("0.1"),
            entry_quantity=Decimal("0.1"), entry_price=Decimal("50000"),
            mark_price=Decimal("50000"),
        )
        rest_client.get_positions.return_value = [pos]
        result = await account_service.get_positions()
        assert result == [pos]


class TestGetEquity:
    async def test_returns_total_margin_balance(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        rest_client.get_account_info.return_value = {"totalMarginBalance": "12345.67"}
        result = await account_service.get_equity()
        assert result == Decimal("12345.67")


class TestGetAvailableBalance:
    async def test_returns_available_balance(
        self, account_service: BinanceAccountService, rest_client: AsyncMock
    ) -> None:
        rest_client.get_account_info.return_value = {"availableBalance": "9876.54"}
        result = await account_service.get_available_balance()
        assert result == Decimal("9876.54")
