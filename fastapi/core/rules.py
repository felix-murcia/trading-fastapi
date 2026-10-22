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
MIN_WIN_EUR = 3.0

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
