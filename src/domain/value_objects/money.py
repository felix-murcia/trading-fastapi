from decimal import Decimal
from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = "EUR"

    def __post_init__(self):
        if self.currency != "EUR":
            raise ValueError("Only EUR supported")
