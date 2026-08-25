.PHONY: help install install-dev lint format typecheck test test-unit test-integration \
        test-coverage clean docker-build docker-up docker-down migrate backtest \
        health security-check

PYTHON := python3
PIP := pip
SRC := src
TESTS := tests

help:
	@echo "Trading System — Available commands:"
	@echo ""
	@echo "  install         Install production dependencies"
	@echo "  install-dev     Install development dependencies"
	@echo "  lint            Run ruff linter"
	@echo "  format          Format code with black + isort"
	@echo "  typecheck       Run mypy type checker"
	@echo "  test            Run all tests"
	@echo "  test-unit       Run unit tests only"
	@echo "  test-integration Run integration tests"
	@echo "  test-coverage   Run tests with coverage report"
	@echo "  clean           Remove build artifacts"
	@echo "  docker-build    Build Docker image"
	@echo "  docker-up       Start all services"
	@echo "  docker-down     Stop all services"
	@echo "  migrate         Run database migrations"
	@echo "  backtest        Run backtesting script"
	@echo "  health          Check system health"
	@echo "  security-check  Run bandit + safety"

install:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -r requirements-dev.txt
	pre-commit install

lint:
	ruff check $(SRC) $(TESTS) scripts

format:
	black $(SRC) $(TESTS) scripts
	isort $(SRC) $(TESTS) scripts

typecheck:
	mypy $(SRC)

test:
	pytest $(TESTS) -x

test-unit:
	pytest $(TESTS)/unit -x -m unit

test-integration:
	pytest $(TESTS)/integration -x -m integration

test-strategy:
	pytest $(TESTS)/strategy -x -m "not exchange"

test-risk:
	pytest $(TESTS)/risk -x

test-coverage:
	pytest $(TESTS) --cov=$(SRC) --cov-report=html --cov-report=term-missing

test-fast:
	pytest $(TESTS) -x -n auto --ignore=$(TESTS)/integration --ignore=$(TESTS)/end_to_end

security-check:
	bandit -r $(SRC) -ll
	safety check -r requirements.txt

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	rm -f .coverage

docker-build:
	docker build -t trading-system:latest .

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

migrate:
	$(PYTHON) scripts/migrate_db.py

backtest:
	$(PYTHON) scripts/backtest.py

optimize:
	$(PYTHON) scripts/optimize.py

health:
	$(PYTHON) scripts/healthcheck.py

start-paper:
	TRADING_ENV=paper $(PYTHON) -m src.main

start-testnet:
	TRADING_ENV=testnet $(PYTHON) -m src.main

start-live:
	@echo "WARNING: Starting LIVE trading. Press Ctrl+C within 5 seconds to abort."
	@sleep 5
	TRADING_ENV=production $(PYTHON) -m src.main
