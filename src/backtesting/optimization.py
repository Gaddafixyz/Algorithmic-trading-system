"""
Strategy parameter optimization using Optuna.

Performs walk-forward optimization to avoid overfitting:
1. Split data into in-sample (IS) and out-of-sample (OOS) windows
2. Optimize on IS data using Optuna TPE sampler
3. Validate best params on OOS data
4. Report both IS and OOS performance
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from src.backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from src.core.enums import Timeframe
from src.core.models import OHLCV

logger = logging.getLogger(__name__)

ObjectiveFn = Callable[[dict[str, Any]], float]


@dataclass
class OptimizationResult:
    best_params: dict[str, Any]
    best_is_score: float
    oos_result: BacktestResult | None
    all_trials: list[dict[str, Any]]
    n_trials: int


def optimize(
    strategy_factory: Callable[[dict[str, Any]], Any],
    candles: list[OHLCV],
    symbol: str,
    timeframe: Timeframe,
    param_space: dict[str, Any],
    n_trials: int = 100,
    is_fraction: float = 0.7,
    objective: str = "sharpe_ratio",
) -> OptimizationResult:
    """
    Optimize strategy parameters using Optuna.

    Args:
        strategy_factory: Callable(params) → strategy instance
        candles: Full candle history
        symbol: Symbol to backtest
        timeframe: Candle timeframe
        param_space: Dict describing parameter search space
        n_trials: Number of Optuna trials
        is_fraction: Fraction of data for in-sample
        objective: Metric to maximize ('sharpe_ratio', 'profit_factor', etc.)

    Returns:
        OptimizationResult with best params and OOS validation
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError as e:
        raise ImportError("optuna is required for optimization") from e

    split_idx = int(len(candles) * is_fraction)
    is_candles = candles[:split_idx]
    oos_candles = candles[split_idx:]

    all_trials: list[dict[str, Any]] = []

    def optuna_objective(trial: Any) -> float:
        params: dict[str, Any] = {}
        for name, spec in param_space.items():
            if spec["type"] == "int":
                params[name] = trial.suggest_int(name, spec["low"], spec["high"])
            elif spec["type"] == "float":
                params[name] = trial.suggest_float(name, spec["low"], spec["high"], log=spec.get("log", False))
            elif spec["type"] == "categorical":
                params[name] = trial.suggest_categorical(name, spec["choices"])

        try:
            strategy = strategy_factory(params)
            config = BacktestConfig()
            engine = BacktestEngine(strategy, config)
            result = asyncio.run(engine.run(is_candles, symbol, timeframe))

            if result.performance is None or result.performance.total_trades < 10:
                return -999.0

            score = getattr(result.performance, objective, 0.0)
            if isinstance(score, Decimal):
                score = float(score)

            all_trials.append({"params": params, "score": score})
            return score
        except Exception as exc:
            logger.debug("Trial failed: %s", exc)
            return -999.0

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler())
    study.optimize(optuna_objective, n_trials=n_trials, show_progress_bar=False)

    best_params = study.best_params
    best_score = study.best_value

    # Validate on OOS data
    oos_result: BacktestResult | None = None
    if len(oos_candles) >= 50:
        try:
            best_strategy = strategy_factory(best_params)
            engine = BacktestEngine(best_strategy, BacktestConfig())
            # n_trials=n_trials: best_params is the winner of an n_trials
            # Optuna search, so its OOS Sharpe should be judged against
            # what picking the best of n_trials random searches would
            # produce by chance (deflated_sharpe_ratio), not treated as
            # if it were a single pre-registered backtest.
            oos_result = asyncio.run(engine.run(oos_candles, symbol, timeframe, n_trials=n_trials))
        except Exception as exc:
            logger.warning("OOS validation failed: %s", exc)

    logger.info(
        "Optimization complete: %d trials, best %s=%.4f params=%s",
        n_trials, objective, best_score, best_params,
    )

    return OptimizationResult(
        best_params=best_params,
        best_is_score=best_score,
        oos_result=oos_result,
        all_trials=all_trials,
        n_trials=n_trials,
    )
