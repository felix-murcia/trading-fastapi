"""Test de paridad: reglas de parseo puro."""

from src.domain.rules.parsing import validate_symbol, parse_bool, parse_timestamp, UnknownSymbol


def test_validate_symbol_listed():
    listed = {"EURUSD", "GBPUSD", "USDCHF"}
    assert validate_symbol("EURUSD", listed) == "EURUSD"


def test_validate_symbol_unknown():
    listed = {"EURUSD", "GBPUSD", "USDCHF"}
    try:
        validate_symbol("UNKNOWN", listed)
        assert False, "debería lanzar UnknownSymbol"
    except UnknownSymbol:
        pass


def test_parse_bool():
    assert parse_bool(None) is False
    assert parse_bool(None, default=True) is True
    assert parse_bool(True) is True
    assert parse_bool(False) is False
    assert parse_bool(1) is True
    assert parse_bool(0) is False
    assert parse_bool("true") is True
    assert parse_bool("1") is True
    assert parse_bool("yes") is True
    assert parse_bool("false") is False
    assert parse_bool("0") is False


def test_parse_timestamp():
    assert parse_timestamp(1700000000.0) == 1700000000.0
    assert parse_timestamp("1700000000") == 1700000000.0
    assert parse_timestamp("1700000000.5") == 1700000000.5
