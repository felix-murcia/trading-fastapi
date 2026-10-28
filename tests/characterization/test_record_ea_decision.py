"""Test de caracterización: captura el comportamiento ACTUAL del legacy
para RecordEADecision (migra la parte de /ea/decision webhook,
routers/ai.py:2310).

Documenta lo que el backend hace HOY al recibir una decisión del EA:
  - Recibe EADecisionRequest (cycle_id, symbol, received_decision, action, ...)
  - mt5_time fallback a time.time() si falta
  - Audita evento "ea_decision" con todos los campos
  - Retorna {"status": "recorded"}

NO es idempotente: cada llamada audita un evento nuevo, incluso con el mismo
cycle_id. Esto se captura tal cual (no se corrige).

Los tests usan FakeAuditLogger + FakeClock (sin infraestructura, sin red).
Nunca toca Qwen, DB ni broker.
"""

import pytest
from datetime import datetime

from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.record_ea_decision import (
    RecordEADecision, RecordEADecisionInput,
)
from tests.fakes.fake_audit_logger import FakeAuditLogger
from tests.fakes.fake_clock import FakeClock


# ─── Casos de caracterización (≥8) ──────────────────────────────────────

# (name, input_kwargs, expected_audited, expected_error, expected_events_count)
#   input_kwargs: dict para construir RecordEADecisionInput
#   expected_audited: True si se audita correctamente
#   expected_error: None si éxito, string si error de auditoría
#   expected_events_count: número de eventos esperados en el FakeAuditLogger
CAPTURADO_LEGACY = [
    # 1. Decisión BUY válida → auditada
    ("1_buy_valido", {
        "cycle_id": "c1", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
    }, True, None, 1),
    # 2. Decisión SELL válida → auditada
    ("2_sell_valido", {
        "cycle_id": "c2", "symbol": "GBPUSD", "received_decision": "SELL",
        "action": "close",
    }, True, None, 1),
    # 3. Decisión HOLD válida → auditada
    ("3_hold_valido", {
        "cycle_id": "c3", "symbol": "EURUSD", "received_decision": "HOLD",
        "action": "hold",
    }, True, None, 1),
    # 4. Mismo cycle_id dos veces → NO es idempotente (audita una vez por llamada)
    ("4_no_idempotente_una_llamada", {
        "cycle_id": "c_dup", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
    }, True, None, 1),
    # 5. Campos opcionales faltantes (None) → usa None tal cual
    ("5_campos_opcionales_none", {
        "cycle_id": "c5", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
        "order_ticket": None, "volume": None, "sl": None, "tp": None,
        "reason": None, "latency_ms": None, "raw_response": None,
        "mt5_time": None,
    }, True, None, 1),
    # 6. Con todos los campos opcionales → se pasan tal cual
    ("6_todos_los_campos", {
        "cycle_id": "c6", "symbol": "EURUSD", "received_decision": "SELL",
        "action": "modify",
        "order_ticket": "12345", "volume": 0.1, "sl": 1.0950, "tp": 1.1000,
        "reason": "tp_hit", "latency_ms": 45.5, "raw_response": '{"ok":true}',
        "mt5_time": 1730000000.0,
    }, True, None, 1),
    # 7. mt5_time ausente → usa clock.now() (determinista en tests)
    ("7_mt5_time_ausente_usa_clock", {
        "cycle_id": "c7", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
        "mt5_time": None,
    }, True, None, 1),
    # 8. Audit falla (side_effect) → audited=False, error capturado
    ("8_audit_falla", {
        "cycle_id": "c8", "symbol": "EURUSD", "received_decision": "BUY",
        "action": "open",
    }, False, "audit_error", 0),
]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "name,input_kwargs,expected_audited,expected_error,expected_events_count",
    CAPTURADO_LEGACY,
)
def test_legacy_record_ea_decision_produces_captured_output(
    name, input_kwargs, expected_audited, expected_error, expected_events_count,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    audit = FakeAuditLogger()
    clock = FakeClock()
    # Para el caso 8 (audit falla), inyectar side_effect
    if name == "8_audit_falla":
        audit.log = _raise_on_log

    uc = RecordEADecision(audit=audit, clock=clock)
    inp = RecordEADecisionInput(**input_kwargs)
    out = uc.execute(inp)

    assert out.audited == expected_audited, \
        f'{name}: audited={out.audited} != {expected_audited}'
    if expected_error is not None:
        assert out.error is not None and expected_error in out.error, \
            f'{name}: error="{out.error}" no contiene "{expected_error}"'
    else:
        assert out.error is None, \
            f'{name}: error="{out.error}" != None'

    # Verificar número de eventos en el audit logger
    actual_events = len(audit.events)
    assert actual_events == expected_events_count, \
        f'{name}: events={actual_events} != {expected_events_count}'


def _raise_on_log(event: str, details: dict) -> None:
    raise RuntimeError("audit logger falló")


@pytest.mark.characterization
def test_record_ea_decision_no_es_idempotente():
    """El legacy NO es idempotente: mismo cycle_id audita dos veces."""
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
    assert len(audit.events) == 2, \
        f"Esperaba 2 eventos (no idempotente), obtuvo {len(audit.events)}"
    # Ambos eventos tienen el mismo cycle_id
    assert audit.events[0]["details"]["cycle_id"] == "c_same"
    assert audit.events[1]["details"]["cycle_id"] == "c_same"


@pytest.mark.characterization
def test_record_ea_decision_mt5_time_usa_clock_cuando_falta():
    """Cuando mt5_time falta, se usa clock.now() (determinista en tests)."""
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


@pytest.mark.characterization
def test_record_ea_decision_output_campos():
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
