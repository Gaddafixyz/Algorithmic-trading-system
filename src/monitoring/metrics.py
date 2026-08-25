"""
Prometheus metrics definitions.

All metrics are registered here as module-level singletons.
Import and use them anywhere in the system.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server
import logging

logger = logging.getLogger(__name__)

# ─── Trading metrics ──────────────────────────────────────────────────────────

signals_generated = Counter(
    "trading_signals_total",
    "Total signals generated",
    ["strategy", "symbol", "direction"],
)

orders_submitted = Counter(
    "trading_orders_submitted_total",
    "Total orders submitted",
    ["symbol", "side", "order_type"],
)

orders_filled = Counter(
    "trading_orders_filled_total",
    "Total orders filled",
    ["symbol", "side"],
)

orders_cancelled = Counter(
    "trading_orders_cancelled_total",
    "Total orders cancelled",
    ["symbol", "reason"],
)

trades_closed = Counter(
    "trading_trades_closed_total",
    "Total trades closed",
    ["symbol", "strategy", "outcome"],  # outcome: win/loss/breakeven
)

signals_expired = Counter(
    "trading_signals_expired_total",
    "Total signals expired without being acted on",
    ["symbol", "reason"],  # reason: expired_at_generation / expired_in_transit
)

# ─── Position metrics ─────────────────────────────────────────────────────────

open_positions_count = Gauge(
    "trading_open_positions",
    "Current number of open positions",
)

position_pnl = Gauge(
    "trading_position_pnl_usd",
    "Unrealized PnL of open positions",
    ["symbol"],
)

# ─── Account metrics ──────────────────────────────────────────────────────────

account_equity = Gauge(
    "trading_account_equity_usd",
    "Current account equity in USD",
)

account_daily_pnl = Gauge(
    "trading_account_daily_pnl_usd",
    "Daily realized + unrealized PnL",
)

account_drawdown_pct = Gauge(
    "trading_account_drawdown_pct",
    "Current drawdown percentage from peak",
)

# ─── Market data metrics ───────────────────────────────────────────────────────

orderbook_spread_pct = Gauge(
    "trading_orderbook_spread_pct",
    "Latest best-ask/best-bid spread as a percentage of best_bid",
    ["symbol"],
)

# ─── Risk metrics ─────────────────────────────────────────────────────────────

circuit_breaker_state = Gauge(
    "trading_circuit_breaker_open",
    "1 if circuit breaker is open, 0 if closed",
)

risk_validations = Counter(
    "trading_risk_validations_total",
    "Total risk validations",
    ["result"],  # passed / failed
)

# ─── System metrics ───────────────────────────────────────────────────────────

ws_messages_received = Counter(
    "trading_ws_messages_total",
    "Total WebSocket messages received",
    ["stream"],
)

ws_reconnects = Counter(
    "trading_ws_reconnects_total",
    "Total WebSocket reconnection events",
)

api_requests = Counter(
    "trading_api_requests_total",
    "Total exchange API requests",
    ["method", "endpoint", "status"],
)

api_latency = Histogram(
    "trading_api_latency_seconds",
    "Exchange API request latency",
    ["endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

candle_processing_latency = Histogram(
    "trading_candle_processing_seconds",
    "Time to process a candle close through strategy",
    ["symbol", "timeframe"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)

system_info = Info("trading_system", "Trading system version information")


def start_metrics_server(port: int = 9090) -> None:
    """Start the Prometheus metrics HTTP server."""
    start_http_server(port)
    logger.info("Prometheus metrics server started on port %d", port)
