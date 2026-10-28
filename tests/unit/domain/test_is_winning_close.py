"""Tests unitarios para is_winning_close (regla dominio)."""

from decimal import Decimal
from src.domain.rules.is_winning_close import is_winning_close
from src.domain.value_objects.pnl import PnL
from src.domain.value_objects.money import Money
from src.domain.value_objects.thresholds import TradingThresholds


def test_is_winning_above_threshold():
    # Paridad con legacy/core/rules.py:32, MIN_WIN_EUR = 3.0
    assert is_winning_close(PnL(Money(Decimal("5.00"))), TradingThresholds()) is True


def test_is_winning_at_exact_threshold():
    # Frontera exacta: pnl >= 3.0 → ganancia
    assert is_winning_close(PnL(Money(Decimal("3.00"))), TradingThresholds()) is True


def test_is_winning_below_threshold():
    # Debajo del umbral: pérdida
    assert is_winning_close(PnL(Money(Decimal("2.99"))), TradingThresholds()) is False


def test_is_winning_near_zero():
    # Cero beneficios => pérdida (no cubre spread)
    assert is_winning_close(PnL(Money(Decimal("0.00"))), TradingThresholds()) is False


def test_is_winning_negative():
    # Pérdida negativa => pérdida
    assert is_winning_close(PnL(Money(Decimal("-4.16"))), TradingThresholds()) is False


def test_is_winning_with_default_thresholds():
    # Que funcione con defaults (min_win_eur=3.00)
    assert is_winning_close(PnL(Money(Decimal("3.50")))) is True