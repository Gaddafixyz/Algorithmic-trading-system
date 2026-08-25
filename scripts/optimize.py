"""
Parameter optimization script using walk-forward Optuna optimization.

Usage:
    # Original single-symbol usage (unchanged) — tunes min_score only:
    python scripts/optimize.py --symbol BTCUSDT --timeframe 1h --trials 100

    # Multi-symbol / multi-timeframe sweep, also tuning the SMC confluence
    # scoring weights (ROADMAP.md: "Empirically validate and tune the
    # default SMC confluence scoring weights ... against multiple
    # symbols/timeframes"):
    python scripts/optimize.py \
        --symbols BTCUSDT,ETHUSDT,SOLUSDT \
        --timeframes 15m,1h,4h \
        --tune-weights \
        --trials 150

Each (symbol, timeframe) combination is optimized independently (its own
walk-forward IS/OOS split, its own Optuna study). When --tune-weights is
set, a final validation pass compares each combination's individually
tuned confluence weights against a single "global" weight set (the mean
across all combinations) on that combination's own OOS data — this is the
actual empirical check for whether one weight set generalizes across
symbols/timeframes or whether per-symbol tuning is overfitting to
symbol-specific quirks. See _run_global_weights_validation().
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtesting.engine import BacktestConfig, BacktestEngine
from src.backtesting.optimization import optimize
from src.core.config import get_settings
from src.core.enums import Timeframe
from src.core.models import OHLCV
from src.exchange.binance.rest import BinanceFuturesRESTClient
from src.main import SMCStrategy
from src.monitoring.telemetry import configure_logging
from src.smart_money.confluence import ConfluenceWeights
from src.utils.files import write_json

logger = logging.getLogger(__name__)

# Default search bounds for each confluence weight, expressed directly in
# the same point-budget units as ConfluenceWeights' field defaults
# (20/20/15/15/10/10/5/5). Bounds are roughly 0.2x-2x of each factor's
# original budget — wide enough for Optuna to meaningfully shift emphasis
# between factors, narrow enough that a run isn't mostly wasted exploring
# implausible combinations (e.g. volume confirmation outweighing market
# structure). ConfluenceWeights.normalized() (applied inside
# score_confluence()) rescales whatever's sampled here to sum to 100, so
# only the *relative* proportions across these 8 matter, not their raw
# scale.
WEIGHT_PARAM_SPACE: dict[str, dict] = {
    "w_market_structure": {"type": "float", "low": 4.0, "high": 40.0},
    "w_order_block": {"type": "float", "low": 4.0, "high": 40.0},
    "w_fair_value_gap": {"type": "float", "low": 3.0, "high": 30.0},
    "w_liquidity_sweep": {"type": "float", "low": 3.0, "high": 30.0},
    "w_premium_discount": {"type": "float", "low": 2.0, "high": 20.0},
    "w_session": {"type": "float", "low": 2.0, "high": 20.0},
    "w_volume": {"type": "float", "low": 1.0, "high": 10.0},
    "w_htf_alignment": {"type": "float", "low": 1.0, "high": 10.0},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Strategy Parameter Optimizer")
    p.add_argument(
        "--symbol", default="BTCUSDT", help="Single symbol (ignored if --symbols is given)"
    )
    p.add_argument(
        "--symbols", default=None, help="Comma-separated symbols, e.g. BTCUSDT,ETHUSDT,SOLUSDT"
    )
    p.add_argument(
        "--timeframe", default="1h", help="Single timeframe (ignored if --timeframes is given)"
    )
    p.add_argument("--timeframes", default=None, help="Comma-separated timeframes, e.g. 15m,1h,4h")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--trials", type=int, default=100)
    p.add_argument(
        "--objective",
        default="sharpe_ratio",
        choices=["sharpe_ratio", "profit_factor", "total_net_pnl"],
    )
    p.add_argument(
        "--tune-weights",
        action="store_true",
        help="Also tune the 8 SMC confluence scoring weights, " "not just min_score.",
    )
    p.add_argument("--output", type=Path, default=Path("./optimization_results"))
    return p.parse_args()


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return [args.symbol.upper()]


def _resolve_timeframes(args: argparse.Namespace) -> list[Timeframe]:
    raw = [t.strip() for t in args.timeframes.split(",")] if args.timeframes else [args.timeframe]
    resolved: list[Timeframe] = []
    for tf_str in raw:
        try:
            resolved.append(Timeframe(tf_str))
        except ValueError:
            logger.error("Skipping invalid timeframe: %s", tf_str)
    return resolved


def make_strategy_factory(symbol: str, timeframe: Timeframe, tune_weights: bool):
    """
    Build a strategy_factory closure bound to one (symbol, timeframe)
    combination.

    Previously this was a single module-level function hardcoded to
    `symbols=["BTCUSDT"], timeframes=[Timeframe.H1]` regardless of the
    --symbol/--timeframe CLI args — SMCStrategy.on_candle() looks its
    signal engine up by an exact f"{symbol}_{timeframe.value}" key, so
    running `--symbol ETHUSDT` silently produced zero signals for every
    trial (the strategy only had a BTCUSDT_1h engine registered) and the
    optimizer converged on whatever param happened to be first, having
    never actually scored a single real trade. Binding symbol/timeframe
    per-combination here fixes that.
    """

    def strategy_factory(params: dict):
        weights = None
        if tune_weights:
            weights = ConfluenceWeights(
                market_structure=params["w_market_structure"],
                order_block=params["w_order_block"],
                fair_value_gap=params["w_fair_value_gap"],
                liquidity_sweep=params["w_liquidity_sweep"],
                premium_discount=params["w_premium_discount"],
                session=params["w_session"],
                volume=params["w_volume"],
                htf_alignment=params["w_htf_alignment"],
            )
        return SMCStrategy(
            symbols=[symbol],
            timeframes=[timeframe],
            min_score=params.get("min_score", 60.0),
            confluence_weights=weights,
        )

    return strategy_factory


async def fetch_candles(settings: object, symbol: str, tf: Timeframe, days: int) -> list[OHLCV]:
    from src.core.config import Settings

    assert isinstance(settings, Settings)
    assert settings.binance is not None
    client = BinanceFuturesRESTClient(
        api_key=settings.binance.api_key.get_secret_value(),
        api_secret=settings.binance.api_secret.get_secret_value(),
        base_url=settings.binance.rest_base_url,
    )
    async with client:
        start = datetime.now(tz=UTC) - timedelta(days=days)
        return await client.get_candles(symbol, tf, limit=1500, start_time=start)


async def fetch_funding_rates(settings: object, symbol: str, days: int) -> list:
    """Fetch historical funding-rate settlements for the same window as
    ``fetch_candles``, for use in the OOS/global-weights validation pass
    (the honest final check, not the per-trial Optuna search — see
    ``_backtest_with_weights``)."""
    from src.core.config import Settings

    assert isinstance(settings, Settings)
    assert settings.binance is not None
    client = BinanceFuturesRESTClient(
        api_key=settings.binance.api_key.get_secret_value(),
        api_secret=settings.binance.api_secret.get_secret_value(),
        base_url=settings.binance.rest_base_url,
    )
    async with client:
        start = datetime.now(tz=UTC) - timedelta(days=days)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        return await client.fetch_funding_rate_history_parsed(
            symbol, start_time=start_ms, end_time=end_ms
        )


async def _backtest_with_weights(
    symbol: str,
    timeframe: Timeframe,
    candles: list[OHLCV],
    min_score: float,
    weights: ConfluenceWeights | None,
    funding_rates: list | None = None,
):
    """Run one backtest with a fixed (non-tuned) weight set — used by the
    global-weights validation pass, not by the Optuna search itself."""
    strategy = SMCStrategy(
        symbols=[symbol],
        timeframes=[timeframe],
        min_score=min_score,
        confluence_weights=weights,
    )
    engine = BacktestEngine(strategy, BacktestConfig(funding_rates=funding_rates))
    return await engine.run(candles, symbol, timeframe)


def _global_mean_weights(per_combo_best: list[dict]) -> ConfluenceWeights:
    """Mean of each tuned weight across every successfully-optimized
    (symbol, timeframe) combination — the "one weight set for everything"
    candidate that _run_global_weights_validation() checks for
    generalization."""
    return ConfluenceWeights(
        market_structure=statistics.mean(p["w_market_structure"] for p in per_combo_best),
        order_block=statistics.mean(p["w_order_block"] for p in per_combo_best),
        fair_value_gap=statistics.mean(p["w_fair_value_gap"] for p in per_combo_best),
        liquidity_sweep=statistics.mean(p["w_liquidity_sweep"] for p in per_combo_best),
        premium_discount=statistics.mean(p["w_premium_discount"] for p in per_combo_best),
        session=statistics.mean(p["w_session"] for p in per_combo_best),
        volume=statistics.mean(p["w_volume"] for p in per_combo_best),
        htf_alignment=statistics.mean(p["w_htf_alignment"] for p in per_combo_best),
    )


async def _run_global_weights_validation(
    combos: list[dict],
    objective: str,
) -> dict:
    """
    The actual "empirically validate ... against multiple symbols/
    timeframes" step from ROADMAP.md: is a single global weight set
    (the mean of everyone's individually-tuned best) a reasonable
    default, or does each symbol/timeframe need its own?

    For each combination, re-runs its OOS candles once more with the
    global mean weights (instead of that combination's own individually
    tuned weights) and compares the objective metric. Small, consistent
    gaps -> the global default in ConfluenceWeights is fine as shipped.
    Large gaps on particular symbols/timeframes -> that combination's
    individually tuned weights should be used instead (e.g. loaded via
    strategy.yaml's per-deployment scoring_weights override), not the
    shared default.
    """
    global_weights = _global_mean_weights([c["best_params"] for c in combos])
    logger.warning("Global mean weights across all combos: %s", global_weights.as_dict())

    comparisons = []
    for combo in combos:
        oos_candles = combo["oos_candles"]
        if not oos_candles or len(oos_candles) < 50:
            continue
        min_score = combo["best_params"].get("min_score", 60.0)
        global_result = await _backtest_with_weights(
            combo["symbol"],
            combo["timeframe"],
            oos_candles,
            min_score,
            global_weights,
            funding_rates=combo.get("funding_rates"),
        )
        global_score = 0.0
        if global_result.performance is not None:
            global_score = float(getattr(global_result.performance, objective, 0.0) or 0.0)

        tuned_score = combo["oos_score"]
        comparisons.append(
            {
                "symbol": combo["symbol"],
                "timeframe": combo["timeframe"],
                "individually_tuned_oos": tuned_score,
                "global_weights_oos": global_score,
                "gap": tuned_score - global_score,
            }
        )
        logger.warning(
            "%s %s — individually tuned OOS %s=%.4f vs global-weights OOS %s=%.4f (gap=%.4f)",
            combo["symbol"],
            combo["timeframe"],
            objective,
            tuned_score,
            objective,
            global_score,
            tuned_score - global_score,
        )

    return {"global_weights": global_weights.as_dict(), "comparisons": comparisons}


async def async_main(args: argparse.Namespace) -> None:
    configure_logging("WARNING")
    settings = get_settings()

    symbols = _resolve_symbols(args)
    timeframes = _resolve_timeframes(args)
    if not timeframes:
        logger.error("No valid timeframes given")
        sys.exit(1)

    param_space: dict = {
        "min_score": {"type": "float", "low": 45.0, "high": 85.0},
    }
    if args.tune_weights:
        param_space.update(WEIGHT_PARAM_SPACE)

    args.output.mkdir(parents=True, exist_ok=True)
    combos_for_validation: list[dict] = []

    for symbol in symbols:
        for tf in timeframes:
            logger.warning("Fetching %d days of %s %s...", args.days, symbol, tf.value)
            try:
                candles = await fetch_candles(settings, symbol, tf, args.days)
            except Exception as exc:
                logger.error("Failed to fetch candles for %s %s: %s", symbol, tf.value, exc)
                continue

            if len(candles) < 100:
                logger.error("Insufficient data for %s %s, skipping", symbol, tf.value)
                continue

            funding_rates = []
            if args.tune_weights:
                try:
                    funding_rates = await fetch_funding_rates(settings, symbol, args.days)
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch funding rates for %s: %s (validation "
                        "pass will run without funding accrual)",
                        symbol,
                        exc,
                    )

            logger.warning(
                "Optimizing %s %s with %d trials, objective=%s, tune_weights=%s",
                symbol,
                tf.value,
                args.trials,
                args.objective,
                args.tune_weights,
            )

            result = optimize(
                strategy_factory=make_strategy_factory(symbol, tf, args.tune_weights),
                candles=candles,
                symbol=symbol,
                timeframe=tf,
                param_space=param_space,
                n_trials=args.trials,
                objective=args.objective,
            )

            print(f"\n=== {symbol} {tf.value} Optimization Results ===")
            print(f"Best params: {result.best_params}")
            print(f"Best IS {args.objective}: {result.best_is_score:.4f}")

            oos_score = 0.0
            if result.oos_result and result.oos_result.performance:
                p = result.oos_result.performance
                oos_score = float(getattr(p, args.objective, 0.0) or 0.0)
                print("OOS Validation:")
                print(f"  Trades: {p.total_trades}")
                print(f"  Win rate: {p.win_rate*100:.1f}%")
                print(f"  Sharpe: {p.sharpe_ratio:.2f}")
                print(f"  Deflated Sharpe (n_trials={args.trials}): {p.deflated_sharpe_ratio:.2f}")
                print(f"  Net PnL: {p.total_net_pnl}")

            write_json(
                args.output / f"{symbol}_{tf.value}_opt.json",
                {
                    "best_params": result.best_params,
                    "best_is_score": result.best_is_score,
                    "n_trials": result.n_trials,
                    "objective": args.objective,
                    "oos_summary": result.oos_result.summarize() if result.oos_result else None,
                },
            )

            # split_idx matches optimize()'s internal default is_fraction=0.7
            split_idx = int(len(candles) * 0.7)
            if args.tune_weights and all(k in result.best_params for k in WEIGHT_PARAM_SPACE):
                combos_for_validation.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "best_params": result.best_params,
                        "oos_score": oos_score,
                        "oos_candles": candles[split_idx:],
                        "funding_rates": funding_rates,
                    }
                )

    print(f"\nPer-combination results saved to {args.output}")

    if args.tune_weights and len(combos_for_validation) >= 2:
        print("\n=== Global-vs-per-symbol weight validation ===")
        validation = await _run_global_weights_validation(combos_for_validation, args.objective)
        write_json(args.output / "global_weights_validation.json", validation)
        print(f"Global weight validation saved to {args.output / 'global_weights_validation.json'}")
    elif args.tune_weights:
        logger.warning(
            "Only one (symbol, timeframe) combination had valid trials — "
            "skipping global-vs-per-symbol validation (needs >=2 to compare)."
        )

    print(
        "\nNote: this searches whatever history --days pulls from the "
        "configured exchange account right now. ROADMAP.md's ask is a real "
        "empirical validation run (longer history, more symbols/timeframes, "
        "reviewed by a human before any weights ship as the new default in "
        "configs/strategy.yaml) — treat this script's output as candidate "
        "evidence for that review, not as an automatic config change."
    )


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
