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
import os

MIN_WIN_EUR = 3.0

# C1 (orden Félix 2026-10-20, informe de viabilidad): el mínimo de 3 € se
# usaba para DOS papeles distintos y se confundían en una sola constante:
#   (a) CLASIFICACIÓN: qué cierre cuenta como "ganancia" (ésta es regla de
#       negocio de AGENTS.md y NO se toca: sigue en 3 €);
#   (b) FILTRO DE ENTRADA: que el beneficio esperado cubra el coste real
#       (spread, slippage, comisiones) ⇒ depende del TAMAÑO: con la mitad
#       de riesgo por operación, el beneficio esperado también baja a la
#       mitad y un mínimo fijo de 3 € empezaría a bloquear entradas
#       (medido: 23 de las pruebas de servicio pasaron a HOLD).
# Separarlos permite reducir el riesgo SIN cambiar qué se opera: el mínimo
# de entrada escala con el riesgo (3 € con riesgo 3 % ⇒ 1,5 € con riesgo
# 1,5 %) y la clasificación de ganancias queda intacta.
# El valor por defecto (3.0) preserva el comportamiento histórico; se
# ajusta por env (MIN_ENTRY_REWARD_EUR) cuando cambia el riesgo.
MIN_ENTRY_REWARD_EUR = float(os.getenv("MIN_ENTRY_REWARD_EUR", "3.0"))


def is_winning_close(pnl: float) -> bool:
    """True si y solo si el cierre alcanza MIN_WIN_EUR euros.

    Frontera: pnl >= 3.0 → ganancia; pnl < 3.0 (0.95, 0.00, -4.16…) → pérdida.
    """
    return float(pnl) >= MIN_WIN_EUR
