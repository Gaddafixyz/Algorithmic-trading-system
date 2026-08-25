"""
Portfolio-level capital allocation and rebalancing.

- ``allocator``: converts weights (equal/risk-parity/fixed) + equity into
  per-symbol target notional, subject to account risk constraints.
- ``rebalancer``: detects drift between current positions and target
  allocation. Detection-only — never places orders itself.
- ``service``: facade combining the domain ``Portfolio`` with the above two.
"""

from src.portfolio.allocator import AllocationMethod, PortfolioAllocator
from src.portfolio.rebalancer import PortfolioRebalancer, RebalanceAction, RebalanceDirection
from src.portfolio.service import PortfolioService

__all__ = [
    "AllocationMethod",
    "PortfolioAllocator",
    "PortfolioRebalancer",
    "RebalanceAction",
    "RebalanceDirection",
    "PortfolioService",
]
