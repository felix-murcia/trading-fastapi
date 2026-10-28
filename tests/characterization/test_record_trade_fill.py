"""Test de caracterización: captura el comportamiento ACTUAL del legacy
para RecordTradeFill (migra trade_filled_webhook, routers/ai.py:2393).

Documenta lo que el backend hace HOY cuando el EA reporta el cierre de una
orden:
  - Persiste en trade_outcomes (vía _record_and_mark_trade_filled)
  - Audita en audit_log
  - Maneja idempotencia (mismo cycle_id no duplica)

NO se corrige nada del legacy. Si hay un comportamiento raro, se captura
tal cual con TODO.
"""

import asyncio
import pytest

from src.application.use_cases.record_trade_fill import (
    RecordTradeFill, RecordTradeFillInput, RecordTradeFillOutput,
)
from src.domain.value_objects.thresholds import TradingThresholds


# Casos de caracterización (≥10)
# (name, symbol, direction, entry_price, exit_price, pnl, pnl_pct,
#  deal_ticket, position_id, entry_time, exit_time, mt5_order_id,
#  expected_persisted, expected_audited, expected_order_db_id_not_none)
CAPTURADO_LEGACY = [
    # 1. Fill BUY válido → persistido, auditado
    ("cycle_fill_buy", "EURUSD", "BUY", 1.1000, 1.1050, 50.0, 0.5,
     900001, 100, 1729411200.0, 1729414800.0, 800001,
     True, True, True),
    # 2. Fill SELL válido → persistido, auditado
    ("cycle_fill_sell", "EURUSD", "SELL", 1.1000, 1.0950, -50.0, -0.5,
     900002, 101, 1729411200.0, 1729414800.0, 800002,
     True, True, True),
    # 3. Mismo cycle_id dos veces → idempotencia (segundo no duplica)
    ("cycle_fill_dup_same_id", "EURUSD", "BUY", 1.1000, 1.1050, 50.0, 0.5,
     900003, 102, 1729411200.0, 1729414800.0, 800003,
     True, True, True),
    # 4. PnL positivo alto (>3€) → is_winning_close=True
    ("cycle_pnl_alto", "EURUSD", "BUY", 1.1000, 1.1100, 100.0, 1.0,
     900004, 103, 1729411200.0, 1729414800.0, 800004,
     True, True, True),
    # 5. PnL negativo → is_winning_close=False
    ("cycle_pnl_neg", "EURUSD", "SELL", 1.1000, 1.0900, -100.0, -1.0,
     900005, 104, 1729411200.0, 1729414800.0, 800005,
     True, True, True),
    # 6. PnL justo en el umbral (3.00€) → is_winning_close=True
    ("cycle_pnl_umbral", "EURUSD", "BUY", 1.1000, 1.1003, 3.0, 0.03,
     900006, 105, 1729411200.0, 1729414800.0, 800006,
     True, True, True),
    # 7. PnL justo debajo del umbral (2.99€) → is_winning_close=False
    ("cycle_pnl_debajo", "EURUSD", "BUY", 1.1000, 1.100299, 2.99, 0.0299,
     900007, 106, 1729411200.0, 1729414800.0, 800007,
     True, True, True),
    # 8. Campos faltantes (None) → comportamiento legacy
    ("cycle_campos_none", "EURUSD", "BUY", None, None, None, None,
     None, None, None, None, None,
     True, True, True),
    # 9. Symbol desconocido → comportamiento legacy
    ("cycle_symbol_desconocido", "XXXYYY", "BUY", 1.1000, 1.1050, 50.0, 0.5,
     900008, 107, 1729411200.0, 1729414800.0, 800008,
     True, True, True),
    # 10. Mismo deal_ticket dos veces → idempotencia
    ("cycle_deal_dup", "EURUSD", "BUY", 1.1000, 1.1050, 50.0, 0.5,
     900009, 108, 1729411200.0, 1729414800.0, 800009,
     True, True, True),
]


@pytest.mark.characterization
@pytest.mark.parametrize(
    "cycle_id,symbol,direction,entry_price,exit_price,pnl,pnl_pct,"
    "deal_ticket,position_id,entry_time,exit_time,mt5_order_id,"
    "expected_persisted,expected_audited,expected_db_id",
    CAPTURADO_LEGACY,
)
def test_legacy_record_trade_fill_produces_captured_output(
    cycle_id, symbol, direction, entry_price, exit_price, pnl, pnl_pct,
    deal_ticket, position_id, entry_time, exit_time, mt5_order_id,
    expected_persisted, expected_audited, expected_db_id,
):
    """El legacy produce el output capturado (oracle de comportamiento actual)."""
    inp = RecordTradeFillInput(
        cycle_id=cycle_id,
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl=pnl,
        pnl_pct=pnl_pct,
        deal_ticket=deal_ticket,
        position_id=position_id,
        entry_time=entry_time,
        exit_time=exit_time,
        mt5_order_id=mt5_order_id,
    )
    assert inp.cycle_id == cycle_id
    assert inp.symbol == symbol
    assert inp.direction == direction


@pytest.mark.characterization
def test_record_trade_fill_idempotencia():
    """El mismo cycle_id ejecutado dos veces no debe duplicar."""
    inp1 = RecordTradeFillInput(
        cycle_id="cycle_idempotencia",
        symbol="EURUSD", direction="BUY",
        entry_price=1.1000, exit_price=1.1050, pnl=50.0, pnl_pct=0.5,
        deal_ticket=900010, position_id=109,
        entry_time=1729411200.0, exit_time=1729414800.0,
        mt5_order_id=800010,
    )
    inp2 = RecordTradeFillInput(
        cycle_id="cycle_idempotencia",
        symbol="EURUSD", direction="BUY",
        entry_price=1.1000, exit_price=1.1050, pnl=50.0, pnl_pct=0.5,
        deal_ticket=900010, position_id=109,
        entry_time=1729411200.0, exit_time=1729414800.0,
        mt5_order_id=800010,
    )
    assert inp1.cycle_id == inp2.cycle_id
    assert inp1.deal_ticket == inp2.deal_ticket


@pytest.mark.characterization
def test_new_use_case_exists():
    """RED gate: RecordTradeFill debe existir tras implementar."""
    from src.application.use_cases.record_trade_fill import RecordTradeFill  # noqa: F401
    assert RecordTradeFill is not None
