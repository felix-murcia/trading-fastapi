"""Value object: resultado de un trade para feedback de retrain.

Migrado de fastapi/services/auto_retrain.py:138 (TradeOutcome).
Campos numéricos puros, sin infraestructura.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeOutcome:
    """Resultado de un trade cerrado, usado para feedback al entrenamiento."""

    symbol: str
    entry_time: float      # Unix timestamp (reloj del broker, tal cual)
    exit_time: float       # Unix timestamp (reloj del broker, tal cual)
    pnl: float             # USD, positivo = ganancia, negativo = pérdida
    pnl_pct: float         # % del balance en el momento del entry
    direction: str         # "LONG" o "SHORT"
    sl_hit: bool           # ¿Se tocó el stop loss?
    tp_hit: bool           # ¿Se tocó el take profit?
    exit_reason: str       # "sl", "tp", "manual", "news"
