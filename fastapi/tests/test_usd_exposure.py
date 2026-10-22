"""Tests del guard de exposición USD (2026-10-21).

Cubre la clasificación de signo y la detección de conflicto. El caso que
motivó el guard: EURUSD BUY (corto USD) + USDCHF BUY (largo USD) abiertos a
la vez; `MAX_TOTAL_SAME_DIR_POSITIONS` los contaba como "misma dirección".
"""
import pytest

from services.usd_exposure import usd_conflict, usd_sign


@pytest.mark.parametrize(
    "symbol,order_type,expected",
    [
        # quote=USD, base!=USD: BUY = corto USD, SELL = largo USD
        ("EURUSD", "BUY", -1),
        ("EURUSD", "SELL", +1),
        ("EURUSD", "buy", -1),
        ("GBPUSD", "BUY", -1),
        ("AUDUSD", "SELL", +1),
        ("NZDUSD", "BUY", -1),
        ("XAUUSD", "BUY", -1),
        ("XAUUSD", "SELL", +1),
        # base=USD: BUY = largo USD, SELL = corto USD
        ("USDCHF", "BUY", +1),
        ("USDCHF", "SELL", -1),
        ("USDJPY", "BUY", +1),
        ("USDJPY", "SELL", -1),
        ("USDCAD", "BUY", +1),
        ("USDCNH", "SELL", -1),
        ("USDSEK", "BUY", +1),
        # Sin pata USD: no se inventa dirección
        ("EURGBP", "BUY", None),
        ("EURCHF", "SELL", None),
        # Normalización y tipos desconocidos
        (" eurusd ", " buy ", -1),
        ("eurusd", "long", -1),
        ("eurusd", "short", +1),
        ("usdchf", "long", +1),
        ("usdchf", "short", -1),
        ("EURUSD", "HOLD", None),
        ("EURUSD", "", None),
        ("", "BUY", None),
        ("UNKNOWN", "BUY", None),
    ],
)
def test_usd_sign(symbol, order_type, expected):
    assert usd_sign(symbol, order_type) == expected


def test_eurusd_buy_vs_usdchf_buy_conflict():
    """El incidente exacto: ambos BUY, pero el dólar va al revés."""
    conflict = usd_conflict(
        "EURUSD",
        "BUY",
        [{"symbol": "USDCHF", "type": "buy", "ticket": 58986641010}],
    )
    assert conflict == {
        "conflict_symbol": "USDCHF",
        "conflict_type": "BUY",
        "conflict_ticket": 58986641010,
        "conflict_usd_sign": +1,
        "order_usd_sign": -1,
    }


def test_eurusd_buy_vs_usdchf_sell_no_conflict():
    """Ambos cortos USD: no hay contradicción."""
    assert usd_conflict(
        "EURUSD", "BUY", [{"symbol": "USDCHF", "type": "sell", "ticket": 1}]
    ) is None


def test_eurusd_buy_vs_gbpusd_buy_no_conflict():
    """EURUSD y GBPUSD comparten el mismo factor USD: mismo signo."""
    assert usd_conflict(
        "EURUSD", "BUY", [{"symbol": "GBPUSD", "type": "buy", "ticket": 2}]
    ) is None


def test_eurusd_sell_vs_usdchf_buy_no_conflict():
    """Ambos largos USD: alineados."""
    assert usd_conflict(
        "EURUSD", "SELL", [{"symbol": "USDCHF", "type": "buy", "ticket": 3}]
    ) is None


def test_eurusd_buy_vs_usdjpy_buy_conflict():
    """USDJPY BUY también es largo USD."""
    conflict = usd_conflict(
        "EURUSD", "BUY", [{"symbol": "USDJPY", "type": "buy", "ticket": 4}]
    )
    assert conflict is not None
    assert conflict["conflict_symbol"] == "USDJPY"
    assert conflict["conflict_usd_sign"] == +1
    assert conflict["order_usd_sign"] == -1


def test_xauusd_buy_vs_usdchf_buy_conflict():
    """XAUUSD BUY es corto USD; choca con USDCHF BUY."""
    assert usd_conflict(
        "XAUUSD", "BUY", [{"symbol": "USDCHF", "type": "buy", "ticket": 5}]
    ) is not None


def test_same_symbol_is_skipped():
    """El mismo par lo gobierna position_veto / el tope por par, no este guard."""
    assert usd_conflict(
        "EURUSD", "BUY", [{"symbol": "EURUSD", "type": "sell", "ticket": 6}]
    ) is None


def test_cross_without_usd_does_not_block():
    """Una orden sin pata USD no se bloquea por este guard."""
    assert usd_conflict(
        "EURGBP", "BUY", [{"symbol": "EURUSD", "type": "sell", "ticket": 7}]
    ) is None


def test_unknown_order_type_does_not_block():
    assert usd_conflict(
        "EURUSD", "HOLD", [{"symbol": "USDCHF", "type": "buy", "ticket": 8}]
    ) is None


def test_non_dict_positions_are_ignored():
    assert usd_conflict(
        "EURUSD", "BUY", [None, "USDCHF", 42, {"symbol": "USDCHF", "type": "buy"}]
    ) is not None


def test_empty_positions_no_conflict():
    assert usd_conflict("EURUSD", "BUY", []) is None
    assert usd_conflict("EURUSD", "BUY", None) is None
