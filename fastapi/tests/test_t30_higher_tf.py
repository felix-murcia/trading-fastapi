"""T-30 — canales de horizonte lento H4/D1 en el obs (contrato 32/33 tras G3).

Pre-registrado en audits/t30-pre-registration-hf-obs-2026-10-17.md. Un SOLO
candidato (dist/slope SMA200 de H4 y D1, sin tuning de umbrales), flag
`higher_tf_features` default off (byte-idéntico para el contrato vivo (20,25)):

1. Join asof estricto de vela CERRADA (`join_higher_tf_channels`): la barra
   superior sólo es usable cuando `bar.time + timeframe ≤ t` (igualdad
   INCLUSIVA), NaN donde no hay cobertura — cero lookahead, cero relleno.
2. Paridad REQ-1: `obs × (close_hf + ε)` reconstruye EXACTAMENTE los valores
   crudos que `_sma200_context` entrega al prompt del LLM (mismo hecho, misma
   serie, misma referencia).
3. Serve: brain (20,32) une los canales desde el fetch P3-4 existente; serie
   no servible (breaker/Exception/<205 velas/gap>168h) o ventana del obs sin
   canales formados → HOLD fail-closed con log explícito (nunca obs de
   relleno). Contratos 24/25/28/29 no se tocan: el bloque no se ejecuta (G3: el bloque trend pasó a 4 features).
4. Train: con el flag on se fetchean H4 (lookback//4+300) y D1
   (lookback//24+500), se unen al df ANTES del trim de warm-up y CERO NaN
   tolerados (RuntimeError explícito antes del snapshot).
5. Sin branching por símbolo (G2): la lógica HF vive en el contrato, no en
   `if symbol == ...`.
"""
import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import gymnasium as gym
import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

import routers.ai
import services.feed_breaker as feed_breaker
from config import Settings
from main import app
from ml.trading_env_v2 import (
    TREND_MARKET_FEATURES,
    HIGHER_TF_MARKET_FEATURES,
    MARKET_FEATURES,
    ForexTradingEnvV2,
    engineer_market_features,
    get_market_features,
    join_higher_tf_channels,
)
from routers.ai import Brain
from tests.test_pack_1_4_prompt_context import (
    _QWEN_OK,
    _candles,
    _model,
    _post,
)


# ─── Datos sintéticos H4/D1 ──────────────────────────────────────────────────

def _h4_candles(n: int = 260, end: str = "2026-01-10T19:00",
                base: float = 1.10, step: float = 0.0002) -> list[dict]:
    """Velas H4 ASCENDENTES (el join argsorta: el orden de entrada da igual;
    el MCP real devuelve DESC) con rampa alcista suave — |dist| < 0.1 (sin
    clip) y slope positivo no nulo en toda la serie."""
    end_ts = pd.Timestamp(end)
    times = pd.date_range(end_ts - pd.Timedelta(hours=4 * (n - 1)),
                          periods=n, freq="4h")
    rows = []
    for i, t in enumerate(times):
        close = round(base + i * step, 5)
        rows.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(close - 0.0001, 5),
            "high": round(close + 0.0002, 5),
            "low": round(close - 0.0002, 5),
            "close": close,
            "tick_volume": 1000 + i,
        })
    return rows


def _d1_candles(n: int = 260, end: str = "2026-01-08T00:00",
                base: float = 1.10, step: float = 0.0005) -> list[dict]:
    """Velas D1 diarias (00:00) con rampa alcista suave — mismo esquema que
    `_h4_candles`. `end` debe ser una barra YA CERRADA a la hora de la última
    fila H1 del test para que la paridad obs↔prompt mire la misma barra."""
    end_ts = pd.Timestamp(end)
    times = pd.date_range(end_ts - pd.Timedelta(days=n - 1), periods=n,
                          freq="D")
    rows = []
    for i, t in enumerate(times):
        close = round(base + i * step, 5)
        rows.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(close - 0.0001, 5),
            "high": round(close + 0.0002, 5),
            "low": round(close - 0.0002, 5),
            "close": close,
            "tick_volume": 1000 + i,
        })
    return rows


def _h1_df(times) -> pd.DataFrame:
    """df H1 mínimo para el join (sólo `time` y `close` son leídos)."""
    return pd.DataFrame({
        "time": [pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ") for t in times],
        "close": 1.10,
    })


# ─── Serve: _FakeMT5 con dispatch por timeframe ──────────────────────────────

class _FakeMT5:
    """httpx.AsyncClient fake de /predict: equity, H1 (candles) y H4/D1
    (candles_by_tf) — con `fail_timeframes` para simular el MCP caído."""

    candles: list = []
    candles_by_tf: dict = {}
    fail_timeframes: set = set()
    requests: list = []

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).requests.append(url)
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            tf = "H1"
            for cand in ("H4", "D1", "H1"):
                if f"timeframe={cand}" in url:
                    tf = cand
                    break
            if tf in type(self).fail_timeframes:
                raise httpx.ConnectTimeout(f"{tf} caído")
            if tf == "H1":
                return self._response(type(self).candles)
            return self._response(type(self).candles_by_tf.get(tf, []))
        return self._response({})

    async def post(self, url, **kwargs):
        return self._response({})

    @staticmethod
    def _response(payload, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = json.dumps(payload)
        return response


@contextmanager
def _serve(monkeypatch, model, candles, h4=(), d1=(), qwen=None,
           fail_timeframes=()):
    """Stack de /predict espejo del pack 1-4 (Qwen/calendario mockados) con
    series H4/D1 por timeframe. El brain bajo prueba se inyecta DESPUÉS del
    enter vía setitem (mismo patrón que test_t22)."""
    _FakeMT5.candles = candles
    _FakeMT5.candles_by_tf = {"H4": list(h4), "D1": list(d1)}
    _FakeMT5.fail_timeframes = set(fail_timeframes)
    _FakeMT5.requests = []
    qwen_mock = AsyncMock(return_value=dict(_QWEN_OK if qwen is None else qwen))
    with patch("main.init_pool", AsyncMock()), \
         patch("main.close_pool", AsyncMock()), \
         patch("routers.ai.is_news_blackout", AsyncMock(return_value=(False, None))), \
         patch("routers.ai.fetch_bars_since_close", AsyncMock(return_value=None)), \
         patch("routers.ai.get_pool", return_value=MagicMock(execute=AsyncMock())), \
         patch("routers.ai.PPO.load", return_value=model), \
         patch("services.news_scraper.get_macro_news", AsyncMock(return_value="No macro news")), \
         patch("routers.ai.query_qwen_unified", qwen_mock), \
         patch("routers.ai.get_upcoming_news", AsyncMock(return_value=[])), \
         patch("httpx.AsyncClient", _FakeMT5):
        with TestClient(app) as client:
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD",
                Brain(model=model, vec_norm=None, bundle={}, memory_channel=True),
            )
            yield client, qwen_mock


def _hf_model(width: int = 33) -> MagicMock:
    """Modelo serve para el brain bajo prueba: obs_space del contrato T-30."""
    model = _model()
    model.observation_space = Box(-np.inf, np.inf, shape=(20, width),
                                  dtype=np.float32)
    return model


def _hf_brain(model: MagicMock) -> Brain:
    return Brain(
        model=model, vec_norm=None, bundle={},
        memory_channel=True, trend_features=True, higher_tf_features=True,
    )


@pytest.fixture(autouse=True)
def _fresh_breaker():
    """Estado del breaker H4/D1 aislado por test (módulo con estado global)."""
    feed_breaker.h4d_breaker_reset()
    yield
    feed_breaker.h4d_breaker_reset()


# ─── 1. Join asof estricto: cero lookahead ───────────────────────────────────

class TestJoinStrictClosedBarAsof:
    def test_full_vs_truncated_series_identical_before_close_differ_at_close(self):
        """Sin-lookahead quirúrgico: quitar la ÚLTIMA barra H4 deja intactas
        las filas anteriores a su cierre (la feature no mira al futuro) y
        cambia SÓLO la fila del cierre exacto (igualdad inclusiva: la vela
        nueva entra justo en su cierre, ni antes)."""
        h4 = _h4_candles(260, end="2026-01-10T19:00")
        # salto alcista en la última barra: la diferencia de dist es grande
        # y de signo inequívoco (rampa pura la haría de 2º orden).
        h4[-1]["close"] = round(float(h4[-1]["close"]) + 0.05, 5)
        h4_asc = sorted(h4, key=lambda c: c["time"])
        close_ts = pd.Timestamp(h4_asc[-1]["time"]) + pd.Timedelta(hours=4)
        df = _h1_df([
            close_ts - pd.Timedelta(hours=2),
            close_ts - pd.Timedelta(hours=1),
            close_ts,                      # cierre EXACTO de la última barra
            close_ts + pd.Timedelta(hours=1),
        ])
        full = join_higher_tf_channels(df, h4, _d1_candles())
        trunc = join_higher_tf_channels(df, h4_asc[:-1], _d1_candles())
        # antes del cierre: idéntico byte a byte (misma vela cerrada, misma sma)
        np.testing.assert_array_equal(
            full["dist_sma200_h4"].iloc[:2].values,
            trunc["dist_sma200_h4"].iloc[:2].values,
        )
        np.testing.assert_array_equal(
            full["slope_sma200_h4"].iloc[:2].values,
            trunc["slope_sma200_h4"].iloc[:2].values,
        )
        # en el cierre exacto: el full ve la barra nueva (dist salta) y el
        # truncado sigue en la anterior — la feature cambia POR la vela nueva
        assert full["dist_sma200_h4"].iloc[2] > trunc["dist_sma200_h4"].iloc[2]
        # tras el cierre: ambas se estancan en SU última barra cerrada
        assert full["dist_sma200_h4"].iloc[3] == full["dist_sma200_h4"].iloc[2]
        assert trunc["dist_sma200_h4"].iloc[3] == trunc["dist_sma200_h4"].iloc[2]
        assert trunc["dist_sma200_h4"].iloc[2] == trunc["dist_sma200_h4"].iloc[1]

    def test_d1_blocks_full_24h_and_nan_before_first_close(self):
        """Bloque asof de 24 h (R3): la vela D1 de hoy NO entra hasta mañana
        00:00 — las filas de hoy 03:00 y 23:00 ven MISMA vela (la de ayer) y
        la de mañana 00:00 ya ve la de hoy (igualdad inclusiva). Mucho antes
        de la primera vela cerrada: NaN (nunca relleno)."""
        d1 = _d1_candles(260, end="2026-01-09T00:00")   # última barra = 01-08
        day_d = pd.Timestamp("2026-01-08")              # su vela cierra mañana
        df = _h1_df([
            day_d + pd.Timedelta(hours=3),
            day_d + pd.Timedelta(hours=23),
            day_d + pd.Timedelta(hours=24),             # 01-09T00:00
            pd.Timestamp("2025-01-01"),                 # antes de toda vela
        ])
        out = join_higher_tf_channels(df, _h4_candles(), d1)
        # hoy 03:00 y hoy 23:00: la vela de hoy sigue ABierta → misma vela
        assert out["dist_sma200_d1"].iloc[0] == out["dist_sma200_d1"].iloc[1]
        assert out["slope_sma200_d1"].iloc[0] == out["slope_sma200_d1"].iloc[1]
        # mañana 00:00: la vela de hoy CIERRA exactamente ahora → entra
        assert out["dist_sma200_d1"].iloc[2] != out["dist_sma200_d1"].iloc[1]
        # sin ninguna vela D1 cerrada: NaN (jamás «sin tendencia» inventado)
        assert pd.isna(out["dist_sma200_d1"].iloc[3])
        assert pd.isna(out["slope_sma200_d1"].iloc[3])

    def test_join_rejects_timeless_df_and_empty_series(self):
        no_time = pd.DataFrame({"close": [1.1, 1.2]})
        with pytest.raises(ValueError, match="columna `time`"):
            join_higher_tf_channels(no_time, _h4_candles(), _d1_candles())
        df = _h1_df(pd.date_range("2026-01-05", periods=4, freq="h"))
        with pytest.raises(ValueError, match="sin velas H4"):
            join_higher_tf_channels(df, [], _d1_candles())
        with pytest.raises(ValueError, match="sin velas D1"):
            join_higher_tf_channels(df, _h4_candles(), [])


# ─── 2. Paridad REQ-1: obs × (close + ε) = números crudos del prompt ─────────

class TestParityObsVsPrompt:
    def test_hf_channels_reconstruct_the_llm_context_numbers(self):
        """El canal del obs (normalizado por el close de la barra SUPERIOR)
        multiplica exactamente a los (dist, slope) crudos que `_sma200_context`
        entrega al prompt con la MISMA serie — mismo rolling(200), mismo
        shift(5), misma barra (la última cerrada)."""
        h4 = _h4_candles(260, end="2026-01-10T19:00")
        last_close = float(max(h4, key=lambda c: c["time"])["close"])
        # única fila H1: el cierre efectivo de la última barra H4
        close_ts = pd.Timestamp(max(h4, key=lambda c: c["time"])["time"]) \
            + pd.Timedelta(hours=4)
        out = join_higher_tf_channels(_h1_df([close_ts]), h4, _d1_candles())

        h4_asc = sorted(h4, key=lambda c: c["time"])
        dist_raw, slope_raw = routers.ai._sma200_context(pd.DataFrame(h4_asc))
        assert dist_raw is not None and slope_raw is not None
        assert dist_raw > 0 and slope_raw > 0        # rampa: ALZA para ambos

        obs_dist = float(out["dist_sma200_h4"].iloc[0]) * (last_close + 1e-10)
        obs_slope = float(out["slope_sma200_h4"].iloc[0]) * (last_close + 1e-10)
        np.testing.assert_allclose([obs_dist, obs_slope],
                                   [dist_raw, slope_raw], rtol=1e-9)
        # el SIGNO —lo decisivo para la dirección— es idéntico por construcción
        assert (obs_dist > 0) == (dist_raw > 0)
        assert (obs_slope > 0) == (slope_raw > 0)


_HF_LINE = re.compile(
    r"- (H4|D1): Price (?:ABOVE|BELOW) SMA200 \(([+-]?[0-9.]+)\), "
    r"SMA200 (?:RISING|FALLING|FLAT) \(([+-]?[0-9.]+)\)"
)


# ─── 3. Serve: obs (20,32) con paridad vs prompt ─────────────────────────────

class TestServeHigherTfContract:
    def test_serves_obs_20_33_with_parity_to_prompt(self, monkeypatch):
        """Brain (20,32): el serve une los canales H4/D1 desde el mismo fetch
        del prompt y sirve el obs con ellos — los reconstructan EXACTAMENTE
        los {dist}/{slope} crudos de la línea del LLM (tol 6e-5 = render
        +.4f), y los 4 canales están presentes y no degenerados."""
        startup = _model()
        h4 = list(reversed(_h4_candles(260, end="2026-01-10T19:00")))  # DESC real
        d1 = list(reversed(_d1_candles(260, end="2026-01-08T00:00")))
        with _serve(monkeypatch, startup, _candles(240), h4=h4, d1=d1) as (client, qwen_mock):
            hf_model = _hf_model(32)
            monkeypatch.setitem(routers.ai._BRAINS, "EURUSD", _hf_brain(hf_model))
            resp = _post(client, "t30-hf-ok")
            assert resp.status_code == 200, resp.text
            qwen_mock.assert_awaited_once()

        obs = hf_model.predict.call_args.args[0]
        assert obs.shape == (20, 33)
        # canales en los índices 20..23 (17 base + 3 tendencia) y no nulos
        # G3: el bloque de tendencia tiene 4 features ⇒ HF empieza en 21
        _hf_pos = len(MARKET_FEATURES) + len(TREND_MARKET_FEATURES)
        assert abs(float(obs[-1, _hf_pos])) > 1e-3   # dist_sma200_h4 ≈ 0.017
        assert abs(float(obs[-1, _hf_pos + 1])) > 1e-5   # slope_sma200_h4 ≈ 8.7e-4
        assert abs(float(obs[-1, _hf_pos + 2])) > 1e-3   # dist_sma200_d1 ≈ 0.040
        assert abs(float(obs[-1, _hf_pos + 3])) > 1e-5   # slope_sma200_d1 ≈ 2.0e-3

        block = qwen_mock.await_args.kwargs["higher_tf_context"]
        lines = {m.group(1): (float(m.group(2)), float(m.group(3)))
                 for m in _HF_LINE.finditer(block)}
        assert set(lines) == {"H4", "D1"}, block
        c_h4 = float(max(h4, key=lambda c: c["time"])["close"])
        c_d1 = float(max(d1, key=lambda c: c["time"])["close"])
        np.testing.assert_allclose(float(obs[-1, _hf_pos + 0]) * (c_h4 + 1e-10),
                                   lines["H4"][0], atol=6e-5)
        np.testing.assert_allclose(float(obs[-1, _hf_pos + 1]) * (c_h4 + 1e-10),
                                   lines["H4"][1], atol=6e-5)
        np.testing.assert_allclose(float(obs[-1, _hf_pos + 2]) * (c_d1 + 1e-10),
                                   lines["D1"][0], atol=6e-5)
        np.testing.assert_allclose(float(obs[-1, _hf_pos + 3]) * (c_d1 + 1e-10),
                                   lines["D1"][1], atol=6e-5)


# ─── 4. Serve: HOLD fail-closed (nunca obs de relleno) ───────────────────────

class TestServeHoldFailClosed:
    @staticmethod
    def _assert_hold(monkeypatch, caplog, expect_log: str, h4=(), d1=(),
                     fail_timeframes=(), breaker_pretripped=False,
                     reject_log: str = "sin canales H4/D1 formados"):
        import logging
        startup = _model()
        if breaker_pretripped:
            feed_breaker.h4d_breaker_trip("manual: pre-test")
        with caplog.at_level(logging.ERROR, logger="routers.ai"):
            with _serve(monkeypatch, startup, _candles(240), h4=h4, d1=d1,
                        fail_timeframes=fail_timeframes) as (client, qwen_mock):
                hf_model = _hf_model(32)
                monkeypatch.setitem(
                    routers.ai._BRAINS, "EURUSD", _hf_brain(hf_model))
                resp = _post(client, "t30-hold")
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "HOLD"
        assert resp.json()["llm_bias"] == "ERROR"
        qwen_mock.assert_not_awaited()      # sin LLM si el obs no es servible
        hf_model.predict.assert_not_called()
        assert expect_log in caplog.text, caplog.text
        assert reject_log not in caplog.text, caplog.text

    def test_h4_fetch_outage_holds(self, monkeypatch, caplog):
        """MCP H4 caído (Exception de fetch) → sin serie → HOLD."""
        self._assert_hold(
            monkeypatch, caplog,
            h4=_h4_candles(), d1=_d1_candles(), fail_timeframes=("H4",),
            expect_log="T-30: H4/D1 no servibles",
        )

    def test_short_h4_series_holds(self, monkeypatch, caplog):
        """H4 con <205 velas (SMA200 + 5 de margen no formables) → HOLD."""
        self._assert_hold(
            monkeypatch, caplog,
            h4=_h4_candles(150), d1=_d1_candles(),
            expect_log="T-30: H4/D1 no servibles",
        )

    def test_broken_h4_series_holds(self, monkeypatch, caplog):
        """Hueco >168 h en H4: la serie no es continua → no servible → HOLD."""
        h4 = _h4_candles(260, end="2026-01-10T19:00")
        for row in h4[:130]:                          # más viejas: −20 días
            row["time"] = (pd.Timestamp(row["time"]) - pd.Timedelta(days=20)) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
        self._assert_hold(
            monkeypatch, caplog, h4=h4, d1=_d1_candles(),
            expect_log="T-30: H4/D1 no servibles",
        )

    def test_breaker_open_holds(self, monkeypatch, caplog):
        """Breaker H4/D1 en cooldown → res None → sin series → HOLD."""
        self._assert_hold(
            monkeypatch, caplog, h4=_h4_candles(), d1=_d1_candles(),
            breaker_pretripped=True,
            expect_log="T-30: H4/D1 no servibles",
        )

    def test_unformed_channels_in_obs_window_hold(self, monkeypatch, caplog):
        """Serie H4 parser-válida pero empezando tarde: la ventana del obs
        no tiene 200 velas H4 cerradas → canales NaN → HOLD distinguido
        («sin canales formados», no «no servibles»)."""
        self._assert_hold(
            monkeypatch, caplog,
            h4=_h4_candles(260, end="2026-02-22T19:00"), d1=_d1_candles(),
            expect_log="sin canales H4/D1 formados",
            reject_log="no servibles",
        )


# ─── 5. Contratos existentes intactos (default off ≡ byte-idéntico) ──────────

class TestExistingContractsUntouched:
    def test_config_default_is_off_and_brain_defaults_off(self):
        assert Settings.model_fields["higher_tf_features"].default is False
        brain = Brain(model=MagicMock(), vec_norm=None, bundle={})
        assert brain.trend_features is False
        assert brain.higher_tf_features is False

    def test_default_brain_obs_byte_identical_whether_hf_up_or_down(self, monkeypatch):
        """Contrato vivo (20,25): el bloque HF NO se ejecuta — la indisponi-
        bilidad H4/D1 no cambia un byte del obs (y el join jamás se llama)."""
        startup = _model()
        live_model = _model()
        h4 = list(reversed(_h4_candles(260)))
        d1 = list(reversed(_d1_candles(260)))
        with patch("routers.ai.join_higher_tf_channels",
                   side_effect=AssertionError("no debe ejecutarse")) as join_mock:
            with _serve(monkeypatch, startup, _candles(240), h4=h4, d1=d1) as (client, qwen_a):
                monkeypatch.setitem(
                    routers.ai._BRAINS, "EURUSD",
                    Brain(model=live_model, vec_norm=None, bundle={}, memory_channel=True),
                )
                r1 = _post(client, "t30-default-up")
                assert r1.status_code == 200, r1.text
                qwen_a.assert_awaited_once()
                obs_up = live_model.predict.call_args.args[0].copy()
            with _serve(monkeypatch, startup, _candles(240), fail_timeframes=("H4",)) as (client, qwen_b):
                monkeypatch.setitem(
                    routers.ai._BRAINS, "EURUSD",
                    Brain(model=live_model, vec_norm=None, bundle={}, memory_channel=True),
                )
                r2 = _post(client, "t30-default-down")
                assert r2.status_code == 200, r2.text
                qwen_b.assert_awaited_once()
                obs_down = live_model.predict.call_args.args[0].copy()
        join_mock.assert_not_called()
        assert obs_up.shape == (20, 25)
        assert obs_up.tobytes() == obs_down.tobytes()

    def test_trend_contract_is_not_gated_by_hf_outage(self, monkeypatch):
        """Contrato trend (20,29): el gating T-30 es SÓLO para brains con
        higher_tf_features — la caída H4/D1 no puede tumbar a T-22."""
        startup = _model()
        with _serve(monkeypatch, startup, _candles(240),
                    fail_timeframes=("H4",)) as (client, qwen_mock):
            trend_model = _hf_model(28)
            monkeypatch.setitem(
                routers.ai._BRAINS, "EURUSD",
                Brain(model=trend_model, vec_norm=None, bundle={},
                      memory_channel=True, trend_features=True),
            )
            resp = _post(client, "t30-trend-outage")
            assert resp.status_code == 200, resp.text
            qwen_mock.assert_awaited_once()
        obs = trend_model.predict.call_args.args[0]
        assert obs.shape == (20, 29)


# ─── 6. Env: flag, ValueError y columnas del obs ─────────────────────────────

def _hf_df(n: int = 300) -> pd.DataFrame:
    """df H1 engineered + canales H4/D1 unidos SIN NaN en todo su rango
    (H4/D1 lo bastante antiguos para que el SMA200 esté formado desde la
    primera fila H1)."""
    raw = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="h"),
        "open": 1.10,
        "high": 1.1004,
        "low": 1.0996,
        "close": 1.10 + np.arange(n) * 0.0002 + np.sin(np.arange(n) / 7.0) * 8e-4,
        "tick_volume": 1000 + np.arange(n),
        "spread": 1.5,
    })
    eng = engineer_market_features(raw)
    last_h1 = pd.Timestamp(eng["time"].iloc[-1])
    h4 = _h4_candles(320, end=(last_h1 + pd.Timedelta(hours=4))
                     .strftime("%Y-%m-%dT%H:%M:%SZ"))
    d1 = _d1_candles(300, end=last_h1.strftime("%Y-%m-%dT00:00:00Z"))
    out = join_higher_tf_channels(eng, h4, d1)
    assert not out[list(HIGHER_TF_MARKET_FEATURES)].isna().any().any(), \
        "fixture: el df HF debe estar completamente cubierto"
    return out


class TestEnvHigherTfContract:
    def test_hf_requires_trend_features(self):
        df = _hf_df()
        with pytest.raises(ValueError, match="exige trend_features=True"):
            ForexTradingEnvV2(df=df, higher_tf_features=True)

    def test_trend_hf_memory_gives_obs_20_33_with_hf_channels(self):
        """trend+hf+memoria → obs (20,32) y los canales 20..23 del obs son
        los valores del df en la ventana del episodio (paridad por
        construcción, mismo patrón que T-22)."""
        df = _hf_df()
        env = ForexTradingEnvV2(
            df=df, random_reset=False, memory_channel=True,
            trend_features=True, higher_tf_features=True,
        )
        assert env.observation_space.shape == (20, 33)
        obs = env.reset()[0]
        assert obs.shape == (20, 33) and obs.dtype == np.float32
        # G3: los canales HF van detrás del bloque de tendencia, que ahora
        # tiene 4 features ⇒ la posición se deriva de las constantes.
        _hf_pos = len(MARKET_FEATURES) + len(TREND_MARKET_FEATURES)
        for offset, col in enumerate(HIGHER_TF_MARKET_FEATURES):
            np.testing.assert_allclose(
                obs[:, _hf_pos + offset], df[col].iloc[1:21].values,
                rtol=1e-5, atol=1e-7,
            )
        assert abs(float(obs[-1, _hf_pos])) > 1e-5   # canal honesto, no relleno

    def test_default_env_ignores_hf_columns(self):
        """Flag off (default): el df puede traer las columnas H4/D1 y el env
        sigue en el contrato vivo (20,25) con los 17 canales base."""
        df = _hf_df()
        env = ForexTradingEnvV2(df=df, random_reset=False)
        assert env.observation_space.shape == (20, 25)
        assert env.features == list(MARKET_FEATURES)
        obs = env.reset()[0]
        assert obs.shape == (20, 25)

    def test_get_market_features_hf_exige_sus_columnas(self):
        """Sin las columnas H4/D1 el vector HF es un ValueError explícito —
        su ausencia NO puede rellenarse con ceros (mentira de estado)."""
        df = engineer_market_features(pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=300, freq="h"),
            "open": 1.10,
            "high": 1.1004,
            "low": 1.0996,
            "close": 1.10 + np.arange(300) * 0.0002,
        }))
        with pytest.raises(ValueError, match="include_higher_tf"):
            get_market_features(df, include_trend=True, include_higher_tf=True)
        # con las columnas: vector ordenado 17 + 4 (trend, G3) + 4 (HF)
        df_hf = join_higher_tf_channels(df, _h4_candles(320, end="2026-01-20T00:00"),
                                        _d1_candles(300, end="2026-01-18T00:00"))
        features = get_market_features(df_hf, include_trend=True,
                                       include_higher_tf=True)
        assert features == (list(MARKET_FEATURES)
                            + list(TREND_MARKET_FEATURES)
                            + list(HIGHER_TF_MARKET_FEATURES))
        assert len(features) == 25   # G3: el bloque trend pasó a 4 features


# ─── 7. Eval del retrain: contrato por ancho (31/32 y legacy) ────────────────

class TestEvaluateModelHigherTfContract:
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
        (32, (1, 20, 32)),      # G3: T-30 pre-memoria
        (33, (1, 20, 33)),      # G3: T-30 + memoria
        (25, (1, 20, 25)),      # contrato vivo: las columnas HF se IGNORAN
    ])
    def test_models_are_evaluated_in_their_own_env(self, width, expected):
        from services.auto_retrain import _evaluate_model
        df = _hf_df(300)
        model = self._recorder(width)
        metrics = _evaluate_model(model, df, self._cfg(df))
        assert model.seen and model.seen[0] == expected
        assert "trade_pnls" in metrics


# ─── 8. Train: fetch por timeframe + join sin NaN ────────────────────────────

_TRAIN_T0 = pd.Timestamp("2026-09-01")


def _train_h1(n: int = 800) -> list:
    """Velas H1 ISO ascendentes (mismo patrón que test_auto_retrain)."""
    rng = np.random.default_rng(7)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0005, n))
    out = []
    for i in range(n):
        c = float(close[i])
        o = float(close[i - 1]) if i else c
        out.append({
            "time": (_TRAIN_T0 + pd.Timedelta(hours=i)).isoformat(),
            "open": o,
            "high": max(o, c) + 0.0008,
            "low": min(o, c) - 0.0008,
            "close": c,
        })
    return out


def _train_h4(n: int = 260) -> list:
    """H4 que cubre la ventana H1 completa con SMA200 (+5 de pendiente)
    formado desde la PRIMERA fila H1: ≥830 h de margen previo → ≥209 barras
    cerradas antes de la fila 0 (tras engineer), holgadas para idx ≥ 204."""
    times = pd.date_range(_TRAIN_T0 - pd.Timedelta(hours=830), periods=n,
                          freq="4h")
    return [{"time": t.isoformat(), "open": 1.10, "high": 1.1008,
             "low": 1.0992, "close": round(1.10 + i * 0.0002, 5)}
            for i, t in enumerate(times)]


def _train_d1(n: int = 260) -> list:
    """D1 con ≥200 barras cerradas antes de la primera fila H1."""
    times = pd.date_range(_TRAIN_T0 - pd.Timedelta(hours=6000), periods=n,
                          freq="D")
    return [{"time": t.isoformat(), "open": 1.10, "high": 1.101,
             "low": 1.099, "close": round(1.10 + i * 0.0005, 5)}
            for i, t in enumerate(times)]


class TestDoRetrainHigherTfWiring:
    """_do_retrain con settings.higher_tf_features on/off (pre-registrado §3):
    fetch por timeframe, join antes del trim y CERO NaN tolerados."""

    @staticmethod
    def _wire_fetch(monkeypatch, ar, d1=None):
        calls = []

        async def _fetch(symbol, count, timeframe="H1"):
            calls.append((symbol, count, timeframe))
            return {
                "H1": _train_h1(800),
                "H4": _train_h4(),
                "D1": d1 if d1 is not None else _train_d1(),
            }[timeframe]

        monkeypatch.setattr(ar, "_fetch_historical_candles", _fetch)
        return calls

    @pytest.mark.asyncio
    async def test_train_fetches_hf_and_joins_zero_nan(self, tmp_path, monkeypatch):
        from tests.test_auto_retrain import TestDoRetrainPromotion
        ar, *_ = TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(ar.settings, "higher_tf_features", True)
        calls = self._wire_fetch(monkeypatch, ar)
        monkeypatch.setattr(ar, "_do_retrain_core", AsyncMock())

        await ar._do_retrain(symbol="EURUSD")

        assert calls == [("EURUSD", 30000, "H1"),
                         ("EURUSD", 7800, "H4"),
                         ("EURUSD", 1750, "D1")]
        df = ar._do_retrain_core.await_args.args[0]
        for col in HIGHER_TF_MARKET_FEATURES:
            assert col in df.columns
            assert int(df[col].isna().sum()) == 0, f"{col} con NaN"
        # join sin pérdida de filas + trim de warm-up (800 − 19 − 204)
        assert len(df) == 577
        # rampa alcista H4 → canal honesto con signo estable
        assert (df["dist_sma200_h4"].iloc[-20:] > 0).all()

    @pytest.mark.asyncio
    async def test_train_flag_off_fetches_only_h1(self, tmp_path, monkeypatch):
        from tests.test_auto_retrain import TestDoRetrainPromotion
        ar, *_ = TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(ar.settings, "higher_tf_features", False)
        calls = self._wire_fetch(monkeypatch, ar)
        monkeypatch.setattr(ar, "_do_retrain_core", AsyncMock())

        await ar._do_retrain(symbol="EURUSD")

        assert calls == [("EURUSD", 30000, "H1")]
        df = ar._do_retrain_core.await_args.args[0]
        for col in HIGHER_TF_MARKET_FEATURES:
            assert col not in df.columns

    @pytest.mark.asyncio
    async def test_train_short_d1_raises_before_experiment(self, tmp_path, monkeypatch):
        """Feed sin cobertura (D1 corta) → RuntimeError explícito ANTES del
        snapshot y de la fila del experimento: nunca se entrena a ciegas."""
        from tests.test_auto_retrain import TestDoRetrainPromotion
        ar, *_ = TestDoRetrainPromotion._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(ar.settings, "higher_tf_features", True)
        short_d1 = _train_d1(50)
        self._wire_fetch(monkeypatch, ar, d1=short_d1)
        monkeypatch.setattr(ar, "_do_retrain_core", AsyncMock())

        with pytest.raises(RuntimeError, match="T-30"):
            await ar._do_retrain(symbol="EURUSD")

        assert ar.experiment_tracker.start_experiment.await_count == 0


# ─── 9. G2: cero branching por símbolo ───────────────────────────────────────

class TestNoSymbolBranching:
    def test_the_three_t30_modules_have_no_symbol_conditionals(self):
        import ml.trading_env_v2 as env_mod
        import services.auto_retrain as ar_mod
        for mod in (routers.ai, ar_mod, env_mod):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "if symbol ==" not in src, mod.__name__
            assert "req.symbol ==" not in src, mod.__name__
