"""Test de caracterización: captura el comportamiento ACTUAL del legacy.

Documenta lo que `predict_direction` (routers/ai.py) hace HOY al decidir
BUY/SELL/HOLD/CLOSE/FLAT, pasando por: decode_v3_direction → prepare_trade →
vetos mecánicos → bias → quórum → sizing veto.

NO se corrige nada del legacy. Si hay un comportamiento raro, se captura
tal cual con TODO.
"""

import pytest

from ml.trading_env_v2 import decode_v3_direction
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

# (name, direction, env_position, steps_h1, time_stop_bars, contract_case,
#  reentry_window, bars_since_close, churn_bars, qwen_ok, qwen_reentry_ok,
#  min_entry_confidence, effective_prob, open_positions, qwen,
#  expected_decision, expected_time_stop, expected_reentry_ok,
#  expected_contract_rejected, expected_position_rejected, expected_low_confidence,
#  expected_reward_eur)
CAPTURADO_LEGACY = [
    # 1. BUY válido sin vetos
    ("1_buy_valido", 0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5, 0.7,
     [], qwen_ok, "BUY", False, None, False, False, False, 13.38),
    # 2. SELL válido sin vetos
    ("2_sell_valido", -0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5, 0.7,
     [], qwen_ok, "SELL", False, None, False, False, False, 13.38),
    # 3. Ya hay posición (env_position=1) → decode_v3_direction da HOLD antes
    #    de llegar al veto de posición.
    ("3_ya_hay_posicion", 0.8, 1, 5, 120, 'ok', False, 5, 12, True, True, 0.5,
     0.7, [], qwen_ok, "HOLD", False, None, False, False, False, None),
    # 4. BUY con veto de contrato (rechazado)
    ("4_veto_contrato", 0.8, 0, 5, 120, 'reject', False, 5, 12, True, True, 0.5,
     0.7, [], qwen_ok, "HOLD", False, None, True, False, False, None),
    # 5. BUY con confianza baja (effective_prob < umbral)
    ("5_veto_confianza", 0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.8, 0.7,
     [], qwen_ok, "HOLD", False, None, False, False, True, 13.38),
    # 6. Qwen degradado
    ("6_qwen_degradado", 0.8, 0, 5, 120, 'ok', False, 5, 12, False, None, 0.5,
     0.7, [], qwen_degraded, "HOLD", False, None, False, False, False, 13.38),
    # 7. Señal FLAT directa (direction=0, sin posición)
    ("7_flat_directo", 0.0, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5, 0.7,
     [], qwen_ok, "FLAT", False, None, False, False, False, None),
    # 8. Time-stop activo (steps >= time_stop_bars)
    ("8_time_stop", 0.8, 1, 130, 120, 'ok', False, 5, 12, True, True, 0.5, 0.7,
     [], qwen_ok, "CLOSE", True, None, False, False, False, None),
    # 9. Re-entrada bloqueada (reentry_ok=False, bars=0)
    ("9_reentrada", 0.8, 0, 5, 120, 'ok', True, 0, 12, True, False, 0.5, 0.7,
     [], qwen_ok, "HOLD", False, False, False, False, False, 13.38),
    # 10. EURUSD SELL + USDCHF SELL = consistente (USD fuerte + USD débil),
    #     NO es conflicto → permitido
    ("10_usd_consistente", -0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5,
     0.7, [{'symbol': 'USDCHF', 'type': 'SELL'}], qwen_ok, "SELL", False, None,
     False, False, False, 13.38),
    # 11. Bias NEUTRAL → HOLD
    ("11_bias_neutral", 0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5, 0.7,
     [], qwen_neutral, "HOLD", False, None, False, False, False, 13.38),
    # 12. Qwen degradado con flip → HOLD (default bloquea)
    ("12_qwen_degradado_flip", 0.8, 0, 5, 120, 'ok', False, 5, 12, False, None,
     0.5, 0.7, [], qwen_degraded, "HOLD", False, None, False, False, False, 13.38),
    # 13. Ya hay posición + contrato OK → HOLD (posición ya es HOLD antes
    #    del veto, position_rejected=False).
    ("13_ya_hay_posicion_ok", 0.8, 1, 5, 120, 'ok', False, 5, 12, True, True,
     0.5, 0.7, [], qwen_ok, "HOLD", False, None, False, False, False, None),
    # 14. Contrato OK, decision=BUY → permitido (sin sizing inviable en este
    #     test porque no llamamos a prepare_trade con head_lots=0).
    ("14_contrato_ok_buy", 0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5,
     0.7, [], qwen_ok, "BUY", False, None, False, False, False, 13.38),
    # 15. Misma entrada que 14 (sin prepare_trade no hay ValueError por equity
    #     cero; el ValueError solo ocurre en prepare_trade, fuera de este test).
    ("15_misma_que_14", 0.8, 0, 5, 120, 'ok', False, 5, 12, True, True, 0.5,
     0.7, [], qwen_ok, "BUY", False, None, False, False, False, 13.38),
]


def _llamar_legacy(direction, env_position, steps_h1, time_stop_bars,
                   contract_case, reentry_window, bars_since_close, churn_bars,
                   qwen_ok, qwen_reentry_ok, min_entry_confidence,
                   effective_prob, open_positions, qwen):
    """Ejecuta el flujo legacy y devuelve la decisión final + flags."""
    target_pos, decision = decode_v3_direction(direction, env_position)
    contract = contract_reject if contract_case == 'reject' else contract_ok
    inp = MechanicalInputs(
        cycle_id='test', symbol='EURUSD', decision=decision,
        env_position=env_position, position_raw=env_position,
        steps_h1=steps_h1, time_stop_bars=time_stop_bars, contract=contract,
        lots=contract.lots, tp_pips=contract.tp_pips, min_lock_eur=4.0,
        reentry_window=reentry_window, bars_since_close=bars_since_close,
        churn_bars=churn_bars, qwen_ok=qwen_ok, qwen_reentry_ok=qwen_reentry_ok,
        min_entry_confidence=min_entry_confidence, effective_prob=effective_prob,
        open_positions=open_positions, qwen=qwen,
    )
    mech = apply_mechanical_vetoes(inp)
    return mech, target_pos, decision


@pytest.mark.characterization
@pytest.mark.parametrize(
    "name,direction,env_position,steps_h1,time_stop_bars,contract_case,"
    "reentry_window,bars_since_close,churn_bars,qwen_ok,qwen_reentry_ok,"
    "min_entry_confidence,effective_prob,open_positions,qwen,"
    "expected_decision,expected_time_stop,expected_reentry_ok,"
    "expected_contract_rejected,expected_position_rejected,expected_low_confidence,"
    "expected_reward_eur",
    CAPTURADO_LEGACY,
)
def test_legacy_produces_captured_output(
    name, direction, env_position, steps_h1, time_stop_bars, contract_case,
    reentry_window, bars_since_close, churn_bars, qwen_ok, qwen_reentry_ok,
    min_entry_confidence, effective_prob, open_positions, qwen,
    expected_decision, expected_time_stop, expected_reentry_ok,
    expected_contract_rejected, expected_position_rejected,
    expected_low_confidence, expected_reward_eur,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    mech, target_pos, decision = _llamar_legacy(
        direction, env_position, steps_h1, time_stop_bars, contract_case,
        reentry_window, bars_since_close, churn_bars, qwen_ok,
        qwen_reentry_ok, min_entry_confidence, effective_prob, open_positions,
        qwen,
    )
    assert mech.decision == expected_decision, \
        f'{name}: decision={mech.decision} != {expected_decision}'
    assert mech.time_stop == expected_time_stop, \
        f'{name}: time_stop={mech.time_stop} != {expected_time_stop}'
    assert mech.reentry_ok == expected_reentry_ok, \
        f'{name}: reentry_ok={mech.reentry_ok} != {expected_reentry_ok}'
    assert mech.contract_rejected == expected_contract_rejected, \
        f'{name}: contract_rejected={mech.contract_rejected} != {expected_contract_rejected}'
    assert mech.position_rejected == expected_position_rejected, \
        f'{name}: position_rejected={mech.position_rejected} != {expected_position_rejected}'
    assert mech.low_confidence == expected_low_confidence, \
        f'{name}: low_confidence={mech.low_confidence} != {expected_low_confidence}'
    if expected_reward_eur is None:
        assert mech.reward_eur is None, \
            f'{name}: reward_eur={mech.reward_eur} != None'
    else:
        assert mech.reward_eur == expected_reward_eur, \
            f'{name}: reward_eur={mech.reward_eur} != {expected_reward_eur}'


@pytest.mark.characterization
def test_new_use_case_matches_legacy():
    """RED gate: EvaluateSignal aún no existe → ImportError."""
    from src.application.use_cases.evaluate_signal import EvaluateSignal  # noqa: F401
    assert EvaluateSignal is not None
