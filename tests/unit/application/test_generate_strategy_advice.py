"""Tests unitarios de GenerateStrategyAdvice con fakes en memoria.

VERIFICA COMPOSICIÓN:
  - GenerateStrategyAdvice inyecta LLMClient + EvaluateSignal + Clock +
    TradingThresholds como dependencias.
  - NO reimplementa lógica de dominio (usa EvaluateSignal directamente).
  - El LLM es FakeLLMClient (canned responses, sin red).

CASOS (≥14):
  - Respuesta BUY válida → consejo BUY con confidence y metadata
  - Respuesta SELL válida → consejo SELL
  - Respuesta HOLD válida → consejo HOLD
  - Respuesta vacía → abstained (calidad 5.0, bias NEUTRAL)
  - Respuesta JSON malformado → degradado (ok=False, quality 0.0)
  - Timeout del LLM → degradado
  - Rate limit del LLM → degradado
  - bias_prob fuera de rango → clamp
  - Campos faltantes → defaults
  - Idempotencia (mismo input → mismo output)
  - EvaluateSignal HOLD → decisión final HOLD
  - Qwen degradado → veto qwen_degradado (BUY/SELL → HOLD)
  - Bias veto (BUY + BEARISH extremo + autorizado → HOLD)
  - Quórum (lateral sin consenso → HOLD)
"""

import pytest
from src.domain.value_objects.thresholds import TradingThresholds
from tests.fakes.fake_llm_client import (
    FakeLLMClient, RESPONSE_BUY, RESPONSE_SELL, RESPONSE_HOLD,
    RESPONSE_EMPTY, RESPONSE_MISSING_FIELDS, RateLimitError,
)
from tests.fakes.fake_clock import FakeClock


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
    def __init__(self, output: FakeEvaluateSignalOutput):
        self._output = output
        self.called = False
        self.input_received = None

    def execute(self, inp):
        self.called = True
        self.input_received = inp
        # Simula veto qwen_degradado de ValidateVetoChain
        if not inp.qwen_ok and inp.direction in (0.8, -0.8):
            decision = "HOLD"
        else:
            decision = self._output.decision
        return FakeEvaluateSignalOutput(
            decision=decision, lots=self._output.lots, sl_pips=self._output.sl_pips,
            tp_pips=self._output.tp_pips, time_stop=self._output.time_stop,
            reentry_ok=self._output.reentry_ok,
            contract_rejected=self._output.contract_rejected,
            position_rejected=self._output.position_rejected,
            low_confidence=self._output.low_confidence,
            reward_eur=self._output.reward_eur, risk_eur=self._output.risk_eur,
            target_pos=self._output.target_pos, ppl_eur=self._output.ppl_eur,
            reward_gross_eur=self._output.reward_gross_eur,
            cost_eur=self._output.cost_eur,
            reward_net_eur=self._output.reward_net_eur,
            lock_eur=self._output.lock_eur, cap_eur=self._output.cap_eur,
        )


CASOS = [
    # (name, llm_response, llm_side_effect, eval_direction, bias_authorized,
    #  quorum_lateral, quorum_consensus, expected_decision, expected_llm_bias,
    #  expected_qwen_degraded, expected_qwen_abstained, expected_bias_veto,
    #  expected_quorum_rejected, expected_quality)
    ("1_buy_valido", RESPONSE_BUY, None, 0.8, False, False, True,
     "BUY", "BULLISH", False, False, False, False, 8.0),
    ("2_sell_valido", RESPONSE_SELL, None, -0.8, False, False, True,
     "SELL", "BEARISH", False, False, False, False, 7.0),
    ("3_hold_valido", RESPONSE_HOLD, None, 0.0, False, False, True,
     "HOLD", "NEUTRAL", False, False, False, False, 5.0),
    ("4_respuesta_vacia", RESPONSE_EMPTY, None, 0.8, False, False, True,
     "BUY", "NEUTRAL", False, True, False, False, 5.0),
    ("5_json_malformado", None, ValueError("parse"), 0.8, False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    ("6_timeout", None, TimeoutError("tiempo"), 0.8, False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    ("7_rate_limit", None, RateLimitError("cupo"), 0.8, False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    ("8_conf_fuera_rango", {"ok": True, "abstained": False, "quality": 6.0,
                            "reason": "x", "bias": "BULLISH", "bias_prob": 1.5,
                            "confidence_modifier": 1.0, "effective_prob": 0.6,
                            "regime": "TRENDING", "reentry_ok": True},
     None, 0.8, False, False, True, "BUY", "BULLISH", False, False, False, False, 6.0),
    ("9_campos_faltantes", RESPONSE_MISSING_FIELDS, None, 0.8, False, False, True,
     "BUY", "BULLISH", False, False, False, False, 6.0),
    ("10_idempotencia", RESPONSE_BUY, None, 0.8, False, False, True,
     "BUY", "BULLISH", False, False, False, False, 8.0),
    ("11_eval_hold", RESPONSE_BUY, None, 0.0, False, False, True,
     "HOLD", "BULLISH", False, False, False, False, 8.0),
    ("12_qwen_degradado", None, None, 0.8, False, False, True,
     "HOLD", "NEUTRAL", True, False, False, False, 0.0),
    ("13_bias_veto_buy_revertido", RESPONSE_SELL, None, 0.8, True, False, True,
     "HOLD", "BEARISH", False, False, True, False, 7.0),
    ("14_quorum_sin_consenso", RESPONSE_BUY, None, 0.8, False, True, False,
     "HOLD", "BULLISH", False, False, False, True, 8.0),
]


def _ejecutar_uc(name, llm_response, llm_side_effect, eval_direction,
                 bias_authorized, quorum_lateral, quorum_consensus,
                 expected_decision, expected_llm_bias, expected_qwen_degraded,
                 expected_qwen_abstained, expected_bias_veto,
                 expected_quorum_rejected, expected_quality):
    from src.application.use_cases.generate_strategy_advice import (
        GenerateStrategyAdvice, GenerateStrategyAdviceInput,
    )
    llm = FakeLLMClient(response=llm_response, side_effect=llm_side_effect)
    evaluate = FakeEvaluateSignal(
        FakeEvaluateSignalOutput(decision=("BUY" if eval_direction > 0
                                          else ("SELL" if eval_direction < 0
                                                else "HOLD")),
                                 target_pos=1 if eval_direction > 0
                                   else (-1 if eval_direction < 0 else 0),
                                 lots=0.1, sl_pips=10.0, tp_pips=20.0,
                                 risk_eur=5.0)
    )
    clock = FakeClock()
    thresholds = TradingThresholds(min_win_eur=3.0, safe_lock_eur=4.0,
                                   cost_buffer_pips=2)
    uc = GenerateStrategyAdvice(
        llm=llm, evaluate=evaluate, clock=clock, thresholds=thresholds,
    )
    inp = GenerateStrategyAdviceInput(
        cycle_id=f"test_{name}",
        symbol="EURUSD",
        direction=eval_direction,
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
    return uc.execute(inp), llm, evaluate


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,llm_response,llm_side_effect,eval_direction,bias_authorized,"
    "quorum_lateral,quorum_consensus,expected_decision,expected_llm_bias,"
    "expected_qwen_degraded,expected_qwen_abstained,expected_bias_veto,"
    "expected_quorum_rejected,expected_quality",
    CASOS,
)
def test_generate_strategy_advice_composes_llm_and_eval(
    name, llm_response, llm_side_effect, eval_direction, bias_authorized,
    quorum_lateral, quorum_consensus, expected_decision, expected_llm_bias,
    expected_qwen_degraded, expected_qwen_abstained, expected_bias_veto,
    expected_quorum_rejected, expected_quality,
):
    """GenerateStrategyAdvice compone LLM + EvaluateSignal, sin red."""
    out, llm, evaluate = _ejecutar_uc(
        name, llm_response, llm_side_effect, eval_direction,
        bias_authorized, quorum_lateral, quorum_consensus,
        expected_decision, expected_llm_bias, expected_qwen_degraded,
        expected_qwen_abstained, expected_bias_veto,
        expected_quorum_rejected, expected_quality,
    )
    assert out.decision == expected_decision, \
        f'{name}: decision={out.decision} != {expected_decision}'
    assert out.llm_bias == expected_llm_bias, \
        f'{name}: llm_bias={out.llm_bias} != {expected_llm_bias}'
    assert out.qwen_degraded == expected_qwen_degraded, \
        f'{name}: qwen_degraded={out.qwen_degraded} != {expected_qwen_degraded}'
    assert out.qwen_abstained == expected_qwen_abstained, \
        f'{name}: qwen_abstained={out.qwen_abstained} != {expected_qwen_abstained}'
    assert out.bias_veto_fired == expected_bias_veto, \
        f'{name}: bias_veto_fired={out.bias_veto_fired} != {expected_bias_veto}'
    assert out.quorum_rejected == expected_quorum_rejected, \
        f'{name}: quorum_rejected={out.quorum_rejected} != {expected_quorum_rejected}'
    assert abs(out.quality_score - expected_quality) < 0.01, \
        f'{name}: quality_score={out.quality_score} != {expected_quality}'
    assert evaluate.called, f'{name}: EvaluateSignal no fue llamado'
    # CMD9: el fake no toca red
    assert len(llm.calls) >= 1, f'{name}: LLM no fue consultado'


def test_generate_strategy_advice_idempotencia():
    """Mismo input → mismo output (idempotencia)."""
    from src.application.use_cases.generate_strategy_advice import (
        GenerateStrategyAdvice, GenerateStrategyAdviceInput,
    )
    llm = FakeLLMClient(response=RESPONSE_BUY)
    evaluate = FakeEvaluateSignal(
        FakeEvaluateSignalOutput(decision="BUY", lots=0.1, sl_pips=10.0,
                                 tp_pips=20.0, risk_eur=5.0)
    )
    clock = FakeClock()
    thresholds = TradingThresholds(min_win_eur=3.0, safe_lock_eur=4.0,
                                   cost_buffer_pips=2)
    uc = GenerateStrategyAdvice(
        llm=llm, evaluate=evaluate, clock=clock, thresholds=thresholds,
    )
    inp = GenerateStrategyAdviceInput(
        cycle_id="cycle_idem", symbol="EURUSD", direction=0.8,
        env_position=0, steps_h1=10, time_stop_bars=120, volume_norm=0.1,
        sl_pips=10.0, equity=10000.0, risk_pct=0.01, min_lot=0.01,
        max_lot=1.0, min_lock_eur=4.0, reentry_window=False,
        bars_since_close=5, churn_bars=2, min_entry_confidence=0.0,
        effective_prob=0.7, open_positions=[],
    )
    out1 = uc.execute(inp)
    out2 = uc.execute(inp)
    assert out1.decision == out2.decision
    assert out1.quality_score == out2.quality_score
    assert out1.effective_prob == out2.effective_prob


def test_generate_strategy_advice_uses_domain_only():
    """CMD2: solo imports de src.domain/."""
    import src.application.use_cases.generate_strategy_advice as mod
    source = open(mod.__file__).read()
    forbidden = ["import asyncpg", "import httpx", "import MetaTrader5",
                 "from fastapi", "from sqlalchemy", "import openai",
                 "import qwen", "import anthropic", "import requests"]
    for word in forbidden:
        assert word not in source, f'CMD2 falló: import prohibido: {word}'


def test_generate_strategy_advice_composes_evaluate_signal():
    """CMD10: GenerateStrategyAdvice compone EvaluateSignal (>= 1 referencia)."""
    import src.application.use_cases.generate_strategy_advice as mod
    source = open(mod.__file__).read()
    assert source.count("EvaluateSignal") >= 1, \
        f'CMD10 falló: solo {source.count("EvaluateSignal")} referencias'
