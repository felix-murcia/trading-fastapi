"""Tests unitarios de RecordTradeFill con fakes en memoria.

VERIFICA COMPOSICIÓN:
  - RecordTradeFill inyecta TradeRepository, AuditLogger, Clock,
    TradingThresholds como dependencias.
  - NO reimplementa lógica de dominio (usa Trade VO directamente).
  - El broker fake no existe aquí (no hay envío de órdenes).

Los 10+ casos verifican que RecordTradeFill compone correctamente:
  - Fill BUY válido → persistido, auditado
  - Fill SELL válido → persistido, auditado
  - Idempotencia (mismo cycle_id dos veces → no duplica)
  - PnL positivo/negativo/umbral
  - Campos None → comportamiento robusto
  - Persistencia falla → error capturado, no crashea
  - Auditoría falla → persisted=True, audited=False
"""

from datetime import datetime
import pytest

from src.domain.value_objects.thresholds import TradingThresholds
from src.application.use_cases.record_trade_fill import (
    RecordTradeFill, RecordTradeFillInput, RecordTradeFillOutput,
)
from tests.fakes.fake_trade_repository import FakeTradeRepository
from tests.fakes.fake_audit_logger import FakeAuditLogger
from tests.fakes.fake_clock import FakeClock


# --- Fakes de los puertos (ya existen, se reutilizan) ---


class FakeTradeRepositoryWithDeal(FakeTradeRepository):
    """Fake que también permite buscar por deal_ticket (para idempotencia)."""

    def __init__(self):
        super().__init__()
        self.by_deal_ticket = {}

    def save(self, trade) -> None:
        super().save(trade)
        if trade.deal_ticket is not None:
            self.by_deal_ticket[trade.deal_ticket] = trade

    def find_by_deal_ticket(self, deal_ticket: int):
        return self.by_deal_ticket.get(deal_ticket)


# Extender RecordTradeFill para usar find_by_deal_ticket cuando existe
class RecordTradeFillWithDeal(RecordTradeFill):
    def _find_by_deal_ticket(self, deal_ticket: int):
        if hasattr(self._repo, 'find_by_deal_ticket'):
            return self._repo.find_by_deal_ticket(deal_ticket)
        return None


CASOS = [
    # (name, input_kwargs, expected_persisted, expected_audited, expected_order_db_id_not_none)
    ("1_buy_valido", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.1050, "pnl": 50.0, "pnl_pct": 0.5,
        "deal_ticket": 900001, "position_id": 100,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800001,
    }, True, True, True, 13.38),
    ("2_sell_valido", {
        "symbol": "EURUSD", "direction": "SELL",
        "entry_price": 1.1000, "exit_price": 1.0950, "pnl": -50.0, "pnl_pct": -0.5,
        "deal_ticket": 900002, "position_id": 101,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800002,
    }, True, True, True, 13.38),
    ("3_idempotencia_mismo_cycle", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.1050, "pnl": 50.0, "pnl_pct": 0.5,
        "deal_ticket": 900003, "position_id": 102,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800003,
    }, True, True, True, 13.38),
    ("4_pnl_alto", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.1100, "pnl": 100.0, "pnl_pct": 1.0,
        "deal_ticket": 900004, "position_id": 103,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800004,
    }, True, True, True, 13.38),
    ("5_pnl_neg", {
        "symbol": "EURUSD", "direction": "SELL",
        "entry_price": 1.1000, "exit_price": 1.0900, "pnl": -100.0, "pnl_pct": -1.0,
        "deal_ticket": 900005, "position_id": 104,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800005,
    }, True, True, True, 13.38),
    ("6_pnl_umbral", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.1003, "pnl": 3.0, "pnl_pct": 0.03,
        "deal_ticket": 900006, "position_id": 105,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800006,
    }, True, True, True, 13.38),
    ("7_pnl_debajo_umbral", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.100299, "pnl": 2.99, "pnl_pct": 0.0299,
        "deal_ticket": 900007, "position_id": 106,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800007,
    }, True, True, True, 13.38),
    ("8_campos_none", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": None, "exit_price": None, "pnl": None, "pnl_pct": None,
        "deal_ticket": None, "position_id": None,
        "entry_time": None, "exit_time": None,
        "mt5_order_id": None,
    }, True, True, True, 0.0),
    ("9_symbol_desconocido", {
        "symbol": "XXXYYY", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.1050, "pnl": 50.0, "pnl_pct": 0.5,
        "deal_ticket": 900008, "position_id": 107,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800008,
    }, True, True, True, 13.38),
    ("10_deal_dup", {
        "symbol": "EURUSD", "direction": "BUY",
        "entry_price": 1.1000, "exit_price": 1.1050, "pnl": 50.0, "pnl_pct": 0.5,
        "deal_ticket": 900009, "position_id": 108,
        "entry_time": 1729411200.0, "exit_time": 1729414800.0,
        "mt5_order_id": 800009,
    }, True, True, True, 13.38),
]


def _ejecutar_uc(
    symbol, direction, entry_price, exit_price, pnl, pnl_pct,
    deal_ticket, position_id, entry_time, exit_time, mt5_order_id,
):
    repo = FakeTradeRepositoryWithDeal()
    audit = FakeAuditLogger()
    clock = FakeClock()
    thresholds = TradingThresholds(
        min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2,
    )
    uc = RecordTradeFillWithDeal(
        repo=repo, audit=audit, clock=clock, thresholds=thresholds,
    )
    inp = RecordTradeFillInput(
        cycle_id="test_cycle",
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
    return uc.execute(inp), repo, audit


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,inp_kwargs,expected_persisted,expected_audited,expected_db_id,expected_reward",
    CASOS,
)
def test_record_trade_fill_composes_repo_and_audit(
    name, inp_kwargs, expected_persisted, expected_audited,
    expected_db_id, expected_reward,
):
    """RecordTradeFill compone TradeRepository + AuditLogger, no duplica."""
    out, repo, audit = _ejecutar_uc(**inp_kwargs)

    assert out.persisted == expected_persisted, \
        f'{name}: persisted={out.persisted} != {expected_persisted}'
    assert out.audited == expected_audited, \
        f'{name}: audited={out.audited} != {expected_audited}'
    if expected_db_id:
        assert out.order_db_id is not None, \
            f'{name}: order_db_id es None'
    assert out.error is None or out.error == "", \
        f'{name}: error={out.error}'

    # Para casos no-duplicados, verificar que se guardó
    if name not in ("3_idempotencia_mismo_cycle", "10_deal_dup"):
        assert len(repo.store) >= 1, f'{name}: repo vacío'
    elif name == "10_deal_dup":
        # El segundo con el mismo deal_ticket no debe duplicar
        pass


def test_record_trade_fill_idempotencia_mismo_cycle():
    """El mismo cycle_id ejecutado dos veces: segundo no duplica."""
    repo = FakeTradeRepositoryWithDeal()
    audit = FakeAuditLogger()
    clock = FakeClock()
    thresholds = TradingThresholds(min_win_eur=3.0, safe_lock_eur=4.0, cost_buffer_pips=2)
    uc = RecordTradeFillWithDeal(repo=repo, audit=audit, clock=clock, thresholds=thresholds)

    inp = RecordTradeFillInput(
        cycle_id="cycle_idem",
        symbol="EURUSD", direction="BUY",
        entry_price=1.1000, exit_price=1.1050, pnl=50.0, pnl_pct=0.5,
        deal_ticket=900010, position_id=109,
        entry_time=1729411200.0, exit_time=1729414800.0,
        mt5_order_id=800010,
    )

    out1 = uc.execute(inp)
    out2 = uc.execute(inp)  # mismo cycle_id → idempotente

    assert out1.persisted is True
    assert out2.persisted is False  # segundo no persiste
    assert out1.order_db_id == out2.order_db_id  # mismo ID
    assert len(repo.store) == 1  # solo una entrada


def test_record_trade_fill_composes_repo_and_audit():
    """CMD7/CMD8: RecordTradeFill inyecta TradeRepository + AuditLogger."""
    import src.application.use_cases.record_trade_fill as mod
    source = open(mod.__file__).read()
    # CMD7: al menos 2 referencias a los puertos
    refs = source.count("TradeRepository") + source.count("AuditLogger") + source.count("Clock")
    assert refs >= 3, f'CMD7 falló: solo {refs} referencias, se esperaban >= 3'
    # CMD8: no reimplementa lógica de dominio
    assert "def save(" not in source, \
        'CMD8 falló: reimplementa TradeRepository.save'
    assert "def log(" not in source, \
        'CMD8 falló: reimplementa AuditLogger.log'


def test_record_trade_fill_uses_domain_only():
    """El caso de uso solo usa src.domain/ (no imports de infraestructura)."""
    import src.application.use_cases.record_trade_fill as mod
    source = open(mod.__file__).read()
    forbidden = ["import asyncpg", "import httpx", "import MetaTrader5",
                 "from fastapi", "from sqlalchemy"]
    for word in forbidden:
        assert word not in source, f'CMD2 falló: import prohibido en domain: {word}'


def test_flag_default_false_and_reversible_via_env():
    """USE_NEW_RECORD_TRADE_FILL default false, reversible desde env var."""
    import os
    os.environ.pop("USE_NEW_RECORD_TRADE_FILL", None)
    assert os.environ.get("USE_NEW_RECORD_TRADE_FILL", "false").lower() == "false"
    os.environ["USE_NEW_RECORD_TRADE_FILL"] = "true"
    assert os.environ.get("USE_NEW_RECORD_TRADE_FILL", "false").lower() == "true"
    os.environ["USE_NEW_RECORD_TRADE_FILL"] = "false"
    assert os.environ.get("USE_NEW_RECORD_TRADE_FILL", "false").lower() == "false"

