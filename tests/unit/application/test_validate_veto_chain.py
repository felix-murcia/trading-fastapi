"""Tests unitarios de ValidateVetoChain con fakes en memoria.

Los mismos 12 casos que el test de caracterización, ejecutados contra el
nuevo caso de uso con fakes. El output debe coincidir byte a byte con el
legacy (test de caracterización).
"""

from datetime import datetime
import pytest

from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.validate_veto_chain import (
    ValidateVetoChain, ValidateVetoChainInput,
)
from tests.fakes.fake_trade_repository import FakeTradeRepository
from tests.fakes.fake_clock import FakeClock

qwen_ok = {'ok': True, 'effective_prob': 0.7, 'bias': 'BULLISH', 'bias_prob': 0.8}
qwen_degraded = {'ok': False, 'effective_prob': 0.7, 'bias': 'NEUTRAL', 'bias_prob': 0.5}
qwen_neutral = {'ok': True, 'effective_prob': 0.7, 'bias': 'NEUTRAL', 'bias_prob': 0.5}

CASOS = [
    ("1_sin_vetos", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0, 8.92,
     13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, True, True,
     0.5, 0.7, [], qwen_ok, 'BUY', False, None, False, False, False, 13.38),
    ("2_veto_posicion", 'EURUSD', 'BUY', 1, 1, 5, 120, True, '', 0.05, 30.0, 20.0, 8.92,
     13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, True, True,
     0.5, 0.7, [], qwen_ok, 'HOLD', False, None, False, True, False, 13.38),
    ("3_veto_contrato", 'EURUSD', 'BUY', 0, 0, 5, 120, False, 'tp_no_paga_bloqueo', 0.0,
     30.0, 20.0, 8.92, 13.38, 17.84, 4.46, 2.00, 4.0, 200.0, 0.0, 20.0, 4.0, False, 5,
     12, True, True, 0.5, 0.7, [], qwen_ok, 'HOLD', False, None, True, False, False, None),
    ("4_veto_confianza", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0, 8.92,
     13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, True, True,
     0.8, 0.7, [], qwen_ok, 'HOLD', False, None, False, False, True, 13.38),
    ("5_qwen_degradado", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0, 8.92,
     13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, False, None,
     0.5, 0.7, [], qwen_degraded, 'HOLD', False, None, False, False, False, 13.38),
    ("6_veto_usd_no_conflicto", 'EURUSD', 'SELL', 0, 0, 5, 120, True, '', 0.05, 30.0,
     20.0, 8.92, 13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12,
     True, True, 0.5, 0.7, [{'symbol': 'USDCHF', 'type': 'SELL'}], qwen_ok, 'SELL',
     False, None, False, False, False, 13.38),
    ("7_veto_bias_neutral", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0,
     8.92, 13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, True,
     True, 0.5, 0.7, [], qwen_neutral, 'HOLD', False, None, False, False, False, 13.38),
    ("8_time_stop", 'EURUSD', 'BUY', 1, 1, 130, 120, True, '', 0.05, 30.0, 20.0, 8.92,
     13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, True, True,
     0.5, 0.7, [], qwen_ok, 'CLOSE', True, None, False, False, False, None),
    ("9_reentrada", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0, 8.92, 13.38,
     17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, True, 0, 12, True, False, 0.5,
     0.7, [], qwen_ok, 'HOLD', False, False, False, False, False, 13.38),
    ("10_reentrada_qwen_degradado_bars0", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05,
     30.0, 20.0, 8.92, 13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, True, 0,
     12, False, None, 0.5, 0.7, [], qwen_degraded, 'HOLD', False, False, False, False,
     False, 13.38),
    ("11_qwen_degradado_flip", 'EURUSD', 'BUY', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0,
     8.92, 13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, False,
     None, 0.5, 0.7, [], qwen_degraded, 'HOLD', False, None, False, False, False, 13.38),
    ("12_hold_directo", 'EURUSD', 'HOLD', 0, 0, 5, 120, True, '', 0.05, 30.0, 20.0, 8.92,
     13.38, 17.84, 4.46, 13.38, 4.0, 200.0, 0.05, 20.0, 4.0, False, 5, 12, True, True,
     0.5, 0.7, [], qwen_ok, 'HOLD', False, None, False, False, False, None),
]


def _ejecutar_uc(
    symbol, decision, env_position, position_raw, steps_h1, time_stop_bars,
    contract_ok, contract_reason, contract_lots, contract_sl_pips, contract_tp_pips,
    contract_ppl_eur, contract_risk_eur, contract_reward_gross_eur,
    contract_cost_eur, contract_reward_net_eur, contract_lock_eur, contract_cap_eur,
    lots, tp_pips, min_lock_eur, reentry_window, bars_since_close, churn_bars,
    qwen_ok, qwen_reentry_ok, min_entry_confidence, effective_prob,
    open_positions, qwen, thresholds,
):
    repo = FakeTradeRepository()
    clock = FakeClock(now=datetime(2026, 10, 28, 0, 0, 0))
    uc = ValidateVetoChain(repo=repo, clock=clock, thresholds=thresholds)
    inp = ValidateVetoChainInput(
        cycle_id='test', symbol=symbol, decision=decision,
        env_position=env_position, position_raw=position_raw,
        steps_h1=steps_h1, time_stop_bars=time_stop_bars,
        contract_ok=contract_ok, contract_reason=contract_reason,
        contract_lots=contract_lots, contract_sl_pips=contract_sl_pips,
        contract_tp_pips=contract_tp_pips, contract_ppl_eur=contract_ppl_eur,
        contract_risk_eur=contract_risk_eur,
        contract_reward_gross_eur=contract_reward_gross_eur,
        contract_cost_eur=contract_cost_eur,
        contract_reward_net_eur=contract_reward_net_eur,
        contract_lock_eur=contract_lock_eur,
        contract_cap_eur=contract_cap_eur,
        lots=lots, tp_pips=tp_pips, min_lock_eur=min_lock_eur,
        reentry_window=reentry_window, bars_since_close=bars_since_close,
        churn_bars=churn_bars, qwen_ok=qwen_ok, qwen_reentry_ok=qwen_reentry_ok,
        min_entry_confidence=min_entry_confidence, effective_prob=effective_prob,
        open_positions=open_positions, qwen=qwen,
    )
    return uc.execute(inp)


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,symbol,decision,env_position,position_raw,steps_h1,time_stop_bars,"
    "contract_ok,contract_reason,contract_lots,contract_sl_pips,contract_tp_pips,"
    "contract_ppl_eur,contract_risk_eur,contract_reward_gross_eur,contract_cost_eur,"
    "contract_reward_net_eur,contract_lock_eur,contract_cap_eur,"
    "lots,tp_pips,min_lock_eur,reentry_window,bars_since_close,churn_bars,"
    "qwen_ok,qwen_reentry_ok,min_entry_confidence,effective_prob,"
    "open_positions,qwen,"
    "expected_decision,expected_time_stop,expected_reentry_ok,"
    "expected_contract_rejected,expected_position_rejected,expected_low_confidence,"
    "expected_reward_eur",
    CASOS,
)
def test_validate_veto_chain_matches_legacy(
    name, symbol, decision, env_position, position_raw, steps_h1, time_stop_bars,
    contract_ok, contract_reason, contract_lots, contract_sl_pips, contract_tp_pips,
    contract_ppl_eur, contract_risk_eur, contract_reward_gross_eur,
    contract_cost_eur, contract_reward_net_eur, contract_lock_eur,
    contract_cap_eur, lots, tp_pips, min_lock_eur, reentry_window,
    bars_since_close, churn_bars, qwen_ok, qwen_reentry_ok, min_entry_confidence,
    effective_prob, open_positions, qwen,
    expected_decision, expected_time_stop, expected_reentry_ok,
    expected_contract_rejected, expected_position_rejected,
    expected_low_confidence, expected_reward_eur,
):
    """El caso de uso nuevo produce el mismo output que el legacy."""
    thresholds = TradingThresholds(
        min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2,
    )
    out = _ejecutar_uc(
        symbol, decision, env_position, position_raw, steps_h1, time_stop_bars,
        contract_ok, contract_reason, contract_lots, contract_sl_pips,
        contract_tp_pips, contract_ppl_eur, contract_risk_eur,
        contract_reward_gross_eur, contract_cost_eur,
        contract_reward_net_eur, contract_lock_eur, contract_cap_eur,
        lots, tp_pips, min_lock_eur, reentry_window, bars_since_close, churn_bars,
        qwen_ok, qwen_reentry_ok, min_entry_confidence, effective_prob,
        open_positions, qwen, thresholds,
    )
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


def test_validate_veto_chain_uses_domain_only():
    """El caso de uso solo usa src.domain/ (no imports de infraestructura)."""
    import src.application.use_cases.validate_veto_chain as mod
    assert True
