"""Utilidades compartidas entre los subrouters de fastapi/api/routers/.

Contiene estado y helpers usados por más de un subrouter para evitar
duplicación. No contiene endpoints: solo funciones, clases y variables de
estado compartidas. Cada subrouter importa de aquí lo que necesita.
"""

from typing import Optional

from config import settings
from core.pair import PairRegistry
from services.auto_retrain import TRAINING_SYMBOL as _TRAINING_SYMBOL


# ─── Estado compartido ──────────────────────────────────────────────────────

_REGISTRY: Optional[PairRegistry] = None   # mundos configurados vía PAIRS (cache perezoso)
_BRAINS: dict = {}                          # symbol -> Brain (en memoria)
_QUOTE_CACHE: dict = {}                     # cycle_id -> {bid, ask, quote_time} (slippage en /trade/filled)
_DEAD_ZONE_COUNTER: dict[str, int] = {}     # symbol -> contador de histeresis CLOSE K=2 (/predict)

VALID_EXIT_REASONS = frozenset({"sl", "tp", "manual", "news", "signal_end", "ea"})
DIRECTION_CANONICAL = {"LONG": "LONG", "SHORT": "SHORT", "BUY": "LONG", "SELL": "SHORT"}


class Brain:
    """Bundle certificado de UN par en memoria (P0-5/P1-2 + P3-1b).

    Clase ligera sin __init__: los atributos se asignan por nombre al
    instanciar (Brain(model=..., vec_norm=..., bundle=..., ...)).
    """

    model: object
    vec_norm: object | None
    bundle: dict
    memory_channel: bool = False
    trend_features: bool = False
    higher_tf_features: bool = False


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_registry() -> PairRegistry:
    """Registro de pares (mundos) — construido una vez, de forma explícita.

    Construcción perezosa (nunca en import del módulo): los tests parchean
    `services.*` antes de que exista ningún adapter, y un PAIRS inválido
    falla con su error en el primer uso, no en el import.
    """
    global _REGISTRY
    if _REGISTRY is None:
        from core.container import build_registry_from_settings
        _REGISTRY = build_registry_from_settings()
    return _REGISTRY


def _sole_enabled_symbol() -> Optional[str]:
    """Símbolo del único par habilitado en PAIRS.

    `None` si hay 0 o varios habilitados: la ambigüedad no se resuelve por
    heurística — el caller debe elegir explícitamente.
    """
    enabled = [p.get("symbol") for p in settings.pairs if p.get("enabled")]
    return enabled[0] if len(enabled) == 1 else None


def _dead_zone_close(symbol: str, decision: str, env_position: int = 1) -> bool:
    """Histeresis CLOSE K=2. True = cerrar; False = mantener (HOLD).

    Resetea el contador cuando la decisión no es CLOSE o no hay posición.
    `env_position` default 1 = posición abierta (el caso que importa).
    """
    if decision != "CLOSE" or env_position == 0:
        _DEAD_ZONE_COUNTER[symbol] = 0
        return True
    count = _DEAD_ZONE_COUNTER.get(symbol, 0) + 1
    _DEAD_ZONE_COUNTER[symbol] = count
    return count >= 2
