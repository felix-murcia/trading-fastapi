"""Preparación de UNA entrada: geometría, lote y TP bajo el contrato de dinero.

Application layer: el router NO decide dinero. Esta función reúne las piezas
que sí lo deciden y devuelve magnitudes + contrato, sin hacer I/O (la tasa
EURUSD se pasa ya resuelta por el llamador).

Por qué existe (H-I, 2026-10-22): `predict_direction` tenía 1.229 líneas y
dentro vivían la geometría (SL en el suelo/techo), el lote, los candidatos de
TP y el contrato. Ese bloque es una unidad con contrato propio y tests; se
extrae aquí para que el router orqueste y no re-decida.

Invariantes (las verifica `evaluate_trade`, no se repiten aquí):
    I1  riesgo_eur ≤ equity × risk_pct
    I2  riesgo_eur ≥ min_lock_eur (Safe-4)
    I3  beneficio NETO en el TP ≥ max(MIN_WIN_EUR, min_lock_eur)
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import pandas as pd

from config import settings
from core.rules import size_ppo_position
from ml.trading_env_v2 import (
    effective_sl_pips,
    production_sl,
    sl_cap_geom_pips,
    target_level_candidates,
)
from services.trade_contract import (
    TradeContract,
    evaluate_trade,
    pip_value_eur_per_lot,
)


@dataclass(frozen=True)
class TradeSetup:
    """Salida única de la preparación: lo que el router solo puede LEER."""

    sl_geom_pips: float      # lo que se envía al EA (sin buffer)
    sl_pips: float           # distancia REAL (geom + buffer) — sizing/audit
    tp_pips: float
    lots: float              # 0.0 si el contrato rechaza
    risk_eur: float
    head_lots: float         # lote que pidió la política (antes del contrato)
    lock_alive: bool         # Safe-4 puede dispararse antes del TP
    contract: TradeContract
    tp_candidates: tuple[float, ...]
    atr_pips: float          # ATR físico del ciclo (pips) — audit H-B
    sl_cap_geom_pips: float  # tope H-B aplicado al geométrico (pips) — audit

    def audit_fields(self) -> dict[str, float]:
        """Campos H-B para el audit: ATR físico del ciclo y tope geométrico.

        Permiten verificar ex-post la geometría (`sl_pips_geom ==
        sl_cap_geom_pips` cuando el tope muerde; `sl_pips/atr_pips ≈ 1,5`
        salvo el suelo de 10 p geométricos) sin recalcular las velas."""
        return {
            "atr_pips": round(self.atr_pips, 1),
            "sl_cap_geom_pips": round(self.sl_cap_geom_pips, 1),
        }


def prepare_trade(
    *,
    symbol: str,
    price: float,
    equity: float,
    atr: float,
    pip_size: float,
    direction: int,
    floor_below: Optional[float],
    ceiling_above: Optional[float],
    volume_norm: float,
    df: pd.DataFrame,
    risk_pct: float,
    min_lot: float,
    max_lot: float,
    min_lock_eur: float,
    eurusd_price: Optional[float] = None,
) -> TradeSetup:
    """Geometría → lote → contrato, en ese orden (variante B).

    `direction`: +1 LONG, −1 SHORT, 0 sin dirección (el contrato se evalúa
    igual para la traza; el router sólo ejecuta BUY/SELL).
    """
    sl_geom = production_sl(
        atr, pip_size,
        direction=direction,
        floor_below=floor_below or None,
        ceiling_above=ceiling_above or None,
        current_price=price,
    )
    sl_pips = effective_sl_pips(sl_geom)
    atr_pips = (atr * price) / pip_size if price > 0 else 0.0
    cap_geom = sl_cap_geom_pips(atr, pip_size, price)

    ppl = pip_value_eur_per_lot(symbol, price, eurusd_price=eurusd_price)
    head_lots = size_ppo_position(
        volume_norm=volume_norm, sl_pips=sl_pips, equity=equity,
        pip_value=ppl, risk_pct=risk_pct,
        max_lot=max_lot, min_lot=min_lot,
    )

    levels = target_level_candidates(
        df, 1 if direction == 1 else -1,
        atr / pip_size, price, pip_size=pip_size,
    )
    candidates = tuple(abs(float(level) - price) / pip_size for level in levels)
    if not candidates:
        # Fallback declarado: sin nivel utilizable se evalúa 2×SL; el contrato
        # dirá si paga el bloqueo neto de costes y, si no, no hay entrada.
        candidates = (2.0 * sl_pips,)

    if settings.USE_NEW_COMPUTE_POSITION_SIZE:
        # Camino nuevo: caso de uso aislado en src/application/ (paridad byte a byte con legacy)
        from src.application.use_cases.compute_position_size import (
            ComputePositionSize, ComputePositionSizeInput,
        )
        from src.domain.value_objects.thresholds import TradingThresholds
        uc = ComputePositionSize(
            repo=None, clock=None,
            thresholds=TradingThresholds(
                min_win_eur=Decimal("3.0"),          # MIN_WIN_EUR del legacy (core/rules.py)
                safe_lock_eur=Decimal(str(min_lock_eur)),
                cost_buffer_pips=2,                   # EXEC_COST_PIPS = SPREAD_BUFFER_PIPS = 2
            ),
        )
        _result = uc.execute(ComputePositionSizeInput(
            symbol=symbol, price=price, equity=equity, sl_pips=sl_pips,
            head_lots=head_lots, tp_candidates_pips=candidates,
            risk_pct=risk_pct, min_lot=min_lot, max_lot=max_lot,
            eurusd_price=eurusd_price,
        ))
        contract = TradeContract(
            ok=_result.ok, reason=_result.reason, lots=_result.lots,
            sl_pips=_result.sl_pips, tp_pips=_result.tp_pips,
            ppl_eur=_result.ppl_eur, risk_eur=_result.risk_eur,
            reward_gross_eur=_result.reward_gross_eur,
            cost_eur=_result.cost_eur,
            reward_net_eur=_result.reward_net_eur,
            lock_eur=_result.lock_eur, cap_eur=_result.cap_eur,
        )
    else:
        # Camino legacy (byte-idéntico al comportamiento previo)
        contract = evaluate_trade(
            symbol=symbol, price=price, equity=equity, sl_pips=sl_pips,
            head_lots=head_lots, tp_candidates_pips=candidates,
            risk_pct=risk_pct, min_lot=min_lot, max_lot=max_lot,
            min_lock_eur=min_lock_eur, eurusd_price=eurusd_price,
        )
    tp_pips = contract.tp_pips if contract.tp_pips > 0 else candidates[0]
    return TradeSetup(
        sl_geom_pips=sl_geom,
        sl_pips=sl_pips,
        tp_pips=tp_pips,
        lots=contract.lots if contract.ok else 0.0,
        risk_eur=contract.risk_eur,
        head_lots=head_lots,
        lock_alive=bool(contract.ok and contract.risk_eur >= min_lock_eur),
        contract=contract,
        tp_candidates=candidates,
        atr_pips=atr_pips,
        sl_cap_geom_pips=cap_geom if cap_geom is not None else sl_geom,
    )
