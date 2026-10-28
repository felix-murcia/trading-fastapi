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

    def save_outcome(self, outcome) -> None:
        self.outcomes.append(outcome)

    def load_outcomes(self, symbol: str) -> list:
        return [o for o in self.outcomes if o.symbol == symbol]

    def save_state(self, symbol: str, state: object) -> None:
        self.states[symbol] = state

    def load_state(self, symbol: str) -> object | None:
        return self.states.get(symbol)

    def __init__(self):
        self.store = {}
        self.saved = []
        self.outcomes = []
        self.states = {}
