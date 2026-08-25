"""
Binance account data service.

Fetches and caches account state: balances, positions, leverage configs.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.domain.account import Account, AssetBalance
from src.domain.position import Position
from src.exchange.binance.rest import BinanceFuturesRESTClient

logger = logging.getLogger(__name__)


class BinanceAccountService:
    """
    Provides high-level account data access.
    Aggregates raw API responses into domain objects.
    """

    def __init__(self, client: BinanceFuturesRESTClient) -> None:
        self._client = client

    async def get_account(self) -> Account:
        """Fetch full account state and parse into domain Account."""
        data = await self._client.get_account_info()
        account = Account()

        account.total_wallet_balance = Decimal(str(data.get("totalWalletBalance", "0")))
        account.total_unrealized_pnl = Decimal(str(data.get("totalUnrealizedProfit", "0")))
        account.total_margin_balance = Decimal(str(data.get("totalMarginBalance", "0")))
        account.total_available_balance = Decimal(str(data.get("availableBalance", "0")))
        account.total_initial_margin = Decimal(str(data.get("totalInitialMargin", "0")))
        account.total_maintenance_margin = Decimal(str(data.get("totalMaintMargin", "0")))
        account.can_trade = bool(data.get("canTrade", True))
        account.can_deposit = bool(data.get("canDeposit", True))
        account.can_withdraw = bool(data.get("canWithdraw", True))

        for asset_data in data.get("assets", []):
            asset = asset_data.get("asset", "")
            if not asset:
                continue
            balance = AssetBalance(
                asset=asset,
                wallet_balance=Decimal(str(asset_data.get("walletBalance", "0"))),
                unrealized_pnl=Decimal(str(asset_data.get("unrealizedProfit", "0"))),
                margin_balance=Decimal(str(asset_data.get("marginBalance", "0"))),
                available_balance=Decimal(str(asset_data.get("availableBalance", "0"))),
                initial_margin=Decimal(str(asset_data.get("initialMargin", "0"))),
                maintenance_margin=Decimal(str(asset_data.get("maintMargin", "0"))),
            )
            account.assets[asset] = balance

        return account

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions."""
        return await self._client.get_positions()

    async def get_equity(self) -> Decimal:
        """Return current total equity in USDT."""
        data = await self._client.get_account_info()
        return Decimal(str(data.get("totalMarginBalance", "0")))

    async def get_available_balance(self) -> Decimal:
        """Return available USDT balance."""
        data = await self._client.get_account_info()
        return Decimal(str(data.get("availableBalance", "0")))
