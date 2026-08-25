# AGENTS.md

Operating instructions for autonomous coding agents in this repository.
This file follows the same intent as `CLAUDE.md` but is provider-agnostic.

## Before Making Changes

1. Read `ARCHITECTURE.md` to understand the layering — respect dependency
   direction (outer layers depend on inner, never reverse).
2. Read `DESIGN.md` "Known Limitations" before assuming something is a bug;
   it may be documented, intentional scope.
3. Check `ROADMAP.md` before implementing a "missing" feature — it may
   already be tracked with context on why it isn't done yet.

## Required Checks Before Declaring Work Complete

```bash
make lint        # ruff
make typecheck   # mypy
make test        # full pytest suite
```

All three must pass. If `mypy` or `ruff` flag pre-existing issues unrelated
to your change, do not silently suppress them — note them in your summary.

## Financial Code Review Checklist

When touching anything under `src/domain/`, `src/risk/`, `src/execution/`,
or `src/backtesting/`, verify:

- [ ] All monetary values use `Decimal`, not `float`.
- [ ] No new code path can submit an order without passing
      `RiskManager.validate_signal()`.
- [ ] Backtest fills still occur at next-bar-open, not signal-bar-close.
- [ ] New Pydantic model fields have appropriate validators (positive
      price/quantity, valid enum values, etc.) consistent with existing
      patterns in `src/core/models.py` and `src/domain/`.

## Honesty Requirements

- Never claim a backtest or test was run if it wasn't actually executed in
  this session.
- If a network-dependent operation (e.g., fetching live Binance data)
  cannot be verified in the current environment, say so explicitly rather
  than asserting it works.
- Distinguish between "implemented" and "validated against historical
  data" — the SMC scoring weights, for example, are implemented but their
  parameters have not been empirically tuned (see `DESIGN.md`).

## Style

Match existing conventions exactly: module-level docstring explaining
purpose, `from __future__ import annotations`, full type hints, structured
logging via `logging.getLogger(__name__)`, Pydantic models for all
structured data crossing a layer boundary.
