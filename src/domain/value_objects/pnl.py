from decimal import Decimal
from dataclasses import dataclass
from .money import Money


@dataclass(frozen=True)
class PnL:
    value: Money

    def is_winning(self, threshold: Money) -> bool:
        return self.value.amount >= threshold.amount
