"""
Abstract interfaces (protocols) for the trading system.

Using Protocol (structural subtyping) where possible to allow duck-typing
without forcing inheritance hierarchies.

These interfaces define the contracts between layers:
  - Exchange abstraction
  - Market data feeds
  - Strategy
  - Risk management
  - Execution
  - Persistence
  - Notifications
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.core.enums import OrderSide, OrderType, PositionSide, Timeframe
    from src.core.models import OHLCV, OrderBook, Tick, Ticker
    from src.domain.order import Order
    from src.domain.position import Position
    from src.domain.signal import Signal


# ─── Exchange ────────────────────────────────────────────────────────────────

@runtime_checkable
class IExchangeClient(Protocol):
    """Contract for exchange REST/WebSocket clients."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def get_account_balance(self) -> dict[str, Decimal]: ...
    async def get_positions(self) -> list["Position"]: ...

    async def place_order(
        self,
        symbol: str,
        side: "OrderSide",
        order_type: "OrderType",
        quantity: Decimal,
        price: Decimal | None,
        *,
        stop_price: Decimal | None = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> "Order": ...

    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    async def get_order(self, symbol: str, order_id: str) -> "Order": ...
    async def get_open_orders(self, symbol: str | None = None) -> list["Order"]: ...

    async def get_candles(
        self,
        symbol: str,
        timeframe: "Timeframe",
        limit: int,
        start_time: datetime | None = None,
    ) -> list["OHLCV"]: ...

    async def get_ticker(self, symbol: str) -> "Ticker": ...
    async def get_order_book(self, symbol: str, depth: int) -> "OrderBook": ...

    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
    async def set_margin_type(self, symbol: str, margin_type: str) -> None: ...

    async def get_exchange_info(self) -> dict[str, Any]: ...


@runtime_checkable
class IWebSocketStreamClient(Protocol):
    """
    Contract for exchange WebSocket streaming clients.

    Kept separate from ``IExchangeClient`` (REST) because provider-specific
    wiring (app.py, exchange_factory) needs to construct and manage the two
    independently — a WS client has its own connect/disconnect lifecycle,
    subscription surface, and reconnect behaviour that doesn't map onto the
    REST contract. Modeled on ``BinanceFuturesWebSocket`` /
    ``BybitFuturesWebSocket``, which already share this exact surface
    (Bybit's wire format is normalized to Binance-shaped dicts at the
    adapter boundary, so handlers work unmodified across providers).
    """

    #: Short exchange identifier ("binance", "bybit", ...). Lets
    #: provider-agnostic consumers (MarketDataFeed, CandleService) label
    #: events/logs correctly without importing a concrete client class.
    provider: str

    def subscribe_kline(self, symbol: str, timeframe: "Timeframe", handler: Any) -> str: ...
    def subscribe_agg_trade(self, symbol: str, handler: Any) -> str: ...
    def subscribe_book_ticker(self, symbol: str, handler: Any) -> str: ...
    def subscribe_depth(self, symbol: str, depth: int, speed_ms: int, handler: Any) -> str: ...
    def subscribe_mark_price(self, symbol: str, handler: Any) -> str: ...
    def subscribe_user_data(self, handler: Any) -> None: ...
    def set_reconnect_callback(self, callback: Any) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    @property
    def stats(self) -> dict[str, int]: ...


# ─── Market Data ─────────────────────────────────────────────────────────────

@runtime_checkable
class IMarketDataFeed(Protocol):
    """Contract for live market data subscriptions."""

    async def subscribe_candles(
        self,
        symbol: str,
        timeframe: "Timeframe",
        callback: Any,
    ) -> None: ...

    async def subscribe_ticks(self, symbol: str, callback: Any) -> None: ...
    async def subscribe_order_book(self, symbol: str, depth: int, callback: Any) -> None: ...
    async def unsubscribe_all(self) -> None: ...


@runtime_checkable
class ICandleProvider(Protocol):
    """Contract for OHLCV data access."""

    async def get_candles(
        self,
        symbol: str,
        timeframe: "Timeframe",
        limit: int,
        end_time: datetime | None = None,
    ) -> list["OHLCV"]: ...


# ─── Strategy ─────────────────────────────────────────────────────────────────

class IStrategy(ABC):
    """Abstract base for trading strategies."""

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Unique strategy identifier."""

    @property
    @abstractmethod
    def symbols(self) -> list[str]:
        """Symbols this strategy trades."""

    @property
    @abstractmethod
    def timeframes(self) -> list["Timeframe"]:
        """Timeframes this strategy uses."""

    @abstractmethod
    async def on_candle(
        self,
        symbol: str,
        timeframe: "Timeframe",
        candles: list["OHLCV"],
    ) -> "Signal | None":
        """Process new candle data and optionally return a signal."""

    @abstractmethod
    async def on_tick(self, tick: "Tick") -> None:
        """Handle real-time tick update."""

    @abstractmethod
    async def initialize(self) -> None:
        """Load required data and warm up indicators."""

    @abstractmethod
    async def on_position_closed(self, position: "Position") -> None:
        """Callback when a managed position closes."""


# ─── Risk ────────────────────────────────────────────────────────────────────

@runtime_checkable
class IRiskManager(Protocol):
    """Contract for pre-trade risk validation."""

    async def validate_signal(self, signal: "Signal") -> bool: ...
    async def calculate_position_size(self, signal: "Signal") -> Decimal: ...
    async def check_portfolio_risk(self) -> bool: ...
    async def on_position_update(self, position: "Position") -> None: ...


# ─── Execution ───────────────────────────────────────────────────────────────

@runtime_checkable
class IOrderManager(Protocol):
    """Contract for order lifecycle management."""

    async def submit_order(self, order: "Order") -> "Order": ...
    async def cancel_order(self, order_id: str, symbol: str) -> bool: ...
    async def get_order(self, order_id: str) -> "Order | None": ...
    async def cancel_all_orders(self, symbol: str | None = None) -> int: ...
    # get_open_orders/persist_order were missing from this contract despite
    # being the two other OrderManager methods real consumers actually call
    # (ExecutionEngine.get_open_orders, ReconciliationService.get_open_orders
    # + persist_order) — added here rather than left silently uncovered when
    # this Protocol was finally wired as a type hint in [1.3.22].
    def get_open_orders(self, symbol: str | None = None) -> list["Order"]: ...
    async def persist_order(self, order: "Order") -> None: ...


# ─── Persistence ─────────────────────────────────────────────────────────────

@runtime_checkable
class IRepository(Protocol):
    """Generic repository contract."""

    async def save(self, entity: Any) -> None: ...
    async def find_by_id(self, entity_id: str) -> Any | None: ...
    async def find_all(self, **filters: Any) -> list[Any]: ...
    async def delete(self, entity_id: str) -> bool: ...


# ─── Notifications ───────────────────────────────────────────────────────────

@runtime_checkable
class INotifier(Protocol):
    """Contract for notification channels."""

    async def send(self, message: str, severity: str) -> bool: ...
    async def send_trade_notification(self, trade_data: dict[str, Any]) -> bool: ...


# ─── Indicators ──────────────────────────────────────────────────────────────

class IIndicator(ABC):
    """Abstract base for technical indicators."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def min_periods(self) -> int:
        """Minimum data points required for valid output."""

    @abstractmethod
    def calculate(self, data: Any) -> Any:
        """Calculate indicator values from OHLCV data."""

    @abstractmethod
    def update(self, candle: "OHLCV") -> Any:
        """Incrementally update with a new candle (streaming mode)."""


# ─── Lifecycle ───────────────────────────────────────────────────────────────

class ILifecycle(ABC):
    """Component with async startup/shutdown."""

    @abstractmethod
    async def start(self) -> None:
        """Start the component."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the component gracefully."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True if component is currently active."""


# ─── Health ──────────────────────────────────────────────────────────────────

@runtime_checkable
class IHealthCheck(Protocol):
    """Contract for health-checkable components."""

    @property
    def component_name(self) -> str: ...

    async def health_check(self) -> dict[str, Any]: ...
