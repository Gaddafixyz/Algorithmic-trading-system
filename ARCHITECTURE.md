# Architecture

## Layering

The system follows Clean Architecture principles with strict separation of
concerns. Dependencies point inward: outer layers depend on inner layers,
never the reverse.

```
┌─────────────────────────────────────────────────────┐
│  bootstrap/ (app wiring, startup/shutdown)            │
├─────────────────────────────────────────────────────┤
│  strategy/ execution/ risk/ backtesting/ paper_trading/│
├─────────────────────────────────────────────────────┤
│  smart_money/ indicators/ market_data/ analytics/      │
├─────────────────────────────────────────────────────┤
│  domain/ (Order, Position, Trade, Signal, Portfolio)    │
├─────────────────────────────────────────────────────┤
│  core/ (config, enums, models, events, exceptions, DI)   │
└─────────────────────────────────────────────────────┘
        ↑                                  ↑
  exchange/ (Binance)              persistence/ (PostgreSQL)
  notifications/                   monitoring/
```

`core/` has no dependencies on any other layer — it is pure infrastructure
(configuration, value objects, the event bus, exceptions). `domain/` depends
only on `core/`. Everything else depends on `domain/` and `core/` but layers
at the same level (`strategy/`, `risk/`, `execution/`) communicate primarily
through the event bus rather than direct calls, to keep them decoupled.

## Event-Driven Communication

The system uses an async, in-process event bus (`core/events.py`) as the
primary integration mechanism between major subsystems:

```
MarketDataFeed → CandleClosedEvent → StrategyOrchestrator
                                            │
                                  SignalGeneratedEvent
                                            │
                                            ▼
                                    ExecutionEngine
                                       │        │
                              OrderSubmittedEvent  RiskManager.validate_signal()
                                       │
                                  OrderFilledEvent
                                       │
                                       ▼
                              PositionOpenedEvent / PositionClosedEvent
```

Handlers are registered per event type and run concurrently via
`asyncio.gather`. A failing handler is logged but does not block other
handlers — this prevents one buggy notification channel from halting order
execution, for example.

## Anti-Look-Ahead-Bias Guarantee

The backtesting engine (`backtesting/engine.py`) enforces a hard invariant:
on bar `i`, the strategy only ever receives `candles[0:i+1]`. Orders placed
on bar `i` are filled at bar `i+1`'s open price — never at the signal bar's
own close or any future price. This mirrors realistic live execution where a
signal generated on a closed candle can only be acted on going forward.

## Dependency Injection

`core/dependency.py` provides a lightweight DI container supporting
singleton and factory bindings, with async resolution support. The
`bootstrap/dependency_container.py` module wires all concrete
implementations (Binance clients, risk manager, etc.) into both the DI
container and a simpler `ComponentRegistry` (`core/registry.py`) for direct
named lookup where DI is overkill.

## Financial Precision

All monetary values — prices, quantities, P&L, fees — are represented as
Python `Decimal`, never `float`. Conversions from external sources (exchange
JSON responses) always go through `Decimal(str(value))` to avoid the
float-to-Decimal precision trap. Domain models (`Order`, `Position`,
`Trade`, `Signal`) validate this invariant via Pydantic field validators.

## Strategy Execution Flow

1. `MarketDataFeed` subscribes to Binance WebSocket kline streams.
2. On candle close, a `CandleClosedEvent` is published.
3. `StrategyOrchestrator` routes the event to registered strategies whose
   `symbols`/`timeframes` match.
4. The strategy (`SMCStrategy` → `SMCSignalEngine`) runs the full SMC
   analysis pipeline: market structure → BOS/CHoCH → order blocks → FVGs →
   liquidity → premium/discount → confluence scoring.
5. If the confluence score clears the minimum threshold, a `Signal` is
   returned and a `SignalGeneratedEvent` is published.
6. `ExecutionEngine` receives the event, validates it through
   `RiskManager.validate_signal()`, sizes the position via
   `RiskManager.calculate_position_size()`, and submits the entry order.
7. On fill, `ExecutionEngine` opens a `Position` and places bracket
   stop-loss/take-profit orders.

## Testing Strategy

- **Unit tests** (`tests/unit/`): pure functions and domain logic in
  isolation — indicators, SMC detection algorithms, domain model
  invariants, risk calculations.
- **Integration tests** (`tests/integration/`, `tests/strategy/`,
  `tests/risk/`, `tests/execution/`): multiple components wired together,
  e.g. the full SMC signal engine pipeline or the risk manager's
  multi-step validation gate.
- **End-to-end tests** (`tests/end_to_end/`): full backtest runs against
  synthetic or recorded historical data.

All exchange-dependent tests use mocked clients (`unittest.mock.AsyncMock`)
rather than live network calls.
