"""Contrato de dinero de UNA entrada — fuente única (2026-10-22).

POR QUÉ EXISTE
--------------
La orden 80 (USDCHF, 22-10 10:02 UTC) violó TRES reglas a la vez:

  · TP bruto 3,34 € (audit 3,36) con la regla "todo cierre < 3 € es
    PÉRDIDA" ⇒ el TP sellaba la salida por debajo de la regla **antes**
    de descontar el spread.
  · El trailing "Safe-4" del EA exige un beneficio de 4,00 € para bloquear
    el stop (`SL_LockSafetyNetEur`, mql5/AI_Quant_Terminal.mq5:648) ⇒ con
    un TP de 3,34 € el bloqueo **no puede dispararse nunca**.
  · Riesgo real 11,06 € con tope de 2 % sobre 486,86 € = 9,74 € ⇒ +13,5 %.

No eran tres bugs: era UNO. El lote, el SL, el TP y el gate vivían en
cuatro sitios que no se hablaban (probe de lote para elegir TP, suelo de
protección, tope de riesgo, gate de recompensa), cada uno con su propia
conversión de dinero. Aquí se decide UNA vez, en EUR netos, y se verifican
TRES invariantes:

    I1  riesgo_eur            ≤ equity × risk_pct
    I2  riesgo_eur            ≥ min_lock_eur        (el EA enciende el trailing)
    I3  beneficio_neto_eur    ≥ max(min_win_eur, min_lock_eur)
                                 ^ neto de costes: el TP paga la regla de
                                   3 € DESPUÉS del spread y el bloqueo de
                                   4 € cabe ANTES del TP

Si no existe lote que cumpla las tres, la entrada NO se abre y se declara
cuál es la restricción que la hace imposible. No hay heurísticas, ni
valores inventados, ni fallbacks silenciosos: todo sale del precio, del
pip value en EUR de la cuenta (`expected_close_eur`, la MISMA conversión
que usa el gate de recompensa y el ledger) y de constantes declaradas.

POR QUÉ EL PIP VALUE SALE DE `expected_close_eur`
-------------------------------------------------
`position_sizing.pip_value_per_lot` devuelve el pip en la divisa de
cotización (10 USD para EURUSD y para USDCHF). Usarlo contra un
presupuesto en EUR mezcla unidades: en los pares base=USD el lote sale
1/precio más grande de lo permitido (medido: +13,5 % de riesgo en USDCHF;
en USDJPY sería +600 %). Derivar el pip value en EUR de la MISMA función
que ya usa el gate (`expected_close_eur(sym, 1 pips, 1 lote, precio)`)
elimina la segunda conversión de la ecuación: sizing y recompensa no
pueden volver a discrepar.

QUÉ NO ES
---------
No modela el MDP ni la política: recibe el lote que pide la cabeza
`volume` (`head_lots`) y sólo lo CORRIGE a la ventana factible
[lot_lo, lot_hi]. El entorno de entrenamiento sigue simulando la
geometría con su dinero ficticio (no es EUR) — esta es una regla de la
capa de ejecución, como el gate de recompensa.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from core.rules import MIN_WIN_EUR, SPREAD_BUFFER_PIPS
from services.position_sizing import expected_close_eur

# Coste de ejecución declarado, en pips: es el MISMO buffer de spread que el
# EA suma al stop (mql5/AI_Quant_Terminal.mq5; `SPREAD_BUFFER_PIPS` vive en
# `core.rules`). No se inventa un spread: se usa el que el sistema
# ya asume al colocar el SL.
EXEC_COST_PIPS = SPREAD_BUFFER_PIPS


@dataclass(frozen=True)
class TradeContract:
    """Resultado del contrato: o una entrada factible, o el motivo del no."""

    ok: bool
    reason: str                 # "" si ok; si no, la restricción que manda
    lots: float
    sl_pips: float
    tp_pips: float
    ppl_eur: float              # € por pip y lote (cuenta en EUR)
    risk_eur: float
    reward_gross_eur: float
    cost_eur: float
    reward_net_eur: float
    lock_eur: float             # bloqueo Safe-4 exigido
    cap_eur: float              # tope de riesgo (I1)
    detail: dict = field(default_factory=dict)

    def audit(self) -> dict:
        """Payload de auditoría (snapshot replayable de la decisión)."""
        return {
            "contract_ok": self.ok,
            "contract_reason": self.reason,
            "ppl_eur": round(self.ppl_eur, 6),
            "risk_eur": round(self.risk_eur, 4),
            "reward_gross_eur": round(self.reward_gross_eur, 4),
            "cost_eur": round(self.cost_eur, 4),
            "reward_net_eur": round(self.reward_net_eur, 4),
            "lock_eur": self.lock_eur,
            "cap_eur": round(self.cap_eur, 4),
            **self.detail,
        }


def _floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-6) * step, 2)


def pip_value_eur_per_lot(
    symbol: str, price: float, eurusd_price: Optional[float] = None
) -> float:
    """€ por pip y lote en la divisa de la CUENTA (EUR).

    Delegado en `expected_close_eur` a propósito: es la MISMA conversión que
    usa el gate de recompensa y el ledger. Antes el sizing usaba el pip en
    USD (10) contra un presupuesto en EUR ⇒ en los pares base=USD el lote
    salía 1/precio más grande de lo permitido (medido: +13,5 % de riesgo en
    USDCHF; en USDJPY sería +600 %).
    """
    return expected_close_eur(symbol, 1.0, 1.0, price, eurusd_price=eurusd_price)


def _ceil_step(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-6) * step, 2)


def evaluate_trade(
    *,
    symbol: str,
    price: float,
    equity: float,
    sl_pips: float,
    head_lots: float,
    tp_candidates_pips: Sequence[float],
    risk_pct: float,
    min_lot: float,
    max_lot: float,
    min_lock_eur: float,
    eurusd_price: Optional[float] = None,
    min_win_eur: float = MIN_WIN_EUR,
    cost_pips: float = EXEC_COST_PIPS,
) -> TradeContract:
    """Decide lote y TP a la vez y verifica I1/I2/I3 en EUR netos.

    `head_lots` es el lote que pide la política (0 ⇒ no quiere entrada).
    `tp_candidates_pips` son las DISTANCIAS de TP candidatas del lado
    objetivo, en pips, en orden de preferencia (el llamador las ordena por
    proximidad; el contrato elige la primera que hace factible el trade).

    Lanza ValueError si faltan magnitudes físicas (tipo/razón inválidas):
    eso es un error de programación, nunca una sesión degradada.
    """
    if price <= 0.0 or equity <= 0.0 or sl_pips <= 0.0:
        raise ValueError(
            f"evaluate_trade exige price/equity/sl_pips > 0: {price}/{equity}/{sl_pips}"
        )
    if risk_pct <= 0.0 or min_lot <= 0.0 or max_lot < min_lot:
        raise ValueError(
            f"evaluate_trade exige risk_pct/min_lot > 0 y max_lot >= min_lot: "
            f"{risk_pct}/{min_lot}/{max_lot}"
        )

    # € por pip y lote: la MISMA conversión que el gate de recompensa y el
    # ledger (una sola fuente; lanza si el símbolo no tiene tasa EUR).
    ppl = pip_value_eur_per_lot(symbol, price, eurusd_price=eurusd_price)
    if ppl <= 0.0:
        raise ValueError(f"evaluate_trade: pip value EUR inválido para {symbol}: {ppl}")

    cap_eur = equity * risk_pct
    lock_eur = float(min_lock_eur)
    need_net = max(float(min_win_eur), lock_eur)

    # I1: el mayor lote que respeta el tope de riesgo.
    lot_hi = min(max_lot, _floor_step(cap_eur / (sl_pips * ppl), min_lot))

    def _reject(reason: str, detail: dict) -> TradeContract:
        return TradeContract(
            ok=False, reason=reason, lots=0.0, sl_pips=sl_pips, tp_pips=0.0,
            ppl_eur=ppl, risk_eur=0.0, reward_gross_eur=0.0, cost_eur=0.0,
            reward_net_eur=0.0, lock_eur=lock_eur, cap_eur=cap_eur,
            detail=detail,
        )

    if head_lots <= 0.0:
        return _reject("sizing_inviable", {"head_lots": head_lots})
    if lot_hi < min_lot:
        # Ni el lote mínimo cabe en el tope: el stop es demasiado ancho.
        return _reject(
            "riesgo_no_cabe",
            {"lot_max_por_riesgo": lot_hi, "riesgo_min_lot_eur": min_lot * sl_pips * ppl},
        )

    # I2: lote mínimo que enciende el trailing Safe-4 del EA.
    lot_by_lock = _ceil_step(lock_eur / (sl_pips * ppl), min_lot)
    if lot_by_lock > lot_hi:
        return _reject(
            "safe4_no_cabe_en_riesgo",
            {"lot_min_por_lock": lot_by_lock, "lot_max_por_riesgo": lot_hi},
        )

    # I3: el TP debe pagar `need_net` DESPUÉS de costes.
    tried = []
    for tp in tp_candidates_pips:
        tp = float(tp)
        if tp <= cost_pips:
            tried.append({"tp_pips": tp, "motivo": "tp_no_cubre_costes"})
            continue
        lot_by_reward = _ceil_step(need_net / ((tp - cost_pips) * ppl), min_lot)
        lot_lo = max(min_lot, lot_by_lock, lot_by_reward)
        tried.append({"tp_pips": tp, "lot_lo": lot_lo})
        if lot_lo > lot_hi:
            continue

        # El lote final: el que pide la política, corregido a la ventana.
        lots = min(max(float(head_lots), lot_lo), lot_hi)
        risk_eur = lots * sl_pips * ppl
        reward_gross = lots * tp * ppl
        cost_eur = lots * cost_pips * ppl
        reward_net = reward_gross - cost_eur

        # Verificación numérica de los tres invariantes (no se confía en
        # el álgebra: si alguno falla es un error, no un trade).
        if risk_eur > cap_eur + 1e-6:
            raise AssertionError(f"I1 roto: riesgo {risk_eur:.4f} > tope {cap_eur:.4f}")
        if risk_eur < lock_eur - 1e-6:
            raise AssertionError(f"I2 roto: riesgo {risk_eur:.4f} < lock {lock_eur:.4f}")
        if reward_net < need_net - 1e-6:
            raise AssertionError(
                f"I3 roto: beneficio neto {reward_net:.4f} < exigido {need_net:.4f}"
            )

        return TradeContract(
            ok=True, reason="", lots=lots, sl_pips=sl_pips, tp_pips=tp,
            ppl_eur=ppl, risk_eur=risk_eur, reward_gross_eur=reward_gross,
            cost_eur=cost_eur, reward_net_eur=reward_net, lock_eur=lock_eur,
            cap_eur=cap_eur,
            detail={"lot_by_lock": lot_by_lock, "lot_by_reward": lot_by_reward,
                    "lot_hi": lot_hi, "candidatos": tried},
        )

    return _reject("tp_no_paga_bloqueo", {"candidatos": tried, "lot_hi": lot_hi})
