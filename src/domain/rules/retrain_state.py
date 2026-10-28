"""Estado del ciclo de retrain por símbolo (entity frozen).

Migrado de fastapi/services/auto_retrain.py:152 (RetrainState).
Sin infraestructura: solo datos. El caso de uso inyecta el puerto TradeRepository
para persistencia.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrainState:
    filled_count: int = 0
    last_retrain_time: float = 0.0
    outcomes: tuple = field(default_factory=tuple)
    retrain_in_progress: bool = False
    scheduled_due: bool = False
    schedule_anchor: float = 0.0
    consecutive_failures: int = 0
