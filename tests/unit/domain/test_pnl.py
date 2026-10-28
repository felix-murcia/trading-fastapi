"""Tests unitarios para PnL."""

from decimal import Decimal
from src.domain.value_objects.pnl import PnL
from src.domain.value_objects.money import Money


def test_construccion_valida():
    p = PnL(value=Money(Decimal("5.50")))
    assert p.value.amount == Decimal("5.50")

def test_is_winning_umbral_3eur():
    # Paridad exacta con legacy/core/rules.py: MIN_WIN_EUR = 3.0
    assert PnL(Money(Decimal("3.00"))).is_winning(Money(Decimal("3.00"))) is True
    assert PnL(Money(Decimal("2.99"))).is_winning(Money(Decimal("3.00"))) is False
    assert PnL(Money(Decimal("5.00"))).is_winning(Money(Decimal("3.00"))) is True

def test_igualdad_e_inmutabilidad():
    p1 = PnL(Money(Decimal("10")))
    p2 = PnL(Money(Decimal("10.00")))
    assert p1 == p2
    try:
        p1.value = Money(Decimal("0"))
        assert False, "inmutable"
    except AttributeError:
        pass