"""RealThresholds: construye TradingThresholds con valores de config.py.

No es un puerto de dominio, es un helper de infraestructura que lee los
valores de umbral desde la configuración y devuelve un TradingThresholds
listo para inyectar en los casos de uso.
"""

from decimal import Decimal

from src.domain.value_objects.thresholds import TradingThresholds


class RealThresholds:
    def __init__(self, min_lock_eur: float = 4.0,
                 min_win_eur: float = 3.0,
                 cost_buffer_pips: int = 2):
        self._min_lock_eur = Decimal(str(min_lock_eur))
        self._min_win_eur = Decimal(str(min_win_eur))
        self._cost_buffer_pips = cost_buffer_pips

    @classmethod
    def from_settings(cls, settings) -> "RealThresholds":
        return cls(
            min_lock_eur=settings.min_lock_eur,
            min_win_eur=3.0,
            cost_buffer_pips=2,
        )

    def build(self) -> TradingThresholds:
        return TradingThresholds(
            min_win_eur=self._min_win_eur,
            safe_lock_eur=self._min_lock_eur,
            cost_buffer_pips=self._cost_buffer_pips,
        )
