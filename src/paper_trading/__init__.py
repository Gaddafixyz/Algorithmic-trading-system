"""Paper trading: simulated exchange adapter for TRADING_ENV=paper.

See ``paper_exchange_client.PaperExchangeClient`` for the implementation.
Paper trading routes through the exact same RiskManager -> ExecutionEngine
-> OrderManager -> FillEngine pipeline as live trading; there is no
separate/parallel simulator.
"""

from src.paper_trading.paper_exchange_client import PaperExchangeClient

__all__ = ["PaperExchangeClient"]
