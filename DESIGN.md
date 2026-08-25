# Design Decisions

## Why Smart Money Concepts?

SMC (market structure, order blocks, fair value gaps, liquidity) provides a
rules-based framework for identifying institutional order flow that is more
amenable to systematic implementation than purely indicator-based strategies,
while still requiring genuine market-structure reasoning rather than naive
pattern matching.

## Why fixed-fractional position sizing?

Fixed-fractional risk (risk a constant % of equity per trade) was chosen
over Kelly Criterion as the *default* sizing model because:

1. It is far less sensitive to estimation error in win-rate/payoff-ratio
   inputs, which are themselves noisy in live trading.
2. It naturally compounds (position size scales with equity) without the
   extreme volatility full Kelly sizing introduces.
3. It is simple to reason about and audit.

A Kelly-based sizer (`risk/position_sizing.py::kelly_position_size`) is
provided as an opt-in alternative for users with sufficient trade-history
data to estimate win rate and payoff ratio reliably; it defaults to
fractional Kelly (25%) to reduce variance.

## Why Decimal everywhere?

Floating-point arithmetic introduces small representation errors that
compound across thousands of trades and can cause real financial
discrepancies (e.g., `0.1 + 0.2 != 0.3` in IEEE 754 double precision).
`Decimal` with explicit precision is the only safe choice for anything
touching money. The performance cost is acceptable given the system's
candle-close-driven (not tick-driven) decision cadence.

## Why an in-process event bus instead of message queue (Kafka/RabbitMQ)?

For a single-process trading bot, an external message broker adds
operational complexity (additional service to deploy, monitor, and secure)
without commensurate benefit. The in-process async event bus
(`core/events.py`) gives the same decoupling benefits — components publish
and subscribe without direct references to each other — while keeping
deployment to a single container. If the system later needs to scale to
multiple processes/symbols-per-worker, the event bus's publish/subscribe
interface could be swapped for a Redis Streams or Kafka-backed
implementation without changing calling code, since events are already
serializable Pydantic models.

## Why next-bar-open fills in the backtester?

Filling orders at the *current* bar's close (the same bar the signal was
generated on) is a common and serious backtesting bug — it assumes you can
trade at a price that was only confirmed after you already "knew" it. Filling
at the *next* bar's open is the standard, conservative correction: the
signal is generated using only data available up to and including bar `i`,
and the earliest realistic fill is the next bar's open.

## Why isolated margin by default?

Isolated margin contains losses to the margin allocated to that specific
position, rather than risking the entire account balance (cross margin) on
a single position's adverse move. For a multi-symbol, multi-position
system, isolating risk per position is a safer default; cross margin is
exposed as a configuration option (`MarginType.CROSS`) for users who
understand and accept that trade-off.

## Why structlog + Prometheus rather than a hosted observability SaaS?

Self-hosted Prometheus + Grafana (via the bundled `docker-compose.yml`)
keeps the system's full data — including potentially sensitive trading
activity — within the user's own infrastructure rather than a third-party
SaaS, which matters for a system handling real trading credentials and P&L
data.

## Known Limitations

- The SMC confluence scoring weights (`smart_money/confluence.py`) are
  reasonable defaults but have **not** been empirically validated against
  historical performance in this codebase; users should run
  `scripts/optimize.py` against their own target symbols/timeframes before
  trusting the default thresholds in live trading.
- The backtester does not model partial fills, order book depth, or
  funding-rate accrual during the holding period (funding is tracked as a
  field on `Position` but the backtest engine does not currently populate
  it from historical funding-rate data).
- ~~WebSocket user-data-stream listen-key renewal is not implemented~~ —
  implemented: `BinanceFuturesWebSocket._renewal_loop()` calls
  `BinanceFuturesRESTClient.renew_listen_key()` every
  `BINANCE_LISTEN_KEY_REFRESH_MINUTES` (30 min, see `core/constants.py`),
  wired up in `bootstrap/startup.py` before `ws_client.start()`. This note
  was stale as of a prior session; corrected here so it doesn't get
  mistaken for an open item in live-readiness review.
