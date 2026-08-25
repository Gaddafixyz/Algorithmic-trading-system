9# Trading System

Production algorithmic trading system implementing Smart Money Concepts (SMC)
for Binance USDT-M Perpetual Futures.

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 (async-first) |
| Exchange | Binance USDT-M Futures (REST + WebSocket) |
| Database | PostgreSQL 16 via SQLAlchemy 2.0 / asyncpg |
| Cache | Redis 7 |
| Config | Pydantic-settings + YAML layering + `.env` |
| Migrations | Alembic (`alembic.ini` + `src/persistence/alembic/`) |
| Monitoring | Prometheus metrics + structlog + HTTP dashboard |
| Backtesting | Event-driven, next-bar-open fills, funding-rate accrual |
| ML filter | XGBoost classifier (`src/machine_learning/`) |
| Packaging | `pyproject.toml` (setuptools) |

## Project layout

```
trading-system/
├── src/
│   ├── core/           # Constants, enums, exceptions, interfaces, models
│   ├── domain/         # Order, Position, Trade, Signal, Portfolio (Decimal-safe)
│   ├── exchange/       # Binance REST + WebSocket clients
│   ├── market_data/    # Candle cache, live feed
│   ├── smart_money/    # SMC engine: BOS, CHoCH, OB, FVG, liquidity, confluence
│   ├── strategy/       # SMCSignalEngine, orchestrator
│   ├── risk/           # RiskManager, position sizing, exposure, correlation
│   ├── execution/      # ExecutionEngine, OrderManager, FillEngine
│   ├── backtesting/    # BacktestEngine, BacktestBroker, slippage, commission
│   ├── machine_learning/  # SignalFilter, SignalFilterTrainer, features
│   ├── analytics/      # PerformanceReport, ratios
│   ├── persistence/    # SQLAlchemy ORM + Alembic migrations
│   ├── monitoring/     # Prometheus, heartbeat, watchdog, dashboard, alerts
│   ├── notifications/  # Telegram, Discord, Slack, Email
│   ├── paper_trading/  # Simulated broker driven by live market data
│   └── main.py         # Async entrypoint
├── scripts/            # CLI: start, backtest, optimize, migrate_db, export_data
├── configs/            # YAML configs: strategy, risk, exchanges, symbols
├── tests/              # pytest: unit, integration, end_to_end, performance
├── deployments/
│   ├── kubernetes/     # deployment.yaml, service.yaml, configmap.yaml, hpa.yaml
│   └── terraform/      # main.tf, variables.tf, outputs.tf (AWS ECS Fargate)
├── docs/               # Architecture, strategy, operations, API docs
├── alembic.ini         # Alembic CLI configuration
├── docker-compose.yml  # App + PostgreSQL + Redis + Prometheus + Grafana
├── Dockerfile          # Multi-stage, non-root build
└── pyproject.toml      # Deps, ruff, mypy, pytest config
```

## How to run

### Quick start (Docker Compose — recommended)

```bash
cp .env.example .env
# Edit .env: set BINANCE_API_KEY, BINANCE_API_SECRET, POSTGRES_PASSWORD
docker compose up -d
```

### Local development

```bash
# Install dependencies
pip install -e ".[dev]"

# Apply DB migrations
alembic upgrade head

# Run in paper-trading mode
TRADING_ENV=paper python scripts/start.py
```

### Database migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Generate a new migration after ORM model changes
alembic revision --autogenerate -m "add_foo_column"

# Stamp an existing DB to the baseline (skip DDL re-run)
alembic stamp 001
```

### Backtesting

```bash
python scripts/backtest.py --symbol BTCUSDT --timeframe 1h --days 90
```

### Parameter optimisation

```bash
python scripts/optimize.py --symbol BTCUSDT --timeframe 1h --trials 100
```

## Required environment variables

| Variable | Description |
|---|---|
| `BINANCE_API_KEY` | Binance Futures API key |
| `BINANCE_API_SECRET` | Binance Futures API secret |
| `DATABASE_URL` | PostgreSQL URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | Redis URL (`redis://localhost:6379/0`) |
| `TRADING_ENV` | `paper` / `testnet` / `production` |

See `.env.example` for the full list.

## Code quality gates

```bash
make lint       # ruff
make typecheck  # mypy --strict
make test       # pytest (≥ 80% coverage required)
```

All three must pass before merging.  See `AGENTS.md` for the full checklist.

## User preferences

- Use `Decimal` (never `float`) for all monetary values.
- All new structured data crossing a layer boundary must be a Pydantic model.
- Module-level docstring + `from __future__ import annotations` + full type hints on every new file.
- Structured logging via `logging.getLogger(__name__)` — no bare `print()`.
