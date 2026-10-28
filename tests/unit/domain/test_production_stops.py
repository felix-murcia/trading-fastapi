"""Tests unitarios para production_stops (regla dominio)."""

import pytest
from src.domain.rules.production_stops import production_stops


def test_production_stops_fallback_atr():
    # Sin niveles (floor/ceiling) -> fallback ATR
    # atr_norm = 0.0015 (15 pips), pip_size = 0.0001
    # sl = 1.5 * 15 = 22.5, tp = 2 * 22.5 = 45
    sl, tp = production_stops(atr_norm=0.0015, pip_size=0.0001)
    assert sl == pytest.approx(22.5)
    assert tp == pytest.approx(45.0)


def test_production_stops_long_with_levels():
    # LONG: current_price=1.1000, floor_below=1.0990, ceiling_above=1.1020
    # SL = floor - 2*pips = 1.0988 -> 12 pips
    # TP = ceiling = 1.1020 -> 20 pips
    sl, tp = production_stops(
        atr_norm=0.0015,
        pip_size=0.0001,
        direction=1,
        floor_below=1.0990,
        ceiling_above=1.1020,
        current_price=1.1000,
    )
    assert sl == pytest.approx(12.0)
    assert tp == pytest.approx(20.0)


def test_production_stops_short_with_levels():
    # SHORT: SL = ceiling + 2*pips = 1.1022 -> 22 pips
    # TP = floor = 1.0990 -> 10 pips
    sl, tp = production_stops(
        atr_norm=0.0015,
        pip_size=0.0001,
        direction=-1,
        floor_below=1.0990,
        ceiling_above=1.1020,
        current_price=1.1000,
    )
    assert sl == pytest.approx(22.0)
    assert tp == pytest.approx(10.0)


def test_production_stops_zero_direction():
    # direction=0: fallback ATR (sin niveles geometricos)
    sl, tp = production_stops(
        atr_norm=0.0015,
        pip_size=0.0001,
        direction=0,
        floor_below=1.0990,
        ceiling_above=1.1020,
        current_price=1.1000,
    )
    assert sl == pytest.approx(22.5)
    assert tp == pytest.approx(45.0)


def test_production_stops_min_bounds():
    # SL clamp a min 10; TP = 2*10 = 20
    sl, tp = production_stops(atr_norm=0.00005, pip_size=0.0001)
    assert sl == pytest.approx(10.0)
    assert tp == pytest.approx(20.0)


def test_production_stops_max_bounds():
    # ATR muy grande -> sl clamp 50; tp = sl*2 = 100
    sl, tp = production_stops(atr_norm=0.005, pip_size=0.0001)
    assert sl == pytest.approx(50.0)
    assert tp == pytest.approx(100.0)


def test_production_stops_min_level_pips():
    # Nivel demasiado cerca (< 5 pips) -> fallback ATR
    sl, tp = production_stops(
        atr_norm=0.0015,
        pip_size=0.0001,
        direction=1,
        floor_below=1.0999,   # 1 pip abajo
        ceiling_above=1.1001, # 1 pip arriba
        current_price=1.1000,
    )
    assert sl == pytest.approx(22.5)
    assert tp == pytest.approx(45.0)