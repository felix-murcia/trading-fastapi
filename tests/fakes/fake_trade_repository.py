"""Fake TradeRepository en memoria (satisface TradeRepository por typing estructural)."""

from src.domain.ports import TradeRepository


class FakeTradeRepository(TradeRepository):
    def __init__(self):
        self.store = {}
        self.saved = []

    def save(self, trade) -> None:
        self.store[trade.id] = trade
        self.saved.append(trade)

    def find_by_id(self, id: str):
        return self.store.get(id)
