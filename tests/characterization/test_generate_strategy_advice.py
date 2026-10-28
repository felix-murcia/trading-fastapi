"""Test de caracterización: captura el comportamiento ACTUAL del legacy
para GenerateStrategyAdvice (migra la parte de Qwen + vetos de
predict_direction, routers/ai.py:818).

Documenta lo que el backend hace HOY al generar un consejo de estrategia:
  - Llama al LLM (Qwen) o usa degraded/abstained/skip
  - Aplica bias_veto (reversión por sesgo contrario extremo)
  - Aplica quórum (mercado lateral sin consenso → HOLD)
  - Aplica sizing veto (lote 0 → HOLD)
  - Devuelve decisión final + metadatos LLM (bias, effective_prob, quality,
    regime, confidence_modifier)

NO se corrige nada del legacy. Si hay un comportamiento raro, se captura
tal cual con TODO.

Los tests usan FakeLLMClient (canned responses) + FakeEvaluateSignal.
Nunca Qwen real.
"""

import pytest
from datetime import datetime
from decimal import Decimal

from src.domain.value_objects.thresholds import TradingThresholds
from tests.fakes.fake_llm_client import (
    FakeLLMClient, RESPONSE_BUY, RESPONSE_SELL, RESPONSE_HOLD,
    RESPONSE_EMPTY, RESPONSE_MISSING_FIELDS, RateLimitError,
)
from tests.fakes.fake_clock import FakeClock


# ─── Fake EvaluateSignal (composición, no duplicación) ───────────────────

class FakeEvaluateSignalOutput:
    def __init__(self, decision="BUY", lots=0.1, sl_pips=10.0, tp_pips=20.0,
                 time_stop=False, reentry_ok=None, contract_rejected=False,
                 position_rejected=False, low_confidence=False,
                 reward_eur=None, risk_eur=5.0, target_pos=1,
                 ppl_eur=0.0, reward_gross_eur=0.0, cost_eur=0.0,
                 reward_net_eur=0.0, lock_eur=4.0, cap_eur=100.0):
        self.decision = decision
        self.lots = lots
        self.sl_pips = sl_pips
        self.tp_pips = tp_pips
        self.time_stop = time_stop
        self.reentry_ok = reentry_ok
        self.contract_rejected = contract_rejected
        self.position_rejected = position_rejected
        self.low_confidence = low_confidence
        self.reward_eur = reward_eur
        self.risk_eur = risk_eur
        self.target_pos = target_pos
        self.ppl_eur = ppl_eur
        self.reward_gross_eur = reward_gross_eur
        self.cost_eur = cost_eur
        self.reward_net_eur = reward_net_eur
        self.lock_eur = lock_eur
        self.cap_eur = cap_eur


class FakeEvaluateSignal:
    """Fake que devuelve una decisión base configurable (sin tocar legacy)."""

    def __init__(self, output: FakeEvaluateSignalOutput):
        self._output = output
        self.called = False
        self.input_received = None

    def execute(self, inp):
        self.called = True
        self.input_received = inp
        # Simula el veto qwen_degradado de ValidateVetoChain:
        # si Qwen está degradado y la dirección es BUY/SELL → HOLD
        fake_decision = "BUY" if inp.direction > 0 else ("SELL" if inp.direction < 0 else "HOLD")
        if not inp.qwen_ok and fake_decision in ("BUY", "SELL"):
            decision = "HOLD"
        else:
            decision = self._output.decision
        return FakeEvaluateSignalOutput(
            decision=decision,
            lots=self._output.lots,
            sl_pips=self._output.sl_pips,
            tp_pips=self._output.tp_pips,
            time_stop=self._output.time_stop,
            reentry_ok=self._output.reentry_ok,
            contract_rejected=self._output.contract_rejected,
            position_rejected=self._output.position_rejected,
            low_confidence=self._output.low_confidence,
            reward_eur=self._output.reward_eur,
            risk_eur=self._output.risk_eur,
            target_pos=self._output.target_pos,
            ppl_eur=self._output.ppl_eur,
            reward_gross_eur=self._output.reward_gross_eur,
            cost_eur=self._output.cost_eur,
            reward_net_eur=self._output.reward_net_eur,
            lock_eur=self._output.lock_eur,
            cap_eur=self._output.cap_eur,
        )


# ─── Casos de caracterización (≥12) ──────────────────────────────────────

# (name, llm_response, llm_side_effect, eval_decision, bias_authorized,
#  quorum_lateral, quorum_consensus, expected_final_decision,
#  expected_llm_bias, expected_qwen_degraded, expected_qwen_abstained,
#  expected_bias_veto_fired, expected_quorum_rejected, expected_quality_score)
CAPTURADO_LEGACY = [
    # 1. Respuesta BUY válida → consejo BUY con confidence y metadata
    ("1_buy_valido", RESPONSE_BUY, None, "BUY", False, False, True,
     "BUY", "BULLISH", False, False, False, False, 8.0),
    # 2. Respuesta SELL válida → consejo SELL
    ("2_sell_valido", RESPONSE_SELL, None, "SELL", False, False, True,
     "SELL", "BEARISH", False, False, False, False, 7.0),
    # 3. Respuesta HOLD válida → consejo HOLD
    ("3_hold_valido", RESPONSE_HOLD, None, "HOLD", False, False, True,
     "HOLD", "NEUTRAL", False, False, False, False, 5.0),
    # 4. Respuesta vacía → abstained (ok=True, no veto qwen_degradado; decisión = eval)
    ("4_respuesta_vacia", RESPONSE_EMPTY, None, "BUY", False, False, True,
     "BUY", "NEUTRAL", False, True, False, False, 5.0),
    # 5. Respuesta JSON malformado → fallback degradado (ok=False, quality 0.0)
    ("5_json_malformado", None, ValueError("parse error"), "BUY", False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    # 6. Timeout del LLM → fallback degradado
    ("6_timeout", None, TimeoutError("tiempo agotado"), "BUY", False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    # 7. Rate limit del LLM → fallback degradado
    ("7_rate_limit", None, RateLimitError("cupo agotado"), "BUY", False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    # 8. Respuesta con bias_prob fuera de rango → clamp (legacy clampa a [0,1])
    ("8_conf_fuera_rango", {"ok": True, "abstained": False, "quality": 6.0,
                            "reason": "x", "bias": "BULLISH", "bias_prob": 1.5,
                            "confidence_modifier": 1.0, "effective_prob": 0.6,
                            "regime": "TRENDING", "reentry_ok": True},
     None, "BUY", False, False, True, "BUY", "BULLISH", False, False, False, False, 6.0),
    # 9. Respuesta con campos faltantes → defaults (bias_prob=0.5, conf_mod=1.0)
    ("9_campos_faltantes", RESPONSE_MISSING_FIELDS, None, "BUY", False, False, True,
     "BUY", "BULLISH", False, False, False, False, 6.0),
    # 10. Mismo cycle_id dos veces → idempotencia (mismo output)
    ("10_idempotencia", RESPONSE_BUY, None, "BUY", False, False, True,
     "BUY", "BULLISH", False, False, False, False, 8.0),
    # 11. Composición: EvaluateSignal devuelve HOLD → decisión final HOLD,
    #     pero el LLM todavía respondió (su sesgo se reporta igual)
    ("11_eval_hold", RESPONSE_BUY, None, "HOLD", False, False, True,
     "HOLD", "BULLISH", False, False, False, False, 8.0),
    # 12. Qwen degradado → veto qwen_degradado (BUY/SELL → HOLD)
    ("12_qwen_degradado", None, None, "BUY", False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    # 13. Bias veto: BUY + sesgo BEARISH extremo (prob ≤ 0.30) + autorizado → HOLD
    ("13_bias_veto_buy_revertido", RESPONSE_SELL, None, "BUY", True, False, True,
     "HOLD", "BEARISH", False, False, True, False, 7.0),
    # 14. Quórum: mercado lateral sin consenso → HOLD
    ("14_quorum_sin_consenso", RESPONSE_BUY, None, "BUY", False, True, False,
     "HOLD", "BULLISH", False, False, False, True, 8.0),
]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "name,llm_response,llm_side_effect,eval_decision,bias_authorized,"
    "quorum_lateral,quorum_consensus,expected_final_decision,expected_llm_bias,"
    "expected_qwen_degraded,expected_qwen_abstained,expected_bias_veto_fired,"
    "expected_quorum_rejected,expected_quality_score",
    CAPTURADO_LEGACY,
)
def test_legacy_generate_strategy_advice_produces_captured_output(
    name, llm_response, llm_side_effect, eval_decision, bias_authorized,
    quorum_lateral, quorum_consensus, expected_final_decision,
    expected_llm_bias, expected_qwen_degraded, expected_qwen_abstained,
    expected_bias_veto_fired, expected_quorum_rejected, expected_quality_score,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    from src.application.use_cases.generate_strategy_advice import (
        GenerateStrategyAdvice, GenerateStrategyAdviceInput,
    )
    llm = FakeLLMClient(response=llm_response, side_effect=llm_side_effect)
    evaluate = FakeEvaluateSignal(
        FakeEvaluateSignalOutput(decision=eval_decision, lots=0.1, sl_pips=10.0,
                                 tp_pips=20.0, risk_eur=5.0,
                                 target_pos=1 if eval_decision == "BUY" else (-1 if eval_decision == "SELL" else 0))
    )
    clock = FakeClock()
    thresholds = TradingThresholds(min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2)
    uc = GenerateStrategyAdvice(
        llm=llm, evaluate=evaluate, clock=clock, thresholds=thresholds,
    )

    inp = GenerateStrategyAdviceInput(
        cycle_id=f"cycle_{name}",
        symbol="EURUSD",
        direction=0.8 if eval_decision == "BUY" else (-0.8 if eval_decision == "SELL" else 0.0),
        env_position=0,
        steps_h1=10,
        time_stop_bars=120,
        volume_norm=0.1,
        sl_pips=10.0,
        equity=10000.0,
        risk_pct=0.01,
        min_lot=0.01,
        max_lot=1.0,
        min_lock_eur=4.0,
        reentry_window=False,
        bars_since_close=5,
        churn_bars=2,
        min_entry_confidence=0.0,
        effective_prob=0.7,
        open_positions=[],
        bias_authorized=bias_authorized,
        quorum_lateral=quorum_lateral,
        quorum_consensus=quorum_consensus,
    )
    out = uc.execute(inp)

    assert out.decision == expected_final_decision, f'{name}: decision={out.decision} != {expected_final_decision}'
    assert out.llm_bias == expected_llm_bias, f'{name}: llm_bias={out.llm_bias} != {expected_llm_bias}'
    assert out.qwen_degraded == expected_qwen_degraded, f'{name}: qwen_degraded={out.qwen_degraded} != {expected_qwen_degraded}'
    assert out.qwen_abstained == expected_qwen_abstained, f'{name}: qwen_abstained={out.qwen_abstained} != {expected_qwen_abstained}'
    assert out.bias_veto_fired == expected_bias_veto_fired, f'{name}: bias_veto_fired={out.bias_veto_fired} != {expected_bias_veto_fired}'
    assert out.quorum_rejected == expected_quorum_rejected, f'{name}: quorum_rejected={out.quorum_rejected} != {expected_quorum_rejected}'
    assert abs(out.quality_score - expected_quality_score) < 0.01, f'{name}: quality_score={out.quality_score} != {expected_quality_score}'


@pytest.mark.characterization
def test_generate_strategy_advice_idempotencia():
    """El mismo cycle_id ejecutado dos veces produce el mismo output."""
    from src.application.use_cases.generate_strategy_advice import (
        GenerateStrategyAdvice, GenerateStrategyAdviceInput,
    )
    llm = FakeLLMClient(response=RESPONSE_BUY)
    evaluate = FakeEvaluateSignal(
        FakeEvaluateSignalOutput(decision="BUY", lots=0.1, sl_pips=10.0,
                                 tp_pips=20.0, risk_eur=5.0)
    )
    clock = FakeClock()
    thresholds = TradingThresholds(min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2)
    uc = GenerateStrategyAdvice(
        llm=llm, evaluate=evaluate, clock=clock, thresholds=thresholds,
    )

    inp = GenerateStrategyAdviceInput(
        cycle_id="cycle_idempotencia",
        symbol="EURUSD",
        direction=0.8,
        env_position=0,
        steps_h1=10,
        time_stop_bars=120,
        volume_norm=0.1,
        sl_pips=10.0,
        equity=10000.0,
        risk_pct=0.01,
        min_lot=0.01,
        max_lot=1.0,
        min_lock_eur=4.0,
        reentry_window=False,
        bars_since_close=5,
        churn_bars=2,
        min_entry_confidence=0.0,
        effective_prob=0.7,
        open_positions=[],
    )
    out1 = uc.execute(inp)
    out2 = uc.execute(inp)

    assert out1.decision == out2.decision
    assert out1.llm_bias == out2.llm_bias
    assert out1.quality_score == out2.quality_score
    assert out1.effective_prob == out2.effective_prob
    assert len(llm.calls) == 2  # LLM se consulta en ambos ciclos (no se cachea en el caso de uso)


@pytest.mark.characterization
def test_new_use_case_exists():
    """RED gate: GenerateStrategyAdvice debe existir tras implementar."""
    from src.application.use_cases.generate_strategy_advice import (
        GenerateStrategyAdvice,  # noqa: F401
    )
    assert GenerateStrategyAdvice is not None
