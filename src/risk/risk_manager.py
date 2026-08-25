"""
Central Risk Manager.

Pre-trade validation gate: every signal must pass through here
before an order is submitted to the exchange.

Responsibilities:
- Position sizing (fixed fractional risk)
- Maximum position count enforcement
- Daily loss limit circuit breaker
- Drawdown limit circuit breaker
- Leverage validation
- Duplicate signal/position detection
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.core.config import RiskConfig
from src.core.enums import AlertSeverity, CircuitBreakerState, RiskLevel, SignalDirection
from src.core.events import (
    CandleClosedEvent,
    DailyLossLimitReachedEvent,
    PositionClosedEvent,
    RiskLimitBreachedEvent,
    get_event_bus,
)
from src.core.exceptions import (
    CircuitBreakerOpenError,
    DailyLossLimitError,
    MaxPositionsExceededError,
    PositionSizingError,
    RiskLimitExceededError,
)
from src.domain.leverage import LeverageConfig
from src.domain.portfolio import Portfolio
from src.monitoring.metrics import risk_validations
from src.domain.signal import Signal
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.drawdown import DrawdownTracker
from src.risk.exposure import CorrelationTracker, ExposureTracker
from src.risk.margin import initial_margin, liquidation_price_long, liquidation_price_short
from src.risk.portfolio_risk import correlation_adjusted_size
from src.risk.position_sizing import (
    calculate_position_size,
    kelly_position_size,
    volatility_adjusted_size,
)
from src.risk.stop_loss import validate_stop_risk
from src.risk.trade_tracker import RollingTradeTracker

logger = logging.getLogger(__name__)


def classify_risk_level(drawdown_pct: float, max_drawdown_pct: float) -> RiskLevel:
    """
    Bucket current drawdown into a RiskLevel, as a ratio of the
    configured max_drawdown_pct limit rather than fixed percentages —
    max_drawdown_pct is itself a per-deployment risk setting, so bucketing
    against it keeps the levels meaningful regardless of what it's set to.

    Thresholds (approved by Allan, [1.3.27]):
        LOW      : drawdown < 50% of the limit
        MEDIUM   : 50%-75% of the limit
        HIGH     : 75%-100% of the limit
        CRITICAL : >= 100% of the limit (circuit-breaker territory —
                   see RiskManager._check_drawdown_limit, which raises
                   RiskLimitExceededError at this same threshold)
    """
    if max_drawdown_pct <= 0:
        # Misconfigured (limit disabled/zero) rather than "no risk" —
        # surface it as the worst case instead of silently reporting LOW.
        return RiskLevel.CRITICAL
    ratio = drawdown_pct / max_drawdown_pct
    if ratio >= 1.0:
        return RiskLevel.CRITICAL
    if ratio >= 0.75:
        return RiskLevel.HIGH
    if ratio >= 0.5:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


class RiskManager:
    """
    Central pre-trade risk gate.

    All orders must be approved by validate_signal() before submission.
    """

    def __init__(
        self,
        config: RiskConfig,
        portfolio: Portfolio,
        order_manager: Any | None = None,
        symbol_leverage: dict[str, int] | None = None,
        correlation_tracker: CorrelationTracker | None = None,
        leverage_configs: dict[str, LeverageConfig] | None = None,
    ) -> None:
        self._config = config
        self._portfolio = portfolio
        # Optional: lets validate_signal count pending/in-flight entry
        # orders toward max_open_positions, not just already-filled
        # positions. Without this, several entries into *different* symbols
        # can all be submitted while unfilled and blow past the configured
        # position cap before any of them settle.
        self._orders = order_manager
        # Per-symbol leverage actually deployed on the exchange (from
        # configs/symbols.yaml, e.g. 5x for BTCUSDT/ETHUSDT). Falls back to
        # config.max_leverage for any symbol without an explicit entry.
        # This must match what's really set on the exchange via
        # set_leverage() at startup — using a different value here (e.g.
        # the account-wide ceiling) would check liquidation/margin math
        # against leverage the position isn't actually opened at.
        self._symbol_leverage = symbol_leverage or {}
        # Validated per-symbol leverage/margin/max-notional config (see
        # src/domain/leverage.py::LeverageConfig). Optional — only the
        # max_notional field is actually consumed here (in
        # calculate_position_size(), as an additional cap alongside the
        # existing max_position_pct_of_equity one); leverage itself keeps
        # using self._symbol_leverage above so this stays backward
        # compatible with callers that only pass that dict.
        self._leverage_configs = leverage_configs or {}
        # Optional: when supplied, calculate_position_size() scales down the
        # incremental size of a new position by how correlated it is with
        # symbols already held (see risk/exposure.py and
        # correlation_adjusted_size() in portfolio_risk.py). Without this,
        # a per-symbol correlation check can't run and the coarse
        # "treat all majors as one correlated group" cap below is the only
        # protection against concentrated, correlated exposure.
        self._correlation_tracker = correlation_tracker
        # Gross/net exposure and per-symbol concentration limits. Unlike
        # CorrelationTracker this needs no live candle feed — it only reads
        # the current Portfolio snapshot — so it's always constructed
        # rather than optionally injected. exposure.py's ExposureTracker
        # was fully implemented but never instantiated anywhere; the only
        # exposure-style guard previously enforced was
        # max_correlated_exposure_pct, which treats all open symbols as one
        # correlated group and says nothing about net long/short skew.
        self._exposure_tracker = ExposureTracker(
            max_gross_exposure_pct=float(config.max_gross_exposure_pct)
        )
        self._circuit_breaker = CircuitBreaker(
            cooldown_minutes=config.circuit_breaker_cooldown_minutes,
        )
        self._drawdown = DrawdownTracker()
        self._daily_pnl: Decimal = Decimal("0")
        self._equity: Decimal = Decimal("0")
        self._day_start_equity: Decimal = Decimal("0")
        self._day_start_date: datetime | None = None
        self._validations_passed: int = 0
        self._validations_failed: int = 0
        self._bus = get_event_bus()
        # Live rolling win-rate/avg-win/avg-loss feed for Kelly Criterion
        # sizing (risk/position_sizing.py::kelly_position_size(), only
        # reachable when position_sizing_method="kelly" — see
        # calculate_position_size()). Always constructed (cheap, in-memory,
        # bounded by kelly_lookback_trades) regardless of
        # position_sizing_method, so switching the config on later doesn't
        # require a restart to start accumulating sample — it's simply
        # unread until then. Fed by the same PositionClosedEvent
        # subscription that already drives record_pnl() below.
        self._trade_tracker = RollingTradeTracker(window=config.kelly_lookback_trades)

    async def start(self) -> None:
        """
        Subscribe to position-close events so realized P&L feeds the daily
        loss circuit breaker. Without this, record_pnl() is never invoked
        by anything and validate_signal's daily-loss check can never trip
        from real trading losses.

        Also subscribes to CandleClosedEvent to feed the (optional)
        CorrelationTracker with closing prices, if one was supplied. This
        subscription only takes effect if start() is actually invoked from
        the lifecycle layer — see startup.py.
        """
        self._bus.subscribe(PositionClosedEvent, self._on_position_closed)
        log_msg = "RiskManager started (subscribed to PositionClosedEvent"
        if self._correlation_tracker is not None:
            self._bus.subscribe(CandleClosedEvent, self._on_candle_closed)
            log_msg += ", CandleClosedEvent"
        logger.info("%s)", log_msg)

    async def _on_position_closed(self, event: Any) -> None:
        if not isinstance(event, PositionClosedEvent):
            return
        net_pnl = event.realized_pnl - event.fees
        self.record_pnl(net_pnl)
        self._trade_tracker.record(net_pnl)
        if self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN:
            self._circuit_breaker.record_probation_result(float(net_pnl))

    async def _on_candle_closed(self, event: Any) -> None:
        if not isinstance(event, CandleClosedEvent):
            return
        if self._correlation_tracker is None:
            return
        self._correlation_tracker.update(event.symbol, float(event.close))

    # ─── Primary gate ─────────────────────────────────────────────────────────

    async def validate_signal(self, signal: Signal) -> bool:
        """
        Full pre-trade risk validation.

        Returns True if the signal passes all risk checks.
        Raises specific risk exceptions on failure.
        """
        symbol = signal.symbol

        # 1. Circuit breaker check
        self._circuit_breaker.check_cooldown()
        if self._circuit_breaker.is_open:
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            raise CircuitBreakerOpenError(self._circuit_breaker.open_reason or "unknown")

        # While HALF_OPEN, only a single reduced-size probation trade is
        # allowed through at a time to re-verify recovery — everything else
        # is rejected until that trade's outcome is known (see
        # CircuitBreaker.allow_probation_trade / record_probation_result).
        if (
            self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN
            and not self._circuit_breaker.allow_probation_trade
        ):
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            raise CircuitBreakerOpenError(
                "HALF_OPEN probation trade already in progress"
            )

        # 2. Max positions (counts open positions AND pending/in-flight
        # entry orders for symbols that don't already have an open
        # position, so several unfilled entries into different symbols
        # can't collectively exceed the cap before any of them settle).
        effective_count = self._portfolio.open_position_count
        if self._orders is not None:
            open_position_symbols = {p.symbol for p in self._portfolio.open_positions}
            in_flight_symbols = {
                o.symbol
                for o in self._orders.get_open_orders()
                if not o.reduce_only and o.symbol not in open_position_symbols
            }
            effective_count += len(in_flight_symbols)

        if effective_count >= self._config.max_open_positions:
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            raise MaxPositionsExceededError(
                effective_count,
                self._config.max_open_positions,
            )

        # 3. Duplicate position
        if self._portfolio.has_position_for_symbol(symbol):
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            raise RiskLimitExceededError(
                f"already_has_position_{symbol}", 1.0, 0.0
            )

        # 4. Daily loss limit
        if self._day_start_equity > Decimal("0"):
            daily_loss_pct = abs(self._daily_pnl) / self._day_start_equity * Decimal("100")
            if self._daily_pnl < Decimal("0") and daily_loss_pct >= self._config.max_daily_loss_pct:
                self._circuit_breaker.trip(f"Daily loss {daily_loss_pct:.2f}%")
                self._validations_failed += 1
                risk_validations.labels(result="failed").inc()
                # DailyLossLimitReachedEvent existed as schema only — the
                # limit was enforced (via DailyLossLimitError + circuit
                # breaker trip) but nothing ever told notifiers/dashboards
                # *why* trading halted beyond the generic circuit-breaker
                # state change.
                await self._bus.publish(
                    DailyLossLimitReachedEvent(
                        daily_pnl=self._daily_pnl,
                        limit_pct=self._config.max_daily_loss_pct,
                    )
                )
                raise DailyLossLimitError(
                    float(daily_loss_pct),
                    float(self._config.max_daily_loss_pct),
                )

        # 5. Drawdown limit
        dd = self._drawdown.current_drawdown_pct
        if dd >= float(self._config.max_drawdown_pct):
            self._circuit_breaker.trip(f"Drawdown {dd:.2f}%")
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            await self._bus.publish(
                RiskLimitBreachedEvent(
                    limit_name="max_drawdown",
                    current_value=dd,
                    limit_value=float(self._config.max_drawdown_pct),
                    severity=AlertSeverity.CRITICAL,
                )
            )
            raise RiskLimitExceededError("max_drawdown", dd, float(self._config.max_drawdown_pct))

        # 6. Minimum risk/reward
        rr = float(signal.risk_reward_ratio)
        if rr < float(self._config.risk_reward_minimum):
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            raise RiskLimitExceededError(
                "risk_reward", rr, float(self._config.risk_reward_minimum)
            )

        # 6b. Stop-loss distance sanity check. validate_stop_risk() existed
        # as a standalone function but was never called from the live
        # validation gate — a stop from a bad ATR/structure/OB calculation
        # (e.g. zero-width OB, ATR data glitch, misconfigured multiplier)
        # could slip through with an absurdly tight or wide stop distance,
        # the former causing a wildly oversized position for the intended
        # risk budget, the latter risking far more than max_portfolio_risk_pct
        # if it filled and immediately reversed.
        if not validate_stop_risk(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            equity=self._equity,
            risk_pct=self._config.max_portfolio_risk_pct,
            max_stop_pct=self._config.max_stop_loss_pct,
            min_stop_pct=self._config.min_stop_loss_pct,
        ):
            self._validations_failed += 1
            risk_validations.labels(result="failed").inc()
            stop_dist_pct = (
                abs(signal.entry_price - signal.stop_loss) / signal.entry_price * Decimal("100")
                if signal.entry_price > Decimal("0")
                else Decimal("0")
            )
            raise RiskLimitExceededError(
                "stop_loss_distance", float(stop_dist_pct), float(self._config.max_stop_loss_pct)
            )

        # 7. Signal score (soft check — log but don't reject)
        if signal.score < 55.0:
            logger.warning("Low confidence signal for %s: score=%.1f", symbol, signal.score)

        if self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN:
            self._circuit_breaker.begin_probation_trade()

        self._validations_passed += 1
        risk_validations.labels(result="passed").inc()
        logger.info(
            "Risk validated: %s %s score=%.1f RR=%.2f",
            symbol, signal.direction, signal.score, rr,
        )
        return True

    # ─── Position sizing ──────────────────────────────────────────────────────

    async def calculate_position_size(self, signal: Signal) -> Decimal:
        """
        Calculate position size using fixed fractional risk.

        Risk = equity × risk_pct
        Size = risk / (entry - stop_loss)
        """
        if self._equity <= Decimal("0"):
            raise PositionSizingError("Equity is zero — cannot size position")

        risk_amount = self._equity * (self._config.max_portfolio_risk_pct / Decimal("100"))
        stop_distance = abs(signal.entry_price - signal.stop_loss)

        if stop_distance <= Decimal("0"):
            raise PositionSizingError(
                f"Invalid stop distance {stop_distance} for {signal.symbol}"
            )

        size = calculate_position_size(
            equity=self._equity,
            risk_pct=self._config.max_portfolio_risk_pct,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
        )

        # Optional alternative base-size model (configs/risk.yaml's
        # position_sizing_method, default "fixed_fractional" — unchanged
        # behavior from before this branch existed). "volatility_adjusted"
        # scales the fixed-fractional size above down when this signal's
        # stop is wider than the configured target ATR multiple, and up
        # (never past the fixed-fractional amount) when tighter — see
        # risk/position_sizing.py::volatility_adjusted_size(). Reuses the
        # same base size rather than recomputing from scratch, so it can
        # only ever reduce or match, never increase beyond, what
        # fixed-fractional alone would have produced.
        if self._config.position_sizing_method == "volatility_adjusted":
            atr_value = signal.metadata.get("atr") if signal.metadata else None
            if atr_value is not None and float(atr_value) > 0:
                size = volatility_adjusted_size(
                    equity=self._equity,
                    risk_pct=self._config.max_portfolio_risk_pct,
                    entry_price=signal.entry_price,
                    stop_loss=signal.stop_loss,
                    atr=float(atr_value),
                    target_atr_risk=self._config.volatility_target_atr_multiple,
                )
            else:
                logger.warning(
                    "position_sizing_method='volatility_adjusted' but signal "
                    "for %s has no usable ATR in metadata — falling back to "
                    "fixed-fractional size for this signal",
                    signal.symbol,
                )
        elif self._config.position_sizing_method == "kelly":
            # Fractional Kelly sizing (risk/position_sizing.py::
            # kelly_position_size()), fed by the live rolling win-rate/
            # avg-win/avg-loss tracker above. Unlike fixed_fractional/
            # volatility_adjusted, Kelly's implied quantity isn't derived
            # from this signal's own stop distance at all — it's a
            # bet-fraction-of-equity model, sized purely from historical
            # edge and entry price. Every cap below (max_position_pct_of_
            # equity, symbol max_notional, correlation adjustment,
            # liquidation check) still applies on top exactly as it does
            # for the other two methods, so a bad Kelly estimate still
            # can't bypass the account-level risk ceilings.
            if self._trade_tracker.has_min_sample(self._config.kelly_min_trades):
                size = kelly_position_size(
                    equity=self._equity,
                    win_rate=self._trade_tracker.win_rate,
                    avg_win=self._trade_tracker.avg_win,
                    avg_loss=self._trade_tracker.avg_loss,
                    entry_price=signal.entry_price,
                    kelly_fraction=self._config.kelly_fraction,
                    max_risk_pct=self._config.kelly_max_risk_pct,
                )
                if size <= Decimal("0"):
                    # kelly_position_size() itself returns 0 when the
                    # estimated edge is non-positive (kelly_f <= 0) —
                    # a real "no edge, don't size this trade with Kelly"
                    # result, not a data problem, so fall back rather
                    # than raise PositionSizingError below for it.
                    logger.info(
                        "Kelly sizing returned zero/negative edge for %s "
                        "(win_rate=%.2f avg_win=%.2f avg_loss=%.2f) — "
                        "falling back to fixed-fractional size",
                        signal.symbol, self._trade_tracker.win_rate,
                        self._trade_tracker.avg_win, self._trade_tracker.avg_loss,
                    )
                    size = calculate_position_size(
                        equity=self._equity,
                        risk_pct=self._config.max_portfolio_risk_pct,
                        entry_price=signal.entry_price,
                        stop_loss=signal.stop_loss,
                    )
            else:
                logger.warning(
                    "position_sizing_method='kelly' but only %d/%d closed "
                    "trades tracked — falling back to fixed-fractional "
                    "size for %s until enough live history accumulates",
                    self._trade_tracker.sample_size, self._config.kelly_min_trades,
                    signal.symbol,
                )

        # Cap position to max_position_pct_of_equity
        max_notional = self._equity * (self._config.max_position_pct_of_equity / Decimal("100"))
        max_size = max_notional / signal.entry_price
        size = min(size, max_size)

        # Optional per-symbol hard notional cap (configs/symbols.yaml's
        # max_notional, via LeverageConfig). Distinct from the equity-pct
        # cap above: that one scales with account equity, this one is a
        # fixed ceiling — useful for a symbol whose order book can't
        # absorb an equity-proportional size without excess slippage
        # regardless of how large the account grows.
        symbol_leverage_cfg = self._leverage_configs.get(signal.symbol)
        if symbol_leverage_cfg is not None and symbol_leverage_cfg.max_notional is not None:
            symbol_max_size = symbol_leverage_cfg.max_notional / signal.entry_price
            size = min(size, symbol_max_size)

        # HALF_OPEN probation trades trade at reduced size rather than full
        # size immediately after a cooldown — a genuine reduced-size
        # re-verification step instead of passing full-size trades through
        # the instant the breaker leaves OPEN.
        multiplier = Decimal(str(self._circuit_breaker.size_multiplier))
        if multiplier != Decimal("1"):
            size = size * multiplier

        # Correlation-adjusted sizing: scale down (never up) the incremental
        # size of this new position based on how correlated it is with
        # symbols already held, so several "independent" 1%-risk trades
        # don't quietly compound into one concentrated bet when the
        # symbols move together. Only runs when a CorrelationTracker was
        # wired in (see __init__/start()); otherwise the coarse
        # all-majors-are-one-group cap further down is the only guard.
        if self._correlation_tracker is not None:
            size = correlation_adjusted_size(
                size,
                signal.symbol,
                self._portfolio,
                self._correlation_tracker,
            )

        if size <= Decimal("0"):
            raise PositionSizingError(f"Calculated zero position size for {signal.symbol}")

        # Pre-trade liquidation check: at the leverage actually set on the
        # exchange for this symbol, the stop-loss must be hit before the
        # position would be liquidated — otherwise the "protective" stop is
        # fiction, since the exchange would force-close the position first.
        # margin.py's helpers existed but were never called from here, so
        # this check never ran. Uses the per-symbol leverage from
        # configs/symbols.yaml (falling back to the account-wide ceiling
        # only for symbols with no explicit entry) — checking against
        # max_leverage unconditionally understated real margin usage for
        # any symbol actually running below the ceiling.
        leverage = self._symbol_leverage.get(signal.symbol, int(self._config.max_leverage))
        if signal.direction == SignalDirection.LONG:
            liq_price = liquidation_price_long(signal.entry_price, leverage)
            if signal.stop_loss <= liq_price:
                raise PositionSizingError(
                    f"Stop-loss {signal.stop_loss} for {signal.symbol} is at or beyond the "
                    f"estimated liquidation price {liq_price} at {leverage}x leverage — the "
                    f"position would be liquidated before the stop-loss could trigger"
                )
        else:
            liq_price = liquidation_price_short(signal.entry_price, leverage)
            if signal.stop_loss >= liq_price:
                raise PositionSizingError(
                    f"Stop-loss {signal.stop_loss} for {signal.symbol} is at or beyond the "
                    f"estimated liquidation price {liq_price} at {leverage}x leverage — the "
                    f"position would be liquidated before the stop-loss could trigger"
                )

        # Pre-trade margin sufficiency check: the initial margin required to
        # open this position at max leverage must not exceed available
        # equity, or the exchange will reject (or partially reject) the
        # order.
        margin_required = initial_margin(size, signal.entry_price, leverage)
        if margin_required > self._equity:
            raise PositionSizingError(
                f"Insufficient margin for {signal.symbol}: requires ~{margin_required} "
                f"but only {self._equity} equity is available at {leverage}x leverage"
            )

        # Correlated-exposure cap: max_correlated_exposure_pct was defined
        # in config but never enforced anywhere — exposure.py's
        # CorrelationTracker exists but isn't wired to a live candle feed
        # here. As an honest, simple stand-in rather than a fabricated
        # correlation calculation: this system currently only trades
        # majors (BTCUSDT/ETHUSDT) which move together, so all open
        # symbols are treated as one correlated group and their combined
        # notional — including the position being sized now — is capped
        # against equity. If non-major/uncorrelated symbols are added
        # later, this should be replaced with a real per-pair correlation
        # check using CorrelationTracker.
        new_notional = size * signal.entry_price
        existing_notional = self._portfolio.gross_exposure
        combined_notional = existing_notional + new_notional
        max_correlated_notional = self._equity * (
            self._config.max_correlated_exposure_pct / Decimal("100")
        )
        if combined_notional > max_correlated_notional:
            raise PositionSizingError(
                f"Correlated exposure cap exceeded for {signal.symbol}: existing "
                f"{existing_notional} + new {new_notional} = {combined_notional} would "
                f"exceed {self._config.max_correlated_exposure_pct}% of equity "
                f"({max_correlated_notional})"
            )

        # Gross exposure cap: max_correlated_exposure_pct above only fires
        # for the "all majors move together" grouping. This is the general,
        # symbol-agnostic ceiling on total notional (longs + shorts
        # combined) across the whole book, via ExposureTracker.
        gross_pct = combined_notional / self._equity * Decimal("100")
        if gross_pct > self._config.max_gross_exposure_pct:
            raise PositionSizingError(
                f"Gross exposure cap exceeded for {signal.symbol}: projected gross "
                f"exposure {gross_pct:.2f}% would exceed "
                f"{self._config.max_gross_exposure_pct}% of equity"
            )

        # Net exposure cap: a book can satisfy every per-position,
        # per-symbol, and gross-exposure limit above and still end up
        # almost entirely long or almost entirely short if positions keep
        # landing on the same side. This checks the *directional* skew
        # projected after this fill, independent of gross notional.
        signed_new_notional = (
            new_notional if signal.direction == SignalDirection.LONG else -new_notional
        )
        projected_net = self._portfolio.net_exposure + signed_new_notional
        net_pct = abs(projected_net) / self._equity * Decimal("100")
        if net_pct > self._config.max_net_exposure_pct:
            raise PositionSizingError(
                f"Net exposure cap exceeded for {signal.symbol}: projected net exposure "
                f"{projected_net} ({net_pct:.2f}%) would exceed "
                f"{self._config.max_net_exposure_pct}% of equity"
            )

        logger.debug(
            "Position size: %s qty=%s risk=%s stop_dist=%s",
            signal.symbol, size, risk_amount, stop_distance,
        )
        return size

    # ─── State updates ────────────────────────────────────────────────────────

    def update_equity(self, equity: Decimal) -> None:
        """Update current equity and drawdown tracking."""
        self._equity = equity
        self._drawdown.update(float(equity))

        # Reset daily P&L at start of day
        now = datetime.now(tz=UTC)
        if self._day_start_date is None or self._day_start_date.date() < now.date():
            self._day_start_equity = equity
            self._day_start_date = now
            self._daily_pnl = Decimal("0")

        self._portfolio.update_equity(equity)

    def record_pnl(self, pnl: Decimal) -> None:
        """Record realized P&L (from a closed position)."""
        self._daily_pnl += pnl
        if pnl < Decimal("0"):
            # Use day-start equity as the denominator so successive losses in
            # a session don't inflate the reported percentage (current equity
            # shrinks with each loss, making later losses look proportionally
            # worse than they are against a moving baseline).
            base = self._day_start_equity if self._day_start_equity > Decimal("0") else self._equity
            abs_loss_pct = abs(pnl) / base * 100 if base > 0 else 0
            logger.info("Loss recorded: %s (%.2f%% of day-start equity)", pnl, abs_loss_pct)

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker (authorized action only)."""
        self._circuit_breaker.reset()
        logger.warning("Circuit breaker manually reset")

    async def check_portfolio_risk(self) -> bool:
        """
        Ongoing (not just pre-trade) portfolio risk check.

        Complements the projected checks in calculate_position_size, which
        only run when a *new* signal is being sized. This catches a book
        that has drifted over the gross exposure ceiling purely from price
        moves on existing positions, with no new trade to trigger a check.
        """
        if self._circuit_breaker.is_open:
            return False
        if not self._exposure_tracker.is_within_limits(self._portfolio, self._equity):
            gross_pct = self._exposure_tracker.gross_exposure_pct(self._portfolio, self._equity)
            logger.warning(
                "Portfolio gross exposure %.2f%% exceeds %.2f%% limit — tripping circuit breaker",
                gross_pct, self._config.max_gross_exposure_pct,
            )
            self._circuit_breaker.trip(f"Gross exposure {gross_pct:.2f}%")
            await self._bus.publish(
                RiskLimitBreachedEvent(
                    limit_name="max_gross_exposure",
                    current_value=gross_pct,
                    limit_value=float(self._config.max_gross_exposure_pct),
                    severity=AlertSeverity.CRITICAL,
                )
            )
            return False
        return True

    async def on_position_update(self, position: Any) -> None:
        pass  # Handled by event bus

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        return self._circuit_breaker.state

    @property
    def is_trading_allowed(self) -> bool:
        return not self._circuit_breaker.is_open

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def daily_pnl(self) -> Decimal:
        return self._daily_pnl

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "equity": str(self._equity),
            "daily_pnl": str(self._daily_pnl),
            "drawdown_pct": self._drawdown.current_drawdown_pct,
            "max_drawdown_pct": self._drawdown.max_drawdown_pct,
            "risk_level": classify_risk_level(
                self._drawdown.current_drawdown_pct, float(self._config.max_drawdown_pct)
            ).value,
            "circuit_breaker": self._circuit_breaker.state.value,
            "daily_loss_limit_pct": float(self._config.max_daily_loss_pct),
            "drawdown_limit_pct": float(self._config.max_drawdown_pct),
            "validations_passed": self._validations_passed,
            "validations_failed": self._validations_failed,
            "open_positions": self._portfolio.open_position_count,
        }
