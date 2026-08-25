"""Trade expectancy and R-multiple analysis."""

from __future__ import annotations

from decimal import Decimal

from src.domain.trade import Trade


def expectancy(trades: list[Trade]) -> Decimal:
    """
    Mathematical expectancy per trade.

    E = (win_rate × avg_win) + (loss_rate × avg_loss)
    Positive expectancy = profitable system.
    """
    if not trades:
        return Decimal("0")
    winners = [t.net_pnl for t in trades if t.is_winner]
    losers = [t.net_pnl for t in trades if t.is_loser]
    n = Decimal(str(len(trades)))
    win_rate = Decimal(str(len(winners))) / n
    loss_rate = Decimal(str(len(losers))) / n
    avg_win = sum(winners, Decimal("0")) / Decimal(str(max(len(winners), 1)))
    avg_loss = sum(losers, Decimal("0")) / Decimal(str(max(len(losers), 1)))
    return win_rate * avg_win + loss_rate * avg_loss


def r_multiples(trades: list[Trade]) -> list[float]:
    """
    Calculate R-multiple for each trade (PnL / initial_risk).

    Trades without initial_risk are excluded.
    """
    result = []
    for t in trades:
        if t.initial_risk and t.initial_risk > Decimal("0"):
            result.append(float(t.net_pnl / t.initial_risk))
    return result


def system_quality_number(trades: list[Trade]) -> float:
    """
    System Quality Number (Van Tharp).

    SQN = sqrt(N) × mean_R / std_R
    > 2 is good, > 3 is excellent.
    """
    import math

    import numpy as np

    rs = r_multiples(trades)
    if len(rs) < 3:
        return 0.0
    arr = np.array(rs)
    std = np.std(arr, ddof=1)
    if std == 0:
        return 0.0
    return float(math.sqrt(len(rs)) * np.mean(arr) / std)
