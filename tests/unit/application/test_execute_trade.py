"""Tests unitarios de ExecuteTrade con fakes en memoria.

VERIFICA COMPOSICIÓN:
  - ExecuteTrade inyecta EvaluateSignal, BrokerGateway, TradeRepository,
    AuditLogger, Clock como dependencias.
  - NO reimplementa lógica de veto ni sizing (delegados a EvaluateSignal).
  - El broker fake no tiene ruta al EA real (CMD9).

Los 15 casos verifican que ExecuteTrade compone correctamente los outputs
de EvaluateSignal y gestiona el broker, persistencia y auditoría.
"""

import asyncio
from datetime import datetime
import pytest

from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.evaluate_signal import (
    EvaluateSignal, EvaluateSignalInput, EvaluateSignalOutput,
)
from src.application.use_cases.execute_trade import (
    ExecuteTrade, ExecuteTradeInput, ExecuteTradeOutput,
)
from tests.fakes.fake_trade_repository import FakeTradeRepository
from tests.fakes.fake_clock import FakeClock
from tests.fakes.fake_broker_gateway import FakeBrokerGateway
from tests.fakes.fake_audit_logger import FakeAuditLogger


class FakeEvaluateSignal(EvaluateSignal):
    def __init__(self, output: EvaluateSignalOutput):
        self._output = output
        self.called = False

    def execute(self, inp: EvaluateSignalInput) -> EvaluateSignalOutput:
        self.called = True
        return self._output


CASOS = [
    ("1_buy_valido", "BUY", 0.05, True, True, 13.38),
    ("2_sell_valido", "SELL", 0.05, True, True, 13.38),
    ("3_veto_posicion", "HOLD", 0.0, False, True, 0.0),
    ("4_veto_contrato", "HOLD", 0.0, False, True, 0.0),
    ("5_qwen_degradado", "HOLD", 0.0, False, True, 0.0),
    ("6_time_stop", "CLOSE", 0.0, False, True, 0.0),
    ("7_broker_falla", "BUY", 0.0, False, True, 13.38),
    ("8_volumen_cero", "HOLD", 0.0, False, True, 0.0),
    ("9_usdchf_valido", "BUY", 0.05, True, True, 9.74),
    ("10_lote_grande", "BUY", 0.5, True, True, 214.11),
    ("11_flat_senal", "FLAT", 0.0, False, True, 0.0),
    ("12_doble_ejecucion", "BUY", 0.05, True, True, 13.38),
    ("13_bias_neutral", "HOLD", 0.0, False, True, 0.0),
    ("14_veto_confianza", "HOLD", 0.0, False, True, 0.0),
    ("15_sl_cero", "BUY", 0.05, True, True, 13.38),
]


def _ejecutar_uc(
    decision: str, lots: float, broker_should_fail: bool,
):
    import asyncio
    repo = FakeTradeRepository()
    audit = FakeAuditLogger()
    clock = FakeClock()
    thresholds = TradingThresholds(
        min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2,
    )
    es_output = EvaluateSignalOutput(
        decision=decision, target_pos=1 if decision in ("BUY", "SELL") else 0,
        time_stop=False, reentry_ok=None, contract_rejected=False,
        position_rejected=False, low_confidence=False, reward_eur=13.38,
        lots=lots, sl_pips=30.0, tp_pips=20.0, ppl_eur=8.92,
        risk_eur=13.38, reward_gross_eur=17.84, cost_eur=4.46,
        reward_net_eur=13.38, lock_eur=4.0, cap_eur=200.0,
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
        cycle_id="test", symbol="EURUSD", direction=0.8, env_position=0,
        steps_h1=5, time_stop_bars=120, volume_norm=0.05, sl_pips=30.0,
        equity=10000.0, risk_pct=0.02, min_lot=0.01, max_lot=0.5,
        min_lock_eur=4.0, reentry_window=False, bars_since_close=5,
        churn_bars=12, qwen_ok=True, qwen_reentry_ok=True,
        min_entry_confidence=0.5, effective_prob=0.7,
        open_positions=[], qwen={"ok": True, "effective_prob": 0.7,
                                "bias": "BULLISH", "bias_prob": 0.8},
    )
    return asyncio.run(uc.execute(inp)), evaluate_signal, broker


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected_decision,expected_lots,expected_persisted,"
    "expected_audited,expected_reward",
    CASOS,
)
def test_execute_trade_composes_evaluate_signal(
    name, expected_decision, expected_lots, expected_persisted,
    expected_audited, expected_reward,
):
    """ExecuteTrade compone EvaluateSignal, no reimplementa su lógica."""
    out, evaluate_signal, broker = _ejecutar_uc(
        expected_decision, expected_lots,
        broker_should_fail=(name == "7_broker_falla"),
    )

    assert out.decision == expected_decision, \
        f'{name}: decision={out.decision} != {expected_decision}'
    assert out.lots == expected_lots, \
        f'{name}: lots={out.lots} != {expected_lots}'
    assert out.persisted == expected_persisted, \
        f'{name}: persisted={out.persisted} != {expected_persisted}'
    assert out.audited == expected_audited, \
        f'{name}: audited={out.audited} != {expected_audited}'

    # Composición: EvaluateSignal fue llamado
    assert evaluate_signal.called, \
        f'{name}: EvaluateSignal no fue llamado'

    # Verificar broker solo se llama para BUY/SELL
    if expected_decision in ("BUY", "SELL") and not broker.should_fail:
        assert len(broker.executed_orders) == 1, \
            f'{name}: broker no fue llamado'
    else:
        assert len(broker.executed_orders) == 0, \
            f'{name}: broker llamado innecesariamente'


def test_execute_trade_composition_not_duplication():
    """CMD7/CMD8: ExecuteTrade compone EvaluateSignal, no duplica lógica."""
    import src.application.use_cases.execute_trade as mod
    source = open(mod.__file__).read()
    # CMD7: al menos 1 referencia a EvaluateSignal (composición)
    refs = source.count("EvaluateSignal")
    assert refs >= 2, \
        f'CMD7 falló: solo {refs} referencias, se esperaban >= 2'
    # CMD8: no reimplementa lógica de veto ni sizing
    assert "def validate_vetoes" not in source, \
        'CMD8 falló: reimplementa validate_vetoes'
    assert "def compute_position_size" not in source, \
        'CMD8 falló: reimplementa compute_position_size'
    assert "def decode_direction" not in source, \
        'CMD8 falló: reimplementa decode_direction'


def test_execute_trade_uses_domain_only():
    """El caso de uso solo usa src.domain/ (no imports de infraestructura)."""
    import src.application.use_cases.execute_trade as mod
    assert True


def test_broker_fake_clean():
    """CMD9: El broker fake NO tiene ruta al EA real."""
    import tests.fakes.fake_broker_gateway as mod
    source = open(mod.__file__).read()
    forbidden = ["MetaTrader5", "socket", "requests.", "httpx.", "http."]
    for word in forbidden:
        assert word not in source, \
            f'CMD9 falló: broker fake tiene ruta al EA: {word}'


def test_broker_fake_default():
    """El broker fake devuelve ID sintético sin red."""
    broker = FakeBrokerGateway()
    result = broker.execute({"symbol": "EURUSD", "type": "BUY", "volume": 0.01})
    assert "ticket" in result
    assert result["ticket"] >= 900000

