"""Tests unitarios para Money."""

from decimal import Decimal
from src.domain.value_objects.money import Money


def test_construccion_valida():
    m = Money(amount=Decimal("100.50"))
    assert m.amount == Decimal("100.50")
    assert m.currency == "EUR"

def test_currency_invalida_levanta():
    try:
        Money(amount=Decimal("10"), currency="USD")
        assert False, "deberia haber lanzado ValueError"
    except ValueError as e:
        assert "Only EUR supported" in str(e)

def test_igualdad():
    assert Money(Decimal("3")) == Money(Decimal("3.00"))

def test_inmutabilidad():
    m = Money(Decimal("50"))
    try:
        m.amount = Decimal("60")
        assert False, "deberia haber lanzado AttributeError"
    except AttributeError:
        pass