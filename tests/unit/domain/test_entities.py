"""Tests unitarios para entidades Trade, Signal, Session."""

from decimal import Decimal
from datetime import datetime
from src.domain.entities import Trade, Signal, Session
from src.domain.value_objects.thresholds import TradingThresholds

def test_trade_abierto_y_cerrado():
    t = Trade(
        id="t1", symbol="EURUSD", side="BUY",
        entry_price=Decimal("1.10"), exit_price=Decimal("1.20"),
        pnl=Decimal("10"), opened_at=datetime.now(), closed_at=datetime.now()
    )
    assert t.is_closed() is True

def test_signal_is_confident():
    s = Signal(id="s1", symbol="EURUSD", side="BUY", confidence=Decimal("0.6"), created_at=datetime.now())
    assert s.is_confident() is True

def test_session_add_trade_y_pnl():
    s = Session(id="s1", started_at=datetime.now(), trades=())
    t = Trade(id="t1", symbol="EURUSD", side="BUY",
              entry_price=Decimal("1"), exit_price=Decimal("1.2"),
              pnl=Decimal("4"), opened_at=datetime.now(), closed_at=datetime.now())
    s2 = s.add_trade(t)
    assert len(s2.trades) == 1
    assert s2.pnl_total() == Decimal("4")
    assert s2.n_wins(TradingThresholds()) == 1