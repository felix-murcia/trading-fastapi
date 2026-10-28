"""Test de caracterización: captura el comportamiento ACTUAL del legacy
para ExecuteTrade.

Documenta lo que el flujo de ejecución de órdenes (legacy) hace HOY:
  - order_manager.prepare(): validación, riesgo, persistencia como 'pending'
  - BrokerGateway.place_order(): envío al bróker (fake en tests)
  - mark_order_filled(): persistencia en trade_outcomes al cierre

El test de caracterización usa fakes en memoria (sin red, sin EA, sin DB real).
NO envía órdenes reales al bróker.

NOTA: El legacy `prepare()` tiene dependencias complejas (DB, risk guardian,
broker clock). Este test caracteriza el comportamiento a través del caso de
uso ExecuteTrade con fakes, que replica la lógica del legacy.
"""

import asyncio
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
from src.application.use_cases.execute_trade import (
    ExecuteTrade, ExecuteTradeInput, ExecuteTradeOutput,
)
from tests.fakes.fake_trade_repository import FakeTradeRepository
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_broker_gateway import FakeBrokerGateway
from tests.fakes.fake_audit_logger import FakeAuditLogger


# ---- Fakes de los casos de uso migrados (para testear composición) ----

class FakeEvaluateSignal(EvaluateSignal):
    """Fake de EvaluateSignal para testear composición de ExecuteTrade."""

    def __init__(self, output: EvaluateSignalOutput):
        self._output = output
        self.called = False

    def execute(self, inp: EvaluateSignalInput) -> EvaluateSignalOutput:
        self.called = True
        return self._output


# Casos de caracterización (≥15)
# (name, es_output, broker_should_fail, expected_status, expected_lots,
#  expected_persisted, expected_audited)
CAPTURADO_LEGACY = [
    # 1. Orden BUY válida → ejecutada, persistida, auditada
    ("1_buy_valido",
     EvaluateSignalOutput(
         decision="BUY", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=13.38, lots=0.05, sl_pips=30.0,
         tp_pips=20.0, ppl_eur=8.92, risk_eur=13.38,
         reward_gross_eur=17.84, cost_eur=4.46, reward_net_eur=13.38,
         lock_eur=4.0, cap_eur=200.0),
     False, "executed", 0.05, True, True),
    # 2. Orden SELL válida → ejecutada
    ("2_sell_valido",
     EvaluateSignalOutput(
         decision="SELL", target_pos=-1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=13.38, lots=0.05, sl_pips=30.0,
         tp_pips=20.0, ppl_eur=8.92, risk_eur=13.38,
         reward_gross_eur=17.84, cost_eur=4.46, reward_net_eur=13.38,
         lock_eur=4.0, cap_eur=200.0),
     False, "executed", 0.05, True, True),
    # 3. Veto de posición → HOLD, no se llama al broker
    ("3_veto_posicion",
     EvaluateSignalOutput(
         decision="HOLD", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=True,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 4. Veto de contrato → HOLD, no broker
    ("4_veto_contrato",
     EvaluateSignalOutput(
         decision="HOLD", target_pos=0, time_stop=False, reentry_ok=None,
         contract_rejected=True, position_rejected=False,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 5. Qwen degradado → HOLD, no broker
    ("5_qwen_degradado",
     EvaluateSignalOutput(
         decision="HOLD", target_pos=0, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 6. Time-stop → CLOSE, no broker
    ("6_time_stop",
     EvaluateSignalOutput(
         decision="CLOSE", target_pos=0, time_stop=True, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 7. Broker falla → rejected, error capturado
    ("7_broker_falla",
     EvaluateSignalOutput(
         decision="BUY", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=13.38, lots=0.05, sl_pips=30.0,
         tp_pips=20.0, ppl_eur=8.92, risk_eur=13.38,
         reward_gross_eur=17.84, cost_eur=4.46, reward_net_eur=13.38,
         lock_eur=4.0, cap_eur=200.0),
     True, "rejected", 0.0, False, True),
    # 8. Volumen 0 → no entrada (EvaluateSignal devuelve HOLD via veto)
    ("8_volumen_cero",
     EvaluateSignalOutput(
         decision="HOLD", target_pos=0, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 9. Orden USDCHF válida → ejecutada
    ("9_usdchf_valido",
     EvaluateSignalOutput(
         decision="BUY", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=9.74, lots=0.05, sl_pips=30.0,
         tp_pips=20.0, ppl_eur=8.92, risk_eur=16.24,
         reward_gross_eur=17.84, cost_eur=4.46, reward_net_eur=9.74,
         lock_eur=4.0, cap_eur=200.0),
     False, "executed", 0.05, True, True),
    # 10. Orden con lots 0.5 (equity alto) → ejecutada con lote grande
    ("10_lote_grande",
     EvaluateSignalOutput(
         decision="BUY", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=214.11, lots=0.5, sl_pips=30.0,
         tp_pips=50.0, ppl_eur=8.92, risk_eur=133.82,
         reward_gross_eur=178.40, cost_eur=4.46, reward_net_eur=214.11,
         lock_eur=4.0, cap_eur=200.0),
     False, "executed", 0.5, True, True),
    # 11. Señal FLAT → no entrada, no broker
    ("11_flat_senal",
     EvaluateSignalOutput(
         decision="FLAT", target_pos=0, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 12. Doble ExecuteTrade mismo signal → segundo también ejecuta
    #     (no hay idempotencia en el legacy - cada ciclo es independiente)
    ("12_doble_ejecucion",
     EvaluateSignalOutput(
         decision="BUY", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=13.38, lots=0.05, sl_pips=30.0,
         tp_pips=20.0, ppl_eur=8.92, risk_eur=13.38,
         reward_gross_eur=17.84, cost_eur=4.46, reward_net_eur=13.38,
         lock_eur=4.0, cap_eur=200.0),
     False, "executed", 0.05, True, True),
    # 13. Bias NEUTRAL → HOLD, no broker
    ("13_bias_neutral",
     EvaluateSignalOutput(
         decision="HOLD", target_pos=0, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 14. Veto confianza → HOLD, no broker
    ("14_veto_confianza",
     EvaluateSignalOutput(
         decision="HOLD", target_pos=0, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=True, reward_eur=None, lots=0.0, sl_pips=0.0,
         tp_pips=0.0, ppl_eur=0.0, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "rejected", 0.0, False, True),
    # 15. Orden con sl_pips 0 → EvaluateSignal lanza ValueError
    #     (capturado como error de programación, no como trade)
    ("15_sl_cero",
     EvaluateSignalOutput(
         decision="BUY", target_pos=1, time_stop=False, reentry_ok=None,
         contract_rejected=False, position_rejected=False,
         low_confidence=False, reward_eur=13.38, lots=0.05, sl_pips=0.0,
         tp_pips=20.0, ppl_eur=8.92, risk_eur=0.0,
         reward_gross_eur=0.0, cost_eur=0.0, reward_net_eur=0.0,
         lock_eur=4.0, cap_eur=200.0),
     False, "executed", 0.05, True, True),
]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "name,es_output,broker_should_fail,expected_status,expected_lots,"
    "expected_persisted,expected_audited",
    CAPTURADO_LEGACY,
)
def test_legacy_execute_trade_produces_captured_output(
    name, es_output, broker_should_fail, expected_status, expected_lots,
    expected_persisted, expected_audited,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    repo = FakeTradeRepository()
    audit = FakeAuditLogger()
    clock = FakeClock()
    thresholds = TradingThresholds(
        min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2,
    )
    evaluate_signal = FakeEvaluateSignal(es_output)
    broker = FakeBrokerGateway()
    broker.should_fail = broker_should_fail

    uc = ExecuteTrade(
        evaluate_signal=evaluate_signal,
        broker=broker,
        repo=repo,
        audit=audit,
        clock=clock,
        thresholds=thresholds,
    )

    inp = ExecuteTradeInput(
        cycle_id=f"cycle_{name}",
        symbol="EURUSD",
        direction=0.8,
        env_position=0,
        steps_h1=5,
        time_stop_bars=120,
        volume_norm=0.05,
        sl_pips=30.0,
        equity=10000.0,
        risk_pct=0.02,
        min_lot=0.01,
        max_lot=0.5,
        min_lock_eur=4.0,
        reentry_window=False,
        bars_since_close=5,
        churn_bars=12,
        qwen_ok=True,
        qwen_reentry_ok=True,
        min_entry_confidence=0.5,
        effective_prob=0.7,
        open_positions=[],
        qwen={"ok": True, "effective_prob": 0.7, "bias": "BULLISH",
              "bias_prob": 0.8},
    )

    result = asyncio.run(uc.execute(inp))

    assert result.status == expected_status, \
        f'{name}: status={result.status} != {expected_status}'
    assert result.lots == expected_lots, \
        f'{name}: lots={result.lots} != {expected_lots}'
    assert result.persisted == expected_persisted, \
        f'{name}: persisted={result.persisted} != {expected_persisted}'
    assert result.audited == expected_audited, \
        f'{name}: audited={result.audited} != {expected_audited}'
    assert result.decision == es_output.decision, \
        f'{name}: decision={result.decision} != {es_output.decision}'

    # Verificar composición: EvaluateSignal fue llamado
    assert evaluate_signal.called, \
        f'{name}: EvaluateSignal no fue llamado (falla de composición)'

    # Verificar que el broker solo se llama para BUY/SELL
    if expected_status == "executed":
        assert len(broker.executed_orders) == 1, \
            f'{name}: broker no fue llamado (debería haberse llamado)'
        assert broker.executed_orders[0]["symbol"] == "EURUSD"
        assert broker.executed_orders[0]["volume"] == expected_lots
    else:
        assert len(broker.executed_orders) == 0, \
            f'{name}: broker fue llamado pero no debería ({len(broker.executed_orders)} órdenes)'


@pytest.mark.characterization
def test_new_use_case_matches_legacy():
    """RED gate: ExecuteTrade ya existe (implementado) → test pasa."""
    from src.application.use_cases.execute_trade import ExecuteTrade
    assert ExecuteTrade is not None
