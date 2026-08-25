"""
Account and balance domain models.

Represents the state of a trading account: equity, available capital,
margin usage, and per-asset balances.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from src.core.models import MutableModel, StrictModel, utc_now


class AssetBalance(StrictModel):
    """Balance for a single asset."""

    asset: str
    wallet_balance: Decimal          # Total in wallet
    unrealized_pnl: Decimal = Decimal("0")
    margin_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    initial_margin: Decimal = Decimal("0")
    maintenance_margin: Decimal = Decimal("0")
    position_initial_margin: Decimal = Decimal("0")
    open_order_initial_margin: Decimal = Decimal("0")
    cross_wallet_balance: Decimal = Decimal("0")
    cross_unrealized_pnl: Decimal = Decimal("0")
    isolated_wallet: Decimal = Decimal("0")

    @property
    def equity(self) -> Decimal:
        return self.wallet_balance + self.unrealized_pnl

    @property
    def margin_usage_pct(self) -> Decimal:
        if self.equity == Decimal("0"):
            return Decimal("0")
        return (self.initial_margin / self.equity) * Decimal("100")


class Account(MutableModel):
    """
    Trading account state snapshot.

    Updated on each balance update from the exchange.
    """

    account_id: str = "default"
    total_wallet_balance: Decimal = Decimal("0")
    total_unrealized_pnl: Decimal = Decimal("0")
    total_margin_balance: Decimal = Decimal("0")
    total_available_balance: Decimal = Decimal("0")
    total_initial_margin: Decimal = Decimal("0")
    total_maintenance_margin: Decimal = Decimal("0")
    total_position_initial_margin: Decimal = Decimal("0")
    total_open_order_initial_margin: Decimal = Decimal("0")

    assets: dict[str, AssetBalance] = Field(default_factory=dict)

    can_trade: bool = True
    can_deposit: bool = True
    can_withdraw: bool = True

    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def equity(self) -> Decimal:
        return self.total_wallet_balance + self.total_unrealized_pnl

    @property
    def margin_ratio(self) -> Decimal:
        """Margin ratio: maintenance_margin / margin_balance."""
        if self.total_margin_balance == Decimal("0"):
            return Decimal("0")
        return self.total_maintenance_margin / self.total_margin_balance

    @property
    def leverage_used(self) -> Decimal:
        """Effective leverage: initial_margin_positions / equity."""
        if self.equity == Decimal("0"):
            return Decimal("0")
        return self.total_position_initial_margin / self.equity

    @property
    def usdt_balance(self) -> AssetBalance | None:
        return self.assets.get("USDT")

    @property
    def available_usdt(self) -> Decimal:
        usdt = self.usdt_balance
        return usdt.available_balance if usdt else Decimal("0")

    def update_from_exchange(self, data: dict[str, Any]) -> None:
        """Update account state from raw exchange response."""
        self.total_wallet_balance = Decimal(str(data.get("totalWalletBalance", "0")))
        self.total_unrealized_pnl = Decimal(str(data.get("totalUnrealizedProfit", "0")))
        self.total_margin_balance = Decimal(str(data.get("totalMarginBalance", "0")))
        self.total_available_balance = Decimal(str(data.get("availableBalance", "0")))
        self.total_initial_margin = Decimal(str(data.get("totalInitialMargin", "0")))
        self.total_maintenance_margin = Decimal(str(data.get("totalMaintMargin", "0")))
        self.updated_at = utc_now()
