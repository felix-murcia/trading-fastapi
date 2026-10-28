"""Tests unitarios para Spread, StopLoss, SafeLockEur."""

from decimal import Decimal
from src.domain.value_objects.spread_stop_safelock import Spread, StopLoss, SafeLockEur

def test_spread_valido():
    s = Spread(Decimal("2"))
    assert s.pips == Decimal("2")

def test_spread_invalido_positivo():
    try:
        Spread(Decimal("0"))
        assert False
    except ValueError:
        pass

def test_stoploss_valido():
    assert StopLoss(Decimal("30")).pips == Decimal("30")

def test_stoploss_invalido_negativo():
    try:
        StopLoss(Decimal("-5"))
        assert False
    except ValueError:
        pass

def test_safelock_valido():
    s = SafeLockEur(Decimal("4.00"))
    assert s.amount == Decimal("4.00")

def test_safelock_invalido_menor_4eur():
    try:
        SafeLockEur(Decimal("3.00"))
        assert False
    except ValueError:
        pass