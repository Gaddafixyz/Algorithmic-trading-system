# Contributing

## Development Setup

```bash
git clone <repo>
cd trading-system
python3.13 -m venv .venv
source .venv/bin/activate
make install-dev
```

This installs pre-commit hooks (`ruff`, `mypy`, `bandit`) that run on every
commit.

## Code Standards

- **Python 3.13+**, full type hints on all public functions.
- **Decimal, never float**, for any monetary value (prices, quantities,
  P&L, fees).
- **Async-first**: I/O-bound code (exchange calls, database, file I/O) must
  be `async def` using `asyncio`-compatible libraries.
- **No bare `except:`** — catch specific exception types from
  `src/core/exceptions.py` or well-known third-party exceptions.
- **No mutable default arguments** — use `field(default_factory=...)` in
  dataclasses/Pydantic models.
- **Structured logging** via the standard `logging` module
  (`logger = logging.getLogger(__name__)`); never use bare `print()` in
  `src/`.
- **Docstrings** on all public classes and non-trivial functions explaining
  *why*, not just *what*.

## Testing Requirements

- All new business logic requires unit tests.
- All new integration points (event bus subscribers, multi-component
  flows) require integration tests.
- Tests must not make real network calls — mock exchange clients with
  `unittest.mock.AsyncMock`.
- Run `make test` before submitting; `make test-coverage` to check
  coverage (target: 80%+ on `src/`).

## Pull Request Process

1. Branch from `main`: `git checkout -b feature/your-feature`.
2. Make changes with accompanying tests.
3. Run `make lint typecheck test` locally — all must pass.
4. Update `CHANGELOG.md` under an "Unreleased" section.
5. Submit PR with a clear description of the change and rationale.

## Adding a New SMC Detection Module

New Smart Money Concepts modules belong in `src/smart_money/`. Follow the
existing pattern:
1. Define a frozen dataclass (or mutable, if state like mitigation/fill
   tracking is needed) representing the detected structure.
2. Write a pure function `detect_x(candles: list[OHLCV], ...) -> list[X]`
   that takes candle data and returns detected instances.
3. Add unit tests in `tests/unit/test_market_structure.py` or a new test
   file following the same naming convention.
4. Wire it into `src/strategy/signal_engine.py::SMCSignalEngine.analyze()`
   and `src/smart_money/confluence.py::score_confluence()` if it should
   contribute to signal scoring.

## Security

Do not commit API keys, secrets, or `.env` files. See
[SECURITY.md](SECURITY.md) for vulnerability reporting.
