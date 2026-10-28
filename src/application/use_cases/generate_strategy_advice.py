"""Caso de uso: GenerateStrategyAdvice.

COMPONE EvaluateSignal (caso de uso migrado) + LLMClient (puerto de dominio)
+ bias_veto + quórum. NO reimplementa lógica de EvaluateSignal, ni de
ValidateVetoChain, ni de ComputePositionSize.

Flujo (paridad con predict_direction, routers/ai.py:818):
  1. LLMClient.query → qwen dict (bias, effective_prob, quality, regime, ...)
  2. EvaluateSignal → decisión base, lote, SL/TP, vetos mecánicos
  3. bias_veto (sesgo extremo contrario + autoridad) → HOLD si aplica
  4. quórum (mercado lateral sin consenso) → HOLD si aplica
  5. Devolver decisión final + metadatos LLM

No lee os.environ. No toca infraestructura.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from src.domain.ports import AuditLogger, Clock, LLMClient  # noqa: F401
from src.domain.value_objects.thresholds import TradingThresholds  # noqa: F401
from src.application.use_cases.evaluate_signal import (
    EvaluateSignal, EvaluateSignalInput, EvaluateSignalOutput,
)


@dataclass(frozen=True)
class GenerateStrategyAdviceInput:
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
    min_entry_confidence: float
    effective_prob: float     # probabilidad cruda del PPO (pre-LLM)
    open_positions: Sequence[dict]
    eurusd_price: Optional[float] = None
    bias_authorized: bool = False   # ¿el LLM tiene autoridad para veto direccional?
    quorum_lateral: bool = False    # ¿mercado lateral detectado?
    quorum_consensus: bool = True   # ¿hay consenso en el quórum?
    qwen: Optional[dict] = None     # respuesta LLM ya calculada (si None, se consulta al LLM)


@dataclass(frozen=True)
class GenerateStrategyAdviceOutput:
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
    # Metadatos LLM
    llm_bias: str             # "BULLISH", "BEARISH", "NEUTRAL"
    llm_bias_prob: float      # 0-1
    quality_score: float      # 0-10
    quality_reason: str
    confidence_modifier: float
    effective_prob: float     # post-LLM (clamp a [0,1])
    regime: str               # "TRENDING", "RANGING", "VOLATILE", "BREAKOUT"
    qwen_ok: bool
    qwen_abstained: bool
    qwen_degraded: bool
    qwen_skipped: bool
    bias_veto_fired: bool
    quorum_rejected: bool


class GenerateStrategyAdvice:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        evaluate: Optional[EvaluateSignal] = None,
        clock: Optional[Clock] = None,
        audit: Optional[AuditLogger] = None,
        thresholds: Optional[TradingThresholds] = None,
    ):
        self._llm = llm
        self._evaluate = evaluate
        self._clock = clock
        self._audit = audit
        self._thresholds = thresholds or TradingThresholds()

    def execute(self, inp: GenerateStrategyAdviceInput) -> GenerateStrategyAdviceOutput:
        # Paso 1: consulta al LLM (Qwen). Si ya se pasó qwen (router), se reusa;
        # si no, se consulta al LLM. Cualquier fallo → degradado (paridad con
        # query_qwen_unified que captura connection/parse/HTTP errores).
        if inp.qwen is not None:
            qwen = inp.qwen
        elif self._llm is not None:
            try:
                qwen = self._llm.query(
                    symbol=inp.symbol,
                    decision="BUY" if inp.direction > 0 else ("SELL" if inp.direction < 0 else "HOLD"),
                    ml_prob=inp.effective_prob,
                    context={
                        "symbol": inp.symbol,
                        "env_position": inp.env_position,
                        "steps_h1": inp.steps_h1,
                        "equity": inp.equity,
                        "reentry_window": inp.reentry_window,
                        "bars_since_close": inp.bars_since_close,
                    },
                )
            except Exception:
                qwen = {
                    "ok": False,
                    "abstained": False,
                    "quality": 0.0,
                    "reason": "modelo no disponible",
                    "bias": "NEUTRAL",
                    "bias_prob": 0.5,
                    "confidence_modifier": 1.0,
                    "effective_prob": inp.effective_prob,
                    "regime": "UNKNOWN",
                }
        else:
            qwen = {
                "ok": False,
                "abstained": False,
                "quality": 0.0,
                "reason": "modelo no disponible",
                "bias": "NEUTRAL",
                "bias_prob": 0.5,
                "confidence_modifier": 1.0,
                "effective_prob": inp.effective_prob,
                "regime": "UNKNOWN",
            }
        qwen_ok = bool(qwen.get("ok", True))
        qwen_abstained = bool(qwen.get("abstained", False))
        qwen_degraded = not qwen_ok
        llm_bias = str(qwen.get("bias", "NEUTRAL")).upper()
        llm_bias_prob = float(qwen.get("bias_prob", 0.5))
        quality_score = float(qwen.get("quality", 0.0))
        quality_reason = str(qwen.get("reason", ""))
        confidence_modifier = float(qwen.get("confidence_modifier", 1.0))
        effective_prob = float(qwen.get("effective_prob", inp.effective_prob))
        regime = str(qwen.get("regime", "UNKNOWN"))

        # Paso 2: EvaluateSignal (composición, incluye veto chain + sizing).
        # Si evaluate es None (punto de inyección en router sin cadena completa),
        # se usa una decisión base HOLD.
        if self._evaluate is not None:
            eval_input = EvaluateSignalInput(
                cycle_id=inp.cycle_id,
                symbol=inp.symbol,
                direction=inp.direction,
                env_position=inp.env_position,
                steps_h1=inp.steps_h1,
                time_stop_bars=inp.time_stop_bars,
                volume_norm=inp.volume_norm,
                sl_pips=inp.sl_pips,
                equity=inp.equity,
                risk_pct=inp.risk_pct,
                min_lot=inp.min_lot,
                max_lot=inp.max_lot,
                min_lock_eur=inp.min_lock_eur,
                reentry_window=inp.reentry_window,
                bars_since_close=inp.bars_since_close,
                churn_bars=inp.churn_bars,
                qwen_ok=qwen_ok,
                qwen_reentry_ok=None,
                min_entry_confidence=inp.min_entry_confidence,
                effective_prob=effective_prob,
                open_positions=inp.open_positions,
                qwen=qwen,
                eurusd_price=inp.eurusd_price,
            )
            eval_output = self._evaluate.execute(eval_input)
        else:
            eval_output = _null_eval_output(inp.direction)

        # Paso 3: bias_veto (sesgo extremo contrario + autoridad)
        # Paridad con legacy: bias_veto_pending + apply_bias_veto
        bias_veto_fired = False
        final_decision = eval_output.decision
        if final_decision in ("BUY", "SELL") and inp.bias_authorized:
            bias_reverts = False
            if final_decision == "BUY" and llm_bias == "BEARISH" and llm_bias_prob <= 0.3:
                bias_reverts = True
            elif final_decision == "SELL" and llm_bias == "BULLISH" and llm_bias_prob >= 0.7:
                bias_reverts = True
            if bias_reverts:
                final_decision = "HOLD"
                bias_veto_fired = True

        # Paso 4: quórum (mercado lateral sin consenso)
        quorum_rejected = False
        if final_decision in ("BUY", "SELL") and inp.quorum_lateral and not inp.quorum_consensus:
            final_decision = "HOLD"
            quorum_rejected = True

        # Auditar ejecución del caso de uso nuevo (evento distinto del legacy
        # para poder contar ejecuciones reales en producción antes de graduar
        # el flag USE_NEW_GENERATE_STRATEGY_ADVICE en 7c-2).
        if self._audit is not None:
            try:
                self._audit.log("generate_strategy_advice_new", {
                    "cycle_id": inp.cycle_id,
                    "symbol": inp.symbol,
                    "decision": final_decision,
                    "qwen_ok": qwen_ok,
                    "bias_veto_fired": bias_veto_fired,
                    "quorum_rejected": quorum_rejected,
                })
            except Exception:
                # No romper el flujo si el audit falla.
                pass

        return GenerateStrategyAdviceOutput(
            decision=final_decision,
            target_pos=eval_output.target_pos,
            time_stop=eval_output.time_stop,
            reentry_ok=eval_output.reentry_ok,
            contract_rejected=eval_output.contract_rejected,
            position_rejected=eval_output.position_rejected,
            low_confidence=eval_output.low_confidence,
            reward_eur=eval_output.reward_eur,
            lots=eval_output.lots,
            sl_pips=eval_output.sl_pips,
            tp_pips=eval_output.tp_pips,
            ppl_eur=eval_output.ppl_eur,
            risk_eur=eval_output.risk_eur,
            reward_gross_eur=eval_output.reward_gross_eur,
            cost_eur=eval_output.cost_eur,
            reward_net_eur=eval_output.reward_net_eur,
            lock_eur=eval_output.lock_eur,
            cap_eur=eval_output.cap_eur,
            llm_bias=llm_bias,
            llm_bias_prob=llm_bias_prob,
            quality_score=quality_score,
            quality_reason=quality_reason,
            confidence_modifier=confidence_modifier,
            effective_prob=effective_prob,
            regime=regime,
            qwen_ok=qwen_ok,
            qwen_abstained=qwen_abstained,
            qwen_degraded=qwen_degraded,
            qwen_skipped=False,
            bias_veto_fired=bias_veto_fired,
            quorum_rejected=quorum_rejected,
        )


def _null_eval_output(direction: float) -> EvaluateSignalOutput:
    """Decisión base neutra cuando EvaluateSignal no está inyectada (router)."""
    decision = "BUY" if direction > 0 else ("SELL" if direction < 0 else "HOLD")
    target_pos = 1 if direction > 0 else (-1 if direction < 0 else 0)
    return EvaluateSignalOutput(
        decision=decision, target_pos=target_pos, time_stop=False, reentry_ok=None,
        contract_rejected=False, position_rejected=False, low_confidence=False,
        reward_eur=None, lots=0.0, sl_pips=0.0, tp_pips=0.0, ppl_eur=0.0,
        risk_eur=0.0, reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
        lock_eur=0.0, cap_eur=0.0,
    )
