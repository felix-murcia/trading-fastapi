from decimal import Decimal
from dataclasses import dataclass
from .money import Money


@dataclass(frozen=True)
class Spread:
    pips: Decimal

    def __post_init__(self):
        if self.pips <= Decimal("0"):
            raise ValueError("Spread must be positive")


@dataclass(frozen=True)
class StopLoss:
    pips: Decimal

    def __post_init__(self):
        if self.pips <= Decimal("0"):
            raise ValueError("StopLoss must be positive")
        if self.pips > Decimal("1000"):
            raise ValueError("StopLoss too large (>1000 pips)")


@dataclass(frozen=True)
class SafeLockEur:
    amount: Decimal

    def __post_init__(self):
        if self.amount <= Decimal("0"):
            raise ValueError("SafeLock amount must be positive")
        if self.amount < Decimal("4.00"):
            raise ValueError("SafeLock minimum is 4.00 EUR")