"""Tests unitarios de RecordEADecision con fakes en memoria.

VERIFICA:
  - RecordEADecision inyecta AuditLogger + Clock como dependencias.
  - NO reimplementa lógica de dominio (usa AuditLogger directamente).
  - El AuditLogger es FakeAuditLogger (en memoria, sin IO).
  - El Clock es FakeClock (determinista).

CASOS (≥8):
  - Decisión BUY válida → auditada
  - Decisión SELL válida → auditada
  - Decisión HOLD válida → auditada
  - Mismo cycle_id dos veces → no es idempotente (audita dos veces)
  - Campos opcionales None → se pasan tal cual
  - Todos los campos opcionales → se pasan tal cual
  - mt5_time ausente → usa clock.now()
  - Audit falla → audited=False, error capturado
"""

import pytest
from datetime import datetime

from src.application.use_cases.record_ea_decision import (
    RecordEADecision, RecordEADecisionInput,
)
from tests.fakes.fake_audit_logger import FakeAuditLogger
from tests.fakes.fake_clock import FakeClock


CASOS = [
    # (name, input_kwargs, expected_audited, expected_error_substring, expected_events_count)
    ("1_buy_valido", {
        "cycle_id": "c1", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
    }, True, None, 1),
    ("2_sell_valido", {
        "cycle_id": "c2", "symbol": "GBPUSD", "received_decision": "SELL",
        "action": "close",
    }, True, None, 1),
    ("3_hold_valido", {
        "cycle_id": "c3", "symbol": "EURUSD", "received_decision": "HOLD",
        "action": "hold",
    }, True, None, 1),
    ("4_no_idempotente", {
        "cycle_id": "c_dup", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
    }, True, None, 1),
    ("5_campos_opcionales_none", {
        "cycle_id": "c5", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
        "order_ticket": None, "volume": None, "sl": None, "tp": None,
        "reason": None, "latency_ms": None, "raw_response": None,
        "mt5_time": None,
    }, True, None, 1),
    ("6_todos_los_campos", {
        "cycle_id": "c6", "symbol": "EURUSD", "received_decision": "SELL",
        "action": "modify",
        "order_ticket": "12345", "volume": 0.1, "sl": 1.0950, "tp": 1.1000,
        "reason": "tp_hit", "latency_ms": 45.5, "raw_response": '{"ok":true}',
        "mt5_time": 1730000000.0,
    }, True, None, 1),
    ("7_mt5_time_ausente_usa_clock", {
        "cycle_id": "c7", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
        "mt5_time": None,
    }, True, None, 1),
    ("8_audit_falla", {
        "cycle_id": "c8", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
    }, False, "audit_error", 0),
]


def _ejecutar_uc(name, input_kwargs, expected_audited, expected_error_substring,
                 expected_events_count):
    audit = FakeAuditLogger()
    clock = FakeClock()
    if name == "8_audit_falla":
        audit.log = _raise_on_log

    uc = RecordEADecision(audit=audit, clock=clock)
    inp = RecordEADecisionInput(**input_kwargs)
    return uc.execute(inp), audit


def _raise_on_log(event: str, details: dict) -> None:
    raise RuntimeError("audit logger falló")


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,input_kwargs,expected_audited,expected_error_substring,expected_events_count",
    CASOS,
)
def test_record_ea_decision_composes_audit_and_clock(
    name, input_kwargs, expected_audited, expected_error_substring,
    expected_events_count,
):
    """RecordEADecision compone AuditLogger + Clock, sin red."""
    out, audit = _ejecutar_uc(
        name, input_kwargs, expected_audited, expected_error_substring,
        expected_events_count,
    )

    assert out.audited == expected_audited, \
        f'{name}: audited={out.audited} != {expected_audited}'
    if expected_error_substring is not None:
        assert out.error is not None and expected_error_substring in out.error, \
            f'{name}: error="{out.error}" no contiene "{expected_error_substring}"'
    else:
        assert out.error is None, \
            f'{name}: error="{out.error}" != None'

    actual_events = len(audit.events)
    assert actual_events == expected_events_count, \
        f'{name}: events={actual_events} != {expected_events_count}'


def test_record_ea_decision_no_es_idempotente():
    """Mismo cycle_id → dos eventos (no idempotente, paridad legacy)."""
    audit = FakeAuditLogger()
    clock = FakeClock()
    uc = RecordEADecision(audit=audit, clock=clock)

    inp = RecordEADecisionInput(
        cycle_id="c_same", symbol="EURUSD",
        received_decision="BUY", action="open",
    )
    out1 = uc.execute(inp)
    out2 = uc.execute(inp)

    assert out1.audited is True
    assert out2.audited is True
    assert len(audit.events) == 2


def test_record_ea_decision_mt5_time_usa_clock():
    """mt5_time ausente → usa clock.now()."""
    fixed_now = datetime(2026, 10, 28, 12, 0, 0)
    clock = FakeClock(now=fixed_now)
    audit = FakeAuditLogger()
    uc = RecordEADecision(audit=audit, clock=clock)

    inp = RecordEADecisionInput(
        cycle_id="c_clock", symbol="EURUSD",
        received_decision="BUY", action="open",
        mt5_time=None,
    )
    out = uc.execute(inp)

    assert out.audited is True
    assert len(audit.events) == 1
    assert audit.events[0]["details"]["mt5_time"] == fixed_now


def test_record_ea_decision_uses_domain_only():
    """CMD2: solo imports de src.domain/."""
    import src.application.use_cases.record_ea_decision as mod
    source = open(mod.__file__).read()
    forbidden = ["import asyncpg", "import httpx", "import MetaTrader5",
                 "from fastapi", "from sqlalchemy", "import openai",
                 "import qwen", "import anthropic", "import requests"]
    for word in forbidden:
        assert word not in source, f'CMD2 falló: import prohibido: {word}'


def test_record_ea_decision_output_tiene_campos():
    """RecordEADecisionOutput tiene los campos correctos."""
    audit = FakeAuditLogger()
    clock = FakeClock()
    uc = RecordEADecision(audit=audit, clock=clock)

    inp = RecordEADecisionInput(
        cycle_id="c_out", symbol="EURUSD",
        received_decision="SELL", action="close",
    )
    out = uc.execute(inp)

    assert hasattr(out, "audited")
    assert hasattr(out, "error")
    assert out.audited is True
    assert out.error is None
