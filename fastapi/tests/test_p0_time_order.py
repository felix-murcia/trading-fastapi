"""
P0: orden temporal — el MCP entrega velas DESCENDENTES y el sistema entero
asume ascendente.

Hallazgo en vivo (2026-09-29, verificado contra el MCP real):
`http://…/market/candles/latest` devuelve las velas más nueva primero y
NINGUNA ruta las ordenaba (`engineer_market_features`, `routers/ai.py` y
`auto_retrain` trabajaban con el orden crudo). Consecuencias medidas:

- `df.iloc[-1]` ("precio/RSI/ATR actuales") = la vela de HACE 6.8 DÍAS
  (close 1.14479 servido como actual vs 1.13283 real; RSI 0.927 de una
  semana atrás en el prompt a Qwen).
- `raw_df['time'].iloc[-1]` para la hora de sesión = vela antigua → el
  blackout y la sesión (ASIA/EU/US) se decidían por la hora de hace días.
- `_build_qwen_candle_context(raw_df)` usaba `.tail(10)` = las velas más
  ANTIGUAS presentadas como "last 10 H1 candles (oldest→newest)".
- `returns` = pct_change de nuevo→viejo (signo invertido), `lag_return_*`
  = vela futura, split `iloc[:n]` del env y walk-forward = pasado/futuro
  invertidos (backtest en tiempo inverso).
- Train/serve skew desde el despliegue: `ml/train_ppo_v3.py` SÍ ordena su
  CSV (ascendente) y `training_experiments` está vacío (0 retrains) ⇒ el
  modelo se entrenó con features correctas y se servían espejadas.

Causa de que la suite no lo pillara: `_build_candles()` del harness E2E
generaba velas ASCENDENTES (nunca reproducía el orden de producción).

Fix: orden ascendente en el choke point `engineer_market_features` (pasan
por ahí inferencia, retrain, entrenamiento y tests) + lecturas de Qwen
(contexto/hora) desde el `df` ya ordenado + sort en `mt5_client.get_candles`.
Estos tests fallan ante cualquier reintroducción del orden descendente.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from gymnasium.spaces import Box

import services.mt5_client as mt5_client
from ml.trading_env_v2 import engineer_market_features

NEWEST_TIME = "2024-01-03T11:00:00Z"   # hora 11 — la más nueva del set
OLDEST_TIME = "2024-01-01T00:00:00Z"   # hora 0  — la más vieja


def _mcp_descending(n: int = 60) -> list[dict]:
    """Velas ISO horarias con closes crecientes, entregadas AL REVÉS
    (exactamente como el MCP real: más nueva primero)."""
    times = pd.date_range("2024-01-01", periods=n, freq="h")
    rows = []
    for i, t in enumerate(times):
        close = round(1.10 + i * 0.004, 5)
        rows.append({
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": round(close - 0.001, 5),
            "high": round(close + 0.002, 5),
            "low": round(close - 0.002, 5),
            "close": close,
            "tick_volume": 1000 + i,
        })
    return rows[::-1]   # DESCENDENTE = orden real del MCP


class TestEngineerSortsToAscending:
    def test_output_is_monotonic_and_current_row_is_newest(self):
        raw = pd.DataFrame(_mcp_descending())
        df = engineer_market_features(raw)

        assert df["time"].is_monotonic_increasing
        # iloc[-1] es "el actual" en todo el sistema: debe ser la vela más nueva
        assert df["time"].iloc[-1] == NEWEST_TIME
        # el calentamiento (rolling(20) → 19 filas) se descarta de las velas
        # VIEJAS, no de las nuevas (pre-fix: las 20 velas nuevas desaparecían
        # y "actual" era la más vieja)
        assert df["time"].iloc[0] == "2024-01-01T19:00:00Z"
        assert len(df) == 60 - 19

    def test_returns_keep_forward_sign(self):
        # closes estrictamente crecientes → tras ordenar, TODO pct_change > 0.
        # Con el orden descendente del MCP eran todas negativas (espejado).
        df = engineer_market_features(pd.DataFrame(_mcp_descending()))
        assert (df["returns"].iloc[1:] > 0).all()

    def test_rsi_of_rising_series_is_max(self):
        # RSI sobre la serie creciente = 1.0 (servido ×100 al prompt de Qwen).
        # Pre-fix (descendente) valía 0.0 — el "RSI actual" era el inverso.
        df = engineer_market_features(pd.DataFrame(_mcp_descending()))
        assert float(df["rsi14"].iloc[-1]) > 0.99

    def test_features_df_keeps_raw_ohlc_and_time(self):
        # _build_qwen_candle_context(df) necesita open/high/low/close/volume/time
        df = engineer_market_features(pd.DataFrame(_mcp_descending()))
        for col in ("time", "open", "high", "low", "close", "tick_volume"):
            assert col in df.columns

    def test_already_ascending_input_is_a_noop(self):
        asc = _mcp_descending()[::-1]   # ascendente
        df = engineer_market_features(pd.DataFrame(asc))
        assert df["time"].iloc[-1] == NEWEST_TIME
        assert df["time"].is_monotonic_increasing

    def test_df_without_time_column_is_untouched(self):
        # dfs sintéticos sin columna time → sin sort (y sin reventar):
        # engineer exige OHLC, como siempre
        n = 30
        closes = [round(1.1 + i * 0.01, 5) for i in range(n)]
        raw = pd.DataFrame({
            "open": closes,
            "high": [c + 0.005 for c in closes],
            "low": [c - 0.005 for c in closes],
            "close": closes,
        })
        df = engineer_market_features(raw)
        assert len(df) > 0
        assert df["close"].is_monotonic_increasing   # orden original intacto


class TestMt5ClientCandlesOrder:
    @pytest.mark.asyncio
    async def test_get_candles_returns_ascending_epoch(self):
        with patch("services.mt5_client._get",
                   AsyncMock(return_value=_mcp_descending(n=5))):
            out = await mt5_client.get_candles("EURUSD", "H1", 5)

        times = [c["time"] for c in out]
        assert times == sorted(times)                      # ascendente
        assert all(isinstance(t, int) for t in times)       # epoch normalizado
        assert len(out) == 5


# ─── E2E /predict con velas en orden de PRODUCCIÓN (descendente) ─────────────

def _qwen_ok() -> dict:
    return {
        "ok": True, "quality": 8.5, "reason": "Strong setup",
        "bias": "BULLISH", "confidence_modifier": 1.1,
        "effective_prob": 0.7, "sl_adjusted": 0.20, "tp_adjusted": 0.28,
        "regime": "TRENDING",
    }


class _DescendingCandleClient:
    """FakeAsyncClient con velas en orden real del MCP (más nueva primero)."""

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        if "/api/v1/account/info" in url:
            return self._response({"equity": 1000.0, "balance": 1000.0})
        if "/api/v1/market/candles/latest" in url:
            return self._response(_mcp_descending())
        return self._response({})

    async def post(self, url, **kwargs):
        return self._response({})

    @staticmethod
    def _response(payload, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = ""
        return response


class TestPredictServesNewestCandle:
    def test_qwen_receives_newest_values_not_a_week_old(self):
        """El /predict con velas descendentes (orden real del MCP) debe
        servir a Qwen la hora/RSI/contexto de la vela MÁS NUEVA.

        Pre-fix: hour = hora de la vela más vieja (0), RSI = 0.0 (serie
        espejada) y contexto = 10 velas antiguas con return negativo.
        """
        model = MagicMock()
        model.predict.return_value = (np.array([0.60, 0.18, 0.15, 0.15],
                                                dtype=np.float32), None)
        model.observation_space = Box(-np.inf, np.inf, shape=(20, 24),
                                      dtype=np.float32)
        model.action_space = Box(-1.0, 1.0, shape=(4,), dtype=np.float32)
        dist = MagicMock()
        dist.distribution.mean.numpy.return_value = np.array(
            [0.1, 0.1, 0.1, 0.1], dtype=np.float32)
        dist.distribution.stddev.numpy.return_value = np.array(
            [0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        model.policy.get_distribution.return_value = dist

        from main import app

        with patch("main.init_pool", AsyncMock()), \
             patch("main.close_pool", AsyncMock()), \
             patch("routers.ai.is_news_blackout",
                   AsyncMock(return_value=(False, None))), \
             patch("routers.ai.get_pool",
                   return_value=MagicMock(execute=AsyncMock())), \
             patch("routers.ai.PPO.load", return_value=model), \
             patch("services.news_scraper.get_macro_news",
                   AsyncMock(return_value="No macro news")), \
             patch("routers.ai.query_qwen_unified",
                   AsyncMock(return_value=_qwen_ok())) as qwen_mock, \
             patch("httpx.AsyncClient", _DescendingCandleClient):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/v1/ai/predict",
                    json={"symbol": "EURUSD", "timeframe": "H1", "position": 0},
                    headers={"x-internal-token": "test-token",
                             "x-cycle-id": "p0-order-1"},
                )

        assert resp.status_code == 200, resp.text
        kw = qwen_mock.call_args.kwargs
        # 1) La hora de sesión es la de la vela más NUEVA (11), no la de la
        #    más vieja (0) — de aquí dependían blackout y clasificación de sesión.
        assert kw["hour"] == 11
        # 2) RSI de la serie creciente (≈100): pre-fix, con la serie espejada,
        #    llegaba 0.0.
        assert kw["rsi"] >= 90.0
        # 3) Contexto de velas = las 10 MÁS RECIENTES con return positivo;
        #    pre-fix eran las 10 más viejas (return negativo).
        assert "return=+" in kw["candle_context"]
        assert "oldest→newest" in kw["candle_context"]
