# CLAUDE.md

Context for AI assistants (Claude Code, etc.) working in this repository.

## Project Summary

Production algorithmic trading system for Binance USDT-M Futures using
Smart Money Concepts (SMC). Python 3.13, async-first, event-driven.

## Non-negotiable Rules

1. **Decimal only for money.** Never use `float` for prices, quantities,
   P&L, fees, or any financial value. Always `Decimal(str(value))` when
   converting from external sources.
2. **No look-ahead bias.** Backtesting and strategy code must never access
   data beyond the current bar. The backtest engine fills orders at the
   *next* bar's open, never the signal bar's close.
3. **Validate before trade.** Every signal must pass through
   `RiskManager.validate_signal()` before an order is submitted. Never
   bypass this gate.
4. **No secrets in code.** Credentials come from environment variables
   only (`src/core/config.py` via `pydantic-settings`).
5. **Async I/O.** Any function touching the network, database, or
   filesystem in the hot path must be `async def`.

## Where Things Live

- Domain models: `src/domain/` (Order, Position, Trade, Signal, Portfolio)
- SMC detection logic: `src/smart_money/` (one file per concept: BOS,
  CHoCH, order blocks, FVGs, liquidity, etc.)
- Signal generation orchestration: `src/strategy/signal_engine.py`
- Risk gate: `src/risk/risk_manager.py`
- Order/position lifecycle: `src/execution/execution_engine.py`
- Exchange client: `src/exchange/binance/`
- Tests mirror `src/` structure under `tests/unit/`, `tests/integration/`,
  `tests/strategy/`, `tests/risk/`, `tests/execution/`.

## Common Tasks

**Add a new indicator**: create `src/indicators/your_indicator.py` with a
pure function `calculate_x(candles_or_values, period) -> np.ndarray` that
raises `InsufficientDataError` if there isn't enough data. Add unit tests
to `tests/unit/test_indicators.py`.

**Add a new SMC pattern**: see `CONTRIBUTING.md` → "Adding a New SMC
Detection Module".

**Modify risk limits**: edit `src/core/config.py::RiskConfig` for new
parameters, then wire validation logic into
`src/risk/risk_manager.py::RiskManager.validate_signal()`.

**Run a single test file**: `pytest tests/unit/test_indicators.py -v`

**Run the full suite**: `make test`

## Testing Conventions

- Mock all exchange calls with `unittest.mock.AsyncMock` — never hit the
  real Binance API in tests.
- Use `pytest.mark.asyncio` for async test functions (configured via
  `asyncio_mode = "auto"` in `pyproject.toml`, so the marker is often
  implicit but still fine to add explicitly).
- Synthetic candle generators (`make_candle`, `make_trending_candles`,
  etc.) are duplicated per test file by design — keep them self-contained
  rather than introducing a shared test-fixtures module, to keep test
  files independently readable.
