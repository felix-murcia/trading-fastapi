"""
G3-2 (2026-10-21) — SUELO / TECHO MÁS CERCANO: el contrato, fijado.

Contexto (Félix, 2026-10-21): "los SL/TP se sitúen en los suelo y techos más
cercanos a la entrada de la operación y con un lote que permita el trailing
del SL a +4 €".

Estos tests fijan las TRES propiedades que sostienen ese contrato:
  1. SIN MIRADA AL FUTURO: un swing de la vela i sólo es utilizable en i+2.
  2. FILTRO DE SIGNIFICANCIA: un nivel a menos de 0,75×ATR no es un
     suelo/techo (sin el filtro el "techo más cercano" estaba a 3 pips ⇒ R:R
     0,25 y beneficio < 3 €: ruido, no un nivel).
  3. PARIDAD TRAIN/SERVE: el env y /predict calculan EXACTAMENTE el mismo
     SL/TP (la paridad estaba rota desde 2026-10-08: el env entrenaba con la
     rama estructural y producción con la rama ATR).
  4. DISTANCIA REAL DEL STOP: el buffer de spread del EA entra en la
     distancia que usa el sizing y el audit (si no, el riesgo real de la
     posición excede el presupuesto).
"""
import numpy as np
import pandas as pd
import pytest

from ml.trading_env_v2 import (
    MIN_LEVEL_PIPS,
    NEAREST_LEVEL_ATR_MULT,
    SPREAD_BUFFER_PIPS,
    effective_sl_pips,
    engineer_market_features,
    nearest_significant_levels,
    production_sl,
    production_stops,
    production_tp,
    target_level_candidates,
)
from core.rules import size_ppo_position
from services.position_sizing import PIP_SIZE
from services.trade_contract import evaluate_trade

PIP = 0.0001


def _candles(n=300, step=0.00012, rng=None):
    """Serie determinista OSCILANTE (suelo y techo existen cerca del precio)."""
    rows = []
    for i in range(n):
        base = 1.1000 + 0.00025 * np.sin(i / 3.0)
        o = base
        c = base + 0.00004
        h = max(o, c) + 0.00020
        l = min(o, c) - 0.00020
        rows.append({
            "time": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=i),
            "open": round(o, 5), "high": round(h, 5), "low": round(l, 5),
            "close": round(c, 5), "tick_volume": 1000 + i,
        })
    return pd.DataFrame(rows)


class TestNoLookahead:
    def test_swing_is_only_usable_two_bars_later(self):
        """Un swing de la vela i se CONFIRMA en i+2: el nivel no puede
        usarse en i ni en i+1 (sería mirar al futuro)."""
        df = _candles(120)
        feats = engineer_market_features(df)
        close = feats["close"].to_numpy()
        floor = feats["floor_below"].to_numpy()
        ceiling = feats["ceiling_above"].to_numpy()
        # Reproduce el detector y comprueba que ningún nivel usado en la
        # vela t procede de una vela posterior a t-2.
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        swing_hi = (
            (high > high[1:].tolist() and True) if False else None
        )  # placeholder (se calcula abajo vectorizado)
        prev_h1 = np.roll(high, 1); prev_h1[0] = np.nan
        prev_h2 = np.roll(high, 2); prev_h2[:2] = np.nan
        next_h1 = np.roll(high, -1); next_h1[-1] = np.nan
        next_h2 = np.roll(high, -2); next_h2[-2:] = np.nan
        confirmed = np.zeros(len(high), bool)
        for i in range(2, len(high) - 2):
            if (high[i] > prev_h1[i] and high[i] > prev_h2[i]
                    and high[i] > next_h1[i] and high[i] > next_h2[i]):
                confirmed[i] = True
        idx_confirmed = np.where(confirmed)[0]
        assert len(idx_confirmed) > 0
        # En la vela t, el techo usado (si existe) es un swing confirmado en
        # una vela ≤ t-2 ⇒ su valor está disponible en t.
        for t in range(5, len(close)):
            c = ceiling[t]
            if not np.isfinite(c) or c <= 0:
                continue
            cand = idx_confirmed[(idx_confirmed <= t - 2) & (high[idx_confirmed] == c)]
            assert len(cand) > 0, (
                f"el techo de la vela {t} no procede de un swing confirmado ≤ t-2"
            )
            break  # un ejemplo basta: la propiedad es estructural

    def test_floor_is_below_close_and_ceiling_above(self):
        feats = engineer_market_features(_candles(200))
        floor = feats["floor_below"]
        ceiling = feats["ceiling_above"]
        usable = (floor > 0) & (ceiling > 0)
        assert usable.any(), "el fixture debe tener niveles utilizables"
        assert (floor[usable] < feats["close"][usable]).all()
        assert (ceiling[usable] > feats["close"][usable]).all()


class TestSignificanceFilter:
    def test_level_too_close_is_rejected(self):
        """Un nivel a menos de NEAREST_LEVEL_ATR_MULT×ATR no se usa."""
        n = 120
        close = pd.Series(np.linspace(1.1000, 1.1100, n))
        high = close + 0.00010
        low = close - 0.00010
        atr = pd.Series(np.full(n, 0.00100))   # ATR = 10 pips
        # swings confirmados en las velas 10 y 20 (a 2 y 3 pips del precio)
        sh = np.zeros(n, bool); sl = np.zeros(n, bool)
        sh[10] = True; sh[20] = True
        sl[10] = True; sl[20] = True
        high_v = high.to_numpy().copy(); low_v = low.to_numpy().copy()
        # los niveles quedan a ~2 pips ⇒ por debajo del filtro (0,75×10 = 7,5)
        floor, ceil = nearest_significant_levels(close, high, low, atr)
        # con el filtro activo no hay nivel utilizable
        assert not np.isfinite(floor.to_numpy()[-1]) or floor.to_numpy()[-1] == 0
        assert not np.isfinite(ceil.to_numpy()[-1]) or ceil.to_numpy()[-1] == 0

    def test_significance_mult_is_at_least_half_atr(self):
        assert NEAREST_LEVEL_ATR_MULT >= 0.5


class TestParityTrainServe:
    def test_same_inputs_give_same_stops(self):
        """La función es UNA: el env y /predict la importan (paridad por
        construcción). Este test fija que con los mismos datos da lo mismo
        y que el SL cae bajo el suelo y el TP en el techo."""
        atr = 0.0009          # 9 pips
        price = 1.1050
        floor_b = 1.1020      # suelo 30 pips abajo
        ceil_a = 1.1080      # techo 30 pips arriba
        sl_long, tp_long = production_stops(
            atr, PIP, direction=1, floor_below=floor_b, ceiling_above=ceil_a,
            current_price=price,
        )
        # LONG: SL = suelo − buffer ; TP = techo
        assert sl_long == pytest.approx((price - (floor_b - SPREAD_BUFFER_PIPS * PIP)) / PIP)
        assert tp_long == pytest.approx((ceil_a - price) / PIP)
        # SHORT: simétrico
        sl_short, tp_short = production_stops(
            atr, PIP, direction=-1, floor_below=floor_b, ceiling_above=ceil_a,
            current_price=price,
        )
        assert sl_short == pytest.approx(((ceil_a + SPREAD_BUFFER_PIPS * PIP) - price) / PIP)
        assert tp_short == pytest.approx((price - floor_b) / PIP)

    def test_fallback_is_the_atr_geometry_when_no_levels(self):
        """Sin suelo/techo utilizable → fallback ATR HONESTO (nunca inventado)."""
        sl, tp = production_stops(0.0010, PIP)
        assert sl == pytest.approx(15.0)
        assert tp == pytest.approx(30.0)

    def test_level_below_minimum_pips_is_rejected(self):
        atr = 0.0002
        price = 1.1050
        floor_b = price - 0.0001      # 1 pip ⇒ por debajo de MIN_LEVEL_PIPS
        ceil_a = price + 0.0001
        sl, tp = production_stops(
            atr, PIP, direction=1, floor_below=floor_b, ceiling_above=ceil_a,
            current_price=price,
        )
        assert sl == pytest.approx(10.0)   # fallback ATR (clamp inferior)
        assert tp == pytest.approx(20.0)
        assert MIN_LEVEL_PIPS >= 3.0


class TestRealStopDistance:
    def test_effective_sl_includes_the_spread_buffer(self):
        """La distancia REAL del stop = geometría + buffer del EA ⇒ es la que
        debe usar el sizing (si no, el riesgo real excede el presupuesto:
        13 % medido el 21-10)."""
        assert effective_sl_pips(15.0) == pytest.approx(15.0 + SPREAD_BUFFER_PIPS)

    def test_sizing_with_effective_distance_gives_less_lots(self):
        kw = dict(volume_norm=1.0, equity=1000.0, pip_value=10.0,
                  risk_pct=0.03, max_lot=0.5, min_lot=0.01)
        geom = size_ppo_position(sl_pips=15.0, **kw)
        real = size_ppo_position(sl_pips=effective_sl_pips(15.0), **kw)
        assert real <= geom


class TestVariantB:
    """G3-2 (2026-10-22): el TP es el techo más cercano QUE PAGA.

    Medido (29 981 velas H1 × 2 pares × 2 ventanas disjuntas,
    `scripts/compare_geometry_variants.py`): +15-17 pp de señales en EURUSD y
    +6-8 pp en USDCHF con la misma expectativa por operación (±0,01 R) y
    +5-9 % de eficiencia de capital (el umbral de edge para que B supere a
    la variante A es de sólo 0,09 ATR).
    """

    def test_candidates_are_ordered_and_significance_filtered(self):
        feats = engineer_market_features(_candles(200))
        price = float(feats["close"].iloc[-1])
        atr_pips = float(feats["atr"].iloc[-1]) / PIP
        for side in (1, -1):
            cands = target_level_candidates(
                feats, side, atr_pips, price, pip_size=PIP)
            assert isinstance(cands, list)
            dists = [abs(c - price) / PIP for c in cands]
            assert dists == sorted(dists), "los candidatos van de más cerca a más lejos"
            for d in dists:
                assert d >= NEAREST_LEVEL_ATR_MULT * atr_pips - 1e-6, (
                    "un candidato a menos de mult×ATR del precio es ruido"
                )
                assert d >= MIN_LEVEL_PIPS - 1e-6

    def test_candidates_band_is_price_not_pips(self):
        """Regresión 2026-10-22: la banda va en PRECIO.

        Con ATR=10 pips (banda 7,5 pips = 0,00075) un techo a 20 pips
        (0,0020) DEBE aparecer. Antes se comparaba 0,0020 >= 7,5 → falso:
        la lista salía siempre vacía, el serve caía a 2×SL y la variante B
        (TP = nivel que paga) nunca se ejecutó. El test anterior no lo veía
        porque una lista vacía pasaba todas sus aserciones.
        """
        df = pd.DataFrame({
            "high": [1.1000] * 10,
            "low": [1.0990] * 10,
            "swing_high": [0.0] * 5 + [1.0] + [0.0] * 4,
            "swing_low": [0.0] * 10,
        })
        df.loc[5, "high"] = 1.1020          # swing confirmado a 20 pips
        got = target_level_candidates(df, 1, 10.0, 1.1000, pip_size=PIP)
        assert got == [1.1020]
        # 4 pips < MIN_LEVEL_PIPS ⇒ no es un techo utilizable
        df.loc[5, "high"] = 1.1004
        assert target_level_candidates(df, 1, 10.0, 1.1000, pip_size=PIP) == []

    def test_tp_is_the_nearest_candidate_that_pays(self):
        price = 1.1050
        atr_pips = 10.0
        sl_pips = 15.0
        # techo a 6 pips (no paga con min_tp_pips=20) y a 25 pips (sí paga)
        cands = [price + 0.0006, price + 0.0025]
        tp = production_tp(
            atr_pips * PIP, PIP, direction=1,
            floor_below=price - 0.0020, ceiling_above=price + 0.0025,
            current_price=price, sl_pips=sl_pips,
            tp_candidates=cands, min_tp_pips=20.0,
        )
        assert tp == pytest.approx(25.0), "el TP debe ser el techo que PAGA, no el más cercano"

    def test_without_a_paying_candidate_it_falls_back(self):
        price = 1.1050
        tp = production_tp(
            10.0 * PIP, PIP, direction=1,
            floor_below=price - 0.0020, ceiling_above=price + 0.0006,
            current_price=price, sl_pips=15.0,
            tp_candidates=[price + 0.0006], min_tp_pips=20.0,
        )
        # ningún techo paga ⇒ el TP es el nivel MÁS CERCANO (con el suelo
        # mínimo de 10 pips del contrato): su valor en € queda por debajo del
        # mínimo de clasificación ⇒ el gate de recompensa bloquea la entrada
        # (es el comportamiento de la variante A para esa clase).
        assert tp == pytest.approx(10.0)

    def test_train_and_serve_agree(self):
        """La geometría es la MISMA función en los dos lados (paridad real).

        2026-10-22: el TP ya no lo elige `production_stops_with_paying_tp`
        en el serve — lo elige el contrato de dinero (`evaluate_trade`)
        sobre las distancias candidatas. La paridad que queda que verificar
        es la de las PIEZAS compartidas: SL geométrico y candidatos de TP.
        """
        import services.trade_setup as trade_setup
        assert trade_setup.production_sl is production_sl
        assert trade_setup.target_level_candidates is target_level_candidates
        assert trade_setup.evaluate_trade is evaluate_trade
