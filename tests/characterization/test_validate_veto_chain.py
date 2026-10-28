"""Test de caracterización: captura el comportamiento ACTUAL del legacy.

Documenta lo que `apply_mechanical_vetoes` (fastapi/services/veto_chain.py)
hace HOY, incluido el orden de evaluación y el primer veto que gana.
NO se corrige nada del legacy. Si hay un comportamiento raro, se captura
tal cual con TODO.
"""

import pytest

from services.trade_contract import TradeContract
from services.veto_chain import MechanicalInputs, apply_mechanical_vetoes

qwen_ok = {'ok': True, 'effective_prob': 0.7, 'bias': 'BULLISH', 'bias_prob': 0.8}
qwen_degraded = {'ok': False, 'effective_prob': 0.7, 'bias': 'NEUTRAL', 'bias_prob': 0.5}
qwen_neutral = {'ok': True, 'effective_prob': 0.7, 'bias': 'NEUTRAL', 'bias_prob': 0.5}

contract_ok = TradeContract(
    ok=True, reason='', lots=0.05, sl_pips=30.0, tp_pips=20.0,
    ppl_eur=8.92, risk_eur=13.38, reward_gross_eur=17.84, cost_eur=4.46,
    reward_net_eur=13.38, lock_eur=4.0, cap_eur=200.0, detail={}
)
contract_reject = TradeContract(
    ok=False, reason='tp_no_paga_bloqueo', lots=0.0, sl_pips=30.0, tp_pips=20.0,
    ppl_eur=8.92, risk_eur=13.38, reward_gross_eur=17.84, cost_eur=4.46,
    reward_net_eur=2.00, lock_eur=4.0, cap_eur=200.0, detail={}
)

# (name, symbol, decision, env_position, position_raw, steps_h1, time_stop_bars,
#  contract_case, lots, tp_pips, min_lock_eur, reentry_window, bars_since_close,
#  churn_bars, qwen_ok, qwen_reentry_ok, min_entry_confidence, effective_prob,
#  open_positions, qwen,
#  expected_decision, expected_time_stop, expected_reentry_ok,
#  expected_contract_rejected, expected_position_rejected, expected_low_confidence,
#  expected_reward_eur)
CAPTURADO_LEGACY = [
    ("1_sin_vetos", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [], qwen_ok,
     'BUY', False, None, False, False, False, 13.38),
    ("2_veto_posicion", 'EURUSD', 'BUY', 1, 1, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [], qwen_ok,
     'HOLD', False, None, False, True, False, 13.38),
    ("3_veto_contrato", 'EURUSD', 'BUY', 0, 0, 5, 120, 'reject', 0.0, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [], qwen_ok,
     'HOLD', False, None, True, False, False, None),
    ("4_veto_confianza", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.8, 0.7, [], qwen_ok,
     'HOLD', False, None, False, False, True, 13.38),
    ("5_qwen_degradado", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, False, None, 0.5, 0.7, [], qwen_degraded,
     'HOLD', False, None, False, False, False, 13.38),
    ("6_veto_usd_no_conflicto", 'EURUSD', 'SELL', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [{'symbol': 'USDCHF', 'type': 'SELL'}], qwen_ok,
     'SELL', False, None, False, False, False, 13.38),
    ("7_veto_bias_neutral", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [], qwen_neutral,
     'HOLD', False, None, False, False, False, 13.38),
    ("8_time_stop", 'EURUSD', 'BUY', 1, 1, 130, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [], qwen_ok,
     'CLOSE', True, None, False, False, False, None),
    ("9_reentrada", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     True, 0, 12, True, False, 0.5, 0.7, [], qwen_ok,
     'HOLD', False, False, False, False, False, 13.38),
    ("10_reentrada_qwen_degradado_bars0", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     True, 0, 12, False, None, 0.5, 0.7, [], qwen_degraded,
     'HOLD', False, False, False, False, False, 13.38),
    ("11_qwen_degradado_flip", 'EURUSD', 'BUY', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, False, None, 0.5, 0.7, [], qwen_degraded,
     'HOLD', False, None, False, False, False, 13.38),
    ("12_hold_directo", 'EURUSD', 'HOLD', 0, 0, 5, 120, 'ok', 0.05, 20.0, 4.0,
     False, 5, 12, True, True, 0.5, 0.7, [], qwen_ok,
     'HOLD', False, None, False, False, False, None),
]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "name,symbol,decision,env_position,position_raw,steps_h1,time_stop_bars,"
    "contract_case,lots,tp_pips,min_lock_eur,reentry_window,bars_since_close,"
    "churn_bars,qwen_ok,qwen_reentry_ok,min_entry_confidence,effective_prob,"
    "open_positions,qwen,"
    "expected_decision,expected_time_stop,expected_reentry_ok,"
    "expected_contract_rejected,expected_position_rejected,expected_low_confidence,"
    "expected_reward_eur",
    CAPTURADO_LEGACY,
)
def test_legacy_produces_captured_output(
    name, symbol, decision, env_position, position_raw, steps_h1,
    time_stop_bars, contract_case, lots, tp_pips, min_lock_eur, reentry_window,
    bars_since_close, churn_bars, qwen_ok, qwen_reentry_ok,
    min_entry_confidence, effective_prob, open_positions, qwen,
    expected_decision, expected_time_stop, expected_reentry_ok,
    expected_contract_rejected, expected_position_rejected,
    expected_low_confidence, expected_reward_eur,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    contract = contract_reject if contract_case == 'reject' else contract_ok
    inp = MechanicalInputs(
        cycle_id='test', symbol=symbol, decision=decision,
        env_position=env_position, position_raw=position_raw,
        steps_h1=steps_h1, time_stop_bars=time_stop_bars, contract=contract,
        lots=lots, tp_pips=tp_pips, min_lock_eur=min_lock_eur,
        reentry_window=reentry_window, bars_since_close=bars_since_close,
        churn_bars=churn_bars, qwen_ok=qwen_ok, qwen_reentry_ok=qwen_reentry_ok,
        min_entry_confidence=min_entry_confidence, effective_prob=effective_prob,
        open_positions=open_positions, qwen=qwen,
    )
    out = apply_mechanical_vetoes(inp)
    assert out.decision == expected_decision, f'{name}: decision={out.decision} != {expected_decision}'
    assert out.time_stop == expected_time_stop, f'{name}: time_stop={out.time_stop} != {expected_time_stop}'
    assert out.reentry_ok == expected_reentry_ok, f'{name}: reentry_ok={out.reentry_ok} != {expected_reentry_ok}'
    assert out.contract_rejected == expected_contract_rejected, f'{name}: contract_rejected={out.contract_rejected} != {expected_contract_rejected}'
    assert out.position_rejected == expected_position_rejected, f'{name}: position_rejected={out.position_rejected} != {expected_position_rejected}'
    assert out.low_confidence == expected_low_confidence, f'{name}: low_confidence={out.low_confidence} != {expected_low_confidence}'
    if expected_reward_eur is None:
        assert out.reward_eur is None, f'{name}: reward_eur={out.reward_eur} != None'
    else:
        assert out.reward_eur == expected_reward_eur, f'{name}: reward_eur={out.reward_eur} != {expected_reward_eur}'


@pytest.mark.characterization
def test_new_use_case_matches_legacy():
    """RED gate: ValidateVetoChain aún no existe → ImportError."""
    from src.application.use_cases.validate_veto_chain import ValidateVetoChain  # noqa: F401
    assert ValidateVetoChain is not None
