from decimal import Decimal
from dataclasses import dataclass


@dataclass(frozen=True)
class TradingThresholds:
    min_win_eur: Decimal = Decimal("3.00")
    safe_lock_eur: Decimal = Decimal("4.00")
    cost_buffer_pips: int = 2