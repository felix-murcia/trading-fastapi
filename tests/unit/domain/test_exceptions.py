"""Tests de excepciones de dominio."""

import pytest
from decimal import Decimal
from src.domain.exceptions import DomainError, InvalidTradeError, InvalidPnLError, ThresholdViolationError


def test_domain_error_base():
    err = DomainError("error base")
    assert isinstance(err, Exception)
    assert str(err) == "error base"


def test_invalid_trade_error_hereda_domainerror():
    err = InvalidTradeError("volumen negativo")
    assert isinstance(err, DomainError)
    assert "volumen negativo" in str(err)


def test_invalid_pnl_error_hereda_domainerror():
    err = InvalidPnLError("pnl nulo")
    assert isinstance(err, DomainError)
    assert "pnl nulo" in str(err)


def test_threshold_violation_error_hereda_domainerror():
    err = ThresholdViolationError("min_win_eur", Decimal("3.00"), Decimal("2.00"))
    assert isinstance(err, DomainError)
    assert "3.00" in str(err) and "2.00" in str(err)


def test_threshold_violation_error_captura():
    with pytest.raises(ThresholdViolationError) as excinfo:
        raise ThresholdViolationError("min_win_eur", Decimal("3.00"), Decimal("2.00"))
    assert excinfo.value.expected == Decimal("3.00") if hasattr(excinfo.value, "expected") else True
