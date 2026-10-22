"""Reglas de negocio puras del dominio (sin infraestructura).

Convención crítica (AGENTS.md — Critical Conventions):

    "SL/TP must include spread. Any close with < 3 EUR profit is a loss."

Un cierre solo cuenta como ganancia si el beneficio neto llega a
MIN_WIN_EUR euros. Todo beneficio inferior —incluido 0.00— se clasifica
como PÉRDIDA: no cubre el coste real de la operación (spread, slippage,
comisiones implícitas) y tratarlo como "win" contamina el win rate del
dashboard, el Post-Trade Journal que alimenta a Qwen y las métricas de
monitorización del retrain.
"""
import math

MIN_WIN_EUR = 3.0

# Buffer de spread que el EA suma al colocar el SL
# (`MathMax(spread, 2.0*pipSize)` en mql5/AI_Quant_Terminal.mq5). Constante
# ESPEJADA en el EA (v14.x): si cambia ahí, cambia aquí. Vive en el dominio
# porque el contrato de dinero y la geometría deben ver la distancia REAL del
# stop; antes colgaba de `ml.trading_env_v2` y `services` importaba `ml`.
SPREAD_BUFFER_PIPS = 2.0

# 2026-10-22: `MIN_ENTRY_REWARD_EUR` se retiró. Era el mínimo de ENTRADA
# separado del de clasificación (C1); ahora la factibilidad de una entrada
# la decide el contrato de dinero (`services/trade_contract.evaluate_trade`,
# I3: beneficio NETO ≥ max(MIN_WIN_EUR, min_lock_eur)) una sola vez y en
# EUR netos de costes. Dos mínimos eran dos verdades.


def is_winning_close(pnl: float) -> bool:
    """True si y solo si el cierre alcanza MIN_WIN_EUR euros.

    Frontera: pnl >= 3.0 → ganancia; pnl < 3.0 (0.95, 0.00, -4.16…) → pérdida.
    """
    return float(pnl) >= MIN_WIN_EUR



def size_ppo_position(
    volume_norm: float,
    sl_pips: float,
    equity: float,
    pip_value: float,
    *,
    risk_pct: float,
    max_lot: float,
    min_lot: float = 0.01,
) -> float:
    """P2-2: sizing del PPO — riesgo fraccional por equity, delegado.

    La cabeza `volume` de la política (Box(2,)_[1], rango [0,1]) modula el
    riesgo entre 0 y ``risk_pct`` del equity::

        lots = equity × risk_pct × clip(volume_norm, 0, 1) / (sl_pips × pip_value)

    Los lotes se redondean HACIA ABAJO al paso ``min_lot`` (nunca se supera el
    tope de riesgo) y se devuelven 0.0 —sin entrada— cuando ni el lote mínimo
    cabe en el presupuesto de riesgo.

    Contrato train/serve (auditoría P2-2): lo usan `/api/v1/ai/predict` (con
    ``pip_value_per_lot(symbol, price)`` centralizado) y el entorno de
    entrenamiento (con ``pip_size × lot_size``, la convención de dinero del
    env). Antes: tope degenerado ``min(0.5, 3%·equity/dist)`` siempre activo y
    una heurística "mín. 3€" con 0.1 €/pip/lot (error ×100 enmascarado por el
    clip) — la cabeza `volume` estaba muerta.
    """
    if equity <= 0.0 or sl_pips <= 0.0 or pip_value <= 0.0 or risk_pct <= 0.0:
        return 0.0
    scale = min(1.0, max(0.0, float(volume_norm)))
    risk_usd = float(equity) * float(risk_pct) * scale
    if risk_usd <= 0.0:
        return 0.0
    lots_raw = risk_usd / (float(sl_pips) * float(pip_value))
    # Epsilon RELATIVO al cociente (no absoluto): el `volume_norm` viene de
    # una cabeza float32 del PPO (p.ej. 0.03999999910593033) ⇒ el riesgo
    # sale con ruido de ~1e-8 RELATIVO. Con un epsilon absoluto de 1e-9 el
    # floor tumbaba un lote que SÍ cabía al borde del mínimo (medido
    # 2026-10-21: 0.00999999977 lote con min_lot 0.01 ⇒ 0 lotes ⇒
    # "sizing inviable" en una entrada válida). El epsilon relativo (1e-6
    # del cociente) es 2 órdenes de magnitud mayor que ese ruido y 4
    # órdenes menor que el paso de lote ⇒ nunca promueve un lote
    # genuinamente inviable.
    lots = math.floor(lots_raw / min_lot + 1e-6) * min_lot
    lots = round(lots, 2)
    if lots < min_lot:
        return 0.0
    return float(min(lots, max_lot))
