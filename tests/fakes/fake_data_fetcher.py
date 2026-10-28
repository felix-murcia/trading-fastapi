"""Fake DataFetcher en memoria (satisface DataFetcher por typing estructural).

Devuelve TradeOutcome predefinidos. Para tests de caracterización de RunAutoRetrain.
"""

from src.domain.rules.trade_outcome import TradeOutcome


class FakeDataFetcher:
    def __init__(self, outcomes: list | None = None, candles: list | None = None):
        self.outcomes = outcomes or []
        self.candles = candles or []
        self.fetch_called = False

    def fetch_trade_outcomes(self, symbol: str, since: float | None = None) -> list:
        self.fetch_called = True
        return list(self.outcomes)

    def fetch_candles(self, symbol: str, count: int, timeframe: str = "H1") -> list:
        self.fetch_called = True
        return list(self.candles)
