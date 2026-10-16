"""
T-22 (2026-10-16, orden «GO T-22»): bloque de tendencia compartido PPO↔LLM.

REQ-1 — el PPO y el LLM responden las MISMAS preguntas con la MISMA
información; REQ-2 — el PPO es fiable con y sin LLM. Evidencia y spec:
`audits/trend-feature-store-design-2026-10-16.md` (T-21, decisión Félix
«Opción (i) H1-only + (a) narrativa no-decisiva»).

Propiedades que fija cada clase:

1. Contrato → `derive_obs_contract` mapea los 4 anchos (24/25/27/28) y nada
   más; `get_market_features(include_trend=True)` añade las 3 features al
   FINAL del vector de 17 base (MARKET_FEATURES intacta → bundle vivo (20,25)
   byte-idéntico).
2. Engineer → ventana corta (120) conserva el contrato de filas de siempre
   (19 de warm-up) con relleno neutro 0.0 (sin esto dropna vaciaría el df);
   ventana larga tiene SMA200 REAL desde la fila 204 y sus últimas 20 filas
   igualan la referencia raw (nunca relleno en lo que ve el obs).
3. Alineación REQ-1 → los números del bloque trend del obs reconstructan
   EXACTAMENTE los que `_sma200_context` (fuente del prompt del LLM) lanza
   con la misma serie: mismo hecho, mismo signo.
4. Env → (20,25) por defecto y (20,28) con trend_features=True, con los
   canales 17..19 = dist_sma200/sma200_slope/adx de la ventana.
5. Eval del retrain → `_evaluate_model` construye el env con el contrato que
   declara el modelo (27/28 → env trend); ancho desconocido = RuntimeError.
6. Serve → contrato trend usa ventana FIJA TREND_FEATURE_WINDOW_MIN (SMA200
   honesto + obs byte-idéntico con cualquier profundidad de fetch ≥224) y
   HOLD explícito con datos insuficientes o sin `time` (cero inventado).
"""
from unittest.mock import MagicMock, patch

import gymnasium as gym
import numpy as np
import pandas as pd
import pytest
from gymnasium.spaces import Box

import routers.ai
from ml.trading_env_v2 import (
    MARKET_FEATURES,
    TREND_FEATURE_WINDOW_MIN,
    TREND_MARKET_FEATURES,
    ForexTradingEnvV2,
    derive_obs_contract,
    engineer_market_features,
    get_market_features,
)
from routers.ai import Brain
from tests.test_pack_1_4_prompt_context import (
    _FakeMT5,
    _candles,
    _model,
    _post,
    _serve,
)


def _df_candles(n: int = 300, base: float = 1.10, step: float = 0.0002) -> pd.DataFrame:
    """Velas H1 ASCENDENTES con rampa + ondulación (tendencia real no plana)."""
    n_ = np.arange(n)
    close = base + n_ * step + np.sin(n_ / 7.0) * step * 4.0
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="h"),
        "open": close + 0.0001,
        "high": close + 0.0004,
        "low": close - 0.0004,
        "close": close,
        "tick_volume": 1000 + n_,
        "spread": 1.5,
    })


# ─── 1. Contrato por ancho ────────────────────────────────────────────────────

class TestObsContract:
    def test_derive_obs_contract_maps_the_four_known_widths(self):
        assert derive_obs_contract(24) == (False, False)   # pre-memoria legado
        assert derive_obs_contract(25) == (False, True)    # contrato VIVO (20,25)
        assert derive_obs_contract(27) == (True, False)    # trend pre-memoria
        assert derive_obs_contract(28) == (True, True)     # objetivo T-27 (20,28)

    def test_derive_obs_contract_rejects_everything_else(self):
        for width in (None, 0, 23, 26, 29, 30, 99):
            assert derive_obs_contract(width) is None, width

    def test_trend_features_appended_after_the_17_base(self):
        df = _df_candles(300)
        base = get_market_features(df)
        assert base == list(MARKET_FEATURES)
        assert len(base) == 17
        trend = get_market_features(df, include_trend=True)
        assert trend == list(MARKET_FEATURES) + list(TREND_MARKET_FEATURES)
        assert len(trend) == 20

    def test_engineer_creates_trend_columns_for_every_path(self):
        # Ventana corta (ruta serve bundle vivo) y larga (entrenamiento):
        # las columnas existen SIEMPRE (get_market_features no rellena huecos).
        short = engineer_market_features(_df_candles(120))
        long = engineer_market_features(_df_candles(300))
        for col in TREND_MARKET_FEATURES:
            assert col in short.columns and col in long.columns


# ─── 2. engineer_market_features: honestidad del warm-up ──────────────────────

class TestEngineerTrendBlock:
    def test_short_window_keeps_row_contract_and_fills_neutral(self):
        """120 velas → 101 filas (mismo warm-up de 19 de siempre: el nuevo
        bloque NO añade descartes — si lo hiciera, dropna vaciaría la ruta
        serve del bundle vivo). Rellen0 0.0 = «precio EN la media, pendiente
        plana» (mismo patrón que adx→0.5), y esas columnas no entran al obs
        de 17 canales."""
        out = engineer_market_features(_df_candles(120))
        assert len(out) == 120 - 19
        assert (out["dist_sma200"] == 0.0).all()
        assert (out["sma200_slope"] == 0.0).all()

    def test_long_window_is_honest_from_warmup_end_and_last20_matches_raw(self):
        """Ventana larga: SMA200 real desde la fila 204 (199 del rolling + 5
        de la pendiente) — a partir de ahí CERO relleno, y las últimas 20
        filas (lo que ve el obs) igualan la referencia calculada sobre el
        raw crudo."""
        raw = _df_candles(300)
        out = engineer_market_features(raw.copy())
        assert len(out) == 300 - 19   # sólo el warm-up de siempre

        close = raw["close"].astype(float)
        sma = close.rolling(200).mean()
        ref_dist = ((close - sma) / (close + 1e-10)).clip(-0.1, 0.1)
        ref_slope = ((sma - sma.shift(5)) / (close + 1e-10)).clip(-0.1, 0.1)

        # fila 204 del raw en adelante = honesta (nunca inventado en train)
        np.testing.assert_allclose(
            out["dist_sma200"].iloc[204 - 19:].values,
            ref_dist.iloc[204:].values, atol=1e-12,
        )
        np.testing.assert_allclose(
            out["sma200_slope"].iloc[204 - 19:].values,
            ref_slope.iloc[204:].values, atol=1e-12,
        )
        # las últimas 20 filas del obs: SMA200 real, rampa alcista → signo +
        assert (out["dist_sma200"].iloc[-20:] > 0).all()
        assert (out["sma200_slope"].iloc[-20:] > 0).all()
        # warm-up relleno con neutro en las primeras filas (visible, no oculto)
        assert out["dist_sma200"].iloc[0] == 0.0
        assert out["sma200_slope"].iloc[0] == 0.0


# ─── 3. Alineación REQ-1: mismo hecho, mismo signo, misma fuente ──────────────

class TestPpoLlmShareTheSameTrendFact:
    def test_trend_features_reconstruct_the_llm_context_numbers(self):
        """El obs del PPO (normalizado) reconstructa EXACTAMENTE los valores
        crudos que `_sma200_context` entrega al prompt del LLM con la misma
        serie H1: mismo close, mismo rolling(200), mismo shift(5). El signo
        —lo decisivo para la dirección— es idéntico por construcción."""
        raw = pd.DataFrame(_candles(240)).sort_values("time").reset_index(drop=True)
        dist_llm, slope_llm = routers.ai._sma200_context(raw)
        assert dist_llm is not None and slope_llm is not None
        assert dist_llm > 0 and slope_llm > 0   # rampa: ALZA para ambos

        # misma ventana que usa el serve con contrato trend
        eng = engineer_market_features(raw.tail(TREND_FEATURE_WINDOW_MIN).copy())
        c_last = float(eng["close"].iloc[-1])
        d = float(eng["dist_sma200"].iloc[-1])
        s = float(eng["sma200_slope"].iloc[-1])

        assert np.sign(d) == np.sign(dist_llm)
        assert np.sign(s) == np.sign(slope_llm)
        assert d * (c_last + 1e-10) == pytest.approx(dist_llm, rel=1e-6)
        assert s * (c_last + 1e-10) == pytest.approx(slope_llm, rel=1e-6)

    def test_adx_is_the_shared_quantity_looked_at_in_different_resolution(self):
        """`adx` es ÚNICA: la misma columna entra al obs del PPO (canal 19)
        y alimenta `df['adx']` del prompt/memoria — misma cantidad [0,1],
        distinta resolución (el régimen categórico del LLM es su rendering)."""
        raw = _df_candles(300)
        out = engineer_market_features(raw)
        assert "adx" in TREND_MARKET_FEATURES
        adx = out["adx"]
        assert adx.notna().all() and (adx >= 0).all() and (adx <= 1).all()


# ─── 4. Env: contratos (20,25) y (20,28) ─────────────────────────────────────

class TestEnvTrendContract:
    @staticmethod
    def _honest_df() -> pd.DataFrame:
        # sin warm-up: el obs nunca debe verse con tendencia de relleno
        return engineer_market_features(_df_candles(300)).iloc[204:].reset_index(drop=True)

    def test_default_env_is_the_live_contract_20_25(self):
        df = self._honest_df()
        env = ForexTradingEnvV2(df=df, random_reset=False, max_episode_steps=50)
        obs = env.reset()[0]
        assert env.observation_space.shape == (20, 25)
        assert obs.shape == (20, 25)
        assert env.features == list(MARKET_FEATURES)

    def test_trend_env_is_20_28_with_trend_channels_at_17_19(self):
        df = self._honest_df()
        env = ForexTradingEnvV2(
            df=df, random_reset=False, max_episode_steps=50, trend_features=True,
        )
        obs = env.reset()[0]
        assert env.observation_space.shape == (20, 28)
        assert obs.shape == (20, 28) and obs.dtype == np.float32
        assert env.features == list(MARKET_FEATURES) + list(TREND_MARKET_FEATURES)
        # canales 17..19 = dist_sma200 / sma200_slope / adx de la ventana 1..20
        np.testing.assert_allclose(obs[:, 17], df["dist_sma200"].iloc[1:21].values, rtol=1e-5, atol=1e-7)
        np.testing.assert_allclose(obs[:, 18], df["sma200_slope"].iloc[1:21].values, rtol=1e-5, atol=1e-7)
        np.testing.assert_allclose(obs[:, 19], df["adx"].iloc[1:21].values, rtol=1e-5, atol=1e-7)
        # el canal de tendencia no está degenerado a 0 (rampa real)
        assert abs(obs[-1, 17]) > 1e-4
        next_obs, *_ = env.step(np.array([0.1, 0.5], dtype=np.float32))
        assert next_obs.shape == (20, 28)


# ─── 5. Eval del retrain: contrato derivado del observation_space ─────────────

class TestEvaluateModelTrendContract:
    @staticmethod
    def _recorder(width: int):
        class _Recorder:
            def __init__(self):
                self.observation_space = gym.spaces.Box(
                    -np.inf, np.inf, shape=(20, width), dtype=np.float32
                )
                self.seen = []

            def predict(self, observation, deterministic=True):
                self.seen.append(tuple(np.asarray(observation).shape))
                return np.zeros(4, dtype=np.float32), None

        return _Recorder()

    @staticmethod
    def _cfg(df: pd.DataFrame) -> dict:
        return dict(
            df=df, window_size=20, initial_balance=1000.0,
            max_episode_steps=30, max_holding_steps=20, random_reset=True,
            real_outcomes=[],
        )

    @pytest.mark.parametrize("width,expected", [
        (27, (1, 20, 27)),
        (28, (1, 20, 28)),
    ])
    def test_trend_models_are_evaluated_in_their_own_env(self, width, expected):
        from services.auto_retrain import _evaluate_model
        df = engineer_market_features(_df_candles(300))
        model = self._recorder(width)
        metrics = _evaluate_model(model, df, self._cfg(df))
        assert model.seen and model.seen[0] == expected
        assert "trade_pnls" in metrics

    def test_unknown_contract_still_raises(self):
        from services.auto_retrain import _evaluate_model
        df = engineer_market_features(_df_candles(300))
        with pytest.raises(RuntimeError, match="Contrato de observación desconocido"):
            _evaluate_model(self._recorder(26), df, self._cfg(df))


# ─── 6. Serve: ventana trend, byte-idéntico y guardas ─────────────────────────

def _trend_brain(model: MagicMock) -> Brain:
    return Brain(
        model=model, vec_norm=None, bundle={},
        memory_channel=True, trend_features=True,
    )


class TestServeTrendBundle:
    @staticmethod
    def _trend_model() -> MagicMock:
        model = _model()   # (20,25) para la validación de manifest del startup
        model.observation_space = Box(-np.inf, np.inf, shape=(20, 28), dtype=np.float32)
        return model

    def test_fixed_window_and_obs_20_28(self, monkeypatch):
        """Contrato trend → features sobre la ventana FIJA 224 (SMA200 real en
        las últimas 20 velas del obs) y obs (20,28) con los canales de
        tendencia presentes y no degenerados."""
        startup_model = _model()
        with _serve(monkeypatch, startup_model, _candles(240)) as (client, *_):
            trend_model = self._trend_model()
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD", _trend_brain(trend_model),
            )
            with patch(
                "routers.ai.engineer_market_features",
                wraps=engineer_market_features,
            ) as eng:
                resp = _post(client, "t22-trend-window")
            assert resp.status_code == 200, resp.text
            # call [0] SIEMPRE es el del modelo (features_input) — el H4/D1
            # viene después (mismo orden que fija test_pack_1_4).
            assert eng.call_args_list[0].args[0].shape[0] == TREND_FEATURE_WINDOW_MIN

        obs = trend_model.predict.call_args.args[0]
        assert obs.shape == (20, 28)
        assert abs(float(obs[-1, 17])) > 1e-4      # dist_sma200 honesto
        assert abs(float(obs[-1, 18])) > 1e-6      # sma200_slope honesto
        assert 0.0 <= float(obs[-1, 19]) <= 1.0    # adx en rango

    def test_obs_byte_identical_whatever_fetch_depth(self, monkeypatch):
        """Con cualquier profundidad de fetch ≥224, las features se calculan
        sobre las MISMAS 224 velas recientes → obs byte-idéntico (mismo
        espíritu que el contrato vivo: el EWM del macd tiene memoria infinita
        y con ventanas de distinto tamaño sus ULPs divergen)."""
        startup_model = _model()
        recent = _candles(224, t0="2026-01-01")
        older = _candles(16, t0="2025-12-31T08:00", base=1.05)
        with _serve(monkeypatch, startup_model, recent) as (client, *_):
            trend_model = self._trend_model()
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD", _trend_brain(trend_model),
            )
            r1 = _post(client, "t22-trend-obs-a")
            assert r1.status_code == 200, r1.text
            obs_a = trend_model.predict.call_args.args[0].copy()
            # mismo final de serie + 16 velas viejas por detrás (240 en total)
            _FakeMT5.candles = recent + older
            r2 = _post(client, "t22-trend-obs-b")
            assert r2.status_code == 200, r2.text
            obs_b = trend_model.predict.call_args.args[0].copy()
        assert obs_a.shape == (20, 28)
        assert obs_a.tobytes() == obs_b.tobytes()

    def test_holds_below_min_window_instead_of_serving_filled_trend(self, monkeypatch):
        """<224 velas → el SMA200 no puede ser honesto en las 20 últimas →
        HOLD con error explícito: jamás un obs con tendencia de relleno."""
        startup_model = _model()
        with _serve(monkeypatch, startup_model, _candles(120)) as (client, qwen_mock, *_):
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD", _trend_brain(self._trend_model()),
            )
            resp = _post(client, "t22-trend-short")
            assert resp.status_code == 200, resp.text
            assert resp.json()["decision"] == "HOLD"
            qwen_mock.assert_not_awaited()

    def test_holds_without_time_column_instead_of_rolling_backwards(self, monkeypatch):
        """Sin `time` no hay orden fiable (el MCP devuelve descendente) →
        un SMA200 se calcularía hacia atrás → HOLD, nunca obs corrupto."""
        timeless = [
            {
                "open": 1.10 + i * 1e-4, "high": 1.101 + i * 1e-4,
                "low": 1.099 + i * 1e-4, "close": 1.10 + i * 1e-4,
                "tick_volume": 1000 + i,
            }
            for i in range(100)
        ]
        startup_model = _model()
        with _serve(monkeypatch, startup_model, timeless) as (client, qwen_mock, *_):
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD", _trend_brain(self._trend_model()),
            )
            resp = _post(client, "t22-trend-notime")
            assert resp.status_code == 200, resp.text
            assert resp.json()["decision"] == "HOLD"
            qwen_mock.assert_not_awaited()
