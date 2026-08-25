# Merge Notes — trading-final-v1.3.29-merged

**Date:** 2026-08-25

## Source
- **Primary base:** `trading-final-v1_3_29.zip` (latest code + CHANGELOG up to [1.3.29])
- **Restored missing files** from earlier complete snapshot (`trading-final-v1_3_25.zip`):
  - `.env.example`
  - `.gitignore`
  - `.pre-commit-config.yaml`
  - `tests/bootstrap/test_check_portfolio_rebalance.py`
  - `tests/bootstrap/test_evict_stale_market_data.py`
  - `tests/bootstrap/test_record_metrics_snapshot.py`
  - `tests/core/test_notification_config.py`
  - `tests/core/test_timeframe_higher_timeframe.py`
  - `tests/execution/test_reconcile_pnl.py`

## What was excluded
- Nested version folders (`v1_3_21/`, etc.) found in some intermediate ZIPs
- Full `.git` history (present in larger intermediate builds) — source-only clean tree
- Duplicate packaging variants

## Result
Clean, flat project tree at version **1.3.29** with the above missing operational/test files restored.

---

## Installation Instructions

> ⚠️ **Risk warning:** Cryptocurrency futures trading carries substantial risk of loss.
> Use testnet / paper trading first. See `LICENSE` and `SECURITY.md`.

### Prerequisites
- Python 3.11+ recommended
- Docker & Docker Compose (for Postgres, Redis, monitoring stack)
- Git (optional)

### Quick Start (recommended)

```bash
# 1. Unzip and enter the project
unzip trading-final-v1.3.29-merged.zip -d trading-system
cd trading-system

# 2. Configure environment
cp .env.example .env
# Edit .env — set Binance/Bybit API keys (use TESTNET keys first!)
# Key settings: TRADING_ENV=paper, EXCHANGE_PROVIDER=binance, BINANCE_TESTNET=true

# 3. Install dependencies
make install-dev          # installs requirements-dev.txt + pre-commit hooks
# or production only:
# make install

# 4. Start infrastructure
docker-compose up -d postgres redis

# 5. Run database migrations
make migrate

# 6. Verify
make test                 # run the test suite
make health               # optional health check

# 7. Start paper trading (safe mode)
make start-paper
```

### Useful Make targets

| Command              | Description                              |
|----------------------|------------------------------------------|
| `make install`       | Production dependencies only             |
| `make install-dev`   | Dev deps + pre-commit                    |
| `make test`          | Run all tests                            |
| `make test-unit`     | Unit tests only                          |
| `make lint`          | Ruff linter                              |
| `make format`        | Black + isort                            |
| `make typecheck`     | mypy                                     |
| `make migrate`       | Alembic database migrations              |
| `make backtest`      | Run backtesting script                   |
| `make docker-up`     | Start full Docker stack                  |
| `make docker-down`   | Stop Docker services                     |
| `make health`        | System health check                      |
| `make security-check`| Bandit + safety                          |

### Alternative: bare-metal install
Run the included helper (Ubuntu/Debian tested):

```bash
./install.sh
```

This installs system packages, creates a service user, sets up PostgreSQL + Redis, and deploys the app under `/opt/trading-system` (configurable via env vars).

### Configuration overview
- **Secrets / infra:** `.env` (copied from `.env.example`)
- **Trading parameters:** YAML files in `configs/`
  - `paper.yaml` — paper trading
  - `testnet.yaml` — exchange testnet
  - `production.yaml` — live
  - `strategy.yaml`, `risk.yaml`, `symbols.yaml`, etc.

### Next steps after install
- Paper trade first: `make start-paper`
- Backtest: `python scripts/backtest.py --symbol BTCUSDT --timeframe 1h --days 180 --save`
- Read `ARCHITECTURE.md`, `DESIGN.md`, and `ROADMAP.md` for deeper details
