"""Caso de uso: ComputePositionSize.

Lógica idéntica al legacy `evaluate_trade` (fastapi/services/trade_contract.py),
pero aislada: solo importa de src.domain/. Los valores de umbral se inyectan
vía TradingThresholds; el pip value EUR se deriva de
src.domain.rules.expected_close_eur (misma conversión que el legacy).

No lee os.environ. No toca infraestructura.

TODO(sesionN): ValueError -> DomainError.
El legacy lanza ValueError para equity<=0, sl_pips<=0 y pip value inválido.
Eso se conserva por paridad de comportamiento en Sesión 2. En una sesión
posterior (cuando el flag USE_NEW_COMPUTE_POSITION_SIZE esté validado en
producción) esos ValueError deben convertirse en excepciones de dominio
específicas (InvalidTradeError, InvalidPnLError, ThresholdViolationError).
"""

from dataclasses import dataclass
import math
from typing import Optional, Sequence

from src.domain.ports import Clock, TradeRepository  # noqa: F401  (firma del caso de uso)
from src.domain.value_objects.thresholds import TradingThresholds
from src.domain.rules.expected_close_eur import expected_close_eur


@dataclass(frozen=True)
class ComputePositionSizeInput:
    symbol: str
    price: float
    equity: float
    sl_pips: float
    head_lots: float
    tp_candidates_pips: Sequence[float]
    risk_pct: float
    min_lot: float
    max_lot: float
    eurusd_price: Optional[float] = None


@dataclass(frozen=True)
class ComputePositionSizeOutput:
    ok: bool
    reason: str
    lots: float
    sl_pips: float
    tp_pips: float
    ppl_eur: float
    risk_eur: float
    reward_gross_eur: float
    cost_eur: float
    reward_net_eur: float
    lock_eur: float
    cap_eur: float


class ComputePositionSize:
    def __init__(self, repo: TradeRepository, clock: Clock,
                 thresholds: TradingThresholds):
        self._repo = repo
        self._clock = clock
        self._thresholds = thresholds

    def execute(self, inp: ComputePositionSizeInput) -> ComputePositionSizeOutput:
        if inp.price <= 0.0 or inp.equity <= 0.0 or inp.sl_pips <= 0.0:
            raise ValueError(
                f"ComputePositionSize exige price/equity/sl_pips > 0: "
                f"{inp.price}/{inp.equity}/{inp.sl_pips}"
            )
        if inp.risk_pct <= 0.0 or inp.min_lot <= 0.0 or inp.max_lot < inp.min_lot:
            raise ValueError(
                f"ComputePositionSize exige risk_pct/min_lot > 0 y max_lot >= min_lot: "
                f"{inp.risk_pct}/{inp.min_lot}/{inp.max_lot}"
            )

        ppl = expected_close_eur(
            inp.symbol, 1.0, 1.0, inp.price, eurusd_price=inp.eurusd_price
        )
        if ppl <= 0.0:
            raise ValueError(
                f"ComputePositionSize: pip value EUR inválido para {inp.symbol}: {ppl}"
            )

        cap_eur = inp.equity * inp.risk_pct
        lock_eur = float(self._thresholds.safe_lock_eur)
        need_net = max(float(self._thresholds.min_win_eur), lock_eur)

        # I1: el mayor lote que respeta el tope de riesgo.
        lot_hi = min(inp.max_lot, _floor_step(cap_eur / (inp.sl_pips * ppl), inp.min_lot))

        def _reject(reason: str) -> ComputePositionSizeOutput:
            return ComputePositionSizeOutput(
                ok=False, reason=reason, lots=0.0, sl_pips=inp.sl_pips, tp_pips=0.0,
                ppl_eur=ppl, risk_eur=0.0, reward_gross_eur=0.0, cost_eur=0.0,
                reward_net_eur=0.0, lock_eur=lock_eur, cap_eur=cap_eur,
            )

        if inp.head_lots <= 0.0:
            return _reject("sizing_inviable")
        if lot_hi < inp.min_lot:
            return _reject("riesgo_no_cabe")

        # I2: lote mínimo que enciende el trailing Safe-4 del EA.
        lot_by_lock = _ceil_step(lock_eur / (inp.sl_pips * ppl), inp.min_lot)
        if lot_by_lock > lot_hi:
            return _reject("safe4_no_cabe_en_riesgo")

        # I3: el TP debe pagar `need_net` DESPUÉS de costes.
        cost_pips = float(self._thresholds.cost_buffer_pips)
        tried = []
        for tp in inp.tp_candidates_pips:
            tp = float(tp)
            if tp <= cost_pips:
                tried.append({"tp_pips": tp, "motivo": "tp_no_cubre_costes"})
                continue
            lot_by_reward = _ceil_step(need_net / ((tp - cost_pips) * ppl), inp.min_lot)
            lot_lo = max(inp.min_lot, lot_by_lock, lot_by_reward)
            tried.append({"tp_pips": tp, "lot_lo": lot_lo})
            if lot_lo > lot_hi:
                continue

            lots = min(max(float(inp.head_lots), lot_lo), lot_hi)
            risk_eur = lots * inp.sl_pips * ppl
            reward_gross = lots * tp * ppl
            cost_eur = lots * cost_pips * ppl
            reward_net = reward_gross - cost_eur

            if risk_eur > cap_eur + 1e-6:
                raise AssertionError(f"I1 roto: riesgo {risk_eur:.4f} > tope {cap_eur:.4f}")
            if risk_eur < lock_eur - 1e-6:
                raise AssertionError(f"I2 roto: riesgo {risk_eur:.4f} < lock {lock_eur:.4f}")
            if reward_net < need_net - 1e-6:
                raise AssertionError(
                    f"I3 roto: beneficio neto {reward_net:.4f} < exigido {need_net:.4f}"
                )

            return ComputePositionSizeOutput(
                ok=True, reason="", lots=lots, sl_pips=inp.sl_pips, tp_pips=tp,
                ppl_eur=ppl, risk_eur=risk_eur, reward_gross_eur=reward_gross,
                cost_eur=cost_eur, reward_net_eur=reward_net, lock_eur=lock_eur,
                cap_eur=cap_eur,
            )

        return _reject("tp_no_paga_bloqueo")


def _floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-6) * step, 2)


def _ceil_step(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-6) * step, 2)
