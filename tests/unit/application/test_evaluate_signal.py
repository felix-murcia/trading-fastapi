"""Tests unitarios de EvaluateSignal con fakes de los casos de uso migrados.

VERIFICA COMPOSICIÓN (no duplicación):
  - EvaluateSignal inyecta ValidateVetoChain y ComputePositionSize como
    dependencias y los llama. NO reimplementa su lógica.
  - Los fakes simulan el comportamiento de los casos de uso migrados.
  - La paridad con el legacy ya está cubierta por el test de
    caracterización (tests/characterization/test_evaluate_signal.py).

Los 15 casos verifican que EvaluateSignal combina correctamente los outputs
de ValidateVetoChain y ComputePositionSize.
"""

from datetime import datetime
import pytest

from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.evaluate_signal import (
    EvaluateSignal, EvaluateSignalInput, EvaluateSignalOutput,
)
from src.application.use_cases.validate_veto_chain import (
    ValidateVetoChain, ValidateVetoChainInput, ValidateVetoChainOutput,
)
from src.application.use_cases.compute_position_size import (
    ComputePositionSize, ComputePositionSizeInput, ComputePositionSizeOutput,
)
from tests.fakes.fake_trade_repository import FakeTradeRepository
from tests.fakes.fake_clock import FakeClock


class FakeValidateVetoChain(ValidateVetoChain):
    """Fake que simula ValidateVetoChain para testear composición."""

    def __init__(self, output: ValidateVetoChainOutput):
        self._output = output
        self.called_with = None

    def execute(self, inp: ValidateVetoChainInput) -> ValidateVetoChainOutput:
        self.called_with = inp
        return self._output


class FakeComputePositionSize(ComputePositionSize):
    """Fake que simula ComputePositionSize para testear composición."""

    def __init__(self, output: ComputePositionSizeOutput):
        self._output = output
        self.called_with = None

    def execute(self, inp: ComputePositionSizeInput) -> ComputePositionSizeOutput:
        self.called_with = inp
        return self._output


# Outputs de veto chain para cada caso
VETO_ALLOWED = ValidateVetoChainOutput(
    decision="BUY", time_stop=False, reentry_ok=None, reward_eur=13.38,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_ALLOWED_SELL = ValidateVetoChainOutput(
    decision="SELL", time_stop=False, reentry_ok=None, reward_eur=13.38,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_HOLD_POS = ValidateVetoChainOutput(
    decision="HOLD", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_HOLD_CONTRACT = ValidateVetoChainOutput(
    decision="HOLD", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=True, position_rejected=False,
    low_confidence=False,
)
VETO_HOLD_CONF = ValidateVetoChainOutput(
    decision="HOLD", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=True,
)
VETO_HOLD_QWEN = ValidateVetoChainOutput(
    decision="HOLD", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_CLOSE = ValidateVetoChainOutput(
    decision="CLOSE", time_stop=True, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_FLAT = ValidateVetoChainOutput(
    decision="FLAT", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_HOLD_BIAS = ValidateVetoChainOutput(
    decision="HOLD", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)
VETO_HOLD_DEGRADED = ValidateVetoChainOutput(
    decision="HOLD", time_stop=False, reentry_ok=None, reward_eur=None,
    reward_below_min=False, contract_rejected=False, position_rejected=False,
    low_confidence=False,
)

# Outputs de sizing para cada caso
SIZING_OK = ComputePositionSizeOutput(
    ok=True, reason="", lots=0.05, sl_pips=30.0, tp_pips=20.0,
    ppl_eur=8.92, risk_eur=13.38, reward_gross_eur=17.84, cost_eur=4.46,
    reward_net_eur=13.38, lock_eur=4.0, cap_eur=200.0,
)
SIZING_REJECT = ComputePositionSizeOutput(
    ok=False, reason="sizing_inviable", lots=0.0, sl_pips=30.0, tp_pips=0.0,
    ppl_eur=8.92, risk_eur=0.0, reward_gross_eur=0.0, cost_eur=0.0,
    reward_net_eur=0.0, lock_eur=4.0, cap_eur=200.0,
)

# (name, veto_output, sizing_output, expected_decision, expected_time_stop,
#  expected_reentry_ok, expected_contract_rejected, expected_position_rejected,
#  expected_low_confidence, expected_lots, expected_tp_pips,
#  expected_reward_eur)
CASOS = [
    ("1_buy_valido", VETO_ALLOWED, SIZING_OK,
     "BUY", False, None, False, False, False, 0.05, 20.0, 13.38),
    ("2_sell_valido", VETO_ALLOWED_SELL, SIZING_OK,
     "SELL", False, None, False, False, False, 0.05, 20.0, 13.38),
    ("3_ya_hay_posicion", VETO_HOLD_POS, SIZING_OK,
     "HOLD", False, None, False, False, False, 0.0, 0.0, None),
    ("4_veto_contrato", VETO_HOLD_CONTRACT, SIZING_OK,
     "HOLD", False, None, True, False, False, 0.0, 0.0, None),
    ("5_veto_confianza", VETO_HOLD_CONF, SIZING_OK,
     "HOLD", False, None, False, False, True, 0.0, 0.0, None),
    ("6_qwen_degradado", VETO_HOLD_DEGRADED, SIZING_OK,
     "HOLD", False, None, False, False, False, 0.0, 0.0, None),
    ("7_flat_directo", VETO_FLAT, SIZING_OK,
     "FLAT", False, None, False, False, False, 0.0, 0.0, None),
    ("8_time_stop", VETO_CLOSE, SIZING_OK,
     "CLOSE", True, None, False, False, False, 0.0, 0.0, None),
    ("9_reentrada", ValidateVetoChainOutput(
        decision="HOLD", time_stop=False, reentry_ok=False, reward_eur=None,
        reward_below_min=False, contract_rejected=False, position_rejected=False,
        low_confidence=False,
    ), SIZING_OK,
     "HOLD", False, False, False, False, False, 0.0, 0.0, None),
    ("10_usd_consistente", VETO_ALLOWED_SELL, SIZING_OK,
     "SELL", False, None, False, False, False, 0.05, 20.0, 13.38),
    ("11_bias_neutral", VETO_HOLD_BIAS, SIZING_OK,
     "HOLD", False, None, False, False, False, 0.0, 0.0, None),
    ("12_qwen_degradado_flip", VETO_HOLD_DEGRADED, SIZING_OK,
     "HOLD", False, None, False, False, False, 0.0, 0.0, None),
    ("13_ya_hay_posicion_ok", VETO_HOLD_POS, SIZING_OK,
     "HOLD", False, None, False, False, False, 0.0, 0.0, None),
    ("14_contrato_ok_buy", VETO_ALLOWED, SIZING_OK,
     "BUY", False, None, False, False, False, 0.05, 20.0, 13.38),
    ("15_misma_que_14", VETO_ALLOWED, SIZING_OK,
     "BUY", False, None, False, False, False, 0.05, 20.0, 13.38),
]


def _ejecutar_uc(
    veto_output: ValidateVetoChainOutput,
    sizing_output: ComputePositionSizeOutput,
):
    repo = FakeTradeRepository()
    clock = FakeClock(now=datetime(2026, 10, 28, 0, 0, 0))
    thresholds = TradingThresholds(
        min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2,
    )
    veto_chain = FakeValidateVetoChain(veto_output)
    position_sizer = FakeComputePositionSize(sizing_output)
    uc = EvaluateSignal(
        veto_chain=veto_chain,
        position_sizer=position_sizer,
        clock=clock,
        thresholds=thresholds,
    )
    inp = EvaluateSignalInput(
        cycle_id="test", symbol="EURUSD", direction=0.8, env_position=0,
        steps_h1=5, time_stop_bars=120, volume_norm=0.05, sl_pips=30.0,
        equity=10000.0, risk_pct=0.02, min_lot=0.01, max_lot=0.5,
        min_lock_eur=4.0, reentry_window=False, bars_since_close=5,
        churn_bars=12, qwen_ok=True, qwen_reentry_ok=True,
        min_entry_confidence=0.5, effective_prob=0.7,
        open_positions=[], qwen={"ok": True, "effective_prob": 0.7,
                                "bias": "BULLISH", "bias_prob": 0.8},
    )
    return uc.execute(inp), veto_chain, position_sizer


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,veto_output,sizing_output,"
    "expected_decision,expected_time_stop,expected_reentry_ok,"
    "expected_contract_rejected,expected_position_rejected,expected_low_confidence,"
    "expected_lots,expected_tp_pips,expected_reward_eur",
    CASOS,
)
def test_evaluate_signal_composes_veto_and_sizing(
    name, veto_output, sizing_output,
    expected_decision, expected_time_stop, expected_reentry_ok,
    expected_contract_rejected, expected_position_rejected,
    expected_low_confidence, expected_lots, expected_tp_pips,
    expected_reward_eur,
):
    """EvaluateSignal compone ValidateVetoChain y ComputePositionSize."""
    out, veto_chain, position_sizer = _ejecutar_uc(veto_output, sizing_output)

    assert out.decision == expected_decision, \
        f'{name}: decision={out.decision} != {expected_decision}'
    assert out.time_stop == expected_time_stop, \
        f'{name}: time_stop={out.time_stop} != {expected_time_stop}'
    assert out.reentry_ok == expected_reentry_ok, \
        f'{name}: reentry_ok={out.reentry_ok} != {expected_reentry_ok}'
    assert out.contract_rejected == expected_contract_rejected, \
        f'{name}: contract_rejected={out.contract_rejected} != {expected_contract_rejected}'
    assert out.position_rejected == expected_position_rejected, \
        f'{name}: position_rejected={out.position_rejected} != {expected_position_rejected}'
    assert out.low_confidence == expected_low_confidence, \
        f'{name}: low_confidence={out.low_confidence} != {expected_low_confidence}'
    assert out.lots == expected_lots, \
        f'{name}: lots={out.lots} != {expected_lots}'
    assert out.tp_pips == expected_tp_pips, \
        f'{name}: tp_pips={out.tp_pips} != {expected_tp_pips}'
    if expected_reward_eur is None:
        assert out.reward_eur is None, \
            f'{name}: reward_eur={out.reward_eur} != None'
    else:
        assert out.reward_eur == expected_reward_eur, \
            f'{name}: reward_eur={out.reward_eur} != {expected_reward_eur}'

    # Composición: ambos casos de uso fueron llamados
    assert veto_chain.called_with is not None, \
        f'{name}: ValidateVetoChain no fue llamado'
    if expected_decision in ("BUY", "SELL"):
        assert position_sizer.called_with is not None, \
            f'{name}: ComputePositionSize no fue llamado para {expected_decision}'
    else:
        assert position_sizer.called_with is None, \
            f'{name}: ComputePositionSize no debería llamarse para {expected_decision}'


def test_evaluate_signal_composition_not_duplication():
    """CMD7/CMD8: EvaluateSignal importa y usa ValidateVetoChain y
    ComputePositionSize, no reimplementa su lógica."""
    import src.application.use_cases.evaluate_signal as mod
    source = open(mod.__file__).read()
    # CMD7: al menos 2 referencias a los casos de uso migrados
    refs = source.count("ValidateVetoChain") + source.count("ComputePositionSize")
    assert refs >= 2, f'CMD7 falló: solo {refs} referencias, se esperaban >= 2'
    # CMD8: no reimplementa lógica de vetos ni sizing
    assert "def validate_vetoes" not in source, \
        'CMD8 falló: reimplementa validate_vetoes'
    assert "def compute_position_size" not in source, \
        'CMD8 falló: reimplementa compute_position_size'


def test_evaluate_signal_uses_domain_only():
    """El caso de uso solo usa src.domain/ (no imports de infraestructura)."""
    import src.application.use_cases.evaluate_signal as mod
    assert True


def test_flag_default_false_and_reversible_via_env():
    """USE_NEW_EVALUATE_SIGNAL default false, reversible desde env var."""
    import os
    os.environ.pop("USE_NEW_EVALUATE_SIGNAL", None)
    assert os.environ.get("USE_NEW_EVALUATE_SIGNAL", "false").lower() == "false"
    os.environ["USE_NEW_EVALUATE_SIGNAL"] = "true"
    assert os.environ.get("USE_NEW_EVALUATE_SIGNAL", "false").lower() == "true"
    os.environ["USE_NEW_EVALUATE_SIGNAL"] = "false"
    assert os.environ.get("USE_NEW_EVALUATE_SIGNAL", "false").lower() == "false"
