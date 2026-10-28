"""Migrado de legacy/core/rules.py:32 -- is_winning_close(pnl) -> bool

Regla de negocio: un cierre cuenta como ganancia SOLO si alcanza
MIN_WIN_EUR (3.00 EUR). Todo debajo (0.95, 0.00, -4.16...) es perdida.
No se mejora ni se cambia el umbral; paridad exacta.
"""

from decimal import Decimal
from src.domain.value_objects.pnl import PnL
from src.domain.value_objects.money import Money
from src.domain.value_objects.thresholds import TradingThresholds


def is_winning_close(pnl: PnL, thresholds: TradingThresholds | None = None) -> bool:
    if thresholds is None:
        thresholds = TradingThresholds()
    return pnl.is_winning(Money(thresholds.min_win_eur))
