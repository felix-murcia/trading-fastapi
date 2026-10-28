"""Migrado de legacy/ml/trading_env_v2.py:911 -- production_stops(atr_norm, pip_size, ...)

Geometria "suelo / techo mas cercano" (Felix 2026-10-21). SL en el suelo mas cercano
bajo la entrada; TP en el techo mas cercano sobre la entrada. Fallback: geometria ATR.
No se mejora; paridad exacta con legacy (production_sl / production_tp).
"""

SPREAD_BUFFER_PIPS = 2.0
MIN_LEVEL_PIPS = 5.0


def production_stops(
    atr_norm: float,
    pip_size: float,
    direction: int = 0,
    floor_below: float | None = None,
    ceiling_above: float | None = None,
    current_price: float | None = None,
) -> tuple[float, float]:
    """SL y TP por geometria suelo/techo mas cercano (P2-1 + G3-2).

    Paridad exacta con legacy: production_sl(...) y production_tp(...).
    """
    atr_pips = atr_norm / pip_size
    _has_levels = (
        direction in (1, -1)
        and floor_below is not None
        and ceiling_above is not None
        and current_price is not None
        and current_price > 0
        and float(floor_below) > 0
        and float(ceiling_above) > 0
    )

    # ---- production_sl (legacy lines 850-865) ----
    sl_pips = max(10.0, min(50.0, atr_pips * 1.5))     # fallback ATR
    if _has_levels:
        if direction == 1:      # LONG: SL bajo el suelo
            sl_px = float(floor_below) - SPREAD_BUFFER_PIPS * float(pip_size)
        else:                   # SHORT: SL sobre el techo
            sl_px = float(ceiling_above) + SPREAD_BUFFER_PIPS * float(pip_size)
        sl_c = abs(float(current_price) - sl_px) / float(pip_size)
        if sl_c >= MIN_LEVEL_PIPS:
            sl_pips = max(10.0, min(50.0, sl_c))

    # ---- production_tp (legacy lines 887-908) ----
    tp_pips = sl_pips * 2.0
    if _has_levels:
        tp_pips = None
        near = None
        if direction == 1:
            near = abs(float(ceiling_above) - float(current_price)) / float(pip_size)
        elif direction == -1:
            near = abs(float(floor_below) - float(current_price)) / float(pip_size)
        tp_pips = near if (near is not None and near >= MIN_LEVEL_PIPS) else sl_pips * 2.0
    tp_pips = max(10.0, min(200.0, tp_pips))

    return sl_pips, tp_pips
