"""
End-to-end training script for the ML signal filter.

Runs the SMC strategy over historical candles via the backtest engine,
captures the (Signal, SMCAnalysis) pair behind every signal generated,
labels each one by its eventual trade outcome (profitable / not), then
trains, evaluates, and saves a SignalFilter model.

This is the "closes the loop" script referenced in ROADMAP.md and
DESIGN.md: `src/machine_learning/` (features.py, signal_filter.py,
trainer.py) was fully built, and `SMCStrategy.on_candle()` already
supports an optional `signal_filter` gate, but nothing ever produced
`models/signal_filter.pkl` — this script is that missing piece.

Label source: a signal only becomes a labelled training example if it
was actually acted on and closed by the backtest broker. Signals that
were never filled (e.g. max positions reached, insufficient equity) are
not scored — there is no realized outcome to label them with. This
mirrors how the live system would encounter setups: only positions that
were actually opened and closed produce ground truth.

No look-ahead: features are extracted from the Signal/SMCAnalysis
snapshot exactly as it existed when the signal was generated (via
SMCSignalEngine.last_analysis, captured synchronously inside
on_candle()) — nothing from the trade's eventual outcome leaks into the
feature vector, only into the label.

Usage:
    python scripts/train_signal_filter.py --symbol BTCUSDT --timeframe 1h --days 365
    python scripts/train_signal_filter.py --symbol ETHUSDT --timeframe 4h --days 730 \
        --min-score 55 --output models/signal_filter.pkl
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtesting.commission import PercentageCommission
from src.backtesting.engine import BacktestConfig, BacktestEngine
from src.backtesting.slippage import FixedSlippage
from src.core.config import get_settings
from src.core.enums import Timeframe
from src.core.models import OHLCV
from src.domain.signal import Signal
from src.exchange.binance.rest import BinanceFuturesRESTClient
from src.machine_learning.features import extract_features
from src.machine_learning.trainer import DEFAULT_MODEL_PATH, SignalFilterTrainer
from src.main import SMCStrategy
from src.monitoring.telemetry import configure_logging
from src.smart_money.confluence import SMCAnalysis

logger = logging.getLogger(__name__)


class _CapturingSMCStrategy(SMCStrategy):
    """
    Thin wrapper around SMCStrategy that records the (Signal, SMCAnalysis)
    pair behind every signal it emits, keyed by signal_id.

    This only *observes* engine state the strategy already computed —
    engine.last_analysis is set synchronously inside analyze() before
    on_candle() returns — so it changes nothing about what signals are
    generated or when, preserving the backtest's no-look-ahead guarantee.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.captured: dict[str, tuple[Signal, SMCAnalysis | None]] = {}

    async def on_candle(
        self, symbol: str, timeframe: Timeframe, candles: list[OHLCV]
    ) -> Signal | None:
        signal = await super().on_candle(symbol, timeframe, candles)
        if signal is not None:
            key = f"{symbol}_{timeframe.value}"
            engine = self._engines.get(key)
            analysis = engine.last_analysis if engine is not None else None
            self.captured[signal.signal_id] = (signal, analysis)
        return signal


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train the ML signal filter")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--htf", default="4h", help="Higher timeframe for bias")
    p.add_argument("--ltf", default="15m", help="Lower timeframe for MTFContext LTF tier")
    p.add_argument("--days", type=int, default=365)
    p.add_argument(
        "--min-score",
        type=float,
        default=55.0,
        help="Min confluence score for the strategy to emit a "
        "signal in the first place — lower than the live "
        "default so the model sees enough borderline "
        "examples to actually learn a boundary",
    )
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--min-probability", type=float, default=0.55)
    p.add_argument(
        "--test-fraction",
        type=float,
        default=0.2,
        help="Fraction of labelled examples (chronologically " "last) held out for evaluation",
    )
    p.add_argument("--cv-splits", type=int, default=5)
    p.add_argument(
        "--tune",
        action="store_true",
        help="Run Optuna hyperparameter search (time-series CV) "
        "before the final fit, instead of using the "
        "trainer's fixed default hyperparameters. Requires "
        "`pip install optuna`.",
    )
    p.add_argument(
        "--tune-trials", type=int, default=50, help="Number of Optuna trials when --tune is set"
    )
    p.add_argument(
        "--tune-metric",
        choices=["auc", "f1"],
        default="auc",
        help="Metric to maximize during tuning",
    )
    p.add_argument("--output", type=Path, default=DEFAULT_MODEL_PATH)
    p.add_argument(
        "--no-funding",
        dest="funding",
        action="store_false",
        help="Skip fetching/applying historical funding-rate accrual",
    )
    p.set_defaults(funding=True)
    return p.parse_args()


async def _fetch_candles(
    client: BinanceFuturesRESTClient, symbol: str, tf: Timeframe, days: int
) -> list[OHLCV]:
    start = datetime.now(tz=UTC) - timedelta(days=days)
    return await client.get_candles(symbol, tf, limit=1500, start_time=start)


async def train(args: argparse.Namespace) -> None:
    configure_logging("INFO")
    settings = get_settings()

    if settings.binance is None:
        logger.error("Binance credentials required in .env")
        sys.exit(1)

    try:
        tf = Timeframe(args.timeframe)
        htf_tf = Timeframe(args.htf)
    except ValueError as exc:
        logger.error("Invalid timeframe: %s", exc)
        sys.exit(1)

    # ltf is optional (unlike htf/timeframe above) — an invalid/absent
    # value simply disables the LTF tier for training data generation,
    # since MTFContext.alignment_score() already degrades gracefully
    # without it (see multi_timeframe.py).
    try:
        ltf_tf = Timeframe(args.ltf) if args.ltf else None
    except ValueError:
        logger.warning("Invalid --ltf %r — training will run without the LTF tier.", args.ltf)
        ltf_tf = None

    client = BinanceFuturesRESTClient(
        api_key=settings.binance.api_key.get_secret_value(),
        api_secret=settings.binance.api_secret.get_secret_value(),
        base_url=settings.binance.rest_base_url,
    )

    logger.info("Fetching %d days of %s %s history...", args.days, args.symbol, args.timeframe)
    async with client:
        candles = await _fetch_candles(client, args.symbol, tf, args.days)
        htf_candles = await _fetch_candles(client, args.symbol, htf_tf, args.days)
        ltf_candles = (
            await _fetch_candles(client, args.symbol, ltf_tf, args.days) if ltf_tf else None
        )

        # Fetch funding-rate settlements so labelled trades reflect real
        # funding cost/credit, not just price action (see backtest.py /
        # optimize.py — same gap, same fix).
        funding_rates = None
        if args.funding:
            try:
                start_ms = int(
                    (datetime.now(tz=UTC) - timedelta(days=args.days)).timestamp() * 1000
                )
                end_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
                funding_rates = await client.fetch_funding_rate_history_parsed(
                    args.symbol, start_time=start_ms, end_time=end_ms
                )
            except Exception:
                logger.warning(
                    "Failed to fetch funding-rate history; training will "
                    "run without funding accrual",
                    exc_info=True,
                )
                funding_rates = None

    if len(candles) < 200:
        logger.error(
            "Insufficient data: only %d candles fetched (need enough history "
            "for a meaningful train/test split, not just the engine's 50-bar "
            "warmup)",
            len(candles),
        )
        sys.exit(1)

    logger.info(
        "Fetched %d candles (%s -> %s)",
        len(candles),
        candles[0].timestamp.date(),
        candles[-1].timestamp.date(),
    )

    strategy = _CapturingSMCStrategy(
        symbols=[args.symbol],
        timeframes=[tf],
        htf_timeframe=htf_tf,
        ltf_timeframe=ltf_tf,
        min_score=args.min_score,
        # No signal_filter here — training data must reflect the
        # *unfiltered* strategy's behaviour, not a previous model's
        # opinion of which signals were worth taking.
    )

    config = BacktestConfig(
        initial_equity=Decimal(str(args.equity)),
        commission_model=PercentageCommission(),
        slippage_model=FixedSlippage(),
        risk_pct_per_trade=Decimal(str(args.risk_pct)),
        funding_rates=funding_rates,
    )
    engine = BacktestEngine(strategy, config)

    logger.info("Running backtest to generate labelled training data...")
    result = await engine.run(
        candles,
        args.symbol,
        tf,
        htf_candles=htf_candles,
        htf_timeframe=htf_tf,
        ltf_candles=ltf_candles,
        ltf_timeframe=ltf_tf,
    )
    logger.info(
        "Backtest complete: %d signals generated, %d trades closed",
        result.signals_generated,
        len(result.trades),
    )

    # ── Join closed trades back to the (Signal, SMCAnalysis) that produced
    # them via signal_id, and build the feature matrix / label vector.
    # Trades are already in closing order (chronological), which is what
    # the time-series train/test split below relies on.
    X_rows: list[np.ndarray] = []
    y_rows: list[int] = []
    skipped_unmatched = 0
    for trade in result.trades:
        pair = strategy.captured.get(trade.signal_id or "")
        if pair is None:
            skipped_unmatched += 1
            continue
        signal, analysis = pair
        X_rows.append(extract_features(signal, analysis))
        y_rows.append(1 if trade.net_pnl > 0 else 0)

    if skipped_unmatched:
        logger.warning(
            "%d closed trades had no matching captured signal (signal_id "
            "mismatch) and were excluded from training data",
            skipped_unmatched,
        )

    n = len(X_rows)
    if n < 30:
        logger.error(
            "Only %d labelled examples produced — too few to train a "
            "reliable classifier. Widen --days, lower --min-score, or "
            "check that the strategy is generating signals at all.",
            n,
        )
        sys.exit(1)

    X = np.vstack(X_rows)
    y = np.array(y_rows, dtype=int)
    logger.info(
        "Built training set: %d examples (%d/%d = %.1f%% profitable)",
        n,
        int(y.sum()),
        n,
        100.0 * y.mean(),
    )

    # ── Chronological train/test split — never shuffle. A random split
    # would let the model implicitly learn from setups that occurred
    # *after* ones in the test set, which is look-ahead bias.
    split = max(1, int(n * (1 - args.test_fraction)))
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    if len(X_test) == 0 or len(set(y_test.tolist())) < 2:
        logger.warning(
            "Test split has %d examples with %d class(es) present — "
            "evaluation metrics below may be unreliable. Consider more "
            "--days of history.",
            len(X_test),
            len(set(y_test.tolist())),
        )

    trainer = SignalFilterTrainer(min_probability=args.min_probability)
    cv_splits = min(args.cv_splits, max(2, len(X_train) // 20))

    if args.tune:
        if cv_splits < 2:
            logger.warning(
                "Not enough training examples for time-series CV — skipping "
                "--tune and using default hyperparameters"
            )
        else:
            logger.info(
                "Tuning hyperparameters (%d Optuna trials, %d-fold "
                "time-series CV, maximizing %s)...",
                args.tune_trials,
                cv_splits,
                args.tune_metric,
            )
            tune_result = trainer.tune(
                X_train,
                y_train,
                n_trials=args.tune_trials,
                n_splits=cv_splits,
                metric=args.tune_metric,
            )
            logger.info(
                "Best trial: %s=%.4f params=%s",
                args.tune_metric,
                tune_result["best_score"],
                tune_result["best_params"],
            )

    logger.info("Cross-validating (time-series, %d splits) on training set...", cv_splits)
    if cv_splits >= 2:
        trainer.cross_validate(X_train, y_train, n_splits=cv_splits)
    else:
        logger.warning("Not enough training examples for cross-validation — skipping")

    logger.info("Training final model on %d examples...", len(X_train))
    signal_filter = trainer.train(X_train, y_train)

    if len(X_test) > 0 and len(set(y_test.tolist())) >= 2:
        logger.info("Evaluating on %d held-out examples...", len(X_test))
        trainer.evaluate(signal_filter, X_test, y_test)
    else:
        logger.info("Skipping held-out evaluation (insufficient/imbalanced test split)")

    trainer.save(signal_filter, args.output)
    logger.info(
        "Saved signal filter to %s — set ml_filter.enabled=true and "
        "ml_filter.model_path=%s in configs/strategy.yaml to use it live",
        args.output,
        args.output,
    )
    logger.info(
        "Reminder: this model was trained on %s %s only. Retrain per "
        "symbol/timeframe before relying on it elsewhere, and re-validate "
        "whenever confluence.py's scoring weights change (see "
        "ROADMAP.md's scripts/optimize.py item) — the feature schema "
        "includes confluence_score directly, so a re-weighted scorer "
        "shifts the input distribution the model was fit to.",
        args.symbol,
        args.timeframe,
    )


def main() -> None:
    args = parse_args()
    asyncio.run(train(args))


if __name__ == "__main__":
    main()
