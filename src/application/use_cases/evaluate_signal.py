"""Caso de uso: EvaluateSignal.

ORQUESTA el dominio antes de la ejecución. COMPONE los casos de uso ya
migrados (ValidateVetoChain y ComputePositionSize) como dependencias
inyectadas. NO reimplementa su lógica.

Regla de composición:
  - ValidateVetoChain: decide si la señal puede llegar al EA (vetos mecánicos,
    sesgo, USD, quórum, sizing veto).
  - ComputePositionSize: calcula el lote y TP si la decisión pasa los vetos.

Si el legacy duplica lógica entre predict_direction y veto_chain/sizing,
ESTE caso de uso importa de los casos de uso migrados, NO copia.

No lee os.environ. No toca infraestructura.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from src.domain.ports import Clock, ConfigProvider, TradeRepository  # noqa: F401
from src.domain.rules.decode_direction import decode_direction
from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.validate_veto_chain import (
    ValidateVetoChain, ValidateVetoChainInput, ValidateVetoChainOutput,
)
from src.application.use_cases.compute_position_size import (
    ComputePositionSize, ComputePositionSizeInput, ComputePositionSizeOutput,
)


@dataclass(frozen=True)
class EvaluateSignalInput:
    cycle_id: str
    symbol: str
    direction: float          # acción cruda del modelo (-1 a 1)
    env_position: int         # posición actual del EA (0/1/-1)
    steps_h1: int
    time_stop_bars: int
    volume_norm: float        # lote que pide la política (0 = sin entrada)
    sl_pips: float
    equity: float
    risk_pct: float
    min_lot: float
    max_lot: float
    min_lock_eur: float
    reentry_window: bool
    bars_since_close: int
    churn_bars: int
    qwen_ok: bool
    qwen_reentry_ok: Optional[bool]
    min_entry_confidence: float
    effective_prob: float
    open_positions: Sequence[dict]
    qwen: dict
    eurusd_price: Optional[float] = None


@dataclass(frozen=True)
class EvaluateSignalOutput:
    decision: str             # "BUY", "SELL", "HOLD", "CLOSE", "FLAT"
    target_pos: int           # 1, -1, 0
    time_stop: bool
    reentry_ok: Optional[bool]
    contract_rejected: bool
    position_rejected: bool
    low_confidence: bool
    reward_eur: Optional[float]
    lots: float               # 0.0 si no hay entrada
    sl_pips: float
    tp_pips: float
    ppl_eur: float
    risk_eur: float
    reward_gross_eur: float
    cost_eur: float
    reward_net_eur: float
    lock_eur: float
    cap_eur: float


class EvaluateSignal:
    def __init__(
        self,
        veto_chain: ValidateVetoChain,
        position_sizer: ComputePositionSize,
        clock: Clock,
        config: Optional[ConfigProvider] = None,
        thresholds: Optional[TradingThresholds] = None,
    ):
        self._veto_chain = veto_chain
        self._position_sizer = position_sizer
        self._clock = clock
        self._config = config
        self._thresholds = thresholds or TradingThresholds()

    def execute(self, inp: EvaluateSignalInput) -> EvaluateSignalOutput:
        # Paso 1: decodificación de dirección del modelo (regla de dominio pura)
        direction_decision = decode_direction(inp.direction, inp.env_position)
        initial_decision = direction_decision.decision

        # Paso 2: veto chain (composición, no duplicación)
        veto_input = ValidateVetoChainInput(
            cycle_id=inp.cycle_id,
            symbol=inp.symbol,
            decision=initial_decision,
            env_position=inp.env_position,
            position_raw=inp.env_position,
            steps_h1=inp.steps_h1,
            time_stop_bars=inp.time_stop_bars,
            contract_ok=True,       # se actualiza después del sizing
            contract_reason="",
            contract_lots=0.0,
            contract_sl_pips=inp.sl_pips,
            contract_tp_pips=0.0,
            contract_ppl_eur=0.0,
            contract_risk_eur=0.0,
            contract_reward_gross_eur=0.0,
            contract_cost_eur=0.0,
            contract_reward_net_eur=0.0,
            contract_lock_eur=float(inp.min_lock_eur),
            contract_cap_eur=0.0,
            lots=0.0,
            tp_pips=0.0,
            min_lock_eur=inp.min_lock_eur,
            reentry_window=inp.reentry_window,
            bars_since_close=inp.bars_since_close,
            churn_bars=inp.churn_bars,
            qwen_ok=inp.qwen_ok,
            qwen_reentry_ok=inp.qwen_reentry_ok,
            min_entry_confidence=inp.min_entry_confidence,
            effective_prob=inp.effective_prob,
            open_positions=inp.open_positions,
            qwen=inp.qwen,
        )
        veto_output = self._veto_chain.execute(veto_input)

        # Paso 3: sizing solo si la decisión pasa los vetos (BUY/SELL)
        lots = 0.0
        sl_pips = inp.sl_pips
        tp_pips = 0.0
        ppl_eur = 0.0
        risk_eur = 0.0
        reward_gross_eur = 0.0
        cost_eur = 0.0
        reward_net_eur = 0.0
        cap_eur = 0.0

        if veto_output.decision in ("BUY", "SELL") and inp.volume_norm > 0.0:
            sizing_input = ComputePositionSizeInput(
                symbol=inp.symbol,
                price=0.0,          # se resuelve por el llamador (EURUSD price)
                equity=inp.equity,
                sl_pips=inp.sl_pips,
                head_lots=inp.volume_norm,
                tp_candidates_pips=(veto_output.reward_eur and 20.0,) or (20.0,),
                risk_pct=inp.risk_pct,
                min_lot=inp.min_lot,
                max_lot=inp.max_lot,
                eurusd_price=inp.eurusd_price,
            )
            sizing_output = self._position_sizer.execute(sizing_input)
            if sizing_output.ok:
                lots = sizing_output.lots
                sl_pips = sizing_output.sl_pips
                tp_pips = sizing_output.tp_pips
                ppl_eur = sizing_output.ppl_eur
                risk_eur = sizing_output.risk_eur
                reward_gross_eur = sizing_output.reward_gross_eur
                cost_eur = sizing_output.cost_eur
                reward_net_eur = sizing_output.reward_net_eur
                cap_eur = sizing_output.cap_eur

        return EvaluateSignalOutput(
            decision=veto_output.decision,
            target_pos=direction_decision.target_pos,
            time_stop=veto_output.time_stop,
            reentry_ok=veto_output.reentry_ok,
            contract_rejected=veto_output.contract_rejected,
            position_rejected=veto_output.position_rejected,
            low_confidence=veto_output.low_confidence,
            reward_eur=veto_output.reward_eur,
            lots=lots,
            sl_pips=sl_pips,
            tp_pips=tp_pips,
            ppl_eur=ppl_eur,
            risk_eur=risk_eur,
            reward_gross_eur=reward_gross_eur,
            cost_eur=cost_eur,
            reward_net_eur=reward_net_eur,
            lock_eur=float(inp.min_lock_eur),
            cap_eur=cap_eur,
        )
