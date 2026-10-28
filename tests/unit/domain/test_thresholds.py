"""Tests unitarios para TradingThresholds."""

from decimal import Decimal
from src.domain.value_objects.thresholds import TradingThresholds

def test_defaults():
    t = TradingThresholds()
    assert t.min_win_eur == Decimal("3.00")
    assert t.safe_lock_eur == Decimal("4.00")
    assert t.cost_buffer_pips == 2

def test_override():
    t = TradingThresholds(min_win_eur=Decimal("5"), safe_lock_eur=Decimal("6"), cost_buffer_pips=3)
    assert t.min_win_eur == Decimal("5")
    assert t.safe_lock_eur == Decimal("6")
    assert t.cost_buffer_pips == 3

def test_inmutabilidad():
    t = TradingThresholds()
    try:
        t.min_win_eur = Decimal("10")
        assert False, "inmutable"
    except AttributeError:
        pass