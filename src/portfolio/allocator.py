"""
Capital allocation across concurrently traded symbols.

This module answers "how much notional should each symbol get", not
"what should each symbol's *weight* be" — the weighting math itself
(risk parity / inverse-volatility) already lives in
``src.risk.portfolio_risk.risk_parity_weights`` and is reused here rather
than duplicated, so there is exactly one place that computes risk-parity
weights for the whole system.

``PortfolioAllocator`` layers the account-level constraints on top of
whatever weights it's given: per-symbol notional cap
(``max_position_pct_of_equity``) and a cap on how many symbols can hold
a position at once (``max_open_positions``). Both mirror the equivalent
``RiskConfig`` fields so allocation targets can never imply a position
RiskManager's own pre-trade checks would reject.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import Enum

from src.risk.portfolio_risk import risk_parity_weights

logger = logging.getLogger(__name__)


class AllocationMethod(str, Enum):
    """How capital weights are derived across symbols."""

    EQUAL_WEIGHT = "equal_weight"
    RISK_PARITY = "risk_parity"
    FIXED = "fixed"


class PortfolioAllocator:
    """
    Computes target notional allocation per symbol from equity + weights.

    Stateless aside from its constraint parameters — callers pass in the
    current equity and candidate symbols on every call, so allocation
    targets are always computed fresh rather than drifting from stale
    cached state.
    """

    def __init__(
        self,
        max_position_pct_of_equity: Decimal = Decimal("20"),
        max_open_positions: int = 10,
    ) -> None:
        if max_position_pct_of_equity <= 0:
            raise ValueError("max_position_pct_of_equity must be positive")
        if max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        self.max_position_pct_of_equity = max_position_pct_of_equity
        self.max_open_positions = max_open_positions

    def allocate(
        self,
        equity: Decimal,
        symbols: list[str],
        *,
        method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT,
        volatilities: dict[str, float] | None = None,
        fixed_weights: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        """
        Compute target notional per symbol.

        Args:
            equity: Current account equity (target notionals scale off this).
            symbols: Candidate symbols to allocate across. If more than
                ``max_open_positions`` are given, only the top-weighted
                ``max_open_positions`` receive an allocation — the rest
                are silently omitted from the result (not zeroed with a
                0 entry), so callers can tell "not allocated" apart from
                "allocated 0 notional".
            method: Weighting scheme. RISK_PARITY requires ``volatilities``
                for every symbol or falls back to equal weight for symbols
                missing a volatility estimate (logged, not raised — a
                missing vol estimate for one symbol shouldn't block
                allocating the rest of the book).
            volatilities: symbol -> volatility estimate, required for
                RISK_PARITY.
            fixed_weights: symbol -> weight, required for FIXED. Does not
                need to sum to 1 — it's renormalized against the *provided*
                symbols so partial weight maps still work.

        Returns:
            symbol -> target notional (Decimal), for allocated symbols only.
            Empty dict if ``equity <= 0`` or no symbols given.
        """
        if equity <= 0 or not symbols:
            return {}

        weights = self._compute_weights(symbols, method, volatilities, fixed_weights)
        if not weights:
            return {}

        # Cap the number of allocated symbols, keeping the highest-weighted.
        if len(weights) > self.max_open_positions:
            top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
            top = top[: self.max_open_positions]
            dropped = set(weights) - {s for s, _ in top}
            logger.info(
                "Allocation capped at max_open_positions=%d; dropped %s",
                self.max_open_positions,
                sorted(dropped),
            )
            weights = dict(top)

        # Renormalize the (possibly-truncated) weight set back to 1.0 so
        # the remaining symbols still receive full-strength allocation
        # rather than silently under-deploying capital.
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return {}
        weights = {s: w / total_weight for s, w in weights.items()}

        cap = equity * self.max_position_pct_of_equity / Decimal("100")
        allocation: dict[str, Decimal] = {}
        for symbol, weight in weights.items():
            target = equity * Decimal(str(weight))
            if target > cap:
                logger.debug(
                    "%s target notional %.2f capped to %.2f (max_position_pct_of_equity=%s%%)",
                    symbol,
                    target,
                    cap,
                    self.max_position_pct_of_equity,
                )
                target = cap
            allocation[symbol] = target

        return allocation

    def _compute_weights(
        self,
        symbols: list[str],
        method: AllocationMethod,
        volatilities: dict[str, float] | None,
        fixed_weights: dict[str, Decimal] | None,
    ) -> dict[str, float]:
        if method == AllocationMethod.FIXED:
            if not fixed_weights:
                raise ValueError("fixed_weights required for AllocationMethod.FIXED")
            relevant = {s: float(w) for s, w in fixed_weights.items() if s in symbols and w > 0}
            return relevant

        if method == AllocationMethod.RISK_PARITY:
            vols = volatilities or {}
            have_vol = {s: vols[s] for s in symbols if s in vols and vols[s] > 0}
            missing = [s for s in symbols if s not in have_vol]
            if missing:
                logger.info(
                    "RISK_PARITY: no volatility estimate for %s — falling back to equal "
                    "weight for those symbols",
                    missing,
                )
            weights = risk_parity_weights(have_vol) if have_vol else {}
            if missing:
                # Blend: give equal-weighted symbols the average weight of
                # the risk-parity set so neither group is arbitrarily
                # favored just because of data availability.
                avg = (sum(weights.values()) / len(weights)) if weights else (1.0 / len(symbols))
                for s in missing:
                    weights[s] = avg
            return weights

        # EQUAL_WEIGHT
        if not symbols:
            return {}
        w = 1.0 / len(symbols)
        return dict.fromkeys(symbols, w)
