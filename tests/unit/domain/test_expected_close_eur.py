"""Tests unitarios para expected_close_eur (regla dominio)."""

from decimal import Decimal
from src.domain.rules.expected_close_eur import expected_close_eur


def test_expected_close_eur_eurusd():
    # Paridad con legacy: EURUSD, pips=1, volume=0.01, price=1.12094
    # En legacy: 1 * 0.01 * 10 / 1.12094 = 0.0892... EUR (verificado en trade #255)
    result = expected_close_eur("EURUSD", 1.0, 0.01, 1.12094, 1.12094)
    assert round(result, 2) == 0.09  # 1 pips × 0.01 lot × 10 / 1.12094 ≈ 0.0892


def test_expected_close_eur_gbpusd():
    # GBPUSD: pips=1, volume=0.01, price=1.25345, eurusd_price=1.25345 (simplificado)
    # Legacy: 1 * 0.01 * 10 / 1.25345 = 0.0798 €
    result = expected_close_eur("GBPUSD", 1.0, 0.01, 1.25345, 1.25345)
    assert round(result, 4) == 0.0798  # Aproximado a 0.0798 en legacy


def test_expected_close_eur_usdchf():
    # USDCHF: 10/(0.82197*1.1236) = 10.828 EUR/pip/lote (legacy deal 58603377390)
    # pips=10, volume=0.04 -> 10*0.04*10/(0.82197*1.1236) = 4.331 EUR
    result = expected_close_eur("USDCHF", 10.0, 0.04, 0.82197, 1.1236)
    assert round(result, 2) == 4.33  # legacy medido: +4.33 EUR


def test_expected_close_eur_invalid_args():
    try:
        expected_close_eur("EURUSD", 0.0, 0.01, 1.12094, 1.12094)
        assert False, "debería haber lanzado ValueError"
    except ValueError as e:
        assert "pips/volume/price > 0" in str(e)


def test_expected_close_eur_missing_eurusd():
    try:
        expected_close_eur("GBPUSD", 1.0, 0.01, 1.25345, None)
        assert False, "debería haber lanzado ValueError"
    except ValueError as e:
        assert "exige el precio EURUSD" in str(e)


def test_expected_close_eur_non_positive_price():
    try:
        expected_close_eur("EURUSD", 1.0, 0.01, 0.0, 1.12094)
        assert False, "debería haber lanzado ValueError"
    except ValueError as e:
        assert "pips/volume/price > 0" in str(e)


def test_expected_close_eur_non_positive_pips():
    try:
        expected_close_eur("EURUSD", 0.0, 0.01, 1.12094, 1.12094)
        assert False, "debería haber lanzado ValueError"
    except ValueError as e:
        assert "pips/volume/price > 0" in str(e)


def test_expected_close_eur_non_positive_volume():
    try:
        expected_close_eur("EURUSD", 1.0, 0.0, 1.12094, 1.12094)
        assert False, "debería haber lanzado ValueError"
    except ValueError as e:
        assert "pips/volume/price > 0" in str(e)