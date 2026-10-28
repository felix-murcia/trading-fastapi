"""Regla de dominio: decodificación de dirección del modelo (decode_v3_direction).

Lógica idéntica a fastapi/ml/trading_env_v2.py:714 (decode_v3_direction).
1: LONG, -1: SHORT, 0: FLAT/CLOSE. Zona muerta de 5% para evitar mesetas
de gradiente nulo.

No lee os.environ. No toca infraestructura. Función pura.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DirectionDecision:
    target_pos: int   # 1=LONG, -1=SHORT, 0=FLAT
    decision: str     # "BUY", "SELL", "HOLD", "CLOSE", "FLAT"


def decode_direction(direction: float, current_position: int) -> DirectionDecision:
    """Decodifica la acción cruda del modelo en target_pos y decisión.

    Paridad exacta con legacy/ml/trading_env_v2.py:714.
    Zona muerta de 5% (|direction| <= 0.05 → FLAT).
    """
    if direction < -0.05:
        target_pos = -1
    elif direction > 0.05:
        target_pos = 1
    else:
        target_pos = 0

    if target_pos == 1:
        decision = "HOLD" if current_position == 1 else "BUY"
    elif target_pos == -1:
        decision = "HOLD" if current_position == -1 else "SELL"
    else:
        decision = "CLOSE" if current_position != 0 else "FLAT"

    return DirectionDecision(target_pos=target_pos, decision=decision)
