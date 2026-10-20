"""
G3 (2026-10-20) · LA FEATURE QUE FALTABA: momentum de 24 h.

CAUSA RAÍZ (medida): la reward `v3_edge` premia la dirección a +24 h
(`REWARD_V3_DIRECTION_HORIZON_BARS = 24`) pero el obs del PPO sólo traía
retornos de 1/2/3/5 velas ⇒ el agente estaba GRADUADO en una magnitud que
NO VEÍA. En USDCHF, además, la única señal con skill real medido es
precisamente el momentum de 24 h (+7,1 pp, n=481, IC [+2,6,+11,4], p=0,001).

Este fichero fija:
  1. el motor calcula `lag_return_24` (el retorno de 24 velas atrás);
  2. el bloque opt-in de tendencia lo incluye (17 base + 4 = 21 market);
  3. el contrato por ancho refleja el bloque ampliado (28/29 trend,
     32/33 T-30) y el contrato VIVO (24/25) NO se toca;
  4. el canario del retrain se activa con un flag EXPLÍCITO y su default
     es inerte (el bundle vivo de EURUSD sigue byte-idéntico);
  5. el serve deriva el contrato del ancho del bundle (paridad con el
     retrain) ⇒ un bundle de 29 columnas se sirve con el bloque de
     tendencia.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.trading_env_v2 import (
    EXTRA_STATE_FEATURES,
    FOREX_MARKET_FEATURE_COUNT,
    MARKET_FEATURES,
    TREND_MARKET_FEATURES,
    derive_obs_contract,
    engineer_market_features,
    get_market_features,
)


def _df_candles(n: int = 300) -> "pd.DataFrame":  # noqa: F821
    import pandas as pd
    rng = np.random.default_rng(7)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0006, n))
    ts = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({
        "time": ts, "open": close, "high": close + 0.0004,
        "low": close - 0.0004, "close": close,
        "volume": np.full(n, 1000.0),
    })


class TestMomentum24Feature:
    def test_engine_computes_lag_return_24(self):
        df = engineer_market_features(_df_candles(300))
        assert "lag_return_24" in df.columns
        # el valor es el retorno de 24 velas atrás (mismo hecho, mismo signo)
        rets = df["returns"].to_numpy()
        lag = df["lag_return_24"].to_numpy()
        # shift(24) sobre la serie FINAL (el motor recorta el warm-up al
        # final): el valor en i es el retorno de la vela i-24.
        assert np.allclose(lag[24:], rets[:-24])

    def test_momentum_is_in_the_opt_in_trend_block_not_the_base(self):
        """El bloque VIVO (17 market) no cambia: el bundle de EURUSD sigue
        byte-idéntico. La feature nueva entra por el bloque opt-in."""
        assert "lag_return_24" not in MARKET_FEATURES
        assert "lag_return_24" in TREND_MARKET_FEATURES
        assert len(MARKET_FEATURES) == FOREX_MARKET_FEATURE_COUNT == 17

    def test_trend_vector_is_17_plus_4(self):
        df = engineer_market_features(_df_candles(300))
        features = get_market_features(df, include_trend=True)
        assert features == list(MARKET_FEATURES) + list(TREND_MARKET_FEATURES)
        assert len(features) == 21


class TestContractWidths:
    def test_live_contract_untouched(self):
        """24/25 = el contrato vivo de EURUSD (byte-idéntico)."""
        assert derive_obs_contract(24) == (False, False, False)
        assert derive_obs_contract(25) == (False, False, True)

    def test_trend_contract_is_28_29_after_g3(self):
        assert derive_obs_contract(28) == (True, False, False)
        assert derive_obs_contract(29) == (True, False, True)

    def test_higher_tf_contract_is_32_33(self):
        assert derive_obs_contract(32) == (True, True, False)
        assert derive_obs_contract(33) == (True, True, True)

    def test_unknown_width_is_rejected(self):
        for w in (None, 0, 23, 26, 27, 30, 31, 34, 99):
            assert derive_obs_contract(w) is None, w

    def test_width_arithmetic_is_consistent(self):
        base = FOREX_MARKET_FEATURE_COUNT + EXTRA_STATE_FEATURES
        assert base == 24
        assert base + len(TREND_MARKET_FEATURES) == 28


class TestCanaryFlagIsInertByDefault:
    def test_default_is_off(self):
        from config import settings

        assert settings.retrain_trend_features is False
        assert settings.higher_tf_features is False

    def test_retrain_uses_the_flag_without_touching_the_live_default(self):
        """La línea revisable de T-27 se conserva literal y el canario
        sólo se enciende con el flag explícito."""
        from pathlib import Path as _P

        src = (_P(__file__).resolve().parents[1] / "services" / "auto_retrain.py").read_text(
            encoding="utf-8")
        assert "trend_features=settings.higher_tf_features" in src
        assert "settings.retrain_trend_features" in src
