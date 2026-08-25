# Trading System — Smart Money Concepts Algorithmic Trading

Production-grade algorithmic trading system for cryptocurrency futures on
Binance, using Smart Money Concepts (SMC) as the primary trading methodology.

> ⚠️ **Trading cryptocurrency futures carries substantial risk of loss.**
> This software is provided for educational purposes. Test extensively on
> testnet/paper trading before deploying real capital. See [LICENSE](LICENSE)
> for the full disclaimer.

## Features

- **Smart Money Concepts engine**: market structure (BOS/CHoCH), order
  blocks, fair value gaps, liquidity sweeps, premium/discount zones,
  session/kill-zone timing, and a weighted confluence scoring system.
- **Async-first architecture**: built on `asyncio` + `uvloop`, event-driven
  via an internal event bus, with a lightweight dependency-injection
  container.
- **Strict financial correctness**: all monetary values use `Decimal`,
  domain objects are immutable Pydantic models, and the backtest engine
  enforces strict anti-look-ahead-bias guarantees.
- **Full risk management stack**: fixed-fractional position sizing, circuit
  breaker, drawdown/daily-loss limits, stop-loss/take-profit/trailing-stop/
  breakeven management, margin and liquidation calculations.
- **Exchange integration**: Binance USDT-M Futures REST + WebSocket clients
  with HMAC authentication, rate limiting, and automatic reconnection.
- **Persistence**: async SQLAlchemy + PostgreSQL repositories for trades,
  orders, signals, and metrics.
- **Backtesting & optimization**: event-driven backtest engine with
  commission/slippage models and Optuna-based walk-forward optimization.
- **Paper trading**: live market data with simulated fills — test strategies
  risk-free before going live.
- **Monitoring**: Prometheus metrics, structured logging (structlog), health
  checks, heartbeat, watchdog, and a lightweight status dashboard.
- **Notifications**: Telegram, Discord, Slack, and email channels.
- **Full Docker Compose stack**: PostgreSQL, Redis, Prometheus, Grafana.

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your Binance API credentials (use testnet keys first!)

# 2. Install dependencies
make install-dev

# 3. Start infrastructure (Postgres, Redis)
docker-compose up -d postgres redis

# 4. Run database migrations
make migrate

# 5. Run tests
make test

# 6. Start paper trading
make start-paper
```

## Architecture

```
src/
├── core/           # Config, enums, models, exceptions, events, DI container
├── domain/         # Order, Position, Trade, Signal, Account, Portfolio
├── exchange/        # Binance REST + WebSocket clients
├── market_data/      # Candle cache, live feeds
├── indicators/        # ATR, EMA, RSI, MACD, volume, volatility, momentum
├── smart_money/        # SMC: structure, BOS/CHoCH, OBs, FVGs, liquidity, confluence
├── strategy/             # Signal engine, multi-timeframe, orchestrator, filters
├── risk/                  # Risk manager, position sizing, circuit breaker
├── execution/               # Execution engine, order/position managers
├── persistence/               # Async SQLAlchemy repositories
├── analytics/                   # Performance, Sharpe/Sortino, expectancy
├── monitoring/                    # Metrics, health, alerts, telemetry
├── notifications/                   # Telegram, Discord, Slack, email
├── backtesting/                       # Event-driven backtest engine
├── paper_trading/                       # Simulated broker
├── bootstrap/                             # App factory, startup/shutdown
└── main.py                                  # Entry point
```

See [ARCHITECTURE.md](ARCHITECTURE.md) and [DESIGN.md](DESIGN.md) for details.

## Configuration

Configuration is layered: environment variables (`.env`) provide secrets and
infrastructure settings; YAML files in `configs/` (`paper.yaml`,
`testnet.yaml`, `production.yaml`) provide trading parameters per
environment. See `.env.example` for all available environment variables.

## Running a Backtest

```bash
python scripts/backtest.py --symbol BTCUSDT --timeframe 1h --days 180 --save
```

## Running Parameter Optimization

```bash
python scripts/optimize.py --symbol BTCUSDT --timeframe 1h --trials 100
```

## Testing

```bash
make test              # All tests
make test-unit         # Unit tests only
make test-coverage     # With coverage report
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture and layering
- [DESIGN.md](DESIGN.md) — design decisions and trade-offs
- [ROADMAP.md](ROADMAP.md) — planned features
- [SECURITY.md](SECURITY.md) — security policy and reporting
- [CONTRIBUTING.md](CONTRIBUTING.md) — contribution guidelines

## License

MIT — see [LICENSE](LICENSE). Includes a trading-risk disclaimer.
